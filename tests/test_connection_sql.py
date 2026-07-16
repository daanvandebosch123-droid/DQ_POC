from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dqtool.models.entities import Connection, ConnectionType, Rule, RuleType
from dqtool.services.connectors import ConnectorService
from dqtool.services.execution import ExecutionService


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


if __name__ == "__main__":
    unittest.main()
