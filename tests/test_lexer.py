"""词法器测试。"""

from __future__ import annotations

import unittest

from banyan.errors import ParseError
from banyan.lexer import tokenize


def kinds(sql):
    return [token.kind for token in tokenize(sql) if token.kind != "eof"]


class LexerTests(unittest.TestCase):
    def test_keywords_are_case_insensitive(self):
        tokens = tokenize("SeLeCt a FrOm t")
        self.assertEqual([token.upper for token in tokens[:4]], ["SELECT", "A", "FROM", "T"])

    def test_identifier_value_is_preserved(self):
        tokens = tokenize("CustomerName")
        self.assertEqual(tokens[0].value, "CustomerName")
        self.assertEqual(tokens[0].upper, "CUSTOMERNAME")

    def test_number_forms(self):
        values = [token.value for token in tokenize("1 2.5 .5 10. 1e3 0.5e-2")[:-1]]
        self.assertEqual(values, [1, 2.5, 0.5, 10.0, 1000.0, 0.005])

    def test_numbers_have_number_kind(self):
        self.assertEqual(kinds("1 2.5"), ["number", "number"])

    def test_string_with_doubled_quote(self):
        tokens = tokenize("'it''s ok'")
        self.assertEqual(tokens[0].value, "it's ok")
        self.assertEqual(tokens[0].kind, "string")

    def test_empty_string(self):
        self.assertEqual(tokenize("''")[0].value, "")

    def test_string_keeps_newlines(self):
        self.assertEqual(tokenize("'a\nb'")[0].value, "a\nb")

    def test_two_char_operators(self):
        self.assertEqual(kinds("a<>b"), ["word", "op", "word"])
        self.assertEqual(tokenize("a>=b")[1].text, ">=")
        self.assertEqual(tokenize("a!=b")[1].text, "!=")
        self.assertEqual(tokenize("a||b")[1].text, "||")

    def test_punctuation_kinds(self):
        self.assertEqual(kinds("a,b.c(d)*"),
                         ["word", "punct", "word", "punct", "word",
                          "punct", "word", "punct", "op"])

    def test_position_points_at_token(self):
        self.assertEqual(tokenize("SELECT  x")[1].pos, 8)

    def test_unterminated_string(self):
        with self.assertRaises(ParseError):
            tokenize("'abc")

    def test_unterminated_block_comment(self):
        with self.assertRaises(ParseError):
            tokenize("/* abc")

    def test_unexpected_character(self):
        with self.assertRaises(ParseError):
            tokenize("SELECT #")

    def test_eof_always_present(self):
        self.assertEqual(tokenize("")[-1].kind, "eof")
        self.assertEqual(tokenize("SELECT 1")[-1].kind, "eof")

    def test_line_comment_is_skipped(self):
        self.assertEqual(kinds("SELECT 1 -- 这是注释"), ["word", "number"])

    def test_block_comment_is_skipped(self):
        self.assertEqual(kinds("SELECT /* 注解 */ 1"), ["word", "number"])

    def test_minus_is_operator_not_comment(self):
        self.assertEqual(kinds("a - b"), ["word", "op", "word"])

    def test_quoted_identifier_keeps_case(self):
        token = tokenize('"Order ID"')[0]
        self.assertEqual(token.kind, "word")
        self.assertEqual(token.value, "Order ID")

    def test_quoted_identifier_escapes_quote(self):
        self.assertEqual(tokenize('"a""b"')[0].value, 'a"b')


if __name__ == "__main__":
    unittest.main()
