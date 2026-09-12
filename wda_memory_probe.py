#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""采样 WDA 进程内存，验证 CV/Vision 轮询端点是否泄漏内存。

背景
----
wda_cv_vision 的 wait* 轮询端点（/wda/cv/waitForImage、/wda/cv/waitForColor、
/wda/vision/waitForText、/wda/cv/waitForStable）会在一个 while 循环里反复截图。
如果循环体没有独立的 autoreleasepool，每次迭代产生的全分辨率截图（1170x2532
PNG，解码后约 11.8MB）会一直堆在同一个自动释放池里，直到整个请求结束（最长
60 秒）才排空 —— 表现为 WDA 进程内存/CPU 无限增长，最后被系统杀掉，客户端
表现为连不上 / ConnectionResetError 10054，重启 WDA 才恢复。

用法
----
    # 只采样：每 1 秒读一次内存，共 60 次
    python wda_memory_probe.py 00008030-000D65402EF8202E

    # 压力测试：每次采样前先打一次 waitForStable（timeout=3s, interval=0.02s）
    python wda_memory_probe.py 00008030-000D65402EF8202E --stress 20

    # 指定完整 URL（比如走 WiFi 或已建好的转发端口）
    python wda_memory_probe.py http://127.0.0.1:8100 --stress 20

判定
----
- 只采样时 memoryMB 基本不动 -> WDA 空闲状态下是稳的。
- 加 --stress 后 memoryMB 单调上升且不回落 -> 服务端截图/图像缓冲没释放，
  就是上面的 autoreleasepool 问题（需要重新编译带修复的 WDA）。
- 服务端返回里没有 memoryFootprintMB 字段 -> 设备上的 WDA 是旧构建，
  没有本探针需要的 status 字段，只能先重新编译部署。
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

import wdap


def build_url(target: str) -> str:
    if target.startswith("http://") or target.startswith("https://"):
        return target
    if target.startswith("http+usbmux://"):
        return target
    return "http+usbmux://%s:8100" % target


def main() -> int:
    parser = argparse.ArgumentParser(description="采样 WDA 进程内存占用")
    parser.add_argument("target", help="设备 UDID 或完整 WDA URL")
    parser.add_argument("--samples", type=int, default=60, help="采样次数，默认 60")
    parser.add_argument("--interval", type=float, default=1.0, help="采样间隔秒，默认 1.0")
    parser.add_argument(
        "--stress",
        type=int,
        default=0,
        help="每次采样前调用 waitForStable 的次数（0 表示只采样不压测）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="压测时单次 waitForStable 的超时秒数，默认 3.0",
    )
    args = parser.parse_args()

    url = build_url(args.target)
    print("目标: %s" % url)
    client = wdap.Client(url)

    baseline: Optional[float] = None
    peak = 0.0
    supported = True

    for index in range(1, args.samples + 1):
        if args.stress > 0:
            for _ in range(args.stress):
                try:
                    client.http.post(
                        "/wda/cv/waitForStable",
                        {"timeout": args.timeout * 1000.0, "interval": 20.0},
                    )
                except Exception as err:  # noqa: BLE001 - 压测里任何错误都只记录
                    print("  [!] waitForStable 失败: %s" % type(err).__name__)
                    break

        try:
            status = client.cv.status()
        except Exception as err:  # noqa: BLE001
            print("  [!] 读取 /wda/cv/status 失败: %s: %s" % (type(err).__name__, err))
            return 2

        memory = status.memory_footprint_mb
        if memory <= 0.0:
            if supported:
                supported = False
                print("  [!] 服务端没有返回 memoryFootprintMB，设备上的 WDA 是旧构建，"
                      "请重新编译部署带该字段的版本后再测")
        if baseline is None:
            baseline = memory
        peak = max(peak, memory)
        print("[%3d] memoryMB=%8.2f  (基线 %8.2f, 峰值 %8.2f, 增量 %+8.2f)"
              % (index, memory, baseline, peak, memory - baseline))
        sys.stdout.flush()

        if index < args.samples:
            time.sleep(args.interval)

    if supported and baseline is not None:
        growth = peak - baseline
        print("\n结论: 基线 %.1fMB -> 峰值 %.1fMB，增量 %.1fMB" % (baseline, peak, growth))
        if growth > 150.0:
            print("  => 内存明显增长，服务端存在截图/图像缓冲未释放的问题")
        elif growth > 50.0:
            print("  => 有一定增长，可能是正常的缓存预热，建议多跑几轮确认")
        else:
            print("  => 内存基本平稳")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
