"""Banyan：一套带成本模型的 SQL 查询引擎。

    >>> from banyan import Engine, sampledata
    >>> engine = Engine(sampledata.build(scale=0.2))
    >>> result = engine.query("SELECT city, COUNT(*) FROM customers GROUP BY city LIMIT 3")
    >>> len(result)                     # 3
    >>> engine.explain("SELECT * FROM orders LIMIT 10").splitlines()[0]

设计上是"能被读懂的数据库"：解析器、逻辑计划、基于统计的代价模型、火山模型
执行器，每一层都是独立的模块，可以单独读、单独测。
"""

from __future__ import annotations

__version__ = "1.0.0"

from .catalog import Column, Database, Schema, Table
from .engine import Engine, Finding, Result, run_sql
from .errors import BindError, EvalError, ParseError, PlanError, SQLError
from .executor import Runtime, execute
from .parser import parse
from .values import FLOAT, INT, TEXT
from .planner import explain, plan_query

__all__ = [
    "BindError",
    "Column",
    "Database",
    "Engine",
    "EvalError",
    "Finding",
    "ParseError",
    "PlanError",
    "Result",
    "Runtime",
    "SQLError",
    "Schema",
    "FLOAT",
    "INT",
    "TEXT",
    "Table",
    "__version__",
    "execute",
    "explain",
    "parse",
    "plan_query",
    "run_sql",
]
