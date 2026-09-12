#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按 iOS 版本选择后端拉起 WDA

这是**拉起/启动 WDA**（跑 XCTest bundle），与 :mod:`wdap.activation`
的「设备激活（ActivationState）」是两件不同的事：

===================  ========================================================
iOS 版本              后端
===================  ========================================================
iOS 16 及以下         ``tidevice xctest``（tidevice / tins2）
iOS 17 及以上         ``ios runwda``（go-ios）
版本未知              先 go-ios，失败再回退 tidevice
===================  ========================================================

为什么必须分流：iOS 17 起 testmanagerd 换成了 RemoteXPC（RSD 隧道），
tidevice 0.12.x 仍走老的 lockdown/DeveloperDiskImage 路径，会在挂载开发者
镜像阶段直接失败（``DeveloperImage not found``），故 iOS 17+ 必须换 go-ios。

两个后端都是**外部可执行程序**，通过 ``subprocess`` 拉起，本模块不嵌入
go-ios / tidevice 的任何代码，可以安全打包进 ipa。

命令契约（已对照源码核对）：

* tidevice ``xcuitest``（``xctest`` 是它的 alias）::

      <tool> -u <udid> xctest [-B <bundle-id>] [-e KEY:VALUE] [--test-runner-args a,b]

  注意 ``-e`` 的分隔符是**冒号** ``key:value``。

* go-ios ``runwda``::

      ios runwda --udid=<udid> [--bundleid=<b> --testrunnerbundleid=<b>
                 --xctestconfig=<c>] [--arg=<a>] [--env=K=V]

  ``--env`` 是**等号** ``K=V``；``--bundleid`` / ``--testrunnerbundleid`` /
  ``--xctestconfig`` 三者必须**全给或全不给**（go-ios 源码里的硬校验），
  只给一部分它会直接拒绝执行。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

logger = logging.getLogger("wdap.wda_launch")

#: iOS 16 及以下走 tidevice；大于等于这个 major 就走 go-ios
IOS_TIDEVICE_MAX = 16

BACKEND_AUTO = "auto"
BACKEND_TIDEVICE = "tidevice"
BACKEND_GOIOS = "goios"
ALL_STRATEGIES = (BACKEND_AUTO, BACKEND_TIDEVICE, BACKEND_GOIOS)

#: go-ios runwda 三个参数都不传时的默认值（取自 go-ios 源码）
GOIOS_DEFAULT_BUNDLE_ID = "com.facebook.WebDriverAgentRunner.xctrunner"
GOIOS_DEFAULT_TESTRUNNER_BUNDLE_ID = "com.facebook.WebDriverAgentRunner.xctrunner"
GOIOS_DEFAULT_XCTEST_CONFIG = "WebDriverAgentRunner.xctest"

#: tidevice xctest 的 -B 默认值（不传时由 tidevice 自己通配）
TIDEVICE_DEFAULT_BUNDLE_ID = "com.*.xctrunner"

#: WDA 监听端口通过环境变量传给 XCTest runner
PORT_ENV_KEY = "USE_PORT"

#: go-ios tunnel daemon 的 HTTP 管理接口（见 go-ios 的 ios/tunnel/tunnel_api.go）
#: ``/health`` 判断 agent 在不在，``/ready`` 判断隧道是否已建好
TUNNEL_DEFAULT_HOST = "127.0.0.1"
TUNNEL_DEFAULT_PORT = 28100
TUNNEL_MODE_AUTO = "auto"
TUNNEL_MODE_KERNEL = "kernel"
TUNNEL_MODE_USERSPACE = "userspace"
ALL_TUNNEL_MODES = (TUNNEL_MODE_AUTO, TUNNEL_MODE_KERNEL, TUNNEL_MODE_USERSPACE)


