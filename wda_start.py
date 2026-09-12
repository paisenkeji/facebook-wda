#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按 iOS 版本选择后端拉起 WDA（等价于 ``python -m wdap.wda_launch``，但不会有 runpy 警告）

用法::

    python wda_start.py <UDID>                      # 自动判断 iOS 版本选后端
    python wda_start.py <UDID> --strategy tidevice  # 强制 tidevice（iOS 16 及以下）
    python wda_start.py <UDID> --strategy goios     # 强制 go-ios（iOS 17 及以上）
    python wda_start.py <UDID> --mount-image        # 拉起前先 ios image auto
    python wda_start.py <UDID> --no-tunnel          # 不自动起 go-ios tunnel（自己管理）
    python wda_start.py <UDID> --tunnel-mode userspace   # Windows 无管理员时用
    python wda_start.py <UDID> --port 8123 --bundle-id com.demo.xctrunner
"""

from wdap.wda_launch import main

if __name__ == "__main__":
    raise SystemExit(main())
