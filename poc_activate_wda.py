#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
POC: 自动激活 WDA，目标 iOS 16 - iOS 26。

后端策略（与用户确认）：
  * go-ios 主后端（iOS 17+ 唯一可行；iOS <= 16 同样优先尝试）
  * tidevice 兜底（仅当 iOS < 17 且 go-ios 路径失败时回退）

激活流程（6 步，顺序来自 activate_wda18 发布版 kill_ios.ps1 实测结论，go-ios#391）:
  [1] ios image auto                   挂载(个人)开发者镜像, testmanagerd 依赖它
  [2] kill 60105 残留 tunnel agent     保证 tunnel 全新重建
  [3] ios tunnel start (userspace)     起 iOS 17+ tunnel(需管理员 + wintun.dll)
  [4] 等 60105 LISTENING               隧道就绪
  [5] settle 5s                        等 testmanagerd/RSD 可达
  [6] ios runwda ...                   长驻启动 WDA, 轮询 /status 就绪

用法示例:
  python poc_activate_wda.py                                # 自动选唯一 USB 设备
  python poc_activate_wda.py --udid <UDID>                  # 指定设备
  python poc_activate_wda.py --backend tidevice             # 强制 tidevice(iOS<=16)
  python poc_activate_wda.py --stop                         # 结束本工具拉起的 runwda/tunnel
  python poc_activate_wda.py --check                        # 只探测: 设备/iOS 版本/WDA 是否已 ready

依赖:
  * go-ios Windows 版 (ios.exe)。默认按顺序找:
      $WDA_IOS_EXE > D:\\python_project\\activate_wda18\\dist\\go-ios-win\\ios.exe
      > D:\\python_project\\activate_wda18\\dist\\激活器\\ios.exe > PATH 中 ios/ios.exe
  * wintun.dll (iOS 17+ tunnel 用): C:\\Windows\\System32\\wintun.dll 或 ios.exe 同目录
  * 设备已安装 WDA runner(bundle 见 --bundle-id), 已信任证书, 已开开发者模式
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

DEFAULT_BUNDLE_ID = "com.facebook.WebDriverAgentRunner.xctrunner"
XCTEST_CONFIG = "WebDriverAgentRunner.xctest"
AGENT_PORT = 60105
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "poc_wda_logs")
PID_FILE = os.path.join(LOG_DIR, "poc_activate_wda.pid.json")

_CANDIDATE_IOS_EXE = [
    r"D:\python_project\activate_wda18\dist\go-ios-win\ios.exe",
    r"D:\python_project\activate_wda18\dist\激活器\ios.exe",
]


def log(msg: str) -> None:
    print("[poc] %s" % msg, flush=True)


def err(msg: str) -> None:
    print("[poc][ERROR] %s" % msg, flush=True)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def find_ios_exe() -> str:
    env = os.environ.get("WDA_IOS_EXE") or os.environ.get("GO_IOS_BIN")
    cands = ([env] if env else []) + _CANDIDATE_IOS_EXE
    for c in cands:
        if c and os.path.isfile(c):
            return c
    p = shutil.which("ios") or shutil.which("ios.exe")
    return p or ""


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run(cmd, timeout=None, check=False, capture=True):
    """run 子进程; 返回 (exit_code, stdout_text, stderr_text)"""
    p = subprocess.run(cmd, capture_output=capture, text=True,
                       encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=0x08000000)  # CREATE_NO_WINDOW
    if check and p.returncode != 0:
        raise RuntimeError("cmd failed(%d): %s\n%s%s" % (
            p.returncode, " ".join(cmd), p.stdout or "", p.stderr or ""))
    return p.returncode, p.stdout or "", p.stderr or ""


