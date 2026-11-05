"""表、列、统计信息与 CSV 装载。

优化器能不能做对决定，几乎全看这里的统计信息。所以每列都记：空值数、
去重数、最小/最大值、等宽直方图，以及出现频率最高的若干个值（MCV）。
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import BindError
from .values import FLOAT, INT, TEXT, Value, common_type, cast_to, format_value, infer_literal, is_number

HISTOGRAM_BUCKETS = 16
MCV_SIZE = 8


@dataclass(frozen=True)
class Column:
    name: str
    type: str = TEXT
    table: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name

    def with_table(self, table: str) -> "Column":
        return Column(self.name, self.type, table)


class Schema:
    """一组有序的列，负责把名字解析成下标。"""

    __slots__ = ("columns",)

    def __init__(self, columns: Sequence[Column]):
        self.columns: Tuple[Column, ...] = tuple(columns)

    def __len__(self) -> int:
        return len(self.columns)

    def __iter__(self):
        return iter(self.columns)

    def __getitem__(self, index: int) -> Column:
        return self.columns[index]

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Schema({', '.join(column.qualified for column in self.columns)})"

    def names(self) -> List[str]:
        return [column.name for column in self.columns]

    def index_of(self, name: str, table: Optional[str] = None) -> int:
        """按名字（可带表前缀）找下标，歧义时报错而不是随便挑一个。"""
        target = name.lower()
        table_key = table.lower() if table else None
        matches = []
        for index, column in enumerate(self.columns):
            if column.name.lower() != target:
                continue
            if table_key is not None and column.table.lower() != table_key:
                continue
            matches.append(index)
        if not matches:
            where = f"{table}.{name}" if table else name
            raise BindError(f"找不到列 {where}")
        if len(matches) > 1:
            qualified = "、".join(self.columns[index].qualified for index in matches)
            raise BindError(f"列名 {name} 有歧义，可能是 {qualified}，请加上表名")
        return matches[0]

    def extend(self, other: "Schema") -> "Schema":
        return Schema(list(self.columns) + list(other.columns))


@dataclass
class ColumnStats:
    nulls: int = 0
    distinct: int = 0
    minimum: Value = None
    maximum: Value = None
    histogram: List[Tuple[float, int]] = field(default_factory=list)
    mcv: List[Tuple[Value, int]] = field(default_factory=list)

    def equality_selectivity(self, value: Value, rows: int) -> float:
        """等值谓词的选择率：先用 MCV 命中，再退回 1/distinct。"""
        if rows <= 0:
            return 0.0
        for candidate, count in self.mcv:
            if _same(candidate, value):
                return max(count / rows, 1.0 / max(rows, 1))
        if self.distinct > 0:
            return 1.0 / self.distinct
        return 0.1

    def range_selectivity(self, low: Optional[Value], high: Optional[Value],
                          rows: int, low_open: bool = False, high_open: bool = False) -> float:
        """范围谓词的选择率，用直方图按桶累加。"""
        if rows <= 0:
            return 0.0
        if not self.histogram or not is_number(self.minimum) or not is_number(self.maximum):
            return 0.33  # 没有直方图时的经验值
        minimum = float(self.minimum)
        maximum = float(self.maximum)
        if maximum <= minimum:
            return _range_fallback(low, high, minimum, maximum)

        low_value = minimum if low is None else float(low)
        high_value = maximum if high is None else float(high)
        total = sum(count for _, count in self.histogram)
        if total <= 0:
            return 0.33
        width = (maximum - minimum) / len(self.histogram)
        covered = 0.0
        for index, (_, count) in enumerate(self.histogram):
            bucket_low = minimum + width * index
            bucket_high = bucket_low + width
            overlap = min(high_value, bucket_high) - max(low_value, bucket_low)
            if overlap <= 0:
                continue
            covered += count * min(overlap / width, 1.0)
        return max(0.0, min(1.0, covered / total))


def _range_fallback(low, high, minimum, maximum) -> float:
    if low is None and high is None:
        return 1.0
    if low is not None and high is not None:
        return 1.0 if minimum == maximum == float(low) else 0.5
    return 0.5


def _safe_min(values: Sequence[Value]) -> Value:
    try:
        return min(values) if values else None
    except TypeError:
        return None


def _safe_max(values: Sequence[Value]) -> Value:
    try:
        return max(values) if values else None
    except TypeError:
        return None


def _same(left: Value, right: Value) -> bool:
    if left is None or right is None:
        return False
    if is_number(left) and is_number(right):
        return float(left) == float(right)
    return str(left) == str(right)


class Table:
    """内存表：列定义 + 行 + 统计信息。"""

    def __init__(self, name: str, columns: Sequence[Column], rows: Sequence[Sequence[Value]]):
        self.name = name
        self.columns: Tuple[Column, ...] = tuple(columns)
        self.rows: List[Tuple[Value, ...]] = [tuple(row) for row in rows]
        self.stats: Dict[str, ColumnStats] = {}
        self._analyze()

    # -- 元信息 -----------------------------------------------------------
    @property
    def schema(self) -> Schema:
        return Schema([column.with_table(self.name) for column in self.columns])

    def __len__(self) -> int:
        return len(self.rows)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Table({self.name}, rows={len(self.rows)}, cols={len(self.columns)})"

    # -- 统计 -------------------------------------------------------------
    def _analyze(self) -> None:
        rows = len(self.rows)
        for index, column in enumerate(self.columns):
            values = [row[index] for row in self.rows]
            nulls = sum(1 for value in values if value is None)
            non_null = [value for value in values if value is not None]
            distinct_values = set(non_null)
            stats = ColumnStats(
                nulls=nulls,
                distinct=len(distinct_values),
                minimum=_safe_min(non_null),
                maximum=_safe_max(non_null),
            )
            if column.type in (INT, FLOAT) and non_null:
                stats.histogram = _histogram([float(value) for value in non_null], HISTOGRAM_BUCKETS)
                stats.mcv = _top_values(non_null, MCV_SIZE)
            elif column.type == TEXT and len(distinct_values) <= max(64, rows // 4):
                # 低基数列（比如状态、城市）记 MCV 最有价值
                stats.mcv = _top_values(non_null, MCV_SIZE)
            self.stats[column.name] = stats

    def null_fraction(self, column_name: str) -> float:
        stats = self.stats.get(column_name)
        if stats is None or not self.rows:
            return 0.0
        return stats.nulls / len(self.rows)

    def column_index(self, name: str) -> int:
        target = name.lower()
        for index, column in enumerate(self.columns):
            if column.name.lower() == target:
                return index
        raise BindError(f"表 {self.name} 没有列 {name}")

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def from_csv(cls, path: str, name: Optional[str] = None) -> "Table":
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                raise BindError(f"CSV 是空的：{path}") from None
            raw_rows = [row for row in reader if any(cell.strip() for cell in row)]
        cleaned = [(header[index].strip() or f"col{index + 1}") for index in range(len(header))]
        width = len(cleaned)
        columns: List[Column] = []
        matrices: List[List[Value]] = [[] for _ in range(width)]
        for row in raw_rows:
            if len(row) < width:
                row = list(row) + [""] * (width - len(row))
            for index in range(width):
                matrices[index].append(infer_literal(row[index]))
        for index, column_name in enumerate(cleaned):
            kind = common_type(matrices[index])
            columns.append(Column(column_name, kind))
            matrices[index] = [cast_to(value, kind) for value in matrices[index]]
        rows = [tuple(matrices[index][row] for index in range(width)) for row in range(len(raw_rows))]
        table_name = name or os.path.splitext(os.path.basename(path))[0]
        return cls(table_name, columns, rows)

    def describe(self) -> str:
        return f"{self.name}: {len(self.rows)} 行, {len(self.columns)} 列"


def _histogram(values: Sequence[float], buckets: int) -> List[Tuple[float, int]]:
    minimum = min(values)
    maximum = max(values)
    if maximum <= minimum:
        return [(minimum, len(values))]
    width = (maximum - minimum) / buckets
    counts = [0] * buckets
    for value in values:
        index = min(int((value - minimum) / width), buckets - 1)
        counts[index] += 1
    return [(minimum + width * index, count) for index, count in enumerate(counts)]


def _top_values(values: Sequence[Value], limit: int) -> List[Tuple[Value, int]]:
    counts: Dict[Value, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: -item[1])[:limit]
    return ordered


class Database:
    """一堆表的集合，名字大小写不敏感。"""

    def __init__(self) -> None:
        self.tables: Dict[str, Table] = {}

    def add(self, table: Table) -> None:
        self.tables[table.name.lower()] = table

    def table(self, name: str) -> Table:
        found = self.tables.get(name.lower())
        if found is None:
            known = "、".join(sorted(table.name for table in self.tables.values())) or "（空）"
            raise BindError(f"没有这张表：{name}。现有表：{known}")
        return found

    def has(self, name: str) -> bool:
        return name.lower() in self.tables

    def names(self) -> List[str]:
        return [table.name for table in self.tables.values()]

    def load_csv(self, path: str, name: Optional[str] = None) -> Table:
        table = Table.from_csv(path, name)
        self.add(table)
        return table

    def load_dir(self, directory: str) -> List[Table]:
        loaded = []
        for entry in sorted(os.listdir(directory)):
            if not entry.lower().endswith(".csv"):
                continue
            loaded.append(self.load_csv(os.path.join(directory, entry)))
        return loaded

    def __len__(self) -> int:
        return len(self.tables)

    def describe(self) -> str:
        lines = []
        for table in self.tables.values():
            lines.append(f"{table.name}  {len(table.rows)} 行")
            for column in table.columns:
                stats = table.stats.get(column.name)
                distinct = stats.distinct if stats else 0
                nulls = stats.nulls if stats else 0
                lines.append(
                    f"  {column.name:<16} {column.type:<6} 去重 {distinct:<6} 空值 {nulls}"
                )
        return "\n".join(lines)


def render_rows(rows: Iterable[Sequence[Value]], columns: Sequence[str]) -> str:
    """简单的等宽表格，给 REPL 用。"""
    from .console import table as console_table

    body = [[format_value(value) for value in row] for row in rows]
    return console_table(list(columns), body)
