"""语法分析测试：优先级、子句、以及错误信息。"""

from __future__ import annotations

import unittest

from banyan import ast
from banyan.errors import ParseError
from banyan.parser import parse


class SelectClauseTests(unittest.TestCase):
    def test_simple_select(self):
        statement = parse("SELECT a, b FROM t")
        self.assertEqual([str(item.expr) for item in statement.items], ["a", "b"])
        self.assertEqual(statement.source.name, "t")

    def test_alias_with_and_without_as(self):
        statement = parse("SELECT a AS x, b y FROM t")
        self.assertEqual([item.alias for item in statement.items], ["x", "y"])

    def test_star_and_qualified_star(self):
        statement = parse("SELECT *, t.* FROM t")
        self.assertEqual(str(statement.items[0].expr), "*")
        self.assertEqual(str(statement.items[1].expr), "t.*")

    def test_distinct(self):
        self.assertTrue(parse("SELECT DISTINCT a FROM t").distinct)
        self.assertFalse(parse("SELECT a FROM t").distinct)

    def test_trailing_semicolon_is_allowed(self):
        self.assertEqual(len(parse("SELECT a FROM t;").items), 1)

    def test_empty_sql_is_rejected(self):
        with self.assertRaises(ParseError):
            parse("   ")


class FromClauseTests(unittest.TestCase):
    def test_table_alias(self):
        self.assertEqual(parse("SELECT * FROM emp e").source.alias, "e")
        self.assertEqual(parse("SELECT * FROM emp AS e").source.alias, "e")

    def test_join_kinds(self):
        inner = parse("SELECT * FROM a JOIN b ON a.x = b.y").source
        self.assertEqual(inner.kind, "inner")
        left = parse("SELECT * FROM a LEFT JOIN b ON a.x = b.y").source
        self.assertEqual(left.kind, "left")
        cross = parse("SELECT * FROM a CROSS JOIN b").source
        self.assertEqual(cross.kind, "cross")

    def test_comma_join_is_cross(self):
        source = parse("SELECT * FROM a, b").source
        self.assertEqual(source.kind, "cross")
        self.assertIsNone(source.on)

    def test_right_join_is_rejected_with_hint(self):
        with self.assertRaises(ParseError) as caught:
            parse("SELECT * FROM a RIGHT JOIN b ON a.x = b.y")
        self.assertIn("LEFT JOIN", caught.exception.message)

    def test_join_requires_on(self):
        with self.assertRaises(ParseError):
            parse("SELECT * FROM a JOIN b")

    def test_nested_join_associates_left(self):
        source = parse("SELECT * FROM a JOIN b ON a.x=b.y JOIN c ON b.z=c.w").source
        self.assertIsInstance(source, ast.Join)
        self.assertIsInstance(source.left, ast.Join)


class PrecedenceTests(unittest.TestCase):
    def test_or_binds_looser_than_and(self):
        node = parse("SELECT * FROM t WHERE a OR b AND c").where
        self.assertEqual(node.op, "OR")
        self.assertEqual(node.right.op, "AND")

    def test_and_is_left_associative(self):
        node = parse("SELECT * FROM t WHERE a AND b AND c").where
        self.assertEqual(node.op, "AND")
        self.assertEqual(node.left.op, "AND")

    def test_not_binds_tighter_than_and(self):
        node = parse("SELECT * FROM t WHERE NOT a AND b").where
        self.assertEqual(node.op, "AND")
        self.assertIsInstance(node.left, ast.Unary)

    def test_not_applies_to_comparison(self):
        node = parse("SELECT * FROM t WHERE NOT a = 1").where
        self.assertIsInstance(node, ast.Unary)
        self.assertIsInstance(node.operand, ast.Binary)

    def test_concat_binds_looser_than_addition(self):
        node = parse("SELECT a || b + 1 FROM t").items[0].expr
        self.assertEqual(node.op, "||")
        self.assertEqual(node.right.op, "+")

    def test_multiplication_before_addition(self):
        node = parse("SELECT a + b * c FROM t").items[0].expr
        self.assertEqual(node.op, "+")
        self.assertEqual(node.right.op, "*")

    def test_comparison_chains_left_to_right(self):
        node = parse("SELECT * FROM t WHERE a = 1 AND b = 2").where
        self.assertEqual(node.op, "AND")

    def test_unary_minus(self):
        node = parse("SELECT -a * b FROM t").items[0].expr
        self.assertEqual(node.op, "*")
        self.assertIsInstance(node.left, ast.Unary)

    def test_parentheses_override(self):
        node = parse("SELECT (a + b) * c FROM t").items[0].expr
        self.assertEqual(node.op, "*")
        self.assertEqual(node.left.op, "+")


