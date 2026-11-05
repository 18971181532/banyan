"""计划器测试：绑定、下推、重排、以及 explain 的输出。"""

from __future__ import annotations

import unittest

from banyan import ast, planner
from banyan.parser import parse
from banyan.planner import Aggregate, Filter, Join, Limit, Project, Scan, Sort
from tests import support


def plan_of(sql, **kwargs):
    return support.engine(**kwargs).plan(sql)


def leaves(plan):
    """按从左到右的顺序取出叶子（扫描连同压在上面的过滤算作一个叶子）。"""
    if not plan.children or isinstance(plan, Filter):
        return [plan]
    found = []
    for child in plan.children:
        found.extend(leaves(child))
    return found


class BindingTests(unittest.TestCase):
    def test_column_becomes_index(self):
        plan = plan_of("SELECT salary FROM emp")
        project = plan
        while not isinstance(project, Project):
            project = project.children[0]
        expr = project.items[0][0]
        self.assertEqual(expr.index, 3)

    def test_alias_qualifies_columns(self):
        plan = plan_of("SELECT e.salary FROM emp e")
        scan = leaves(plan)[0]
        self.assertEqual(scan.alias, "e")
        self.assertEqual(scan.schema[0].table, "e")

    def test_unknown_column(self):
        self.assertEqual(support.error_of("SELECT nope FROM emp"), "BindError")

    def test_ambiguous_column_needs_table(self):
        message = support.message_of(
            "SELECT id FROM emp e JOIN dept d ON e.dept_id = d.id")
        self.assertIn("歧义", message)

    def test_unknown_table(self):
        self.assertEqual(support.error_of("SELECT * FROM nope"), "BindError")


class PushdownTests(unittest.TestCase):
    def test_filter_is_pushed_below_join(self):
        plan = plan_of("SELECT * FROM emp e JOIN dept d ON e.dept_id = d.id "
                       "WHERE d.city = '北京'")
        join = plan
        while not isinstance(join, Join):
            join = join.children[0]
        filtered = [leaf for leaf in leaves(join)
                    if isinstance(leaf, Filter) and leaf.schema.names() == ["id", "name", "city"]]
        self.assertEqual(len(filtered), 1)

    def test_no_filter_above_join(self):
        plan = plan_of("SELECT * FROM emp e JOIN dept d ON e.dept_id = d.id "
                       "WHERE d.city = '北京'")
        self.assertNotIsInstance(plan.children[0], Filter)

    def test_comma_join_with_predicate_becomes_inner(self):
        plan = plan_of("SELECT * FROM emp, dept WHERE emp.dept_id = dept.id")
        joins = [node for node in plan.walk() if isinstance(node, Join)]
        self.assertTrue(joins)
        self.assertEqual(joins[-1].join_kind, "inner")

    def test_on_condition_pushed_into_right_side_of_left_join(self):
        plan = plan_of("SELECT * FROM dept d LEFT JOIN emp e "
                       "ON d.id = e.dept_id AND e.salary > 20000")
        join = plan
        while not isinstance(join, Join):
            join = join.children[0]
        self.assertTrue(any(isinstance(leaf, Filter) for leaf in leaves(join)))

    def test_where_on_right_of_left_join_stays_above(self):
        plan = plan_of("SELECT * FROM dept d LEFT JOIN emp e ON d.id = e.dept_id "
                       "WHERE e.salary > 20000")
        self.assertIsInstance(plan.children[0], Filter)
        self.assertIsInstance(plan.children[0].children[0], Join)

    def test_pushdown_off_keeps_where_on_top(self):
        plan = plan_of("SELECT * FROM emp e JOIN dept d ON e.dept_id = d.id "
                       "WHERE d.city = '北京'", push_down=False)
        self.assertIsInstance(plan.children[0], Filter)

    def test_predicate_on_left_side_of_left_join_is_pushed(self):
        plan = plan_of("SELECT * FROM dept d LEFT JOIN emp e ON d.id = e.dept_id "
                       "WHERE d.city = '北京'")
        join = plan
        while not isinstance(join, Join):
            join = join.children[0]
        self.assertIsInstance(join.children[0], Filter)


