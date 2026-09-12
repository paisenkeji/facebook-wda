#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Created on Thu Dec 09 2021 09:56:30 by codeskyblue
"""

import atexit
import json
import threading
import time
from http.client import HTTPConnection, HTTPSConnection, HTTPResponse
from urllib.parse import urlparse

from wdap.usbmux.exceptions import HTTPError, MuxConnectError, MuxError
from wdap.usbmux.forwarder import UsbmuxPortForwarder
from wdap.usbmux.pyusbmux import select_device

__all__ = [
    "HTTPResponseWrapper",
    "UsbmuxPortForwarder",
    "close_pool",
    "fetch",
    "http_create",
]

_DEFAULT_CHUNK_SIZE = 4096

#: ``select_device()`` 结果缓存时长（秒），0 表示关闭缓存。
#:
#: ``create_mux()`` 每次都要先连一次 usbmuxd 做版本探测、再连一次建会话，所以每个
#: HTTP 请求光"解析设备"就要向 usbmuxd 开 2 条连接；而 http+usbmux 下常常一秒内连发
#: 多个请求（is_ready 重试、wait_ready 每秒轮询）。把设备记录缓存极短时间可以把
#: 这类连接 churn 砍掉一半——churn 本身就是"跑一会就开始被 RST"的一类诱因。
#: 取值刻意很小，设备重新插拔/换口后仍能很快重新解析。
DEVICE_CACHE_TTL = 2.0
_DEVICE_CACHE = {}  # udid -> (expire_at, MuxDevice)

# --------------------------------------------------------------------------- #
# HTTP 连接池
# --------------------------------------------------------------------------- #
#: 是否复用 HTTP 连接（keep-alive）。设为 False 恢复"每请求新建连接"的旧行为。
POOL_ENABLED = True
#: 同一个 URL 最多缓存多少条空闲连接
POOL_MAX_IDLE_PER_URL = 4
#: 空闲连接最长保留时间（秒）。WDA 端的 HTTP 服务有自己的 keep-alive 超时，
#: 超过这个时间没被使用的连接直接丢弃重建，避免复用到已经半死的连接。
POOL_IDLE_TTL = 30.0

_POOL = {}  # url -> [(expire_at, HTTPConnection)]
_POOL_LOCK = threading.Lock()
_POOL_ATEXIT_REGISTERED = False


def _close_quietly(obj):
    try:
        obj.close()
    except Exception:  # noqa: BLE001
        pass


def _pool_key(url: str) -> str:
    """连接池的键：``scheme://netloc``。

    必须按 host 而不是完整 URL 复用——WDA 的每个接口路径都不同，
    若用完整 URL 当键，池里会变成"每个接口一条连接"，复用等于没做。
    """
    u = urlparse(url)
    return "%s://%s" % (u.scheme, u.netloc)


def _acquire(key: str):
    """从池里取一条可复用的连接；没有则返回 None。"""
    if not POOL_ENABLED:
        return None
    now = time.time()
    with _POOL_LOCK:
        bucket = _POOL.get(key)
        while bucket:
            expire_at, conn = bucket.pop()
            if expire_at > now:
                return conn
            _close_quietly(conn)
        _POOL.pop(key, None)
    return None


def _release(key: str, conn) -> None:
    """把连接放回池里；不适合复用的直接关闭。

    服务端声明了 ``Connection: close`` 时，``http.client`` 会把 ``conn.sock`` 置 None，
    这种连接没必要留着（下次用会自动重连，等于没省）。
    """
    if conn is None:
        return
    if not POOL_ENABLED or getattr(conn, "sock", None) is None:
        _close_quietly(conn)
        return
    with _POOL_LOCK:
        bucket = _POOL.setdefault(key, [])
        if len(bucket) >= POOL_MAX_IDLE_PER_URL:
            _close_quietly(conn)
            return
        bucket.append((time.time() + POOL_IDLE_TTL, conn))


def _discard(conn) -> None:
    if conn is not None:
        _close_quietly(conn)


def close_pool(url: str = None) -> int:
    """关闭池里的空闲连接。``url`` 为 None 时清空全部，返回关闭的连接数。

    ``url`` 可以是完整 URL（如 ``http://127.0.0.1:1234/status``）或 host 前缀，
    内部按 ``scheme://netloc`` 归一化。
    """
    with _POOL_LOCK:
        if url is None:
            items = [c for bucket in _POOL.values() for (_, c) in bucket]
            _POOL.clear()
        else:
            items = [c for (_, c) in _POOL.pop(_pool_key(url), [])]
    for conn in items:
        _close_quietly(conn)
    return len(items)


def _pool_atexit():
    global _POOL_ATEXIT_REGISTERED
    with _POOL_LOCK:
        if _POOL_ATEXIT_REGISTERED:
            return
        _POOL_ATEXIT_REGISTERED = True
    atexit.register(close_pool)


def _select_device(udid: str):
    """带短 TTL 缓存的 select_device。

    解析不到设备时**不缓存**，否则"设备刚插上"要多等一个 TTL 才能恢复。
    """
    ttl = DEVICE_CACHE_TTL
    now = time.time()
    if ttl > 0:
        cached = _DEVICE_CACHE.get(udid)
        if cached and cached[0] > now:
            return cached[1]
    device = select_device(udid)
    if device is not None and ttl > 0:
        _DEVICE_CACHE[udid] = (now + ttl, device)
    return device


def _invalidate_device(udid: str) -> None:
    """作废缓存：连接层异常通常意味着缓存的 devid 已失效（重新插拔/换端口）"""
    _DEVICE_CACHE.pop(udid, None)


def _udid_of(url: str) -> str:
    try:
        return urlparse(url).netloc.split(":")[0]
    except Exception:  # noqa: BLE001
        return ""


def http_create(url: str) -> HTTPConnection:
    u = urlparse(url)
    if u.scheme == "http+usbmux":
        udid, device_wda_port = u.netloc.split(":")
        device = _select_device(udid)
        return device.make_http_connection(int(device_wda_port))
    elif u.scheme == "http":
        return HTTPConnection(u.netloc)
    elif u.scheme == "https":
        return HTTPSConnection(u.netloc)
    else:
        raise ValueError(f"unknown scheme: {u.scheme}")


class HTTPResponseWrapper:
    def __init__(self, content: bytes, status_code: int):
        self.content = content
        self.status_code = status_code

    def json(self):
        return json.loads(self.content)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def getcode(self) -> int:
        return self.status_code


def fetch(url: str, method="GET", data=None, timeout=None, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> HTTPResponseWrapper:
    """
    thread safe http request

    连接可复用：同一 ``url`` 上成功读完响应后会把连接放回池里（keep-alive），
    这样 ``http+usbmux`` 下就不用每个请求都重新建一条 usbmux 隧道——那正是
    usbmuxd 连接被耗尽、新隧道被 RST（WinError 10054）的根源。

    Raises:
        HTTPError
    """
    _pool_atexit()
    method = method.upper()
    key = _pool_key(url)
    conn = None
    try:
        conn = _acquire(key)
        if conn is None:
            conn = http_create(url)

        if timeout is not None:
            # 必须在建连前/连上后立刻作用到 socket 上：http+usbmux 的实现不理会
            # HTTPConnection.timeout，光赋值 conn.timeout 对已经建好的 socket 无效。
            conn.timeout = timeout
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.settimeout(timeout)
                except OSError:
                    pass

        u = urlparse(url)
        urlpath = url[len(u.scheme) + len(u.netloc) + 3:]

        if not data:
            conn.request(method, urlpath)
        else:
            conn.request(method, urlpath, json.dumps(data), headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        content = _read_response(response, chunk_size)
        resp = HTTPResponseWrapper(content, response.status)
    except Exception as e:
        # 走到这里说明连接层出了问题（RST/中止/超时）。缓存的设备记录可能已经失效
        # （设备重插、换了 USB 口、usbmuxd 重启），立刻作废，下次请求重新解析；
        # 复用中的连接也必须丢弃——它可能已经被对端关掉了。
        _invalidate_device(_udid_of(url))
        _discard(conn)
        raise HTTPError(e)

    _release(key, conn)
    return resp


def _read_response(response: HTTPResponse, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> bytearray:
    content = bytearray()
    while True:
        chunk = response.read(chunk_size)
        if len(chunk) == 0:
            break
        content.extend(chunk)
    return content
