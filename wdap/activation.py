#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通过 USB 激活 iOS 设备（对应 go-ios 的 ``ios activate``）

实现参考 danielpaulus/go-ios：
- ``ios/mobileactivation/activation.go`` —— 三步 mobileactivationd 协议
- ``ios/mobileactivation/albert.go`` —— 与 albert.apple.com 的两个 HTTP 请求

流程（设备必须能上网，或在能访问 albert.apple.com 的机器上跑）：

1. lockdown 读 ``ActivationState``，不是 ``Unactivated`` 就直接返回；
2. ``CreateTunnel1SessionInfoRequest`` -> 拿到 handshake 请求
   -> POST ``/deviceservices/drmHandshake``；
3. ``CreateActivationInfoRequest`` -> 拿到 activation info
   -> POST ``/deviceservices/deviceActivation``；
4. ``HandleActivationInfoWithSessionRequest`` 把响应写回设备 -> 激活完成。

只支持 **USB 直连** 设备：Wi-Fi 设备不在 usbmux 的 USB 列表里，会被直接排除。
"""

from __future__ import annotations

import logging
import plistlib
import urllib.error
import urllib.request
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlencode

from wdap.exceptions import (ActivationError, ActivationServerError,
                             DeviceNotFoundError, LockdownError)
from wdap.lockdown import LockdownClient, list_usb_devices
from wdap.usbmux.exceptions import MuxConnectToUsbmuxdError

logger = logging.getLogger("wdap.activation")

#: 设备上的激活守护进程
ACTIVATION_SERVICE = "com.apple.mobileactivationd"

DRM_HANDSHAKE_URL = "https://albert.apple.com/deviceservices/drmHandshake"
DEVICE_ACTIVATION_URL = "https://albert.apple.com/deviceservices/deviceActivation"

#: 必须伪装成 iOS Device Activator，否则 Apple 服务器会拒绝
USER_AGENT = "iOS Device Activator (MobileActivation-592.103.2)"

ACTIVATION_STATE_KEY = "ActivationState"
STATE_UNACTIVATED = "Unactivated"
STATE_ACTIVATED = "Activated"


class ActivationResult(NamedTuple):
    """一次激活尝试的结果"""

    udid: str
    #: 激活前的 ActivationState（读不到时为 None）
    state_before: Optional[str]
    #: 激活后的 ActivationState（读不到时为 None）
    state_after: Optional[str]
    #: 调用结束后设备是否处于已激活状态
    activated: bool
    #: True 表示调用前就已经激活，本次没有做任何事
    already_activated: bool
    detail: str


def _http_post(url: str,
               body: bytes,
               content_type: str,
               accept: str,
               timeout: float = 30.0,
               proxy: Optional[str] = None) -> Tuple[int, List[Tuple[str, str]], bytes]:
    """向 Apple 激活服务器发一个 POST，返回 (状态码, headers 列表, 响应体)"""
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("Accept", accept)
    request.add_header("User-Agent", USER_AGENT)

    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()

    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, list(response.headers.items()), response.read()
    except urllib.error.HTTPError as err:
        payload = b""
        try:
            payload = err.read()
        except Exception:  # noqa: BLE001
            pass
        headers = list(err.headers.items()) if err.headers is not None else []
        return err.code, headers, payload
    except urllib.error.URLError as err:
        raise ActivationServerError("访问 %s 失败: %s" % (url, err.reason))
    except OSError as err:
        raise ActivationServerError("访问 %s 失败: %s" % (url, err))


def activation_state(udid: Optional[str] = None,
                     usbmux_address: Optional[str] = None,
                     timeout: float = 15.0) -> Optional[str]:
    """读取设备的 ``ActivationState``，读不到返回 None

    Args:
        udid: 设备 UDID，为 None 时取第一条 USB 设备
        usbmux_address: 自定义 usbmuxd 地址，一般不需要
        timeout: 单次 lockdown 读写超时（秒）

    Raises:
        DeviceNotFoundError: usbmux 上没有 USB 设备
        DeviceNotPairedError: 没有配对记录
    """
    with LockdownClient(udid, usbmux_address=usbmux_address, timeout=timeout) as client:
        return client.get_value(ACTIVATION_STATE_KEY)


def is_device_activated(udid: Optional[str] = None,
                        usbmux_address: Optional[str] = None,
                        timeout: float = 15.0) -> bool:
    """设备是否已激活（与 go-ios 的 ``IsActivated`` 同语义：只要不是 Unactivated 就算激活）"""
    state = activation_state(udid, usbmux_address=usbmux_address, timeout=timeout)
    return state is not None and state != STATE_UNACTIVATED


def _expect_dict(value: Any, command: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ActivationError("%s 返回的 Value 不是字典: %r" % (command, type(value)))
    return value


def activate_device(udid: Optional[str] = None,
                    usbmux_address: Optional[str] = None,
                    timeout: float = 15.0,
                    http_timeout: float = 30.0,
                    proxy: Optional[str] = None) -> ActivationResult:
    """激活一台 USB 设备

    已激活的设备调用后什么都不做，直接返回；可以安全地重复调用。

    Args:
        udid: 设备 UDID，为 None 时取第一条 USB 设备
        usbmux_address: 自定义 usbmuxd 地址
        timeout: lockdown / mobileactivationd 单次读写超时（秒）
        http_timeout: 访问 albert.apple.com 的超时（秒）
        proxy: 形如 ``http://127.0.0.1:7890`` 的代理，访问 Apple 服务器不畅时指定

    Returns:
        ActivationResult

    Raises:
        DeviceNotFoundError / DeviceNotPairedError / LockdownError / ActivationError
    """
    client = LockdownClient(udid, usbmux_address=usbmux_address, timeout=timeout)
    udid = client.udid
    try:
        state_before = client.get_value(ACTIVATION_STATE_KEY)
        logger.debug("%s 激活前状态: %s", udid, state_before)

        if state_before is not None and state_before != STATE_UNACTIVATED:
            return ActivationResult(udid, state_before, state_before, True, True,
                                    "设备已处于 %s 状态，跳过激活" % state_before)

        # ---------- 第 1 步：向设备要 handshake 请求，转交给 Apple ----------
        handshake = _create_tunnel_session_info(client, timeout)
        handshake_plist = plistlib.dumps(handshake, fmt=plistlib.FMT_XML)
        logger.debug("%s 发送 drmHandshake，%d 字节", udid, len(handshake_plist))
        status, _headers, handshake_response = _http_post(
            DRM_HANDSHAKE_URL, handshake_plist,
            content_type="application/x-apple-plist",
            accept="application/xml",
            timeout=http_timeout, proxy=proxy)
        if not handshake_response:
            raise ActivationServerError("drmHandshake 返回空响应 (HTTP %s)" % status)

        # ---------- 第 2 步：把 handshake 响应喂回设备，取 activation info ----------
        activation_info = _create_activation_info(client, handshake_response, timeout)
        payload = urlencode({
            "activation-info": plistlib.dumps(activation_info, fmt=plistlib.FMT_XML)
        }).encode("utf-8")
        logger.debug("%s 发送 deviceActivation，%d 字节", udid, len(payload))
        status, response_headers, activation_response = _http_post(
            DEVICE_ACTIVATION_URL, payload,
            content_type="application/x-www-form-urlencoded",
            accept="*/*",
            timeout=http_timeout, proxy=proxy)
        if not activation_response:
            raise ActivationServerError("deviceActivation 返回空响应 (HTTP %s)" % status)

        # HTTP 头理论上可以多值，设备端只收 dict，这里按 go-ios 的做法取最后一个值
        merged_headers: Dict[str, str] = {}
        for name, value in response_headers:
            merged_headers[name] = value

        # ---------- 第 3 步：把激活响应写回设备 ----------
        _handle_activation_info(client, activation_response, merged_headers, timeout)
    finally:
        client.close()

    state_after = None
    try:
        state_after = activation_state(udid, usbmux_address=usbmux_address,
                                       timeout=timeout)
    except Exception as err:  # noqa: BLE001 - 回读失败不影响主流程的结果
        logger.debug("%s 回读激活状态失败: %s", udid, err)

    activated = state_after is None or state_after != STATE_UNACTIVATED
    if state_after is None:
        detail = "激活指令已下发，但未能回读到 ActivationState，请稍后复查"
    else:
        detail = "激活完成，当前状态 %s" % state_after
    return ActivationResult(udid, state_before, state_after, activated, False, detail)


def activate_all_usb_devices(usbmux_address: Optional[str] = None,
                             timeout: float = 15.0,
                             http_timeout: float = 30.0,
                             proxy: Optional[str] = None) -> List[ActivationResult]:
    """批量激活当前所有 USB 设备，单台失败不会中断其余设备"""
    results: List[ActivationResult] = []
    for udid in list_usb_devices(usbmux_address):
        try:
            results.append(activate_device(udid, usbmux_address=usbmux_address,
                                           timeout=timeout,
                                           http_timeout=http_timeout,
                                           proxy=proxy))
        except Exception as err:  # noqa: BLE001
            logger.warning("%s 激活失败: %s", udid, err)
            results.append(ActivationResult(udid, None, None, False, False,
                                            "%s: %s" % (type(err).__name__, err)))
    return results


# --------------------------------------------------------------------------- #
# mobileactivationd 三步请求
# --------------------------------------------------------------------------- #
def _create_tunnel_session_info(client: LockdownClient, timeout: float) -> Dict[str, Any]:
    with client.open_service(ACTIVATION_SERVICE) as service:
        response = service.send_recv({"Command": "CreateTunnel1SessionInfoRequest"})
    return _expect_dict(response.get("Value"), "CreateTunnel1SessionInfoRequest")


#: 第二步（拿 activation info）的命令名在两套实现里不一样：
#: go-ios 用 ``CreateActivationInfoRequest``，pymobiledevice3 用
#: ``CreateTunnel1ActivationInfoRequest``（与第一步的 Tunnel1 配套）。
#: 不同 iOS 版本认的命令不同，两个都试一遍，先试 Tunnel1 那个。
ACTIVATION_INFO_COMMANDS = ("CreateTunnel1ActivationInfoRequest",
                            "CreateActivationInfoRequest")


def _create_activation_info(client: LockdownClient,
                            handshake_response: bytes,
                            timeout: float) -> Dict[str, Any]:
    """把 handshake 响应喂回设备换 activation info，两个命令名都尝试

    设备不支持会话模式时会直接返回 ``Error``（或抛错），此时换另一个命令名；
    两个都失败才抛 ActivationError。
    """
    errors: List[str] = []
    for command in ACTIVATION_INFO_COMMANDS:
        try:
            with client.open_service(ACTIVATION_SERVICE) as service:
                response = service.send_recv({
                    "Command": command,
                    "Value": handshake_response,
                    "Options": {"BasebandWaitCount": 90},
                })
        except (ActivationError, LockdownError) as err:
            logger.debug("%s 不可用: %s", command, err)
            errors.append("%s: %s" % (command, err))
            continue

        error = response.get("Error")
        value = response.get("Value")
        if error is None and isinstance(value, dict):
            return value
        errors.append("%s: %s" % (command, error or "Value 不是字典: %r" % (type(value),)))
        logger.debug("设备拒绝了 %s: %s", command, error)

    raise ActivationError(
        "设备既不接受 %s，也不接受 %s（%s）。"
        "该设备可能只支持传统 lockdown 激活路径。"
        % (ACTIVATION_INFO_COMMANDS[0], ACTIVATION_INFO_COMMANDS[1],
           "；".join(errors) or "无更多信息"))


def _handle_activation_info(client: LockdownClient,
                            activation_response: bytes,
                            headers: Dict[str, str],
                            timeout: float) -> Dict[str, Any]:
    with client.open_service(ACTIVATION_SERVICE) as service:
        return service.send_recv({
            "Command": "HandleActivationInfoWithSessionRequest",
            "Value": activation_response,
            "ActivationResponseHeaders": headers,
        })


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m wdap.activation",
        description="通过 USB 激活 iOS 设备（对标 go-ios 的 ios activate）")
    parser.add_argument("udid", nargs="?", default=None,
                        help="设备 UDID，省略时取第一条 USB 设备")
    parser.add_argument("--list", action="store_true", help="只列出 USB 设备")
    parser.add_argument("--state", action="store_true", help="只查询激活状态")
    parser.add_argument("--all", action="store_true", help="激活所有 USB 设备")
    parser.add_argument("--timeout", type=float, default=15.0, help="lockdown 超时（秒）")
    parser.add_argument("--http-timeout", type=float, default=30.0,
                        help="Apple 服务器请求超时（秒）")
    parser.add_argument("--proxy", default=None,
                        help="访问 Apple 服务器的代理，如 http://127.0.0.1:7890")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.list:
        for udid in list_usb_devices():
            print(udid)
        return 0

    try:
        if args.state:
            state = activation_state(args.udid, timeout=args.timeout)
            print("%s: %s" % (args.udid or list_usb_devices()[0], state))
            return 0

        if args.all:
            results = activate_all_usb_devices(timeout=args.timeout,
                                               http_timeout=args.http_timeout,
                                               proxy=args.proxy)
            for item in results:
                print("%s activated=%s state=%s -> %s | %s"
                      % (item.udid, item.activated, item.state_before,
                         item.state_after, item.detail))
            return 0 if all(item.activated for item in results) else 1

        result = activate_device(args.udid, timeout=args.timeout,
                                 http_timeout=args.http_timeout, proxy=args.proxy)
        print("%s activated=%s state=%s -> %s | %s"
              % (result.udid, result.activated, result.state_before,
                 result.state_after, result.detail))
        return 0 if result.activated else 1
    except MuxConnectToUsbmuxdError as err:
        print("连不上 usbmuxd: %s" % err)
        print("Windows 需要安装 iTunes 或『Apple 移动设备支持』，"
              "并保证 Apple Mobile Device Service 正在运行")
        return 4
    except DeviceNotFoundError as err:
        print("未找到设备: %s" % err)
        return 2
    except (LockdownError, ActivationError) as err:
        print("%s: %s" % (type(err).__name__, err))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
