"""逻辑计划、名字绑定与基于代价的优化。

一条 SQL 到这里会被拆成三层：

1. **绑定**：把 :mod:`banyan.ast` 里的名字换成 :mod:`banyan.expr` 里的行下标。
   名字一换成下标，后面所有改写就都不必担心"改完还对不对"。
2. **逻辑计划**：扫描、过滤、连接、聚合、排序、投影、去重、截断。
3. **优化**：谓词下推 + 连接重排。重排用的是经典的 left-deep 动态规划——
   枚举所有子集的最优计划，代价来自 :mod:`banyan.estimate`。

计划节点同时也是估算结果的载体：每个节点都带着自己的估计行数和累计代价，
执行器跑完再把真实行数填进去，于是 `explain` 能并排显示"估"和"实"。
"""

from __future__ import annotations

import itertools
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import ast
from .catalog import Column, ColumnStats, Database, Schema, Table
from .errors import BindError, PlanError
from .estimate import (
    AGG_ROW, DEFAULT_SELECTIVITY, FILTER_ROW, HASH_BUILD_ROW, HASH_PROBE_ROW,
    NL_INNER_ROW, NL_OUTER_ROW, PROJECT_ROW, SEQ_SCAN_ROW, SORT_ROW,
    join_selectivity, rows_after_filter, selectivity, sort_cost,
)
from .expr import (
    AggRef, BetweenExpr, BinaryOp, Call, CaseExpr, Const, Expr, InExpr,
    IsNullExpr, LikeExpr, Ref, UnaryOp, refs_of, simplify,
)
from .functions import check_aggregate, check_scalar, is_aggregate

# 动态规划最多枚举到 8 张表（2^8 个子集），再多就退回贪心
DP_LIMIT = 8


# ---------------------------------------------------------------------------
# 绑定
# ---------------------------------------------------------------------------
class AggCollector:
    """收集聚合调用，同一个聚合只算一次。

    `SELECT COUNT(*) ... HAVING COUNT(*) > 2` 里的两个 COUNT 共用同一个槽位，
    这样聚合算子只需要维护一份状态。
    """

    def __init__(self) -> None:
        self.specs: List["AggSpec"] = []
        self._slots: Dict[str, int] = {}

    def slot(self, node: ast.Function, arg: Optional[Expr]) -> int:
        key = f"{node.name.upper()}|{arg.render() if arg is not None else '*'}|{node.distinct}"
        if key in self._slots:
            return self._slots[key]
        check_aggregate(node.name, 0 if node.star else len(node.args), node.star)
        label = _aggregate_label(node, arg)
        self._slots[key] = len(self.specs)
        self.specs.append(AggSpec(node.name.upper(), arg, node.star, node.distinct, label))
        return self._slots[key]


class Binder:
    """把语法树表达式绑成可求值表达式。

    `agg_base` 是聚合前计划的列数：聚合结果统一排在它后面，这样"分组列保持
    原下标"，选择列表里同时出现分组列和聚合函数时不必做任何映射。
    """

    def __init__(self, schema: Schema, aggregates: Optional[AggCollector] = None,
                 agg_base: int = 0):
        self.schema = schema
        self.aggregates = aggregates
        self.agg_base = agg_base

    # -- 对外 -------------------------------------------------------------
    def bind(self, node: ast.Node) -> Expr:
        method = getattr(self, f"_bind_{type(node).__name__.lower()}", None)
        if method is None:
            raise BindError(f"不支持的表达式 {type(node).__name__}", sql=None)
        # 绑定完顺手做常量折叠：`WHERE x > 1 + 1` 里的加法不必留到执行期
        return simplify(method(node))

    # -- 各类节点 ---------------------------------------------------------
    def _bind_literal(self, node: ast.Literal) -> Expr:
        return Const(node.value)

    def _bind_column(self, node: ast.Column) -> Expr:
        index = self.schema.index_of(node.name, node.table)
        column = self.schema[index]
        name = column.qualified if node.table else column.name
        return Ref(index, name)

    def _bind_star(self, node: ast.Star) -> Expr:
        raise BindError("`*` 只能出现在选择列表里")

    def _bind_unary(self, node: ast.Unary) -> Expr:
        return UnaryOp(node.op, self.bind(node.operand))

    def _bind_binary(self, node: ast.Binary) -> Expr:
        return BinaryOp(node.op, self.bind(node.left), self.bind(node.right))

    def _bind_case(self, node: ast.Case) -> Expr:
        whens = tuple((self.bind(cond), self.bind(result)) for cond, result in node.whens)
        default = self.bind(node.default) if node.default is not None else None
        return CaseExpr(whens, default)

    def _bind_inlist(self, node: ast.InList) -> Expr:
        return InExpr(self.bind(node.operand), tuple(self.bind(item) for item in node.items),
                      node.negated)

    def _bind_between(self, node: ast.Between) -> Expr:
        return BetweenExpr(self.bind(node.operand), self.bind(node.low),
                           self.bind(node.high), node.negated)

    def _bind_like(self, node: ast.Like) -> Expr:
        return LikeExpr(self.bind(node.operand), self.bind(node.pattern), node.negated)

    def _bind_isnull(self, node: ast.IsNull) -> Expr:
        return IsNullExpr(self.bind(node.operand), node.negated)

    def _bind_function(self, node: ast.Function) -> Expr:
        if is_aggregate(node.name):
            if self.aggregates is None:
                raise BindError(f"{node.name.upper()} 不能出现在这里：当前上下文没有分组")
            if node.args and any(
                    isinstance(item, ast.Function) and is_aggregate(item.name) for item in node.args):
                raise BindError("聚合函数不能嵌套")
            arg = self.bind(node.args[0]) if node.args else None
            slot = self.aggregates.slot(node, arg)
            return Ref(self.agg_base + slot, _aggregate_label(node, arg))
        # 普通函数：先校验参数个数，再绑参数
        check_scalar(node.name, 0 if node.star else len(node.args))
        spec = check_scalar(node.name, 0 if node.star else len(node.args))
        if node.distinct:
            raise BindError(f"{node.name.upper()} 是标量函数，不能加 DISTINCT")
        args = tuple(self.bind(arg) for arg in node.args)
        return Call(node.name.upper(), spec, args)


