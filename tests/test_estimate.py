"""选择率与代价估算测试。

这里的断言不是"估得很准"，而是"该单调的地方单调、该有界的地方有界"——
代价模型要的是稳定可靠，不是精确。
"""

from __future__ import annotations

import unittest

from banyan import planner
from banyan.catalog import ColumnStats
from banyan.estimate import (
    DEFAULT_SELECTIVITY, join_selectivity, rows_after_filter, selectivity, sort_cost,
)
from banyan.expr import BinaryOp, Const, LikeExpr, Ref
from banyan.planner import Filter, Scan
from tests import support


def column_stats(distinct=100, nulls=0, minimum=0, maximum=100, histogram=None, mcv=None):
    return ColumnStats(nulls=nulls, distinct=distinct, minimum=minimum, maximum=maximum,
                       histogram=histogram or [], mcv=mcv or [])


class SelectivityTests(unittest.TestCase):
    def setUp(self):
        self.stats = [column_stats(distinct=100, histogram=[(float(i), 10) for i in range(10)],
                                   mcv=[(42, 30)])]

    def test_constant_true_and_false(self):
        self.assertEqual(selectivity(Const(True), self.stats, 100), 1.0)
        self.assertEqual(selectivity(Const(False), self.stats, 100), 0.0)

    def test_equality_uses_mcv(self):
        expr = BinaryOp("=", Ref(0, "x"), Const(42))
        self.assertAlmostEqual(selectivity(expr, self.stats, 100), 0.30)

    def test_equality_without_mcv_uses_distinct(self):
        expr = BinaryOp("=", Ref(0, "x"), Const(7))
        self.assertAlmostEqual(selectivity(expr, self.stats, 100), 0.01)

    def test_and_multiplies(self):
        left = BinaryOp("=", Ref(0, "x"), Const(42))
        right = BinaryOp("=", Ref(0, "x"), Const(7))
        combined = BinaryOp("AND", left, right)
        self.assertAlmostEqual(selectivity(combined, self.stats, 100), 0.30 * 0.01)

    def test_or_uses_inclusion_exclusion(self):
        left = BinaryOp("=", Ref(0, "x"), Const(42))
        right = BinaryOp("=", Ref(0, "x"), Const(7))
        combined = BinaryOp("OR", left, right)
        expected = 0.30 + 0.01 - 0.30 * 0.01
        self.assertAlmostEqual(selectivity(combined, self.stats, 100), expected)

    def test_or_is_capped_at_one(self):
        left = BinaryOp(">", Ref(0, "x"), Const(0))
        right = BinaryOp(">", Ref(0, "x"), Const(1))
        self.assertLessEqual(selectivity(BinaryOp("OR", left, right), self.stats, 100), 1.0)

    def test_not_is_complement(self):
        expr = BinaryOp("=", Ref(0, "x"), Const(42))
        from banyan.expr import UnaryOp

        self.assertAlmostEqual(
            selectivity(UnaryOp("NOT", expr), self.stats, 100), 0.70)

    def test_range_is_monotonic(self):
        narrow = selectivity(BinaryOp(">", Ref(0, "x"), Const(90)), self.stats, 100)
        wide = selectivity(BinaryOp(">", Ref(0, "x"), Const(10)), self.stats, 100)
        self.assertLess(narrow, wide)

    def test_reversed_comparison(self):
        direct = selectivity(BinaryOp("<", Ref(0, "x"), Const(50)), self.stats, 100)
        flipped = selectivity(BinaryOp(">", Const(50), Ref(0, "x")), self.stats, 100)
        self.assertAlmostEqual(direct, flipped)

    def test_column_versus_column_is_default(self):
        expr = BinaryOp("=", Ref(0, "x"), Ref(1, "y"))
        self.assertEqual(selectivity(expr, self.stats, 100), DEFAULT_SELECTIVITY)

    def test_like_prefix_is_more_selective_than_contains(self):
        prefix = LikeExpr(Ref(0, "x"), Const("abc%"))
        contains = LikeExpr(Ref(0, "x"), Const("%abc%"))
        self.assertLess(selectivity(prefix, self.stats, 100),
                        selectivity(contains, self.stats, 100))

    def test_in_list_scales_with_items(self):
        from banyan.expr import InExpr

        one = InExpr(Ref(0, "x"), (Const(1),))
        three = InExpr(Ref(0, "x"), (Const(1), Const(2), Const(3)))
        self.assertLess(selectivity(one, self.stats, 100),
                        selectivity(three, self.stats, 100))

    def test_between_uses_range(self):
        from banyan.expr import BetweenExpr

        narrow = BetweenExpr(Ref(0, "x"), Const(40), Const(45))
        wide = BetweenExpr(Ref(0, "x"), Const(0), Const(100))
        self.assertLess(selectivity(narrow, self.stats, 100),
                        selectivity(wide, self.stats, 100))

    def test_unknown_column_falls_back(self):
        expr = BinaryOp("=", Ref(9, "x"), Const(1))
        self.assertEqual(selectivity(expr, self.stats, 100), DEFAULT_SELECTIVITY)

    def test_is_null_uses_null_fraction(self):
        from banyan.expr import IsNullExpr

        stats = [column_stats(nulls=25)]
        self.assertAlmostEqual(selectivity(IsNullExpr(Ref(0, "x")), stats, 100), 0.25)
        self.assertAlmostEqual(
            selectivity(IsNullExpr(Ref(0, "x"), negated=True), stats, 100), 0.75)


