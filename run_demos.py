#!/usr/bin/env python3
"""生成 README 用的图，并打印基准数字。

图里的每个数字都来自这次真实运行，没有手写的常量——所以重新跑一遍，
图会跟着变，不会和图下面的文字对不上。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Dict, List, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from banyan import Engine, sampledata  # noqa: E402
from banyan.console import enable_utf8, table  # noqa: E402
from banyan.planner import Plan  # noqa: E402

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")

BOX_W, BOX_H, GAP_X, GAP_Y = 190, 56, 14, 22
INK = "#1f2933"
MUTED = "#6b7785"
LINE = "#c9d3df"
FILL = "#f7f9fc"
ACCENT = "#b06000"
BLUE = "#1f6feb"

BENCH = [
    ("全表扫描", "SELECT COUNT(*) FROM order_items"),
    ("单表过滤", "SELECT COUNT(*) FROM orders WHERE status = '退款中'"),
    ("分组聚合", "SELECT city, COUNT(*) FROM customers GROUP BY city"),
    ("两表连接", "SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id"),
    ("四表连接", """SELECT p.category, COUNT(*) AS n FROM order_items i
                    JOIN orders o ON i.order_id = o.id
                    JOIN customers c ON o.customer_id = c.id
                    JOIN products p ON i.product_id = p.id
                    WHERE c.city = '上海' AND p.category = '乐器'
                    GROUP BY p.category"""),
    ("五表连接", """SELECT r.zone, COUNT(*) AS n FROM order_items i
                    JOIN orders o ON i.order_id = o.id
                    JOIN customers c ON o.customer_id = c.id
                    JOIN regions r ON c.city = r.city
                    JOIN products p ON i.product_id = p.id
                    WHERE r.zone = '华东' AND p.category <> '图书'
                    GROUP BY r.zone"""),
]


# ---------------------------------------------------------------------------
# SVG 小工具
# ---------------------------------------------------------------------------
def _svg(width: int, height: int, body: Sequence[str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="PingFang SC, Microsoft YaHei, '
        f'Segoe UI, sans-serif">\n'
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>\n'
        + "\n".join(body) + "\n</svg>\n")


def _text(x: float, y: float, content: str, size: float = 12, color: str = INK,
          weight: str = "normal", anchor: str = "start") -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" '
            f'font-weight="{weight}" text-anchor="{anchor}">{_escape(content)}</text>')


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _rect(x: float, y: float, w: float, h: float, fill: str, stroke: str,
          radius: float = 6) -> str:
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')


def _line(x1, y1, x2, y2, color=LINE, width=1.0) -> str:
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{color}" stroke-width="{width}"/>')


def text_width(content: str, size: float) -> float:
    """粗略估算像素宽度：中日韩字符按 1 个字号，其余按 0.55 个字号。"""
    total = 0.0
    for char in content:
        total += size * (1.0 if ord(char) > 0x2E80 else 0.55)
    return total


def fit(content: str, max_px: float, size: float) -> str:
    if text_width(content, size) <= max_px:
        return content
    trimmed = content
    while trimmed and text_width(trimmed + "…", size) > max_px:
        trimmed = trimmed[:-1]
    return trimmed + "…"


def rows_text(value: float) -> str:
    if value >= 1e4:
        return f"{value / 1e3:,.1f}k"
    if value >= 100:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:.0f}"
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
# 图一：计划树（估算 vs 实际）
# ---------------------------------------------------------------------------
def plan_svg(plan: Plan, title: str, subtitle: str, path: str) -> None:
    positions: Dict[int, Tuple[float, float]] = {}
    width_used = [0.0]

    def place(node: Plan, depth: int, cursor: float) -> float:
        if not node.children:
            positions[id(node)] = (cursor, depth)
            return cursor + BOX_W + GAP_X
        for child in node.children:
            cursor = place(child, depth + 1, cursor)
        xs = [positions[id(child)][0] for child in node.children]
        positions[id(node)] = ((min(xs) + max(xs)) / 2, depth)
        width_used[0] = max(width_used[0], cursor)
        return cursor

    place(plan, 0, 0)
    depth_max = max(depth for _, depth in positions.values())
    content_w = width_used[0] - GAP_X
    width = int(content_w + 48)
    height = int(74 + (depth_max + 1) * (BOX_H + GAP_Y) + 56)

    body: List[str] = [_text(24, 32, title, 14, INK, "bold"),
                       _text(24, 52, subtitle, 11, MUTED)]

    margin_x = 24
    for node in plan.walk():
        x, depth = positions[id(node)]
        x += margin_x
        y = 74 + depth * (BOX_H + GAP_Y)
        for child in node.children:
            cx, cdepth = positions[id(child)]
            cy = 74 + cdepth * (BOX_H + GAP_Y)
            mid = cy - GAP_Y / 2
            body.append(_line(x + BOX_W / 2, y + BOX_H, cx + BOX_W / 2, mid))
            body.append(_line(cx + BOX_W / 2, mid, cx + BOX_W / 2, cy))
        body.append(_rect(x, y, BOX_W, BOX_H, FILL, LINE))
        body.append(_text(x + 10, y + 17, node.kind, 10.5, INK, "bold"))
        detail = fit(node.detail(), BOX_W - 20, 9)
        if detail:
            body.append(_text(x + 10, y + 32, detail, 9, MUTED))
        body.append(_text(x + 10, y + 48, f"估 {rows_text(node.est_rows)}", 9, MUTED))
        body.append(_text(x + 68, y + 48, f"实 {rows_text(node.actual_rows)}", 9, ACCENT))
        body.append(_text(x + BOX_W - 10, y + 48, f"代价 {node.est_cost:,.0f}", 8.5, MUTED,
                          anchor="end"))

    body.append(_text(24, height - 16,
                      "每个框里：估 = 优化器的估计行数，实 = 真正跑出来的行数。"
                      "左侧分支先被算出来，因为它的估计行数最少。", 10, MUTED))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_svg(width, height, body))
    print(f"  写出 {os.path.relpath(path)}  ({width}×{height})")


# ---------------------------------------------------------------------------
# 图二：重排前后的耗时
# ---------------------------------------------------------------------------
def bench_svg(results: Sequence[Tuple[str, float, float]], path: str) -> None:
    left, top, row_h = 96, 74, 37
    bar_max = 300
    width, height = 640, int(top + row_h * len(results) + 66)
    longest = max(max(fast, slow) for _, fast, slow in results)
    scale = bar_max / longest

    body: List[str] = [
        _text(24, 32, "连接重排的收益", 14, INK, "bold"),
        _text(24, 52, "同一份数据、同一条 SQL，只切换优化器的连接顺序", 11, MUTED),
    ]
    for index, (label, fast, slow) in enumerate(results):
        y = top + index * row_h
        body.append(_text(left - 12, y + 16, label, 11, INK, anchor="end"))
        body.append(f'<rect x="{left}" y="{y + 3}" width="{slow * scale:.1f}" height="11" '
                    f'rx="3" fill="#e6ebf2"/>')
        body.append(f'<rect x="{left}" y="{y + 19}" width="{fast * scale:.1f}" height="11" '
                    f'rx="3" fill="{BLUE}" opacity="0.85"/>')
        body.append(_text(left + max(slow, fast) * scale + 10, y + 13,
                          f"{slow:.1f} ms", 10, MUTED))
        body.append(_text(left + fast * scale + 10, y + 29, f"{fast:.1f} ms", 10, BLUE))

    legend_y = height - 40
    body.append(f'<rect x="{left}" y="{legend_y}" width="10" height="10" rx="2" fill="#e6ebf2"/>')
    body.append(_text(left + 16, legend_y + 9, "关闭重排（按书写顺序）", 10, MUTED))
    body.append(f'<rect x="{left + 150}" y="{legend_y}" width="10" height="10" rx="2" '
                f'fill="{BLUE}" opacity="0.85"/>')
    body.append(_text(left + 166, legend_y + 9, "开启重排（代价模型选的顺序）", 10, MUTED))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_svg(width, height, body))
    print(f"  写出 {os.path.relpath(path)}  ({width}×{height})")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def measure(engine: Engine, sql: str, repeat: int = 3) -> Tuple[float, object]:
    engine.query(sql)
    best = None
    result = None
    for _ in range(repeat):
        start = time.perf_counter()
        result = engine.query(sql)
        elapsed = (time.perf_counter() - start) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best, result


def main() -> int:
    enable_utf8()
    os.makedirs(DOCS, exist_ok=True)
    database = sampledata.build(scale=1.0)
    print("数据规模：", "  ".join(f"{name} {len(database.table(name)):,} 行"
                                  for name in database.names()))

    print("\n[1/3] 计划树")
    sql = BENCH[4][1]
    fast_engine = Engine(database, reorder=True)
    slow_engine = Engine(database, reorder=False)
    fast_ms, result = measure(fast_engine, sql)
    slow_ms, _ = measure(slow_engine, sql)
    joins = next(node for node in result.plan.walk() if node.kind == "连接")
    plan_svg(
        joins,
        f"四表连接的连接树（开启重排，整条查询 {fast_ms:.0f} ms）",
        f"同一条 SQL 关闭重排要 {slow_ms:.0f} ms——差别只在于先接哪张表",
        os.path.join(DOCS, "plan.svg"))
    print()
    print(fast_engine.explain(sql))

    print("\n[2/3] 重排对比")
    lines = []
    for label, query in BENCH:
        fast, _ = measure(fast_engine, query)
        slow, _ = measure(slow_engine, query)
        lines.append((label, fast, slow))
    bench_svg(lines, os.path.join(DOCS, "reorder.svg"))
    print(table(["查询", "重排后 ms", "关闭重排 ms", "差距"],
                [[label, f"{fast:.1f}", f"{slow:.1f}",
                  f"{slow / fast:.1f}×" if fast else "-"] for label, fast, slow in lines],
                ["left", "right", "right", "right"]))

    print("\n[3/3] 估算偏差")
    engine = Engine(database)
    skew_sql = ("SELECT c.segment, COUNT(*) FROM customers c "
                "JOIN orders o ON c.id = o.customer_id "
                "WHERE c.segment = 'VIP' GROUP BY c.segment")
    result = engine.query(skew_sql)
    for finding in engine.diagnose(result):
        print(f"  {finding.label}")
        print(f"    估 {finding.estimate:,.0f} / 实 {finding.actual:,} "
              f"（{finding.q_error:.1f}×）")
        print(f"    {finding.hint}")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
