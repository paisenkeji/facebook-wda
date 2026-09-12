# coding: utf-8
"""/wda/log/* 客户端封装自检

用假的 client 记录请求，核对「请求契约」与「结果映射」两端：

- 契约来自服务端 ``FBLogCommands.m``：路径不带 session 前缀、GET 的参数
  走 JSON body（服务端只解析 body，不解析 query string）；
- 结果字段来自 ``FBRunLogStore.m`` 的 ``snapshotWithLimit`` / ``stats`` /
  ``entryPayload`` / handleCrash / handleConfig。

本机没有真机，这里验证的是客户端行为，跑法::

    python verify_log.py
"""

import sys
import unittest
from typing import Any, Dict, Optional

sys.path.insert(0, "D:/python_project/facebook-wda-master")

from wdap.exceptions import WDARequestError  # noqa: E402
from wdap.log import (Log, LogCategory, LogConfig, LogCrashReport, LogEntry,  # noqa: E402
                      LogLevel, LogSnapshot, LogStats, LogUnsupportedError)


class Resp(object):
    def __init__(self, value):
        self.value = value


class RawResp(object):
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class FakeClient(object):
    """记录 http.get / http.post / _fetch_raw 的调用"""

    def __init__(self, get_value=None, post_value=None, raw_text="", raise_on=None):
        self.calls = []
        self.raw_calls = []
        self.get_value = get_value if get_value is not None else {}
        self.post_value = post_value if post_value is not None else {"cleared": True}
        self.raw_text = raw_text
        self.raise_on = raise_on or {}

    # --- 模拟 wdap.BaseClient.http ------------------------------------ #
    @property
    def http(self):
        return _HTTP(self)

    def _fetch_raw(self, method, urlpath, data=None, timeout=None):
        self.raw_calls.append((method, urlpath, data, timeout))
        if "raw" in self.raise_on:
            raise self.raise_on["raw"]
        return RawResp(self.raw_text)


class _HTTP(object):
    def __init__(self, client: FakeClient):
        self._client = client

    def get(self, path, data=None, timeout=None):
        self._client.calls.append(("GET", path, data, timeout))
        key = path
        if key in self._client.raise_on:
            raise self._client.raise_on[key]
        return Resp(self._client.get_value)

    def post(self, path, data=None, timeout=None):
        self._client.calls.append(("POST", path, data, timeout))
        key = path
        if key in self._client.raise_on:
            raise self._client.raise_on[key]
        return Resp(self._client.post_value)


def unknown_command(path: str) -> WDARequestError:
    """服务端路由未注册时的返回"""
    return WDARequestError(110, {
        "error": "unknown command",
        "message": "Unhandled endpoint: %s" % path,
        "traceback": "",
    })


SNAPSHOT = {
    "runnerUptimeSeconds": 123.4,
    "total": 1000,
    "stored": 300,
    "capacity": 2000,
    "returned": 2,
    "filters": {"minLevel": "warn", "category": "http", "since": 7, "limit": 50},
    "capture": {"stderrRequested": True, "stderrActive": True, "capturedLines": 42},
    "entries": [
        {"seq": 8, "time": "2026-09-12T00:00:01+0800", "uptimeSec": 10.5,
         "level": "warn", "category": "http", "thread": "main",
         "message": "slow request"},
        {"seq": 9, "time": "2026-09-12T00:00:02+0800", "uptimeSec": 11.0,
         "level": "error", "category": "exception", "thread": "bg",
         "message": "boom"},
    ],
}

STATS = {
    "runnerUptimeSeconds": 61.0,
    "log": {"installed": True, "totalRecorded": 900, "stored": 300,
            "capacity": 2000, "suppressedEntries": 5},
    "http": {"requests": 1234, "nonSuccessResponses": 7, "slowRequests": 3,
             "slowestRequestSeconds": 4.5, "slowestRequest": "POST /wda/xxx"},
    "mjpeg": {"exceptions": 2, "screenshotFailurePeak": 6},
    "listenerRestarts": 1,
    "exceptionsByName": {"exception": 3, "http": 1},
    "capture": {"stderrRequested": True, "stderrActive": False,
                "capturedLines": 88, "oversizedLines": 2},
    "crash": {"previousSessionCrashed": True,
              "previousSessionReportBytes": 512,
              "reportPath": "/var/mobile/x.log"},
}

