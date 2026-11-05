"""表、统计信息与 CSV 装载测试。"""

from __future__ import annotations

import os
import tempfile
import unittest

from banyan.catalog import Column, ColumnStats, Database, Table, render_rows
from banyan.errors import BindError
from banyan.values import INT, TEXT


def write_csv(text: str) -> str:
    handle, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)
    return path


class StatsTests(unittest.TestCase):
    def setUp(self):
        from tests.support import make_database

        self.db = make_database()
        self.emp = self.db.table("emp")

    def test_row_count(self):
        self.assertEqual(len(self.emp), 8)

    def test_nulls_are_counted(self):
        self.assertEqual(self.emp.stats["salary"].nulls, 1)

    def test_distinct_ignores_nulls(self):
        self.assertEqual(self.emp.stats["salary"].distinct, 7)

    def test_min_max(self):
        stats = self.emp.stats["salary"]
        self.assertEqual(stats.minimum, 12000)
        self.assertEqual(stats.maximum, 32000)

    def test_histogram_covers_all_rows(self):
        stats = self.emp.stats["salary"]
        self.assertEqual(sum(count for _, count in stats.histogram), 7)
        self.assertGreater(len(stats.histogram), 1)

    def test_mcv_is_sorted_by_frequency(self):
        stats = self.emp.stats["level"]
        counts = [count for _, count in stats.mcv]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_text_column_with_low_cardinality_gets_mcv(self):
        self.assertTrue(self.emp.stats["level"].mcv)

    def test_equality_selectivity_uses_mcv(self):
        # P6 出现两次，共 8 行
        self.assertAlmostEqual(self.emp.stats["level"].equality_selectivity("P6", 8), 0.25)

    def test_equality_selectivity_falls_back_to_distinct(self):
        stats = self.emp.stats["salary"]
        value = stats.equality_selectivity(99999, 8)
        self.assertAlmostEqual(value, 1 / 7)

    def test_range_selectivity_is_monotonic(self):
        stats = self.emp.stats["salary"]
        narrow = stats.range_selectivity(20000, 21000, 8)
        wide = stats.range_selectivity(10000, 32000, 8)
        self.assertLessEqual(narrow, wide)
        self.assertGreater(wide, 0.9)

    def test_range_selectivity_without_bounds_is_one(self):
        self.assertAlmostEqual(self.emp.stats["salary"].range_selectivity(None, None, 8), 1.0)

    def test_range_selectivity_without_histogram(self):
        stats = ColumnStats(distinct=10)
        self.assertAlmostEqual(stats.range_selectivity(1, 2, 100), 0.33)

    def test_selectivity_of_empty_table_is_zero(self):
        self.assertEqual(ColumnStats().equality_selectivity(1, 0), 0.0)
        self.assertEqual(ColumnStats().range_selectivity(1, 2, 0), 0.0)

    def test_single_value_column(self):
        table = Table("t", [Column("x", INT)], [(5,), (5,), (5,)])
        self.assertEqual(table.stats["x"].distinct, 1)
        self.assertEqual(table.stats["x"].histogram, [(5.0, 3)])


class SchemaTests(unittest.TestCase):
    def setUp(self):
        from tests.support import make_database

        self.schema = make_database().table("emp").schema

    def test_index_of_name(self):
        self.assertEqual(self.schema.index_of("salary"), 3)

    def test_index_of_is_case_insensitive(self):
        self.assertEqual(self.schema.index_of("SALARY"), 3)

    def test_index_of_qualified(self):
        self.assertEqual(self.schema.index_of("salary", "emp"), 3)

    def test_unknown_column(self):
        with self.assertRaises(BindError) as caught:
            self.schema.index_of("nope")
        self.assertIn("nope", caught.exception.message)

    def test_qualified_lookup_on_wrong_table(self):
        with self.assertRaises(BindError):
            self.schema.index_of("salary", "dept")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        from tests.support import make_database

        self.db = make_database()

    def test_names(self):
        self.assertEqual(sorted(self.db.names()), ["dept", "emp", "proj", "skill"])

    def test_lookup_is_case_insensitive(self):
        self.assertIs(self.db.table("EMP"), self.db.table("emp"))

    def test_unknown_table_message_lists_known(self):
        with self.assertRaises(BindError) as caught:
            self.db.table("nope")
        self.assertIn("dept", caught.exception.message)

    def test_has(self):
        self.assertTrue(self.db.has("EMP"))
        self.assertFalse(self.db.has("nope"))

    def test_describe_mentions_columns(self):
        self.assertIn("salary", self.db.describe())


class CsvTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "path", None) and os.path.exists(self.path):
            os.unlink(self.path)

    def test_load_csv_infers_types(self):
        self.path = write_csv("id,name,score\n1,a,1.5\n2,b,2.5\n")
        table = Table.from_csv(self.path)
        self.assertEqual([column.type for column in table.columns], [INT, TEXT, "FLOAT"])
        self.assertEqual(len(table), 2)

    def test_table_name_from_filename(self):
        self.path = write_csv("x\n1\n")
        self.assertTrue(Table.from_csv(self.path).name)

    def test_explicit_name(self):
        self.path = write_csv("x\n1\n")
        self.assertEqual(Table.from_csv(self.path, "自定义").name, "自定义")

    def test_empty_cells_become_null(self):
        self.path = write_csv("a,b\n1,\n")
        table = Table.from_csv(self.path)
        self.assertIsNone(table.rows[0][1])

    def test_blank_lines_are_skipped(self):
        self.path = write_csv("a\n1\n\n2\n")
        self.assertEqual(len(Table.from_csv(self.path)), 2)

    def test_short_rows_are_padded(self):
        self.path = write_csv("a,b\n1\n")
        self.assertIsNone(Table.from_csv(self.path).rows[0][1])

    def test_bom_is_stripped(self):
        self.path = write_csv("\ufeffa,b\n1,2\n")
        self.assertEqual(Table.from_csv(self.path).columns[0].name, "a")

    def test_empty_csv_is_rejected(self):
        self.path = write_csv("")
        with self.assertRaises(BindError):
            Table.from_csv(self.path)

    def test_load_dir(self):
        directory = tempfile.mkdtemp()
        with open(os.path.join(directory, "a.csv"), "w", encoding="utf-8") as stream:
            stream.write("x\n1\n")
        with open(os.path.join(directory, "b.csv"), "w", encoding="utf-8") as stream:
            stream.write("y\n2\n")
        db = Database()
        loaded = db.load_dir(directory)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(len(db), 2)


class RenderTests(unittest.TestCase):
    def test_render_rows_aligns(self):
        text = render_rows([[1, "a"], [22, "bb"]], ["n", "s"])
        self.assertIn("22", text)
        # 表头 + 分隔线 + 两行数据
        self.assertEqual(len(text.splitlines()), 4)


if __name__ == "__main__":
    unittest.main()
