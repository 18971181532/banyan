"""命令行入口。

子命令的设计原则：每个都对应一种"你想知道的事"——数据长什么样（query）、
引擎打算怎么算（explain）、为什么算得慢（explain --analyze）、表里有什么
（tables）。不加子命令时默认执行 query，方便 `banyan "SELECT ..."`。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional, Sequence

from . import __version__, sampledata
from .catalog import Database
from .console import BOLD, DIM, GREEN, RED, YELLOW, display_width, enable_utf8, pad, paint, table
from .engine import Engine, Result
from .errors import SQLError
from .values import format_value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="banyan",
        description="Banyan —— 一套带成本模型的 SQL 查询引擎（纯标准库）",
        epilog="例子：banyan \"SELECT city, COUNT(*) FROM customers GROUP BY city LIMIT 5\"",
    )
    parser.add_argument("--version", action="version", version=f"banyan {__version__}")
    parser.add_argument("--data", metavar="目录", help="从目录加载 CSV 表")
    parser.add_argument("--scale", type=float, default=1.0, help="内置示例数据的规模倍数（默认 1.0）")
    parser.add_argument("--no-color", action="store_true", help="关闭颜色")
    parser.add_argument("--no-reorder", action="store_true", help="关闭连接重排（用来对照代价模型的效果）")
    parser.add_argument("--no-pushdown", action="store_true", help="关闭谓词下推")

    sub = parser.add_subparsers(dest="command")

    query = sub.add_parser("query", aliases=["q"], help="执行一条 SQL")
    query.add_argument("sql", nargs="+", help="SQL 语句")
    query.add_argument("--limit", type=int, default=0, help="最多显示多少行（0 表示全部）")
    query.add_argument("--json", action="store_true", help="按 JSON 输出")
    query.add_argument("--timing", action="store_true", help="显示计划与执行耗时")

    explain = sub.add_parser("explain", help="显示执行计划")
    explain.add_argument("sql", nargs="+", help="SQL 语句")
    explain.add_argument("--analyze", "-a", action="store_true",
                         help="真正跑一遍，并给出估算偏差诊断")

    sub.add_parser("tables", help="列出表结构与统计信息")

    sample = sub.add_parser("sample", help="把内置示例数据写成 CSV")
    sample.add_argument("--out", default="data", help="输出目录（默认 data）")
    sample.add_argument("--scale", type=float, default=0.1, help="数据规模倍数（默认 0.1）")

    shell = sub.add_parser("shell", help="交互式终端")
    shell.add_argument("--limit", type=int, default=50, help="每次最多显示多少行")

    bench = sub.add_parser("bench", help="跑一组基准查询")
    bench.add_argument("--repeat", type=int, default=3, help="每条查询重复次数")
    bench.add_argument("--scale", type=float, default=1.0, help="数据规模倍数")

    sub.add_parser("functions", help="列出标量函数")
    return parser


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
COMMANDS = {"query", "q", "explain", "tables", "sample", "shell", "bench", "functions"}
_VALUE_OPTIONS = {"--data", "--scale"}


def normalize(argv: Sequence[str]) -> List[str]:
    """没写子命令时把第一个位置参数当成 SQL，`banyan "SELECT ..."` 才顺手。"""
    args = list(argv)
    index = 0
    while index < len(args):
        token = args[index]
        if token in _VALUE_OPTIONS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if token in COMMANDS:
            return args
        return args[:index] + ["query"] + args[index:]
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    enable_utf8()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(normalize(raw))
    if args.no_color:
        os.environ["NO_COLOR"] = "1"

    if args.command == "sample":
        return cmd_sample(args)
    if args.command == "bench":
        return cmd_bench(args)
    if args.command == "functions":
        return cmd_functions()

    try:
        database = load_database(args)
    except SQLError as exc:
        print(paint(exc.render(), RED), file=sys.stderr)
        return 1
    engine = Engine(database, reorder=not args.no_reorder, push_down=not args.no_pushdown)

    if args.command == "tables":
        return cmd_tables(database)
    if args.command == "shell":
        return cmd_shell(engine, args.limit)
    if args.command == "explain":
        return cmd_explain(engine, join(args.sql), args.analyze)
    sql = join(args.sql) if getattr(args, "sql", None) else None
    if sql is None:
        build_parser().print_help()
        return 0
    return cmd_query(engine, sql, getattr(args, "limit", 0),
                     getattr(args, "json", False), getattr(args, "timing", False))


def join(parts: Sequence[str]) -> str:
    return " ".join(parts).strip()


def load_database(args) -> Database:
    if args.data:
        database = Database()
        loaded = database.load_dir(args.data)
        if not loaded:
            raise SQLError(f"{args.data} 里没有找到 CSV 文件")
        return database
    return sampledata.build(scale=args.scale)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_query(engine: Engine, sql: str, limit: int = 0, as_json: bool = False,
              timing: bool = False) -> int:
    try:
        result = engine.query(sql)
    except SQLError as exc:
        print(paint(exc.render(), RED if hasattr(exc, "position") else YELLOW), file=sys.stderr)
        return 1
    if as_json:
        import json

        print(json.dumps({"columns": result.columns, "rows": result.rows},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    print(render_result(result, limit))
    if timing:
        print(paint(f"计划 {result.plan_ms:.2f} ms · 执行 {result.run_ms:.2f} ms · "
                    f"{len(result.rows)} 行", DIM))
    return 0


def render_result(result: Result, limit: int = 0) -> str:
    if not result.columns:
        return "（没有列）"
    shown = result.rows if not limit else result.rows[:limit]
    body = [[format_value(value) for value in row] for row in shown]
    aligns = []
    for index in range(len(result.columns)):
        numeric = bool(shown) and all(
            isinstance(row[index], (int, float)) and not isinstance(row[index], bool)
            for row in shown)
        aligns.append("right" if numeric else "left")
    text = table(result.columns, body, aligns)
    if limit and len(result.rows) > limit:
        text += "\n" + paint(f"  … 还有 {len(result.rows) - limit} 行", DIM)
    elif len(result.rows) == 0:
        text += "\n" + paint("  （没有匹配的行）", DIM)
    return text


def cmd_explain(engine: Engine, sql: str, analyze: bool) -> int:
    try:
        if analyze:
            print(engine.report(sql))
        else:
            print(engine.explain(sql))
    except SQLError as exc:
        print(paint(exc.render(), YELLOW), file=sys.stderr)
        return 1
    return 0


def cmd_tables(database: Database) -> int:
    for name in database.names():
        info = database.table(name)
        header = f"{name}  {len(info.rows)} 行  {len(info.columns)} 列"
        print(paint(header, BOLD))
        body = []
        for column in info.columns:
            stats = info.stats.get(column.name)
            body.append([
                column.name, column.type,
                f"{stats.distinct:,}" if stats else "-",
                f"{stats.nulls:,}" if stats else "-",
                format_value(stats.minimum) if stats else "-",
                format_value(stats.maximum) if stats else "-",
            ])
        print(table(["列", "类型", "去重", "空值", "最小", "最大"], body,
                    ["left", "left", "right", "right", "left", "left"], indent="  "))
        print()
    return 0


def cmd_shell(engine: Engine, limit: int) -> int:
    print(paint(f"Banyan {__version__} —— 输入 SQL，以分号结束；.tables 看表，.exit 退出", DIM))
    buffer: List[str] = []
    while True:
        prompt = paint("banyan> " if not buffer else "   ...> ", GREEN)
        try:
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        stripped = line.strip()
        if not buffer and stripped.startswith("."):
            if stripped in (".exit", ".quit"):
                return 0
            if stripped == ".tables":
                cmd_tables(engine.db)
                continue
            if stripped == ".help":
                print("  .tables  列出表\n  .exit    退出\n  其余输入按 SQL 执行，分号结束")
                continue
            print(paint(f"未知命令 {stripped}", YELLOW))
            continue
        if not stripped:
            continue
        buffer.append(line)
        if ";" not in line:
            continue
        sql = " ".join(buffer).strip().rstrip(";")
        buffer = []
        if not sql:
            continue
        start = time.perf_counter()
        try:
            result = engine.query(sql)
        except SQLError as exc:
            print(paint(exc.render(), RED))
            continue
        print(render_result(result, limit))
        print(paint(f"  {len(result.rows)} 行 · {(time.perf_counter() - start) * 1000:.1f} ms", DIM))
    return 0


def cmd_sample(args) -> int:
    written = sampledata.write_csvs(args.out, scale=args.scale)
    print(paint(f"已写出 {len(written)} 个 CSV 到 {os.path.abspath(args.out)}", GREEN))
    for path in written:
        size = os.path.getsize(path)
        print(f"  {os.path.basename(path):<20} {size / 1024:,.0f} KB")
    return 0


BENCH_QUERIES = [
    ("全表扫描", "SELECT COUNT(*) FROM order_items"),
    ("单表过滤", "SELECT COUNT(*) FROM orders WHERE status = '退款中'"),
    ("分组聚合", "SELECT city, COUNT(*) FROM customers GROUP BY city"),
    ("两表连接", "SELECT COUNT(*) FROM orders o JOIN customers c ON o.customer_id = c.id"),
    ("四表连接", """SELECT p.category, COUNT(*) AS n, ROUND(SUM(i.quantity * i.unit_price), 2) AS gmv
                    FROM order_items i
                    JOIN orders o ON i.order_id = o.id
                    JOIN customers c ON o.customer_id = c.id
                    JOIN products p ON i.product_id = p.id
                    WHERE c.city = '上海' AND p.category = '乐器'
                    GROUP BY p.category"""),
    ("五表连接", """SELECT r.zone, COUNT(*) AS n
                    FROM order_items i
                    JOIN orders o ON i.order_id = o.id
                    JOIN customers c ON o.customer_id = c.id
                    JOIN regions r ON c.city = r.city
                    JOIN products p ON i.product_id = p.id
                    WHERE r.zone = '华东' AND p.category <> '图书'
                    GROUP BY r.zone"""),
]


def cmd_bench(args) -> int:
    database = sampledata.build(scale=args.scale)
    fast = Engine(database, reorder=True)
    slow = Engine(database, reorder=False)
    print(paint(f"数据规模 ×{args.scale}：", BOLD)
                + "  ".join(f"{name} {len(database.table(name)):,} 行" for name in database.names()))
    print()

    rows = []
    for label, sql in BENCH_QUERIES:
        fast.query(sql)
        slow.query(sql)
        best_fast = min(_time(fast, sql) for _ in range(args.repeat))
        best_slow = min(_time(slow, sql) for _ in range(args.repeat))
        rows.append([label, f"{best_fast:.1f}", f"{best_slow:.1f}",
                     f"{best_slow / best_fast:.1f}×" if best_fast else "-"])
    print(table(["查询", "重排后 ms", "关闭重排 ms", "差距"], rows,
                ["left", "right", "right", "right"]))
    print()
    print(paint("注：只关掉连接重排，谓词下推仍然开启；数字取 %d 次最小值。" % args.repeat, DIM))
    return 0


def _time(engine: Engine, sql: str) -> float:
    start = time.perf_counter()
    engine.query(sql)
    return (time.perf_counter() - start) * 1000


def cmd_functions() -> int:
    from .functions import AGGREGATES, SCALARS

    items = sorted(SCALARS.values(), key=lambda item: item.name)
    print(paint(f"标量函数（{len(items)} 个）", BOLD))
    rows = [[item.name, item.signature, item.summary] for item in items]
    print(table(["名字", "签名", "说明"], rows, ["left", "left", "left"]))
    print()
    print(paint(f"聚合函数（{len(AGGREGATES)} 个）：" + ", ".join(AGGREGATES), BOLD))
    return 0