def _aggregate_label(node: ast.Function, arg: Optional[Expr]) -> str:
    inner = "*" if node.star else (arg.render() if arg is not None else "")
    prefix = "DISTINCT " if node.distinct else ""
    return f"{node.name.upper()}({prefix}{inner})"


# ---------------------------------------------------------------------------
# 计划节点
# ---------------------------------------------------------------------------
class Plan:
    """所有计划节点的基类。

    `est_rows` / `est_cost` 由计划器填，`actual_rows` / `actual_ms` 由执行器填。
    两者都放在同一个对象上，`explain` 才能做"估算 vs 实际"的对照。
    """

    kind = "计划"

    def __init__(self, schema: Schema, stats: Sequence[Optional[ColumnStats]],
                 children: Sequence["Plan"] = ()) -> None:
        self.schema = schema
        self.stats: List[Optional[ColumnStats]] = list(stats)
        self.children: Tuple[Plan, ...] = tuple(children)
        self.est_rows: float = 0.0
        self.est_cost: float = 0.0
        self.actual_rows: int = 0
        self.actual_ms: float = 0.0
        # 最外层计划才有意义：最终输出的列名，以及去掉隐藏排序列后的宽度
        self.output_columns: List[str] = []
        self.visible: int = 0

    # -- 元信息 -----------------------------------------------------------
    @property
    def width(self) -> int:
        return len(self.schema)

    def detail(self) -> str:
        return ""

    def label(self) -> str:
        extra = self.detail()
        return f"{self.kind} · {extra}" if extra else self.kind

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} est={self.est_rows:.1f}>"


class Scan(Plan):
    kind = "扫描"

    def __init__(self, table: Table, alias: Optional[str] = None) -> None:
        self.table = table
        self.alias = alias or table.name
        columns = []
        for column in table.columns:
            columns.append(Column(column.name, column.type, self.alias))
        stats = [table.stats.get(column.name) for column in table.columns]
        super().__init__(Schema(columns), stats)
        self.est_rows = float(len(table))
        self.est_cost = max(len(table), 1) * SEQ_SCAN_ROW

    def detail(self) -> str:
        suffix = f" AS {self.alias}" if self.alias != self.table.name else ""
        return f"{self.table.name}{suffix}"


class SingleRow(Plan):
    """没有 FROM 时的来源：恰好一行、零列。

    这样 `SELECT 1+1` 也能走进同一套绑定与执行流程，不必在解析器里特判。
    """

    kind = "单行"

    def __init__(self) -> None:
        super().__init__(Schema(()), ())
        self.est_rows = 1.0
        self.est_cost = 1.0

    def detail(self) -> str:
        return "无 FROM"


class Filter(Plan):
    kind = "过滤"

    def __init__(self, child: Plan, predicates: Sequence[Expr]) -> None:
        super().__init__(child.schema, child.stats, (child,))
        self.predicates: Tuple[Expr, ...] = tuple(predicates)
        nulls = _null_fractions(child.stats, child.est_rows)
        factors = [selectivity(item, child.stats, child.est_rows, nulls)
                   for item in self.predicates]
        self.factors = factors
        factor = 1.0
        for value in factors:
            factor *= value
        self.est_rows = rows_after_filter(child.est_rows, factor)
        self.est_cost = child.est_cost + child.est_rows * FILTER_ROW

    def detail(self) -> str:
        return " AND ".join(item.render() for item in self.predicates)