def json_lines(text: str):
    """go-ios 默认 JSON-lines 输出; 返回其中所有可解析对象"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def listen_pids_on(port: int) -> list:
    """返回本机监听指定端口的 PID 列表"""
    pids = []
    try:
        _, out, _ = run(["netstat", "-ano"])
        for line in out.splitlines():
            m = re.search(r":%d\s+.*?(LISTENING|LISTEN)\s+(\d+)\s*$" % port, line)
            if m:
                pids.append(int(m.group(2)))
    except Exception:
        pass
    return sorted(set(pids))


def kill_pids(pids) -> None:
    for pid in pids:
        try:
            run(["taskkill", "/PID", str(pid), "/F"])
            log("killed PID=%s" % pid)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# go-ios 探测
# --------------------------------------------------------------------------- #
def gios_devices(ios_exe: str) -> list:
    """ios list -> [{udid, ...}]"""
    rc, out, errs = run([ios_exe, "list"])
    for obj in json_lines(out) + json_lines(errs):
        if isinstance(obj, dict) and "deviceList" in obj:
            return obj["deviceList"] or []
    return []


def gios_info(ios_exe: str, udid: str) -> dict:
    """ios info -> plist dict(含 ProductVersion/DeviceName/...), 失败返回 {}"""
    rc, out, errs = run([ios_exe, "info", "--udid", udid], timeout=60)
    # info 可能整块打到 stderr(go-ios INFO 日志走 stderr), 结果 JSON 也可能在其中
    for obj in json_lines(out) + json_lines(errs):
        if isinstance(obj, dict) and ("ProductVersion" in obj or "DeviceName" in obj):
            return obj
    # 兜底: 裸 JSON 对象(无 time/level 字段的行)
    for line in (out + errs).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "ProductVersion" in obj:
            return obj
    return {}


def ios_major(info: dict):
    pv = str(info.get("ProductVersion", "") or "")
    m = re.match(r"(\d+)", pv)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# WDA ready 检查(经 usbmuxd 直连设备 8100, 无需本地端口转发)
# --------------------------------------------------------------------------- #
def wda_ready(udid: str, port: int, timeout: float = 15.0) -> bool:
    sys.path.insert(0, HERE)
    try:
        from wdap import BaseClient
    except Exception as e:  # 依赖缺失时退回裸 HTTP 探测
        return _wda_ready_raw(udid, port, timeout)
    c = BaseClient(url="http+usbmux://%s:%d" % (udid, port))
    return c.is_ready()


def _wda_ready_raw(udid: str, port: int, timeout: float) -> bool:
    try:
        sys.path.insert(0, HERE)
        from wdap.usbmux import http_create  # noqa
    except Exception:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http_create("http+usbmux://%s:%d" % (udid, port))
            conn.request("GET", "/status")
            resp = conn.getresponse()
            ok = resp.status == 200 and b'"value"' in resp.read()
            conn.close()
            return ok
        except Exception:
            time.sleep(1.0)
    return False


# --------------------------------------------------------------------------- #
# 激活后端
# --------------------------------------------------------------------------- #
class GoIOSActivator:
    def __init__(self, ios_exe: str, udid: str, log_path: str):
        self.ios_exe = ios_exe
        self.udid = udid
        self.log_path = log_path
        self.procs = []          # 需要保持存活的 Popen
        self.pids = []           # 已 spawn 的 pid

    def _spawn(self, args, tag: str):
        f = open(self.log_path + ".%s.log" % tag, "ab", buffering=0)
        env = dict(os.environ)
        env["ENABLE_GO_IOS_AGENT"] = "user"
        p = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT,
                             env=env, creationflags=0x08000000)
        self.procs.append(p)
        self.pids.append(p.pid)
        log("%s PID=%s  (日志: %s)" % (tag, p.pid, f.name))
        return p

    def step_image_auto(self) -> None:
        log("[1/6] ios image auto (挂载开发者镜像) ...")
        rc, out, errs = run([self.ios_exe, "image", "auto", "--udid", self.udid],
                            timeout=180)
        log("  image auto exit=%d" % rc)
        if rc != 0:
            raise RuntimeError("image auto 失败(exit=%d)。请手动执行:\n  %s image auto --udid %s\n看真实报错(可能需联网下载 DDI)。\n%s%s"
                               % (rc, self.ios_exe, self.udid, out[-800:], errs[-800:]))
        time.sleep(5)

    def step_kill_agent(self) -> None:
        log("[2/6] 清理 60105 残留 tunnel agent ...")
        pids = listen_pids_on(AGENT_PORT)
        if pids:
            kill_pids(pids)
            time.sleep(3)
        else:
            log("  无 60105 残留")

    def step_tunnel_start(self) -> None:
        log("[3/6] 启动 userspace tunnel ...")
        if not is_admin():
            raise RuntimeError("tunnel start 需要管理员权限。请用管理员身份重跑本脚本, "
                               "或先手动在管理员终端执行:\n  %s tunnel start --udid %s"
                               % (self.ios_exe, self.udid))
        self._spawn([self.ios_exe, "tunnel", "start", "--udid", self.udid], "tunnel")
        # 等 60105 LISTENING (最长 60s)
        deadline = time.time() + 60
        ok = False
        while time.time() < deadline:
            if listen_pids_on(AGENT_PORT):
                ok = True
                break
            time.sleep(2)
        if not ok:
            raise RuntimeError("tunnel 60s 未就绪(60105 未 LISTENING)。查看日志: %s.tunnel.log"
                               % self.log_path)
        log("  tunnel ready (60105 LISTENING)")
        time.sleep(5)  # [5/6] settle: 等 testmanagerd/RSD 可达

    def step_runwda(self, bundle_id: str) -> None:
        log("[6/6] 启动 runwda ...")
        self._spawn([self.ios_exe, "runwda", "--udid", self.udid,
                     "--bundleid", bundle_id,
                     "--testrunnerbundleid", bundle_id,
                     "--xctestconfig", XCTEST_CONFIG], "runwda")


def activate_with_goios(ios_exe: str, udid: str, bundle_id: str, port: int,
                        major: int, need_tunnel: bool, log_path: str,
                        ready_timeout: float) -> bool:
    act = GoIOSActivator(ios_exe, udid, log_path)
    try:
        act.step_image_auto()
        if need_tunnel:
            act.step_kill_agent()
            act.step_tunnel_start()
        act.step_runwda(bundle_id)
    except Exception as e:
        err("go-ios 激活失败: %s" % e)
        return False

    log("等待 WDA /status 就绪 (最长 %.0fs) ..." % ready_timeout)
    deadline = time.time() + ready_timeout
    last_tail = ""
    while time.time() < deadline:
        if wda_ready(udid, port, timeout=5.0):
            log("WDA READY: http+usbmux://%s:%d" % (udid, port))
            _save_pids(act.pids, udid, port)
            return True
        tail = _tail_log(log_path + ".runwda.log", 600)
        if tail != last_tail and time.time() % 10 < 1:
            last_tail = tail
        time.sleep(2)

    err("WDA 未在 %ss 内就绪。runwda 日志尾部:" % ready_timeout)
    print("----- runwda.log tail -----")
    print(_tail_log(log_path + ".runwda.log", 2500))
    _hint_runwda_errors(_tail_log(log_path + ".runwda.log", 8000))
    return False


def activate_with_tidevice(udid: str, bundle_id: str) -> bool:
    """iOS<=16 兜底: tidevice/tins2 xctest(与 wdap 库既有 _start_wda_xctest 同思路)"""
    exe = shutil.which("tins2") or shutil.which("tidevice")
    if not exe:
        err("未找到 tidevice/tins2。请先: pip install tidevice")
        return False
    log("tidevice 兜底启动: %s xctest -u %s -B %s" % (exe, udid, bundle_id))
    args = [exe, "xctest", "-u", udid, "-B", bundle_id]
    f = open(os.path.join(LOG_DIR, "tidevice_xctest.log"), "ab", buffering=0)
    subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT,
                     creationflags=0x08000000)
    return True


# --------------------------------------------------------------------------- #
# 状态与清理
# --------------------------------------------------------------------------- #
def _save_pids(pids: list, udid: str, port: int) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    data = {"udid": udid, "port": port, "pids": pids}
    with open(PID_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log("PID 记录: %s" % PID_FILE)


def _load_pids():
    if not os.path.isfile(PID_FILE):
        return {}
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def stop_all() -> None:
    data = _load_pids()
    pids = data.get("pids") or []
    if not pids:
        # 兜底: 清理本机 runwda 相关(谨慎: 只杀记录过的与 60105 agent)
        log("无 PID 记录, 尝试清理 60105 agent")
        kill_pids(listen_pids_on(AGENT_PORT))
        return
    log("结束记录的进程: %s" % pids)
    kill_pids(pids)
    # tunnel agent 若仍残留一并清
    kill_pids(listen_pids_on(AGENT_PORT))


def _tail_log(path: str, n: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return "(无日志)"


_RUNWDA_HINTS = [
    (r"ApplicationVerificationFailed|0xe8008015|provisioning profile", "证书/描述文件未信任 → 设备 设置-通用-VPN与设备管理 信任开发者, 或用企业证书重签"),
    (r"Developer Mode|developer mode", "设备未开启开发者模式 → 设置-隐私与安全性-开发者模式 打开后重启"),
    (r"not.*found|No such file|failed to find", "bundle id 不对或 WDA 未安装 → ios apps --udid 确认, 用 ios install 装 WDA.ipa"),
    (r"Symbol not found|_OBJC_CLASS|dyld", "WDA 二进制与设备系统不兼容(SDK/系统符号) → 用与设备 iOS 匹配的 SDK 重编 WDA(见 activate_wda18/build_wda_ios18_sdk.sh)"),
    (r"timed out|timeout", "XCTest 握手超时 → 确认 tunnel 存活, 设备开发者模式, 重试"),
]


def _hint_runwda_errors(tail: str) -> None:
    for pat, hint in _RUNWDA_HINTS:
        if re.search(pat, tail, re.IGNORECASE):
            err("诊断提示: %s" % hint)


# --------------------------------------------------------------------------- #
# 探测命令
# --------------------------------------------------------------------------- #
def do_check(ios_exe: str, udid: str, port: int) -> int:
    if not ios_exe:
        err("找不到 ios.exe, 请设 WDA_IOS_EXE 或放入候选路径")
        return 2
    log("ios.exe: %s" % ios_exe)
    devs = gios_devices(ios_exe)
    if not devs:
        err("未检测到 USB 设备(请插线并在手机上点“信任”)")
        return 3
    if not udid:
        if len(devs) > 1:
            err("检测到多台设备, 请用 --udid 指定:")
            for d in devs:
                print("   ", d.get("udid"))
            return 4
        udid = devs[0].get("udid")
    log("设备: %s" % udid)
    info = gios_info(ios_exe, udid)
    major = ios_major(info)
    log("系统: %s (major=%s)" % (info.get("ProductVersion", "?"), major))
    log("WDA ready: %s" % wda_ready(udid, port, timeout=5.0))
    log("wintun: %s" % ("OK" if _wintun_ok(ios_exe) else "MISSING(iOS17+ tunnel 需要)"))
    log("管理员: %s" % is_admin())
    return 0


def _wintun_ok(ios_exe: str) -> bool:
    return (os.path.isfile(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                        "System32", "wintun.dll"))
            or os.path.isfile(os.path.join(os.path.dirname(ios_exe), "wintun.dll")))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="POC 自动激活 WDA (go-ios 主 / tidevice 兜底, iOS16-26)")
    ap.add_argument("--udid", default="", help="设备 UDID(默认: 唯一 USB 设备)")
    ap.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID, help="WDA runner bundle id")
    ap.add_argument("--port", type=int, default=8100, help="WDA HTTP 端口(默认 8100)")
    ap.add_argument("--backend", choices=["auto", "goios", "tidevice"], default="auto")
    ap.add_argument("--ready-timeout", type=float, default=90.0, help="等待 WDA 就绪秒数")
    ap.add_argument("--stop", action="store_true", help="结束本工具拉起的 runwda/tunnel 进程")
    ap.add_argument("--check", action="store_true", help="只探测, 不激活")
    args = ap.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)

    if args.stop:
        stop_all()
        return 0

    ios_exe = find_ios_exe()
    if not ios_exe:
        err("找不到 ios.exe。请设环境变量 WDA_IOS_EXE 指向 go-ios Windows 版, 或放入候选路径")
        return 2

    if args.check:
        return do_check(ios_exe, args.udid, args.port)

    # ---- 设备选择 ----
    devs = gios_devices(ios_exe)
    if not devs:
        err("未检测到 USB 设备。请插线 → 手机上点“信任”后重试")
        return 3
    udid = args.udid
    if not udid:
        if len(devs) > 1:
            err("检测到多台设备: %s" % [d.get("udid") for d in devs])
            err("请用 --udid 指定要激活的设备")
            return 4
        udid = devs[0].get("udid")
    log("目标设备: %s" % udid)

    # ---- 版本探测 ----
    info = gios_info(ios_exe, udid)
    major = ios_major(info)
    log("iOS 版本: %s" % info.get("ProductVersion", "未知(读取失败)"))
    if major is None:
        err("无法读取设备系统版本, 中止(排查: 设备是否信任本机 / usbmuxd 是否正常)")
        return 5

    # ---- WDA 已 ready? ----
    if wda_ready(udid, args.port, timeout=8.0):
        log("WDA 已在运行: http+usbmux://%s:%d (无需激活)" % (udid, args.port))
        return 0

    need_tunnel = major >= 17
    log("后端=go-ios, need_tunnel=%s (iOS>=17 需 userspace tunnel)" % need_tunnel)
    if not _wintun_ok(ios_exe) and need_tunnel:
        err("iOS 17+ 需要 wintun.dll: 放到 C:\\Windows\\System32 或 ios.exe 同目录")
        return 6

    ok = activate_with_goios(ios_exe, udid, args.bundle_id, args.port,
                             major, need_tunnel,
                             os.path.join(LOG_DIR, "activate_%s" % re.sub(r"[^0-9A-Za-z]", "", udid)),
                             args.ready_timeout)

    # ---- 兜底: iOS<17 且 go-ios 失败 ----
    if not ok and (args.backend in ("auto", "tidevice")) and major < 17:
        log("go-ios 失败且 iOS<17, 回退 tidevice ...")
        activate_with_tidevice(udid, args.bundle_id)
        log("等待 WDA ready (tidevice, %.0fs) ..." % args.ready_timeout)
        deadline = time.time() + args.ready_timeout
        while time.time() < deadline:
            if wda_ready(udid, args.port, timeout=5.0):
                log("WDA READY (tidevice): http+usbmux://%s:%d" % (udid, args.port))
                return 0
            time.sleep(2)
        err("tidevice 启动后 WDA 仍未就绪")
        return 7

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
