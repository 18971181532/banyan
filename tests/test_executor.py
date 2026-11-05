"""执行器测试：算子语义、NULL 处理、以及各种边界。"""

from __future__ import annotations

import unittest

from banyan import ast
from banyan.executor import Runtime, execute
from banyan.planner import Project, Scan
from tests import support


class ScanAndFilterTests(unittest.TestCase):
    def test_select_all(self):
        self.assertEqual(len(support.rows("SELECT * FROM dept")), 5)

    def test_projection_keeps_requested_order(self):
        result = support.engine().query("SELECT city, name FROM dept")
        self.assertEqual(result.columns, ["city", "name"])

    def test_where_equality(self):
        self.assertEqual(support.rows("SELECT name FROM dept WHERE city = '北京'"),
                         [("研发",), ("财务",)])

    def test_where_comparison(self):
        self.assertEqual(support.one("SELECT COUNT(*) FROM emp WHERE salary > 20000"), 3)

    def test_where_and_or(self):
        self.assertEqual(
            support.one("SELECT COUNT(*) FROM emp WHERE dept_id = 1 AND salary > 25000"), 2)
        self.assertEqual(
            support.one("SELECT COUNT(*) FROM emp WHERE dept_id = 2 OR dept_id = 3"), 3)

    def test_null_comparison_never_matches(self):
        self.assertEqual(support.one("SELECT COUNT(*) FROM emp WHERE salary = NULL"), 0)
        self.assertEqual(support.one("SELECT COUNT(*) FROM emp WHERE salary > 0"), 7)

    def test_is_null(self):
        self.assertEqual(support.one("SELECT COUNT(*) FROM emp WHERE salary IS NULL"), 1)
        self.assertEqual(support.one("SELECT COUNT(*) FROM emp WHERE salary IS NOT NULL"), 7)

    def test_in_and_not_in(self):
        self.assertEqual(support.one("SELECT COUNT(*) FROM emp WHERE dept_id IN (1, 2)"), 6)
        self.assertEqual(support.one("SELECT COUNT(*) FROM emp WHERE dept_id NOT IN (1, 2)"), 2)

    def test_in_with_null_never_matches(self):
        self.assertEqual(support.one("SELECT COUNT(*) FROM emp WHERE salary IN (32000, NULL)"), 1)

    def test_between(self):
        self.assertEqual(
            support.one("SELECT COUNT(*) FROM emp WHERE salary BETWEEN 15000 AND 21000"), 4)

    def test_like(self):
        self.assertEqual(support.one("SELECT COUNT(*) FROM dept WHERE name LIKE '市%'"), 1)
        self.assertEqual(support.one("SELECT COUNT(*) FROM dept WHERE name LIKE '%务'"), 1)

    def test_arithmetic_and_concat(self):
        self.assertEqual(support.one("SELECT salary * 2 FROM emp WHERE id = 1"), 64000)
        self.assertEqual(support.one("SELECT name || '@' || city FROM dept WHERE id = 1"),
                         "研发@北京")

    def test_case_expression(self):
        rows = support.rows("SELECT CASE WHEN salary > 20000 THEN '高' ELSE '低' END FROM emp"
                            " WHERE id <= 3")
        self.assertEqual(rows, [("高",), ("高",), ("高",)])

    def test_limit_and_offset(self):
        self.assertEqual(support.one("SELECT id FROM emp ORDER BY id LIMIT 1 OFFSET 2"), 3)

    def test_limit_zero_returns_nothing(self):
        self.assertEqual(support.rows("SELECT id FROM emp LIMIT 0"), [])

    def test_offset_beyond_end(self):
        self.assertEqual(support.rows("SELECT id FROM emp LIMIT 5 OFFSET 99"), [])