class Join(Plan):
    kind = "连接"

    def __init__(self, left: Plan, right: Plan, kind: str = "inner",
                 predicates: Sequence[Expr] = ()) -> None:
        super().__init__(left.schema.extend(right.schema), left.stats + right.stats,
                         (left, right))
        self.join_kind = kind
        self.predicates: Tuple[Expr, ...] = tuple(predicates)
        self.left_width = left.width
        self.keys, self.residual = _split_keys(self.predicates, left.width)
        if kind == "cross":
            self.strategy = "cross"
        elif self.keys:
            self.strategy = "hash"
        else:
            self.strategy = "loop"
        # 左连接必须建在右表上：要按左表逐行找匹配，才能补 NULL
        if kind == "left":
            self.build_left = False
        else:
            self.build_left = left.est_rows <= right.est_rows
        self._estimate(left, right)

    def _estimate(self, left: Plan, right: Plan) -> None:
        factor = 1.0
        for left_index, right_index, _ in self.keys:
            left_stats = left.stats[left_index] if left_index < len(left.stats) else None
            right_stats = right.stats[right_index] if right_index < len(right.stats) else None
            if left_stats is not None and right_stats is not None:
                factor *= join_selectivity(left_stats, right_stats)
            else:
                factor *= 0.1
        nulls = _null_fractions(self.stats, left.est_rows + right.est_rows)
        for item in self.residual:
            factor *= selectivity(item, self.stats, left.est_rows + right.est_rows, nulls)
        rows = left.est_rows * right.est_rows * factor
        if self.join_kind == "left":
            rows = max(rows, left.est_rows)
        self.join_factor = factor
        self.est_rows = max(rows, 1.0)

        if self.strategy == "hash":
            build, probe = ((left.est_rows, right.est_rows) if self.build_left
                            else (right.est_rows, left.est_rows))
            local = build * HASH_BUILD_ROW + probe * HASH_PROBE_ROW
        elif self.strategy == "loop":
            local = left.est_rows * NL_OUTER_ROW + left.est_rows * right.est_rows * NL_INNER_ROW
        else:
            local = left.est_rows * right.est_rows * NL_INNER_ROW
        local += self.est_rows * FILTER_ROW * max(len(self.residual), 0)
        self.est_cost = left.est_cost + right.est_cost + local

    def detail(self) -> str:
        how = {"hash": "哈希", "loop": "嵌套循环", "cross": "笛卡尔"}[self.strategy]
        if self.join_kind == "left":
            how = "左外 · " + how
        elif self.join_kind == "cross":
            how = "笛卡尔"
        body = how
        if self.predicates:
            body += " · " + " AND ".join(item.render() for item in self.predicates)
        return body


class Aggregate(Plan):
    kind = "聚合"

    def __init__(self, child: Plan, group_exprs: Sequence[Expr],
                 specs: Sequence["AggSpec"]) -> None:
        columns = [Column(column.name, column.type, "") for column in child.schema]
        stats: List[Optional[ColumnStats]] = list(child.stats)
        for spec in specs:
            columns.append(Column(spec.label, spec.result_type, ""))
            stats.append(None)
        super().__init__(Schema(columns), stats, (child,))
        self.group_exprs: Tuple[Expr, ...] = tuple(group_exprs)
        self.specs: Tuple[AggSpec, ...] = tuple(specs)

        if self.group_exprs:
            groups = 1.0
            for item in self.group_exprs:
                groups *= _group_count(item, child, self.group_exprs)
            self.est_rows = max(1.0, min(groups, child.est_rows))
        else:
            self.est_rows = 1.0
        self.est_cost = child.est_cost + child.est_rows * AGG_ROW

    def detail(self) -> str:
        if not self.group_exprs:
            return "全局聚合 " + ", ".join(spec.label for spec in self.specs)
        return "按 " + ", ".join(item.render() for item in self.group_exprs)


class Project(Plan):
    kind = "投影"

    def __init__(self, child: Plan, items: Sequence[Tuple[Expr, str]],
                 hidden: Sequence[Tuple[Expr, str]] = ()) -> None:
        self.items: Tuple[Tuple[Expr, str], ...] = tuple(items)
        self.hidden: Tuple[Tuple[Expr, str], ...] = tuple(hidden)
        columns = [Column(name, "TEXT", "") for _, name in self.items]
        columns += [Column(name, "TEXT", "") for _, name in self.hidden]
        stats: List[Optional[ColumnStats]] = []
        for expr, _ in list(self.items) + list(self.hidden):
            stats.append(child.stats[expr.index] if isinstance(expr, Ref)
                         and expr.index < len(child.stats) else None)
        super().__init__(Schema(columns), stats, (child,))
        self.visible = len(self.items)
        self.est_rows = child.est_rows
        self.est_cost = child.est_cost + child.est_rows * PROJECT_ROW

    def detail(self) -> str:
        names = [name for _, name in self.items]
        if len(names) > 4:
            return ", ".join(names[:4]) + f" … ({len(names)} 列)"
        return ", ".join(names)


class Distinct(Plan):
    kind = "去重"

    def __init__(self, child: Plan) -> None:
        super().__init__(child.schema, child.stats, (child,))
        self.est_rows = max(1.0, child.est_rows * 0.5)
        self.est_cost = child.est_cost + child.est_rows * FILTER_ROW

    def detail(self) -> str:
        return f"{self.width} 列"


