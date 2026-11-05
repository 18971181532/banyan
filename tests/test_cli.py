"""命令行测试：子命令、输出格式、以及退出码。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from banyan.cli import main, normalize

SMALL = ["--scale", "0.02"]


def run(argv):
    """跑一次 CLI，返回 (退出码, 标准输出, 标准错误)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class NormalizeTests(unittest.TestCase):
    def test_sql_without_command_gets_query(self):
        self.assertEqual(normalize(["SELECT 1"]), ["query", "SELECT 1"])

    def test_known_command_is_kept(self):
        self.assertEqual(normalize(["tables"]), ["tables"])

    def test_options_before_command(self):
        self.assertEqual(normalize(["--scale", "0.1", "tables"]),
                         ["--scale", "0.1", "tables"])

    def test_options_before_sql(self):
        self.assertEqual(normalize(["--no-color", "SELECT 1"]),
                         ["--no-color", "query", "SELECT 1"])

    def test_empty_arguments(self):
        self.assertEqual(normalize([]), [])


class QueryTests(unittest.TestCase):
    def test_basic_query(self):
        code, out, _ = run(SMALL + ["SELECT city, COUNT(*) AS n FROM customers "
                                    "GROUP BY city ORDER BY n DESC LIMIT 3"])
        self.assertEqual(code, 0)
        self.assertIn("city", out)
        self.assertEqual(len([line for line in out.splitlines() if line.strip()]), 5)

    def test_query_alias(self):
        code, out, _ = run(SMALL + ["q", "SELECT COUNT(*) FROM customers"])
        self.assertEqual(code, 0)
        self.assertIn("COUNT(*)", out)

    def test_timing_flag(self):
        code, out, _ = run(SMALL + ["SELECT COUNT(*) FROM customers", "--timing"])
        self.assertEqual(code, 0)
        self.assertIn("执行", out)

    def test_json_output(self):
        code, out, _ = run(SMALL + ["SELECT COUNT(*) AS n FROM customers", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["columns"], ["n"])
        self.assertEqual(len(payload["rows"]), 1)

    def test_limit_option(self):
        code, out, _ = run(SMALL + ["SELECT id FROM customers", "--limit", "2"])
        self.assertEqual(code, 0)
        self.assertIn("还有", out)

    def test_empty_result_is_reported(self):
        code, out, _ = run(SMALL + ["SELECT id FROM customers WHERE id > 999999"])
        self.assertEqual(code, 0)
        self.assertIn("没有匹配的行", out)

    def test_sql_error_exits_nonzero(self):
        code, _, err = run(SMALL + ["SELECT nope FROM customers"])
        self.assertEqual(code, 1)
        self.assertIn("nope", err)

    def test_parse_error_points_at_position(self):
        code, _, err = run(SMALL + ["SELECT FROM customers"])
        self.assertEqual(code, 1)
        self.assertIn("^", err)


class ExplainTests(unittest.TestCase):
    SQL = "SELECT c.city, COUNT(*) FROM customers c JOIN orders o " \
          "ON c.id = o.customer_id WHERE o.status = '退款中' GROUP BY c.city"

    def test_explain(self):
        code, out, _ = run(SMALL + ["explain", self.SQL])
        self.assertEqual(code, 0)
        self.assertIn("估行数", out)
        self.assertIn("连接", out)

    def test_explain_analyze(self):
        code, out, _ = run(SMALL + ["explain", "--analyze", self.SQL])
        self.assertEqual(code, 0)
        self.assertIn("实行数", out)

    def test_no_reorder_changes_plan(self):
        _, on, _ = run(SMALL + ["explain", self.SQL])
        _, off, _ = run(SMALL + ["--no-reorder", "explain", self.SQL])
        self.assertNotEqual(on, off)


class MetaCommandTests(unittest.TestCase):
    def test_tables(self):
        code, out, _ = run(SMALL + ["tables"])
        self.assertEqual(code, 0)
        for name in ("customers", "orders", "order_items", "products", "regions"):
            self.assertIn(name, out)

    def test_functions(self):
        code, out, _ = run(["functions"])
        self.assertEqual(code, 0)
        self.assertIn("UPPER", out)
        self.assertIn("聚合函数", out)

    def test_version(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                main(["--version"])
        self.assertIn("banyan", out.getvalue())

    def test_no_arguments_prints_help(self):
        code, out, _ = run(SMALL)
        self.assertEqual(code, 0)
        self.assertIn("banyan", out.lower())


class SampleTests(unittest.TestCase):
    def test_sample_writes_csvs(self):
        directory = os.path.join(tempfile.mkdtemp(), "out")
        code, out, _ = run(["sample", "--out", directory, "--scale", "0.02"])
        self.assertEqual(code, 0)
        self.assertEqual(len(os.listdir(directory)), 5)

    def test_data_option_loads_csvs(self):
        directory = os.path.join(tempfile.mkdtemp(), "out")
        run(["sample", "--out", directory, "--scale", "0.02"])
        code, out, _ = run(["--data", directory, "SELECT COUNT(*) FROM customers"])
        self.assertEqual(code, 0)
        self.assertIn("COUNT(*)", out)

    def test_empty_data_directory_is_reported(self):
        directory = tempfile.mkdtemp()
        code, _, err = run(["--data", directory, "SELECT 1 FROM x"])
        self.assertEqual(code, 1)
        self.assertIn("CSV", err)


class BenchTests(unittest.TestCase):
    def test_bench_runs(self):
        code, out, _ = run(["bench", "--scale", "0.02", "--repeat", "1"])
        self.assertEqual(code, 0)
        self.assertIn("查询", out)
        self.assertIn("四表连接", out)


if __name__ == "__main__":
    unittest.main()