CRASH = {
    "previousSessionCrashed": True,
    "previousSessionReportBytes": 512,
    "reportPath": "/var/mobile/x.log",
    "report": "SIGABRT at ...",
    "cleared": True,
}

CONFIG = {
    "capacity": 5000,
    "storedEntries": 120,
    "stderrCaptureRequested": True,
    "stderrCaptureActive": True,
}


class TestLogContract(unittest.TestCase):
    """请求是否按服务端的契约发出"""

    def setUp(self):
        self.client = FakeClient(get_value=SNAPSHOT)
        self.log = Log(self.client)

    def test_recent_path_has_no_session_prefix(self):
        self.log.recent()
        method, path, data, _ = self.client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/wda/log/recent")
        self.assertNotIn("/session/", path)

    def test_recent_params_go_to_body_not_query(self):
        """服务端只解析 body：参数必须在 data 里，path 上不能挂 query"""
        self.log.recent(limit=50, level="warn", category="http", since=7)
        _, path, data, _ = self.client.calls[0]
        self.assertNotIn("?", path)
        self.assertEqual(data, {"limit": 50, "level": "warn",
                                "category": "http", "since": 7})

    def test_recent_without_params_sends_no_body(self):
        self.log.recent()
        _, _, data, _ = self.client.calls[0]
        self.assertIsNone(data)

    def test_errors_is_get_with_min_level_error(self):
        self.log.errors(limit=10, category=LogCategory.EXCEPTION)
        method, path, data, _ = self.client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/wda/log/errors")
        self.assertEqual(data, {"limit": 10, "category": "exception"})
        # 服务端固定用 FBRunLogLevelError，客户端不需要传 level
        self.assertNotIn("level", data)

    def test_stats_and_crash_are_get(self):
        self.log.stats()
        self.log.crash(clear=True)
        self.assertEqual(self.client.calls[0][:2], ("GET", "/wda/log/stats"))
        self.assertEqual(self.client.calls[1][:2], ("GET", "/wda/log/crash"))
        self.assertEqual(self.client.calls[1][2], {"clear": True})

    def test_crash_clear_defaults_to_false(self):
        self.log.crash()
        self.assertEqual(self.client.calls[0][2], {"clear": False})

    def test_clear_is_post(self):
        self.log.clear()
        self.assertEqual(self.client.calls[0][:2], ("POST", "/wda/log/clear"))

    def test_config_is_post_with_camel_case_keys(self):
        self.log.config(capacity=5000, stderr_capture=True)
        method, path, data, _ = self.client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/wda/log/config")
        self.assertEqual(data, {"capacity": 5000, "stderrCapture": True})

    def test_config_query_sends_no_body(self):
        self.log.config()
        self.assertIsNone(self.client.calls[0][2])

    def test_timeout_is_not_leaked_into_body(self):
        """timeout 是传输层参数，不能混进请求体"""
        self.log.stats(timeout=3.5)
        _, _, data, timeout = self.client.calls[0]
        self.assertIsNone(data)
        self.assertEqual(timeout, 3.5)


