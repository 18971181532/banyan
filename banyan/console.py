"""终端输出：等宽对齐与颜色。

中文在终端里占两格，`len()` 只算一格，直接用 `ljust` 排出来的表格会歪。
所以这里自己算显示宽度：东亚宽字符（W/F）计 2，其余计 1。
"""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import Iterable, Optional, Sequence

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
GREY = "\033[90m"


def enable_utf8() -> None:
    """Windows 控制台默认是 cp936/cp1252，中文输出会直接抛异常。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 少数被重定向的场景
                pass


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def paint(text: str, color: str, enabled: Optional[bool] = None) -> str:
    if enabled is None:
        enabled = supports_color()
    return f"{color}{text}{RESET}" if enabled else text


def display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    if limit <= 0:
        return ""
    if display_width(text) <= limit:
        return text
    target = limit - display_width(ellipsis)
    kept = []
    width = 0
    for char in text:
        step = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if width + step > target:
            break
        kept.append(char)
        width += step
    return "".join(kept) + ellipsis


def pad(text: str, limit: int, align: str = "left") -> str:
    text = truncate(text, limit)
    space = max(0, limit - display_width(text))
    if align == "right":
        return " " * space + text
    if align == "center":
        left = space // 2
        return " " * left + text + " " * (space - left)
    return text + " " * space


def rule(width: int, char: str = "─") -> str:
    return char * max(width, 0)


def table(headers: Sequence[str], rows: Iterable[Sequence[object]],
          aligns: Optional[Sequence[str]] = None,
          indent: str = "  ", max_width: Optional[int] = None) -> str:
    """画一张等宽表；列宽按显示宽度计算。"""
    body = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
    count = len(headers)
    aligns = list(aligns or ["left"] * count)
    widths = [display_width(headers[index]) for index in range(count)]
    for row in body:
        for index in range(min(count, len(row))):
            widths[index] = max(widths[index], display_width(row[index]))

    if max_width is not None:
        # 整体超宽时，按比例压缩最宽的几列
        while sum(widths) + 2 * (count - 1) + display_width(indent) > max_width:
            widest = max(range(count), key=lambda index: widths[index])
            if widths[widest] <= 6:
                break
            widths[widest] -= 1

    lines = [indent + "  ".join(pad(headers[index], widths[index], aligns[index])
                                for index in range(count))]
    lines.append(indent + rule(sum(widths) + 2 * (count - 1)))
    for row in body:
        cells = []
        for index in range(count):
            value = row[index] if index < len(row) else ""
            cells.append(pad(value, widths[index], aligns[index]))
        lines.append(indent + "  ".join(cells))
    return "\n".join(lines)
