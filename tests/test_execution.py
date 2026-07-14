from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dqtool.models.entities import Connection, ConnectionType, Dataset, DatasetType, Rule, RuleType
from dqtool.services.connectors import ConnectorService
from dqtool.services.execution import ExecutionService


class ExecutionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent
        self.csv_path = self.root / "fixtures" / "customers.csv"
        self.results_dir = self.root / ".runtime-results"
        self.results_dir.mkdir(exist_ok=True)
        self.non_utf8_csv_path = self.root / ".runtime-latin1.csv"
        self.non_utf8_csv_path.write_bytes(b"id;name\n1;VARKENSKROON +/- 2,25KG\n2;caf\xe9\n")
        self.csv_connection = Connection(
            id=11,
            name="fixture-csv",
            connection_type=ConnectionType.CSV,
            owner_username="tester",
            config={"base_path": str(self.root / "fixtures")},
        )
        self.latin1_connection = Connection(
            id=12,
            name="latin1-csv",
            connection_type=ConnectionType.CSV,
            owner_username="tester",
            config={"base_path": str(self.root)},
        )
        self.dataset = Dataset(
            id=1,
            name="customers",
            dataset_type=DatasetType.CSV_FILE,
            connection_id=None,
            owner_username="tester",
            config={"path": str(self.csv_path)},
        )
        self.service = ExecutionService(ConnectorService())

    def tearDown(self) -> None:
        for result in self.results_dir.glob("*"):
            result.unlink()
        self.results_dir.rmdir()
        self.non_utf8_csv_path.unlink(missing_ok=True)

    def test_csv_rule_runs_without_dataframe_dependencies(self) -> None:
        rule = Rule(
            id=1,
            name="unique ids",
            rule_type=RuleType.UNIQUE,
            dataset_id=None,
            owner_username="tester",
            config={"source_connection_id": 11, "source_kind": "csv_file", "source_name": "customers.csv", "source_sql": "", "columns": ["id"]},
        )

        run = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual("failed", run.status)
        self.assertEqual(2, run.summary_json["failed_count"])
        self.assertTrue(Path(run.failed_rows_path).exists())

    def test_failed_rows_append_to_one_file_per_rule_with_execution_datetime(self) -> None:
        import csv

        rule = Rule(
            id=1,
            name="unique ids",
            rule_type=RuleType.UNIQUE,
            dataset_id=None,
            owner_username="tester",
            config={"source_connection_id": 11, "source_kind": "csv_file", "source_name": "customers.csv", "source_sql": "", "columns": ["id"]},
        )

        first = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]
        second = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual(first.failed_rows_path, second.failed_rows_path)
        self.assertEqual([Path(first.failed_rows_path)], list(self.results_dir.glob("*.csv")))
        with Path(first.failed_rows_path).open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(4, len(rows))  # 2 failed rows per run, appended
        stamps = {first.started_at, second.started_at}
        self.assertTrue(all(row["execution_datetime"] in stamps for row in rows))

    def test_csv_connection_lists_available_files_for_rule_selection(self) -> None:
        targets = self.service.connector_service.list_connection_targets(self.csv_connection)

        self.assertIn("customers.csv", targets)
        self.assertIn("orders.csv", targets)

    def test_single_file_csv_connection_exposes_only_its_bound_file(self) -> None:
        connection = Connection(
            id=13,
            name="customers",
            connection_type=ConnectionType.CSV,
            owner_username="tester",
            config={
                "file_path": str(self.csv_path),
                "base_path": str(self.csv_path.parent),
            },
        )

        targets = self.service.connector_service.list_connection_targets(connection)

        self.assertEqual(["customers.csv"], targets)
        self.assertEqual(self.csv_path, self.service.connector_service.csv_connection_file(connection))

    def test_db2_connection_lists_targets_from_sysibm_when_syscat_is_unavailable(self) -> None:
        connection = Connection(
            id=15,
            name="db2",
            connection_type=ConnectionType.DB2,
            owner_username="tester",
            config={
                "host": "localhost",
                "port": 50000,
                "database": "TESTDB",
                "username": "tester",
            },
        )
        db_conn = Mock()
        cursor = Mock()
        db_conn.cursor.return_value.__enter__.return_value = cursor
        cursor.execute.side_effect = [RuntimeError("SYSCAT unavailable"), RuntimeError("SYSIBM unavailable")]
        cursor.tables.return_value = [(None, "MYSCHEMA", "MYTABLE", "TABLE", None)]

        with patch.object(self.service.connector_service, "connect_database", return_value=db_conn):
            targets = self.service.connector_service.list_connection_targets(connection)

        self.assertEqual(["MYSCHEMA.MYTABLE"], targets)
        self.assertEqual(cursor.execute.call_count, 2)
        self.assertEqual(cursor.tables.call_count, 1)

    def test_legacy_csv_connection_named_after_a_file_is_treated_as_single_file(self) -> None:
        connection = Connection(
            id=14,
            name="customers",
            connection_type=ConnectionType.CSV,
            owner_username="tester",
            config={"base_path": str(self.csv_path.parent)},
        )

        self.assertEqual(self.csv_path, self.service.connector_service.csv_connection_file(connection))

    def test_invalid_rule_is_recorded_as_error(self) -> None:
        rule = Rule(
            id=2,
            name="missing column",
            rule_type=RuleType.NOT_NULL,
            dataset_id=None,
            owner_username="tester",
            config={"source_connection_id": 11, "source_kind": "csv_file", "source_name": "customers.csv", "source_sql": "", "column": "does_not_exist"},
        )

        run = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual("error", run.status)
        self.assertIn("does_not_exist", run.summary_json["error"])

    def test_rule_with_unknown_connection_is_recorded_as_clear_error(self) -> None:
        rule = Rule(
            id=20,
            name="unknown source",
            rule_type=RuleType.NOT_NULL,
            dataset_id=None,
            owner_username="tester",
            config={
                "source_connection_id": 999,
                "source_kind": "csv_file",
                "source_name": "customers.csv",
                "column": "id",
            },
        )

        run = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual("error", run.status)
        self.assertIn("source connection no longer exists", run.summary_json["error"])

    def test_rule_source_mode_must_match_its_connection_type(self) -> None:
        rule = Rule(
            id=21,
            name="wrong source mode",
            rule_type=RuleType.NOT_NULL,
            dataset_id=None,
            owner_username="tester",
            config={
                "source_connection_id": 11,
                "source_kind": "oracle_table",
                "source_name": "CUSTOMERS",
                "column": "id",
            },
        )

        run = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual("error", run.status)
        self.assertIn("must use CSV File mode", run.summary_json["error"])

    def test_latin1_semicolon_csv_is_detected_automatically(self) -> None:
        rule = Rule(
            id=3,
            name="names required",
            rule_type=RuleType.NOT_NULL,
            dataset_id=None,
            owner_username="tester",
            config={"source_connection_id": 12, "source_kind": "csv_file", "source_name": ".runtime-latin1.csv", "source_sql": "", "column": "name"},
        )

        run = self.service.run_rules([rule], {}, {12: self.latin1_connection}, self.results_dir, "tester")[0]

        self.assertEqual("passed", run.status)
        self.assertEqual(2, run.summary_json["checked_count"])
        schema = self.service.connector_service.detect_csv_schema(self.non_utf8_csv_path)
        self.assertEqual(["id", "name"], [column["name"] for column in schema])

    def test_unique_rule_accepts_singular_column_setting(self) -> None:
        rule = Rule(
            id=4,
            name="unique id",
            rule_type=RuleType.UNIQUE,
            dataset_id=None,
            owner_username="tester",
            config={"source_connection_id": 11, "source_kind": "csv_file", "source_name": "customers.csv", "source_sql": "", "column": "id"},
        )

        run = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual("failed", run.status)
        self.assertEqual(2, run.summary_json["failed_count"])

    def test_empty_unique_rule_reports_required_columns(self) -> None:
        rule = Rule(
            id=5,
            name="invalid unique",
            rule_type=RuleType.UNIQUE,
            dataset_id=None,
            owner_username="tester",
            config={"source_connection_id": 11, "source_kind": "csv_file", "source_name": "customers.csv", "source_sql": ""},
        )

        run = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual("error", run.status)
        self.assertIn("Missing required setting: columns", run.summary_json["error"])

    def test_referential_integrity_between_two_csv_datasets(self) -> None:
        rule = Rule(
            id=6,
            name="orders require customers",
            rule_type=RuleType.REFERENTIAL_INTEGRITY,
            dataset_id=None,
            owner_username="tester",
            config={
                "source_connection_id": 11,
                "source_kind": "csv_file",
                "source_name": "orders.csv",
                "source_sql": "",
                "source_key": "customer_id",
                "target_connection_id": 11,
                "target_kind": "csv_file",
                "target_name": "customers.csv",
                "target_sql": "",
                "target_key": "id",
            },
        )

        run = self.service.run_rules(
            [rule],
            {},
            {11: self.csv_connection},
            self.results_dir,
            "tester",
        )[0]

        self.assertEqual("failed", run.status)
        self.assertEqual(3, run.summary_json["checked_count"])
        self.assertEqual(1, run.summary_json["failed_count"])
        self.assertIn("11,3", Path(run.failed_rows_path).read_text(encoding="utf-8"))

    def _unique_id_rule(self, rule_id: int, **extra_config: object) -> Rule:
        # customers.csv has 3 rows with ids 1, 2, 2 -> the two id=2 rows always fail a UNIQUE check.
        return Rule(
            id=rule_id,
            name=f"unique ids #{rule_id}",
            rule_type=RuleType.UNIQUE,
            dataset_id=None,
            owner_username="tester",
            config={
                "source_connection_id": 11,
                "source_kind": "csv_file",
                "source_name": "customers.csv",
                "source_sql": "",
                "columns": ["id"],
                **extra_config,
            },
        )

    def test_fail_threshold_count_allows_failures_up_to_the_limit(self) -> None:
        rule = self._unique_id_rule(7, fail_threshold_count=2)

        run = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual("passed", run.status)
        self.assertEqual(2, run.summary_json["failed_count"])
        self.assertEqual(2, run.summary_json["fail_threshold_allowed"])

    def test_fail_threshold_count_still_fails_once_exceeded(self) -> None:
        rule = self._unique_id_rule(8, fail_threshold_count=1)

        run = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual("failed", run.status)

    def test_fail_threshold_percent_allows_proportional_failures(self) -> None:
        # 2 of 3 rows fail (~67%); a 70% tolerance should still pass.
        rule = self._unique_id_rule(9, fail_threshold_percent=70)

        run = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual("passed", run.status)

    def test_fail_threshold_percent_below_failure_rate_still_fails(self) -> None:
        # A 50% tolerance only allows 1 of 3 rows to fail, but 2 fail here.
        rule = self._unique_id_rule(10, fail_threshold_percent=50)

        run = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual("failed", run.status)

    def test_explicit_zero_thresholds_match_default_any_failure_fails_behavior(self) -> None:
        rule = self._unique_id_rule(11, fail_threshold_count=0, fail_threshold_percent=0)

        run = self.service.run_rules([rule], {}, {11: self.csv_connection}, self.results_dir, "tester")[0]

        self.assertEqual("failed", run.status)
        self.assertNotIn("fail_threshold_allowed", run.summary_json)


if __name__ == "__main__":
    unittest.main()