class Sort(Plan):
    kind = "排序"

    def __init__(self, child: Plan, keys: Sequence[Tuple[Expr, bool]]) -> None:
        super().__init__(child.schema, child.stats, (child,))
        self.keys: Tuple[Tuple[Expr, bool], ...] = tuple(keys)
        self.est_rows = child.est_rows
        self.est_cost = child.est_cost + sort_cost(child.est_rows)

    def detail(self) -> str:
        parts = []
        for expr, descending in self.keys:
            parts.append(f"{expr.render()} {'DESC' if descending else 'ASC'}")
        return ", ".join(parts)


class Limit(Plan):
    kind = "限制"

    def __init__(self, child: Plan, limit: Optional[int], offset: int = 0) -> None:
        super().__init__(child.schema, child.stats, (child,))
        self.limit = limit
        self.offset = offset
        taken = child.est_rows - offset
        self.est_rows = max(1.0, min(float(limit), taken)) if limit is not None else max(taken, 1.0)
        self.est_cost = child.est_cost + self.est_rows * PROJECT_ROW

    def detail(self) -> str:
        body = f"{self.limit}" if self.limit is not None else "全部"
        return body + (f" OFFSET {self.offset}" if self.offset else "")


class AggSpec:
    """一个聚合调用的描述：函数、参数、是否 DISTINCT。"""

    __slots__ = ("name", "arg", "star", "distinct", "label")

    def __init__(self, name: str, arg: Optional[Expr], star: bool, distinct: bool,
                 label: str) -> None:
        self.name = name
        self.arg = arg
        self.star = star
        self.distinct = distinct
        self.label = label

    @property
    def result_type(self) -> str:
        if self.name == "COUNT":
            return "INT"
        if self.name in ("SUM", "AVG"):
            return "FLOAT"
        return "TEXT"

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"AggSpec({self.label})"


# ---------------------------------------------------------------------------
# 估算辅助
# ---------------------------------------------------------------------------
def _null_fractions(stats: Sequence[Optional[ColumnStats]], rows: float) -> List[float]:
    total = max(rows, 1.0)
    result = []
    for item in stats:
        if item is None:
            result.append(0.0)
        else:
            result.append(min(1.0, item.nulls / total))
    return result


def _group_count(expr: Expr, child: Plan, group_exprs: Sequence[Expr]) -> float:
    """分组数：单列取去重数，多列按独立假设相乘，再被"总行数"夹住。"""
    if isinstance(expr, Ref):
        if expr.index in _equality_pinned(child):
            # 分组列已经被 `= 常量` 钉死，只剩一个分组
            return 1.0
        if expr.index < len(child.stats):
            stats = child.stats[expr.index]
            if stats is not None and stats.distinct > 0:
                return float(stats.distinct)
    return max(1.0, child.est_rows * DEFAULT_SELECTIVITY)


def _equality_pinned(node: Plan) -> Set[int]:
    """沿计划往下找"被等值常量约束住的列"，返回该节点坐标系里的下标。

    这是估算分组数时最有用的一条信息：`GROUP BY segment` 的上游如果已经有
    `segment = 'VIP'`，那分组数必然是 1，而不是整张表的 NDV。
    """
    if isinstance(node, Filter):
        found: Set[int] = set()
        for predicate in node.predicates:
            found |= _pinned_columns(predicate)
        return found | _equality_pinned(node.children[0])
    if isinstance(node, Join):
        found = set()
        for predicate in node.predicates:
            found |= _pinned_columns(predicate)
        found |= _equality_pinned(node.children[0])
        found |= {index + node.left_width for index in _equality_pinned(node.children[1])}
        return found
    return set()


def _pinned_columns(expr: Expr) -> Set[int]:
    if not isinstance(expr, BinaryOp) or expr.op != "=":
        return set()
    if isinstance(expr.left, Ref) and isinstance(expr.right, Const):
        return {expr.left.index}
    if isinstance(expr.right, Ref) and isinstance(expr.left, Const):
        return {expr.right.index}
    return set()


def _split_keys(predicates: Sequence[Expr], left_width: int):
    """把等值连接条件挑出来当哈希键，其余留在 residual 里。"""
    keys: List[Tuple[int, int, Expr]] = []
    residual: List[Expr] = []
    for item in predicates:
        pair = _equi_pair(item, left_width)
        if pair is None:
            residual.append(item)
        else:
            keys.append((pair[0], pair[1], item))
    return tuple(keys), tuple(residual)


def _equi_pair(expr: Expr, left_width: int) -> Optional[Tuple[int, int]]:
    if not isinstance(expr, BinaryOp) or expr.op != "=":
        return None
    if not isinstance(expr.left, Ref) or not isinstance(expr.right, Ref):
        return None
    a, b = expr.left.index, expr.right.index
    if a < left_width <= b:
        return a, b - left_width
    if b < left_width <= a:
        return b, a - left_width
    return None