class WdaLaunchResult(NamedTuple):
    """一次拉起尝试的结果"""

    #: 子进程是否成功拉起且未在 startup_wait 内退出
    ok: bool
    #: 实际使用的后端：``tidevice`` / ``goios`` / ``""``（没找到工具）
    backend: str
    #: 探测到的 iOS 版本（如 ``"18.7.1"``），读不到时为 None
    ios_version: Optional[str]
    #: 实际执行的命令行
    command: List[str]
    #: 子进程 stdout/stderr 的落盘位置
    log_path: str
    #: 子进程 pid，拉起失败时为 None
    pid: Optional[int]
    detail: str
    #: go-ios 隧道是否就绪；None 表示本次没有走隧道（tidevice 后端或 start_tunnel=False）
    tunnel_ok: Optional[bool] = None

    def __bool__(self) -> bool:
        return self.ok


# --------------------------------------------------------------------------- #
# 版本探测
# --------------------------------------------------------------------------- #
def parse_ios_version(value: Any) -> Optional[Tuple[int, int, int]]:
    """把 ``"18.7.1"`` / ``"26.0"`` 解析成 ``(major, minor, patch)``

    解析不出来返回 None（iOS 26 之后 Apple 改用年份式主版本号，这里只取
    第一段做分流判断，因此不受影响）。
    """
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    parts: List[int] = []
    for chunk in value.strip().split(".")[:3]:
        head = ""
        for char in chunk:
            if not char.isdigit():
                break
            head += char
        if not head:
            break
        parts.append(int(head))
    if not parts:
        return None
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def read_ios_version(udid: Optional[str] = None,
                     usbmux_address: Optional[str] = None,
                     timeout: float = 15.0) -> Optional[str]:
    """通过 lockdown 读 ``ProductVersion``，失败返回 None

    读版本需要已配对（要开 SSL session）。未信任的设备、usbmux 不通都会
    返回 None —— 调用方要能接受"版本未知"并回退到依次尝试两个后端。
    """
    try:
        from wdap.lockdown import LockdownClient

        with LockdownClient(udid, usbmux_address=usbmux_address,
                            timeout=timeout) as client:
            value = client.get_value("ProductVersion")
    except Exception as err:  # noqa: BLE001 - 版本探测失败不该中断拉起流程
        logger.debug("读取 ProductVersion 失败: %s", err)
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return value if isinstance(value, str) else None


# --------------------------------------------------------------------------- #
# 后端选择
# --------------------------------------------------------------------------- #
def pick_backend(ios_version: Optional[str],
                 strategy: str = BACKEND_AUTO,
                 fallback: bool = True) -> List[str]:
    """返回按优先级排序的后端列表

    Args:
        ios_version: ``ProductVersion`` 原文，None 表示未知
        strategy: ``auto`` / ``tidevice`` / ``goios``
        fallback: 首选后端不可用时，是否把另一个后端作为备选
    """
    if strategy not in ALL_STRATEGIES:
        raise ValueError("strategy 只能是 %s，收到 %r" % ("/".join(ALL_STRATEGIES),
                                                      strategy))
    if strategy != BACKEND_AUTO:
        return [strategy]

    parsed = parse_ios_version(ios_version)
    if parsed is None:
        # 版本未知：优先 go-ios（覆盖面更广），失败再退 tidevice
        primary, secondary = BACKEND_GOIOS, BACKEND_TIDEVICE
    elif parsed[0] <= IOS_TIDEVICE_MAX:
        primary, secondary = BACKEND_TIDEVICE, BACKEND_GOIOS
    else:
        # iOS 17+ **不回退 tidevice**：它在这上面拉不起 WDA（testmanagerd 已换成
        # RemoteXPC），子进程倒是活着，于是 ok=True 但 WDA 永不监听端口 ——
        # 这个假阳性比直接报错更难排查。要强制用 tidevice 请显式传 strategy。
        if fallback:
            logger.info("iOS %s 只支持 go-ios，跳过 tidevice 回退", ios_version)
        return [BACKEND_GOIOS]
    return [primary, secondary] if fallback else [primary]


