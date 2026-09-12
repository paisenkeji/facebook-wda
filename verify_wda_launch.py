#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wdap.wda_launch 自检：iOS 版本分流 + tidevice / go-ios 双后端

本机没有真机也没有 tidevice/go-ios，这里用**假的 .bat 可执行文件**代替：
成功场景 = 批处理睡够时间不退出；失败场景 = 立刻 exit 1。
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wdap import wda_launch as wl  # noqa: E402

UDID = "00008030-000D65402EF8202E"

#: 模拟"一直在跑"的后端：睡约 30 秒。
#: 用 ping 而不是 timeout —— 测试常在 Git Bash 下跑，它的 PATH 里的
#: timeout 是 GNU coreutils，不认 Windows 的 /t 参数（exit 125）。
BAT_ALIVE = ("@echo off\r\n"
             "%SystemRoot%\\System32\\ping.exe -n 31 127.0.0.1 > nul\r\n")
#: 模拟"启动即失败"的后端
BAT_DEAD = "@echo off\r\nexit /b 1\r\n"


def _write_bat(directory: str, name: str, body: str) -> str:
    path = os.path.join(directory, name + ".bat")
    with open(path, "w", encoding="ascii") as fh:
        fh.write(body)
    return path


def _kill(pid: Optional[int]) -> None:
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10)
    except Exception:  # noqa: BLE001
        pass


