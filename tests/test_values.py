"""值模型测试：三值逻辑是这里最容易错的地方。"""

from __future__ import annotations

import unittest

from banyan.errors import EvalError
from banyan.values import (
    BOOL, FLOAT, INT, NULLTYPE, TEXT, cast_to, common_type, compare, equals,
    format_value, infer_literal, is_number, to_number, truthy, type_of,
)


class TypeTests(unittest.TestCase):
    def test_type_of(self):
        self.assertEqual(type_of(None), NULLTYPE)
        self.assertEqual(type_of(1), INT)
        self.assertEqual(type_of(1.5), FLOAT)
        self.assertEqual(type_of("x"), TEXT)
        self.assertEqual(type_of(True), BOOL)

    def test_bool_is_not_a_number(self):
        self.assertFalse(is_number(True))
        self.assertFalse(is_number(False))
        self.assertTrue(is_number(0))
        self.assertTrue(is_number(0.0))

    def test_common_type_promotes_int_to_float(self):
        self.assertEqual(common_type([1, 2, 3]), INT)
        self.assertEqual(common_type([1, 2.5]), FLOAT)
        self.assertEqual(common_type([1, "x"]), TEXT)
        self.assertEqual(common_type([None, None]), TEXT)

    def test_common_type_ignores_nulls(self):
        self.assertEqual(common_type([None, 1, None]), INT)

    def test_bool_counts_as_int_in_csv(self):
        self.assertEqual(common_type([True, False]), INT)

    def test_cast_to_float(self):
        self.assertEqual(cast_to(2, FLOAT), 2.0)
        self.assertEqual(cast_to(None, FLOAT), None)
        self.assertEqual(cast_to("x", FLOAT), "x")


class ConversionTests(unittest.TestCase):
    def test_to_number_keeps_numeric(self):
        self.assertEqual(to_number(3), 3)
        self.assertEqual(to_number(3.5), 3.5)

    def test_to_number_parses_text(self):
        self.assertEqual(to_number("12"), 12)
        self.assertEqual(to_number("1.5"), 1.5)

    def test_to_number_rejects_garbage(self):
        with self.assertRaises(EvalError):
            to_number("abc")

    def test_to_number_rejects_null(self):
        with self.assertRaises(ValueError):
            to_number(None)


class TruthTests(unittest.TestCase):
    def test_null_is_false(self):
        self.assertFalse(truthy(None))

    def test_zero_is_false(self):
        self.assertFalse(truthy(0))
        self.assertFalse(truthy(0.0))
        self.assertTrue(truthy(0.1))

    def test_empty_string_is_truthy(self):
        # SQL 里 '' 不等于 NULL，也不等于 0；这里保持"非空即真"的朴素语义
        self.assertTrue(truthy(""))

    def test_bool_passthrough(self):
        self.assertTrue(truthy(True))
        self.assertFalse(truthy(False))


class CompareTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(compare(1, 2), -1)
        self.assertEqual(compare(2, 2), 0)
        self.assertEqual(compare(3, 2), 1)

    def test_int_and_float_compare_numerically(self):
        self.assertEqual(compare(2, 2.0), 0)

    def test_text_compares_lexicographically(self):
        self.assertEqual(compare("a", "b"), -1)
        self.assertEqual(compare("ab", "a"), 1)

    def test_bool_ordering(self):
        self.assertEqual(compare(False, True), -1)
        self.assertEqual(compare(True, True), 0)

    def test_null_makes_comparison_unknown(self):
        self.assertIsNone(compare(None, 1))
        self.assertIsNone(compare(1, None))
        self.assertIsNone(compare(None, None))

    def test_mixed_types_raise(self):
        with self.assertRaises(EvalError):
            compare(1, "1")

    def test_equals_returns_three_states(self):
        self.assertTrue(equals(1, 1))
        self.assertFalse(equals(1, 2))
        self.assertIsNone(equals(None, 1))


class LiteralTests(unittest.TestCase):
    def test_infer_int(self):
        self.assertEqual(infer_literal("42"), 42)

    def test_infer_float(self):
        self.assertEqual(infer_literal("1.5"), 1.5)

    def test_infer_text(self):
        self.assertEqual(infer_literal("abc"), "abc")

    def test_empty_is_null(self):
        self.assertIsNone(infer_literal(""))

    def test_spaces_are_kept_as_text(self):
        self.assertEqual(infer_literal("  "), "  ")

    def test_surrounding_space_is_trimmed_for_numbers(self):
        self.assertEqual(infer_literal(" 7 "), 7)

    def test_leading_zero_is_not_octal(self):
        self.assertEqual(infer_literal("007"), 7)

    def test_negative_numbers(self):
        self.assertEqual(infer_literal("-3"), -3)
        self.assertEqual(infer_literal("-3.5"), -3.5)


class FormatTests(unittest.TestCase):
    def test_null(self):
        self.assertEqual(format_value(None), "NULL")

    def test_float_with_integral_value_keeps_one_decimal(self):
        self.assertEqual(format_value(3.0), "3.0")

    def test_float_precision(self):
        self.assertEqual(format_value(1 / 3), "0.333333")

    def test_int_and_text(self):
        self.assertEqual(format_value(7), "7")
        self.assertEqual(format_value("x"), "x")

    def test_bool(self):
        self.assertEqual(format_value(True), "true")


if __name__ == "__main__":
    unittest.main()