def find_tool(backend: str,
              tidevice_path: Optional[str] = None,
              goios_path: Optional[str] = None) -> Optional[str]:
    """定位后端可执行文件，找不到返回 None

    tidevice 优先用显式路径，其次 ``tins2``（tidevice 的 Rust 实现）、
    ``tidevice``；go-ios 优先显式路径，其次 ``ios``、``go-ios``。
    """
    if backend == BACKEND_TIDEVICE:
        if tidevice_path:
            return tidevice_path
        return shutil.which("tins2") or shutil.which("tidevice")
    if backend == BACKEND_GOIOS:
        if goios_path:
            return goios_path
        return shutil.which("ios") or shutil.which("go-ios")
    raise ValueError("未知后端: %r" % (backend,))


# --------------------------------------------------------------------------- #
# 命令行构造
# --------------------------------------------------------------------------- #
def build_command(backend: str,
                  tool: str,
                  udid: Optional[str] = None,
                  wda_bundle_id: Optional[str] = None,
                  wda_port: Optional[int] = None,
                  extra_env: Optional[Dict[str, str]] = None,
                  extra_args: Optional[Sequence[str]] = None) -> List[str]:
    """构造拉起命令行（两个后端的参数风格完全不同，分开处理）"""
    env = dict(extra_env or {})
    if wda_port:
        env.setdefault(PORT_ENV_KEY, str(int(wda_port)))

    if backend == BACKEND_TIDEVICE:
        args = [tool]
        if udid:
            args += ["-u", udid]
        args.append("xctest")
        if wda_bundle_id:
            args += ["-B", wda_bundle_id]
        # tidevice 的 --env 分隔符是冒号
        for key, value in env.items():
            args += ["-e", "%s:%s" % (key, value)]
        if extra_args:
            args += ["--test-runner-args", ",".join(extra_args)]
        return args

    if backend == BACKEND_GOIOS:
        args = [tool, "runwda"]
        if udid:
            args.append("--udid=%s" % udid)
        if wda_bundle_id:
            # go-ios 要求这三个参数要么全给要么全不给
            args += [
                "--bundleid=%s" % wda_bundle_id,
                "--testrunnerbundleid=%s" % wda_bundle_id,
                "--xctestconfig=%s" % GOIOS_DEFAULT_XCTEST_CONFIG,
            ]
        for key, value in env.items():
            args.append("--env=%s=%s" % (key, value))
        for item in extra_args or []:
            args.append("--arg=%s" % item)
        return args

    raise ValueError("未知后端: %r" % (backend,))


def _tail_file(path: str, max_bytes: int = 2048) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _default_log_path(backend: str) -> str:
    name = "wdap_goios_runwda.log" if backend == BACKEND_GOIOS \
        else "wdap_tidevice_xctest.log"
    return os.path.join(tempfile.gettempdir(), name)


