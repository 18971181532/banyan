"""对外接口测试：Result、诊断、以及内置数据。"""

from __future__ import annotations

import os
import tempfile
import unittest

from banyan import Engine, sampledata
from banyan.engine import Finding
from tests import support


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.result = support.engine().query("SELECT id, name FROM dept ORDER BY id")

    def test_columns(self):
        self.assertEqual(self.result.columns, ["id", "name"])

    def test_rows(self):
        self.assertEqual(len(self.result), 5)
        self.assertEqual(self.result.rows[0], (1, "研发"))

    def test_dicts(self):
        first = self.result.dicts()[0]
        self.assertEqual(first["id"], 1)
        self.assertEqual(first["name"], "研发")

    def test_text_renders_header(self):
        text = self.result.text()
        self.assertIn("id", text)
        self.assertIn("研发", text)

    def test_text_limit(self):
        self.assertEqual(len(self.result.text(limit=2).splitlines()), 4)

    def test_timings_are_recorded(self):
        self.assertGreaterEqual(self.result.plan_ms, 0)
        self.assertGreaterEqual(self.result.run_ms, 0)
        self.assertAlmostEqual(self.result.total_ms,
                               self.result.plan_ms + self.result.run_ms)

    def test_execute_is_alias_of_query(self):
        engine = support.engine()
        self.assertEqual(engine.query("SELECT id FROM dept").rows,
                         engine.execute("SELECT id FROM dept").rows)


class EngineOptionTests(unittest.TestCase):
    SQL = "SELECT COUNT(*) FROM emp e JOIN dept d ON e.dept_id = d.id WHERE d.city = '北京'"

    def test_options_do_not_change_answers(self):
        answers = {support.one(self.SQL),
                   support.one(self.SQL, reorder=False),
                   support.one(self.SQL, push_down=False)}
        # 北京有研发和财务两个部门：研发 4 人 + 财务 1 人
        self.assertEqual(answers, {5})

    def test_explain_reflects_option(self):
        on = support.engine().explain(self.SQL)
        off = support.engine(push_down=False).explain(self.SQL)
        self.assertNotEqual(on, off)

    def test_engine_holds_database(self):
        self.assertIs(support.engine().db, support.database())


class DiagnoseTests(unittest.TestCase):
    def test_accurate_query_has_no_findings(self):
        result = support.engine().query("SELECT COUNT(*) FROM dept")
        self.assertEqual(support.engine().diagnose(result), [])

    def test_skewed_join_is_flagged(self):
        db = sampledata.build(scale=0.3)
        engine = Engine(db)
        result = engine.query(
            "SELECT c.segment, COUNT(*) FROM customers c JOIN orders o "
            "ON c.id = o.customer_id WHERE c.segment = 'VIP' GROUP BY c.segment")
        findings = engine.diagnose(result)
        self.assertTrue(findings)
        self.assertTrue(any("连接" in item.label for item in findings))

    def test_findings_are_sorted_by_error(self):
        db = sampledata.build(scale=0.3)
        engine = Engine(db)
        result = engine.query(
            "SELECT COUNT(*) FROM customers WHERE segment = '企业' AND credit_limit > 30000")
        findings = engine.diagnose(result)
        errors = [item.q_error for item in findings]
        self.assertEqual(errors, sorted(errors, reverse=True))

    def test_threshold_filters_small_errors(self):
        db = sampledata.build(scale=0.3)
        engine = Engine(db)
        result = engine.query(
            "SELECT COUNT(*) FROM customers WHERE segment = '企业' AND credit_limit > 30000")
        self.assertEqual(engine.diagnose(result, threshold=1000), [])

    def test_finding_as_dict(self):
        item = Finding("连接", 10.0, 5, 2.0, "因为")
        data = item.as_dict()
        self.assertEqual(data["operator"], "连接")
        self.assertEqual(data["actual"], 5)
        self.assertEqual(data["q_error"], 2.0)

    def test_report_contains_plan_and_hint(self):
        db = sampledata.build(scale=0.3)
        engine = Engine(db)
        text = engine.report(
            "SELECT COUNT(*) FROM customers WHERE segment = '企业' AND credit_limit > 30000")
        self.assertIn("估行数", text)
        self.assertIn("实行数", text)

    def test_report_says_when_estimates_match(self):
        text = support.engine().report("SELECT COUNT(*) FROM dept")
        self.assertIn("吻合", text)

    def test_limit_does_not_create_fake_findings(self):
        # LIMIT 只向上游要 2 行，扫描的 actual_rows 当然是 2，不能算成"估错 4 倍"
        engine = support.engine()
        result = engine.query("SELECT id FROM emp LIMIT 2")
        self.assertEqual(engine.diagnose(result), [])

    def test_report_explains_limit_exclusion(self):
        text = support.engine().report("SELECT id FROM emp LIMIT 2")
        self.assertIn("LIMIT", text)


class SampleDataTests(unittest.TestCase):
    def test_build_is_deterministic(self):
        first = sampledata.build(scale=0.05)
        second = sampledata.build(scale=0.05)
        self.assertEqual(first.table("orders").rows, second.table("orders").rows)

    def test_scale_changes_row_counts(self):
        small = sampledata.build(scale=0.02)
        large = sampledata.build(scale=0.04)
        self.assertLess(len(small.table("orders")), len(large.table("orders")))

    def test_all_tables_present(self):
        db = sampledata.build(scale=0.02)
        self.assertEqual(sorted(db.names()),
                         ["customers", "order_items", "orders", "products", "regions"])

    def test_orders_reference_existing_customers(self):
        db = sampledata.build(scale=0.05)
        ids = {row[0] for row in db.table("customers").rows}
        self.assertTrue(all(row[1] in ids for row in db.table("orders").rows))

    def test_items_reference_existing_orders(self):
        db = sampledata.build(scale=0.05)
        ids = {row[0] for row in db.table("orders").rows}
        self.assertTrue(all(row[1] in ids for row in db.table("order_items").rows))

    def test_city_is_in_regions(self):
        db = sampledata.build(scale=0.05)
        cities = {row[0] for row in db.table("regions").rows}
        self.assertTrue(all(row[2] in cities for row in db.table("customers").rows))

    def test_write_and_load_csvs(self):
        directory = tempfile.mkdtemp()
        paths = sampledata.write_csvs(directory, scale=0.02)
        self.assertEqual(len(paths), 5)

        from banyan.catalog import Database

        db = Database()
        db.load_dir(directory)
        self.assertEqual(len(db), 5)
        self.assertGreater(len(db.table("customers")), 0)

    def test_csv_roundtrip_keeps_row_count(self):
        directory = tempfile.mkdtemp()
        original = sampledata.build(scale=0.02)
        sampledata.write_csvs(directory, scale=0.02)
        from banyan.catalog import Database

        db = Database()
        db.load_dir(directory)
        self.assertEqual(len(db.table("customers")), len(original.table("customers")))


if __name__ == "__main__":
    unittest.main()
