#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""USB 设备激活命令行入口（等价于 ``python -m wdap.activation``，但不会有 runpy 警告）

用法::

    python activate_ios.py --list                 # 列出 USB 设备
    python activate_ios.py --state <UDID>         # 查询激活状态
    python activate_ios.py <UDID>                 # 激活指定设备
    python activate_ios.py --all                  # 批量激活所有 USB 设备
    python activate_ios.py --all --proxy http://127.0.0.1:7890
"""

from wdap.activation import main

if __name__ == "__main__":
    raise SystemExit(main())