def _remap(expr: Expr, mapping: Dict[int, int]) -> Expr:
    """按映射改写表达式里的列下标；映射不到的原样保留（会被外层拒绝）。"""
    if isinstance(expr, Ref):
        return Ref(mapping.get(expr.index, expr.index), expr.name)
    if isinstance(expr, Const):
        return expr
    if isinstance(expr, BinaryOp):
        return BinaryOp(expr.op, _remap(expr.left, mapping), _remap(expr.right, mapping))
    if isinstance(expr, UnaryOp):
        return UnaryOp(expr.op, _remap(expr.operand, mapping))
    if isinstance(expr, Call):
        return Call(expr.name, expr.spec, tuple(_remap(arg, mapping) for arg in expr.args))
    if isinstance(expr, CaseExpr):
        whens = tuple((_remap(c, mapping), _remap(r, mapping)) for c, r in expr.whens)
        default = _remap(expr.default, mapping) if expr.default is not None else None
        return CaseExpr(whens, default)
    if isinstance(expr, InExpr):
        return InExpr(_remap(expr.operand, mapping),
                      tuple(_remap(item, mapping) for item in expr.items), expr.negated)
    if isinstance(expr, BetweenExpr):
        return BetweenExpr(_remap(expr.operand, mapping), _remap(expr.low, mapping),
                           _remap(expr.high, mapping), expr.negated)
    if isinstance(expr, LikeExpr):
        return LikeExpr(_remap(expr.operand, mapping), _remap(expr.pattern, mapping),
                        expr.negated)
    if isinstance(expr, IsNullExpr):
        return IsNullExpr(_remap(expr.operand, mapping), expr.negated)
    if isinstance(expr, AggRef):
        return expr
    return expr


def _shift(expr: Expr, offset: int) -> Expr:
    return _remap(expr, {index: index + offset for index in refs_of(expr)})


def split_conjuncts(expr: Optional[Expr]) -> List[Expr]:
    """把 AND 链拆成一个个独立条件，下推和选择率估算都以单个条件为单位。"""
    if expr is None:
        return []
    if isinstance(expr, BinaryOp) and expr.op == "AND":
        return split_conjuncts(expr.left) + split_conjuncts(expr.right)
    return [expr]


# ---------------------------------------------------------------------------
# FROM 子句 → 连接树
# ---------------------------------------------------------------------------
def build_from(node: ast.Node, db: Database) -> Plan:
    if isinstance(node, ast.TableRef):
        table = db.table(node.name)
        return Scan(table, node.alias)
    if isinstance(node, ast.Join):
        left = build_from(node.left, db)
        right = build_from(node.right, db)
        if node.kind == "cross" or node.on is None:
            return Join(left, right, "cross", ())
        binder = Binder(left.schema.extend(right.schema))
        predicates = split_conjuncts(binder.bind(node.on))
        # 空连接先建出来，再把 ON 条件当待下推谓词交给统一流程：
        # 单侧条件会落到对应的输入上（左连接的右表也能过滤，语义不变），
        # 跨两侧的留作连接条件。
        skeleton = Join(left, right, node.kind, ())
        return pushdown(skeleton, [_Pending(item, False) for item in predicates])
    raise PlanError(f"FROM 里不支持 {type(node).__name__}")


# ---------------------------------------------------------------------------
# 谓词下推
# ---------------------------------------------------------------------------
class _Pending:
    """待下推的谓词，带一个"是否来自 WHERE"的标记。

    WHERE 里的谓词不能越过左连接推到右表（否则会把本该保留的 NULL 行滤掉），
    而 ON 里的可以——`A LEFT JOIN B ON ... AND B.x > 5` 等价于先把 B 过滤一遍。
    """

    __slots__ = ("expr", "from_where")

    def __init__(self, expr: Expr, from_where: bool) -> None:
        self.expr = expr
        self.from_where = from_where


def pushdown(plan: Plan, pending: Sequence[_Pending]) -> Plan:
    if not pending:
        return plan
    if isinstance(plan, Filter):
        merged = [_Pending(item, False) for item in plan.predicates] + list(pending)
        return pushdown(plan.children[0], merged)
    if isinstance(plan, Join):
        left_width = plan.left_width
        to_left: List[_Pending] = []
        to_right: List[_Pending] = []
        stay: List[_Pending] = []
        both_sides = False
        for item in pending:
            refs = refs_of(item.expr)
            if not refs:
                stay.append(item)
            elif max(refs) < left_width:
                to_left.append(item)
            elif min(refs) >= left_width:
                if plan.join_kind == "left" and item.from_where:
                    stay.append(item)
                else:
                    to_right.append(_Pending(_shift(item.expr, -left_width), item.from_where))
            else:
                stay.append(item)
                both_sides = True
        new_left = pushdown(plan.children[0], to_left)
        new_right = pushdown(plan.children[1], to_right)
        # `FROM a, b WHERE a.x = b.y` 里的谓词能把笛卡尔积降级成内连接
        kind = "inner" if plan.join_kind == "cross" and both_sides else plan.join_kind
        # 内连接里 WHERE 和 ON 等价，可以并进连接条件（还能被挑成哈希键）；
        # 左连接的 WHERE 必须留在连接之上，否则会把补出来的 NULL 行滤掉
        merged = [item.expr for item in stay
                  if not item.from_where or kind != "left"]
        above = [item.expr for item in stay
                 if item.from_where and kind == "left"]
        node = Join(new_left, new_right, kind, merged + list(plan.predicates))
        return Filter(node, above) if above else node
    keep = [item.expr for item in pending]
    return Filter(plan, keep)


