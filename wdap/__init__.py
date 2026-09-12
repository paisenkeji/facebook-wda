#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function, unicode_literals

#: 与 pyproject.toml 中的 version 保持一致
__version__ = "0.2.5"


import base64
import contextlib
import enum
import functools
import http.client
import io
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, namedtuple
from typing import Callable, Optional, Union, Dict, NamedTuple, List
from urllib.parse import urlparse

import retry
import six
from deprecated import deprecated

from wdap import xcui_element_types
from wdap._proto import *
from wdap.cv import CV, CVResult, CVMatch, CVStatus
from wdap.log import (Log, LogCategory, LogConfig, LogCrashReport, LogEntry,
                      LogLevel, LogSnapshot, LogStats, LogUnsupportedError)
from wdap.exceptions import *
from wdap.usbmux import UsbmuxPortForwarder, close_pool, fetch
from wdap.usbmux.exceptions import HTTPError as MuxHTTPError
from wdap.usbmux.exceptions import MuxConnectError as MuxRelayRefusedError
from wdap.usbmux.exceptions import MuxConnectToUsbmuxdError
from wdap.usbmux.exceptions import MuxError as MuxTransportError
# wdap.exceptions 里遗留着一份**同名但运行时从不抛出**的 MuxError / MuxConnectError
# （上面 `from wdap.exceptions import *` 带进来的）。它会造成
# `except wdap.MuxConnectError` 永远抓不到真实异常这种坑，这里显式覆盖成真正会抛的那份。
from wdap.usbmux.exceptions import MuxConnectError, MuxError  # noqa: F401
from wdap.usbmux.pyusbmux import list_devices, select_device
from wdap.utils import inject_call, limit_call_depth, AttrDict, convert
# 设备激活（仅 USB）：对标 go-ios 的 `ios activate`，走 lockdown + mobileactivationd
from wdap.activation import (ActivationResult, activate_all_usb_devices,
                             activate_device, activation_state,
                             is_device_activated)
# 拉起 WDA（注意与上面的"设备激活"是两回事）：iOS<=16 tidevice / iOS>=17 go-ios
from wdap.wda_launch import (ALL_STRATEGIES as ALL_WDA_STRATEGIES,
                             ALL_TUNNEL_MODES, WdaLaunchResult, ensure_tunnel,
                             mount_developer_image, parse_ios_version,
                             pick_backend, read_ios_version, start_wda,
                             tunnel_agent_alive)
from wdap.lockdown import (LOCKDOWN_PORT, LockdownClient, PairRecord,
                           list_usb_devices, load_pair_record)

try:
    from functools import cached_property  # Python3.8+
except ImportError:
    from cached_property import cached_property

try:
    import sys

    import logzero
    if not (hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()):
        log_format = '[%(levelname)1.1s %(asctime)s %(module)s:%(lineno)d] %(message)s'
        logzero.setup_default_logger(formatter=logzero.LogFormatter(
            fmt=log_format))
    logger = logzero.logger
except ImportError:
    logger = logging.getLogger("facebook-wdap")  # default level: WARNING

DEBUG = False
HTTP_TIMEOUT = 180.0  # unit second
DEVICE_WAIT_TIMEOUT = 180.0  # wait ready

# --------------------------------------------------------------------------- #
# 传输层瞬态错误自动恢复（连接被重置 / 中断 / 对端提前关闭）
#
# 背景：每次 HTTP 请求都会新建一条连接（http+usbmux 下是一条 usbmux 隧道）。
# 长跑时若隧道持续累积，设备端 HTTP 服务或 usbmuxd 达到并发上限后会把新连接 RST 掉，
# 表现为 WinError 10054 "远程主机强迫关闭了一个现有的连接"。
# 关闭泄漏已由 usbmux.fetch 的 finally 保证，这里再补一层"这类错误可自愈"。
# --------------------------------------------------------------------------- #
#: 传输层错误的总尝试次数（1 = 不重试；2 = 失败后重试 1 次）
HTTP_TRANSPORT_TRIES = 2
#: 两次尝试之间的基础间隔（秒），实际按 0.5s、1.0s... 递增
HTTP_TRANSPORT_RETRY_DELAY = 0.5
#: 默认只对幂等方法重试。POST 在 WDA 里多为点击/输入/滑动等动作，重复执行有副作用；
#: 若确认自己的 POST 都是查询类（如 findElement），可置为 True
HTTP_TRANSPORT_RETRY_ON_POST = False

LANDSCAPE = 'LANDSCAPE'
PORTRAIT = 'PORTRAIT'
LANDSCAPE_RIGHT = 'UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT'
PORTRAIT_UPSIDEDOWN = 'UIA_DEVICE_ORIENTATION_PORTRAIT_UPSIDEDOWN'


class Status(enum.IntEnum):
    # 不是怎么准确，status在mds平台上变来变去的
    UNKNOWN = 100  # other status
    ERROR = 110


class Callback(str, enum.Enum):
    ERROR = "::error"
    HTTP_REQUEST_BEFORE = "::http-request-before"
    HTTP_REQUEST_AFTER = "::http-request-after"

    RET_RETRY = "::retry"  # Callback return value
    RET_ABORT = "::abort"
    RET_CONTINUE = "::continue"

    # Old implement
    # return namedtuple('GenericDict', list(dictionary.keys()))(**dictionary)


def urljoin(*urls):
    """
    The default urlparse.urljoin behavior look strange
    Standard urlparse.urljoin('http://a.com/foo', '/bar')
    Expect: http://a.com/foo/bar
    Actually: http://a.com/bar

    This function fix that.
    """
    return '/'.join([u.strip("/") for u in urls])


def roundint(i):
    return int(round(i, 0))


def namedlock(name):
    """
    Returns:
        threading.Lock
    """
    if not hasattr(namedlock, 'locks'):
        namedlock.locks = defaultdict(threading.Lock)
    return namedlock.locks[name]


def httpdo(url, method="GET", data=None, timeout=None) -> AttrDict:
    """
    thread safe http request

    Raises:
        WDAError, WDARequestError, WDAEmptyResponseError
    """
    p = urlparse(url)
    with namedlock(p.scheme + "://" + p.netloc):
        return _unsafe_httpdo(url, method, data, timeout)


#: 认定为"传输层"的异常类型（区别于 WDA 返回的业务错误，后者不该重试）
_TRANSPORT_ERRORS = (
    ConnectionResetError,            # WinError 10054 / ECONNRESET
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,                    # Python 3.10+ 起 socket.timeout 即 TimeoutError
    socket.timeout,
    http.client.RemoteDisconnected,  # 对端在响应前就关闭
    http.client.BadStatusLine,
    http.client.ResponseNotReady,
)

#: 幂等方法，重试不会产生副作用
_TRANSPORT_SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "DELETE")


def _unwrap_transport_error(err: BaseException) -> BaseException:
    """usbmux 用 ``raise HTTPError(e)`` 把底层异常塞进 args[0]，这里剥出真实原因"""
    for _ in range(5):
        if not isinstance(err, MuxHTTPError) or not err.args:
            break
        inner = err.args[0]
        if not isinstance(inner, BaseException):
            break
        err = inner
    return err


def _is_transport_error(err: BaseException) -> bool:
    """是否为可安全重试的传输层瞬态错误"""
    return isinstance(_unwrap_transport_error(err), _TRANSPORT_ERRORS)


def _explain_probe_error(err: BaseException) -> str:
    """
    把"探测 WDA 是否就绪"失败的原因翻译成人能看懂的提示。

    ``is_ready()`` 吞异常的设计会让人误以为"WDA 没启动"，实际可能是设备没插、
    usbmuxd 没跑、端口不对或是隧道被重置。这里把这几类区分开。
    """
    inner = _unwrap_transport_error(err)
    if isinstance(inner, MuxConnectToUsbmuxdError):
        return ("连不上 usbmuxd（Windows 需 Apple Mobile Device Service 监听 127.0.0.1:27015；"
                "macOS/Linux 为 /var/run/usbmuxd）")
    if isinstance(inner, MuxRelayRefusedError):
        return "usbmuxd 拒绝中继到设备端口：设备上 8100 没有服务在监听（WDA 未真正启动，或端口不是 8100）"
    if isinstance(inner, AttributeError):
        return "usbmuxd 没有枚举到该设备（已拔出 / 未点信任 / 仅网络连接）"
    if isinstance(inner, _TRANSPORT_ERRORS):
        return ("usbmux 隧道被重置/中止（{}）。常见诱因：设备上该端口没有服务在监听、"
                "usbmuxd 通道被其它工具（tidevice/iTunes/爱思助手）争用、或 USB 连接不稳"
                "（重新插拔 / 换线 / 换口可验证）").format(inner)
    if isinstance(inner, MuxTransportError):
        return "usbmux 通道异常"
    return "{}: {}".format(type(inner).__name__, inner)


def _control_channel_alive(udid: str = "") -> bool:
    """
    usbmuxd 的**控制通道**是否仍然正常（还能枚举到设备）。

    隧道被 RST 时靠它区分两种性质完全不同的故障：

    * 控制通道正常 → usbmuxd 活着、设备在，只是"设备上那个端口"连不上（WDA 没起来），
      这时去激活 WDA 是有意义的；
    * 控制通道也不通 → usbmux 通道本身坏了（usbmuxd 异常 / 设备失联 / 被别的工具独占），
      启动 WDA 纯属徒劳。
    """
    try:
        devices = list_devices()
    except Exception:  # noqa: BLE001 - 这里就是要用一个探针判断通道是否可用
        return False
    if not udid:
        return bool(devices)
    return any(dev.matches_udid(udid) for dev in devices)


def _probe_error_is_device_channel(err: BaseException, udid: str = "") -> bool:
    """
    探测失败是否发生在"设备/usbmuxd 通道"这一层。

    这一层坏了，再启动 WDA 也没用（tidevice 同样连不上），应直接报错而不是徒劳激活。
    注意：relay 被明确拒绝（usbmuxd 回 CONNREFUSED，即设备在线但该端口无监听）
    恰恰说明"WDA 真没起来"，属于值得激活的情况。

    ``udid`` 用于在"隧道被 RST"这种模糊场景下探一次控制通道来自证是哪一侧的问题。
    """
    inner = _unwrap_transport_error(err)
    if isinstance(inner, MuxConnectToUsbmuxdError):
        # 连 usbmuxd 都失败（注意它是 MuxConnectError 的子类，必须排在前面判断）
        return True
    if isinstance(inner, MuxRelayRefusedError):
        # usbmuxd 明确拒绝中继 → 设备在线、端口没人听 → 值得去激活
        return False
    if isinstance(inner, AttributeError):
        # select_device 返回 None，设备没被枚举到
        return True
    if isinstance(inner, _TRANSPORT_ERRORS):
        # 隧道被 RST/中止/超时。两种可能：设备端口没开（可激活），或通道坏了（激活无用）。
        # 判据：控制通道还活着吗？活着说明通道没问题，问题在设备端口。
        return not _control_channel_alive(udid)
    return isinstance(inner, MuxTransportError)


def _tail_file(path: str, limit: int = 400) -> str:
    """读文件尾部若干字符，用于把子进程失败原因带进日志"""
    try:
        with open(path, "rb") as fp:
            data = fp.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace").strip()
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text


def _fetch_with_retry(url: str, method: str, data, timeout):
    """
    发一次 HTTP 请求；遇到传输层瞬态错误时丢弃旧连接、重建后重试。

    fetch() 每次都新建连接（并在 finally 里关闭），所以重试天然走一条全新的
    usbmux 隧道，这正是 RST 场景需要的恢复手段。
    """
    method_upper = (method or "GET").upper()
    retryable = (HTTP_TRANSPORT_RETRY_ON_POST
                 or method_upper in _TRANSPORT_SAFE_METHODS)
    tries = max(1, int(HTTP_TRANSPORT_TRIES)) if retryable else 1

    last_err = None
    for index in range(tries):
        try:
            return fetch(url, method, data, timeout)
        except Exception as err:
            if not _is_transport_error(err) or index == tries - 1:
                raise
            last_err = err
            delay = HTTP_TRANSPORT_RETRY_DELAY * (index + 1)
            logger.debug("transport error on %s %s (%s), retry %d/%d after %.1fs",
                         method_upper, url, err, index + 1, tries - 1, delay)
            time.sleep(delay)
    raise last_err  # pragma: no cover


