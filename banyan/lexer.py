"""词法分析。

SQL 的词法比看起来麻烦一点：字符串用单引号且 ``''`` 表示一个引号；
标识符可以加双引号来保住大小写和空格；``<=`` / ``<>`` / ``||`` 这类双字符
运算符要先于单字符匹配。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .errors import ParseError

WORD = "word"
NUMBER = "number"
STRING = "string"
OP = "op"
PUNCT = "punct"
EOF = "eof"

_TWO_CHAR = ("<=", ">=", "<>", "!=", "||")
_ONE_CHAR = "=<>+-*/%(),.;"


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    pos: int
    value: object = None

    @property
    def upper(self) -> str:
        return self.text.upper()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Token({self.kind}, {self.text!r})"


def tokenize(sql: str) -> List[Token]:
    tokens: List[Token] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]

        if char in " \t\r\n":
            index += 1
            continue

        # -- 行注释 与 /* 块注释 */
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            index = length if end < 0 else end + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise ParseError("块注释没有闭合", index, sql)
            index = end + 2
            continue

        # 单引号字符串，'' 转义一个单引号
        if char == "'":
            start = index
            index += 1
            pieces: List[str] = []
            while True:
                if index >= length:
                    raise ParseError("字符串没有闭合", start, sql)
                current = sql[index]
                if current == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        pieces.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                pieces.append(current)
                index += 1
            tokens.append(Token(STRING, sql[start:index], start, "".join(pieces)))
            continue

        # 双引号标识符
        if char == '"':
            start = index
            index += 1
            pieces = []
            while True:
                if index >= length:
                    raise ParseError("标识符没有闭合", start, sql)
                current = sql[index]
                if current == '"':
                    if index + 1 < length and sql[index + 1] == '"':
                        pieces.append('"')
                        index += 2
                        continue
                    index += 1
                    break
                pieces.append(current)
                index += 1
            tokens.append(Token(WORD, sql[start:index], start, "".join(pieces)))
            continue

        # 数字：整数、小数、科学计数法
        if char.isdigit() or (char == "." and index + 1 < length and sql[index + 1].isdigit()):
            start = index
            while index < length and sql[index].isdigit():
                index += 1
            is_float = False
            if index < length and sql[index] == ".":
                is_float = True
                index += 1
                while index < length and sql[index].isdigit():
                    index += 1
            if index < length and sql[index] in "eE":
                look = index + 1
                if look < length and sql[look] in "+-":
                    look += 1
                if look < length and sql[look].isdigit():
                    is_float = True
                    index = look
                    while index < length and sql[index].isdigit():
                        index += 1
            text = sql[start:index]
            value = float(text) if is_float else int(text)
            tokens.append(Token(NUMBER, text, start, value))
            continue

        # 标识符 / 关键字
        if char.isalpha() or char == "_":
            start = index
            while index < length and (sql[index].isalnum() or sql[index] == "_"):
                index += 1
            text = sql[start:index]
            tokens.append(Token(WORD, text, start, text))
            continue

        two = sql[index:index + 2]
        if two in _TWO_CHAR:
            tokens.append(Token(OP, two, index))
            index += 2
            continue

        if char in _ONE_CHAR:
            kind = PUNCT if char in "(),.;" else OP
            tokens.append(Token(kind, char, index))
            index += 1
            continue

        raise ParseError(f"不认识的字符 {char!r}", index, sql)

    tokens.append(Token(EOF, "", length))
    return tokens
