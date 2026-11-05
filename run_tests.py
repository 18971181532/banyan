#!/usr/bin/env python3
"""跑全部测试，并输出一行中文汇总。

`python -m unittest discover` 的输出够用，但 Windows 控制台遇到中文容易炸，
所以这里先切 UTF-8，再把结果收成一行 "N 个测试 · X 秒 · 失败 · 错误"。
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from banyan.console import GREEN, RED, enable_utf8, paint  # noqa: E402


def main() -> int:
    enable_utf8()
    suite = unittest.defaultTestLoader.discover(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"))
    runner = unittest.TextTestRunner(verbosity=1, buffer=True)
    start = time.perf_counter()
    result = runner.run(suite)
    elapsed = time.perf_counter() - start

    total = result.testsRun
    failures = len(result.failures)
    errors = len(result.errors)
    skipped = len(result.skipped)
    summary = f"{total} 个测试 · {elapsed:.2f}s · {failures} 失败 · {errors} 错误"
    if skipped:
        summary += f" · {skipped} 跳过"
    print()
    if result.wasSuccessful():
        print(paint(summary, GREEN))
        return 0
    print(paint(summary, RED))
    return 1


if __name__ == "__main__":
    sys.exit(main())
