"""选择率与代价估算。

优化器的全部"聪明"都建立在这里：一个谓词能过滤掉多少行、一次连接会产出
多少行、每种算子的相对代价。数字不准，连接顺序就会选错，而且错得很安静。
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

from .catalog import ColumnStats
from .expr import (
    BetweenExpr, BinaryOp, Call, CaseExpr, Const, Expr, InExpr, IsNullExpr,
    LikeExpr, UnaryOp,
)
from .values import Value, is_number

# 代价常量：单位是"处理一行"的相对开销
SEQ_SCAN_ROW = 1.0
FILTER_ROW = 0.15
HASH_BUILD_ROW = 1.4
HASH_PROBE_ROW = 0.9
NL_OUTER_ROW = 0.6
NL_INNER_ROW = 0.35
SORT_ROW = 0.9
AGG_ROW = 0.7
PROJECT_ROW = 0.08

# 没有统计时的兜底选择率
DEFAULT_SELECTIVITY = 0.33
LIKE_PREFIX_SELECTIVITY = 0.10
LIKE_CONTAINS_SELECTIVITY = 0.25
IN_SELECTIVITY_PER_ITEM = 0.08


def stats_for_column(stats: Sequence[Optional[ColumnStats]], index: int) -> Optional[ColumnStats]:
    if 0 <= index < len(stats):
        return stats[index]
    return None


def selectivity(expr: Expr, column_stats: Sequence[Optional[ColumnStats]], rows: float,
                null_fractions: Optional[Sequence[float]] = None) -> float:
    """估算一个布尔的过滤条件能留下多少比例的行。"""
    if isinstance(expr, Const):
        return 1.0 if expr.value else 0.0

    if isinstance(expr, UnaryOp) and expr.op == "NOT":
        return max(0.0, 1.0 - selectivity(expr.operand, column_stats, rows, null_fractions))

    if isinstance(expr, BinaryOp) and expr.op == "AND":
        return (selectivity(expr.left, column_stats, rows, null_fractions)
                * selectivity(expr.right, column_stats, rows, null_fractions))

    if isinstance(expr, BinaryOp) and expr.op == "OR":
        left = selectivity(expr.left, column_stats, rows, null_fractions)
        right = selectivity(expr.right, column_stats, rows, null_fractions)
        return min(1.0, left + right - left * right)

    if isinstance(expr, BinaryOp) and expr.op in ("=", "<>", "<", "<=", ">", ">="):
        return _comparison_selectivity(expr, column_stats, rows, null_fractions)

    if isinstance(expr, IsNullExpr):
        base = _null_fraction(expr.operand, column_stats, null_fractions, rows)
        return max(0.0, 1.0 - base) if expr.negated else base

    if isinstance(expr, InExpr):
        column = _single_column(expr.operand)
        if column is not None:
            stats = stats_for_column(column_stats, column)
            if stats is not None and stats.distinct > 0:
                return min(1.0, len(expr.items) / max(stats.distinct, 1))
        return min(1.0, IN_SELECTIVITY_PER_ITEM * len(expr.items))

    if isinstance(expr, BetweenExpr):
        column = _single_column(expr.operand)
        if column is not None:
            stats = stats_for_column(column_stats, column)
            if stats is not None:
                low = expr.low.value if isinstance(expr.low, Const) else None
                high = expr.high.value if isinstance(expr.high, Const) else None
                return stats.range_selectivity(low, high, int(max(rows, 1)))
        return DEFAULT_SELECTIVITY

    if isinstance(expr, LikeExpr):
        if isinstance(expr.pattern, Const) and isinstance(expr.pattern.value, str):
            pattern = expr.pattern.value
            if pattern.startswith("%") or pattern.startswith("_"):
                return LIKE_CONTAINS_SELECTIVITY
            return LIKE_PREFIX_SELECTIVITY
        return LIKE_CONTAINS_SELECTIVITY

    if isinstance(expr, CaseExpr):
        return 0.5

    if isinstance(expr, Call):
        return 0.5

    return DEFAULT_SELECTIVITY


def _single_column(expr: Expr) -> Optional[int]:
    from .expr import Ref

    return expr.index if isinstance(expr, Ref) else None


def _null_fraction(expr: Expr, column_stats, null_fractions, rows: float = 0.0) -> float:
    index = _single_column(expr)
    if index is None:
        return 0.0
    if null_fractions is not None and index < len(null_fractions):
        return null_fractions[index]
    # 没传空值比例时，直接拿统计信息里的空值数除以行数
    stats = stats_for_column(column_stats, index)
    if stats is None or rows <= 0:
        return 0.0
    return min(1.0, stats.nulls / rows)


def _comparison_selectivity(expr: BinaryOp, column_stats, rows: float,
                            null_fractions) -> float:
    left, right = expr.left, expr.right
    column = _single_column(left)
    other = right
    flipped = False
    if column is None:
        column = _single_column(right)
        other = left
        flipped = True
    if column is None or not isinstance(other, Const):
        return DEFAULT_SELECTIVITY
    stats = stats_for_column(column_stats, column)
    if stats is None:
        return DEFAULT_SELECTIVITY

    op = expr.op
    if flipped:
        op = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(op, op)

    if op == "=":
        base = stats.equality_selectivity(other.value, int(max(rows, 1)))
        return base
    if op == "<>":
        return max(0.0, 1.0 - stats.equality_selectivity(other.value, int(max(rows, 1))))

    value = other.value
    if not is_number(value):
        return DEFAULT_SELECTIVITY
    if op in ("<", "<="):
        return stats.range_selectivity(None, value, int(max(rows, 1)))
    return stats.range_selectivity(value, None, int(max(rows, 1)))


def join_selectivity(left_stats: ColumnStats, right_stats: ColumnStats) -> float:
    """等值连接的选择率：1 / max(去重数)。"""
    left = max(left_stats.distinct, 1)
    right = max(right_stats.distinct, 1)
    return 1.0 / max(left, right)


def sort_cost(rows: float) -> float:
    if rows <= 1:
        return rows * SORT_ROW
    return rows * math.log2(rows) * SORT_ROW


def rows_after_filter(rows: float, factor: float) -> float:
    return max(1.0, rows * max(0.0, min(1.0, factor)))