def _unsafe_httpdo(url: str, method='GET', data=None, timeout=None):
    """
    Do HTTP Request
    """
    start = time.time()
    if DEBUG:
        body = json.dumps(data) if data else ''
        print("Shell$ curl -X {method} -d '{body}' '{url}'".format(
            method=method.upper(), body=body or '', url=url))

    if timeout is None:
        timeout = HTTP_TIMEOUT
    response = _fetch_with_retry(url, method, data, timeout)
    if response.status_code == 502:  # Bad Gateway
        raise WDABadGateway(response.status_code, response.text)
    if DEBUG:
        ms = (time.time() - start) * 1000
        response_text = response.text
        if url.endswith("/screenshot"):
            response_text = response_text[:100] + "..." # limit length of screenshot response
        print('Return ({:.0f}ms): {}'.format(ms, response_text))

    try:
        retjson = response.json()
        retjson['status'] = retjson.get('status', 0)
        r = convert(retjson)

        if isinstance(r.value, dict) and r.value.get("error"):
            status = Status.ERROR
            value = r.value.copy()
            value.pop("traceback", None)

            for errCls in (WDAInvalidSessionIdError, WDAPossiblyCrashedError, WDAKeyboardNotPresentError, WDAUnknownError, WDAStaleElementReferenceError):
                if errCls.check(value):
                    raise errCls(status, value)
            raise WDARequestError(status, value)
        return r
    except JSONDecodeError:
        if response.text == "":
            raise WDAEmptyResponseError(method, url, data)
        raise WDAError(method, url, response.text[:100] + "...") # should not too long


