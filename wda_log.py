#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""拉取设备上的 WDA 运行日志（/wda/log/*）。

排查"端口还在但没反应""跑一小时整个 WDA 掉了"这类问题时，先看 WDA 自己记了什么。
这些路由不需要 session、且绕开共享路由队列，所以 WDA 卡住时通常还能问到。

用法
----
    # 默认：打一份计数器总览 + 最近 50 条
    python wda_log.py 00008030-000D65402EF8202E

    # 只看 error 及以上，最多 200 条
    python wda_log.py <UDID> --errors 200

    # 只看某一分类（http / exception / mjpeg / socket / stderr / crash / lifecycle）
    python wda_log.py <UDID> --category exception

    # 上一次会话的崩溃报告（看完顺带在设备上清掉）
    python wda_log.py <UDID> --crash --clear-crash

    # 实时跟随（Ctrl-C 退出）
    python wda_log.py <UDID> --follow --level warn

    # 纯文本落盘（默认 5000 条）
    python wda_log.py <UDID> --save wda-run.log --limit 5000

    # 调整环形缓冲容量 / 打开 stderr 捕获（不给参数就是只查询）
    python wda_log.py <UDID> --config --capacity 5000 --stderr on

    # 走 WiFi 或已建好的转发端口
    python wda_log.py http://127.0.0.1:8100

退出码：0 正常；2 设备上的 WDA 不支持 /wda/log/*；3 其它错误。
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import wdap


def build_url(target: str) -> str:
    if target.startswith(("http://", "https://", "http+usbmux://")):
        return target
    return "http+usbmux://%s:8100" % target


def _print_entries(snapshot: "wdap.LogSnapshot") -> None:
    for entry in snapshot.entries:
        print("%s %s [%s] %s" % (entry.time, entry.level.upper(),
                                 entry.category, entry.message))


def _print_stats(stats: "wdap.LogStats") -> None:
    http = stats.http
    log = stats.log
    mjpeg = stats.mjpeg
    print("运行时间      : %.0f 秒" % stats.runner_uptime_seconds)
    print("日志缓冲      : %s/%s（累计记录 %s，因重复被抑制 %s）" % (
        log.get("stored", 0), log.get("capacity", 0),
        log.get("totalRecorded", 0), log.get("suppressedEntries", 0)))
    print("HTTP 请求     : %s 次，非 2xx 响应 %s，慢请求 %s" % (
        http.get("requests", 0), http.get("nonSuccessResponses", 0),
        http.get("slowRequests", 0)))
    print("最慢请求      : %.2fs  %s" % (
        http.get("slowestRequestSeconds", 0.0), http.get("slowestRequest", "")))
    print("MJPEG 异常    : %s（连续截图失败峰值 %s）" % (
        mjpeg.get("exceptions", 0), mjpeg.get("screenshotFailurePeak", 0)))
    print("监听器重建    : %s" % stats.listener_restarts)
    if stats.exceptions_by_name:
        print("异常分类      : %s" % ", ".join(
            "%s=%s" % (k, v) for k, v in stats.exceptions_by_name.items()))
    print("stderr 捕获   : requested=%s active=%s（已捕获 %s 行，超长丢弃 %s 行）" % (
        stats.capture.get("stderrRequested"),
        stats.capture.get("stderrActive"),
        stats.capture.get("capturedLines", 0),
        stats.capture.get("oversizedLines", 0)))
    print("上次崩溃      : %s（报告 %s 字节，路径 %s）" % (
        "是" if stats.previous_session_crashed else "否",
        stats.crash.get("previousSessionReportBytes", 0),
        stats.crash.get("reportPath", "")))


def main() -> int:
    parser = argparse.ArgumentParser(description="拉取 WDA 运行日志")
    parser.add_argument("target", help="设备 UDID 或完整 WDA URL")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多返回几条，1..20000（默认 recent/errors=200，save=5000）")
    parser.add_argument("--level", default=None,
                        help="最低级别：debug/info/warn/error/fatal")
    parser.add_argument("--category", default=None,
                        help="分类过滤：http/exception/mjpeg/socket/stderr/crash/lifecycle")
    parser.add_argument("--errors", type=int, nargs="?", const=200, default=None,
                        help="只看 error 及以上")
    parser.add_argument("--stats", action="store_true", help="只打计数器总览")
    parser.add_argument("--crash", action="store_true", help="打上一次会话的崩溃报告")
    parser.add_argument("--clear-crash", action="store_true",
                        help="取完崩溃报告后顺带在设备上清掉")
    parser.add_argument("--follow", action="store_true", help="实时跟随（Ctrl-C 退出）")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="--follow 的轮询间隔秒，默认 1.0")
    parser.add_argument("--save", metavar="PATH", default=None, help="纯文本日志落盘")
    parser.add_argument("--config", action="store_true", help="读取/修改日志配置")
    parser.add_argument("--capacity", type=int, default=None, help="配合 --config")
    parser.add_argument("--stderr", choices=("on", "off"), default=None,
                        help="配合 --config，开关 stderr 捕获")
    parser.add_argument("--clear", action="store_true", help="清空内存中的日志条目")
    args = parser.parse_args()

    try:
        client = wdap.Client(build_url(args.target))
    except Exception as err:  # noqa: BLE001
        print("连接失败: %s" % err)
        return 3

    log = client.log
    try:
        if not log.available():
            print("这台设备上的 WDA 没有注册 /wda/log/* 路由。\n"
                  "需要用带运行日志支持的源码（wda_cv_vision）重新编译并部署 WDA。")
            return 2

        if args.config:
            cfg = log.config(
                capacity=args.capacity,
                stderr_capture=(None if args.stderr is None else args.stderr == "on"))
            print("容量 %s，当前存 %s 条，stderr 捕获 requested=%s active=%s"
                  % (cfg.capacity, cfg.stored_entries,
                     cfg.stderr_capture_requested, cfg.stderr_capture_active))
            return 0

        if args.clear:
            log.clear()
            print("已清空内存中的日志条目")
            return 0

        if args.crash:
            rep = log.crash(clear=args.clear_crash)
            if not rep.previous_session_crashed:
                print("上一次会话没有记录到崩溃")
                return 0
            print("上一次会话崩溃，报告 %s 字节（路径 %s）：\n"
                  % (rep.report_bytes, rep.report_path))
            print(rep.report)
            return 0

        if args.save:
            path = log.save(args.save, limit=args.limit or 5000, level=args.level)
            print("已写入 %s" % path)
            return 0

        _print_stats(log.stats())
        print("")

        if args.stats:
            return 0

        if args.follow:
            try:
                for entry in log.follow(interval=args.interval, level=args.level,
                                        category=args.category, limit=args.limit):
                    print(entry.format())
            except KeyboardInterrupt:
                pass
            return 0

        if args.errors is not None:
            _print_entries(log.errors(limit=args.limit or args.errors,
                                      category=args.category))
        else:
            _print_entries(log.recent(limit=args.limit or 50, level=args.level,
                                      category=args.category))
        return 0
    except wdap.LogUnsupportedError as err:
        print(err)
        return 2
    except ValueError as err:
        print("参数错误: %s" % err)
        return 3
    except Exception as err:  # noqa: BLE001
        print("执行出错: %s: %s" % (type(err).__name__, err))
        return 3


if __name__ == "__main__":
    sys.exit(main())