# ---------------------------------------------------------------------------
# 连接重排
# ---------------------------------------------------------------------------
class _Candidate:
    """重排过程中的一个候选计划。"""

    __slots__ = ("plan", "subset", "positions", "applied", "cost", "rows")

    def __init__(self, plan: Plan, subset: frozenset, positions: Sequence[int],
                 applied: frozenset, cost: float, rows: float) -> None:
        self.plan = plan
        self.subset = subset
        self.positions = list(positions)
        self.applied = applied
        self.cost = cost
        self.rows = rows


def _collect_inner(plan: Plan, base: int, leaves: List[Plan],
                   predicates: List[Tuple[Expr, int]]) -> None:
    """把一棵内连接树摊平成"叶子 + 谓词"，谓词下标统一到拼接后的坐标系。"""
    if isinstance(plan, Join) and plan.join_kind in ("inner", "cross"):
        _collect_inner(plan.children[0], base, leaves, predicates)
        _collect_inner(plan.children[1], base + plan.left_width, leaves, predicates)
        for item in plan.predicates:
            predicates.append((item, base))
    else:
        leaves.append(plan)


_REBUILD = {
    "Filter": lambda node, child: Filter(child, node.predicates),
    "Aggregate": lambda node, child: Aggregate(child, node.group_exprs, node.specs),
    "Project": lambda node, child: Project(child, node.items, node.hidden),
    "Distinct": lambda node, child: Distinct(child),
    "Sort": lambda node, child: Sort(child, node.keys),
    "Limit": lambda node, child: Limit(child, node.limit, node.offset),
}


def reorder(plan: Plan) -> Plan:
    """对最外层的内连接组做重排；左连接内部各自递归处理。"""
    if isinstance(plan, Join):
        if plan.join_kind in ("inner", "cross"):
            leaves: List[Plan] = []
            predicates: List[Tuple[Expr, int]] = []
            _collect_inner(plan, 0, leaves, predicates)
            if len(leaves) <= 1:
                return plan
            exprs = [_shift(expr, offset) for expr, offset in predicates]
            ordered = _order_joins(leaves, exprs)
            return ordered if ordered is not None else plan
        left = reorder(plan.children[0])
        right = reorder(plan.children[1])
        return Join(left, right, plan.join_kind, plan.predicates)
    if plan.children:
        rebuild = _REBUILD.get(type(plan).__name__)
        if rebuild is not None:
            return rebuild(plan, reorder(plan.children[0]))
    return plan


def _order_joins(leaves: Sequence[Plan], predicates: Sequence[Expr]) -> Optional[Plan]:
    """left-deep 动态规划：每个子集保留代价最低的那个计划。"""
    count = len(leaves)
    if count > DP_LIMIT:
        return _greedy_joins(leaves, predicates)

    group_width = sum(leaf.width for leaf in leaves)
    offsets = []
    running = 0
    for leaf in leaves:
        offsets.append(running)
        running += leaf.width
    owner = [0] * group_width
    for index, leaf in enumerate(leaves):
        for column in range(leaf.width):
            owner[offsets[index] + column] = index

    prepared: List[Tuple[int, frozenset, Expr]] = []
    for pid, expr in enumerate(predicates):
        touched = frozenset(owner[i] for i in refs_of(expr))
        if len(touched) < 2:
            continue  # 单表谓词已经由下推阶段处理掉了
        prepared.append((pid, touched, expr))

    best: Dict[frozenset, _Candidate] = {}
    for index, leaf in enumerate(leaves):
        positions = [-1] * group_width
        for column in range(leaf.width):
            positions[offsets[index] + column] = column
        best[frozenset({index})] = _Candidate(
            leaf, frozenset({index}), positions, frozenset(), leaf.est_cost, leaf.est_rows)

    for size in range(2, count + 1):
        for subset in itertools.combinations(range(count), size):
            key = frozenset(subset)
            winner: Optional[_Candidate] = None
            for right_index in subset:
                rest = key - {right_index}
                left = best.get(rest)
                if left is None:
                    continue
                right = best[frozenset({right_index})]
                fresh = [item for item in prepared
                         if item[1] <= key and not item[1] <= rest
                         and not item[1] <= frozenset({right_index})]
                # 两个子集的位置映射逐一合并（互不相交，不是列表拼接）
                positions = [
                    left_value if left_value >= 0 else
                    (right_value + left.plan.width if right_value >= 0 else -1)
                    for left_value, right_value in zip(left.positions, right.positions)
                ]
                mapping = {index: value for index, value in enumerate(positions) if value >= 0}
                joined = _make_join(left.plan, right.plan,
                                    [_remap(item[2], mapping) for item in fresh])
                candidate = _Candidate(
                    joined, key, positions,
                    left.applied | right.applied | frozenset(item[0] for item in fresh),
                    joined.est_cost, joined.est_rows)
                if winner is None or candidate.cost < winner.cost:
                    winner = candidate
            if winner is not None:
                best[key] = winner
    full = best.get(frozenset(range(count)))
    if full is None:
        return None
    return full.plan


