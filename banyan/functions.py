"""函数库：标量函数与聚合函数。

标量函数在绑定阶段解析成可调用对象，运行期只做值变换；聚合函数不在这里
执行，而是由聚合算子按组累积——两者混在一起会让执行器写得很难看。
"""

from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple

from .errors import BindError, EvalError
from .values import Value, is_number, to_number

AGGREGATES = ("COUNT", "SUM", "AVG", "MIN", "MAX")


def is_aggregate(name: str) -> bool:
    return name.upper() in AGGREGATES


@dataclass(frozen=True)
class Scalar:
    name: str
    fn: Callable
    min_args: int
    max_args: Optional[int]
    summary: str
    signature: str


def _require_text(value: Value, func: str) -> str:
    if value is None:
        raise ValueError("NULL")
    if isinstance(value, str):
        return value
    if is_number(value):
        return str(value)
    raise EvalError(f"{func} 需要文本参数")


def _null_safe(fn):
    """任何一个参数为 NULL 就返回 NULL，省掉每个函数里的重复判断。"""

    @functools.wraps(fn)
    def wrapper(*args):
        if any(arg is None for arg in args):
            return None
        return fn(*args)

    return wrapper


@_null_safe
def _abs(value):
    return abs(to_number(value))


@_null_safe
def _round(value, digits=0):
    digits = int(to_number(digits))
    factor = 10 ** digits
    scaled = to_number(value) * factor
    # 加一个极小量抵消 2.675 这类数的二进制误差
    return math.floor(scaled + 0.5 + 1e-9) / factor if scaled >= 0 else -math.floor(-scaled + 0.5 + 1e-9) / factor


@_null_safe
def _ceil(value):
    return math.ceil(to_number(value))


@_null_safe
def _floor(value):
    return math.floor(to_number(value))


@_null_safe
def _upper(value):
    return _require_text(value, "UPPER").upper()


@_null_safe
def _lower(value):
    return _require_text(value, "LOWER").lower()


@_null_safe
def _length(value):
    return len(_require_text(value, "LENGTH"))


@_null_safe
def _trim(value):
    return _require_text(value, "TRIM").strip()


@_null_safe
def _ltrim(value):
    return _require_text(value, "LTRIM").lstrip()


@_null_safe
def _rtrim(value):
    return _require_text(value, "RTRIM").rstrip()


@_null_safe
def _substr(value, start, length=None):
    text = _require_text(value, "SUBSTR")
    begin = int(to_number(start))
    # SQL 的下标从 1 开始，且允许负数从尾部数
    index = begin - 1 if begin > 0 else len(text) + begin
    if length is None:
        return text[max(index, 0):]
    count = int(to_number(length))
    if count < 0:
        raise EvalError("SUBSTR 的长度不能是负数")
    return text[max(index, 0):max(index, 0) + count]


@_null_safe
def _left(value, count):
    text = _require_text(value, "LEFT")
    return text[:max(int(to_number(count)), 0)]


@_null_safe
def _right(value, count):
    text = _require_text(value, "RIGHT")
    size = max(int(to_number(count)), 0)
    return text[-size:] if size else ""


@_null_safe
def _replace(value, old, new):
    return _require_text(value, "REPLACE").replace(
        _require_text(old, "REPLACE"), _require_text(new, "REPLACE")
    )


@_null_safe
def _concat(*args):
    return "".join(_require_text(arg, "CONCAT") for arg in args)


def _coalesce(*args):
    for arg in args:
        if arg is not None:
            return arg
    return None


@_null_safe
def _nullif(left, right):
    from .values import equals

    return None if equals(left, right) else left


@_null_safe
def _greatest(*args):
    best = args[0]
    for arg in args[1:]:
        from .values import compare

        if compare(arg, best) == 1:
            best = arg
    return best


@_null_safe
def _least(*args):
    best = args[0]
    for arg in args[1:]:
        from .values import compare

        if compare(arg, best) == -1:
            best = arg
    return best


SCALARS: Dict[str, Scalar] = {}


def _register(name, min_args, max_args, summary, signature, fn):
    SCALARS[name] = Scalar(name, fn, min_args, max_args, summary, signature)


_register("ABS", 1, 1, "绝对值", "ABS(数字)", _abs)
_register("ROUND", 1, 2, "四舍五入到指定小数位", "ROUND(数字, [位数])", _round)
_register("CEIL", 1, 1, "向上取整", "CEIL(数字)", _ceil)
_register("FLOOR", 1, 1, "向下取整", "FLOOR(数字)", _floor)
_register("UPPER", 1, 1, "转大写", "UPPER(文本)", _upper)
_register("LOWER", 1, 1, "转小写", "LOWER(文本)", _lower)
_register("LENGTH", 1, 1, "字符个数", "LENGTH(文本)", _length)
_register("TRIM", 1, 1, "去掉两端空白", "TRIM(文本)", _trim)
_register("LTRIM", 1, 1, "去掉左端空白", "LTRIM(文本)", _ltrim)
_register("RTRIM", 1, 1, "去掉右端空白", "RTRIM(文本)", _rtrim)
_register("SUBSTR", 2, 3, "截取子串，下标从 1 开始", "SUBSTR(文本, 起点, [长度])", _substr)
_register("LEFT", 2, 2, "取左边 n 个字符", "LEFT(文本, n)", _left)
_register("RIGHT", 2, 2, "取右边 n 个字符", "RIGHT(文本, n)", _right)
_register("REPLACE", 3, 3, "替换所有匹配的子串", "REPLACE(文本, 旧, 新)", _replace)
_register("CONCAT", 1, None, "拼接文本", "CONCAT(文本, ...)", _concat)
_register("COALESCE", 1, None, "返回第一个非 NULL 参数", "COALESCE(值, ...)", _coalesce)
_register("NULLIF", 2, 2, "两值相等时返回 NULL", "NULLIF(a, b)", _nullif)
_register("GREATEST", 1, None, "最大值", "GREATEST(值, ...)", _greatest)
_register("LEAST", 1, None, "最小值", "LEAST(值, ...)", _least)


def lookup_scalar(name: str) -> Optional[Scalar]:
    return SCALARS.get(name.upper())


def check_scalar(name: str, argc: int) -> Scalar:
    spec = lookup_scalar(name)
    if spec is None:
        raise BindError(f"没有这个函数：{name}")
    if argc < spec.min_args or (spec.max_args is not None and argc > spec.max_args):
        raise BindError(f"{name} 的参数个数不对，应该写成 {spec.signature}")
    return spec


def check_aggregate(name: str, argc: int, star: bool) -> None:
    upper = name.upper()
    if star and upper != "COUNT":
        raise BindError(f"只有 COUNT 支持 *，{name}(*) 不合法")
    if not star and argc != 1:
        raise BindError(f"{name} 需要且只需要一个参数")
    if argc > 1:
        raise BindError(f"{name} 需要且只需要一个参数")


@functools.lru_cache(maxsize=256)
def like_regex(pattern: str) -> re.Pattern:
    """把 LIKE 模式翻译成正则：% 匹配任意长度，_ 匹配一个字符。"""
    parts = ["^"]
    for char in pattern:
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    parts.append("$")
    return re.compile("".join(parts), re.DOTALL)


def match_like(value: Value, pattern: Value) -> Optional[bool]:
    if value is None or pattern is None:
        return None
    return bool(like_regex(str(pattern)).match(str(value)))


def catalogue() -> Tuple[Sequence[Scalar], Sequence[str]]:
    return tuple(sorted(SCALARS.values(), key=lambda item: item.name)), AGGREGATES
