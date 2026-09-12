#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""USB lockdown 客户端（仅支持 USB 直连设备）

协议参考 danielpaulus/go-ios：
- ``ios/lockdown.go``、``ios/startsession.go``、``ios/startservice.go``、``ios/connect.go``
- lockdown 固定监听设备的 **62078** 端口（go-ios 里的 ``Lockdownport = 32498`` 是这个值
  的网络字节序写法，``0xF27E`` <-> ``0x7EF2``，不要照抄成 32498）

传输栈：usbmux 中继 -> 4 字节大端长度前缀的 XML plist。
``StartSession`` 之后设备会要求把这条连接升级成双向 TLS（客户端证书取自配对记录）。
"""

from __future__ import annotations

import os
import plistlib
import socket
import ssl
import struct
import sys
import tempfile
from typing import Any, Dict, Iterator, List, Optional

from wdap.exceptions import (DeviceNotFoundError, DeviceNotPairedError,
                             LockdownError, ServiceStartError)
from wdap.usbmux.exceptions import NotPairedError
from wdap.usbmux.pyusbmux import MuxDevice, create_mux, select_device

#: 设备上 lockdownd 固定监听的端口（主机字节序）
LOCKDOWN_PORT = 62078

#: 客户端标识，lockdown 只用来做日志，随意但必须存在
CLIENT_LABEL = "wdap.control"
CLIENT_PROG_NAME = "wdap"

#: 单个 plist 报文的上限，防止读到坏长度后一次性分配几个 G
MAX_PLIST_PAYLOAD = 64 * 1024 * 1024


def list_usb_devices(usbmux_address: Optional[str] = None) -> List[str]:
    """返回当前通过 USB 连接的设备 UDID 列表"""
    from wdap.usbmux.pyusbmux import list_devices

    return [d.serial for d in list_devices(usbmux_address) if d.is_usb]


def _as_bytes(value: Any) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return None


class PairRecord(object):
    """本机保存的配对记录

    字段与 Apple 的 ``Lockdown/<UDID>.plist`` 一致：
    ``HostID`` / ``SystemBUID`` / ``HostCertificate`` / ``HostPrivateKey`` / ``DeviceCertificate``
    """

    def __init__(self, data: Dict[str, Any]):
        self.raw: Dict[str, Any] = data
        self.udid: Optional[str] = data.get("UDID")
        self.host_id: Optional[str] = data.get("HostID")
        self.system_buid: Optional[str] = data.get("SystemBUID")
        self.host_certificate: Optional[bytes] = _as_bytes(data.get("HostCertificate"))
        self.host_private_key: Optional[bytes] = _as_bytes(data.get("HostPrivateKey"))
        self.device_certificate: Optional[bytes] = _as_bytes(data.get("DeviceCertificate"))

    @property
    def usable(self) -> bool:
        return bool(self.host_id and self.host_certificate and self.host_private_key)

    def __repr__(self) -> str:
        return "PairRecord(udid=%r, host_id=%r, usable=%s)" % (
            self.udid, self.host_id, self.usable)


def _pair_record_files(udid: str) -> Iterator[str]:
    """本机可能存放配对记录的目录（Windows 的 Apple Mobile Device Service 为主）"""
    serials = [udid, udid.replace("-", "")]
    directories: List[str] = []
    if sys.platform in ("win32", "cygwin"):
        for env_name in ("ProgramData", "ALLUSERSPROFILE"):
            base = os.environ.get(env_name)
            if base:
                directories.append(os.path.join(base, "Apple", "Lockdown"))
        drive = os.environ.get("SystemDrive", "C:")
        directories.append(os.path.join(drive, "ProgramData", "Apple", "Lockdown"))
    elif sys.platform == "darwin":
        directories += ["/var/db/lockdown", "/Library/Lockdown"]
    else:
        directories += ["/var/lib/lockdown"]

    for directory in directories:
        for serial in serials:
            for name in (serial + ".plist", serial.upper() + ".plist",
                         serial.lower() + ".plist"):
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    yield path


def load_pair_record(udid: str,
                     usbmux_address: Optional[str] = None) -> PairRecord:
    """按 usbmux -> 本地文件 的顺序找配对记录

    Raises:
        DeviceNotPairedError: 两个渠道都没有，需要先在设备上点「信任此电脑」
    """
    # 1) 先问 usbmuxd（Windows 上是 Apple Mobile Device Service）
    try:
        mux = create_mux(usbmux_address)
        try:
            getter = getattr(mux, "get_pair_record", None)
            if getter is not None:
                record = PairRecord(getter(udid))
                if record.usable:
                    return record
        finally:
            mux.close()
    except NotPairedError:
        pass
    except Exception:  # noqa: BLE001 - usbmux 侧任何异常都退回到读文件
        pass

    # 2) 退回到本机 Lockdown 目录
    for path in _pair_record_files(udid):
        try:
            with open(path, "rb") as fh:
                data = plistlib.load(fh)
        except Exception:  # noqa: BLE001
            continue
        record = PairRecord(data)
        if record.usable:
            return record

    raise DeviceNotPairedError(
        "没有找到 %s 的配对记录：请在设备上点『信任此电脑』，或用 iTunes/Apple 设备 App 完成一次配对"
        % udid)


class PlistSocket(object):
    """4 字节大端长度前缀 + plist 的最小实现"""

    def __init__(self, sock: socket.socket, timeout: float = 15.0):
        self.sock = sock
        try:
            self.sock.settimeout(timeout)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._closed = False

    def send(self, payload: Dict[str, Any]) -> None:
        data = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
        self.sock.sendall(struct.pack(">I", len(data)) + data)

    def recv(self) -> Dict[str, Any]:
        header = self._recv_exact(4)
        if len(header) != 4:
            raise LockdownError("lockdown 连接被对端关闭")
        (length,) = struct.unpack(">I", header)
        if length <= 0 or length > MAX_PLIST_PAYLOAD:
            raise LockdownError("lockdown 返回了非法的报文长度: %d" % length)
        body = self._recv_exact(length)
        try:
            return plistlib.loads(body)
        except Exception as err:  # noqa: BLE001
            raise LockdownError("lockdown 返回的不是合法 plist: %s" % err)

    def send_recv(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.send(payload)
        return self.recv()

    def _recv_exact(self, size: int) -> bytes:
        buffer = b""
        while len(buffer) < size:
            chunk = self.sock.recv(size - len(buffer))
            if not chunk:
                break
            buffer += chunk
        return buffer

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "PlistSocket":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class LockdownClient(object):
    """与一台 USB 设备的 lockdownd 对话"""

    def __init__(self,
                 udid: Optional[str] = None,
                 usbmux_address: Optional[str] = None,
                 timeout: float = 15.0,
                 pair_record: Optional[PairRecord] = None):
        device = select_device(udid, connection_type="USB",
                               usbmux_address=usbmux_address)
        if device is None:
            raise DeviceNotFoundError(
                "usbmux 上没有找到 USB 设备%s，请确认数据线已连接且已信任此电脑"
                % ("" if udid is None else " (udid=%s)" % udid))
        self.device: MuxDevice = device
        self.udid: str = device.serial
        self.usbmux_address = usbmux_address
        self.timeout = timeout
        self.session_id: Optional[str] = None

        self._pair_record = pair_record
        self._buid: Optional[str] = None
        self._session_started = False
        self._temp_files: List[str] = []
        self._plist: Optional[PlistSocket] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def connect(self) -> PlistSocket:
        """建立到 lockdownd 的中继连接（不开启 session）"""
        if self._plist is not None:
            return self._plist
        sock = self.device.connect(LOCKDOWN_PORT)
        self._plist = PlistSocket(sock, self.timeout)
        return self._plist

    @property
    def plist(self) -> PlistSocket:
        return self.connect()

    def close(self) -> None:
        if self._session_started and self._plist is not None:
            try:
                self._plist.send({"Label": CLIENT_LABEL,
                                  "Request": "StopSession",
                                  "SessionID": self.session_id})
            except Exception:  # noqa: BLE001
                pass
        if self._plist is not None:
            self._plist.close()
            self._plist = None
        for path in self._temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._temp_files = []

    def __enter__(self) -> "LockdownClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 配对记录 / BUID
    # ------------------------------------------------------------------ #
    @property
    def pair_record(self) -> PairRecord:
        if self._pair_record is None:
            self._pair_record = load_pair_record(self.udid, self.usbmux_address)
        return self._pair_record

    @property
    def system_buid(self) -> Optional[str]:
        if self._buid is not None:
            return self._buid
        if self.pair_record.system_buid:
            self._buid = self.pair_record.system_buid
            return self._buid
        try:
            mux = create_mux(self.usbmux_address)
            try:
                self._buid = mux.get_buid()
            finally:
                mux.close()
        except Exception:  # noqa: BLE001
            self._buid = None
        return self._buid

    # ------------------------------------------------------------------ #
    # lockdown 请求
    # ------------------------------------------------------------------ #
    def query_type(self) -> Dict[str, Any]:
        """不需要 session 的握手探测，用来判断 lockdown 是否活着"""
        return self.plist.send_recv({"Label": CLIENT_LABEL, "Request": "QueryType"})

    def start_session(self) -> Dict[str, Any]:
        if self._session_started:
            return {"SessionID": self.session_id}
        pair = self.pair_record
        response = self.plist.send_recv({
            "Label": CLIENT_LABEL,
            "ProtocolVersion": "2",
            "Request": "StartSession",
            "HostID": pair.host_id,
            "SystemBUID": self.system_buid or "",
        })
        if response.get("Error"):
            raise LockdownError("StartSession 失败: %s" % response["Error"])
        self.session_id = response.get("SessionID")
        self._session_started = True
        if response.get("EnableSessionSSL"):
            self._enable_ssl(pair)
        return response

    def get_value(self, key: Optional[str] = None,
                  domain: Optional[str] = None) -> Any:
        """读取 lockdown 的键值（如 ``ActivationState``）"""
        self.start_session()
        request = {"Label": CLIENT_LABEL, "Request": "GetValue"}
        if domain:
            request["Domain"] = domain
        if key:
            request["Key"] = key
        response = self.plist.send_recv(request)
        if response.get("Error"):
            raise LockdownError("GetValue(%s) 失败: %s" % (key, response["Error"]))
        return response.get("Value")

    def start_service(self, name: str) -> Dict[str, Any]:
        """请求设备启动指定服务，返回含 ``Port`` / ``EnableServiceSSL`` 的响应"""
        self.start_session()
        response = self.plist.send_recv({
            "Label": CLIENT_LABEL,
            "Request": "StartService",
            "Service": name,
        })
        if response.get("Error"):
            raise ServiceStartError("启动服务 %s 失败: %s" % (name, response["Error"]))
        return response

    def open_service(self, name: str) -> PlistSocket:
        """启动服务并新建一条中继连接连上去（按需开启 SSL）

        注意：每次调用都会新开一条 usbmux 连接，go-ios 的 mobileactivation 也是
        每个步骤各开一条，这里保持一致。
        """
        response = self.start_service(name)
        port = int(response.get("Port", 0))
        if port <= 0:
            raise ServiceStartError("服务 %s 返回了非法端口 %d" % (name, port))
        sock = self.device.connect(port)
        if response.get("EnableServiceSSL"):
            sock = self._ssl_context(self.pair_record).wrap_socket(sock)
        return PlistSocket(sock, self.timeout)

    # ------------------------------------------------------------------ #
    # TLS
    # ------------------------------------------------------------------ #
    def _ssl_context(self, pair: PairRecord) -> ssl.SSLContext:
        if not (pair.host_certificate and pair.host_private_key):
            raise DeviceNotPairedError(
                "配对记录里缺少 HostCertificate/HostPrivateKey，无法开启 lockdown SSL 会话")
        cert_path = self._write_temp(pair.host_certificate, suffix=".pem")
        key_path = self._write_temp(pair.host_private_key, suffix=".key")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # 设备侧是自签证书，不做校验，只把本机证书交给它做客户端认证
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            # 部分平台默认安全级别会拒绝 Apple 的老证书套件
            context.set_ciphers("ALL:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        return context

    def _enable_ssl(self, pair: PairRecord) -> None:
        raw = self._plist.sock if self._plist is not None else None
        if raw is None:
            return
        wrapped = self._ssl_context(pair).wrap_socket(raw)
        self._plist = PlistSocket(wrapped, self.timeout)

    def _write_temp(self, data: bytes, suffix: str = ".pem") -> str:
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        self._temp_files.append(path)
        return path
