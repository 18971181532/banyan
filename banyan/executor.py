"""执行器：火山模型（逐行拉取的生成器）。

每个计划节点对应一个生成器，父节点向子节点要一行，子节点算出一行就交出来。
这样做的好处是不用把中间结果物化——`LIMIT 10` 只真的算 10 行。缺点是每一行
都要过一层 Python 函数调用，这也正是纯 Python 引擎的固有开销。

只有三个算子必须把输入吃干净：排序、哈希连接（建表那一侧）、聚合。
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import Dict, Iterator, List, Sequence, Set, Tuple

from .errors import EvalError
from .expr import Expr
from .planner import (
    Aggregate, Distinct, Filter, Join, Limit, Plan, Project, Scan, SingleRow, Sort,
)
from .values import Value, compare, truthy

Row = Tuple[Value, ...]


class Runtime:
    """一次查询的运行时状态：只统计"每个算子产出了多少行"。"""

    __slots__ = ("rows",)

    def __init__(self) -> None:
        self.rows: Dict[int, int] = {}

    def produced(self, plan: Plan) -> int:
        return self.rows.get(id(plan), 0)

    def count(self, plan: Plan) -> None:
        key = id(plan)
        self.rows[key] = self.rows.get(key, 0) + 1


def execute(plan: Plan, runtime: Runtime) -> Iterator[Row]:
    """按计划节点类型分派到具体生成器。"""
    handler = _HANDLERS.get(type(plan))
    if handler is None:  # pragma: no cover - 计划器不会造出别的类型
        raise EvalError(f"无法执行 {type(plan).__name__}")
    return handler(plan, runtime)


# ---------------------------------------------------------------------------
# 各算子
# ---------------------------------------------------------------------------
def _scan(plan: Scan, runtime: Runtime) -> Iterator[Row]:
    for row in plan.table.rows:
        runtime.count(plan)
        yield row


def _single_row(plan: SingleRow, runtime: Runtime) -> Iterator[Row]:
    runtime.count(plan)
    yield ()


def _filter(plan: Filter, runtime: Runtime) -> Iterator[Row]:
    predicates = plan.predicates
    for row in execute(plan.children[0], runtime):
        keep = True
        for predicate in predicates:
            if not _truth(predicate.eval(row)):
                keep = False
                break
        if keep:
            runtime.count(plan)
            yield row


def _project(plan: Project, runtime: Runtime) -> Iterator[Row]:
    items = plan.items + plan.hidden
    for row in execute(plan.children[0], runtime):
        runtime.count(plan)
        yield tuple(expr.eval(row) for expr, _ in items)


def _join(plan: Join, runtime: Runtime) -> Iterator[Row]:
    if plan.strategy == "hash":
        yield from _hash_join(plan, runtime)
    else:
        yield from _loop_join(plan, runtime)


def _hash_join(plan: Join, runtime: Runtime) -> Iterator[Row]:
    left, right = plan.children
    build_plan, probe_plan = (left, right) if plan.build_left else (right, left)
    # 键要按各自输入行的下标来取，不能用拼接后的下标
    left_keys = [left_index for left_index, _, _ in plan.keys]
    right_keys = [right_index for _, right_index, _ in plan.keys]
    if plan.build_left:
        build_keys, probe_keys = left_keys, right_keys
    else:
        build_keys, probe_keys = right_keys, left_keys

    table: Dict[Tuple[Value, ...], List[Row]] = {}
    for row in execute(build_plan, runtime):
        key = tuple(row[index] for index in build_keys)
        if any(value is None for value in key):
            continue  # NULL 不参与等值匹配
        table.setdefault(key, []).append(row)

    width = right.width
    for row in execute(probe_plan, runtime):
        key = tuple(row[index] for index in probe_keys)
        matches = None if any(value is None for value in key) else table.get(key)
        emitted = False
        if matches:
            for other in matches:
                # 输出列序必须永远是"左表在前、右表在后"，与哪边建表无关
                joined = (other + row) if plan.build_left else (row + other)
                if _passes(plan.residual, joined):
                    emitted = True
                    runtime.count(plan)
                    yield joined
        if not emitted and plan.join_kind == "left":
            # 没找到匹配（或者匹配都被 ON 的附加条件滤掉了），补一整行 NULL
            runtime.count(plan)
            yield row + (None,) * width


def _loop_join(plan: Join, runtime: Runtime) -> Iterator[Row]:
    left, right = plan.children
    if plan.join_kind == "left":
        # 左连接必须按左表驱动，右侧行要能反复扫描
        right_rows = list(execute(right, runtime))
        for row in execute(left, runtime):
            matched = False
            for other in right_rows:
                joined = row + other
                if _passes(plan.predicates, joined):
                    matched = True
                    runtime.count(plan)
                    yield joined
            if not matched:
                runtime.count(plan)
                yield row + (None,) * right.width
        return
    right_rows = list(execute(right, runtime))
    for row in execute(left, runtime):
        for other in right_rows:
            joined = row + other
            if _passes(plan.predicates, joined):
                runtime.count(plan)
                yield joined


def _aggregate(plan: Aggregate, runtime: Runtime) -> Iterator[Row]:
    width = plan.children[0].width
    groups: Dict[Tuple[Value, ...], List] = {}
    order: List[Tuple[Value, ...]] = []

    for row in execute(plan.children[0], runtime):
        key = tuple(expr.eval(row) for expr in plan.group_exprs)
        state = groups.get(key)
        if state is None:
            state = [_new_accumulator(spec) for spec in plan.specs]
            state.append(row)
            groups[key] = state
            order.append(key)
        _accumulate(state, plan.specs, row)

    if not plan.group_exprs and not order:
        # 没有 GROUP BY 时，空输入也要产出一行：COUNT 是 0，SUM 是 NULL
        empty = [_new_accumulator(spec) for spec in plan.specs]
        empty.append((None,) * width)
        runtime.count(plan)
        yield tuple(empty[-1]) + tuple(_finish(item) for item in empty[:-1])
        return

    for key in order:
        state = groups[key]
        runtime.count(plan)
        yield tuple(state[-1]) + tuple(_finish(item) for item in state[:-1])


class _Counter:
    __slots__ = ("name", "distinct", "value", "seen")

    def __init__(self, name: str, distinct: bool) -> None:
        self.name = name
        self.distinct = distinct
        self.value = 0
        self.seen: Set[Value] = set()


class _Summer:
    __slots__ = ("name", "total", "count", "seen")

    def __init__(self, name: str, distinct: bool) -> None:
        self.name = name
        self.total = None
        self.count = 0
        self.seen: Set[Value] = set() if distinct else None


class _Extreme:
    __slots__ = ("name", "best", "has", "seen")

    def __init__(self, name: str) -> None:
        self.name = name
        self.best: Value = None
        self.has = False
        self.seen = None


def _new_accumulator(spec):
    if spec.name == "COUNT":
        return _Counter("COUNT", spec.distinct)
    if spec.name in ("SUM", "AVG"):
        return _Summer(spec.name, spec.distinct)
    return _Extreme(spec.name)


def _accumulate(state: List, specs, row: Row) -> None:
    for index, spec in enumerate(specs):
        accumulator = state[index]
        if spec.star:
            accumulator.value += 1
            continue
        value = spec.arg.eval(row)
        if isinstance(accumulator, _Counter):
            if value is None:
                continue
            if accumulator.distinct:
                if value in accumulator.seen:
                    continue
                accumulator.seen.add(value)
            accumulator.value += 1
        elif isinstance(accumulator, _Summer):
            if value is None:
                continue
            if accumulator.seen is not None:
                if value in accumulator.seen:
                    continue
                accumulator.seen.add(value)
            accumulator.total = value if accumulator.total is None else accumulator.total + value
            accumulator.count += 1
        else:
            if value is None:
                continue
            if not accumulator.has:
                accumulator.best = value
                accumulator.has = True
            else:
                order = compare(value, accumulator.best)
                if order is None:
                    continue
                if (spec.name == "MIN" and order < 0) or (spec.name == "MAX" and order > 0):
                    accumulator.best = value


def _finish(accumulator):
    if isinstance(accumulator, _Counter):
        return accumulator.value
    if isinstance(accumulator, _Summer):
        if accumulator.count == 0:
            return None
        if accumulator.name == "AVG":
            return accumulator.total / accumulator.count
        return accumulator.total
    return accumulator.best


def _distinct(plan: Distinct, runtime: Runtime) -> Iterator[Row]:
    seen: Set[Row] = set()
    for row in execute(plan.children[0], runtime):
        if row in seen:
            continue
        seen.add(row)
        runtime.count(plan)
        yield row


def _compare_rows(left: Row, right: Row, keys) -> int:
    for index, descending in keys:
        a, b = left[index], right[index]
        if a is None and b is None:
            continue
        if a is None:            # NULL 排在最前（ASC 时）
            return 1 if descending else -1
        if b is None:
            return -1 if descending else 1
        result = compare(a, b)
        if result:
            return -result if descending else result
    return 0


def _sort(plan: Sort, runtime: Runtime) -> Iterator[Row]:
    rows = list(execute(plan.children[0], runtime))
    keys = [(expr.index, descending) for expr, descending in plan.keys]
    rows.sort(key=cmp_to_key(lambda a, b: _compare_rows(a, b, keys)))
    for row in rows:
        runtime.count(plan)
        yield row


def _limit(plan: Limit, runtime: Runtime) -> Iterator[Row]:
    """注意这里不用 `for row in child`：那样会多拉一行。

    `LIMIT 10` 就该只算 10 行——这是火山模型的卖点，实现得对得起这个说法。
    """
    source = execute(plan.children[0], runtime)
    skipped = 0
    while skipped < plan.offset:
        if next(source, None) is None:
            return
        skipped += 1
    emitted = 0
    while plan.limit is None or emitted < plan.limit:
        row = next(source, None)
        if row is None:
            return
        emitted += 1
        runtime.count(plan)
        yield row


_HANDLERS = {
    Scan: _scan,
    SingleRow: _single_row,
    Filter: _filter,
    Project: _project,
    Join: _join,
    Aggregate: _aggregate,
    Distinct: _distinct,
    Sort: _sort,
    Limit: _limit,
}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _truth(value: Value) -> bool:
    """WHERE / ON 里的真值：NULL 一律不成立。"""
    return truthy(value)


def _passes(predicates: Sequence[Expr], row: Row) -> bool:
    for predicate in predicates:
        if not _truth(predicate.eval(row)):
            return False
    return True


def collect(plan: Plan, runtime: Runtime) -> Tuple[List[str], List[Row]]:
    """跑完整个计划，把每个算子的真实行数写回计划节点。"""
    rows = list(execute(plan, runtime))
    for node in plan.walk():
        node.actual_rows = runtime.produced(node)
    visible = plan.visible or len(plan.output_columns)
    if visible and len(rows) and len(rows[0]) > visible:
        rows = [row[:visible] for row in rows]
    return list(plan.output_columns), rows
