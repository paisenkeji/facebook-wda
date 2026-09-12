#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wdap 设备激活（USB）自检脚本

本机没有真机，这里用假的 lockdown 客户端 + 假的 HTTP 通道验证**协议顺序与分支**，
真机上只要 lockdown 能连通，跑的就是这一模一样的字节流。

运行::

    python verify_activation.py
"""

from __future__ import annotations

import os
import socket
import sys
import unittest
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wdap  # noqa: E402
from wdap import activation as act  # noqa: E402
from wdap.exceptions import (ActivationError, ActivationServerError,  # noqa: E402
                             DeviceNotFoundError, DeviceNotPairedError)
from wdap.lockdown import PlistSocket  # noqa: E402

TEST_UDID = "00008030-000D65402EF8202E"


class FakeService(object):
    """模拟 mobileactivationd 的一条服务连接"""

    def __init__(self, handler):
        self._handler = handler

    def send_recv(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._handler(payload)

    def __enter__(self) -> "FakeService":
        return self

    def __exit__(self, *args) -> None:
        return None


class FakeLockdown(object):
    """模拟 LockdownClient：只实现激活流程用到的接口"""

    def __init__(self, state_box: Dict[str, Any],
                 commands: List[Tuple[str, str]],
                 handshake_info: Optional[Dict[str, Any]] = None,
                 activation_info: Optional[Dict[str, Any]] = None,
                 fail_value_type: bool = False,
                 rejected_commands=()):
        self.udid = TEST_UDID
        self._rejected_commands = set(rejected_commands)
        self._state_box = state_box
        self._commands = commands
        self._handshake_info = handshake_info or {"HandshakeRequestMessage": b"handshake-bytes"}
        self._activation_info = activation_info or {"ActivationInfo": b"activation-bytes"}
        self._fail_value_type = fail_value_type
        self.closed = False

    def get_value(self, key: Optional[str] = None, domain: Optional[str] = None) -> Any:
        self._commands.append(("GetValue", key))
        return self._state_box["state"]

    def open_service(self, name: str) -> FakeService:
        self._commands.append(("StartService", name))
        return FakeService(self._handle)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeLockdown":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _handle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        command = payload.get("Command")
        self._commands.append(("Command", command))
        if command == "CreateTunnel1SessionInfoRequest":
            value: Any = self._handshake_info
            if self._fail_value_type:
                value = "not-a-dict"
            return {"Value": value}
        if command in ("CreateActivationInfoRequest",
                       "CreateTunnel1ActivationInfoRequest"):
            if command in self._rejected_commands:
                return {"Error": "UnsupportedCommand"}
            assert payload["Value"] == b"handshake-response", payload["Value"]
            assert payload["Options"] == {"BasebandWaitCount": 90}, payload["Options"]
            return {"Value": self._activation_info}
        if command == "HandleActivationInfoWithSessionRequest":
            assert payload["Value"] == b"activation-response"
            # 重复头按 last-wins 合并，设备端收到的应是 application/xml
            assert payload["ActivationResponseHeaders"]["Content-Type"] == "application/xml"
            self._state_box["state"] = "Activated"
            return {"Value": {}}
        raise AssertionError("意外的 Command: %s" % command)


class FakeHttp(object):
    def __init__(self):
        self.calls: List[Tuple[str, bytes, str, str]] = []

    def __call__(self, url: str, body: bytes, content_type: str, accept: str,
                 timeout: float = 30.0, proxy: Optional[str] = None):
        self.calls.append((url, body, content_type, accept))
        if url == act.DRM_HANDSHAKE_URL:
            return 200, [("Content-Type", "application/xml")], b"handshake-response"
        return (200,
                [("Content-Type", "text/xml"), ("Content-Type", "application/xml")],
                b"activation-response")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.commands: List[Tuple[str, str]] = []
        self.http = FakeHttp()
        self._origin_client = act.LockdownClient
        self._origin_post = act._http_post
        act._http_post = self.http

    def tearDown(self) -> None:
        act.LockdownClient = self._origin_client
        act._http_post = self._origin_post

    def install_client(self, state: Optional[str], **kwargs) -> FakeLockdown:
        box = {"state": state}
        fake = FakeLockdown(box, self.commands, **kwargs)
        act.LockdownClient = lambda udid=None, usbmux_address=None, timeout=15.0, pair_record=None: fake
        return fake


# --------------------------------------------------------------------------- #
class TestPlistSocket(Base):
    def test_roundtrip(self):
        left, right = socket.socketpair()
        try:
            writer = PlistSocket(left, timeout=1.0)
            writer.send({"Hello": "world", "n": 1})
            raw = right.recv(4096)
            self.assertEqual(raw[:4], (len(raw) - 4).to_bytes(4, "big"))
            # 把收到的数据原样喂回给另一个 PlistSocket
            right.sendall(raw)
            reader = PlistSocket(writer.sock, timeout=1.0)
            self.assertEqual(reader.recv(), {"Hello": "world", "n": 1})
        finally:
            left.close()
            right.close()

    def test_invalid_length(self):
        left, right = socket.socketpair()
        try:
            right.sendall((0xFFFFFFFF).to_bytes(4, "big"))
            with self.assertRaises(Exception):
                PlistSocket(left, timeout=1.0).recv()
        finally:
            left.close()
            right.close()


# --------------------------------------------------------------------------- #
class TestActivationFlow(Base):
    def test_already_activated_does_nothing(self):
        self.install_client("Activated")
        result = act.activate_device(TEST_UDID)
        self.assertTrue(result.already_activated)
        self.assertTrue(result.activated)
        # 已激活时直接返回，不再回读、不再发任何 HTTP
        self.assertEqual(self.commands, [("GetValue", "ActivationState")])
        self.assertEqual(self.http.calls, [])

    def test_full_flow(self):
        self.install_client("Unactivated")
        result = act.activate_device(TEST_UDID)

        self.assertFalse(result.already_activated)
        self.assertTrue(result.activated)
        self.assertEqual(result.state_before, "Unactivated")
        self.assertEqual(result.state_after, "Activated")
        self.assertEqual(result.udid, TEST_UDID)

        # 三步 mobileactivationd + 两次 HTTP
        self.assertEqual(
            self.commands,
            [("GetValue", "ActivationState"),
             ("StartService", act.ACTIVATION_SERVICE), ("Command", "CreateTunnel1SessionInfoRequest"),
             ("StartService", act.ACTIVATION_SERVICE),
             ("Command", act.ACTIVATION_INFO_COMMANDS[0]),
             ("StartService", act.ACTIVATION_SERVICE), ("Command", "HandleActivationInfoWithSessionRequest"),
             ("GetValue", "ActivationState")])

        self.assertEqual(len(self.http.calls), 2)
        handshake_url, handshake_body, handshake_ct, handshake_accept = self.http.calls[0]
        self.assertEqual(handshake_url, act.DRM_HANDSHAKE_URL)
        self.assertEqual(handshake_ct, "application/x-apple-plist")
        self.assertEqual(handshake_accept, "application/xml")
        self.assertIn(b"HandshakeRequestMessage", handshake_body)
        self.assertIn(b"<plist", handshake_body)

        activation_url, activation_body, activation_ct, _accept = self.http.calls[1]
        self.assertEqual(activation_url, act.DEVICE_ACTIVATION_URL)
        self.assertEqual(activation_ct, "application/x-www-form-urlencoded")
        self.assertTrue(activation_body.startswith(b"activation-info="))

    def test_activation_info_command_fallback(self):
        # 设备不认 Tunnel1 版的命令时，应回退到 go-ios 用的那个
        self.install_client(
            "Unactivated",
            rejected_commands=[act.ACTIVATION_INFO_COMMANDS[0]])
        result = act.activate_device(TEST_UDID)
        self.assertTrue(result.activated)
        used = [name for kind, name in self.commands if kind == "Command"]
        # 第一个 Tunnel1 命令被拒后继续尝试第二个，最终成功
        self.assertEqual(used, ["CreateTunnel1SessionInfoRequest",
                                act.ACTIVATION_INFO_COMMANDS[0],
                                act.ACTIVATION_INFO_COMMANDS[1],
                                "HandleActivationInfoWithSessionRequest"])

    def test_activation_info_all_commands_rejected(self):
        self.install_client("Unactivated",
                            rejected_commands=list(act.ACTIVATION_INFO_COMMANDS))
        with self.assertRaises(ActivationError) as ctx:
            act.activate_device(TEST_UDID)
        self.assertIn("CreateActivationInfoRequest", str(ctx.exception))

    def test_bad_value_type(self):
        self.install_client("Unactivated", fail_value_type=True)
        with self.assertRaises(ActivationError):
            act.activate_device(TEST_UDID)

    def test_empty_server_response(self):
        self.install_client("Unactivated")
        act._http_post = lambda *a, **kw: (500, [], b"")
        with self.assertRaises(ActivationServerError):
            act.activate_device(TEST_UDID)

    def test_http_headers_merged_last_wins(self):
        # 重复的 Content-Type 头应保留最后一个（与 go-ios 行为一致）
        merged: Dict[str, str] = {}
        for name, value in [("Content-Type", "text/xml"), ("Content-Type", "application/xml")]:
            merged[name] = value
        self.assertEqual(merged["Content-Type"], "application/xml")

    def test_state_helpers(self):
        self.install_client("Activated")
        self.assertEqual(act.activation_state(TEST_UDID), "Activated")
        self.assertTrue(act.is_device_activated(TEST_UDID))

        self.install_client("Unactivated")
        self.assertFalse(act.is_device_activated(TEST_UDID))


# --------------------------------------------------------------------------- #
class TestErrorPaths(Base):
    def test_no_usb_device(self):
        def boom(*args, **kwargs):
            raise DeviceNotFoundError("没有 USB 设备")

        act.LockdownClient = boom
        with self.assertRaises(DeviceNotFoundError):
            act.activate_device()

    def test_not_paired(self):
        def boom(*args, **kwargs):
            raise DeviceNotPairedError("没有配对记录")

        act.LockdownClient = boom
        with self.assertRaises(DeviceNotPairedError):
            act.activate_device(TEST_UDID)

    def test_batch_never_raises(self):
        origin_list = act.list_usb_devices
        act.list_usb_devices = lambda *a, **kw: [TEST_UDID, "BAD-UDID"]

        def factory(udid=None, usbmux_address=None, timeout=15.0, pair_record=None):
            if udid == "BAD-UDID":
                raise DeviceNotPairedError("没有配对记录")
            box = {"state": "Activated"}
            return FakeLockdown(box, [])

        act.LockdownClient = factory
        try:
            results = act.activate_all_usb_devices()
        finally:
            act.list_usb_devices = origin_list
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].activated)
        self.assertFalse(results[1].activated)
        self.assertIn("DeviceNotPairedError", results[1].detail)


# --------------------------------------------------------------------------- #
class TestPublicApi(Base):
    def test_top_level_exports(self):
        for name in ("activate_device", "activate_all_usb_devices",
                     "activation_state", "is_device_activated",
                     "ActivationResult", "LockdownClient", "list_usb_devices",
                     "load_pair_record", "PairRecord", "LOCKDOWN_PORT"):
            self.assertTrue(hasattr(wdap, name), "wdap 缺少导出: %s" % name)

    def test_lockdown_port(self):
        self.assertEqual(wdap.LOCKDOWN_PORT, 62078)

    def test_cli_list(self):
        origin = act.list_usb_devices
        act.list_usb_devices = lambda *a, **kw: [TEST_UDID]
        try:
            self.assertEqual(act.main(["--list"]), 0)
        finally:
            act.list_usb_devices = origin


if __name__ == "__main__":
    unittest.main(verbosity=2)