# --------------------------------------------------------------------------- #
# 拉起
# --------------------------------------------------------------------------- #
def start_wda(udid: Optional[str] = None,
              wda_bundle_id: Optional[str] = None,
              strategy: str = BACKEND_AUTO,
              wda_port: Optional[int] = None,
              fallback: bool = True,
              tidevice_path: Optional[str] = None,
              goios_path: Optional[str] = None,
              usbmux_address: Optional[str] = None,
              startup_wait: float = 3.0,
              log_path: Optional[str] = None,
              mount_image: bool = False,
              start_tunnel: bool = True,
              tunnel_mode: str = TUNNEL_MODE_AUTO,
              tunnel_host: str = TUNNEL_DEFAULT_HOST,
              tunnel_port: int = TUNNEL_DEFAULT_PORT,
              extra_env: Optional[Dict[str, str]] = None,
              extra_args: Optional[Sequence[str]] = None) -> WdaLaunchResult:
    """按 iOS 版本选择后端拉起 WDA

    Args:
        udid: 设备 UDID；None 时交给后端自己选（只有一台设备时可用）
        wda_bundle_id: WDA 的 bundle id，留空用各后端的默认值
        strategy: ``auto`` / ``tidevice`` / ``goios``
        wda_port: 传给 WDA 的 ``USE_PORT``；None 表示不传（用 WDA 默认 8100）
        fallback: 首选后端找不到 / 拉起失败时是否换另一个后端
        tidevice_path / goios_path: 显式指定可执行文件，省去 PATH 查找
        usbmux_address: 自定义 usbmuxd 地址，仅版本探测用到
        startup_wait: 拉起后等待几秒再判断子进程是否已退出
        log_path: 子进程输出落盘位置，默认放系统临时目录
        extra_args: 传给 test runner 的额外命令行参数
        mount_image: 拉起前先跑 ``ios image auto``（仅 go-ios 后端需要，
            tidevice 会在 xctest 内部自己挂载）
        start_tunnel: 拉起前先确保 go-ios tunnel daemon 在跑（仅 go-ios 后端）。
            **iOS 17+ 的硬前置条件** —— ``runwda`` 不会自己起隧道，
            没有隧道会在连接 testmanagerd 时失败。已由外部管理时设 False。
        tunnel_mode: ``auto``（默认，无管理员权限时直接走 userspace）/
            ``kernel``（需管理员/sudo）/ ``userspace``
            （Windows 需 wintun.dll 在 ``C:/Windows/system32``）
        tunnel_host / tunnel_port: tunnel agent 的 HTTP 管理接口地址

    Returns:
        WdaLaunchResult —— 注意 ``ok=True`` 只代表**子进程活着**，
        WDA 是否真的监听了端口要另外探测（``Client.is_ready()``）。
    """
    ios_version = read_ios_version(udid, usbmux_address=usbmux_address)
    if ios_version:
        logger.debug("设备 %s iOS 版本 %s", udid, ios_version)
    else:
        logger.debug("设备 %s 的 iOS 版本未知，按回退顺序尝试", udid)

    backends = pick_backend(ios_version, strategy=strategy, fallback=fallback)
    last: Optional[WdaLaunchResult] = None

    for backend in backends:
        tool = find_tool(backend, tidevice_path, goios_path)
        if not tool:
            last = WdaLaunchResult(
                False, backend, ios_version, [], "", None,
                "PATH 里找不到 %s，跳过"
                % ("tidevice/tins2" if backend == BACKEND_TIDEVICE
                   else "go-ios(ios)"))
            logger.info("%s", last.detail)
            continue

        if backend == BACKEND_TIDEVICE:
            parsed = parse_ios_version(ios_version)
            if parsed and parsed[0] > IOS_TIDEVICE_MAX:
                logger.warning(
                    "iOS %s 上 tidevice 拉不起 WDA（testmanagerd 已换成 RemoteXPC）。"
                    "即使子进程活着，WDA 也不会监听端口 —— 别把 ok=True 当成功。",
                    ios_version)

        tunnel_ok: Optional[bool] = None
        if backend == BACKEND_GOIOS:
            # iOS 17+ 必须先挂开发者镜像、并确保 tunnel daemon 在跑，
            # 否则 runwda 起来后连不上 testmanagerd（RemoteXPC）会立刻退出，
            # 典型报错就是 "lost connection to testmanagerd"。
            if mount_image:
                mount_developer_image(udid, goios_path=goios_path)
            if start_tunnel:
                tunnel_ok = ensure_tunnel(goios_path=goios_path,
                                          mode=tunnel_mode,
                                          host=tunnel_host, port=tunnel_port)
                if not tunnel_ok:
                    logger.warning(
                        "go-ios tunnel 不可用，本次 runwda 大概率会以 "
                        "『lost connection to testmanagerd』失败")

        command = build_command(backend, tool, udid, wda_bundle_id, wda_port,
                                extra_env, extra_args)
        path = log_path or _default_log_path(backend)
        result = _popen(backend, command, path, ios_version, startup_wait,
                        tunnel_ok)
        if result.ok:
            return result
        last = result
        if not fallback:
            return result

    assert last is not None
    return last


