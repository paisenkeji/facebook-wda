#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WDA 连通性诊断：定位"设备上 WDA 明明在跑，客户端却说 WDA 没启动 / 隧道被 RST"卡在哪一层。

逐层探测，每层给出明确结论：

  0. 是否有其它工具（tidevice / ios.exe / iTunes 等）在争用 usbmux
  1. usbmuxd（Windows: Apple Mobile Device Service 的 127.0.0.1:27015）是否可达
  2. usbmuxd 能否枚举到设备（USB / Network）—— 控制通道是否健康
  3. 中继到目标端口分三段拆开测，并连测多次看是否"时好时坏"：
       create_mux() 版本协商 -> Connect 命令 -> 隧道上跑原始 HTTP
  3b. **对照组**：同一条中继去连 lockdown(62078)。设备在就必然监听，
       用它把"中继通道坏了"和"目标端口没服务"彻底分开。
  4. 库内本地转发（UsbmuxPortForwarder，USBClient 默认通道）取 /status
  5. 走 http+usbmux 取 /status（旧的直连通道）
  6. 走 http://127.0.0.1:8100 取 /status（go-ios / iproxy 之类的外部端口转发）

把完整输出贴回来即可定位。

用法::

    python diagnose_wda.py                 # 自动选唯一 USB 设备
    python diagnose_wda.py <UDID>          # 指定设备
    python diagnose_wda.py <UDID> 8100     # 指定端口
