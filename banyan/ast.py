"""语法树节点。

刻意保持成不可变的纯数据：解析器只负责生产它，绑定（bind）阶段再把它
翻译成"带列下标的可求值表达式"。两套结构分开的好处是，优化器改写计划时
不必担心动到用户写的原始语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


class Node:
    """所有 AST 节点的基类。"""

    __slots__ = ()


# ---------------------------------------------------------------------------
# 表达式
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Literal(Node):
    value: object

    def __str__(self) -> str:
        if isinstance(self.value, str):
            return "'" + self.value.replace("'", "''") + "'"
        if self.value is None:
            return "NULL"
        return str(self.value)


@dataclass(frozen=True)
class Column(Node):
    name: str
    table: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass(frozen=True)
class Star(Node):
    table: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.table}.*" if self.table else "*"


@dataclass(frozen=True)
class Unary(Node):
    op: str
    operand: Node

    def __str__(self) -> str:
        return f"{self.op} {self.operand}"


@dataclass(frozen=True)
class Binary(Node):
    op: str
    left: Node
    right: Node

    def __str__(self) -> str:
        return f"({self.left} {self.op} {self.right})"


@dataclass(frozen=True)
class Function(Node):
    name: str
    args: Tuple[Node, ...] = ()
    star: bool = False
    distinct: bool = False

    def __str__(self) -> str:
        inner = "*" if self.star else ", ".join(str(arg) for arg in self.args)
        prefix = "DISTINCT " if self.distinct else ""
        return f"{self.name}({prefix}{inner})"


@dataclass(frozen=True)
class Case(Node):
    whens: Tuple[Tuple[Node, Node], ...]
    default: Optional[Node] = None

    def __str__(self) -> str:
        body = " ".join(f"WHEN {c} THEN {r}" for c, r in self.whens)
        tail = f" ELSE {self.default}" if self.default is not None else ""
        return f"CASE {body}{tail} END"


@dataclass(frozen=True)
class InList(Node):
    operand: Node
    items: Tuple[Node, ...]
    negated: bool = False

    def __str__(self) -> str:
        join = ", ".join(str(item) for item in self.items)
        return f"{self.operand} {'NOT ' if self.negated else ''}IN ({join})"


@dataclass(frozen=True)
class Between(Node):
    operand: Node
    low: Node
    high: Node
    negated: bool = False

    def __str__(self) -> str:
        return f"{self.operand} {'NOT ' if self.negated else ''}BETWEEN {self.low} AND {self.high}"


@dataclass(frozen=True)
class Like(Node):
    operand: Node
    pattern: Node
    negated: bool = False

    def __str__(self) -> str:
        return f"{self.operand} {'NOT ' if self.negated else ''}LIKE {self.pattern}"


@dataclass(frozen=True)
class IsNull(Node):
    operand: Node
    negated: bool = False

    def __str__(self) -> str:
        return f"{self.operand} IS {'NOT ' if self.negated else ''}NULL"


# ---------------------------------------------------------------------------
# 查询语句
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SelectItem(Node):
    expr: Node
    alias: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.expr} AS {self.alias}" if self.alias else str(self.expr)


@dataclass(frozen=True)
class TableRef(Node):
    name: str
    alias: Optional[str] = None

    @property
    def label(self) -> str:
        return self.alias or self.name

    def __str__(self) -> str:
        return f"{self.name} AS {self.alias}" if self.alias else self.name


@dataclass(frozen=True)
class Join(Node):
    left: Node
    right: Node
    kind: str = "inner"          # inner | left | cross
    on: Optional[Node] = None

    def __str__(self) -> str:
        word = "LEFT JOIN" if self.kind == "left" else ("CROSS JOIN" if self.kind == "cross" else "JOIN")
        text = f"{self.left} {word} {self.right}"
        return f"{text} ON {self.on}" if self.on is not None else text


@dataclass(frozen=True)
class OrderItem(Node):
    expr: Node
    descending: bool = False

    def __str__(self) -> str:
        return f"{self.expr} {'DESC' if self.descending else 'ASC'}"


@dataclass(frozen=True)
class Select(Node):
    items: Sequence[SelectItem]
    source: Optional[Node]  # None 表示没有 FROM：只有一行、零列
    where: Optional[Node] = None
    group_by: Sequence[Node] = field(default_factory=tuple)
    having: Optional[Node] = None
    order_by: Sequence[OrderItem] = field(default_factory=tuple)
    limit: Optional[int] = None
    offset: int = 0
    distinct: bool = False

    def __str__(self) -> str:
        parts = ["SELECT"]
        if self.distinct:
            parts.append("DISTINCT")
        parts.append(", ".join(str(item) for item in self.items))
        if self.source is not None:
            parts.append("FROM " + str(self.source))
        if self.where is not None:
            parts.append("WHERE " + str(self.where))
        if self.group_by:
            parts.append("GROUP BY " + ", ".join(str(item) for item in self.group_by))
        if self.having is not None:
            parts.append("HAVING " + str(self.having))
        if self.order_by:
            parts.append("ORDER BY " + ", ".join(str(item) for item in self.order_by))
        if self.limit is not None:
            parts.append(f"LIMIT {self.limit}")
        if self.offset:
            parts.append(f"OFFSET {self.offset}")
        return " ".join(parts)


def walk(node: Node):
    """深度遍历一个表达式里的所有节点。"""
    yield node
    if isinstance(node, Unary):
        yield from walk(node.operand)
    elif isinstance(node, Binary):
        yield from walk(node.left)
        yield from walk(node.right)
    elif isinstance(node, Function):
        for arg in node.args:
            yield from walk(arg)
    elif isinstance(node, Case):
        for condition, result in node.whens:
            yield from walk(condition)
            yield from walk(result)
        if node.default is not None:
            yield from walk(node.default)
    elif isinstance(node, InList):
        yield from walk(node.operand)
        for item in node.items:
            yield from walk(item)
    elif isinstance(node, Between):
        yield from walk(node.operand)
        yield from walk(node.low)
        yield from walk(node.high)
    elif isinstance(node, Like):
        yield from walk(node.operand)
        yield from walk(node.pattern)
    elif isinstance(node, IsNull):
        yield from walk(node.operand)


def contains_aggregate(node: Node) -> bool:
    from .functions import is_aggregate

    return any(isinstance(item, Function) and is_aggregate(item.name) for item in walk(node))


def columns_of(node: Node) -> List[Column]:
    return [item for item in walk(node) if isinstance(item, Column)]
