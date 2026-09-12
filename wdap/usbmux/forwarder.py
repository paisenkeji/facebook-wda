#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 iOS 设备的 TCP 端口通过 usbmux 隧道映射到本机回环地址。

为什么需要它（``http+usbmux`` 直连模式的结构性缺陷）:

1. **每请求建隧道**。``http_create('http+usbmux://...')`` 每条 HTTP 请求都要
   ``select_device()``（内部 ``create_mux()`` 开 2 条 usbmuxd 连接）+ ``MuxDevice.connect()``
   （再开 2 条）才能拿到一条到设备端口的隧道。而 WDA 客户端在一秒内常常连发多个请求
   （``is_ready`` 重试、``wait_ready`` 每秒轮询、``session_id`` 探测）。usbmuxd
   （Windows 上是 Apple Mobile Device Service）的连接数被推高到上限后，新隧道会被
   RST，表现为 ``WinError 10054 远程主机强迫关闭了一个现有的连接``。
2. **无连接复用**。每条请求都是新连接，HTTP keep-alive 完全用不上。
3. **无超时**。``USBMuxHTTPConnection`` 不理会 ``HTTPConnection.timeout``，
   ``fetch()`` 里 ``conn.timeout = timeout`` 又是在 socket 建好之后才赋值，等于没设。
4. **socket 生命周期怪**。返回的是 usbmux 会话里的裸 socket，会话对象随即被回收。

本模块把隧道"下沉"成一条本地 TCP 监听：

    WDA 客户端 --(标准 TCP)--> 127.0.0.1:<随机端口> --(usbmux 隧道)--> 设备:8100

好处：

* HTTP 侧是标准 socket，``HTTPConnection`` 原生的 keep-alive / timeout / ``makefile``
  全部有效，连接池可以正常复用 → **隧道数与请求数解耦**；
* 隧道按需建立、用完即关，不会给 usbmuxd 堆积连接；
* 同一个本地端口可以被任意工具/进程复用（不只是本库）。