"""
import socket
import subprocess
import sys
import time

import wdap
from wdap.usbmux.exceptions import HTTPError as MuxHTTPError
from wdap.usbmux.pyusbmux import MuxConnection, create_mux, list_devices, select_device

#: 第 3 层重复次数：一次成功一次失败 = 典型的连接 churn 问题
RELAY_ATTEMPTS = 3

#: 对照组端口。lockdown 服务只要设备连着就一定在监听，用它把
#: "中继通道坏了" 和 "目标端口没服务" 这两件性质完全不同的事分开。
CONTROL_PORT = 62078

#: 可能占用 usbmux 的进程镜像名（不含 .exe）。
#: 注意：**不要**把 AppleMobileDeviceService 放进来——它就是 usbmuxd 本身，运行是必须的，
#: 把它当"争用进程"会误报。
_NOISY_STEMS = (
    "tidevice", "ios", "itunes", "ipodservice", "apple devices",
    "aisi", "i4tools", "pphelper", "xctest", "webdriveragent",
)


def head(title):
    print("\n=== %s ===" % title)


def ok(msg):
    print("  [OK]   %s" % msg)


def bad(msg):
    print("  [FAIL] %s" % msg)


def info(msg):
    print("  [--]   %s" % msg)


# --------------------------------------------------------------------------- #
# 0. 进程争用
# --------------------------------------------------------------------------- #
def step0_competing_processes():
    head("0. 是否有其它工具在争用 usbmux")
    if sys.platform not in ("win32", "cygwin"):
        info("非 Windows，跳过（macOS/Linux 上请自行检查 idevice*/iproxy/usbmuxd 相关进程）")
        return
    try:
        proc = subprocess.run(["tasklist"], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=15)
        text = proc.stdout.decode("gbk", "replace")
    except Exception as err:  # noqa: BLE001
        info("tasklist 执行失败: %r" % (err,))
        return

    hits = {}
    for line in text.splitlines():
        low = line.lower()
        for stem in _NOISY_STEMS:
            # 按 "<名字>.exe" 精确匹配，避免 "ios" 这类短词误伤其它镜像名
            if (stem + ".exe") in low:
                hits[stem] = hits.get(stem, 0) + 1
                break
    if hits:
        bad("发现可能占用 usbmux 的进程: %s" % ", ".join(
            "%s.exe x%d" % (k, v) for k, v in sorted(hits.items())))
        info("多个工具同时抢 usbmux 会让隧道被反复 RST。先全部结束（含上次跑挂掉的 tidevice）再重试")
        info("Windows: taskkill /F /IM tidevice.exe  /  taskkill /F /IM ios.exe")
    else:
        ok("未发现明显争用进程（tidevice / ios.exe / iTunes 等）")

    # Apple Mobile Device Service 是否在跑
    for svc in ("AppleMobileDeviceService", "Apple Mobile Device Service"):
        try:
            q = subprocess.run(["sc", "query", svc], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=10)
            out = q.stdout.decode("gbk", "replace")
            if "RUNNING" in out.upper():
                ok("服务 %s 正在运行" % svc)
            elif "STOPPED" in out.upper():
                bad("服务 %s 已停止 -> net start \"%s\"" % (svc, svc))
            else:
                info("服务 %s 状态未知" % svc)
            break
        except Exception:  # noqa: BLE001
            continue


# --------------------------------------------------------------------------- #
# 1. usbmuxd 可达
# --------------------------------------------------------------------------- #
def step1_usbmuxd():
    head("1. usbmuxd 是否可达")
    host, port = MuxConnection.ITUNES_HOST
    try:
        sock = socket.create_connection((host, port), timeout=3)
        sock.close()
        ok("TCP %s:%d 可连" % (host, port))
        return True
    except OSError as err:
        bad("TCP %s:%d 不可连 -> %r" % (host, port, err))
        info("Windows 上需安装 iTunes / Apple Devices，且 'Apple Mobile Device Service' 正在运行")
        info("macOS/Linux 上对应 /var/run/usbmuxd")
        return False


# --------------------------------------------------------------------------- #
# 2. 设备枚举（= 控制通道是否健康）
# --------------------------------------------------------------------------- #
def step2_devices(udid):
    head("2. usbmuxd 能否枚举到设备（控制通道是否健康）")
    try:
        devices = list_devices()
    except Exception as err:  # noqa: BLE001
        bad("list_devices() 失败 -> %r" % err)
        if isinstance(err, MuxHTTPError) and err.args:
            info("底层原因: %r" % (err.args[0],))
        return None
    if not devices:
        bad("枚举到 0 台设备")
        info("检查：USB 线/接口、手机是否点了『信任此电脑』、是否被其它工具独占")
        return None
    for dev in devices:
        info("%s  connection_type=%s  devid=%s" % (dev.serial, dev.connection_type, dev.devid))
    if udid:
        chosen = select_device(udid)
        if chosen is None:
            bad("指定 UDID %s 未在列表里" % udid)
            return None
        ok("按 UDID 选中: %s" % chosen.serial)
        return chosen
    usb = [d for d in devices if d.connection_type == 'USB']
    if not usb:
        bad("没有 USB 设备（只有 Network/WiFi）——http+usbmux 依赖 USB 通道")
        return None
    if len(usb) > 1:
        info("有多台 USB 设备，自动取第一台；建议显式传 UDID")
    ok("选中: %s" % usb[0].serial)
    return usb[0]


# --------------------------------------------------------------------------- #
# 3. 中继分层 + 重复
# --------------------------------------------------------------------------- #
def _relay_once(device, port, index):
    """完整走一次 create_mux -> Connect -> 隧道上发 HTTP，返回 (阶段, 说明)"""
    started = time.time()

    # 3b 版本协商（内部会连 usbmuxd 两次）
    try:
        mux = create_mux()
    except Exception as err:  # noqa: BLE001
        return "3b", "create_mux() 失败 -> %r" % (err,)

    # 3c Connect 命令：这一步的失败原因最能说明问题
    try:
        mux._connect(device.devid, socket.htons(port))  # noqa: SLF001 - 诊断脚本，故意拆开
    except Exception as err:  # noqa: BLE001
        try:
            mux.close()
        except Exception:  # noqa: BLE001
            pass
        return "3c", "Connect 被 usbmuxd 拒绝 -> %r" % (err,)

    # 3d 隧道上跑原始 HTTP（lib 里就是这条路）
    tunnel = mux._sock.sock  # noqa: SLF001
    try:
        tunnel.settimeout(5)
        tunnel.sendall(b"GET /status HTTP/1.1\r\nHost: localhost\r\n"
                       b"Connection: close\r\n\r\n")
        data = bytearray()
        while True:
            chunk = tunnel.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        ms = (time.time() - started) * 1000
        if not data:
            return "3d", "隧道通了但没收到任何字节（对端直接关闭）耗时 %.0fms" % ms
        first = bytes(data[:64]).decode("latin-1", "replace").replace("\r\n", " | ")
        return "3d-OK", "收到 %d 字节，耗时 %.0fms，首行: %s" % (len(data), ms, first)
    except Exception as err:  # noqa: BLE001
        ms = (time.time() - started) * 1000
        return "3d", "隧道上 HTTP 读写失败（%.0fms）-> %r" % (ms, err)
    finally:
        try:
            tunnel.close()
        except Exception:  # noqa: BLE001
            pass


def step3_relay(device, port):
    head("3. 中继到设备 %d（拆成 3b/3c/3d，重复 %d 次）" % (port, RELAY_ATTEMPTS))
    if device is None:
        info("跳过（上一步没拿到设备）")
        return None

    stages = []
    for i in range(1, RELAY_ATTEMPTS + 1):
        stage, detail = _relay_once(device, port, i)
        stages.append(stage)
        line = "#%d [%s] %s" % (i, stage, detail)
        if stage == "3d-OK":
            ok(line)
        elif stage == "3c":
            bad(line)
            info("『CONNREFUSED / MuxConnectError』= 设备在线但该端口没人监听：WDA 没真正启动，或端口不是 %d" % port)
        else:
            bad(line)

    success = sum(1 for s in stages if s == "3d-OK")
    print("  ---- 结论：%d/%d 次成功 ----" % (success, RELAY_ATTEMPTS))
    if success == 0 and "3c" in stages:
        info("→ 端口层面就被拒：去确认设备上 8100 到底有没有服务（WDA 是否真的起来了）")
    elif 0 < success < RELAY_ATTEMPTS:
        info("→ 时好时坏 = 典型 usbmux 连接 churn / 争用（看第 0 步的进程争用、第 1 步服务状态）")
    elif success == 0:
        info("→ Connect 被接受但隧道上无法收发 = 设备端服务立刻断开，或中继通道被抢")
    else:
        info("→ 中继通道本身正常；问题在更上层（看第 4 步）")
    return success


def step3b_control(device, port):
    """对照组：同一条中继去连 lockdown。用来把"中继坏了"和"目标端口没服务"分开。"""
    head("3b. 对照组：同一条中继连 lockdown:%d（设备在就必然监听）" % CONTROL_PORT)
    if device is None:
        info("跳过（上一步没拿到设备）")
        return None
    stage, detail = _relay_once(device, CONTROL_PORT, 0)
    if stage == "3d-OK":
        ok("中继通道正常（lockdown 通）: %s" % detail)
        return True
    bad("中继通道也不通: [%s] %s" % (stage, detail))
    return False


# --------------------------------------------------------------------------- #
# 4 / 5 / 6. 上层通道
# --------------------------------------------------------------------------- #
def step4_forward(udid, port):
    head("4. 库内本地转发 UsbmuxPortForwarder -> 设备 %d（USBClient 默认走这条）" % port)
    forwarder = wdap.UsbmuxPortForwarder(udid=udid, remote_port=port)
    try:
        forwarder.start()
    except Exception as err:  # noqa: BLE001
        bad("转发器启动失败: %r" % (err,))
        return False
    try:
        info("本机监听 %s，隧道按需建立（不随请求重建）" % forwarder.url)
        client = wdap.Client(forwarder.url)
        ready, res = client.probe(timeout=5)
        if ready:
            ok("就绪。value=%r" % (res.value,))
            info("→ 可直接用 USBClient(transport='forward') 或 Client('%s')" % forwarder.url)
            return True
        bad("不可用: %s" % wdap._explain_probe_error(res))
        if forwarder.last_tunnel_error is not None:
            info("隧道层原因: %s" % wdap._explain_probe_error(forwarder.last_tunnel_error))
        return False
    finally:
        forwarder.stop()


def step5_usbmux_http(udid, port):
    head("5. http+usbmux://%s:%d/status（旧的直连通道，按请求建隧道）" % (udid, port))
    client = wdap.Client("http+usbmux://%s:%d" % (udid, port))
    ready, res = client.probe(timeout=5)
    if ready:
        ok("就绪。value=%r" % (res.value,))
        return True
    bad("不可用: %s" % wdap._explain_probe_error(res))
    info("原始异常: %r" % (res,))
    return False


def step6_local_http(port):
    head("6. http://127.0.0.1:%d/status（外部工具做的端口转发，如 go-ios / iproxy）" % port)
    client = wdap.Client("http://127.0.0.1:%d" % port)
    ready, res = client.probe(timeout=3)
    if ready:
        ok("就绪。value=%r" % (res.value,))
        info("说明有工具把设备 %d 端口转发到了本机 —— 直接用 Client('http://127.0.0.1:%d') 即可" % (port, port))
        return True
    info("不可用（正常，若你没做端口转发）: %r" % (res,))
    return False


def main():
    udid = sys.argv[1] if len(sys.argv) > 1 else ""
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8100

    print("diagnose_wda.py  wdap %s  port=%d  udid=%s" % (wdap.__version__, port, udid or "(auto)"))

    step0_competing_processes()
    step1_usbmuxd()
    device = step2_devices(udid)
    if device is not None and not udid:
        udid = device.serial

    relay_ok = step3_relay(device, port)
    control_ok = step3b_control(device, port)

    if relay_ok == 0 and control_ok is True:
        head("★ 判定：中继没问题，是设备上 %d 端口没有可用服务" % port)
        info("同一时刻 lockdown(%d) 能通、%d 不能通 —— 本机 usbmux 中继是好的。" % (CONTROL_PORT, port))
        info("也就是说 WDA 的 runner 进程很可能已经不接受连接了（被挂起/会话结束，")
        info("端口还占着但没人处理），需要在设备上把 WDA 重新跑起来再连：")
        info("  · go-ios:  ios tunnel start → ios runwda")
        info("  · 或 Xcode 打开 WebDriverAgent 重新 Run 一次（保持会话不退出）")
    elif relay_ok == 0 and control_ok is False:
        head("★ 判定：本机 usbmux 中继整体不可用（与 WDA 无关）")
        info("lockdown(%d) 和 %d 都不通 → 是 usbmuxd / AMDS 通道坏了：" % (CONTROL_PORT, port))
        info("  1) 先结束第 0 步列出的争用进程（尤其是上次跑挂掉的 tidevice）")
        info("  2) 重启 Apple Mobile Device Service，或重插 USB / 换线换口")
        info("  3) 再跑一次本脚本")

    if udid:
        forward_ok = step4_forward(udid, port)
        usbmux_ok = step5_usbmux_http(udid, port)
        if forward_ok or usbmux_ok:
            return 0
    if step6_local_http(port):
        return 0

    head("建议")
    info("· 中继/隧道层不通时，绕开 USB：WiFi 直连 Client('http://<设备IP>:8100')")
    info("· 或让 go-ios 做转发：ios forward 8100 8100，再 Client('http://127.0.0.1:8100')")
    info("· iOS 17+ 若需隧道：ios tunnel start（需管理员），然后再 forward")
    return 1


if __name__ == '__main__':
    sys.exit(main())