class TestLogDownload(unittest.TestCase):
    """/wda/log/download 返回 text/plain，不能走 JSON 通道"""

    def test_download_uses_raw_channel(self):
        client = FakeClient(raw_text="line1\nline2\n")
        log = Log(client)
        text = log.download(limit=100, level="warn")
        self.assertEqual(text, "line1\nline2\n")
        method, path, data, _ = client.raw_calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/wda/log/download")
        self.assertEqual(data, {"limit": 100, "level": "warn"})
        # 没有走 http.get（那会 json.loads 然后炸掉）
        self.assertEqual(client.calls, [])

    def test_download_defaults_to_max_limit(self):
        client = FakeClient(raw_text="x")
        Log(client).download()
        self.assertIsNone(client.raw_calls[0][2])

    def test_save_writes_utf8_text(self):
        import os
        import tempfile

        client = FakeClient(raw_text="中文日志\n")
        log = Log(client)
        path = os.path.join(tempfile.gettempdir(), "wda_run_log_test.txt")
        try:
            returned = log.save(path, limit=10)
            self.assertEqual(returned, path)
            with open(path, encoding="utf-8") as fp:
                self.assertEqual(fp.read(), "中文日志\n")
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestLogValidation(unittest.TestCase):
    """服务端会静默 clamp / 回退的参数，客户端提前拦"""

    def setUp(self):
        self.log = Log(FakeClient(get_value=SNAPSHOT))

    def test_limit_bounds(self):
        for bad in (0, -1, 20001, 10 ** 9):
            with self.assertRaises(ValueError):
                self.log.recent(limit=bad)
        self.log.recent(limit=1)
        self.log.recent(limit=20000)

    def test_unknown_level_is_rejected(self):
        """服务端把未知 level 静默当 info，这里必须报错"""
        for bad in ("warning", "err", "", "trace"):
            with self.assertRaises(ValueError):
                self.log.recent(level=bad)

    def test_level_is_case_insensitive_and_normalized(self):
        self.log.recent(level="WARN")
        self.assertEqual(self.client_of(self.log).calls[0][2]["level"], "warn")

    def test_since_must_be_non_negative_int(self):
        with self.assertRaises(ValueError):
            self.log.recent(since=-1)
        self.log.recent(since=0)

    def test_category_is_not_validated(self):
        """分类是纯字符串，服务端允许新分类"""
        self.log.recent(category="my-own-category")
        self.assertEqual(
            self.client_of(self.log).calls[0][2]["category"], "my-own-category")

    @staticmethod
    def client_of(log: Log) -> FakeClient:
        return log._client


class TestLogResults(unittest.TestCase):
    """结果字段映射"""

    def test_snapshot_mapping(self):
        snap = LogSnapshot(SNAPSHOT)
        self.assertEqual(snap.returned, 2)
        self.assertEqual(snap.total, 1000)
        self.assertEqual(snap.stored, 300)
        self.assertEqual(snap.capacity, 2000)
        self.assertAlmostEqual(snap.runner_uptime_seconds, 123.4)
        self.assertEqual(snap.filters["minLevel"], "warn")
        self.assertEqual(snap.capture["capturedLines"], 42)
        self.assertEqual(len(snap), 2)
        self.assertEqual(snap.last_seq, 9)

    def test_entry_mapping(self):
        entry = LogEntry(SNAPSHOT["entries"][1])
        self.assertEqual(entry.seq, 9)
        self.assertEqual(entry.level, "error")
        self.assertEqual(entry.category, "exception")
        self.assertEqual(entry.thread, "bg")
        self.assertEqual(entry.message, "boom")
        self.assertAlmostEqual(entry.uptime_sec, 11.0)
        self.assertIn("boom", entry.format())
        self.assertIn("ERROR", entry.format())

    def test_snapshot_iterates_entries(self):
        snap = LogSnapshot(SNAPSHOT)
        self.assertEqual([e.seq for e in snap], [8, 9])

    def test_stats_mapping(self):
        stats = LogStats(STATS)
        self.assertEqual(stats.http["requests"], 1234)
        self.assertEqual(stats.http["nonSuccessResponses"], 7)
        self.assertEqual(stats.mjpeg["exceptions"], 2)
        self.assertEqual(stats.listener_restarts, 1)
        self.assertEqual(stats.exceptions_by_name["exception"], 3)
        self.assertTrue(stats.previous_session_crashed)
        self.assertEqual(stats.capture["oversizedLines"], 2)
        self.assertAlmostEqual(stats.runner_uptime_seconds, 61.0)

    def test_crash_mapping(self):
        rep = LogCrashReport(CRASH)
        self.assertTrue(rep.previous_session_crashed)
        self.assertEqual(rep.report, "SIGABRT at ...")
        self.assertEqual(rep.report_bytes, 512)
        self.assertEqual(rep.report_path, "/var/mobile/x.log")
        self.assertTrue(rep.cleared)
        self.assertTrue(bool(rep))

    def test_config_mapping(self):
        cfg = LogConfig(CONFIG)
        self.assertEqual(cfg.capacity, 5000)
        self.assertEqual(cfg.stored_entries, 120)
        self.assertTrue(cfg.stderr_capture_requested)
        self.assertTrue(cfg.stderr_capture_active)

    def test_stats_defaults_when_missing(self):
        stats = LogStats({})
        self.assertEqual(stats.listener_restarts, 0)
        self.assertFalse(stats.previous_session_crashed)
        self.assertEqual(stats.http, {})


