"""值模型：NULL、整数、浮点、文本。

只有四种值，且 NULL 用 Python 的 ``None`` 表示。三值逻辑（true / false /
unknown）是这里最容易写错的地方，所以比较、布尔运算都单独实现，不依赖
Python 自己的真值判断。
"""

from __future__ import annotations

import math
from typing import Any, Optional

Value = Optional[Any]

INT = "INT"
FLOAT = "FLOAT"
TEXT = "TEXT"
BOOL = "BOOL"
NULLTYPE = "NULL"


def type_of(value: Value) -> str:
    if value is None:
        return NULLTYPE
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, int):
        return INT
    if isinstance(value, float):
        return FLOAT
    return TEXT


def is_number(value: Value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def to_number(value: Value, what: str = "值") -> float | int:
    """算术上下文里的隐式转换：只接受数字和能读成数字的文本。"""
    if value is None:
        raise ValueError("NULL")
    if is_number(value):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
    from .errors import EvalError

    raise EvalError(f"{what} {value!r} 不是数字")


def truthy(value: Value) -> bool:
    """WHERE / ON / CASE WHEN 用的真值判断。NULL 一律当作不成立。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if is_number(value):
        return value != 0
    return True


def compare(left: Value, right: Value) -> Optional[int]:
    """三值比较：任意一边是 NULL 就返回 None（unknown）。"""
    if left is None or right is None:
        return None
    if is_number(left) and is_number(right):
        if left == right:
            return 0
        return -1 if left < right else 1
    if isinstance(left, str) and isinstance(right, str):
        if left == right:
            return 0
        return -1 if left < right else 1
    if isinstance(left, bool) and isinstance(right, bool):
        return 0 if left == right else (-1 if not left else 1)
    from .errors import EvalError

    raise EvalError(f"无法比较 {left!r} 与 {right!r}")


def equals(left: Value, right: Value) -> Optional[bool]:
    result = compare(left, right)
    return None if result is None else result == 0


def format_value(value: Value, null_text: str = "NULL") -> str:
    if value is None:
        return null_text
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        if value == int(value) and abs(value) < 1e16:
            return f"{value:.1f}"
        return f"{value:.6g}"
    return str(value)


def infer_literal(text: str) -> Value:
    """CSV 单元格 → 值。整数优先，其次浮点，都不行就当文本。"""
    if text == "":
        return None
    stripped = text.strip()
    if stripped == "":
        return text
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return text


def common_type(values: list[Value]) -> str:
    """一列值推断出列类型。"""
    seen = set()
    for value in values:
        if value is None:
            continue
        kind = type_of(value)
        if kind == BOOL:
            kind = INT
        seen.add(kind)
    if not seen:
        return TEXT
    if seen == {INT}:
        return INT
    if seen <= {INT, FLOAT}:
        return FLOAT
    return TEXT


def cast_to(value: Value, kind: str) -> Value:
    """CSV 加载后按列类型统一：全是整数但混进浮点，就把整数升成浮点。"""
    if value is None:
        return None
    if kind == FLOAT and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value