def _greedy_joins(leaves: Sequence[Plan], predicates: Sequence[Expr]) -> Plan:
    """表太多时的退路：每次挑"当前代价增量最小"的一张表接上去。"""
    count = len(leaves)
    total_width = sum(leaf.width for leaf in leaves)
    offsets = []
    running = 0
    for leaf in leaves:
        offsets.append(running)
        running += leaf.width
    owner = [0] * total_width
    for index, leaf in enumerate(leaves):
        for column in range(leaf.width):
            owner[offsets[index] + column] = index

    remaining = list(range(1, count))
    current = leaves[0]
    # positions[g] = 全局下标 g 在 current 里的位置，-1 表示还不在
    positions = [-1] * total_width
    for column in range(leaves[0].width):
        positions[column] = column
    used = {0}
    applied: Set[int] = set()

    while remaining:
        best_choice = None
        for index in remaining:
            fresh = []
            for pid, expr in enumerate(predicates):
                if pid in applied:
                    continue
                touched = frozenset(owner[i] for i in refs_of(expr))
                if touched <= used | {index} and not touched <= used:
                    fresh.append((pid, expr))
            mapping: Dict[int, int] = {}
            for global_index in range(total_width):
                if owner[global_index] in used:
                    if positions[global_index] >= 0:
                        mapping[global_index] = positions[global_index]
                elif owner[global_index] == index:
                    mapping[global_index] = current.width + (global_index - offsets[index])
            candidate = _make_join(current, leaves[index],
                                   [_remap(expr, mapping) for _, expr in fresh])
            if best_choice is None or candidate.est_cost < best_choice[0].est_cost:
                best_choice = (candidate, index, [pid for pid, _ in fresh])
        assert best_choice is not None
        chosen_plan, chosen, pids = best_choice
        new_positions = list(positions)
        for global_index in range(total_width):
            if owner[global_index] == chosen:
                new_positions[global_index] = chosen_plan.width - leaves[chosen].width + (
                    global_index - offsets[chosen])
        positions = new_positions
        current = chosen_plan
        remaining.remove(chosen)
        used.add(chosen)
        applied.update(pids)
    return current


def _make_join(left: Plan, right: Plan, predicates: Sequence[Expr]) -> Join:
    return Join(left, right, "inner", predicates)


# ---------------------------------------------------------------------------
# 完整查询
# ---------------------------------------------------------------------------
def plan_query(select: ast.Select, db: Database, reorder_joins: bool = True,
               push_down: bool = True) -> Plan:
    """把一条 SELECT 变成可执行的计划。"""
    if select.source is None:
        source: Plan = SingleRow()
        if any(isinstance(item.expr, ast.Star) for item in select.items):
            raise PlanError("没有 FROM 时不能用 *，没有列可展开")
    else:
        source = build_from(select.source, db)

    # WHERE 先绑在 FROM 的完整 schema 上，再整体下推
    where_binder = Binder(source.schema)
    where_exprs = split_conjuncts(
        where_binder.bind(select.where) if select.where is not None else None)
    pending = [_Pending(expr, True) for expr in where_exprs]
    if push_down:
        source = pushdown(source, pending)
        if reorder_joins:
            source = reorder(source)
    elif pending:
        source = Filter(source, [item.expr for item in pending])

    # 有聚合函数或有 GROUP BY，就得插一个聚合算子
    aggregate_calls = any(
        isinstance(node, ast.Function) and is_aggregate(node.name)
        for expr in _expressions_of(select) for node in ast.walk(expr))
    if select.group_by or aggregate_calls:
        collector = AggCollector()
        group_exprs = [Binder(source.schema).bind(item) for item in select.group_by]
        binder = Binder(source.schema, collector, agg_base=source.width)
        items = [_bind_select_item(item, binder) for item in select.items]
        having = binder.bind(select.having) if select.having is not None else None
        _check_grouped(items, group_exprs, source)
        plan: Plan = Aggregate(source, group_exprs, collector.specs)
        if having is not None:
            plan = Filter(plan, split_conjuncts(having))
    else:
        binder = Binder(source.schema)
        items = [_bind_select_item(item, binder) for item in select.items]
        plan = source

    visible = _expand_star(items, plan.schema)
    alias_map: Dict[str, int] = {}
    for index, (_, name) in enumerate(visible):
        alias_map.setdefault(name.lower(), index)

    # ORDER BY 先当作别名解析；解析不了就退回去引用投影前的列，作为隐藏列一起算
    sort_keys: List[Tuple[Expr, bool]] = []
    hidden: List[Tuple[Expr, str]] = []
    for item in select.order_by:
        expr, descending, is_output = _resolve_order(item, binder, alias_map, len(visible))
        if is_output:
            sort_keys.append((expr, descending))
        elif select.distinct:
            raise PlanError("DISTINCT 查询的 ORDER BY 只能引用选择列表里的列")
        else:
            hidden.append((expr, f"_sort{len(hidden)}"))
            sort_keys.append((Ref(len(visible) + len(hidden) - 1, expr.render()), descending))

    node: Plan = Project(plan, visible, hidden)
    if select.distinct:
        node = Distinct(node)
    if sort_keys:
        node = Sort(node, sort_keys)
    if select.limit is not None or select.offset:
        node = Limit(node, select.limit, select.offset)
    node.output_columns = [name for _, name in visible]
    node.visible = len(visible)
    return node