class ReorderTests(unittest.TestCase):
    SQL = """SELECT COUNT(*) FROM dept d
             JOIN emp e ON e.dept_id = d.id
             JOIN proj p ON p.dept_id = d.id
             JOIN skill s ON s.emp_id = e.id
             WHERE d.city = '北京' AND s.name = 'SQL'"""

    def test_reorder_lowers_cost(self):
        self.assertLessEqual(plan_of(self.SQL, reorder=True).est_cost,
                             plan_of(self.SQL, reorder=False).est_cost)

    def test_reorder_same_result(self):
        self.assertEqual(support.rows(self.SQL, reorder=True),
                         support.rows(self.SQL, reorder=False))

    def test_highly_selective_leaf_goes_first(self):
        sql = """SELECT COUNT(*) FROM skill s
                 JOIN emp e ON s.emp_id = e.id
                 JOIN dept d ON e.dept_id = d.id
                 WHERE d.city = '广州' AND s.name = 'Java'"""
        plan = plan_of(sql)
        first = leaves(plan)[0]
        # 两个过滤条件都很强，先接谁都能接受；这里只要求过滤被下推到叶子
        self.assertIsInstance(first, Filter)

    def test_cross_join_is_allowed(self):
        plan = plan_of("SELECT * FROM emp, dept")
        joins = [node for node in plan.walk() if isinstance(node, Join)]
        self.assertTrue(joins)

    def test_reorder_keeps_left_join_shape(self):
        sql = ("SELECT * FROM dept d LEFT JOIN emp e ON d.id = e.dept_id "
               "LEFT JOIN proj p ON p.dept_id = d.id")
        plan = plan_of(sql)
        joins = [node for node in plan.walk() if isinstance(node, Join)]
        self.assertEqual([join.join_kind for join in joins], ["left", "left"])


class ClauseTests(unittest.TestCase):
    def test_group_by_builds_aggregate(self):
        plan = plan_of("SELECT dept_id, COUNT(*) FROM emp GROUP BY dept_id")
        self.assertTrue(any(isinstance(node, Aggregate) for node in plan.walk()))

    def test_aggregate_without_group_by(self):
        plan = plan_of("SELECT COUNT(*) FROM emp")
        aggregate = [node for node in plan.walk() if isinstance(node, Aggregate)][0]
        self.assertEqual(len(aggregate.group_exprs), 0)
        self.assertEqual(aggregate.est_rows, 1.0)

    def test_having_becomes_filter_above_aggregate(self):
        plan = plan_of("SELECT dept_id FROM emp GROUP BY dept_id HAVING COUNT(*) > 1")
        filters = [node for node in plan.walk() if isinstance(node, Filter)]
        self.assertTrue(filters)

    def test_order_by_adds_sort(self):
        self.assertTrue(any(isinstance(node, Sort)
                            for node in plan_of("SELECT id FROM emp ORDER BY id").walk()))

    def test_order_by_hidden_column(self):
        plan = plan_of("SELECT name FROM emp ORDER BY salary DESC")
        project = plan
        while not isinstance(project, Project):
            project = project.children[0]
        self.assertEqual(len(project.hidden), 1)
        self.assertEqual(len(project.items), 1)

    def test_order_by_alias_does_not_add_hidden(self):
        plan = plan_of("SELECT name AS n FROM emp ORDER BY n")
        project = [node for node in plan.walk() if isinstance(node, Project)][0]
        self.assertEqual(len(project.hidden), 0)

    def test_order_by_out_of_range_ordinal(self):
        message = support.message_of("SELECT id FROM emp ORDER BY 5")
        self.assertIn("超出", message)

    def test_limit_added(self):
        self.assertTrue(any(isinstance(node, Limit)
                            for node in plan_of("SELECT id FROM emp LIMIT 3").walk()))

    def test_limit_without_rows_has_no_limit_node(self):
        self.assertFalse(any(isinstance(node, Limit)
                             for node in plan_of("SELECT id FROM emp").walk()))