class JoinTests(unittest.TestCase):
    def test_inner_join_row_count(self):
        self.assertEqual(support.one(
            "SELECT COUNT(*) FROM emp e JOIN dept d ON e.dept_id = d.id"), 8)

    def test_inner_join_drops_unmatched_left(self):
        self.assertEqual(support.one(
            "SELECT COUNT(*) FROM dept d JOIN emp e ON d.id = e.dept_id"), 8)

    def test_left_join_pads_nulls(self):
        self.assertEqual(support.one(
            "SELECT COUNT(*) FROM dept d LEFT JOIN emp e ON d.id = e.dept_id"), 9)

    def test_left_join_null_row_is_really_null(self):
        rows = support.rows(
            "SELECT e.name FROM dept d LEFT JOIN emp e ON d.id = e.dept_id "
            "WHERE d.name = '新设'")
        self.assertEqual(rows, [(None,)])

    def test_left_join_with_extra_on_condition(self):
        # 每个部门仍然各出一行（或更多），没匹配上的补 NULL
        self.assertEqual(support.one(
            "SELECT COUNT(DISTINCT d.id) FROM dept d LEFT JOIN emp e "
            "ON d.id = e.dept_id AND e.salary > 25000"), 5)

    def test_where_on_right_side_of_left_join_filters(self):
        self.assertEqual(support.one(
            "SELECT COUNT(*) FROM dept d LEFT JOIN emp e ON d.id = e.dept_id "
            "WHERE e.salary > 25000"), 2)

    def test_cross_join(self):
        self.assertEqual(support.one("SELECT COUNT(*) FROM dept, emp"), 40)

    def test_comma_join_with_where_becomes_inner(self):
        self.assertEqual(support.one(
            "SELECT COUNT(*) FROM dept, emp WHERE dept.id = emp.dept_id"), 8)

    def test_three_way_join(self):
        self.assertEqual(support.one(
            "SELECT COUNT(*) FROM emp e JOIN dept d ON e.dept_id = d.id "
            "JOIN proj p ON p.dept_id = d.id"), 11)

    def test_join_output_column_order_is_left_then_right(self):
        rows = support.rows("SELECT d.name, e.name FROM dept d JOIN emp e "
                            "ON d.id = e.dept_id WHERE e.id = 1")
        self.assertEqual(rows, [("研发", "张伟")])

    def test_multi_key_join(self):
        self.assertEqual(support.one(
            "SELECT COUNT(*) FROM emp e JOIN skill s ON e.id = s.emp_id AND e.name = s.name"),
            0)


class AggregateTests(unittest.TestCase):
    def test_count_star_counts_rows(self):
        self.assertEqual(support.one("SELECT COUNT(*) FROM emp"), 8)

    def test_count_column_skips_nulls(self):
        self.assertEqual(support.one("SELECT COUNT(salary) FROM emp"), 7)

    def test_sum_skips_nulls(self):
        self.assertEqual(support.one("SELECT SUM(salary) FROM emp"), 142000)

    def test_avg_skips_nulls(self):
        self.assertAlmostEqual(support.one("SELECT AVG(salary) FROM emp"), 142000 / 7)

    def test_min_max(self):
        self.assertEqual(support.one("SELECT MIN(salary) FROM emp"), 12000)
        self.assertEqual(support.one("SELECT MAX(salary) FROM emp"), 32000)

    def test_sum_of_empty_input_is_null(self):
        self.assertIsNone(support.one("SELECT SUM(salary) FROM emp WHERE id > 100"))

    def test_count_of_empty_input_is_zero(self):
        self.assertEqual(support.one("SELECT COUNT(*) FROM emp WHERE id > 100"), 0)

    def test_group_by(self):
        rows = support.rows("SELECT dept_id, COUNT(*) FROM emp GROUP BY dept_id ORDER BY dept_id")
        self.assertEqual(rows, [(1, 4), (2, 2), (3, 1), (4, 1)])

    def test_group_by_with_empty_result(self):
        self.assertEqual(support.rows(
            "SELECT dept_id, COUNT(*) FROM emp WHERE id > 100 GROUP BY dept_id"), [])

    def test_group_by_two_columns(self):
        rows = support.rows("SELECT dept_id, level, COUNT(*) FROM emp "
                            "GROUP BY dept_id, level ORDER BY dept_id, level")
        self.assertEqual(rows, [(1, "P5", 1), (1, "P6", 2), (1, "P7", 1),
                                (2, "P4", 1), (2, "P5", 1),
                                (3, "P4", 1), (4, "P5", 1)])

    def test_count_distinct(self):
        self.assertEqual(support.one("SELECT COUNT(DISTINCT level) FROM emp"), 4)

    def test_sum_distinct(self):
        self.assertEqual(support.one("SELECT SUM(DISTINCT dept_id) FROM emp"), 10)

    def test_avg_distinct(self):
        self.assertAlmostEqual(support.one("SELECT AVG(DISTINCT dept_id) FROM emp"), 2.5)

    def test_having(self):
        rows = support.rows("SELECT dept_id FROM emp GROUP BY dept_id HAVING COUNT(*) > 2")
        self.assertEqual(rows, [(1,)])

    def test_aggregate_and_group_key_together(self):
        rows = support.rows("SELECT dept_id, MAX(salary) FROM emp GROUP BY dept_id "
                            "ORDER BY dept_id")
        self.assertEqual(rows, [(1, 32000), (2, 18000), (3, 12000), (4, 16000)])

    def test_aggregate_of_expression(self):
        self.assertEqual(support.one("SELECT SUM(salary * 2) FROM emp"), 284000)

    def test_ungrouped_column_is_rejected(self):
        self.assertEqual(support.error_of("SELECT name, COUNT(*) FROM emp"),
                         "PlanError")

    def test_order_by_alias_of_aggregate(self):
        rows = support.rows("SELECT dept_id, COUNT(*) AS n FROM emp GROUP BY dept_id "
                            "ORDER BY n DESC, dept_id")
        self.assertEqual(rows[0], (1, 4))


