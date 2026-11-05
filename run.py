#!/usr/bin/env python3
"""开发入口：`python run.py <子命令>`，等价于 `python -m banyan`。

直接跑脚本时不会把包安装进环境，所以这里手动把项目根目录塞进 `sys.path`。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from banyan.cli import main  # noqa: E402  （必须放在 path 调整之后）

if __name__ == "__main__":
    sys.exit(main())