class TestLogUnsupported(unittest.TestCase):
    """旧 WDA 没有 /wda/log/* 路由"""

    def test_unknown_command_becomes_log_unsupported(self):
        client = FakeClient(raise_on={"/wda/log/recent": unknown_command("/wda/log/recent")})
        with self.assertRaises(LogUnsupportedError):
            Log(client).recent()

    def test_other_errors_are_not_swallowed(self):
        err = WDARequestError(13, {"error": "unknown error", "message": "boom"})
        client = FakeClient(raise_on={"/wda/log/stats": err})
        with self.assertRaises(WDARequestError):
            Log(client).stats()

    def test_available_is_false_when_route_missing(self):
        client = FakeClient(raise_on={"/wda/log/stats": unknown_command("/wda/log/stats")})
        self.assertFalse(Log(client).available())

    def test_available_is_true_when_route_present(self):
        client = FakeClient(get_value=STATS)
        self.assertTrue(Log(client).available())

    def test_download_unsupported(self):
        client = FakeClient(raise_on={"raw": unknown_command("/wda/log/download")})
        with self.assertRaises(LogUnsupportedError):
            Log(client).download()


class TestLogFollow(unittest.TestCase):
    """follow() 的增量语义：since 推进 + 不重复"""

    def test_follow_advances_since_and_dedups(self):
        class SeqClient(FakeClient):
            """每次返回 seq 递增的条目，模拟 WDA 不断产生新日志"""

            def __init__(self):
                super().__init__()
                self.seq = 0
                self.sinces = []

            @property
            def http(self):
                return _SeqHTTP(self)

        class _SeqHTTP(_HTTP):
            def get(self, path, data=None, timeout=None):
                client = self._client
                client.sinces.append((data or {}).get("since"))
                if len(client.sinces) > 4:
                    raise KeyboardInterrupt  # 让生成器停在这里
                client.seq += 1
                seq = client.seq
                return Resp({
                    "runnerUptimeSeconds": 1.0,
                    "total": seq, "stored": 1, "capacity": 100, "returned": 1,
                    "filters": {}, "capture": {},
                    "entries": [{"seq": seq, "time": "t", "uptimeSec": 1.0,
                                 "level": "info", "category": "http",
                                 "thread": "main", "message": "m%d" % seq}],
                })

        client = SeqClient()
        log = Log(client)
        got = []
        try:
            for entry in log.follow(interval=0):
                got.append(entry.seq)
        except KeyboardInterrupt:
            pass

        self.assertEqual(got, [1, 2, 3, 4])          # 不重复
        self.assertIsNone(client.sinces[0])           # 首次不带 since
        self.assertEqual(client.sinces[1], 2)         # 之后 = 上次 seq + 1
        self.assertEqual(client.sinces[2], 3)


class TestLogCategoryConstants(unittest.TestCase):
    """常量必须与服务端 FBRunLogCategory* 一致"""

    def test_values_match_server(self):
        self.assertEqual(LogCategory.HTTP, "http")
        self.assertEqual(LogCategory.EXCEPTION, "exception")
        self.assertEqual(LogCategory.MJPEG, "mjpeg")
        self.assertEqual(LogCategory.SOCKET, "socket")
        self.assertEqual(LogCategory.STDERR, "stderr")
        self.assertEqual(LogCategory.CRASH, "crash")
        self.assertEqual(LogCategory.LIFECYCLE, "lifecycle")

    def test_level_values_match_server(self):
        self.assertEqual(LogLevel.ALL,
                         ("debug", "info", "warn", "error", "fatal"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
