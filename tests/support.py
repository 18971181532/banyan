"""测试公共夹具：一套固定的小数据，结果可预期。

刻意不用 `sampledata` 的随机数据——测试要的是"算得对不对"，不是"跑得快不快"。
里面有 NULL 薪水、有空部门、有一个人多项技能，覆盖聚合、左连接、多对多的坑。
"""

from __future__ import annotations

from typing import Any, List, Sequence

from banyan import Engine
from banyan.catalog import Column, Database, Table
from banyan.errors import SQLError
from banyan.values import FLOAT, INT, TEXT

DEPT_ROWS = [
    (1, "研发", "北京"),
    (2, "销售", "上海"),
    (3, "市场", "广州"),
    (4, "财务", "北京"),
    (5, "新设", "深圳"),
]

EMP_ROWS = [
    (1, "张伟", 1, 32000, "P7"),
    (2, "李娜", 1, 28000, "P6"),
    (3, "王强", 1, 21000, "P5"),
    (4, "刘洋", 2, 15000, "P4"),
    (5, "陈静", 2, 18000, "P5"),
    (6, "杨帆", 3, 12000, "P4"),
    (7, "赵敏", 4, 16000, "P5"),
    (8, "周涛", 1, None, "P6"),
]

PROJ_ROWS = [
    (1, 1, 1200000.0, "进行中"),
    (2, 1, 800000.0, "已完成"),
    (3, 2, 300000.0, "进行中"),
    (4, 3, 150000.0, "暂停"),
]

SKILL_ROWS = [
    (1, "Python"), (1, "SQL"), (2, "SQL"), (3, "Java"),
    (4, "Excel"), (5, "Excel"), (6, "SQL"),
]


def make_database() -> Database:
    db = Database()
    db.add(Table("dept", [Column("id", INT), Column("name", TEXT), Column("city", TEXT)],
                 DEPT_ROWS))
    db.add(Table("emp", [Column("id", INT), Column("name", TEXT), Column("dept_id", INT),
                         Column("salary", INT), Column("level", TEXT)], EMP_ROWS))
    db.add(Table("proj", [Column("id", INT), Column("dept_id", INT),
                          Column("budget", FLOAT), Column("status", TEXT)], PROJ_ROWS))
    db.add(Table("skill", [Column("emp_id", INT), Column("name", TEXT)], SKILL_ROWS))
    return db


_DATABASE = None


def database() -> Database:
    """整个测试会话共用一个库，省掉每次重建统计信息的开销。"""
    global _DATABASE
    if _DATABASE is None:
        _DATABASE = make_database()
    return _DATABASE


def engine(**kwargs) -> Engine:
    return Engine(database(), **kwargs)


def rows(sql: str, **kwargs) -> List[Sequence[Any]]:
    return engine(**kwargs).query(sql).rows


def column(sql: str, index: int = 0, **kwargs) -> List[Any]:
    return [row[index] for row in rows(sql, **kwargs)]


def one(sql: str, **kwargs) -> Any:
    """取第一行第一列。"""
    result = rows(sql, **kwargs)
    assert len(result) == 1, f"期望 1 行，实际 {len(result)} 行：{sql}"
    return result[0][0]


def error_of(sql: str) -> str:
    """跑一条应当失败的查询，返回错误类型的名字。"""
    try:
        engine().query(sql)
    except SQLError as exc:
        return type(exc).__name__
    raise AssertionError(f"这条 SQL 本该报错：{sql}")


def message_of(sql: str) -> str:
    try:
        engine().query(sql)
    except SQLError as exc:
        return exc.message
    raise AssertionError(f"这条 SQL 本该报错：{sql}")


def multiset(values):
    return sorted(values, key=repr)
