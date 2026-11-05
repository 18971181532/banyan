"""随机化性质测试。

这里不验证"某个查询返回什么"，而是验证**优化不改变语义**：

* 打开/关闭连接重排，结果必须一样；
* 打开/关闭谓词下推，结果必须一样；
* 表里行的物理顺序变了，结果必须一样；
* 去重后的行数必须等于手动去重后的行数。

这几条一旦破了，说明优化器改坏了计划——而这类 bug 用逐个用例很难覆盖。
"""

from __future__ import annotations

import random
import unittest

from banyan import Engine
from banyan.catalog import Column, Database, Table
from banyan.values import FLOAT, INT, TEXT
from tests import support

TABLES = ("dept", "emp", "proj", "skill")

# 可用的连接边：(左表, 左列, 右表, 右列)
EDGES = [
    ("emp", "dept_id", "dept", "id"),
    ("emp", "id", "skill", "emp_id"),
    ("proj", "dept_id", "dept", "id"),
    ("proj", "dept_id", "emp", "dept_id"),
]

PREDICATES = {
    "emp": ["e.salary > 20000", "e.salary IS NULL", "e.level = 'P5'", "e.name LIKE '张%'"],
    "dept": ["d.city = '北京'", "d.name <> '研发'", "d.city IN ('北京', '上海')"],
    "proj": ["p.budget > 500000", "p.status = '进行中'"],
    "skill": ["s.name = 'SQL'", "s.name <> 'Excel'"],
}

COLUMNS = {
    "dept": "d.name, d.city",
    "emp": "e.id, e.salary",
    "proj": "p.budget",
    "skill": "s.name",
}

ALIASES = {"emp": "e", "dept": "d", "proj": "p", "skill": "s"}


def random_tables(rng):
    """随机挑一个**连通**的表集合（{dept, skill} 之间没有边，直接采样会连不起来）。"""
    chosen = [rng.choice(list(TABLES))]
    for _ in range(rng.randint(1, 3)):
        frontier = [edge for edge in EDGES
                    if (edge[0] in chosen) != (edge[2] in chosen)]
        if not frontier:
            break
        edge = rng.choice(frontier)
        new = edge[0] if edge[0] not in chosen else edge[2]
        if new not in chosen:
            chosen.append(new)
    return chosen


def build_sql(rng):
    tables = random_tables(rng)
    in_from = [tables[0]]
    remaining = set(tables[1:])
    parts = [f"{tables[0]} {ALIASES[tables[0]]}"]

    while remaining:
        candidates = [edge for edge in EDGES
                      if (edge[0] in in_from and edge[2] in remaining)
                      or (edge[2] in in_from and edge[0] in remaining)]
        left, left_col, right, right_col = rng.choice(candidates)
        new = right if left in in_from else left
        parts.append(f"JOIN {new} {ALIASES[new]} "
                     f"ON {ALIASES[left]}.{left_col} = {ALIASES[right]}.{right_col}")
        in_from.append(new)
        remaining.discard(new)

    sql = "SELECT " + ", ".join(COLUMNS[name] for name in sorted(tables))
    sql += " FROM " + " ".join(parts)

    conditions = []
    for name in tables:
        if rng.random() < 0.5:
            conditions.append(rng.choice(PREDICATES[name]))
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    return sql


def normalize(rows):
    return sorted(rows, key=repr)


