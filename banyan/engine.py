"""对外接口：跑一条 SQL，并诊断"估算为什么不准"。

这一层只做三件事：解析 → 计划 → 执行，外加把两者的数字摆在一起对比。
真正有意思的是 :func:`diagnose`：它不满足于告诉你"估算错了 12 倍"，而是
指出最可能的原因——谓词相关、连接键倾斜、还是直方图压根没覆盖。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import planner
from .catalog import Database
from .executor import Runtime, Row, collect
from .planner import Aggregate, Filter, Join, Limit, Plan, Scan
from .values import Value, format_value


@dataclass
class Result:
    """一次查询的全部产物：数据、计划、以及计划上填好的真实行数。"""

    sql: str
    columns: List[str]
    rows: List[Row]
    plan: Plan
    plan_ms: float = 0.0
    run_ms: float = 0.0

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def total_ms(self) -> float:
        return self.plan_ms + self.run_ms

    def dicts(self) -> List[Dict[str, Value]]:
        return [dict(zip(self.columns, row)) for row in self.rows]

    def text(self, limit: Optional[int] = None) -> str:
        from .console import table

        shown = self.rows if limit is None else self.rows[:limit]
        body = [[format_value(value) for value in row] for row in shown]
        aligns = []
        for index in range(len(self.columns)):
            numeric = all(isinstance(row[index], (int, float)) and not isinstance(row[index], bool)
                          for row in shown) if shown else False
            aligns.append("right" if numeric else "left")
        return table(self.columns, body, aligns)


@dataclass
class Finding:
    """一处估算偏差。"""

    label: str
    estimate: float
    actual: float
    q_error: float
    hint: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "operator": self.label,
            "estimated": round(self.estimate, 1),
            "actual": self.actual,
            "q_error": round(self.q_error, 2),
            "hint": self.hint,
        }


class Engine:
    """一个可以反复查询的会话。"""

    def __init__(self, database: Optional[Database] = None, reorder: bool = True,
                 push_down: bool = True) -> None:
        self.db: Database = database if database is not None else Database()
        self.reorder = reorder
        self.push_down = push_down

    # -- 计划 -------------------------------------------------------------
    def plan(self, sql: str) -> Plan:
        from .parser import parse

        statement = parse(sql)
        return planner.plan_query(statement, self.db, reorder_joins=self.reorder,
                                  push_down=self.push_down)

    def explain(self, sql: str, actual: bool = False) -> str:
        plan = self.plan(sql)
        if actual:
            result = self._run(plan, sql)
            return planner.explain(result.plan, actual=True)
        return planner.explain(plan)

    # -- 执行 -------------------------------------------------------------
    def query(self, sql: str) -> Result:
        start = time.perf_counter()
        plan = self.plan(sql)
        plan_ms = (time.perf_counter() - start) * 1000
        return self._run(plan, sql, plan_ms)

    def _run(self, plan: Plan, sql: str, plan_ms: float = 0.0) -> Result:
        runtime = Runtime()
        start = time.perf_counter()
        columns, rows = collect(plan, runtime)
        run_ms = (time.perf_counter() - start) * 1000
        return Result(sql=sql, columns=columns, rows=rows, plan=plan,
                      plan_ms=plan_ms, run_ms=run_ms)

    def execute(self, sql: str) -> Result:
        """`query` 的别名，脚本里读着更顺。"""
        return self.query(sql)

    # -- 诊断 -------------------------------------------------------------
    def diagnose(self, result: Result, threshold: float = 2.0) -> List[Finding]:
        """找出估算偏差最大的算子，并给出可能的原因。

        投影、限制这类"原样传递"的算子会和它的子算子有一样的偏差，报出来只是
        噪音，所以只在偏差比子算子明显更大时才记一笔。
        """
        findings: List[Finding] = []
        skipped = _under_limit(result.plan)
        for node in _walk_top_down(result.plan):
            if id(node) in skipped:
                continue
            estimate = max(node.est_rows, 0.5)
            actual = float(node.actual_rows)
            if actual == 0:
                q_error = estimate
            else:
                q_error = max(estimate / actual, actual / estimate)
            if q_error < threshold:
                continue
            inherited = 0.0
            for child in node.children:
                if id(child) in skipped:
                    continue
                child_estimate = max(child.est_rows, 0.5)
                if child.actual_rows:
                    inherited = max(inherited,
                                    child_estimate / child.actual_rows,
                                    child.actual_rows / child_estimate)
            if inherited and q_error <= inherited * 1.1:
                continue  # 偏差是从下游传上来的，不算这一笔
            findings.append(Finding(
                label=node.label(),
                estimate=node.est_rows,
                actual=node.actual_rows,
                q_error=q_error,
                hint=_hint_for(node, actual, estimate),
            ))
        findings.sort(key=lambda item: -item.q_error)
        return findings

    def report(self, sql: str, threshold: float = 2.0) -> str:
        """跑一次并输出"计划 + 偏差 + 提示"的可读报告。"""
        from .console import BOLD, DIM, YELLOW, paint

        result = self.query(sql)
        lines = [planner.explain(result.plan, actual=True), ""]
        lines.append(paint(f"计划 {result.plan_ms:.1f} ms · 执行 {result.run_ms:.1f} ms · "
                           f"返回 {len(result.rows)} 行", DIM))
        findings = self.diagnose(result, threshold)
        if not findings:
            note = "（LIMIT 之下的算子不参与比较）" if _under_limit(result.plan) else ""
            lines.append(f"估算与实际基本吻合（所有算子的偏差都在 {threshold:.1f}× 以内）{note}")
            return "\n".join(lines)

        lines.append("")
        lines.append(paint("估算偏差（按偏差从大到小）：", BOLD))
        for index, item in enumerate(findings[:5], start=1):
            lines.append(f"  {index}. {item.label}")
            lines.append(f"     估 {item.estimate:,.0f} 行 → 实 {item.actual:,} 行"
                         f"（{item.q_error:.1f}×）")
            lines.append(paint(f"     {item.hint}", YELLOW))
        if _under_limit(result.plan):
            lines.append("")
            lines.append(paint("注：LIMIT 之下的算子只被拉取了够用的那几行，"
                               "实际行数没有可比性，上面不计入。", DIM))
        return "\n".join(lines)


def _walk_top_down(node: Plan):
    yield node
    for child in node.children:
        yield from _walk_top_down(child)


def _under_limit(plan: Plan) -> Set[int]:
    """LIMIT 之下所有算子的 id。

    火山模型里 `LIMIT 10` 只向上游要 10 行，于是扫描、过滤的 `actual_rows`
    只有 10 而不是全表——拿它跟估算比会得出"扫描估错了 2000 倍"这种假警报。
    """
    skipped: Set[int] = set()

    def mark(node: Plan) -> None:
        for child in node.children:
            skipped.add(id(child))
            mark(child)

    for node in _walk_top_down(plan):
        if isinstance(node, Limit):
            mark(node)
    return skipped


def _hint_for(node: Plan, actual: float, estimate: float) -> str:
    under = actual > estimate   # 实际比估计多
    if isinstance(node, Join):
        if under:
            return ("等值连接按 1/max(去重数) 估，两侧过滤之后键变得更集中时就会偏小"
                    "（客户类型与下单量相关就是典型）")
        return "连接键分布有倾斜，实际匹配数少于均匀假设"
    if isinstance(node, Filter):
        if under:
            return ("多个条件被当成互相独立相乘，但它们其实是相关的"
                    "（比如客户类型和授信额度），真实命中率比估算高得多")
        return "某个条件的实际选择率比统计信息预期的更低（LIKE 前缀或稀疏区间）"
    if isinstance(node, Aggregate):
        if under:
            return "多列分组被当成互相独立，真实组合数比相乘的结果更多"
        return ("分组列的 NDV 取自上卷统计；上游过滤把它限制住之后，"
                "实际分组数远少于整列的取值个数")
    if isinstance(node, Limit):
        return "LIMIT 的上游行数估偏了"
    if isinstance(node, Scan):
        if under:
            return "表的实际行数比统计信息里的少（装载之后数据被改过）"
        return "扫描被上层提前截断，只读了其中一部分行"
    return "上游算子的偏差传到了这里"


def run_sql(database: Database, sql: str) -> Result:
    """一次性查询的便捷入口。"""
    return Engine(database).query(sql)