class Rect(list):
    def __init__(self, x, y, width, height):
        super().__init__([x, y, width, height])
        self.__dict__.update({
            "x": x,
            "y": y,
            "width": width,
            "height": height
        })

    def __str__(self):
        return 'Rect(x={x}, y={y}, width={w}, height={h})'.format(
            x=self.x, y=self.y, w=self.width, h=self.height)

    def __repr__(self):
        return str(self)

    @property
    def center(self):
        return namedtuple('Point', ['x', 'y'])(self.x + self.width // 2,
                                               self.y + self.height // 2)

    @property
    def origin(self):
        return namedtuple('Point', ['x', 'y'])(self.x, self.y)

    @property
    def left(self):
        return self.x

    @property
    def top(self):
        return self.y

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height


def _start_wda_xctest(udid: str,
                      wda_bundle_id=None,
                      strategy: str = "auto",
                      wda_port=None,
                      fallback: bool = True,
                      tidevice_path=None,
                      goios_path=None,
                      startup_wait: float = 3.0) -> bool:
    """按 iOS 版本选择后端拉起 WDA（保留的薄封装，只返回成功与否）

    真正的实现在 ``wdap.wda_launch.start_wda``，返回值里带有后端名、
    iOS 版本、命令行和日志路径，排查时用它更好。

    Note:
        iOS 17+ 的 testmanagerd 换成了 RemoteXPC，tidevice 会在挂载开发者
        镜像阶段失败（DeveloperImage not found），所以 iOS 17+ 必须走
        ``go-ios runwda``。``strategy="auto"`` 会读 ``ProductVersion`` 自动选。
    """
    return start_wda(udid,
                     wda_bundle_id=wda_bundle_id,
                     strategy=strategy,
                     wda_port=wda_port,
                     fallback=fallback,
                     tidevice_path=tidevice_path,
                     goios_path=goios_path,
                     startup_wait=startup_wait).ok


class BaseClient(object):
    def __init__(self, url=None, _session_id=None):
        """
        Args:
            target (string): the device url

        If target is empty, device url will set to env-var "DEVICE_URL" if defined else set to "http://localhost:8100"
        """
        if not url:
            url = os.environ.get('DEVICE_URL', 'http://localhost:8100')
        assert re.match(r"^(http\+usbmux|https?)://", url), "Invalid URL: %r" % url

        # Session variable
        self.__wda_url = url
        self.__session_id = _session_id
        self.__is_app = bool(_session_id)  # set to freeze session_id
        self.__timeout = 30.0
        self.__callbacks = defaultdict(list)
        self.__callback_depth = 0
        self.__callback_running = False

        if not _session_id:
            self._init_callback()

        # u = urllib.parse.urlparse(self.__wda_url)
        # if u.scheme == "http+usbmux" and not self.is_ready():
        #     udid = u.netloc.split(":")[0]
        #     if _start_wda_xctest(udid):
        #         self.wait_ready()
                # raise RuntimeError("xctest start failed")

    def _callback_fix_invalid_session_id(self, err: WDAError):
        """ 当遇到 invalid session id错误时，更新session id并重试 """
        if isinstance(err, WDAInvalidSessionIdError):  # and not self.__is_app:
            self.session_id = None
            return Callback.RET_RETRY
        if isinstance(err, WDAPossiblyCrashedError):
            self.session_id = self.session().session_id  # generate new sessionId
            return Callback.RET_RETRY
        """ 等待设备恢复上线 """

    def _init_callback(self):
        self.register_callback(Callback.ERROR,
                               self._callback_fix_invalid_session_id)

    def _callback_json_report(self, method, urlpath):
        """ TODO: ssx """
        pass

    def _set_output_report(self, filename: str):
        """
        Args:
            filename: json log
        """
        self.register_callback(
            Callback.HTTP_REQUEST_BEFORE, self._callback_json_report)

    def probe(self, timeout: float = 3.0):
        """
        探测 WDA 是否就绪，**并把失败原因带回来**（``is_ready`` 会把原因吞掉）。

        Returns:
            (bool, object): 成功 ``(True, status_value)``；失败 ``(False, exception)``

        Example:
            ok, info = c.probe()
            if not ok:
                print("WDA 不可用:", info)
        """
        try:
            return True, self.http.get("status", timeout=timeout)
        except Exception as err:  # noqa: BLE001 - 探测就是要拿到任意原因
            return False, err

    def is_ready(self, timeout: float = 3.0, tries: int = 1, delay: float = 0.3) -> bool:
        """
        探测 WDA 是否就绪。

        Args:
            timeout: 单次请求超时（秒）。http+usbmux 首次请求要先建隧道，
                设得太短会把"建连慢"误判成"WDA 没启动"。
            tries: 尝试次数。>1 时对瞬态失败自动重试，避免一次网络抖动被误判。
            delay: 重试间隔（秒）。

        Note:
            只返回 True/False。需要知道**为什么**不可用时请用 :meth:`probe`。
        """
        tries = max(1, int(tries))
        for index in range(tries):
            try:
                self.http.get("status", timeout=timeout)
                return True
            except Exception as err:  # noqa: BLE001
                if index == tries - 1:
                    logger.debug("probe %r failed: %s", self.__wda_url, err)
                    return False
                time.sleep(delay)
        return False  # pragma: no cover

    def wait_ready(self, timeout=120, noprint=False) -> bool:
        """
        wait until WDA back to normal

        Returns:
            bool (if wdap works)
        """
        deadline = time.time() + timeout

        def _dprint(message: str):
            if noprint:
                return
            print("facebook-wdap", time.ctime(), message)

        _dprint("Wait ready (timeout={:.1f})".format(timeout))
        last_err = None
        while time.time() < deadline:
            ok, info = self.probe()
            if ok:
                _dprint("device back online")
                return True
            last_err = info
            _dprint("{!r} wait_ready left {:.1f} seconds".format(self.__wda_url, deadline - time.time()))
            time.sleep(1.0)
        # 把最后一次失败原因打出来：否则"device still offline"完全无法定位
        _dprint("device still offline: {}".format(_explain_probe_error(last_err)
                                                  if last_err else "unknown"))
        return False

    @retry.retry(exceptions=WDAEmptyResponseError, tries=3, delay=2)
    def status(self):
        res = self.http.get('status')
        res["value"]['sessionId'] = res.get("sessionId")
        # Can't use res.value['sessionId'] = ...
        return res.value

    def register_callback(self, event_name: str, func: Callable, try_first: bool = False):
        if try_first:
            self.__callbacks[event_name].insert(0, func)
        else:
            self.__callbacks[event_name].append(func)

    def unregister_callback(self,
                            event_name: Optional[str] = None,
                            func: Optional[Callable] = None):
        """ 反注册 """
        if event_name is None:
            self.__callbacks.clear()
        elif func is None:
            self.__callbacks[event_name].clear()
        else:
            self.__callbacks[event_name].remove(func)

    def _run_callback(self, event_name, callbacks,
                      **kwargs) -> Union[None, Callback]:
        """ 运行回调函数 """
        if not callbacks:
            return

        self.__callback_running = True
        try:
            for fn in callbacks[event_name]:
                ret = inject_call(fn, **kwargs)
                if ret in [
                        Callback.RET_RETRY, Callback.RET_ABORT,
                        Callback.RET_CONTINUE
                ]:
                    return ret
        finally:
            self.__callback_running = False

    @property
    def callbacks(self):
        return self.__callbacks

    @limit_call_depth(4)
    def _fetch(self,
               method: str,
               urlpath: str,
               data: Optional[dict] = None,
               with_session: bool = False,
               timeout: Optional[float] = None) -> AttrDict:
        """ do http request """
        urlpath = "/" + urlpath.lstrip("/")  # urlpath always startswith /

        callbacks = self.__callbacks

        if self.__callback_running:
            callbacks = None

        url = urljoin(self.__wda_url, urlpath)

        run_callback = functools.partial(self._run_callback,
                                         callbacks=callbacks,
                                         method=method,
                                         url=url,
                                         urlpath=urlpath,
                                         with_session=with_session,
                                         data=data,
                                         client=self)

        try:
            if with_session:
                url = urljoin(self.__wda_url, "session", self.session_id,
                              urlpath)
            run_callback(Callback.HTTP_REQUEST_BEFORE)
            response = httpdo(url, method, data, timeout)
            run_callback(Callback.HTTP_REQUEST_AFTER, response=response)
            return response
        except Exception as err:
            ret = run_callback(Callback.ERROR, err=err)
            if ret == Callback.RET_RETRY:
                return self._fetch(method, urlpath, data, with_session)
            elif ret == Callback.RET_CONTINUE:
                return
            else:
                raise

    @property
    def http(self):
        return namedtuple("HTTPRequest", ['fetch', 'get', 'post'])(
            self._fetch,
            functools.partial(self._fetch, "GET"),
            functools.partial(self._fetch, "POST"))  # yapf: disable

    @property
    def _session_http(self):
        return namedtuple("HTTPSessionRequest", ['fetch', 'get', 'post', 'delete'])(
            functools.partial(self._fetch, with_session=True),
            functools.partial(self._fetch, "GET", with_session=True),
            functools.partial(self._fetch, "POST", with_session=True),
            functools.partial(self._fetch, "DELETE", with_session=True))  # yapf: disable

    @property
    def wda_url(self) -> str:
        """当前连接的 WDA 基址，如 ``http+usbmux://<udid>:8100``"""
        return self.__wda_url

    def _fetch_raw(self,
                   method: str,
                   urlpath: str,
                   data: Optional[dict] = None,
                   timeout: Optional[float] = None):
        """取回**原始响应**，不做 WDA 的 JSON 信封解析

        :meth:`_fetch` 总是 ``response.json()``，而少数端点（如
        ``/wda/log/download``）返回的是 ``text/plain``，走通用通道会被当成
        JSON 解析失败。这里直接返回带 ``.text`` / ``.status_code`` 的响应包装。
        """
        urlpath = "/" + urlpath.lstrip("/")  # urlpath always startswith /
        url = urljoin(self.__wda_url, urlpath)
        return _fetch_with_retry(url, method, data, timeout)

    def home(self):
        """Press home button"""
        try:
            self.http.post('/wda/homescreen')
        except WDARequestError as e:
            if "Timeout waiting until SpringBoard is visible" in str(e):
                return
            raise

    def healthcheck(self):
        """Hit healthcheck"""
        return self.http.get('/wda/healthcheck')

    def locked(self) -> bool:
        """ returns locked status, true or false """
        return self.http.get("/wda/locked").value

    def lock(self):
        return self.http.post('/wda/lock')

    def unlock(self):
        """ unlock screen, double press home """
        return self.http.post('/wda/unlock')

    def sleep(self, secs: float):
        """ same as time.sleep """
        time.sleep(secs)

    @retry.retry(WDAUnknownError, tries=3, delay=.5, jitter=.2)
    def app_current(self) -> dict:
        """
        Returns:
            dict, eg:
            {"pid": 1281,
             "name": "",
             "bundleId": "com.netease.cloudmusic"}
        """
        return self.http.get("/wda/activeAppInfo").value

    def source(self, format='xml', accessible=False):
        """
        Args:
            format (str): only 'xml' and 'json' source types are supported
            accessible (bool): when set to true, format is always 'json'
        """
        if accessible:
            return self.http.get('/wda/accessibleSource').value
        return self.http.get('source?format=' + format).value

    def screenshot(self, png_filename=None, format='pillow'):
        """
        Screenshot with PNG format

        Args:
            png_filename(string): optional, save file name
            format(string): return format, "raw" or "pillow” (default)

        Returns:
            PIL.Image or raw png data

        Raises:
            WDARequestError
        """
        value = self.http.get('screenshot').value
        raw_value = base64.b64decode(value)
        png_header = b"\x89PNG\r\n\x1a\n"
        if not raw_value.startswith(png_header) and png_filename:
            raise WDARequestError(-1, "screenshot png format error")

        if png_filename:
            with open(png_filename, 'wb') as f:
                f.write(raw_value)

        if format == 'raw':
            return raw_value
        elif format == 'pillow':
            from PIL import Image
            buff = io.BytesIO(raw_value)
            im = Image.open(buff)
            return im.convert("RGB") # convert to RGB to fix save jpeg error
        else:
            raise ValueError("unknown format")

    def session(self,
                bundle_id=None,
                arguments: Optional[list] = None,
                environment: Optional[dict] = None,
                alert_action: Optional[AlertAction] = None):
        """
        Launch app in a session

        Args:
            - bundle_id (str): the app bundle id
            - arguments (list): ['-u', 'https://www.google.com/ncr']
            - enviroment (dict): {"KEY": "VAL"}
            - alert_action (AlertAction): AlertAction.ACCEPT or AlertAction.DISMISS

        WDA Return json like

        {
            "value": {
                "sessionId": "69E6FDBA-8D59-4349-B7DE-A9CA41A97814",
                "capabilities": {
                    "device": "iphone",
                    "browserName": "部落冲突",
                    "sdkVersion": "9.3.2",
                    "CFBundleIdentifier": "com.supercell.magic"
                }
            },
            "sessionId": "69E6FDBA-8D59-4349-B7DE-A9CA41A97814",
            "status": 0
        }

        To create a new session, send json data like

        {
            "capabilities": {
                "alwaysMatch": {
                    "bundleId": "your-bundle-id",
                    "app": "your-app-path"
                    "shouldUseCompactResponses": (bool),
                    "shouldUseTestManagerForVisibilityDetection": (bool),
                    "maxTypingFrequency": (integer),
                    "arguments": (list(str)),
                    "environment": (dict: str->str)
                }
            },
        }

        Or {"capabilities": {}}
        """
        # if not bundle_id:
        #     # 旧版的WDA创建Session不允许bundleId为空，但是总是可以拿到sessionId
        #     # 新版的WDA允许bundleId为空，但是初始状态没有sessionId
        #     session_id = self.status().get("sessionId")
        #     if session_id:
        #         return self

        capabilities = {}
        if bundle_id:
            always_match = {
                "bundleId": bundle_id,
                "arguments": arguments or [],
                "environment": environment or {},
                "shouldWaitForQuiescence": False,
            }
            if alert_action:
                assert alert_action in ["accept", "dismiss"]
                capabilities["defaultAlertAction"] = alert_action

            capabilities['alwaysMatch'] = always_match

        payload = {
            "capabilities": capabilities,
            "desiredCapabilities": capabilities.get('alwaysMatch',
                                                    {}),  # 兼容旧版的wda
        }

        # when device is Locked, it is unable to start app
        if self.locked():
            self.unlock()
        try:
            res = self.http.post('session', payload)
        except WDAEmptyResponseError:
            """ when there is alert, might be got empty response
            use /wda/apps/state may still get sessionId
            """
            res = self.session().app_state(bundle_id)
            if res.value != 4:
                raise
        client = Client(self.__wda_url, _session_id=res.sessionId)
        client.__timeout = self.__timeout
        client.__callbacks = self.__callbacks
        return client


    '''
    TODO: Should the ctx of the client be written back after this code is executed,\
    as the session ID is already empty when delete session api trigger.
    '''
    def close(self):
        '''Close created session which session id saved in class ctx.'''
        try:
            return self._session_http.delete('/')
        except WDARequestError as e:
            if not isinstance(e, (WDAInvalidSessionIdError, WDAPossiblyCrashedError)):
                raise

    #@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@#
    ######  Session methods and properties ######
    #@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@#
    def __enter__(self):
        """
        Usage example:
            with c.session("com.example.app") as app:
                # do something
        """
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @property
    @deprecated(version="1.0.0", reason="Use session_id instread id")
    def id(self):
        return self._get_session_id()

    @property
    def session_id(self) -> str:
        if self.__session_id:
            return self.__session_id
        current_sid = self.status()['sessionId']
        if current_sid:
            self.__session_id = current_sid  # store old session id to reduce request count
            return current_sid
        return self.session().session_id

    @session_id.setter
    def session_id(self, value):
        self.__session_id = value

    def _get_session_id(self) -> str:
        return self.session_id

    @cached_property
    def scale(self) -> int:
        """
        UIKit scale factor

        Refs:
            https://developer.apple.com/library/archive/documentation/DeviceInformation/Reference/iOSDeviceCompatibility/Displays/Displays.html
        There is another way to get scale
            self._session_http.get("/wda/screen").value returns {"statusBarSize": {'width': 320, 'height': 20}, 'scale': 2}
        """
        try:
            return self._session_http.get("/wda/screen").value['scale']
        except (KeyError, WDARequestError):
            v = max(self.screenshot().size) / max(self.window_size())
            return round(v)

    @cached_property
    def bundle_id(self):
        """ the session matched bundle id """
        v = self._session_http.get("/").value
        return v['capabilities'].get('CFBundleIdentifier')

    def implicitly_wait(self, seconds):
        """
        set default element search timeout
        """
        assert isinstance(seconds, (int, float))
        self.__timeout = seconds

    def battery_info(self):
        """
        Returns dict: (I do not known what it means)
            eg: {"level": 1, "state": 2}
        """
        return self._session_http.get("/wda/batteryInfo").value

    def device_info(self):
        """
        Returns dict:
            eg: {'currentLocale': 'zh_CN', 'timeZone': 'Asia/Shanghai'}
        """
        return self._session_http.get("/wda/device/info").value

    @property
    def info(self):
        """
        Returns:
            {'timeZone': 'Asia/Shanghai',
            'currentLocale': 'zh_CN',
            'model': 'iPhone',
            'uuid': '9DAC43B3-6887-428D-B5D5-4892D1F38BAA',
            'userInterfaceIdiom': 0,
            'userInterfaceStyle': 'unsupported',
            'name': 'iPhoneSE',
            'isSimulator': False}
        """
        return self.device_info()

    def set_clipboard(self, content, content_type="plaintext"):
        """ set clipboard """
        self._session_http.post(
            "/wda/setPasteboard", {
                "content": base64.b64encode(content.encode()).decode(),
                "contentType": content_type
            })

    @deprecated(version="1.0.0", reason="This method is deprecated now.")
    def set_alert_callback(self, callback):
        """
        Args:
            callback (func): called when alert popup

        Example of callback:

            def callback(session):
                session.alert.accept()
        """
        pass

    def get_clipboard(self, wda_bundle_id):
        """ Get clipboard text.

        If you want to use this function, you have to set wdap foreground which would switch the
        current screen of the phone. Then we will try to switch back to the screen before.

        Args:
            wda_bundle_id: The bundle id of the started wdap.

        Returns:
            Clipboard text.
        """
        current_app_bundle_id = self.app_current().get("bundleId", "")
        # Set wdap foreground, it's necessary.
        try:
            self.app_launch(wda_bundle_id)
        except:
            pass
        clipboard_text = self._session_http.post("/wda/getPasteboard").value
        # Switch back to the screen before.
        self.app_launch(current_app_bundle_id)
        return base64.b64decode(clipboard_text).decode('utf-8')
    
    # Not working
    # def siri_activate(self, text):
    #    self.http.post("/wda/siri/activate", {"text": text})

    def app_launch(self,
                   bundle_id,
                   arguments=[],
                   environment={},
                   wait_for_quiescence=False):
        """
        Args:
            - bundle_id (str): the app bundle id
            - arguments (list): ['-u', 'https://www.google.com/ncr']
            - enviroment (dict): {"KEY": "VAL"}
            - wait_for_quiescence (bool): default False
        """
        # Deprecated, use app_start instead
        assert isinstance(arguments, (tuple, list))
        assert isinstance(environment, dict)

        # When device is locked, it is unable to launch
        if self.locked():
            self.unlock()

        return self._session_http.post(
            "/wda/apps/launch", {
                "bundleId": bundle_id,
                "arguments": arguments,
                "environment": environment,
                "shouldWaitForQuiescence": wait_for_quiescence,
            })

    def app_activate(self, bundle_id):
        return self._session_http.post("/wda/apps/launch", {
            "bundleId": bundle_id,
        })

    def app_terminate(self, bundle_id):
        # Deprecated, use app_stop instead
        return self._session_http.post("/wda/apps/terminate", {
            "bundleId": bundle_id,
        })

    def app_state(self, bundle_id):
        """
        Returns example:
            {
                "value": 4,
                "sessionId": "0363BDC5-4335-47ED-A54E-F7CCB65C6A65"
            }

        value 1(not running) 2(running in background) 3(running in foreground)
        """
        return self._session_http.post("/wda/apps/state", {
            "bundleId": bundle_id,
        })

    def app_start(self,
                  bundle_id,
                  arguments=[],
                  environment={},
                  wait_for_quiescence=False):
        """ alias for app_launch """
        return self.app_launch(bundle_id, arguments, environment,
                               wait_for_quiescence)

    def app_stop(self, bundle_id: str):
        """ alias for app_terminate """
        self.app_terminate(bundle_id)

    def app_list(self):
        """
        Not working very well, only show springboard

        Returns:
            list of app

        Return example:
            [{'pid': 52, 'bundleId': 'com.apple.springboard'}]
        """
        return self._session_http.get("/wda/apps/list").value

    def open_url(self, url):
        """
        TODO: Never successed using before. Looks like use Siri to search.
        https://github.com/facebook/WebDriverAgent/blob/master/WebDriverAgentLib/Commands/FBSessionCommands.m#L43
        Args:
            url (str): url

        Raises:
            WDARequestError
        """
        if os.getenv("TMQ_ORIGIN") == "civita": # MDS platform
            return self.http.post("/mds/openurl", {"url": url})
        return self._session_http.post('url', {'url': url})

    def deactivate(self, duration):
        """Put app into background and than put it back
        Args:
            - duration (float): deactivate time, seconds
        """
        return self._session_http.post('/wda/deactivateApp',
                                       dict(duration=duration))

    def tap(self, x, y):
        # Support WDA `BREAKING CHANGES`
        # More see: https://github.com/appium/WebDriverAgent/blob/master/CHANGELOG.md#600-2024-01-31
        try:
            return self._session_http.post('/wda/tap', dict(x=x, y=y))
        except:
            return self._session_http.post('/wda/tap/0', dict(x=x, y=y))

    def _percent2pos(self, x, y, window_size=None):
        if any(isinstance(v, float) for v in [x, y]):
            w, h = window_size or self.window_size()
            x = int(x * w) if isinstance(x, float) else x
            y = int(y * h) if isinstance(y, float) else y
            assert w >= x >= 0
            assert h >= y >= 0
        return (x, y)

    def click(self, x, y, duration: Optional[float] = None):
        """
        Combine tap and tap_hold

        Args:
            x, y: can be float(percent) or int
            duration (optional): tap_hold duration
        """
        x, y = self._percent2pos(x, y)
        if duration:
            return self.tap_hold(x, y, duration)
        return self.tap(x, y)

    def double_tap(self, x, y):
        x, y = self._percent2pos(x, y)
        return self._session_http.post('/wda/doubleTap', dict(x=x, y=y))

    def tap_hold(self, x, y, duration=1.0):
        """
        Tap and hold for a moment

        Args:
            - x, y(int, float): float(percent) or int(absolute coordicate)
            - duration(float): seconds of hold time

        [[FBRoute POST:@"/wda/touchAndHold"] respondWithTarget:self action:@selector(handleTouchAndHoldCoordinate:)],
        """
        x, y = self._percent2pos(x, y)
        data = {'x': x, 'y': y, 'duration': duration}
        return self._session_http.post('/wda/touchAndHold', data=data)

    def swipe(self, x1, y1, x2, y2, duration=0):
        """
        Args:
            x1, y1, x2, y2 (int, float): float(percent), int(coordicate)
            duration (float): start coordinate press duration (seconds)

        [[FBRoute POST:@"/wda/dragfromtoforduration"] respondWithTarget:self action:@selector(handleDragCoordinate:)],
        """
        if any(isinstance(v, float) for v in [x1, y1, x2, y2]):
            size = self.window_size()
            x1, y1 = self._percent2pos(x1, y1, size)
            x2, y2 = self._percent2pos(x2, y2, size)

        data = dict(fromX=x1, fromY=y1, toX=x2, toY=y2, duration=duration)
        return self._session_http.post('/wda/dragfromtoforduration', data=data)

    def _fast_swipe(self, x1, y1, x2, y2, velocity: int = 500):
        """
        velocity: the larger the faster
        """
        data = dict(fromX=x1, fromY=y1, toX=x2, toY=y2, velocity=velocity)
        return self._session_http.post('/wda/drag', data=data)

    def swipe_left(self):
        """ swipe right to left """
        w, h = self.window_size()
        return self.swipe(w, h // 2, 1, h // 2)

    def swipe_right(self):
        """ swipe left to right """
        w, h = self.window_size()
        return self.swipe(1, h // 2, w, h // 2)

    def swipe_up(self):
        """ swipe from center to top """
        w, h = self.window_size()
        return self.swipe(w // 2, h // 2, w // 2, 1)

    def swipe_down(self):
        """ swipe from center to bottom """
        w, h = self.window_size()
        return self.swipe(w // 2, h // 2, w // 2, h - 1)

    def _fast_swipe_ext(self, direction: str):
        if direction == "up":
            w, h = self.window_size()
            return self.swipe(w // 2, h // 2, w // 2, 1)
        elif direction == "down":
            w, h = self.window_size()
            return self._fast_swipe(w // 2, h // 2, w // 2, h - 1)
        else:
            raise RuntimeError("not supported direction:", direction)

    @property
    def orientation(self):
        """
        Return string
        One of <PORTRAIT | LANDSCAPE>
        """
        for _ in range(3):
            result = self._session_http.get('orientation').value
            if result:
                return result
            time.sleep(.5)

    @orientation.setter
    def orientation(self, value):
        """
        Args:
            - orientation(string): LANDSCAPE | PORTRAIT | UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT |
                    UIA_DEVICE_ORIENTATION_PORTRAIT_UPSIDEDOWN
        """
        return self._session_http.post('orientation',
                                       data={'orientation': value})

    def window_size(self):
        """
        Returns:
            namedtuple: eg
                Size(width=320, height=568)
        """
        size = self._unsafe_window_size()
        if min(size) > 0:
            return size

        # get orientation, handle alert
        _ = self.orientation  # after this operation, may safe to get window_size
        if self.alert.exists:
            self.alert.accept()
            time.sleep(.1)

        size = self._unsafe_window_size()
        if min(size) > 0:
            return size

        logger.warning("unable to get window_size(), try to to create a new session")
        with self.session("com.apple.Preferences") as app:
            size = app._unsafe_window_size()
            assert min(size) > 0, "unable to get window_size"
            return size

    def _unsafe_window_size(self):
        """
        returns (width, height) might be (0, 0)
        """
        value = self._session_http.get('/window/size').value
        w = roundint(value['width'])
        h = roundint(value['height'])
        return namedtuple('Size', ['width', 'height'])(w, h)

    @retry.retry(WDAKeyboardNotPresentError, tries=3, delay=1.0)
    def send_keys(self, value):
        """
        send keys, yet I know not, todo function
        """
        if isinstance(value, six.string_types):
            value = list(value)
        return self._session_http.post('/wda/keys', data={'value': value})

    def press(self, name: str):
        """
        Args:
            name: one of <home|volumeUp|volumeDown>
        """
        valid_names = ("home", "volumeUp", "volumeDown")
        if name not in valid_names:
            raise ValueError(
                f"Invalid name: {name}, should be one of {valid_names}")
        self._session_http.post("/wda/pressButton", {"name": name})

    def press_duration(self, name: str, duration: float):
        """
        Args:
            name: one of <home|volumeUp|volumeDown|power|snapshot>
            duration: seconds

        Notes:
            snapshot equals power+home

        Raises:
            ValueError

        Refs:
            https://github.com/appium/WebDriverAgent/pull/494/files
        """
        hid_usages = {
            "home": 0x40,
            "volumeup": 0xE9,
            "volumedown": 0xEA,
            "power": 0x30,
            "snapshot": 0x65,
            "power+home": 0x65
        }
        name = name.lower()
        if name not in hid_usages:
            raise ValueError("Invalid name:", name)
        hid_usage = hid_usages[name]
        return self._session_http.post("/wda/performIoHidEvent", {"page": 0x0C, "usage": hid_usage, "duration": duration})

    def keyboard_dismiss(self):
        """
        Not working for now
        """
        raise RuntimeError("not pass tests, this method is not allowed to use")
        self._session_http.post('/wda/keyboard/dismiss')

    def appium_settings(self, value: Optional[dict] = None,
                        validate: bool = False) -> dict:
        """
        Get and set /session/$sessionId/appium/settings

        Args:
            value: None 表示读取当前全部设置；dict 表示设置
            validate: True 时先用 :func:`validate_appium_settings` 校验 key 与 value。
                      服务端对未知 key 是**静默忽略**的，拼错 key 不会报错，
                      开启校验可以在本地就发现问题。

        Returns:
            dict: 当前全部 settings 的键值

        Example::

            # 读取
            c.appium_settings()

            # 设置（推荐用 AppiumSettings 枚举，避免拼错）
            c.appium_settings({
                AppiumSettings.SnapshotMaxDepth.value: 30,
                AppiumSettings.UseFirstMatch.value: True,
            })

            # 带本地校验
            c.appium_settings({AppiumSettings.ReduceMotion.value: True}, validate=True)

        全部可用 key 见 :class:`AppiumSettings`（34 个，与服务端 FBSettings.m 一一对应），
        每个 key 的类型与默认值见 :data:`APPIUM_SETTINGS_SPEC`。
        """
        if value is None:
            return self._session_http.get("/appium/settings").value
        if validate:
            value = validate_appium_settings(value)
        return self._session_http.post("/appium/settings",
                                       data={
                                           "settings": value
                                       }).value

    # ======================== WDA 端点补全 ========================
    def screens(self) -> list:
        """
        返回当前所有屏幕信息 GET /wda/screens

        Returns:
            list of dict, 每个屏幕一条记录
        """
        return self.http.get("/wda/screens").value

    def app_launch_unattached(self, bundle_id: str):
        """
        启动应用但不依附到 session POST /wda/apps/launchUnattached

        与 app_launch 的区别：不把该应用设为 session 的被测应用。

        Args:
            bundle_id (str): 应用 bundle id
        """
        return self.http.post("/wda/apps/launchUnattached", {"bundleId": bundle_id})

    def device_location(self) -> dict:
        """
        读取设备真实定位 GET /wda/device/location

        Returns:
            dict: {"authorizationStatus": int, "latitude": float,
                   "longitude": float, "altitude": float}

        Note:
            需要给 WebDriverAgent-Runner 授权定位服务，且定位数据更新有延迟，
            返回值可能暂时为 0。
        """
        return self.http.get("/wda/device/location").value

    def simulated_location(self) -> dict:
        """
        读取当前模拟定位 GET /wda/simulatedLocation

        Returns:
            dict: {"latitude": .., "longitude": .., "altitude": ..}
        """
        return self.http.get("/wda/simulatedLocation").value

    def set_simulated_location(self, latitude: float, longitude: float):
        """
        设置模拟定位 POST /wda/simulatedLocation

        Args:
            latitude (float): 纬度
            longitude (float): 经度
        """
        return self.http.post("/wda/simulatedLocation",
                              {"latitude": latitude, "longitude": longitude})

    def clear_simulated_location(self):
        """清除模拟定位 DELETE /wda/simulatedLocation"""
        return self.http.fetch("DELETE", "/wda/simulatedLocation")

    def set_appearance(self, name: str):
        """
        切换浅色 / 深色外观 POST /wda/device/appearance（无需 session）

        Args:
            name (str): "light" 或 "dark"
        """
        name = (name or "").lower()
        if name not in ("light", "dark"):
            raise ValueError("appearance name must be 'light' or 'dark', got %r" % name)
        return self.http.post("/wda/device/appearance", {"name": name})

    def device_orientation(self) -> str:
        """
        读取设备物理朝向 GET /wda/deviceOrientation

        Returns:
            str: 如 "PORTRAIT" / "LANDSCAPE"
        """
        return self.http.get("/wda/deviceOrientation").value

    @property
    def rotation(self) -> dict:
        """
        读取屏幕旋转向量 GET /rotation

        Returns:
            dict: {"x": .., "y": .., "z": ..}
        """
        return self.http.get("/rotation").value

    @rotation.setter
    def rotation(self, value: dict):
        """
        设置屏幕旋转向量 POST /rotation

        Args:
            value (dict): {"x": .., "y": .., "z": ..}，三个键缺一不可
        """
        for key in ("x", "y", "z"):
            if key not in value:
                raise ValueError("rotation requires x, y and z, got %r" % value)
        return self.http.post("/rotation", data=value)

    def tap_with_number_of_taps(self, x, y, taps: int = 2, touches: int = 1):
        """
        多击 POST /wda/tapWithNumberOfTaps

        Args:
            x, y: 坐标（int 像素 / float 百分比）
            taps (int): 连击次数
            touches (int): 同时按下的手指数
        """
        x, y = self._percent2pos(x, y)
        return self._session_http.post(
            "/wda/tapWithNumberOfTaps",
            {"x": x, "y": y, "numberOfTaps": taps, "numberOfTouches": touches})

    def two_finger_tap(self, x, y):
        """双指点击 POST /wda/twoFingerTap"""
        x, y = self._percent2pos(x, y)
        return self._session_http.post("/wda/twoFingerTap", dict(x=x, y=y))

    def force_touch(self, x, y, pressure: float = 1.0, duration: float = 1.0):
        """
        3D Touch 重按 POST /wda/forceTouch

        Args:
            pressure (float): 按压力度 0..1
            duration (float): 按压持续时间（秒）
        """
        x, y = self._percent2pos(x, y)
        return self._session_http.post(
            "/wda/forceTouch",
            {"x": x, "y": y, "pressure": pressure, "duration": duration})

    def press_and_drag(self, from_x, from_y, to_x, to_y,
                       press_duration: float = 0.5,
                       hold_duration: float = 0.5,
                       velocity: float = 500.0):
        """
        长按后拖拽 POST /wda/pressAndDragWithVelocity

        Args:
            press_duration (float): 起点按住时长（秒）
            hold_duration (float): 终点停留时长（秒）
            velocity (float): 拖拽速度，越大越快
        """
        data = {
            "fromX": from_x, "fromY": from_y,
            "toX": to_x, "toY": to_y,
            "pressDuration": press_duration,
            "holdDuration": hold_duration,
            "velocity": velocity,
        }
        return self._session_http.post("/wda/pressAndDragWithVelocity", data=data)

    def scroll(self,
               direction: Optional[str] = None,
               distance: float = 1.0,
               name: Optional[str] = None,
               predicate_string: Optional[str] = None,
               to_visible: bool = False):
        """
        滚动 POST /wda/scroll

        四种用法互斥，按 name > direction > predicate_string > to_visible 的优先级：

        Args:
            direction (str): up / down / left / right，按元素尺寸归一化距离滚动
            distance (float): 滚动距离，相对元素宽高，1.0 即一屏
            name (str): 滚动到指定 identifier 的子元素可见
            predicate_string (str): 滚动到满足 NSPredicate 的子元素可见
            to_visible (bool): 滚动到当前元素可见

        Raises:
            ValueError: 未指定任何有效参数
        """
        if name:
            data = {"name": name}
        elif direction:
            if direction not in ("up", "down", "left", "right"):
                raise ValueError("Invalid direction:", direction)
            data = {"direction": direction, "distance": distance}
        elif predicate_string:
            data = {"predicateString": predicate_string}
        elif to_visible:
            data = {"toVisible": True}
        else:
            raise ValueError(
                "one of direction / name / predicate_string / to_visible is required")
        return self._session_http.post("/wda/scroll", data=data)

    def swipe_direction(self, direction: str, x, y, velocity: Optional[float] = None):
        """
        从指定点按方向快速滑动 POST /wda/swipe

        Args:
            direction (str): up / down / left / right
            x, y: 起点坐标
            velocity (float): 滑动速度，越大越快
        """
        if direction not in ("up", "down", "left", "right"):
            raise ValueError("Invalid direction:", direction)
        x, y = self._percent2pos(x, y)
        data = {"direction": direction, "x": x, "y": y}
        if velocity is not None:
            data["velocity"] = velocity
        return self._session_http.post("/wda/swipe", data=data)

    def pinch(self, scale: float, velocity: float):
        """
        捏合缩放手势 POST /wda/pinch

        Args:
            scale (float): 缩放比例，必须 > 0
            velocity (float): scale < 1 时须为负，scale > 1 时须为正

        Example:
            pinch_in  -> scale=0.5, velocity=-1
            pinch_out -> scale=2.0, velocity=1
        """
        if scale <= 0:
            raise ValueError("scale must be greater than 0")
        return self._session_http.post("/wda/pinch",
                                       {"scale": scale, "velocity": velocity})

    def rotate_gesture(self, rotation: float, velocity: float = 1.0):
        """
        旋转手势 POST /wda/rotate

        Args:
            rotation (float): 旋转弧度
            velocity (float): 旋转速度
        """
        return self._session_http.post("/wda/rotate",
                                       {"rotation": rotation, "velocity": velocity})

    def rotate_digital_crown(self, delta: float, velocity: Optional[float] = None):
        """
        旋转数字表冠 POST /wda/rotateDigitalCrown（watchOS）

        Args:
            delta (float): 旋转增量
            velocity (float): 旋转速度
        """
        data = {"delta": delta}
        if velocity is not None:
            data["velocity"] = velocity
        return self._session_http.post("/wda/rotateDigitalCrown", data=data)

    def perform_hand_gesture(self, name: str):
        """
        执行系统手势 POST /wda/performHandGesture

        Args:
            name (str): 手势名称，如 "Screenshot"、"Shake" 等 watchOS 手势
        """
        return self._session_http.post("/wda/performHandGesture", {"name": name})

    def touch_id(self, match: bool = True):
        """
        模拟 Touch ID / Face ID 结果 POST /wda/touch_id

        Args:
            match (bool): True 表示指纹/面容匹配成功
        """
        return self._session_http.post("/wda/touch_id", {"match": match})

    def siri_activate(self, text: str):
        """
        唤起 Siri 并识别语音文本 POST /wda/siri/activate

        Args:
            text (str): 交给 Siri 的文本
        """
        return self._session_http.post("/wda/siri/activate", {"text": text})

    def expect_notification(self, name: str, timeout: float = 60.0,
                            type: str = "plain"):
        """
        等待指定通知出现 POST /wda/expectNotification

        Args:
            name (str): 通知名（必填）
            timeout (float): 最长等待秒数
            type (str): "plain" 或 "darwin"
        """
        if type not in ("plain", "darwin"):
            raise ValueError("type must be 'plain' or 'darwin', got %r" % type)
        return self._session_http.post(
            "/wda/expectNotification",
            {"name": name, "timeout": timeout, "type": type})

    def reset_app_auth(self, resource: int):
        """
        重置应用授权 POST /wda/resetAppAuth

        Args:
            resource (int): 权限资源编号
        """
        return self._session_http.post("/wda/resetAppAuth", {"resource": resource})

    def perform_accessibility_audit(self, audit_types: Optional[list] = None) -> list:
        """
        执行无障碍审计 POST /wda/performAccessibilityAudit

        Args:
            audit_types (list): 审计类型列表，None 表示全部（XCUIAccessibilityAuditTypeAll）

        Returns:
            list: 审计结果
        """
        data = {}
        if audit_types:
            data["auditTypes"] = audit_types
        return self._session_http.post("/wda/performAccessibilityAudit", data=data).value

    def video_start(self, fps: int = 24, codec: int = 0):
        """
        开始录屏 POST /wda/video/start

        Args:
            fps (int): 帧率，默认 24
            codec (int): 编码格式，默认 0
        """
        return self._session_http.post("/wda/video/start",
                                       {"fps": fps, "codec": codec})

    def video_stop(self):
        """停止录屏 POST /wda/video/stop"""
        return self._session_http.post("/wda/video/stop")

    def video(self):
        """查询录屏状态 GET /wda/video"""
        return self._session_http.get("/wda/video").value

    def voice_over_enabled(self) -> bool:
        """VoiceOver 是否开启 GET /wda/voiceOver/enabled"""
        return self.http.get("/wda/voiceOver/enabled").value

    def voice_over_enable(self):
        """开启 VoiceOver POST /wda/voiceOver/enable"""
        return self.http.post("/wda/voiceOver/enable")

    def voice_over_disable(self):
        """关闭 VoiceOver POST /wda/voiceOver/disable"""
        return self.http.post("/wda/voiceOver/disable")

    def voice_over_move(self, direction: str):
        """
        VoiceOver 焦点移动 POST /wda/voiceOver/move

        Args:
            direction (str): 移动方向，如 "next" / "previous"
        """
        return self.http.post("/wda/voiceOver/move", {"direction": direction})

    def voice_over_speech(self) -> str:
        """读取 VoiceOver 当前朗读内容 GET /wda/voiceOver/currentSpeech"""
        return self.http.get("/wda/voiceOver/currentSpeech").value

    def keyboard_input(self, keys: list):
        """
        按键序列输入 POST /wda/element/0/keyboardInput

        与 send_keys 的区别：send_keys 走文本输入，这里是逐「键」输入，
        支持组合键，需要 Xcode15+ / iPadOS17+。

        Args:
            keys (list): 键名列表，如 ["a"] 或 [["a", ["shift"]]]
        """
        if not isinstance(keys, (list, tuple)):
            raise TypeError("keys must be a list")
        return self._session_http.post("/wda/element/0/keyboardInput",
                                       {"keys": list(keys)})

    def xpath(self, value):
        """
        For weditor, d.xpath(...)
        """
        return Selector(self, xpath=value)

    def __call__(self, *args, **kwargs):
        if 'timeout' not in kwargs:
            kwargs['timeout'] = self.__timeout
        return Selector(self, *args, **kwargs)

    @cached_property
    def alibaba(self):
        """ Only used in alibaba company """
        try:
            import wda_taobao
            return wda_taobao.Alibaba(self)
        except ImportError:
            raise RuntimeError(
                "@alibaba property requires wda_taobao library installed")

    @cached_property
    def taobao(self):
        try:
            import wda_taobao
            return wda_taobao.Taobao(self)
        except ImportError:
            raise RuntimeError(
                "@taobao property requires wda_taobao library installed")
    # ======================== 新增：复杂触摸动作封装 ========================
    class TouchAction:
        """
        触摸动作构建器：基于 W3C 规范，支持单指/多指连续动作序列
        用法示例：
        1. 单指从(500,300)滑动到(100,300)（500ms）：
           client.touch_action().add_pointer(
               pointer_id="finger1",
               actions=[
                   ("move", 500, 300, 0),  # 0ms 移动到起点
                   ("down",),               # 按下
                   ("move", 100, 300, 500), # 500ms 移动到终点
                   ("up",)                  # 抬起
               ]
           ).perform()

        2. 双指缩放（从中心向外扩大）：
           client.touch_action().add_pointer(
               pointer_id="finger1",
               actions=[("move", 200, 300, 0), ("down",), ("move", 100, 300, 1000), ("up",)]
           ).add_pointer(
               pointer_id="finger2",
               actions=[("move", 300, 300, 0), ("down",), ("move", 400, 300, 1000), ("up",)]
           ).perform()
        """

        def __init__(self, client: 'BaseClient'):
            self._client = client  # 关联父客户端，复用会话和HTTP请求
            self._pointers: List[Dict] = []  # 存储所有指针（手指）的动作序列
            self._pointer_ids: List[str] = []  # 校验指针ID唯一性

        def add_pointer(
                self,
                pointer_id: str,
                actions: List[Union[tuple, list]],
                pointer_type: str = "touch"
        ) -> 'BaseClient.TouchAction':
            """
            添加一个指针（手指）的动作序列
            Args:
                pointer_id: 指针唯一标识（如"finger1"），不可重复
                actions: 动作列表，每个动作是元组，格式：
                         - ("move", x, y, duration)：移动到(x,y)，耗时duration毫秒
                         - ("down",)：按下（触摸开始）
                         - ("up",)：抬起（触摸结束）
                         - ("pause", duration)：暂停duration毫秒
                pointer_type: 指针类型，固定为"touch"（触摸）
            Returns:
                自身实例，支持链式调用
            Raises:
                ValueError: 指针ID重复或动作格式错误
            """
            # 1. 校验指针ID唯一性
            if pointer_id in self._pointer_ids:
                raise ValueError(f"Pointer ID '{pointer_id}' already exists (must be unique)")
            self._pointer_ids.append(pointer_id)

            # 2. 解析并校验动作序列
            parsed_actions = []
            for action in actions:
                action_type = action[0].lower()
                if action_type == "move":
                    # 动作格式：("move", x, y, duration)
                    if len(action) != 4:
                        raise ValueError(f"Invalid 'move' action: {action} (expected: (\"move\", x, y, duration))")
                    x, y, duration = action[1], action[2], action[3]
                    # 坐标校验（支持相对百分比，后续由客户端转换为绝对坐标）
                    if not (isinstance(x, (int, float)) and isinstance(y, (int, float))):
                        raise ValueError(f"x/y must be int/float, got x={x}({type(x)}), y={y}({type(y)})")
                    if not (isinstance(duration, int) and duration >= 0):
                        raise ValueError(f"Duration must be non-negative int, got {duration}")
                    parsed_actions.append({
                        "type": "pointerMove",
                        "x": x,
                        "y": y,
                        "duration": duration
                    })
                elif action_type == "down":
                    # 动作格式：("down",)
                    if len(action) != 1:
                        raise ValueError(f"Invalid 'down' action: {action} (expected: (\"down\",))")
                    parsed_actions.append({"type": "pointerDown"})
                elif action_type == "up":
                    # 动作格式：("up",)
                    if len(action) != 1:
                        raise ValueError(f"Invalid 'up' action: {action} (expected: (\"up\",))")
                    parsed_actions.append({"type": "pointerUp"})
                elif action_type == "pause":
                    # 动作格式：("pause", duration)
                    if len(action) != 2:
                        raise ValueError(f"Invalid 'pause' action: {action} (expected: (\"pause\", duration))")
                    duration = action[1]
                    if not (isinstance(duration, int) and duration >= 0):
                        raise ValueError(f"Pause duration must be non-negative int, got {duration}")
                    parsed_actions.append({
                        "type": "pause",
                        "duration": duration
                    })
                else:
                    raise ValueError(f"Unsupported action type: {action_type} (supported: move/down/up/pause)")

            # 3. 添加指针到动作列表
            self._pointers.append({
                "type": "pointer",
                "id": pointer_id,
                "parameters": {"pointerType": pointer_type},
                "actions": parsed_actions
            })
            return self

        def _convert_percent_to_abs(self, x: Union[int, float], y: Union[int, float]) -> tuple:
            """将相对百分比坐标（0.0~1.0）转换为绝对屏幕坐标"""
            if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                window_size = self._client.window_size()  # 复用客户端的窗口尺寸获取
                return (int(x * window_size.width), int(y * window_size.height))
            return (int(x), int(y))

        def _prepare_payload(self) -> Dict:
            """准备发送给 /actions 接口的请求体"""
            if not self._pointers:
                raise ValueError("No pointers added (call add_pointer() first)")

            # 转换百分比坐标为绝对坐标
            for pointer in self._pointers:
                for action in pointer["actions"]:
                    if action["type"] == "pointerMove":
                        x_abs, y_abs = self._convert_percent_to_abs(action["x"], action["y"])
                        action["x"] = x_abs
                        action["y"] = y_abs
            return {"actions": self._pointers}

        def perform(self) -> AttrDict:
            """执行触摸动作序列（调用 /actions 接口）"""
            payload = self._prepare_payload()
            try:
                # 调用原客户端的 _fetch 方法，复用会话和错误处理
                response = self._client._fetch(
                    method="POST",
                    urlpath="/actions",
                    data=payload,
                    with_session=True  # 绑定当前会话
                )
                return response
            except WDARequestError as e:
                raise WDAError(f"Touch action failed: {str(e)}") from e

    def touch_action(self) -> 'BaseClient.TouchAction':
        """创建触摸动作构建器实例"""
        return self.TouchAction(client=self)

    # ======================== 新增：预置常用触摸动作 ========================
    def swipe_complex(
            self,
            x1: Union[int, float],
            y1: Union[int, float],
            x2: Union[int, float],
            y2: Union[int, float],
            duration: int = 500,
            pointer_id: str = "finger1"
    ) -> AttrDict:
        """
        预置：单指滑动动作（基于复杂触摸动作封装）
        Args:
            x1/y1: 起点坐标（支持绝对坐标int或相对百分比float 0.0~1.0）
            x2/y2: 终点坐标（同上）
            duration: 滑动耗时（毫秒），默认500ms
            pointer_id: 指针ID，默认"finger1"
        Returns:
            接口响应结果
        """
        return self.touch_action().add_pointer(
            pointer_id=pointer_id,
            actions=[
                ("move", x1, y1, 0),  # 0ms 移动到起点
                ("down",),  # 按下
                ("move", x2, y2, duration),  # 耗时duration毫秒移动到终点
                ("up",)  # 抬起
            ]
        ).perform()

    def pinch_zoom(
            self,
            center_x: Union[int, float],
            center_y: Union[int, float],
            scale: float = 2.0,
            duration: int = 1000,
            pointer1_id: str = "finger1",
            pointer2_id: str = "finger2"
    ) -> AttrDict:
        """
        预置：双指缩放动作（基于复杂触摸动作封装）
        Args:
            center_x/center_y: 缩放中心点坐标（支持绝对/相对坐标）
            scale: 缩放比例（>1.0放大，0.0~1.0缩小），默认2.0（放大1倍）
            duration: 缩放耗时（毫秒），默认1000ms
            pointer1_id/pointer2_id: 两个指针的ID，默认"finger1"/"finger2"
        Returns:
            接口响应结果
        """
        if scale <= 0:
            raise ValueError(f"Scale must be positive, got {scale}")

        # 1. 转换中心点为绝对坐标
        window_size = self.window_size()
        center_x_abs, center_y_abs = self.touch_action()._convert_percent_to_abs(center_x, center_y)

        # 2. 计算初始/结束位置（基于屏幕短边的10%作为初始间距）
        base_dist = min(window_size.width, window_size.height) * 0.1  # 初始间距
        target_dist = base_dist * scale  # 目标间距
        half_delta = (target_dist - base_dist) / 2  # 每个指针需要移动的距离

        # 3. 指针1：中心点左侧 -> 更左侧（放大）/ 更右侧（缩小）
        p1_start_x = center_x_abs - base_dist / 2
        p1_end_x = center_x_abs - target_dist / 2
        # 指针2：中心点右侧 -> 更右侧（放大）/ 更左侧（缩小）
        p2_start_x = center_x_abs + base_dist / 2
        p2_end_x = center_x_abs + target_dist / 2

        # 4. 执行双指动作
        return self.touch_action().add_pointer(
            pointer_id=pointer1_id,
            actions=[
                ("move", p1_start_x, center_y_abs, 0),
                ("down",),
                ("move", p1_end_x, center_y_abs, duration),
                ("up",)
            ]
        ).add_pointer(
            pointer_id=pointer2_id,
            actions=[
                ("move", p2_start_x, center_y_abs, 0),
                ("down",),
                ("move", p2_end_x, center_y_abs, duration),
                ("up",)
            ]
        ).perform()

class Alert(object):
    DEFAULT_ACCEPT_BUTTONS = [
        "使用App时允许", "无线局域网与蜂窝网络", "好", "稍后", "稍后提醒", "确定",
        "允许", "以后", "打开", "录屏", "Allow", "OK", "YES", "Yes", "Later", "Close"
    ]

    def __init__(self, client: BaseClient):
        self._c = client
        self.http = client._session_http

    @property
    def exists(self):
        try:
            self.text
            return True
        except WDARequestError as e:
            # expect e.status != 27 in old version and e.value == 'no such alert' in new version
            return False

    @property
    def text(self):
        return self.http.get('/alert/text').value
    
    def set_text(self, text: str):
        '''Set text to alert.
        Except return example:
            ```
            wdap.exceptions.WDARequestError: WDARequestError(status=110,
            value={'error': 'no such alert', 'message': 'An attempt was 
            made to operate on a modal dialog when one was not open'})```
        '''
        return self.http.post('/alert/text', data={'value': text})

    def wait(self, timeout=20.0):
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.exists:
                return True
            time.sleep(0.2)
        return False

    def accept(self):
        return self.http.post('/alert/accept')

    def dismiss(self):
        return self.http.post('/alert/dismiss')

    def buttons(self):
        return self.http.get('/wda/alert/buttons').value

    def click(self, button_name: Optional[Union[str, list]] = None):
        """
        Args:
            - button_name: the name of the button

        Returns:
            button_name being clicked

        Raises:
            ValueError when button_name is not in avaliable button names
        """
        # Actually, It has no difference POST to accept or dismiss
        if isinstance(button_name, str):
            self.http.post('/alert/accept', data={"name": button_name})
            return button_name

        avaliable_names = self.buttons()
        buttons: list = button_name
        for bname in buttons:
            if bname in avaliable_names:
                return self.click(bname)
        raise ValueError("Only these buttons can be clicked", avaliable_names)

    def click_exists(self, buttons: Optional[Union[str, list]] = None):
        """
         Args:
            - buttons: the name of the button of list of names

        Returns:
            button_name clicked or None
        """
        try:
            return self.click(buttons)
        except (ValueError, WDARequestError):
            return None

    @contextlib.contextmanager
    def watch_and_click(self,
                        buttons: Optional[list] = None,
                        interval: float = 2.0):
        """ watch and click button
        Args:
            buttons: buttons name which need to click
            interval: check interval
        """
        if not buttons:
            buttons = self.DEFAULT_ACCEPT_BUTTONS

        event = threading.Event()

        def _inner():
            while not event.is_set():
                try:
                    alert_buttons = self.buttons()
                    logger.info("Alert detected, buttons: %s", alert_buttons)
                    for btn_name in buttons:
                        if btn_name in alert_buttons:
                            logger.info("Alert click: %s", btn_name)
                            self.click(btn_name)
                            break
                    else:
                        logger.warning("Alert not handled")
                except WDARequestError:
                    pass
                time.sleep(interval)

        threading.Thread(name="alert", target=_inner, daemon=True).start()
        yield None
        event.set()


class Client(BaseClient):
    @property
    def alert(self) -> Alert:
        return Alert(self)

    @cached_property
    def cv(self) -> CV:
        """
        CV / Vision 图像与文字识别接口（需要 WDA 带 OpenCV + Vision 支持）

        Example::

            c.cv.status()                       # 探测能力
            c.cv.find_text("登录", tap=True)     # 找字点击
            c.cv.match_image("tpl.png", tap=True)  # 找图点击
            c.cv.find_color("#FF5522")          # 找色

        Returns:
            wdap.cv.CV
        """
        return CV(self)

    @cached_property
    def log(self) -> Log:
        """
        WDA 运行日志（/wda/log/*），需要 WDA 带运行日志支持

        这些路由是 session-less + standalone 的——即使 session 没建起来、
        路由队列卡死也能问到，专门用来回答"端口还在但没反应"。

        Example::

            c.log.stats().http["requests"]        # 计数器总览
            c.log.recent(limit=50, level="warn")  # 最近 50 条 warn 以上
            c.log.crash().report                  # 上次崩溃报告
            c.log.save("wda.log")                 # 纯文本落盘

        Returns:
            wdap.log.Log
        """
        return Log(self)


Session = Client  # for compability


class Selector(object):
    def __init__(self,
                 session: Session,
                 predicate=None,
                 id=None,
                 className=None,
                 type=None,
                 name=None,
                 nameContains=None,
                 nameMatches=None,
                 text=None,
                 textContains=None,
                 textMatches=None,
                 value=None,
                 valueContains=None,
                 label=None,
                 labelContains=None,
                 visible=None,
                 enabled=None,
                 classChain=None,
                 xpath=None,
                 parent_class_chains=[],
                 timeout=10.0,
                 index=0):
        '''
        Args:
            predicate (str): predicate string
            id (str): raw identifier
            className (str): attr of className
            type (str): alias of className
            name (str): attr for name
            nameContains (str): attr of name contains
            nameMatches (str): regex string
            text (str): alias of name
            textContains (str): alias of nameContains
            textMatches (str): alias of nameMatches
            value (str): attr of value, not used in most times
            valueContains (str): attr of value contains
            label (str): attr for label
            labelContains (str): attr for label contains
            visible (bool): is visible
            enabled (bool): is enabled
            classChain (str): string of ios chain query, eg: **/XCUIElementTypeOther[`value BEGINSWITH 'blabla'`]
            xpath (str): xpath string, a little slow, but works fine
            timeout (float): maxium wait element time, default 10.0s
            index (int): index of founded elements

        WDA use two key to find elements "using", "value"
        Examples:
        "using" can be on of 
            "partial link text", "link text"
            "name", "id", "accessibility id"
            "class name", "class chain", "xpath", "predicate string"

        predicate string support many keys
            UID,
            accessibilityContainer,
            accessible,
            enabled,
            frame,
            label,
            name,
            rect,
            type,
            value,
            visible,
            wdAccessibilityContainer,
            wdAccessible,
            wdEnabled,
            wdFrame,
            wdLabel,
            wdName,
            wdRect,
            wdType,
            wdUID,
            wdValue,
            wdVisible
        '''
        assert isinstance(session, Session)
        self._session = session

        self._predicate = predicate
        self._id = id
        self._class_name = className or type
        self._name = self._add_escape_character_for_quote_prime_character(
            name or text)
        self._name_part = nameContains or textContains
        self._name_regex = nameMatches or textMatches
        self._value = value
        self._value_part = valueContains
        self._label = label
        self._label_part = labelContains
        self._enabled = enabled
        self._visible = visible
        self._index = index

        self._xpath = self._fix_xcui_type(xpath)
        self._class_chain = self._fix_xcui_type(classChain)
        self._timeout = timeout
        # some fixtures
        if self._class_name and not self._class_name.startswith(
                'XCUIElementType'):
            self._class_name = 'XCUIElementType' + self._class_name
        if self._name_regex:
            if not self._name_regex.startswith(
                    '^') and not self._name_regex.startswith('.*'):
                self._name_regex = '.*' + self._name_regex
            if not self._name_regex.endswith(
                    '$') and not self._name_regex.endswith('.*'):
                self._name_regex = self._name_regex + '.*'
        self._parent_class_chains = parent_class_chains

    @property
    def http(self):
        return self._session._session_http

    def _fix_xcui_type(self, s):
        if s is None:
            return
        re_element = '|'.join(xcui_element_types.ELEMENTS)
        return re.sub(r'/(' + re_element + ')', r'/XCUIElementType\g<1>', s)

    def _add_escape_character_for_quote_prime_character(self, text):
        """
        Fix for https://github.com/openatx/facebook-wda/issues/33
        Returns:
            string with properly formated quotes, or non changed text
        """
        if text is not None:
            if "'" in text:
                return text.replace("'", "\\'")
            elif '"' in text:
                return text.replace('"', '\\"')
            else:
                return text
        else:
            return text

    def _wdasearch(self, using, value):
        """
        Returns:
            element_ids (list(string)): example ['id1', 'id2']

        HTTP example response:
        [
            {"ELEMENT": "E2FF5B2A-DBDF-4E67-9179-91609480D80A"},
            {"ELEMENT": "597B1A1E-70B9-4CBE-ACAD-40943B0A6034"}
        ]
        """
        element_ids = []
        for v in self.http.post('/elements', {
                'using': using,
                'value': value
        }).value:
            element_ids.append(v['ELEMENT'])
        return element_ids

    def _gen_class_chain(self):
        # just return if aleady exists predicate
        if self._predicate:
            return '/XCUIElementTypeAny[`' + self._predicate + '`]'
        qs = []
        if self._name:
            qs.append("name == '%s'" % self._name)
        if self._name_part:
            qs.append("name CONTAINS %r" % self._name_part)
        if self._name_regex:
            qs.append("name MATCHES %r" % self._name_regex)
        if self._label:
            qs.append("label == '%s'" % self._label)
        if self._label_part:
            qs.append("label CONTAINS '%s'" % self._label_part)
        if self._value:
            qs.append("value == '%s'" % self._value)
        if self._value_part:
            qs.append("value CONTAINS '%s'" % self._value_part)
        if self._visible is not None:
            qs.append("visible == %s" % 'true' if self._visible else 'false')
        if self._enabled is not None:
            qs.append("enabled == %s" % 'true' if self._enabled else 'false')
        predicate = ' AND '.join(qs)
        chain = '/' + (self._class_name or 'XCUIElementTypeAny')
        if predicate:
            chain = chain + '[`' + predicate + '`]'
        if self._index:
            chain = chain + '[%d]' % self._index
        return chain

    @retry.retry(WDAStaleElementReferenceError, tries=3, delay=.5, jitter=.2)
    def find_element_ids(self):
        elems = []
        if self._id:
            return self._wdasearch('id', self._id)
        if self._predicate:
            return self._wdasearch('predicate string', self._predicate)
        if self._xpath:
            return self._wdasearch('xpath', self._xpath)
        if self._class_chain:
            return self._wdasearch('class chain', self._class_chain)

        chain = '**' + ''.join(
            self._parent_class_chains) + self._gen_class_chain()
        if DEBUG:
            print('CHAIN:', chain)
        return self._wdasearch('class chain', chain)

    def find_elements(self):
        """
        Returns:
            Element (list): all the elements
        """
        es = []
        for element_id in self.find_element_ids():
            e = Element(self._session, element_id)
            es.append(e)
        return es

    def count(self):
        return len(self.find_element_ids())

    def get(self, timeout=None, raise_error=True):
        """
        Args:
            timeout (float): timeout for query element, unit seconds
                Default 10s
            raise_error (bool): whether to raise error if element not found

        Returns:
            Element: UI Element

        Raises:
            WDAElementNotFoundError if raise_error is True else None
        """
        start_time = time.time()
        if timeout is None:
            timeout = self._timeout
        while True:
            elems = self.find_elements()
            if len(elems) > 0:
                return elems[0]
            if start_time + timeout < time.time():
                break
            time.sleep(0.5)

        if raise_error:
            raise WDAElementNotFoundError("element not found",
                                          "timeout %.1f" % timeout)

    def __getattr__(self, oper):
        if oper.startswith("_"):
            raise AttributeError("invalid attr", oper)
        if not hasattr(Element, oper):
            raise AttributeError("'Element' object has no attribute %r" % oper)

        el = self.get()
        return getattr(el, oper)

    def set_timeout(self, s):
        """
        Set element wait timeout
        """
        self._timeout = s
        return self

    def __getitem__(self, index):
        self._index = index
        return self

    def child(self, *args, **kwargs):
        chain = self._gen_class_chain()
        kwargs['parent_class_chains'] = self._parent_class_chains + [chain]
        return Selector(self._session, *args, **kwargs)

    @property
    def exists(self):
        return len(self.find_element_ids()) > self._index

    def click(self, timeout: Optional[float] = None):
        """
        Click element

        Args:
            timeout (float): max wait seconds
        """
        e = self.get(timeout=timeout)
        e.click()

    def click_exists(self, timeout=0):
        """
        Wait element and perform click

        Args:
            timeout (float): timeout for wait

        Returns:
            bool: if successfully clicked
        """
        e = self.get(timeout=timeout, raise_error=False)
        if e is None:
            return False
        e.click()
        return True

    def wait(self, timeout=None, raise_error=False):
        """ alias of get
        Args:
            timeout (float): timeout seconds
            raise_error (bool): default false, whether to raise error if element not found

        Returns:
            Element or None
        """
        return self.get(timeout=timeout, raise_error=raise_error)

    def wait_gone(self, timeout=None, raise_error=True):
        """
        Args:
            timeout (float): default timeout
            raise_error (bool): return bool or raise error

        Returns:
            bool: works when raise_error is False

        Raises:
            WDAElementNotDisappearError
        """
        start_time = time.time()
        if timeout is None or timeout <= 0:
            timeout = self._timeout
        while start_time + timeout > time.time():
            if not self.exists:
                return True
        if not raise_error:
            return False
        raise WDAElementNotDisappearError("element not gone")

    # todo
    # pinch
    # touchAndHold
    # dragfromtoforduration
    # FFF

    # todo
    # handleGetIsAccessibilityContainer
    # [[FBRoute GET:@"/wda/element/:uuid/accessibilityContainer"] respondWithTarget:self action:@selector(handleGetIsAccessibilityContainer:)],


class Element(object):
    def __init__(self, session: Session, id: str):
        """
        base_url eg: http://localhost:8100/session/$SESSION_ID
        """
        self._session = session
        self._id = id

    def __repr__(self):
        return '<wdap.Element(id="{}")>'.format(self._id)

    @property
    def http(self):
        return self._session._session_http

    def _req(self, method, url, data=None):
        return self.http.fetch(method, '/element/' + self._id + url, data)

    def _wda_req(self, method, url, data=None):
        return self.http.fetch(method, '/wda/element/' + self._id + url, data)

    def _prop(self, key):
        return self._req('GET', '/' + key.lstrip('/')).value

    def _wda_prop(self, key):
        ret = self.http.get('/wda/element/%s/%s' % (self._id, key)).value
        return ret

    @property
    def info(self):
        return {
            "id": self._id,
            "label": self.label,
            "value": self.value,
            "text": self.text,
            "name": self.name,
            "className": self.className,
            "enabled": self.enabled,
            "displayed": self.displayed,
            "visible": self.visible,
            "accessible": self.accessible,
            "accessibilityContainer": self.accessibility_container
        }

    @property
    def id(self):
        return self._id

    @property
    def label(self):
        return self._prop('attribute/label')

    @property
    def className(self):
        return self._prop('attribute/type')

    @property
    def text(self):
        return self._prop('text')

    @property
    def name(self):
        return self._prop('name')

    @property
    def displayed(self):
        return self._prop("displayed")

    @property
    def enabled(self):
        return self._prop('enabled')

    @property
    def accessible(self):
        return self._wda_prop("accessible")

    @property
    def accessibility_container(self):
        return self._wda_prop('accessibilityContainer')

    @property
    def value(self):
        return self._prop('attribute/value')

    @property
    def visible(self):
        return self._prop('attribute/visible')

    @property
    def bounds(self) -> Rect:
        value = self._prop('rect')
        x, y = value['x'], value['y']
        w, h = value['width'], value['height']
        return Rect(x, y, w, h)

    # operations
    def tap(self):
        return self._req('post', '/click')

    def click(self):
        """
        Get element center position and do click, a little slower
        """
        # Some one reported, invisible element can not click
        # So here, git position and then do tap
        x, y = self.bounds.center
        self._session.click(x, y)
        # return self.tap()

    def tap_hold(self, duration=1.0):
        """
        Tap and hold for a moment

        Args:
            duration (float): seconds of hold time

        [[FBRoute POST:@"/wda/element/:uuid/touchAndHold"] respondWithTarget:self action:@selector(handleTouchAndHold:)],
        """
        return self._wda_req('post', '/touchAndHold', {'duration': duration})

    def scroll(self, direction='visible', distance=1.0):
        """
        Args:
            direction (str): one of "visible", "up", "down", "left", "right"
            distance (float): swipe distance, only works when direction is not "visible"

        Raises:
            ValueError

        distance=1.0 means, element (width or height) multiply 1.0
        """
        if direction == 'visible':
            self._wda_req('post', '/scroll', {'toVisible': True})
        elif direction in ['up', 'down', 'left', 'right']:
            self._wda_req('post', '/scroll', {
                'direction': direction,
                'distance': distance
            })
        else:
            raise ValueError("Invalid direction")
        return self

    # TvOS
    # @property
    # def focused(self):
    #
    # def focuse(self):

    def pickerwheel_select(self):
        """ Select by pickerwheel """
        # Ref: https://github.com/appium/WebDriverAgent/blob/e5d46a85fbdb22e401d396cedf0b5a9bbc995084/WebDriverAgentLib/Commands/FBElementCommands.m#L88
        raise NotImplementedError()

    def pinch(self, scale, velocity):
        """
        Args:
            scale (float): scale must > 0
            velocity (float): velocity must be less than zero when scale is less than 1

        Example:
            pinchIn  -> scale:0.5, velocity: -1
            pinchOut -> scale:2.0, velocity: 1
        """
        data = {'scale': scale, 'velocity': velocity}
        return self._wda_req('post', '/pinch', data)

    def set_text(self, value):
        return self._req('post', '/value', {'value': value})

    def clear_text(self):
        return self._req('post', '/clear')

    # def child(self, **kwargs):
    #     return Selector(self.__base_url, self._id, **kwargs)

    # todo lot of other operations
    # tap_hold

    def screenshot(self, png_filename: Optional[str] = None, format='pillow'):
        """
        截取元素图片 GET /element/$id/screenshot

        Args:
            png_filename (str): 可选，保存文件名
            format (str): "raw" 或 "pillow"（默认）

        Returns:
            PIL.Image 或 png 二进制
        """
        value = self._req('GET', '/screenshot').value
        raw_value = base64.b64decode(value)
        if png_filename:
            with open(png_filename, 'wb') as f:
                f.write(raw_value)
        if format == 'raw':
            return raw_value
        elif format == 'pillow':
            from PIL import Image
            return Image.open(io.BytesIO(raw_value)).convert("RGB")
        raise ValueError("unknown format")

    def double_tap(self):
        """双击 POST /wda/element/:uuid/doubleTap"""
        return self._wda_req('post', '/doubleTap')

    def two_finger_tap(self):
        """双指点击 POST /wda/element/:uuid/twoFingerTap"""
        return self._wda_req('post', '/twoFingerTap')

    def tap_with_number_of_taps(self, taps: int = 2, touches: int = 1):
        """
        多击 POST /wda/element/:uuid/tapWithNumberOfTaps

        Args:
            taps (int): 连击次数
            touches (int): 同时按下的手指数
        """
        return self._wda_req('post', '/tapWithNumberOfTaps', {
            "numberOfTaps": taps,
            "numberOfTouches": touches,
        })

    def force_touch(self, pressure: float = 1.0, duration: float = 1.0,
                    x: Optional[float] = None, y: Optional[float] = None):
        """
        3D Touch 重按 POST /wda/element/:uuid/forceTouch

        Args:
            pressure (float): 按压力度
            duration (float): 持续时间（秒）
            x, y: 可选，元素内的按压点；不传则由服务端决定
        """
        data = {"pressure": pressure, "duration": duration}
        if x is not None and y is not None:
            data.update({"x": x, "y": y})
        return self._wda_req('post', '/forceTouch', data)

    def rotate(self, rotation: float, velocity: float = 1.0):
        """
        旋转手势 POST /wda/element/:uuid/rotate

        Args:
            rotation (float): 旋转弧度
            velocity (float): 旋转速度
        """
        return self._wda_req('post', '/rotate',
                             {"rotation": rotation, "velocity": velocity})

    def swipe_direction(self, direction: str, velocity: Optional[float] = None):
        """
        按方向滑动 POST /wda/element/:uuid/swipe

        Args:
            direction (str): up / down / left / right
            velocity (float): 滑动速度
        """
        if direction not in ('up', 'down', 'left', 'right'):
            raise ValueError("Invalid direction:", direction)
        data = {"direction": direction}
        if velocity is not None:
            data["velocity"] = velocity
        return self._wda_req('post', '/swipe', data)

    def press_and_drag(self, to_element,
                       press_duration: float = 0.5,
                       hold_duration: float = 0.5,
                       velocity: float = 500.0):
        """
        长按本元素后拖到另一个元素 POST /wda/element/:uuid/pressAndDragWithVelocity

        Args:
            to_element (Element or str): 目标元素或元素 id
        """
        to_id = to_element.id if isinstance(to_element, Element) else str(to_element)
        return self._wda_req('post', '/pressAndDragWithVelocity', {
            "toElement": to_id,
            "pressDuration": press_duration,
            "holdDuration": hold_duration,
            "velocity": velocity,
        })

    def drag(self, from_x: float, from_y: float, to_x: float, to_y: float,
             duration: float = 0.5):
        """
        在元素内拖拽 POST /wda/element/:uuid/dragfromtoforduration

        Args:
            from_x, from_y, to_x, to_y (float): 相对元素的偏移
            duration (float): 拖拽时长（秒）
        """
        return self._wda_req('post', '/dragfromtoforduration', {
            "fromX": from_x, "fromY": from_y,
            "toX": to_x, "toY": to_y,
            "duration": duration,
        })

    def scroll_to(self):
        """滚动到本元素可见 POST /wda/element/:uuid/scrollTo"""
        return self._wda_req('post', '/scrollTo')

    def get_visible_cells(self) -> list:
        """获取可见 cell 元素 id 列表 GET /wda/element/:uuid/getVisibleCells"""
        return self._wda_req('get', '/getVisibleCells').value

    def keyboard_input(self, keys: list):
        """
        按键序列输入 POST /wda/element/:uuid/keyboardInput

        Args:
            keys (list): 键名列表，需要 Xcode15+ / iPadOS17+
        """
        if not isinstance(keys, (list, tuple)):
            raise TypeError("keys must be a list")
        return self._wda_req('post', '/keyboardInput', {"keys": list(keys)})

    def focuse(self):
        """获取焦点 POST /wda/element/:uuid/focuse（主要用于 tvOS）"""
        return self._wda_req('post', '/focuse')

    @property
    def focused(self) -> bool:
        """是否获得焦点 GET /element/:uuid/attribute/focused"""
        return self._req('GET', '/attribute/focused').value

    def selected(self):
        ''' Element has been selected.
        Returns: bool
        '''
        return self._req('GET', '/selected').value


class USBClient(Client):
    """通过 USB（usbmux）连接设备上的 WDA。

    传输方式由 ``transport`` 决定：

    * ``"forward"`` —— **在库内起一个本地转发**（``wdap.usbmux.UsbmuxPortForwarder``），
      把 ``设备:port`` 映射为 ``http://127.0.0.1:<随机端口>``。HTTP 走标准 socket，
      keep-alive 正常复用，隧道按需建立、用完即关；没有额外依赖。
    * ``"usbmux"`` —— 旧行为，URL 直接用 ``http+usbmux://...``。每条 HTTP 请求都会
      重新 ``select_device()`` + 建一条 usbmux 隧道（各开 2 条 usbmuxd 连接），高频调用
      会把 usbmuxd 连接数推高到上限，之后新隧道被 RST（``WinError 10054``）。
    * ``"auto"`` —— 默认。优先 ``forward``，本机转发起不来时回退 ``usbmux``。
    """

    def __init__(self,
                 udid: str = "",
                 port: int = 8100,
                 wda_bundle_id=None,
                 auto_activate: bool = True,
                 activate_timeout: float = 20.0,
                 probe_tries: int = 3,
                 probe_timeout: float = 5.0,
                 transport: str = "auto",
                 wda_backend: str = "auto",
                 fallback: bool = True,
                 tidevice_path=None,
                  goios_path=None,
                  mount_image: bool = False,
                  start_tunnel: bool = True,
                  tunnel_mode: str = "auto"):
        """
        Args:
            udid: 设备 UDID；留空时自动选择唯一一台 USB 设备
            port: 设备上 WDA 监听的端口，默认 8100
            wda_bundle_id: 传给 tidevice / go-ios 的 WDA bundle id
            auto_activate: 探测不到 WDA 时是否自动拉起它。
                WDA 由外部工具托管时请设 False，避免无谓的尝试。
            activate_timeout: 触发拉起后，等待 WDA 就绪的秒数
            probe_tries: 首次探测的重试次数。首次请求要先建隧道/转发，
                单次探测容易把建连慢/瞬时 RST 误判成"WDA 没启动"。
            probe_timeout: 单次探测的 HTTP 超时（秒）
            transport: ``auto`` / ``forward`` / ``usbmux``，见类文档
            wda_backend: 拉起 WDA 的后端 —— ``auto``（默认，按 iOS 版本选）/
                ``tidevice``（iOS 16 及以下）/ ``goios``（iOS 17 及以上）。
                iOS 17+ 的 testmanagerd 换成了 RemoteXPC，tidevice 拉不起来。
            fallback: 首选后端拉不起来时，是否换另一个后端再试一次
            tidevice_path: tidevice / tins2 的可执行文件路径，省略则查 PATH
            goios_path: go-ios（``ios``）的可执行文件路径，省略则查 PATH
            mount_image: 拉起前先跑 ``ios image auto`` 挂载开发者镜像。
                仅 go-ios 后端需要；tidevice 会自己挂载，无需开启。
            start_tunnel: 拉起前先确保 go-ios tunnel daemon 在跑。
                **iOS 17+ 的硬前置条件** —— go-ios 的 ``runwda`` 不会自己起隧道，
                没有隧道会在连接 testmanagerd（RemoteXPC）时失败。
                已在别处手动跑过 ``ios tunnel start`` 时可设 False。
            tunnel_mode: ``kernel``（Linux/macOS 通常要 sudo、Windows 要管理员）
                / ``userspace``（Windows 需把 wintun.dll 放到 C:/Windows/system32）
        """
        if wda_backend not in ALL_WDA_STRATEGIES:
            raise ValueError("wda_backend 只能是 %s，收到 %r"
                             % ("/".join(ALL_WDA_STRATEGIES), wda_backend))
        if not udid:
            infos = [info for info in list_devices() if info.connection_type == 'USB']
            if len(infos) == 0:
                raise RuntimeError("no device connected")
            elif len(infos) >= 2:
                raise RuntimeError("more then one device connected")
            udid = infos[0].serial

        if transport not in ("auto", "forward", "usbmux"):
            raise ValueError("transport 只能是 auto / forward / usbmux，收到 %r" % (transport,))

        self.udid = udid
        self.port = int(port)
        self.transport = transport
        self._forwarder = None
        #: 上一次拉起 WDA 的结果（WdaLaunchResult），没触发拉起时为 None
        self.last_launch = None

        url = f"http+usbmux://{udid}:{port}"
        if transport in ("auto", "forward"):
            forwarder = UsbmuxPortForwarder(udid=udid, remote_port=self.port,
                                            logger=logger.debug)
            try:
                forwarder.start()
            except Exception as err:  # noqa: BLE001
                if transport == "forward":
                    raise RuntimeError("本机转发启动失败：{}".format(err)) from err
                logger.debug("local forward 不可用（%r），回退 http+usbmux", err)
            else:
                self._forwarder = forwarder
                url = forwarder.url
                logger.debug("USB transport = local forward: %s -> %s:%d",
                             url, udid, self.port)

        self._transport_url = url
        super().__init__(url=url)

        if self.is_ready(timeout=probe_timeout, tries=probe_tries):
            return

        # 探不通 —— 但"探不通"≠"WDA 没启动"，先把真实原因拿到手
        _, err = self.probe(timeout=probe_timeout)
        # 本机转发模式下，真正的失败点在"开隧道"这一步，比上层的连接被关闭更精确
        tunnel_err = getattr(self._forwarder, "last_tunnel_error", None)
        channel_err = tunnel_err if tunnel_err is not None else err
        reason = _explain_probe_error(err)
        if tunnel_err is not None:
            tunnel_reason = _explain_probe_error(tunnel_err)
            if tunnel_reason != reason:
                reason = "{}；隧道层: {}".format(reason, tunnel_reason)
        logger.warning("WDA not ready at %s: %s", url, reason)

        if not auto_activate:
            raise RuntimeError(
                "WDA not ready at {} ({}). auto_activate=False，"
                "请先自行启动 WDA（如 go-ios runwda）后再连接。".format(url, reason))

        if _probe_error_is_device_channel(channel_err, udid):
            raise RuntimeError(
                "无法激活 WDA：{}（{}）。usbmuxd 控制通道本身不可用（连设备都枚举不到或"
                "usbmuxd 无响应），启动 WDA 也救不了。请先确认：Apple Mobile Device Service "
                "在运行、USB 已连接并点了『信任』、没有别的工具独占设备。\n"
                "也可以绕开 USB/usbmux 通道：\n"
                "  · WiFi 直连：Client('http://<设备IP>:8100')\n"
                "  · go-ios 转发：ios forward 8100 8100 之后 Client('http://127.0.0.1:8100')\n"
                "  再用 USBClient(auto_activate=False) 或 Client(...) 连接。".format(url, reason))

        # 挂镜像 / 起隧道都在 start_wda 内部按后端处理：
        # 只有 go-ios 后端需要（tidevice 会在 xctest 内部自己挂镜像）
        launch = start_wda(udid,
                           wda_bundle_id=wda_bundle_id,
                           strategy=wda_backend,
                           wda_port=self.port,
                           fallback=fallback,
                           tidevice_path=tidevice_path,
                           goios_path=goios_path,
                           mount_image=mount_image,
                           start_tunnel=start_tunnel,
                           tunnel_mode=tunnel_mode)
        self.last_launch = launch
        if launch.ok and self.wait_ready(timeout=activate_timeout):
            return

        launch_detail = launch.detail
        if not launch.command:
            # command 为空 = 连可执行文件都没找到，压根没执行
            launch_detail += (
                "\n未找到可用的拉起工具：iOS 16 及以下需要 tidevice/tins2，"
                "iOS 17 及以上需要 go-ios(ios)，请把对应可执行文件放进 PATH，"
                "或用 tidevice_path= / goios_path= 显式指定。")

        raise RuntimeError(
            "WDA 拉起失败 at {}（探测原因：{}）。\n"
            "后端={} iOS版本={} 命令={}\n{}\n"
            "排障：iOS 17+ 只能用 go-ios（tidevice 会报 DeveloperImage not found）；"
            "go-ios 跑 WDA 需要两个前置条件 —— 挂载开发者镜像"
            "（ios image auto --udid=<udid>）、启动隧道 daemon"
            "（ios tunnel start，kernel 模式要管理员/sudo；Windows 可选 "
            "ios tunnel start --userspace，需 wintun.dll 在 C:/Windows/system32）。"
            "也可以先手工拉起 WDA 再用 "
            "USBClient(auto_activate=False) 连接。".format(
                url, reason, launch.backend or "(无)",
                launch.ios_version or "未知",
                " ".join(launch.command) or "(未执行)", launch_detail))

    def disconnect(self):
        """释放本 client 占用的传输资源（本地转发端口 + 复用的 HTTP 连接）。

        幂等；调用后仍需继续使用请重新构造 client。
        """
        forwarder, self._forwarder = self._forwarder, None
        if forwarder is not None:
            forwarder.stop()
            logger.debug("USB local forward stopped (%s)", self._transport_url)
        close_pool(getattr(self, "_transport_url", None))

    def __del__(self):
        try:
            self.disconnect()
        except Exception:  # noqa: BLE001
            pass
