"""标量函数与 LIKE 匹配测试。"""

from __future__ import annotations

import unittest

from banyan.errors import BindError, EvalError
from banyan.functions import like_regex, lookup_scalar, match_like
from tests import support


def call(name, *args):
    spec = lookup_scalar(name)
    assert spec is not None, name
    return spec.fn(*args)


class NumericTests(unittest.TestCase):
    def test_abs(self):
        self.assertEqual(call("ABS", -3), 3)
        self.assertEqual(call("ABS", 3.5), 3.5)

    def test_round_half_up(self):
        self.assertEqual(call("ROUND", 2.4), 2.0)
        self.assertEqual(call("ROUND", 2.5), 3.0)
        self.assertEqual(call("ROUND", -2.5), -3.0)

    def test_round_digits(self):
        self.assertEqual(call("ROUND", 1.2345, 2), 1.23)
        # 2.675 的二进制表示略小于 2.675，直接 round 会得到 2.67
        self.assertEqual(call("ROUND", 2.675, 2), 2.68)

    def test_ceil_and_floor(self):
        self.assertEqual(call("CEIL", 1.1), 2)
        self.assertEqual(call("CEIL", -1.1), -1)
        self.assertEqual(call("FLOOR", 1.9), 1)
        self.assertEqual(call("FLOOR", -1.1), -2)

    def test_numeric_functions_are_null_safe(self):
        self.assertIsNone(call("ABS", None))
        self.assertIsNone(call("ROUND", None, 2))

    def test_abs_rejects_text(self):
        with self.assertRaises(EvalError):
            call("ABS", "abc")


class TextTests(unittest.TestCase):
    def test_case_conversion(self):
        self.assertEqual(call("UPPER", "abc"), "ABC")
        self.assertEqual(call("LOWER", "ABC"), "abc")

    def test_length_counts_characters(self):
        self.assertEqual(call("LENGTH", "上海"), 2)
        self.assertEqual(call("LENGTH", ""), 0)

    def test_trim_family(self):
        self.assertEqual(call("TRIM", "  x  "), "x")
        self.assertEqual(call("LTRIM", "  x  "), "x  ")
        self.assertEqual(call("RTRIM", "  x  "), "  x")

    def test_substr_is_one_based(self):
        self.assertEqual(call("SUBSTR", "abcdef", 2, 3), "bcd")
        self.assertEqual(call("SUBSTR", "abcdef", 1), "abcdef")

    def test_substr_negative_start_counts_from_end(self):
        self.assertEqual(call("SUBSTR", "abcdef", -2), "ef")

    def test_substr_negative_length_is_rejected(self):
        with self.assertRaises(EvalError):
            call("SUBSTR", "abc", 1, -1)

    def test_left_right(self):
        self.assertEqual(call("LEFT", "abcdef", 2), "ab")
        self.assertEqual(call("RIGHT", "abcdef", 2), "ef")
        self.assertEqual(call("LEFT", "abc", 0), "")
        self.assertEqual(call("RIGHT", "abc", 0), "")

    def test_replace(self):
        self.assertEqual(call("REPLACE", "a-b-c", "-", "+"), "a+b+c")

    def test_concat(self):
        self.assertEqual(call("CONCAT", "a", "b", "c"), "abc")
        self.assertIsNone(call("CONCAT", "a", None))

    def test_text_functions_accept_numbers(self):
        self.assertEqual(call("UPPER", 12), "12")

    def test_length_rejects_non_text(self):
        with self.assertRaises(EvalError):
            call("LENGTH", object())


class NullHandlingTests(unittest.TestCase):
    def test_coalesce(self):
        self.assertEqual(call("COALESCE", None, None, 3), 3)
        self.assertIsNone(call("COALESCE", None, None))
        self.assertEqual(call("COALESCE", 0, 3), 0)

    def test_nullif(self):
        self.assertIsNone(call("NULLIF", 1, 1))
        self.assertEqual(call("NULLIF", 1, 2), 1)

    def test_greatest_least(self):
        self.assertEqual(call("GREATEST", 1, 5, 3), 5)
        self.assertEqual(call("LEAST", 1, 5, 3), 1)
        self.assertIsNone(call("GREATEST", 1, None))

    def test_greatest_with_text(self):
        self.assertEqual(call("GREATEST", "a", "c", "b"), "c")


class LikeTests(unittest.TestCase):
    def test_percent_matches_any_length(self):
        self.assertTrue(match_like("上海", "%"))
        self.assertTrue(match_like("", "%"))

    def test_prefix_pattern(self):
        self.assertTrue(match_like("上海", "上%"))
        self.assertFalse(match_like("北京", "上%"))

    def test_underscore_matches_one_char(self):
        self.assertTrue(match_like("ab", "a_"))
        self.assertFalse(match_like("abc", "a_"))

    def test_anchor_is_full_match(self):
        self.assertFalse(match_like("abc", "b"))

    def test_special_characters_are_escaped(self):
        # 正则里的元字符在 LIKE 里应该是普通字符
        self.assertTrue(match_like("a.c", "a.c"))
        self.assertFalse(match_like("abc", "a.c"))

    def test_null_propagates(self):
        self.assertIsNone(match_like(None, "%"))
        self.assertIsNone(match_like("a", None))

    def test_pattern_is_cached(self):
        self.assertIs(like_regex("x%"), like_regex("x%"))


class RegistryTests(unittest.TestCase):
    def test_unknown_function(self):
        self.assertIsNone(lookup_scalar("NOPE"))

    def test_lookup_is_case_insensitive(self):
        self.assertIsNotNone(lookup_scalar("upper"))

    def test_wrong_arity_via_sql(self):
        self.assertEqual(support.error_of("SELECT ABS(1, 2) FROM dept"), "BindError")

    def test_unknown_function_via_sql(self):
        message = support.message_of("SELECT NOPE(1) FROM dept")
        self.assertIn("没有这个函数", message)

    def test_count_star_only(self):
        self.assertEqual(support.error_of("SELECT SUM(*) FROM emp"), "BindError")

    def test_aggregate_needs_one_arg(self):
        self.assertEqual(support.error_of("SELECT SUM(a, b) FROM emp"), "BindError")

    def test_nested_aggregate_is_rejected(self):
        self.assertEqual(support.error_of("SELECT SUM(MAX(salary)) FROM emp"), "BindError")


if __name__ == "__main__":
    unittest.main()