纯标准库实现，不依赖 go-ios / tidevice 等外部工具。
"""

import atexit
import selectors
import socket
import socketserver
import threading
import time

from wdap.usbmux.exceptions import MuxError

__all__ = ["UsbmuxPortForwarder"]

_BUFSIZE = 65536
_DEFAULT_CONNECT_TRIES = 3
_DEFAULT_CONNECT_DELAY = 0.3

#: 本进程内已启动的转发器。解释器退出时统一收尾，避免留下监听端口。
_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


def _shutdown_active():
    for fwd in list(_ACTIVE):
        try:
            fwd.stop()
        except Exception:  # noqa: BLE001
            pass


def _register_atexit():
    global _ATEXIT_REGISTERED
    with _ACTIVE_LOCK:
        if not _ATEXIT_REGISTERED:
            atexit.register(_shutdown_active)
            _ATEXIT_REGISTERED = True


def _close_quietly(sock):
    try:
        sock.close()
    except Exception:  # noqa: BLE001
        pass


def _set_nodelay(sock):
    """小请求/小响应场景下关掉 Nagle，避免 40ms 级别的延迟。"""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


def _pipe(local: socket.socket, remote: socket.socket, bufsize: int = _BUFSIZE) -> None:
    """在两条 socket 之间双向搬运数据，直到任一端关闭。

    不设空闲超时：HTTP keep-alive 的连接在两次请求之间本来就是空闲的，按空闲断开
    会破坏连接复用。连接的结束交给对端关闭 / ``stop()`` 来触发。
    """
    sel = selectors.DefaultSelector()
    try:
        _set_nodelay(local)
        _set_nodelay(remote)
        sel.register(local, selectors.EVENT_READ, remote)
        sel.register(remote, selectors.EVENT_READ, local)
        while True:
            events = sel.select()
            if not events:
                break
            for key, _ in events:
                src = key.fileobj
                dst = key.data
                try:
                    data = src.recv(bufsize)
                except OSError:
                    data = b""
                if not data:
                    # 一端结束：把"不再有数据"传递给另一端（半关闭），让对端把剩余
                    # 响应读完再自然收尾，而不是粗暴 RST。
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    try:
                        sel.unregister(src)
                    except (KeyError, ValueError):
                        pass
                    if not sel.get_map():
                        return
                    continue
                try:
                    dst.sendall(data)
                except OSError:
                    return
    finally:
        try:
            sel.close()
        except Exception:  # noqa: BLE001
            pass


class _ForwarderServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, server_address, handler_cls, forwarder):
        self.forwarder = forwarder
        super().__init__(server_address, handler_cls)

    def handle_error(self, request, client_address):
        # 单条连接出错不应该把 traceback 刷到用户控制台，交给 forwarder 记录
        fwd = getattr(self, "forwarder", None)
        if fwd is not None:
            fwd.log("local connection %s failed" % (client_address,))


class _ForwarderHandler(socketserver.BaseRequestHandler):
    def handle(self):
        forwarder = self.server.forwarder
        try:
            upstream = forwarder.open_tunnel()
        except Exception as err:  # noqa: BLE001
            # 拿不到隧道：留下原因后直接关掉本地连接，客户端会看到连接被关闭，
            # 而不是一个更难解释的错误。
            forwarder.record_tunnel_error(err)
            forwarder.log("open tunnel failed: %r" % (err,))
            return
        try:
            _pipe(self.request, upstream)
        finally:
            _close_quietly(upstream)


class UsbmuxPortForwarder(object):
    """``设备:remote_port`` ←→ ``bind_host:local_port`` 的 usbmux 本地转发。

    Example::

        with UsbmuxPortForwarder(udid, 8100) as fwd:
            c = wdap.Client(fwd.url)
            print(c.status())
    """

    def __init__(self,
                 udid: str = "",
                 remote_port: int = 8100,
                 bind_host: str = "127.0.0.1",
                 bind_port: int = 0,
                 connect_tries: int = _DEFAULT_CONNECT_TRIES,
                 connect_delay: float = _DEFAULT_CONNECT_DELAY,
                 logger=None):
        """
        Args:
            udid: 设备 UDID；留空表示自动选择（优先 USB）
            remote_port: 设备上要转发的端口（WDA 默认 8100）
            bind_host: 本机监听地址，默认只听回环
            bind_port: 本机监听端口，0 表示由系统分配
            connect_tries: 单次开隧道失败后的重试次数
            connect_delay: 重试间隔（秒），按倍数递增
            logger: 可选，``callable(str)``，用于记录转发器自身的日志
        """
        self.udid = udid or ""
        self.remote_port = int(remote_port)
        self.bind_host = bind_host
        self.bind_port = int(bind_port)
        self.connect_tries = max(1, int(connect_tries))
        self.connect_delay = max(0.0, float(connect_delay))
        self._logger = logger

        self._server = None
        self._thread = None
        self._lock = threading.Lock()

        # 统计/诊断
        self.tunnel_count = 0          # 累计成功建立的隧道数
        self.last_tunnel_error = None  # 最近一次开隧道失败的原因

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> int:
        """启动本地监听，返回本机端口。重复调用是幂等的。"""
        with self._lock:
            if self._server is not None:
                return self._server.server_address[1]
            server = _ForwarderServer((self.bind_host, self.bind_port),
                                      _ForwarderHandler, self)
            thread = threading.Thread(target=server.serve_forever,
                                      name="wdap-usbmux-forward", daemon=True)
            self._server = server
            self._thread = thread
        thread.start()
        _register_atexit()
        with _ACTIVE_LOCK:
            _ACTIVE.add(self)
        self.log("forwarding 127.0.0.1:%d -> %s:%d"
                 % (server.server_address[1], self.udid or "<auto>", self.remote_port))
        return server.server_address[1]

    def stop(self) -> None:
        """停止转发（幂等）。会等待工作线程退出。"""
        with self._lock:
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with _ACTIVE_LOCK:
            _ACTIVE.discard(self)

    @property
    def local_port(self):
        server = self._server
        return server.server_address[1] if server is not None else None

    @property
    def url(self) -> str:
        port = self.local_port
        if port is None:
            raise RuntimeError("forwarder 尚未 start()，无法取 url")
        return "http://%s:%d" % (self.bind_host, port)

    @property
    def is_running(self) -> bool:
        return self._server is not None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()

    def __del__(self):
        try:
            self.stop()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # 隧道
    # ------------------------------------------------------------------ #
    def open_tunnel(self) -> socket.socket:
        """新开一条到 ``设备:remote_port`` 的 usbmux 隧道（带重试）。"""
        last = None
        for attempt in range(self.connect_tries):
            device = self._resolve_device()
            if device is None:
                last = MuxError("usbmux 上没有找到设备 %s（是否已连接并点了『信任』？）"
                                % (self.udid or "<auto>",))
            else:
                try:
                    sock = device.connect(self.remote_port)
                    self.tunnel_count += 1
                    self.last_tunnel_error = None
                    return sock
                except Exception as err:  # noqa: BLE001
                    last = err
            if attempt + 1 < self.connect_tries and self.connect_delay > 0:
                time.sleep(self.connect_delay * (attempt + 1))
        raise last if last is not None else MuxError("open tunnel failed")

    def record_tunnel_error(self, err) -> None:
        self.last_tunnel_error = err

    def _resolve_device(self):
        # 延迟 import：避免 wdap.usbmux.__init__ -> forwarder 的循环导入
        from wdap.usbmux import _select_device
        if self.udid:
            return _select_device(self.udid)
        from wdap.usbmux.pyusbmux import select_device
        return select_device(None)

    def log(self, message: str) -> None:
        if self._logger is not None:
            try:
                self._logger(message)
            except Exception:  # noqa: BLE001
                pass