def _expressions_of(select: ast.Select):
    for item in select.items:
        yield item.expr
    if select.having is not None:
        yield select.having
    for item in select.order_by:
        yield item.expr


def _bind_select_item(item: ast.SelectItem, binder: Binder) -> Tuple[Expr, str]:
    if isinstance(item.expr, ast.Star):
        mark = "*" if item.expr.table is None else f"*.{item.expr.table}"
        return Const(None), mark  # 由 _expand_star 展开
    expr = binder.bind(item.expr)
    name = item.alias or _default_name(item.expr, expr)
    return expr, name


def _default_name(node: ast.Node, expr: Expr) -> str:
    if isinstance(node, ast.Column):
        return node.name
    # 用语法树的原始写法做列名：常量折叠会把 `1 + 1` 变成 `2`，
    # 表头跟着变就太怪了
    return str(node)


def _resolve_order(item: ast.OrderItem, binder: Binder, alias_map: Dict[str, int],
                   visible: int) -> Tuple[Expr, bool, bool]:
    """返回 (表达式, 是否降序, 是否直接指向输出列)。"""
    node = item.expr
    if (isinstance(node, ast.Literal) and isinstance(node.value, int)
            and not isinstance(node.value, bool)):
        index = node.value - 1
        if not 0 <= index < visible:
            raise PlanError(f"ORDER BY {node.value} 超出了选择的列数（共 {visible} 列）")
        return Ref(index, f"#{node.value}"), item.descending, True
    if isinstance(node, ast.Column) and node.table is None:
        slot = alias_map.get(node.name.lower())
        if slot is not None:
            return Ref(slot, node.name), item.descending, True
    return binder.bind(node), item.descending, False


def _expand_star(items: Sequence[Tuple[Expr, str]], schema: Schema) -> List[Tuple[Expr, str]]:
    """把 `*` / `t.*` 展开成实际列，重名时补上表名前缀。"""
    counts: Dict[str, int] = {}
    for column in schema:
        counts[column.name.lower()] = counts.get(column.name.lower(), 0) + 1

    expanded: List[Tuple[Expr, str]] = []
    for expr, name in items:
        if not name.startswith("*"):
            expanded.append((expr, name))
            continue
        only = name[2:] if name.startswith("*.") else None
        for index, column in enumerate(schema):
            if only is not None and column.table.lower() != only.lower():
                continue
            label = column.name if counts[column.name.lower()] == 1 else column.qualified
            expanded.append((Ref(index, column.qualified), label))
    return expanded


def _check_grouped(items: Sequence[Tuple[Expr, str]], group_exprs: Sequence[Expr],
                   source: Plan) -> None:
    """选了没分组的列就报错，别悄悄返回某一行的值。"""
    allowed: Set[int] = set()
    for expr in group_exprs:
        allowed |= refs_of(expr)
    for expr, _ in items:
        if isinstance(expr, Const):
            continue  # 星号占位，展开后是整列，不会被选中
        bad = [index for index in refs_of(expr)
               if index < source.width and index not in allowed]
        if bad:
            name = source.schema[bad[0]].qualified
            raise PlanError(f"列 {name} 必须出现在 GROUP BY 里，或者放进聚合函数")


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------
def explain(plan: Plan, actual: bool = False) -> str:
    """把计划画成树；`actual=True` 时并排显示实际行数。"""
    from .console import display_width, pad

    rows: List[Tuple[str, str, str, str]] = []

    def visit(node: Plan, prefix: str, connector: str, last: bool) -> None:
        label = prefix + connector + node.label()
        rows.append((label, _format_rows(node.est_rows), f"{node.est_cost:,.0f}",
                     _format_rows(node.actual_rows) if actual else ""))
        child_prefix = prefix + ("   " if last else "│  ")
        for index, child in enumerate(node.children):
            visit(child, child_prefix, "└─ " if index == len(node.children) - 1 else "├─ ",
                  index == len(node.children) - 1)

    visit(plan, "", "", True)
    width = max(display_width(row[0]) for row in rows)
    header = ("计划", "估行数", "代价") + (("实行数",) if actual else ())
    out = [f"{pad(header[0], width)}  {pad(header[1], 10, 'right')}  "
           f"{pad(header[2], 8, 'right')}" + (f"  {pad(header[3], 10, 'right')}" if actual else "")]
    out.append("─" * display_width(out[0]))
    for label, est, cost, real in rows:
        line = f"{pad(label, width)}  {pad(est, 10, 'right')}  {pad(cost, 8, 'right')}"
        if actual:
            line += f"  {pad(real, 10, 'right')}"
        out.append(line)
    return "\n".join(out)


def _format_rows(value: float) -> str:
    if value >= 1e6:
        return f"{value / 1e6:.1f}M"
    if value >= 1e4:
        return f"{value / 1e3:.1f}k"
    if value >= 100:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:.1f}"
    return f"{value:.2f}"