class FakeToolMixin(unittest.TestCase):
    """造两个假工具，并把 shutil.which/read_ios_version 钉死

    继承 TestCase 但不写 test_* 方法，本身不产生用例，只给下面几个类复用。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="wdap_launch_")
        self.alive = _write_bat(self.tmp, "alive", BAT_ALIVE)
        self.dead = _write_bat(self.tmp, "dead", BAT_DEAD)
        self.dead2 = _write_bat(self.tmp, "dead2", BAT_DEAD)
        self._pids: List[int] = []
        self._orig_which = wl.shutil.which
        self._orig_version = wl.read_ios_version
        # 默认钉成"隧道已就绪"：否则每条 go-ios 用例都会真的去 spawn
        # `ios tunnel start` 并等 /ready 30 秒
        self._orig_tunnel_alive = wl.tunnel_agent_alive
        self.tunnel_alive = True
        self.tunnel_calls = 0

        def fake_tunnel_alive(*a, **kw):
            self.tunnel_calls += 1
            return self.tunnel_alive

        wl.tunnel_agent_alive = fake_tunnel_alive
        self._which_map = {"tins2": None, "tidevice": None,
                           "ios": None, "go-ios": None}
        wl.shutil.which = lambda name, *a, **kw: self._which_map.get(name)
        wl.read_ios_version = lambda *a, **kw: self.ios_version

    def tearDown(self) -> None:
        for pid in self._pids:
            _kill(pid)
        wl.shutil.which = self._orig_which
        wl.read_ios_version = self._orig_version
        wl.tunnel_agent_alive = self._orig_tunnel_alive

    def track(self, result: wl.WdaLaunchResult) -> wl.WdaLaunchResult:
        if result.pid:
            self._pids.append(result.pid)
        return result


class TestVersionParsing(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(wl.parse_ios_version("16.7.2"), (16, 7, 2))
        self.assertEqual(wl.parse_ios_version("17.0"), (17, 0, 0))
        self.assertEqual(wl.parse_ios_version("18.7.1"), (18, 7, 1))
        self.assertEqual(wl.parse_ios_version("26"), (26, 0, 0))
        self.assertEqual(wl.parse_ios_version("26.0.1"), (26, 0, 1))

    def test_parse_bad(self):
        for bad in ("", "abc", None, 123, b"\xff\xfe"):
            self.assertIsNone(wl.parse_ios_version(bad), repr(bad))

    def test_parse_bytes(self):
        self.assertEqual(wl.parse_ios_version(b"18.7.1"), (18, 7, 1))


class TestBackendSelection(unittest.TestCase):
    def test_auto_by_version(self):
        self.assertEqual(wl.pick_backend("16.7.2")[0], wl.BACKEND_TIDEVICE)
        self.assertEqual(wl.pick_backend("16.0")[0], wl.BACKEND_TIDEVICE)
        self.assertEqual(wl.pick_backend("17.0")[0], wl.BACKEND_GOIOS)
        self.assertEqual(wl.pick_backend("18.7.1")[0], wl.BACKEND_GOIOS)
        self.assertEqual(wl.pick_backend("26.0.1")[0], wl.BACKEND_GOIOS)

    def test_unknown_prefers_goios_then_fallback(self):
        self.assertEqual(wl.pick_backend(None),
                         [wl.BACKEND_GOIOS, wl.BACKEND_TIDEVICE])

    def test_fallback_order_is_reversed(self):
        self.assertEqual(wl.pick_backend("16.0", fallback=True),
                         [wl.BACKEND_TIDEVICE, wl.BACKEND_GOIOS])
        self.assertEqual(wl.pick_backend("18.0", fallback=True),
                         [wl.BACKEND_GOIOS, wl.BACKEND_TIDEVICE])

    def test_no_fallback(self):
        self.assertEqual(wl.pick_backend("16.0", fallback=False),
                         [wl.BACKEND_TIDEVICE])
        self.assertEqual(wl.pick_backend("18.0", fallback=False),
                         [wl.BACKEND_GOIOS])

    def test_forced_strategy_ignores_version(self):
        self.assertEqual(wl.pick_backend("18.0", "tidevice"), [wl.BACKEND_TIDEVICE])
        self.assertEqual(wl.pick_backend("16.0", "goios"), [wl.BACKEND_GOIOS])

    def test_bad_strategy(self):
        with self.assertRaises(ValueError):
            wl.pick_backend("18.0", "pymobiledevice3")


class TestCommandBuilding(unittest.TestCase):
    def test_tidevice_basic(self):
        cmd = wl.build_command(wl.BACKEND_TIDEVICE, "tidevice", UDID)
        self.assertEqual(cmd[:4], ["tidevice", "-u", UDID, "xctest"])
        self.assertNotIn("-B", cmd)

    def test_tidevice_bundle_and_env_use_colon(self):
        cmd = wl.build_command(wl.BACKEND_TIDEVICE, "tidevice", UDID,
                               wda_bundle_id="com.demo.xctrunner",
                               wda_port=8123)
        self.assertIn("-B", cmd)
        self.assertEqual(cmd[cmd.index("-B") + 1], "com.demo.xctrunner")
        # tidevice 的 -e 分隔符是冒号，写等号它会当成一个整体 key
        self.assertIn("USE_PORT:8123", cmd)

    def test_goios_basic(self):
        cmd = wl.build_command(wl.BACKEND_GOIOS, "ios", UDID)
        self.assertEqual(cmd[:2], ["ios", "runwda"])
        self.assertIn("--udid=%s" % UDID, cmd)
        # 没给 bundle id 时三个参数都不出现（go-ios 的规则：全给或全不给）
        self.assertFalse(any(c.startswith("--bundleid") for c in cmd))
        self.assertFalse(any(c.startswith("--xctestconfig") for c in cmd))

    def test_goios_all_three_or_none(self):
        cmd = wl.build_command(wl.BACKEND_GOIOS, "ios", UDID,
                               wda_bundle_id="com.demo.xctrunner")
        flags = [c.split("=")[0] for c in cmd]
        for required in ("--bundleid", "--testrunnerbundleid", "--xctestconfig"):
            self.assertIn(required, flags, cmd)

    def test_goios_env_and_port_use_equal(self):
        cmd = wl.build_command(wl.BACKEND_GOIOS, "ios", UDID, wda_port=8123,
                               extra_env={"MJPEG_SERVER_PORT": "8124"},
                               extra_args=["--verbose"])
        self.assertIn("--env=USE_PORT=8123", cmd)
        self.assertIn("--env=MJPEG_SERVER_PORT=8124", cmd)
        self.assertIn("--arg=--verbose", cmd)

    def test_unknown_backend(self):
        with self.assertRaises(ValueError):
            wl.build_command("magic", "tool", UDID)


class TestFindTool(unittest.TestCase):
    def setUp(self):
        self._orig = wl.shutil.which
        self.map = {"tins2": None, "tidevice": None, "ios": None, "go-ios": None}
        wl.shutil.which = lambda name, *a, **kw: self.map.get(name)

    def tearDown(self):
        wl.shutil.which = self._orig

    def test_tidevice_prefers_tins2(self):
        self.map["tins2"] = r"C:\tins2.exe"
        self.map["tidevice"] = r"C:\tidevice.exe"
        self.assertEqual(wl.find_tool(wl.BACKEND_TIDEVICE), r"C:\tins2.exe")

    def test_tidevice_falls_back(self):
        self.map["tidevice"] = r"C:\tidevice.exe"
        self.assertEqual(wl.find_tool(wl.BACKEND_TIDEVICE), r"C:\tidevice.exe")

    def test_goios_lookup_order(self):
        self.map["go-ios"] = r"C:\go-ios.exe"
        self.assertEqual(wl.find_tool(wl.BACKEND_GOIOS), r"C:\go-ios.exe")
        self.map["ios"] = r"C:\ios.exe"
        self.assertEqual(wl.find_tool(wl.BACKEND_GOIOS), r"C:\ios.exe")

    def test_explicit_path_wins(self):
        self.assertEqual(wl.find_tool(wl.BACKEND_GOIOS, goios_path=r"D:\my\ios.exe"),
                         r"D:\my\ios.exe")
        self.assertEqual(wl.find_tool(wl.BACKEND_TIDEVICE,
                                      tidevice_path=r"D:\my\tidevice.exe"),
                         r"D:\my\tidevice.exe")

    def test_not_found(self):
        self.assertIsNone(wl.find_tool(wl.BACKEND_TIDEVICE))
        self.assertIsNone(wl.find_tool(wl.BACKEND_GOIOS))


class TestStartWdaIos16(FakeToolMixin):
    """iOS 16 —— 必须走 tidevice"""

    ios_version = "16.7.2"

    def test_ios16_picks_tidevice(self):
        self._which_map["tidevice"] = self.alive
        result = self.track(wl.start_wda(UDID, startup_wait=1.0))
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, wl.BACKEND_TIDEVICE)
        self.assertEqual(result.command[:4], [self.alive, "-u", UDID, "xctest"])
        self.assertEqual(result.ios_version, "16.7.2")
        self.assertTrue(result.log_path.endswith("wdap_tidevice_xctest.log"))

    def test_ios16_failure_falls_back_to_goios(self):
        self._which_map["tidevice"] = self.dead
        self._which_map["ios"] = self.alive
        result = self.track(wl.start_wda(UDID, startup_wait=1.0))
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, wl.BACKEND_GOIOS)
        self.assertEqual(result.command[:2], [self.alive, "runwda"])

    def test_ios16_no_fallback_stays_on_tidevice(self):
        self._which_map["tidevice"] = self.dead
        self._which_map["ios"] = self.alive
        result = self.track(wl.start_wda(UDID, fallback=False, startup_wait=1.0))
        self.assertFalse(result.ok)
        self.assertEqual(result.backend, wl.BACKEND_TIDEVICE)
        self.assertIn("exit=1", result.detail)

    def test_ios16_forced_goios(self):
        self._which_map["tidevice"] = self.alive
        self._which_map["ios"] = self.alive
        result = self.track(wl.start_wda(UDID, strategy=wl.BACKEND_GOIOS,
                                         startup_wait=1.0))
        self.assertEqual(result.backend, wl.BACKEND_GOIOS)


class TestStartWdaIos17Plus(FakeToolMixin):
    """iOS 17+ / 26 —— 必须走 go-ios"""

    ios_version = "18.7.1"

    def test_ios18_picks_goios(self):
        self._which_map["ios"] = self.alive
        result = self.track(wl.start_wda(UDID, startup_wait=1.0))
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, wl.BACKEND_GOIOS)
        self.assertEqual(result.command[:2], [self.alive, "runwda"])
        self.assertIn("--udid=%s" % UDID, result.command)
        self.assertTrue(result.log_path.endswith("wdap_goios_runwda.log"))

    def test_ios26_picks_goios(self):
        wl.read_ios_version = lambda *a, **kw: "26.0.1"
        self._which_map["ios"] = self.alive
        result = self.track(wl.start_wda(UDID, startup_wait=1.0))
        self.assertEqual(result.backend, wl.BACKEND_GOIOS)
        self.assertEqual(result.ios_version, "26.0.1")

    def test_goios_failure_falls_back_to_tidevice(self):
        self._which_map["ios"] = self.dead
        self._which_map["tidevice"] = self.alive
        result = self.track(wl.start_wda(UDID, startup_wait=1.0))
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, wl.BACKEND_TIDEVICE)

    def test_goios_missing_hints_developer_image(self):
        self._which_map["ios"] = self.dead
        result = self.track(wl.start_wda(UDID, fallback=False, startup_wait=1.0))
        self.assertFalse(result.ok)
        self.assertIn("ios image auto", result.detail)
        self.assertIn("ios tunnel start", result.detail)


class TestStartWdaUnknownVersion(FakeToolMixin):
    ios_version = None

    def test_unknown_tries_goios_first(self):
        self._which_map["ios"] = self.alive
        self._which_map["tidevice"] = self.alive
        result = self.track(wl.start_wda(UDID, startup_wait=1.0))
        self.assertEqual(result.backend, wl.BACKEND_GOIOS)

    def test_unknown_falls_back_to_tidevice(self):
        self._which_map["ios"] = self.dead
        self._which_map["tidevice"] = self.alive
        result = self.track(wl.start_wda(UDID, startup_wait=1.0))
        self.assertEqual(result.backend, wl.BACKEND_TIDEVICE)


class TestNoToolAtAll(FakeToolMixin):
    ios_version = "18.7.1"

    def test_no_tool_returns_empty_command(self):
        result = wl.start_wda(UDID, startup_wait=1.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.command, [])
        self.assertIn("找不到", result.detail)
        self.assertIsNone(result.pid)

    def test_result_is_falsy(self):
        self.assertFalse(wl.start_wda(UDID, startup_wait=1.0))


class TestMountDeveloperImage(FakeToolMixin):
    ios_version = "18.7.1"

    def test_missing_goios_returns_false(self):
        self.assertFalse(wl.mount_developer_image(UDID))


class TestTunnel(FakeToolMixin):
    """iOS 17+ 的硬前置：go-ios tunnel daemon

    go-ios 的 runwda 不会自己起隧道，README 明确要求先 `ios tunnel start`。
    这里核对：探测函数、ensure_tunnel 的短路/拉起/放弃、以及 start_wda 的接线。
    """
    ios_version = "18.7.1"

    def test_agent_alive_false_on_closed_port(self):
        # 用一个几乎不可能被占用的端口，确保探不通时返回 False 而不是抛异常
        self.assertFalse(self._orig_tunnel_alive("127.0.0.1", 28999,
                                                 timeout=0.3))

    def test_ensure_tunnel_short_circuits_when_alive(self):
        self.tunnel_alive = True
        self.assertTrue(wl.ensure_tunnel(goios_path=self.alive))
        # /health 一次 + /ready 一次
        self.assertEqual(self.tunnel_calls, 2)

    def test_ensure_tunnel_no_start_returns_false(self):
        self.tunnel_alive = False
        self.assertFalse(wl.ensure_tunnel(goios_path=self.alive, start=False))
        self.assertEqual(self.tunnel_calls, 1)

    def test_ensure_tunnel_bad_mode(self):
        with self.assertRaises(ValueError):
            wl.ensure_tunnel(goios_path=self.alive, mode="quantum")

    def test_ensure_tunnel_missing_goios(self):
        self.tunnel_alive = False
        self.assertFalse(wl.ensure_tunnel(goios_path=None))

    def test_start_wda_goios_probes_tunnel_by_default(self):
        self._which_map["ios"] = self.alive
        self.tunnel_alive = True
        result = self.track(wl.start_wda(UDID, startup_wait=1.0))
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, "goios")
        self.assertGreaterEqual(self.tunnel_calls, 1)

    def test_start_wda_goios_can_skip_tunnel(self):
        self._which_map["ios"] = self.alive
        result = self.track(wl.start_wda(UDID, startup_wait=1.0,
                                         start_tunnel=False))
        self.assertTrue(result.ok)
        self.assertEqual(self.tunnel_calls, 0)

    def test_tidevice_backend_never_touches_tunnel(self):
        # tidevice 走老 lockdown 路径，不需要也不该去探 go-ios 隧道
        self._which_map["tidevice"] = self.alive
        result = self.track(wl.start_wda(UDID, startup_wait=1.0))
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, "tidevice")
        self.assertEqual(self.tunnel_calls, 0)


class TestConstants(unittest.TestCase):
    def test_threshold(self):
        self.assertEqual(wl.IOS_TIDEVICE_MAX, 16)

    def test_goios_defaults(self):
        self.assertEqual(wl.GOIOS_DEFAULT_XCTEST_CONFIG,
                         "WebDriverAgentRunner.xctest")
        self.assertEqual(wl.GOIOS_DEFAULT_BUNDLE_ID,
                         "com.facebook.WebDriverAgentRunner.xctrunner")

    def test_strategies(self):
        self.assertEqual(set(wl.ALL_STRATEGIES),
                         {"auto", "tidevice", "goios"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
