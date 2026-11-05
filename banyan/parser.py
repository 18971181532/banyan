"""递归下降语法分析。

只支持 SELECT，但尽量把常见写法都吃下来：多表 JOIN（INNER / LEFT / CROSS）、
聚合、CASE WHEN、IN / BETWEEN / LIKE、以及 ``SELECT *`` 这类展开。

优先级（低到高）::

    OR  <  AND  <  NOT  <  比较  <  ||  <  + -  <  * / %  <  一元  <  原子
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from . import ast
from .errors import ParseError
from .lexer import EOF, NUMBER, OP, PUNCT, STRING, WORD, Token, tokenize

_RESERVED = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER", "LIMIT", "OFFSET",
    "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "CROSS", "ON", "AS", "UNION",
    "ASC", "DESC", "AND", "OR", "NOT", "IN", "IS", "NULL", "BETWEEN", "LIKE",
    "DISTINCT", "CASE", "WHEN", "THEN", "ELSE", "END",
}

_JOIN_STARTERS = {"JOIN", "INNER", "LEFT", "RIGHT", "CROSS", "OUTER"}

# 这些词后面即使跟着左括号也不是函数调用
_NOT_FUNCTION = {
    "AND", "OR", "NOT", "IN", "BETWEEN", "LIKE", "IS", "CASE", "WHEN", "THEN",
    "ELSE", "END", "SELECT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER",
    "LIMIT", "OFFSET", "ON", "AS", "JOIN", "INNER", "OUTER", "CROSS", "UNION",
}


class Parser:
    def __init__(self, sql: str):
        self.sql = sql
        self.tokens: List[Token] = tokenize(sql)
        self.index = 0

    # -- 基础操作 ---------------------------------------------------------
    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def peek(self, offset: int = 1) -> Token:
        position = self.index + offset
        return self.tokens[min(position, len(self.tokens) - 1)]

    def advance(self) -> Token:
        token = self.current
        if token.kind != EOF:
            self.index += 1
        return token

    def at_word(self, *words: str) -> bool:
        token = self.current
        return token.kind == WORD and token.upper in words

    def match_word(self, *words: str) -> Optional[Token]:
        if self.at_word(*words):
            return self.advance()
        return None

    def expect_word(self, word: str) -> Token:
        token = self.match_word(word)
        if token is None:
            raise self.error(f"这里需要 {word}")
        return token

    def at_op(self, *ops: str) -> bool:
        token = self.current
        return token.kind == OP and token.text in ops

    def match_op(self, *ops: str) -> Optional[Token]:
        if self.at_op(*ops):
            return self.advance()
        return None

    def expect_op(self, op: str) -> Token:
        token = self.match_op(op)
        if token is None:
            raise self.error(f"这里需要 {op!r}")
        return token

    def at_punct(self, char: str) -> bool:
        token = self.current
        return token.kind == PUNCT and token.text == char

    def match_punct(self, char: str) -> Optional[Token]:
        if self.at_punct(char):
            return self.advance()
        return None

    def expect_punct(self, char: str) -> Token:
        token = self.match_punct(char)
        if token is None:
            raise self.error(f"这里需要 {char!r}")
        return token

    def error(self, message: str, token: Optional[Token] = None) -> ParseError:
        token = token or self.current
        found = "语句结尾" if token.kind == EOF else f"{token.text!r}"
        return ParseError(f"{message}，但读到 {found}", token.pos, self.sql)

    def identifier(self, what: str = "名字") -> str:
        token = self.current
        if token.kind != WORD:
            raise self.error(f"这里需要{what}")
        quoted = token.text.startswith('"')
        if not quoted and token.upper in _RESERVED:
            raise self.error(f"{token.text} 是保留字，不能当作{what}")
        self.advance()
        return token.value if isinstance(token.value, str) else token.text

    # -- 语句 -------------------------------------------------------------
    def parse(self) -> ast.Select:
        statement = self.select()
        self.match_punct(";")
        if self.current.kind != EOF:
            raise self.error("语句已经结束，后面还有多余的内容")
        return statement

    def select(self) -> ast.Select:
        self.expect_word("SELECT")
        distinct = self.match_word("DISTINCT") is not None
        items = self.select_items()
        # FROM 可以省：此时只有一行、零列，用来算 `SELECT 1+1` 这类表达式
        source = self.from_clause() if self.match_word("FROM") else None

        where = None
        if self.match_word("WHERE"):
            where = self.expression()

        group_by: Sequence[ast.Node] = ()
        if self.match_word("GROUP"):
            self.expect_word("BY")
            group_by = self.expression_list()

        having = None
        if self.match_word("HAVING"):
            having = self.expression()

        order_by: Sequence[ast.OrderItem] = ()
        if self.match_word("ORDER"):
            self.expect_word("BY")
            order_by = self.order_list()

        limit = None
        offset = 0
        if self.match_word("LIMIT"):
            limit = self.integer_literal("LIMIT 需要一个整数")
        if self.match_word("OFFSET"):
            offset = self.integer_literal("OFFSET 需要一个整数")

        return ast.Select(
            items=tuple(items),
            source=source,
            where=where,
            group_by=tuple(group_by),
            having=having,
            order_by=tuple(order_by),
            limit=limit,
            offset=offset,
            distinct=distinct,
        )

    def integer_literal(self, message: str) -> int:
        token = self.current
        if token.kind != NUMBER or not isinstance(token.value, int):
            raise self.error(message)
        self.advance()
        return int(token.value)

    def select_items(self) -> List[ast.SelectItem]:
        items = [self.select_item()]
        while self.match_punct(","):
            items.append(self.select_item())
        return items

    def select_item(self) -> ast.SelectItem:
        if self.at_op("*"):
            self.advance()
            return ast.SelectItem(ast.Star())
        # t.* 形式
        if self.current.kind == WORD and self.peek().kind == PUNCT and self.peek().text == ".":
            if self.peek(2).kind == OP and self.peek(2).text == "*":
                table = self.advance().value
                self.advance()
                self.advance()
                return ast.SelectItem(ast.Star(table))

        expression = self.expression()
        alias = None
        if self.match_word("AS"):
            alias = self.identifier("列别名")
        elif self.current.kind == WORD and self.current.upper not in _RESERVED:
            alias = self.advance().value
        return ast.SelectItem(expression, alias)

    def from_clause(self) -> ast.Node:
        node = self.table_ref()
        while True:
            if self.match_punct(","):
                right = self.table_ref()
                node = ast.Join(node, right, "cross", None)
                continue
            if self.at_word(*_JOIN_STARTERS):
                node = self.join(node)
                continue
            break
        return node

    def join(self, left: ast.Node) -> ast.Node:
        kind = "inner"
        if self.match_word("LEFT"):
            self.match_word("OUTER")
            kind = "left"
        elif self.match_word("INNER"):
            kind = "inner"
        elif self.match_word("CROSS"):
            self.expect_word("JOIN")
            return ast.Join(left, self.table_ref(), "cross", None)
        elif self.match_word("RIGHT"):
            raise self.error("暂不支持 RIGHT JOIN，可以改写成一个 LEFT JOIN")
        self.expect_word("JOIN")
        right = self.table_ref()
        on = None
        if self.match_word("ON"):
            on = self.expression()
        elif kind != "cross":
            raise self.error("JOIN 后面需要 ON 条件")
        return ast.Join(left, right, kind, on)

    def table_ref(self) -> ast.TableRef:
        name = self.identifier("表名")
        alias = None
        if self.match_word("AS"):
            alias = self.identifier("表别名")
        elif self.current.kind == WORD and self.current.upper not in _RESERVED:
            alias = self.advance().value
        return ast.TableRef(name, alias)

    def order_list(self) -> List[ast.OrderItem]:
        items = [self.order_item()]
        while self.match_punct(","):
            items.append(self.order_item())
        return items

    def order_item(self) -> ast.OrderItem:
        expression = self.expression()
        descending = False
        if self.match_word("DESC"):
            descending = True
        else:
            self.match_word("ASC")
        return ast.OrderItem(expression, descending)

    # -- 表达式 -----------------------------------------------------------
    def expression(self) -> ast.Node:
        return self.or_expr()

    def expression_list(self) -> List[ast.Node]:
        items = [self.expression()]
        while self.match_punct(","):
            items.append(self.expression())
        return items

    def or_expr(self) -> ast.Node:
        node = self.and_expr()
        while self.match_word("OR"):
            node = ast.Binary("OR", node, self.and_expr())
        return node

    def and_expr(self) -> ast.Node:
        node = self.not_expr()
        while self.match_word("AND"):
            node = ast.Binary("AND", node, self.not_expr())
        return node

    def not_expr(self) -> ast.Node:
        if self.match_word("NOT"):
            return ast.Unary("NOT", self.not_expr())
        return self.comparison()

    def comparison(self) -> ast.Node:
        node = self.concat()
        while True:
            if self.at_op("=", "<>", "!=", "<", "<=", ">", ">="):
                op = self.advance().text
                if op == "!=":
                    op = "<>"
                node = ast.Binary(op, node, self.concat())
                continue
            if self.at_word("IS"):
                self.advance()
                negated = self.match_word("NOT") is not None
                self.expect_word("NULL")
                node = ast.IsNull(node, negated)
                continue
            negated = False
            if self.at_word("NOT") and self.peek().kind == WORD and self.peek().upper in ("IN", "BETWEEN", "LIKE"):
                self.advance()
                negated = True
            if self.match_word("IN"):
                node = self.in_list(node, negated)
                continue
            if self.match_word("BETWEEN"):
                low = self.concat()
                self.expect_word("AND")
                high = self.concat()
                node = ast.Between(node, low, high, negated)
                continue
            if self.match_word("LIKE"):
                node = ast.Like(node, self.concat(), negated)
                continue
            if negated:  # NOT 后面不是 IN/BETWEEN/LIKE，语法错误
                raise self.error("NOT 后面需要 IN、BETWEEN 或 LIKE")
            break
        return node

    def in_list(self, operand: ast.Node, negated: bool) -> ast.Node:
        self.expect_punct("(")
        if self.at_word("SELECT"):
            raise self.error("暂不支持 IN 子查询")
        items = [self.expression()]
        while self.match_punct(","):
            items.append(self.expression())
        self.expect_punct(")")
        return ast.InList(operand, tuple(items), negated)

    def concat(self) -> ast.Node:
        node = self.additive()
        while self.at_op("||"):
            self.advance()
            node = ast.Binary("||", node, self.additive())
        return node

    def additive(self) -> ast.Node:
        node = self.multiplicative()
        while self.at_op("+", "-"):
            op = self.advance().text
            node = ast.Binary(op, node, self.multiplicative())
        return node

    def multiplicative(self) -> ast.Node:
        node = self.unary()
        while self.at_op("*", "/", "%"):
            op = self.advance().text
            node = ast.Binary(op, node, self.unary())
        return node

    def unary(self) -> ast.Node:
        if self.match_op("-"):
            return ast.Unary("-", self.unary())
        if self.match_op("+"):
            return self.unary()
        return self.primary()

    def primary(self) -> ast.Node:
        token = self.current

        if token.kind == NUMBER:
            self.advance()
            return ast.Literal(token.value)

        if token.kind == STRING:
            self.advance()
            return ast.Literal(token.value)

        if self.match_punct("("):
            node = self.expression()
            self.expect_punct(")")
            return node

        if token.kind == WORD:
            upper = token.upper
            if upper == "NULL":
                self.advance()
                return ast.Literal(None)
            if upper in ("TRUE", "FALSE"):
                self.advance()
                return ast.Literal(upper == "TRUE")
            if upper == "CASE":
                return self.case_expr()

            # 函数调用：名字后面紧跟左括号
            if self.peek().kind == PUNCT and self.peek().text == "(" and upper not in _NOT_FUNCTION:
                return self.function_call()

            # 列引用：可能带表前缀
            if upper in _RESERVED:
                raise self.error(f"{token.text} 不能出现在这里")
            self.advance()
            name = token.text
            if self.match_punct("."):
                if self.at_op("*"):
                    self.advance()
                    return ast.Star(name)
                column = self.identifier("列名")
                return ast.Column(column, name)
            return ast.Column(name)

        raise self.error("这里需要一个表达式")

    def function_call(self) -> ast.Function:
        name = self.advance().text
        self.expect_punct("(")
        distinct = self.match_word("DISTINCT") is not None
        if self.at_op("*"):
            self.advance()
            self.expect_punct(")")
            return ast.Function(name, (), star=True, distinct=distinct)
        if self.match_punct(")"):
            return ast.Function(name, (), False, distinct)
        args = [self.expression()]
        while self.match_punct(","):
            args.append(self.expression())
        self.expect_punct(")")
        return ast.Function(name, tuple(args), False, distinct)

    def case_expr(self) -> ast.Case:
        self.expect_word("CASE")
        whens = []
        while self.match_word("WHEN"):
            condition = self.expression()
            self.expect_word("THEN")
            result = self.expression()
            whens.append((condition, result))
        if not whens:
            raise self.error("CASE 至少要有一个 WHEN")
        default = None
        if self.match_word("ELSE"):
            default = self.expression()
        self.expect_word("END")
        return ast.Case(tuple(whens), default)


def parse(sql: str) -> ast.Select:
    return Parser(sql).parse()
