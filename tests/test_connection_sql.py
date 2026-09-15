from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import duckdb

from dqtool.models.entities import Connection, ConnectionType, Rule, RuleType
from dqtool.services.connectors import ConnectorService
from dqtool.services.execution import ExecutionService


class SingleCsvSqlTests(unittest.TestCase):
    def test_queries_with_limits_semicolons_and_comments_execute(self) -> None:
        service = ExecutionService(ConnectorService())
        queries = (
            ("SELECT * FROM dataset_view LIMIT 10", 10),
            ("SELECT * FROM dataset_view;  ", 1000),
            ("SELECT * FROM dataset_view LIMIT 600;", 600),
            ("SELECT * FROM dataset_view -- a comment", 1000),
            ("WITH small AS (SELECT * FROM dataset_view LIMIT 3) SELECT * FROM small;", 3),
        )
        for sql, expected_count in queries:
            with self.subTest(sql=sql):
                rule = Rule(
                    id=1, name="custom", rule_type=RuleType.CUSTOM_SQL_FAIL_ROWS,
                    dataset_id=None, owner_username="tester", config={"sql": sql},
                )
                summary, preview = service._run_duckdb_rule(rule, lambda con: con.sql("SELECT * FROM range(1000)"))
                self.assertEqual(1000, summary["checked_count"])
                self.assertEqual(expected_count, summary["failed_count"])
                self.assertEqual(min(expected_count, 500), len(preview))

    def test_metric_query_accepts_a_trailing_semicolon(self) -> None:
        rule = Rule(
            id=1, name="metric", rule_type=RuleType.CUSTOM_SQL_THRESHOLD, dataset_id=None,
            owner_username="tester", config={"sql": "SELECT COUNT(*) AS value FROM dataset_view;", "threshold": 2},
        )
        summary, preview = ExecutionService(ConnectorService())._run_duckdb_rule(
            rule, lambda con: con.sql("SELECT * FROM range(3)"),
        )
        self.assertEqual(1, summary["failed_count"])
        self.assertEqual([{"value": 3}], preview)


class ConnectionSqlRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        (base / "customers.csv").write_text("customer_id,name\n1,Ann\n2,Bob\n", encoding="utf-8")
        (base / "orders.csv").write_text(
            "order_id,customer_id,amount\n10,1,5\n11,2,7\n12,99,3\n13,98,4\n",
            encoding="utf-8",
        )
        self.connection = Connection(
            id=1,
            name="local-csvs",
            connection_type=ConnectionType.CSV,
            owner_username="tester",
            config={"base_path": str(base)},
        )
        self.service = ExecutionService(ConnectorService())
        self.results_dir = base / "results"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rule(self, sql: str, extra_config: dict | None = None) -> Rule:
        config = {
            "source_connection_id": 1,
            "source_kind": "connection",
            "source_name": "",
            "source_sql": "",
            "sql": sql,
        }
        config.update(extra_config or {})
        return Rule(
            id=1,
            name="orders_without_customer",
            rule_type=RuleType.CUSTOM_SQL_CONNECTION,
            dataset_id=None,
            owner_username="tester",
            config=config,
        )

    def test_sql_joins_across_csv_files_of_one_connection(self) -> None:
        rule = self._rule(
            "SELECT o.* FROM orders o LEFT JOIN customers c "
            "ON o.customer_id = c.customer_id WHERE c.customer_id IS NULL"
        )
        runs = self.service.run_rules([rule], {}, {1: self.connection}, self.results_dir, "tester")
        self.assertEqual(1, len(runs))
        self.assertEqual("failed", runs[0].status, runs[0].summary_json.get("error"))
        self.assertEqual(2, runs[0].summary_json["failed_count"])
        self.assertIsNotNone(runs[0].failed_rows_path)

    def test_passing_connection_sql_returns_no_rows(self) -> None:
        rule = self._rule("SELECT * FROM customers WHERE customer_id IS NULL")
        runs = self.service.run_rules([rule], {}, {1: self.connection}, self.results_dir, "tester")
        self.assertEqual("passed", runs[0].status, runs[0].summary_json.get("error"))
        self.assertEqual(0, runs[0].summary_json["failed_count"])

    def test_fail_threshold_count_applies(self) -> None:
        rule = self._rule(
            "SELECT o.* FROM orders o LEFT JOIN customers c "
            "ON o.customer_id = c.customer_id WHERE c.customer_id IS NULL",
            {"fail_threshold_count": 2},
        )
        runs = self.service.run_rules([rule], {}, {1: self.connection}, self.results_dir, "tester")
        self.assertEqual("passed", runs[0].status, runs[0].summary_json.get("error"))

    def test_missing_sql_is_rejected(self) -> None:
        rule = self._rule("")
        runs = self.service.run_rules([rule], {}, {1: self.connection}, self.results_dir, "tester")
        self.assertEqual("error", runs[0].status)

    def test_view_names_are_sanitized(self) -> None:
        base = Path(self._tmp.name)
        (base / "2024 sales-data.csv").write_text("id\n1\n", encoding="utf-8")
        rule = self._rule("SELECT * FROM t_2024_sales_data WHERE id IS NULL")
        runs = self.service.run_rules([rule], {}, {1: self.connection}, self.results_dir, "tester")
        self.assertEqual("passed", runs[0].status, runs[0].summary_json.get("error"))

    def test_displayed_view_names_match_registered_names_when_filenames_collide(self) -> None:
        base = Path(self._tmp.name)
        (base / "sales-data.csv").write_text("id\n1\n", encoding="utf-8")
        (base / "sales data.csv").write_text("id\n2\n", encoding="utf-8")
        connector = self.service.connector_service
        displayed = connector.csv_connection_view_paths(self.connection)
        with duckdb.connect() as con:
            registered = connector.register_connection_views(con, self.connection)
            self.assertEqual(displayed, registered)
            self.assertEqual((2,), con.execute("SELECT id FROM sales_data").fetchone())
            self.assertEqual((1,), con.execute("SELECT id FROM sales_data_2").fetchone())

    def test_view_names_avoid_case_insensitive_collisions(self) -> None:
        connector = self.service.connector_service
        self.assertEqual("ORDERS_2", connector._view_name_for_file(Path("ORDERS.csv"), {"orders": "orders.csv"}))


if __name__ == "__main__":
    unittest.main()