class JoinSelectivityTests(unittest.TestCase):
    def test_inverse_of_larger_distinct(self):
        left = column_stats(distinct=10)
        right = column_stats(distinct=1000)
        self.assertAlmostEqual(join_selectivity(left, right), 1 / 1000)

    def test_is_symmetric(self):
        left, right = column_stats(distinct=10), column_stats(distinct=1000)
        self.assertEqual(join_selectivity(left, right), join_selectivity(right, left))

    def test_zero_distinct_is_guarded(self):
        left, right = column_stats(distinct=0), column_stats(distinct=0)
        self.assertEqual(join_selectivity(left, right), 1.0)


class CostTests(unittest.TestCase):
    def test_sort_cost_grows_superlinearly(self):
        self.assertLess(sort_cost(100), sort_cost(1000))
        self.assertGreater(sort_cost(1000), 10 * sort_cost(100))

    def test_sort_cost_of_tiny_input(self):
        self.assertEqual(sort_cost(0), 0)
        self.assertGreater(sort_cost(1), 0)

    def test_rows_after_filter_never_below_one(self):
        self.assertEqual(rows_after_filter(1000, 0), 1.0)

    def test_rows_after_filter_is_capped_at_input(self):
        self.assertEqual(rows_after_filter(100, 2.0), 100)


class PlanCostTests(unittest.TestCase):
    def test_filter_lowers_rows(self):
        scan = Scan(support.database().table("emp"), "emp")
        plan = planner.plan_query(
            __import__("banyan.parser", fromlist=["parse"]).parse(
                "SELECT * FROM emp WHERE salary > 20000"),
            support.database())
        filters = [node for node in plan.walk() if isinstance(node, Filter)]
        self.assertTrue(filters)
        self.assertLess(filters[0].est_rows, scan.est_rows)

    def test_join_cost_is_positive(self):
        plan = support.engine().plan(
            "SELECT * FROM emp e JOIN dept d ON e.dept_id = d.id")
        self.assertGreater(plan.est_cost, 0)

    def test_reordering_lowers_estimated_cost(self):
        sql = """SELECT COUNT(*) FROM dept d
                 JOIN emp e ON e.dept_id = d.id
                 JOIN proj p ON p.dept_id = d.id
                 JOIN skill s ON s.emp_id = e.id
                 WHERE d.city = '北京' AND s.name = 'SQL'"""
        cheap = support.engine(reorder=True).plan(sql).est_cost
        naive = support.engine(reorder=False).plan(sql).est_cost
        self.assertLessEqual(cheap, naive)

    def test_estimates_are_floats(self):
        plan = support.engine().plan("SELECT * FROM emp")
        self.assertIsInstance(plan.est_rows, float)
        self.assertIsInstance(plan.est_cost, float)


if __name__ == "__main__":
    unittest.main()