class SortAndDistinctTests(unittest.TestCase):
    def test_order_asc_desc(self):
        self.assertEqual(support.column("SELECT id FROM emp ORDER BY id DESC LIMIT 2"), [8, 7])
        self.assertEqual(support.column("SELECT id FROM emp ORDER BY id LIMIT 2"), [1, 2])

    def test_nulls_sort_first_ascending(self):
        rows = support.rows("SELECT id FROM emp ORDER BY salary, id LIMIT 2")
        self.assertEqual(rows[0], (8,))

    def test_nulls_sort_last_descending(self):
        rows = support.rows("SELECT id FROM emp ORDER BY salary DESC LIMIT 1")
        self.assertEqual(rows[0], (1,))

    def test_multi_key_sort(self):
        rows = support.rows("SELECT dept_id, id FROM emp ORDER BY dept_id, id DESC")
        self.assertEqual(rows[0], (1, 8))

    def test_sort_by_expression_not_in_select(self):
        rows = support.rows("SELECT name FROM emp ORDER BY salary DESC LIMIT 1")
        self.assertEqual(rows, [("张伟",)])

    def test_sort_by_ordinal(self):
        # 按姓名排序，'刘' 的码点最小
        self.assertEqual(support.column("SELECT id, name FROM emp ORDER BY 2 LIMIT 1")[0], 4)

    def test_distinct(self):
        self.assertEqual(support.column("SELECT DISTINCT dept_id FROM emp ORDER BY dept_id"),
                         [1, 2, 3, 4])

    def test_distinct_with_nulls(self):
        rows = support.rows("SELECT DISTINCT salary IS NULL FROM emp")
        self.assertEqual(len(rows), 2)

    def test_distinct_with_order_by_output_column(self):
        self.assertEqual(
            support.column("SELECT DISTINCT dept_id FROM emp ORDER BY dept_id DESC"), [4, 3, 2, 1])

    def test_distinct_order_by_hidden_column_is_rejected(self):
        self.assertEqual(
            support.error_of("SELECT DISTINCT dept_id FROM emp ORDER BY salary"), "PlanError")


class StarTests(unittest.TestCase):
    def test_star_expands_all_columns(self):
        result = support.engine().query("SELECT * FROM dept LIMIT 1")
        self.assertEqual(result.columns, ["id", "name", "city"])

    def test_qualified_star(self):
        result = support.engine().query("SELECT d.* FROM dept d LIMIT 1")
        self.assertEqual(result.columns, ["id", "name", "city"])

    def test_star_over_join_qualifies_duplicates(self):
        result = support.engine().query("SELECT * FROM dept d JOIN emp e ON d.id = e.dept_id LIMIT 1")
        self.assertIn("d.id", result.columns)
        self.assertIn("e.id", result.columns)

    def test_star_with_alias(self):
        result = support.engine().query("SELECT * FROM dept d LIMIT 1")
        self.assertEqual(len(result.rows[0]), 3)


class RuntimeTests(unittest.TestCase):
    def test_counts_are_recorded_per_operator(self):
        result = support.engine().query("SELECT id FROM emp WHERE salary > 20000")
        self.assertEqual(result.plan.actual_rows, 3)

    def test_execute_is_lazy(self):
        scan = Scan(support.database().table("dept"), "dept")
        plan = Project(scan, [])
        rows = list(execute(plan, Runtime()))
        self.assertEqual(rows, [(), (), (), (), ()])

    def test_runtime_counts_rows(self):
        scan = Scan(support.database().table("dept"), "dept")
        runtime = Runtime()
        self.assertEqual(len(list(execute(scan, runtime))), 5)
        self.assertEqual(runtime.produced(scan), 5)

    def test_stopping_early_does_not_consume_everything(self):
        result = support.engine().query("SELECT id FROM emp LIMIT 2")
        scans = [node for node in result.plan.walk() if node.kind == "扫描"]
        # 只拉了 2 行，扫描就真的只走了 2 行
        self.assertEqual(scans[0].actual_rows, 2)


if __name__ == "__main__":
    unittest.main()