class OptimizationEquivalenceTests(unittest.TestCase):
    def test_random_queries_agree_across_options(self):
        rng = random.Random(20240501)
        for index in range(24):
            sql = build_sql(rng)
            with self.subTest(index=index, sql=sql):
                answers = []
                for reorder in (True, False):
                    for push in (True, False):
                        engine = Engine(support.database(), reorder=reorder, push_down=push)
                        answers.append(normalize(engine.query(sql).rows))
                for other in answers[1:]:
                    self.assertEqual(answers[0], other, sql)

    def test_left_join_survives_reordering(self):
        sql = ("SELECT d.name, e.id FROM dept d LEFT JOIN emp e ON d.id = e.dept_id "
               "LEFT JOIN proj p ON p.dept_id = d.id")
        fast = support.rows(sql, reorder=True)
        slow = support.rows(sql, reorder=False)
        self.assertEqual(normalize(fast), normalize(slow))

    def test_aggregate_survives_reordering(self):
        sql = ("SELECT COUNT(*), SUM(e.salary), MAX(p.budget) FROM emp e "
               "JOIN dept d ON e.dept_id = d.id JOIN proj p ON p.dept_id = d.id "
               "WHERE d.city = '北京'")
        self.assertEqual(support.rows(sql, reorder=True),
                         support.rows(sql, reorder=False))


class RowOrderIndependenceTests(unittest.TestCase):
    def shuffled_database(self, rng):
        base = support.make_database()
        db = Database()
        for name in base.names():
            table = base.table(name)
            rows = list(table.rows)
            rng.shuffle(rows)
            db.add(Table(table.name, table.columns, rows))
        return db

    def test_physical_row_order_does_not_matter(self):
        rng = random.Random(7)
        shuffled = self.shuffled_database(rng)
        queries = [
            "SELECT COUNT(*), SUM(salary), AVG(salary) FROM emp",
            "SELECT dept_id, COUNT(*) FROM emp GROUP BY dept_id",
            "SELECT d.name, COUNT(*) FROM emp e JOIN dept d ON e.dept_id = d.id "
            "GROUP BY d.name",
            "SELECT e.name FROM emp e JOIN skill s ON s.emp_id = e.id",
            "SELECT DISTINCT dept_id FROM emp",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                self.assertEqual(normalize(Engine(shuffled).query(sql).rows),
                                 normalize(Engine(support.database()).query(sql).rows))

    def test_order_by_makes_output_stable(self):
        rng = random.Random(11)
        shuffled = self.shuffled_database(rng)
        sql = "SELECT id FROM emp ORDER BY id"
        self.assertEqual(Engine(shuffled).query(sql).rows,
                         Engine(support.database()).query(sql).rows)


class DistinctTests(unittest.TestCase):
    def test_distinct_matches_manual_dedupe(self):
        rows = support.rows("SELECT dept_id FROM emp")
        distinct = support.rows("SELECT DISTINCT dept_id FROM emp")
        self.assertEqual(normalize(distinct), normalize(set(rows)))

    def test_distinct_over_two_columns(self):
        rows = support.rows("SELECT dept_id, level FROM emp")
        distinct = support.rows("SELECT DISTINCT dept_id, level FROM emp")
        self.assertEqual(normalize(distinct), normalize(set(rows)))


class AggregateInvariantTests(unittest.TestCase):
    def test_count_equals_sum_of_group_counts(self):
        total = support.one("SELECT COUNT(*) FROM emp")
        grouped = support.rows("SELECT dept_id, COUNT(*) FROM emp GROUP BY dept_id")
        self.assertEqual(sum(row[1] for row in grouped), total)

    def test_sum_of_groups_equals_global_sum(self):
        total = support.one("SELECT SUM(salary) FROM emp")
        grouped = support.rows("SELECT dept_id, SUM(salary) FROM emp GROUP BY dept_id")
        self.assertEqual(sum(row[1] for row in grouped), total)

    def test_join_then_count_is_at_least_inner_count(self):
        inner = support.one("SELECT COUNT(*) FROM emp e JOIN dept d ON e.dept_id = d.id")
        left = support.one("SELECT COUNT(*) FROM dept d LEFT JOIN emp e ON d.id = e.dept_id")
        self.assertGreaterEqual(left, inner)

    def test_filter_is_monotonic(self):
        everything = support.one("SELECT COUNT(*) FROM emp")
        filtered = support.one("SELECT COUNT(*) FROM emp WHERE salary > 15000")
        self.assertLessEqual(filtered, everything)


if __name__ == "__main__":
    unittest.main()
