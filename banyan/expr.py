"""绑定后的可求值表达式。

和 :mod:`banyan.ast` 的区别：这里的列引用已经变成行元组的下标，函数名也已经
解析成具体实现。执行期只跟这一套结构打交道，不再碰名字。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Set, Tuple

from .errors import EvalError
from .functions import Scalar, match_like
from .values import Value, compare, equals, is_number, to_number, truthy


class Expr:
    """表达式基类。"""

    def eval(self, row: Sequence[Value]) -> Value:  # pragma: no cover - 抽象
        raise NotImplementedError

    def render(self) -> str:  # pragma: no cover - 抽象
        raise NotImplementedError

    def refs(self) -> Set[int]:
        return set()

    def children(self) -> Sequence["Expr"]:
        return ()


@dataclass(frozen=True)
class Const(Expr):
    value: Value

    def eval(self, row: Sequence[Value]) -> Value:
        return self.value

    def render(self) -> str:
        if self.value is None:
            return "NULL"
        if isinstance(self.value, str):
            return "'" + self.value.replace("'", "''") + "'"
        if isinstance(self.value, bool):
            return "TRUE" if self.value else "FALSE"
        return str(self.value)


@dataclass(frozen=True)
class Ref(Expr):
    index: int
    name: str

    def eval(self, row: Sequence[Value]) -> Value:
        return row[self.index]

    def render(self) -> str:
        return self.name

    def refs(self) -> Set[int]:
        return {self.index}


@dataclass(frozen=True)
class Call(Expr):
    name: str
    spec: Scalar
    args: Tuple[Expr, ...]

    def eval(self, row: Sequence[Value]) -> Value:
        values = [arg.eval(row) for arg in self.args]
        try:
            return self.spec.fn(*values)
        except EvalError:
            raise
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            if str(exc) == "NULL":
                return None
            raise EvalError(f"{self.name} 求值失败：{exc}") from exc

    def render(self) -> str:
        return f"{self.name}({', '.join(arg.render() for arg in self.args)})"

    def children(self) -> Sequence[Expr]:
        return self.args


@dataclass(frozen=True)
class UnaryOp(Expr):
    op: str
    operand: Expr

    def eval(self, row: Sequence[Value]) -> Value:
        value = self.operand.eval(row)
        if self.op == "NOT":
            return None if value is None else (not truthy(value))
        if value is None:
            return None
        number = to_number(value, "一元负号的操作数")
        return -number

    def render(self) -> str:
        if self.op == "NOT":
            return f"NOT {self.operand.render()}"
        return f"-{self.operand.render()}"

    def children(self) -> Sequence[Expr]:
        return (self.operand,)


@dataclass(frozen=True)
class BinaryOp(Expr):
    op: str
    left: Expr
    right: Expr

    def eval(self, row: Sequence[Value]) -> Value:
        op = self.op
        if op == "AND":
            left = _truth3(self.left.eval(row))
            if left is False:
                return False
            right = _truth3(self.right.eval(row))
            if right is False:
                return False
            if left is None or right is None:
                return None
            return True
        if op == "OR":
            left = _truth3(self.left.eval(row))
            if left is True:
                return True
            right = _truth3(self.right.eval(row))
            if right is True:
                return True
            if left is None or right is None:
                return None
            return False

        left = self.left.eval(row)
        right = self.right.eval(row)
        if op in ("=", "<>", "<", "<=", ">", ">="):
            return _comparison(op, left, right)
        if left is None or right is None:
            return None
        if op == "||":
            return f"{left}{right}"
        return _arithmetic(op, left, right)

    def render(self) -> str:
        return f"({self.left.render()} {self.op} {self.right.render()})"

    def children(self) -> Sequence[Expr]:
        return (self.left, self.right)


@dataclass(frozen=True)
class CaseExpr(Expr):
    whens: Tuple[Tuple[Expr, Expr], ...]
    default: Optional[Expr] = None

    def eval(self, row: Sequence[Value]) -> Value:
        for condition, result in self.whens:
            if truthy(condition.eval(row)):
                return result.eval(row)
        return self.default.eval(row) if self.default is not None else None

    def render(self) -> str:
        body = " ".join(f"WHEN {c.render()} THEN {r.render()}" for c, r in self.whens)
        tail = f" ELSE {self.default.render()}" if self.default is not None else ""
        return f"CASE {body}{tail} END"

    def children(self) -> Sequence[Expr]:
        items: List[Expr] = []
        for condition, result in self.whens:
            items.extend([condition, result])
        if self.default is not None:
            items.append(self.default)
        return tuple(items)


@dataclass(frozen=True)
class InExpr(Expr):
    operand: Expr
    items: Tuple[Expr, ...]
    negated: bool = False

    def eval(self, row: Sequence[Value]) -> Value:
        value = self.operand.eval(row)
        if value is None:
            return None
        saw_null = False
        for item in self.items:
            candidate = item.eval(row)
            if candidate is None:
                saw_null = True
                continue
            if equals(value, candidate):
                return not self.negated
        if saw_null:
            return None
        return self.negated

    def render(self) -> str:
        body = ", ".join(item.render() for item in self.items)
        return f"{self.operand.render()} {'NOT ' if self.negated else ''}IN ({body})"

    def children(self) -> Sequence[Expr]:
        return (self.operand,) + self.items


@dataclass(frozen=True)
class BetweenExpr(Expr):
    operand: Expr
    low: Expr
    high: Expr
    negated: bool = False

    def eval(self, row: Sequence[Value]) -> Value:
        value = self.operand.eval(row)
        low = self.low.eval(row)
        high = self.high.eval(row)
        lower = compare(value, low)
        upper = compare(value, high)
        if lower is None or upper is None:
            return None
        inside = lower >= 0 and upper <= 0
        return not inside if self.negated else inside

    def render(self) -> str:
        return (f"{self.operand.render()} {'NOT ' if self.negated else ''}"
                f"BETWEEN {self.low.render()} AND {self.high.render()}")

    def children(self) -> Sequence[Expr]:
        return (self.operand, self.low, self.high)


@dataclass(frozen=True)
class LikeExpr(Expr):
    operand: Expr
    pattern: Expr
    negated: bool = False

    def eval(self, row: Sequence[Value]) -> Value:
        result = match_like(self.operand.eval(row), self.pattern.eval(row))
        if result is None:
            return None
        return not result if self.negated else result

    def render(self) -> str:
        return f"{self.operand.render()} {'NOT ' if self.negated else ''}LIKE {self.pattern.render()}"

    def children(self) -> Sequence[Expr]:
        return (self.operand, self.pattern)


@dataclass(frozen=True)
class IsNullExpr(Expr):
    operand: Expr
    negated: bool = False

    def eval(self, row: Sequence[Value]) -> Value:
        empty = self.operand.eval(row) is None
        return (not empty) if self.negated else empty

    def render(self) -> str:
        return f"{self.operand.render()} IS {'NOT ' if self.negated else ''}NULL"

    def children(self) -> Sequence[Expr]:
        return (self.operand,)


# ---------------------------------------------------------------------------
# 运算实现
# ---------------------------------------------------------------------------
def _truth3(value: Value) -> Optional[bool]:
    if value is None:
        return None
    return truthy(value)


def _comparison(op: str, left: Value, right: Value) -> Value:
    result = compare(left, right)
    if result is None:
        return None
    if op == "=":
        return result == 0
    if op == "<>":
        return result != 0
    if op == "<":
        return result < 0
    if op == "<=":
        return result <= 0
    if op == ">":
        return result > 0
    return result >= 0


def _arithmetic(op: str, left: Value, right: Value) -> Value:
    a = to_number(left, "左操作数")
    b = to_number(right, "右操作数")
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if b == 0:
            raise EvalError("除以零")
        return a / b
    if op == "%":
        if b == 0:
            raise EvalError("对零取模")
        return a % b
    raise EvalError(f"不支持的运算符 {op}")


def walk_expr(expr: Expr):
    yield expr
    for child in expr.children():
        yield from walk_expr(child)


def refs_of(expr: Expr) -> Set[int]:
    found: Set[int] = set()
    for node in walk_expr(expr):
        if isinstance(node, Ref):
            found.add(node.index)
    return found


def has_aggregate_placeholder(expr: Expr) -> bool:
    return any(isinstance(node, AggRef) for node in walk_expr(expr))


@dataclass(frozen=True)
class AggRef(Expr):
    """聚合结果的占位：聚合算子产出后，按槽位取值。"""

    slot: int
    label: str

    def eval(self, row: Sequence[Value]) -> Value:
        return row[self.slot]

    def render(self) -> str:
        return self.label

    def refs(self) -> Set[int]:
        return {self.slot}


def simplify(expr: Expr) -> Expr:
    """常量折叠：能在计划期算掉的，就不留到执行期。"""
    if isinstance(expr, BinaryOp):
        left = simplify(expr.left)
        right = simplify(expr.right)
        if isinstance(left, Const) and isinstance(right, Const):
            folded = BinaryOp(expr.op, left, right)
            try:
                return Const(folded.eval(()))
            except EvalError:
                return folded
        return BinaryOp(expr.op, left, right)
    if isinstance(expr, UnaryOp):
        operand = simplify(expr.operand)
        if isinstance(operand, Const):
            try:
                return Const(UnaryOp(expr.op, operand).eval(()))
            except EvalError:
                return UnaryOp(expr.op, operand)
        return UnaryOp(expr.op, operand)
    if isinstance(expr, Call):
        args = tuple(simplify(arg) for arg in expr.args)
        if all(isinstance(arg, Const) for arg in args):
            try:
                return Const(Call(expr.name, expr.spec, args).eval(()))
            except EvalError:
                return Call(expr.name, expr.spec, args)
        return Call(expr.name, expr.spec, args)
    if isinstance(expr, IsNullExpr):
        return IsNullExpr(simplify(expr.operand), expr.negated)
    return expr