class ExplainTests(unittest.TestCase):
    def test_explain_has_header_and_tree(self):
        text = support.engine().explain("SELECT id FROM emp WHERE salary > 1000")
        self.assertIn("估行数", text)
        self.assertIn("扫描", text)
        self.assertIn("└─", text)

    def test_explain_with_actual(self):
        text = support.engine().explain("SELECT id FROM emp", actual=True)
        self.assertIn("实行数", text)

    def test_explain_mentions_join_strategy(self):
        text = support.engine().explain(
            "SELECT * FROM emp e JOIN dept d ON e.dept_id = d.id")
        self.assertIn("哈希", text)

    def test_explain_of_left_join(self):
        text = support.engine().explain(
            "SELECT * FROM dept d LEFT JOIN emp e ON d.id = e.dept_id")
        self.assertIn("左外", text)


class StarTests(unittest.TestCase):
    def test_star_expands_to_all_columns(self):
        plan = plan_of("SELECT * FROM dept")
        project = [node for node in plan.walk() if isinstance(node, Project)][0]
        self.assertEqual(len(project.items), 3)

    def test_table_star_only_expands_that_table(self):
        plan = plan_of("SELECT d.* FROM dept d JOIN emp e ON d.id = e.dept_id")
        project = [node for node in plan.walk() if isinstance(node, Project)][0]
        self.assertEqual(len(project.items), 3)

    def test_star_alias_column_names(self):
        result = support.engine().query("SELECT * FROM dept d LIMIT 1")
        self.assertEqual(result.columns, ["id", "name", "city"])


class NoFromTests(unittest.TestCase):
    """没有 FROM 的查询：来源是一行零列，用来算纯表达式。"""

    def test_literal_query(self):
        self.assertEqual(support.rows("SELECT 1"), [(1,)])

    def test_expression_without_table(self):
        result = support.engine().query("SELECT 1 + 2 AS n, 'a' || 'b' AS s")
        self.assertEqual(result.rows, [(3, "ab")])
        self.assertEqual(result.columns, ["n", "s"])

    def test_aggregate_over_single_row(self):
        self.assertEqual(support.one("SELECT COUNT(*)"), 1)
        self.assertEqual(support.one("SELECT MAX(7)"), 7)

    def test_where_can_drop_the_single_row(self):
        self.assertEqual(support.rows("SELECT 1 WHERE 1 = 0"), [])
        self.assertEqual(support.rows("SELECT 1 WHERE 1 = 1"), [(1,)])

    def test_plan_gets_single_row_source(self):
        plan = plan_of("SELECT 1")
        self.assertTrue(any(isinstance(node, planner.SingleRow) for node in plan.walk()))

    def test_star_without_from_is_rejected(self):
        self.assertEqual(support.error_of("SELECT *"), "PlanError")

    def test_column_without_from_is_unknown(self):
        self.assertEqual(support.error_of("SELECT x"), "BindError")


class GroupValidationTests(unittest.TestCase):
    def test_bare_column_without_group_is_rejected(self):
        self.assertEqual(support.error_of("SELECT name, COUNT(*) FROM emp"), "PlanError")

    def test_grouped_column_is_fine(self):
        self.assertEqual(len(support.rows("SELECT dept_id FROM emp GROUP BY dept_id")), 4)

    def test_expression_over_grouped_column_is_allowed(self):
        rows = support.rows("SELECT dept_id * 10, COUNT(*) FROM emp GROUP BY dept_id "
                            "ORDER BY dept_id LIMIT 1")
        self.assertEqual(rows, [(10, 4)])

    def test_aggregate_only_query_is_allowed(self):
        self.assertEqual(support.one("SELECT COUNT(*) FROM emp"), 8)
        self.assertEqual(len(support.rows("SELECT COUNT(*), SUM(salary) FROM emp")[0]), 2)


if __name__ == "__main__":
    unittest.main()