def _popen(backend: str,
           command: List[str],
           log_path: str,
           ios_version: Optional[str],
           startup_wait: float,
           tunnel_ok: Optional[bool] = None) -> WdaLaunchResult:
    """以子进程方式拉起后端，短等后判断它是否已经挂掉"""
    logger.info("WDA is not running, exec[%s]: %s", backend, " ".join(command))
    try:
        child_out = open(log_path, "wb")
    except OSError:
        child_out = subprocess.DEVNULL  # type: ignore[assignment]
        log_path = ""

    try:
        proc = subprocess.Popen(command, stdout=child_out,
                                stderr=subprocess.STDOUT)
    except OSError as err:
        detail = "启动 %s 失败: %s" % (backend, err)
        logger.warning("%s", detail)
        return WdaLaunchResult(False, backend, ios_version, command, log_path,
                               None, detail)
    finally:
        if child_out is not subprocess.DEVNULL:
            try:
                child_out.close()
            except Exception:  # noqa: BLE001
                pass

    time.sleep(startup_wait)
    if proc.poll() is not None:
        detail = "%s 启动后立即退出 (exit=%s)" % (backend, proc.returncode)
        if log_path:
            detail += "，日志: %s\n%s" % (log_path, _tail_file(log_path))
        logger.warning("%s", detail)
        logger.warning("goios 启动后立即退出，日志: %s", log_path)
        if backend == BACKEND_GOIOS:
            detail += ("\n提示：iOS 17+ 用 go-ios 跑 WDA 需要两个前置条件 —— "
                       "挂载开发者镜像（ios image auto --udid=<udid>）、"
                       "启动隧道 daemon（ios tunnel start，kernel 模式要管理员权限）。"
                       "隧道没起来时典型报错是 "
                       "『lost connection to testmanagerd』。")
        return WdaLaunchResult(False, backend, ios_version, command, log_path,
                               proc.pid, detail, tunnel_ok)

    return WdaLaunchResult(True, backend, ios_version, command, log_path,
                           proc.pid,
                           "%s 已拉起 (pid=%s)" % (backend, proc.pid), tunnel_ok)