class PredicateTests(unittest.TestCase):
    def test_in_list(self):
        node = parse("SELECT * FROM t WHERE a IN (1, 2, 3)").where
        self.assertIsInstance(node, ast.InList)
        self.assertEqual(len(node.items), 3)
        self.assertFalse(node.negated)

    def test_not_in(self):
        self.assertTrue(parse("SELECT * FROM t WHERE a NOT IN (1)").where.negated)

    def test_between(self):
        node = parse("SELECT * FROM t WHERE a BETWEEN 1 AND 5").where
        self.assertIsInstance(node, ast.Between)
        self.assertEqual(str(node.low), "1")

    def test_not_between(self):
        self.assertTrue(parse("SELECT * FROM t WHERE a NOT BETWEEN 1 AND 5").where.negated)

    def test_like_and_not_like(self):
        node = parse("SELECT * FROM t WHERE a LIKE 'x%'").where
        self.assertIsInstance(node, ast.Like)
        self.assertTrue(parse("SELECT * FROM t WHERE a NOT LIKE 'x%'").where.negated)

    def test_is_null(self):
        node = parse("SELECT * FROM t WHERE a IS NULL").where
        self.assertTrue(node.negated is False)
        node = parse("SELECT * FROM t WHERE a IS NOT NULL").where
        self.assertTrue(node.negated)

    def test_not_followed_by_garbage(self):
        with self.assertRaises(ParseError):
            parse("SELECT * FROM t WHERE a NOT 1")

    def test_in_subquery_is_rejected(self):
        with self.assertRaises(ParseError) as caught:
            parse("SELECT * FROM t WHERE a IN (SELECT b FROM u)")
        self.assertIn("子查询", caught.exception.message)


class FunctionAndCaseTests(unittest.TestCase):
    def test_function_call(self):
        node = parse("SELECT COUNT(*) FROM t").items[0].expr
        self.assertTrue(node.star)

    def test_function_with_distinct(self):
        node = parse("SELECT COUNT(DISTINCT a) FROM t").items[0].expr
        self.assertTrue(node.distinct)

    def test_nested_function(self):
        node = parse("SELECT UPPER(TRIM(a)) FROM t").items[0].expr
        self.assertEqual(node.name, "UPPER")
        self.assertEqual(node.args[0].name, "TRIM")

    def test_case_expression(self):
        node = parse("SELECT CASE WHEN a > 1 THEN 'x' ELSE 'y' END FROM t").items[0].expr
        self.assertIsInstance(node, ast.Case)
        self.assertEqual(len(node.whens), 1)
        self.assertIsNotNone(node.default)


class OtherClauseTests(unittest.TestCase):
    def test_where_group_having_order_limit(self):
        statement = parse("""SELECT city, COUNT(*) FROM emp
                             WHERE salary > 1000
                             GROUP BY city
                             HAVING COUNT(*) > 1
                             ORDER BY city DESC
                             LIMIT 10 OFFSET 5""")
        self.assertIsNotNone(statement.where)
        self.assertEqual(len(statement.group_by), 1)
        self.assertIsNotNone(statement.having)
        self.assertTrue(statement.order_by[0].descending)
        self.assertEqual(statement.limit, 10)
        self.assertEqual(statement.offset, 5)

    def test_order_by_asc_is_default(self):
        self.assertFalse(parse("SELECT a FROM t ORDER BY a").order_by[0].descending)

    def test_order_by_multiple_keys(self):
        statement = parse("SELECT a FROM t ORDER BY a, b DESC")
        self.assertEqual([item.descending for item in statement.order_by], [False, True])

    def test_limit_must_be_integer(self):
        with self.assertRaises(ParseError):
            parse("SELECT a FROM t LIMIT 1.5")


class ErrorMessageTests(unittest.TestCase):
    def test_error_carries_position_and_sql(self):
        with self.assertRaises(ParseError) as caught:
            parse("SELECT FROM t")
        error = caught.exception
        self.assertIsNotNone(error.position)
        self.assertEqual(error.sql, "SELECT FROM t")
        self.assertIn("^", error.render())

    def test_reserved_word_as_alias(self):
        with self.assertRaises(ParseError):
            parse("SELECT a AS FROM t")

    def test_unexpected_end(self):
        with self.assertRaises(ParseError):
            parse("SELECT a FROM")

    def test_select_without_from(self):
        statement = parse("SELECT 1 + 1 AS n")
        self.assertIsNone(statement.source)  # 没有 FROM：一行、零列
        self.assertEqual(len(statement.items), 1)
        self.assertEqual("SELECT (1 + 1) AS n", str(statement))

    def test_garbage_after_statement(self):
        with self.assertRaises(ParseError):
            parse("SELECT a FROM t extra garbage")


if __name__ == "__main__":
    unittest.main()
