"""错误类型：带位置信息的 SQL 错误。

解析错误要指到具体字符，执行错误要带上原始片段——否则用户只能靠猜。
"""

from __future__ import annotations

from typing import Optional


class SQLError(Exception):
    """所有对外错误的基类。"""

    def __init__(self, message: str, position: Optional[int] = None, sql: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.position = position
        self.sql = sql

    def render(self) -> str:
        """带出错位置指针的多行文本。"""
        if self.position is None or self.sql is None:
            return self.message
        sql = self.sql.replace("\n", " ")
        position = max(0, min(self.position, len(sql)))
        caret = " " * position + "^"
        return f"{self.message}\n  {sql}\n  {caret}"


class ParseError(SQLError):
    pass


class BindError(SQLError):
    """名字解析失败：表不存在、列不存在、歧义、聚合用错位置。"""


class PlanError(SQLError):
    """逻辑上无法执行的查询，比如没有 GROUP BY 却选了非聚合列。"""


class EvalError(SQLError):
    """运行期类型错误，比如文本参与算术。"""