def mount_developer_image(udid: Optional[str] = None,
                          goios_path: Optional[str] = None,
                          timeout: float = 120.0) -> bool:
    """iOS 17+ 拉起 WDA 前挂载开发者镜像（``ios image auto``）

    tidevice 会在 ``xctest`` 内部自动挂载，go-ios 需要显式这一步。
    失败不抛异常，返回 False —— 让上层继续尝试拉起。
    """
    tool = find_tool(BACKEND_GOIOS, goios_path=goios_path)
    if not tool:
        logger.warning("找不到 go-ios(ios)，跳过挂载开发者镜像")
        return False
    command = [tool, "image", "auto"]
    if udid:
        command.append("--udid=%s" % udid)
    logger.info("挂载开发者镜像: %s", " ".join(command))
    try:
        proc = subprocess.run(command, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except (OSError, subprocess.SubprocessError) as err:
        logger.warning("ios image auto 失败: %s", err)
        return False
    if proc.returncode != 0:
        logger.warning("ios image auto 退出码 %s: %s", proc.returncode,
                       (proc.stdout or b"").decode("utf-8", "replace")[:500])
        return False
    return True


def tunnel_agent_alive(host: str = TUNNEL_DEFAULT_HOST,
                       port: int = TUNNEL_DEFAULT_PORT,
                       path: str = "/health",
                       timeout: float = 1.0) -> bool:
    """go-ios tunnel daemon 是否在跑（``GET http://host:port/health``）

    这是 iOS 17+ 拉起 WDA 的硬前置条件：go-ios 的 ``runwda`` **不会**自己起隧道，
    必须由外部先跑 ``ios tunnel start`` 拉起一个常驻 daemon（见 go-ios README）。
    """
    import urllib.request

    url = "http://%s:%d%s" % (host, int(port), path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", resp.getcode()) < 400
    except Exception:  # noqa: BLE001 - 探不通就当没在跑
        return False


def wait_tunnel_ready(host: str = TUNNEL_DEFAULT_HOST,
                      port: int = TUNNEL_DEFAULT_PORT,
                      timeout: float = 30.0,
                      interval: float = 0.5) -> bool:
    """轮询 ``/ready`` 等隧道真正建好（agent 起来了 ≠ 隧道可用）"""
    deadline = time.time() + max(0.0, timeout)
    while True:
        if tunnel_agent_alive(host, port, path="/ready"):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def _is_admin() -> bool:
    """当前进程是否具备管理员/root 权限（go-ios 的 kernel 隧道模式需要）"""
    try:
        if os.name == "nt":
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def _start_tunnel_once(tool: str,
                       mode: str,
                       host: str,
                       port: int,
                       ready_timeout: float,
                       log_path: Optional[str] = None) -> bool:
    """拉起一次 tunnel daemon 并等它就绪"""
    command = [tool, "tunnel", "start"]
    if mode == TUNNEL_MODE_USERSPACE:
        command.append("--userspace")
    logger.info("启动 go-ios tunnel (%s 模式): %s", mode, " ".join(command))

    path = log_path or os.path.join(tempfile.gettempdir(),
                                    "wdap_goios_tunnel.log")
    try:
        child_out = open(path, "wb")
    except OSError:
        child_out = subprocess.DEVNULL  # type: ignore[assignment]
        path = ""
    try:
        # daemon 要常驻，不能等它退出 —— Popen 后立刻返回
        proc = subprocess.Popen(command, stdout=child_out,
                                stderr=subprocess.STDOUT)
    except OSError as err:
        logger.warning("启动 go-ios tunnel 失败: %s", err)
        return False
    finally:
        if child_out is not subprocess.DEVNULL:
            try:
                child_out.close()
            except Exception:  # noqa: BLE001
                pass

    # 边等 /ready 边盯子进程：权限不足时 go-ios 会直接 fatal 退出，
    # 傻等到 ready_timeout 只会白等（实测白等满 30 秒才继续）
    deadline = time.time() + max(0.0, ready_timeout)
    while time.time() < deadline:
        if proc.poll() is not None:
            logger.warning(
                "go-ios tunnel (%s) 启动后立即退出 (exit=%s)，日志: %s\n%s",
                mode, proc.returncode, path, _tail_file(path) if path else "")
            return False
        if tunnel_agent_alive(host, port, path="/ready"):
            logger.info("go-ios tunnel 已就绪（%s 模式）", mode)
            return True
        time.sleep(0.5)

    logger.warning("go-ios tunnel (%s) 在 %.0fs 内未就绪", mode, ready_timeout)
    return False


def ensure_tunnel(goios_path: Optional[str] = None,
                  mode: str = TUNNEL_MODE_AUTO,
                  host: str = TUNNEL_DEFAULT_HOST,
                  port: int = TUNNEL_DEFAULT_PORT,
                  start: bool = True,
                  ready_timeout: float = 30.0,
                  log_path: Optional[str] = None) -> bool:
    """确保 go-ios 的 tunnel daemon 在跑，不在就后台拉起

    Args:
        mode: ``auto``（默认）/ ``kernel`` / ``userspace``。
            ``kernel`` = ``ios tunnel start``，**需要管理员/sudo** —— 权限不够时
            go-ios 直接 fatal 退出（实测日志：*this program needs elevated
            privileges. Run as administrator.*）。``auto`` 会先判权限：
            有管理员就先试 kernel、失败再退 userspace；没有直接走 userspace
            （Windows 上需把 ``wintun.dll`` 放到 ``C:/Windows/system32``）。
        start: False 时只检查、不尝试拉起（用于"我自己管理隧道"的场景）
        ready_timeout: 拉起后等 ``/ready`` 的最长秒数

    Returns:
        隧道是否可用。**失败不抛异常** —— 让上层继续尝试拉起 WDA，
        也许用户已经在别的终端手动起过隧道。
    """
    if mode not in ALL_TUNNEL_MODES:
        raise ValueError("mode 只能是 %s，收到 %r" % ("/".join(ALL_TUNNEL_MODES),
                                                  mode))
    if tunnel_agent_alive(host, port):
        if wait_tunnel_ready(host, port, timeout=ready_timeout):
            logger.debug("go-ios tunnel 已就绪 (%s:%d)", host, port)
            return True
        logger.warning("go-ios tunnel agent 在跑但隧道未就绪 (%s:%d)", host, port)
        return False

    if not start:
        logger.warning("go-ios tunnel 未启动且 start=False，iOS 17+ 拉起 WDA 会失败")
        return False

    tool = find_tool(BACKEND_GOIOS, goios_path=goios_path)
    if not tool:
        logger.warning("找不到 go-ios(ios)，无法启动 tunnel")
        return False

    modes: List[str]
    if mode != TUNNEL_MODE_AUTO:
        modes = [mode]
    elif _is_admin():
        modes = [TUNNEL_MODE_KERNEL, TUNNEL_MODE_USERSPACE]
    else:
        logger.info("当前无管理员权限，go-ios tunnel 直接用 userspace 模式")
        modes = [TUNNEL_MODE_USERSPACE]

    for current in modes:
        if _start_tunnel_once(tool, current, host, port, ready_timeout,
                              log_path):
            return True

    tip = ("kernel 模式需要管理员/sudo；非管理员请用 userspace 模式"
           "（Windows 还要把 wintun.dll 放到 C:/Windows/system32）")
    logger.warning("go-ios tunnel 起不来（%s）。%s", "/".join(modes), tip)
    return False


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="wda_start",
        description="按 iOS 版本选择后端拉起 WDA（iOS<=16 tidevice / iOS>=17 go-ios）")
    parser.add_argument("udid", nargs="?", default=None, help="设备 UDID")
    parser.add_argument("--strategy", default=BACKEND_AUTO,
                        choices=list(ALL_STRATEGIES),
                        help="后端选择，默认 auto（按 iOS 版本）")
    parser.add_argument("--no-fallback", action="store_true",
                        help="首选后端失败时不要换另一个后端重试")
    parser.add_argument("--bundle-id", default=None, help="WDA bundle id")
    parser.add_argument("--port", type=int, default=None,
                        help="WDA 监听端口（通过 USE_PORT 传给 runner）")
    parser.add_argument("--tidevice-path", default=None, help="tidevice 可执行文件路径")
    parser.add_argument("--goios-path", default=None, help="go-ios 可执行文件路径")
    parser.add_argument("--mount-image", action="store_true",
                        help="拉起前先执行 ios image auto（仅 go-ios 后端需要）")
    parser.add_argument("--tunnel-mode", default=TUNNEL_MODE_AUTO,
                        choices=list(ALL_TUNNEL_MODES),
                        help="go-ios 隧道模式：kernel(需管理员/sudo) / userspace")
    parser.add_argument("--no-tunnel", action="store_true",
                        help="不要自动拉起 go-ios tunnel（你自己管理时用）")
    parser.add_argument("--tunnel-host", default=TUNNEL_DEFAULT_HOST,
                        help="tunnel agent 主机（默认 127.0.0.1）")
    parser.add_argument("--tunnel-port", type=int, default=TUNNEL_DEFAULT_PORT,
                        help="tunnel agent 端口（默认 28100）")
    parser.add_argument("--wait", type=float, default=3.0,
                        help="拉起后等待几秒再判断子进程是否退出")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    version = read_ios_version(args.udid)
    print("iOS 版本: %s" % (version or "未知"))
    print("后端顺序: %s" % ", ".join(
        pick_backend(version, args.strategy, not args.no_fallback)))

    if args.mount_image:
        print("挂载开发者镜像: %s"
              % ("成功" if mount_developer_image(args.udid, args.goios_path)
                 else "失败"))

    result = start_wda(args.udid,
                       wda_bundle_id=args.bundle_id,
                       strategy=args.strategy,
                       wda_port=args.port,
                       fallback=not args.no_fallback,
                       tidevice_path=args.tidevice_path,
                       goios_path=args.goios_path,
                       startup_wait=args.wait,
                       mount_image=args.mount_image,
                       start_tunnel=not args.no_tunnel,
                       tunnel_mode=args.tunnel_mode,
                       tunnel_host=args.tunnel_host,
                       tunnel_port=args.tunnel_port)
    print("后端    : %s" % result.backend)
    print("命令    : %s" % (" ".join(result.command) or "(未执行)"))
    print("日志    : %s" % (result.log_path or "(无)"))
    print("结果    : %s | %s" % ("成功" if result.ok else "失败", result.detail))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
