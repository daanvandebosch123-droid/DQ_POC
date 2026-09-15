from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

import duckdb

from dqtool.models.entities import Connection, ConnectionType
from dqtool.services.connectors import ConnectorService
from dqtool.services.profiling import (
    TEXT_INFERENCE_SAMPLE_LIMIT,
    ProfileAborted,
    ProfilingService,
    _placeholder_findings,
    detect_anomalies,
    gdpr_risk_findings,
    profile_rule_suggestions,
    source_profile_key,
)
from dqtool.services.storage import Storage


class ProfilingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent
        self.connection = Connection(
            id=11,
            name="fixture-csv",
            connection_type=ConnectionType.CSV,
            owner_username="tester",
            config={"base_path": str(self.root / "fixtures")},
        )
        self.source_config = {
            "source_connection_id": 11,
            "source_kind": "csv_file",
            "source_name": "customers.csv",
            "source_sql": "",
        }
        self.service = ProfilingService(ConnectorService())

    def test_profiles_csv_source(self) -> None:
        profile = self.service.profile_rule_source(self.source_config, {11: self.connection})

        # customers.csv: 3 rows with ids 1, 2, 2 and names Alice, Bob, Charlie
        self.assertEqual(3, profile["row_count"])
        self.assertIn("id", profile["columns"])
        id_stats = profile["columns"]["id"]
        self.assertEqual(2, id_stats["distinct_count"])
        self.assertEqual(3, profile["columns"]["name"]["distinct_count"])
        self.assertEqual("Alice", profile["columns"]["name"]["min"])
        self.assertEqual("Charlie", profile["columns"]["name"]["max"])
        self.assertEqual(0.0, id_stats["null_rate"])
        self.assertEqual(3, id_stats["non_null_count"])
        self.assertEqual(2, profile["total_column_count"])
        self.assertFalse(profile["profile_limit_reached"])
        self.assertEqual("Alice", profile["columns"]["name"]["top_values"][0]["value"])
        self.assertTrue(profile["profiled_at"])

    def test_profile_reports_monotonic_stage_progress_until_complete(self) -> None:
        updates: list[tuple[float, str]] = []

        self.service.profile_rule_source(
            self.source_config,
            {11: self.connection},
            progress_callback=lambda value, stage: updates.append((value, stage)),
        )

        self.assertGreater(len(updates), 3)
        self.assertEqual(1.0, updates[-1][0])
        self.assertEqual("Profile complete", updates[-1][1])
        self.assertEqual(sorted(value for value, _stage in updates), [value for value, _stage in updates])

    def test_profile_stops_before_opening_source_when_abort_is_requested(self) -> None:
        with self.assertRaises(ProfileAborted):
            self.service.profile_rule_source(self.source_config, {11: self.connection}, should_abort=lambda: True)

    def test_csv_abort_stops_at_each_requested_stage_and_closes_connection(self) -> None:
        for stage_to_abort in ("Calculating column statistics", "Content analysis: name", "Frequency analysis: name"):
            with self.subTest(stage=stage_to_abort):
                stopped = Event()
                updates = []

                def progress(_value, stage, updates=updates, target=stage_to_abort, stopped=stopped):
                    updates.append(stage)
                    if stage == target:
                        stopped.set()

                raw_connection = duckdb.connect()
                connection = MagicMock(wraps=raw_connection)
                type(connection).description = property(lambda _self, raw=raw_connection: raw.description)
                with patch("dqtool.services.profiling.duckdb.connect", return_value=connection):
                    with self.assertRaises(ProfileAborted):
                        self.service.profile_rule_source(
                            self.source_config, {11: self.connection}, progress_callback=progress,
                            should_abort=stopped.is_set,
                        )
                self.assertEqual(stage_to_abort, updates[-1])
                self.assertNotIn("Profile complete", updates)
                connection.close.assert_called_once()

    def test_csv_summary_is_computed_once(self) -> None:
        raw_connection = duckdb.connect()
        connection = MagicMock(wraps=raw_connection)
        type(connection).description = property(lambda _self: raw_connection.description)
        with patch("dqtool.services.profiling.duckdb.connect", return_value=connection):
            self.service.profile_rule_source(self.source_config, {11: self.connection})
        summaries = [call for call in connection.execute.call_args_list if call.args[0].startswith("SUMMARIZE")]
        self.assertEqual(1, len(summaries))

    def test_database_cursor_stays_open_and_is_closed_on_success_error_and_abort(self) -> None:
        connection = Connection(id=50, name="oracle", connection_type=ConnectionType.ORACLE, owner_username="tester")
        config = {"source_connection_id": 50, "source_kind": "oracle_table", "source_name": "orders"}
        for outcome in ("success", "error", "abort"):
            with self.subTest(outcome=outcome):
                state = {"open": False, "abort": False}
                cursor = MagicMock()
                cursor.description = [("amount", "NUMBER")]
                cursor.fetchone.side_effect = [(2,), (2, 2, 1, 2, 1.5, 0.5)]
                db_connection = MagicMock()
                context = db_connection.cursor.return_value

                def enter(*_args, state=state, cursor=cursor):
                    state["open"] = True
                    return cursor

                def leave(*_args, state=state):
                    state["open"] = False
                    return False

                def execute(sql, state=state, outcome=outcome):
                    self.assertTrue(state["open"], "Query used a closed cursor")
                    if "COUNT(DISTINCT" in sql:
                        if outcome == "error":
                            raise RuntimeError("aggregate failed")
                        state["abort"] = outcome == "abort"

                context.__enter__.side_effect = enter
                context.__exit__.side_effect = leave
                cursor.execute.side_effect = execute
                with patch.object(self.service.connector_service, "connect_database", return_value=db_connection):
                    if outcome == "success":
                        profile = self.service.profile_rule_source(config, {50: connection})
                        self.assertEqual(1.5, profile["columns"]["amount"]["mean"])
                    else:
                        expected = ProfileAborted if outcome == "abort" else RuntimeError
                        with self.assertRaises(expected):
                            self.service.profile_rule_source(
                                config, {50: connection}, should_abort=lambda state=state: state["abort"],
                            )
                self.assertFalse(state["open"])
                context.__exit__.assert_called_once()
                db_connection.close.assert_called_once()

    def test_database_abort_between_frequency_columns_stops_remaining_queries(self) -> None:
        connection = Connection(id=51, name="db2", connection_type=ConnectionType.DB2, owner_username="tester")
        config = {"source_connection_id": 51, "source_kind": "oracle_table", "source_name": "orders"}
        cursor = MagicMock()
        cursor.description = [("a", str), ("b", str)]
        cursor.fetchone.side_effect = [(2,), (2, 2, "A", "B", 0, 2, 2, "C", "D", 0)]
        cursor.fetchmany.side_effect = [[("A", "C"), ("B", "D")], []]
        cursor.fetchall.return_value = [("A", 1), ("B", 1)]
        db_connection = MagicMock()
        db_connection.cursor.return_value.__enter__.return_value = cursor
        stopped = False

        def progress(_value, stage):
            nonlocal stopped
            stopped = stage == "Frequency analysis: a"

        with patch.object(self.service.connector_service, "connect_database", return_value=db_connection):
            with self.assertRaises(ProfileAborted):
                self.service.profile_rule_source(
                    config, {51: connection}, progress_callback=progress, should_abort=lambda: stopped,
                )
        queries = [call.args[0] for call in cursor.execute.call_args_list if "GROUP BY" in call.args[0]]
        self.assertEqual(1, len(queries))
        self.assertIn('q."a"', queries[0])
        db_connection.close.assert_called_once()

    def test_frequency_coverage_explains_skips_and_preserves_full_counts(self) -> None:
        columns = {
            "code": {"type": "VARCHAR", "distinct_count": 101},
            "status": {"type": "VARCHAR", "distinct_count": 1},
            "empty": {"type": "VARCHAR", "distinct_count": 0},
            "number": {"type": "BIGINT", "distinct_count": 101},
        }
        connection = Connection(id=52, name="db2", connection_type=ConnectionType.DB2, owner_username="tester")
        with duckdb.connect() as con:
            con.execute("CREATE VIEW profile_view AS SELECT 'code-' || range AS code, 'active' AS status FROM range(101)")
            self.service._add_duckdb_frequency_analysis(con, columns, 101, [])
        cursor = MagicMock()
        cursor.fetchall.return_value = [("active", 101)]
        database_columns = {name: {key: value for key, value in stats.items() if key in {"type", "distinct_count"}}
                            for name, stats in columns.items()}
        self.service._add_database_frequency_analysis(
            cursor, "SELECT * FROM orders", connection, {"code", "status", "empty"}, database_columns, 101, [],
        )
        for result in (columns, database_columns):
            self.assertEqual("Skipped: more than 100 distinct values", result["code"]["frequency_status"])
            self.assertEqual("No non-null values", result["empty"]["frequency_status"])
            self.assertEqual("Not applicable: non-text column", result["number"]["frequency_status"])
            self.assertEqual(101, result["status"]["top_values"][0]["count"])
            self.assertEqual(1.0, result["status"]["top_values"][0]["share"])
        cursor.execute.assert_called_once()

    def test_inference_match_rate_describes_selected_date_type(self) -> None:
        cursor = MagicMock()
        cursor.fetchmany.side_effect = [[("20260101",)] * 4 + [("42",)], []]
        columns = {"code": {"inferred_type": "text", "non_null_count": 5}}
        self.service._infer_database_text_stats(
            cursor, "SELECT * FROM orders", self.connection, {"code"}, columns, 5, sample_limit=None,
        )
        self.assertEqual("date/time", columns["code"]["inferred_type"])
        self.assertEqual(0.8, columns["code"]["inference_confidence"])

    def test_placeholder_detection_flags_masked_values(self) -> None:
        findings = _placeholder_findings("status", [{"value": "***", "count": 9, "share": 0.9}])

        self.assertEqual("status", findings[0]["column"])
        self.assertIn("Placeholder value '***' occurs 9 time(s) (90.0%)", findings[0]["message"])

    def test_sqlserver_profile_uses_stdev_and_profiles_date_ranges(self) -> None:
        connection = Connection(
            id=22,
            name="sqlserver",
            connection_type=ConnectionType.SQLSERVER,
            owner_username="tester",
        )
        cursor = MagicMock()
        cursor.description = [("amount", "INTEGER"), ("loaded_at", "DATETIME")]
        cursor.fetchone.side_effect = [
            (2,),
            (2, 2, 1, 2, 1.5, 0.5, 2, 2, "2026-01-01", "2026-01-02"),
        ]
        db_connection = MagicMock()
        db_connection.cursor.return_value.__enter__.return_value = cursor
        config = {"source_connection_id": 22, "source_kind": "oracle_table", "source_name": "orders", "source_sql": ""}

        with patch.object(self.service.connector_service, "connect_database", return_value=db_connection):
            profile = self.service.profile_rule_source(config, {22: connection})

        aggregate_sql = cursor.execute.call_args_list[2].args[0]
        self.assertIn("STDEV(\"amount\")", aggregate_sql)
        self.assertNotIn("STDDEV(\"amount\")", aggregate_sql)
        self.assertEqual("2026-01-01", profile["columns"]["loaded_at"]["min"])
        self.assertEqual("2026-01-02", profile["columns"]["loaded_at"]["max"])

    def test_profiles_space_only_text_as_blank_separately_from_sql_null(self) -> None:
        config = {**self.source_config, "source_name": "blank_values.csv"}

        profile = self.service.profile_rule_source(config, {11: self.connection})

        stats = profile["columns"]["code"]
        self.assertEqual(0.0, stats["null_rate"])
        self.assertEqual(1, stats["blank_count"])
        self.assertEqual(0.5, stats["blank_rate"])
        self.assertIn("blank or contain spaces only", profile["content_findings"][0]["message"])

    def test_db2_profile_counts_space_only_text_as_blank(self) -> None:
        connection = Connection(
            id=23,
            name="db2",
            connection_type=ConnectionType.DB2,
            owner_username="tester",
        )
        cursor = MagicMock()
        cursor.description = [("code", str)]
        cursor.fetchone.side_effect = [(3,), (3, 3, "0", "2", 1)]
        cursor.fetchmany.side_effect = [[("0",), ("1",), ("2",)], []]
        db_connection = MagicMock()
        db_connection.cursor.return_value.__enter__.return_value = cursor
        config = {"source_connection_id": 23, "source_kind": "oracle_table", "source_name": "orders", "source_sql": ""}

        with patch.object(self.service.connector_service, "connect_database", return_value=db_connection):
            profile = self.service.profile_rule_source(config, {23: connection})

        self.assertIn('TRIM("code") = \'\'', cursor.execute.call_args_list[2].args[0])
        self.assertEqual(1, profile["columns"]["code"]["blank_count"])
        self.assertAlmostEqual(1 / 3, profile["columns"]["code"]["blank_rate"], places=5)
        self.assertEqual("numeric text", profile["columns"]["code"]["inferred_type"])
        self.assertEqual(0.0, profile["columns"]["code"]["min"])
        self.assertEqual(2.0, profile["columns"]["code"]["max"])
        self.assertEqual(1.0, profile["columns"]["code"]["mean"])

    def test_database_text_date_is_inferred_before_numeric_text(self) -> None:
        connection = Connection(id=24, name="db2", connection_type=ConnectionType.DB2, owner_username="tester")
        cursor = MagicMock()
        cursor.description = [("loaded_at", str)]
        cursor.fetchone.side_effect = [(2,), (2, 2, "20260101", "20260201", 0)]
        cursor.fetchmany.side_effect = [[("20260101",), ("20260201",)], []]
        db_connection = MagicMock()
        db_connection.cursor.return_value.__enter__.return_value = cursor
        config = {"source_connection_id": 24, "source_kind": "oracle_table", "source_name": "orders", "source_sql": ""}

        with patch.object(self.service.connector_service, "connect_database", return_value=db_connection):
            profile = self.service.profile_rule_source(config, {24: connection})

        self.assertEqual("date/time", profile["columns"]["loaded_at"]["inferred_type"])
        self.assertEqual("2026-01-01", profile["columns"]["loaded_at"]["min"])
        self.assertEqual("2026-02-01", profile["columns"]["loaded_at"]["max"])

    def test_database_text_inference_is_bounded_and_records_sample_evidence(self) -> None:
        connection = Connection(id=26, name="db2", connection_type=ConnectionType.DB2, owner_username="tester")
        cursor = MagicMock()
        cursor.description = [("amount_text", str)]
        cursor.fetchone.side_effect = [(100_000,), (100_000, 100_000, "1", "3", 0)]
        cursor.fetchmany.side_effect = [[("1",), ("2",), ("3",)], []]
        db_connection = MagicMock()
        db_connection.cursor.return_value.__enter__.return_value = cursor
        config = {"source_connection_id": 26, "source_kind": "oracle_table", "source_name": "orders", "source_sql": ""}

        with patch.object(self.service.connector_service, "connect_database", return_value=db_connection):
            profile = self.service.profile_rule_source(config, {26: connection})

        inference_sql = cursor.execute.call_args_list[3].args[0]
        self.assertIn(f"FETCH FIRST {TEXT_INFERENCE_SAMPLE_LIMIT} ROWS ONLY", inference_sql)
        self.assertEqual(3, profile["text_inference_rows"])
        self.assertTrue(profile["text_inference_sampled"])
        stats = profile["columns"]["amount_text"]
        self.assertEqual(3, stats["inference_rows_scanned"])
        self.assertEqual(3, stats["inference_sample_size"])
        self.assertEqual(1.0, stats["inference_confidence"])
        self.assertTrue(stats["inference_sampled"])
        self.assertEqual(2.0, stats["mean"])

    def test_deep_database_text_inference_does_not_add_a_row_limit(self) -> None:
        connection = Connection(id=27, name="db2", connection_type=ConnectionType.DB2, owner_username="tester")
        cursor = MagicMock()
        cursor.description = [("code", str)]
        cursor.fetchone.side_effect = [(2,), (2, 2, "1", "2", 0)]
        cursor.fetchmany.side_effect = [[("1",), ("2",)], []]
        db_connection = MagicMock()
        db_connection.cursor.return_value.__enter__.return_value = cursor
        config = {"source_connection_id": 27, "source_kind": "oracle_table", "source_name": "orders", "source_sql": ""}

        with patch.object(self.service.connector_service, "connect_database", return_value=db_connection):
            profile = self.service.profile_rule_source(config, {27: connection}, text_inference_limit=None)

        inference_sql = cursor.execute.call_args_list[3].args[0]
        self.assertNotIn("FETCH FIRST", inference_sql)
        self.assertFalse(profile["text_inference_sampled"])
        self.assertFalse(profile["columns"]["code"]["inference_sampled"])
        self.assertIsNone(profile["text_inference_sample_limit"])

    def test_database_text_min_max_exclude_blank_values(self) -> None:
        connection = Connection(id=25, name="db2", connection_type=ConnectionType.DB2, owner_username="tester")
        cursor = MagicMock()
        cursor.description = [("label", str)]
        cursor.fetchone.side_effect = [(3,), (3, 2, "A", "Z", 1)]
        cursor.fetchmany.side_effect = [[(" ",), ("A",), ("Z",)], []]
        db_connection = MagicMock()
        db_connection.cursor.return_value.__enter__.return_value = cursor
        config = {"source_connection_id": 25, "source_kind": "oracle_table", "source_name": "orders", "source_sql": ""}

        with patch.object(self.service.connector_service, "connect_database", return_value=db_connection):
            profile = self.service.profile_rule_source(config, {25: connection})

        aggregate_sql = cursor.execute.call_args_list[2].args[0]
        self.assertIn("MIN(NULLIF(TRIM(\"label\"), ''))", aggregate_sql)
        self.assertEqual("A", profile["columns"]["label"]["min"])
        self.assertEqual("Z", profile["columns"]["label"]["max"])

    def test_distinct_counts_never_exceed_row_count(self) -> None:
        # SUMMARIZE's approx_unique overshoots (e.g. 11,693 distinct in a 10,000-row file);
        # the profile must report exact counts.
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "big.csv"
            rows = "\n".join(f"{index},customer-{index}" for index in range(10000))
            csv_path.write_text("customer_id,label\n" + rows + "\n", encoding="utf-8")
            connection = Connection(
                id=31,
                name="big",
                connection_type=ConnectionType.CSV,
                owner_username="tester",
                config={"file_path": str(csv_path), "base_path": tmp},
            )
            config = {"source_connection_id": 31, "source_kind": "csv_file", "source_name": "big.csv", "source_sql": ""}
            profile = self.service.profile_rule_source(config, {31: connection})

        self.assertEqual(10000, profile["row_count"])
        for name, stats in profile["columns"].items():
            self.assertLessEqual(stats["distinct_count"], profile["row_count"], name)
        self.assertEqual(10000, profile["columns"]["customer_id"]["distinct_count"])

    def test_source_profile_key_is_stable(self) -> None:
        self.assertEqual(
            source_profile_key(self.source_config),
            source_profile_key(dict(self.source_config)),
        )
        sql_config = {"source_connection_id": 2, "source_kind": "oracle_sql", "source_sql": "SELECT 1 FROM dual"}
        self.assertEqual(source_profile_key(sql_config), source_profile_key(dict(sql_config)))
        self.assertNotEqual(source_profile_key(self.source_config), source_profile_key(sql_config))

    def test_profile_snapshots_round_trip_through_storage(self) -> None:
        profile = self.service.profile_rule_source(self.source_config, {11: self.connection})
        key = source_profile_key(self.source_config)
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "t.sqlite")
            storage.initialize()
            self.assertIsNone(storage.latest_source_profile(key))
            storage.save_source_profile(key, profile)
            loaded = storage.latest_source_profile(key)
        self.assertEqual(profile["row_count"], loaded["row_count"])
        self.assertEqual(set(profile["columns"]), set(loaded["columns"]))


class ContentFindingsTests(unittest.TestCase):
    def test_flags_number_in_name_column_bad_email_and_outlier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "people.csv"
            csv_path.write_text(
                "name,email,amount\n"
                "Alice,alice@example.com,1\n"
                "Bob,bob@example.com,2\n"
                "Carol,carol@example.com,3\n"
                "Dave,dave@example.com,4\n"
                "Eve,eve@example.com,5\n"
                "Frank,frank@example.com,6\n"
                "Grace,grace@example.com,7\n"
                "Heidi,heidi@example.com,8\n"
                "Ivan,ivan@examplecom,9\n"
                "12345,judy@example.com,1000\n",
                encoding="utf-8",
            )
            connection = Connection(
                id=21,
                name="people",
                connection_type=ConnectionType.CSV,
                owner_username="tester",
                config={"file_path": str(csv_path), "base_path": tmp},
            )
            config = {"source_connection_id": 21, "source_kind": "csv_file", "source_name": "people.csv", "source_sql": ""}
            profile = ProfilingService(ConnectorService()).profile_rule_source(config, {21: connection})

        findings = profile["content_findings"]
        by_column = {finding["column"]: finding for finding in findings}
        self.assertIn("name", by_column)  # numeric value in text column
        self.assertIn("12345", by_column["name"]["message"])
        self.assertIn("email", by_column)  # email without dot in domain
        self.assertIn("ivan@examplecom", by_column["email"]["message"])
        self.assertIn("amount", by_column)  # 1000 is an IQR outlier
        self.assertIn("1000", by_column["amount"]["message"])
        # content findings surface on the very first check, without a previous snapshot
        self.assertEqual(findings, detect_anomalies(None, profile))


class DetectAnomaliesTests(unittest.TestCase):
    def _profile(self, row_count: int, columns: dict) -> dict:
        return {"profiled_at": "2026-07-02T10:00:00+00:00", "row_count": row_count, "columns": columns}

    def test_no_findings_for_identical_profiles(self) -> None:
        profile = self._profile(100, {"id": {"null_rate": 0.0, "distinct_count": 100, "mean": 50.0, "stddev": 10.0}})
        self.assertEqual([], detect_anomalies(profile, profile))

    def test_detects_row_count_drop_null_spike_and_missing_column(self) -> None:
        previous = self._profile(
            1000,
            {
                "id": {"null_rate": 0.0, "distinct_count": 1000, "mean": 500.0, "stddev": 5.0},
                "email": {"null_rate": 0.01, "distinct_count": 990, "mean": None, "stddev": None},
                "legacy": {"null_rate": 0.0, "distinct_count": 3, "mean": None, "stddev": None},
            },
        )
        current = self._profile(
            600,
            {
                "id": {"null_rate": 0.0, "distinct_count": 600, "mean": 900.0, "stddev": 5.0},
                "email": {"null_rate": 0.25, "distinct_count": 400, "mean": None, "stddev": None},
            },
        )

        findings = detect_anomalies(previous, current)
        kinds = {(item["severity"], item["column"]) for item in findings}

        self.assertIn(("high", None), kinds)  # row count -40%
        self.assertIn(("high", "email"), kinds)  # null spike
        self.assertIn(("medium", "legacy"), kinds)  # disappeared column
        self.assertIn(("medium", "id"), kinds)  # mean shift > 3 stddev
        self.assertEqual("high", findings[0]["severity"])  # sorted by severity

    def test_new_column_is_low_severity(self) -> None:
        previous = self._profile(10, {})
        current = self._profile(10, {"extra": {"null_rate": 0.0, "distinct_count": 10}})
        findings = detect_anomalies(previous, current)
        self.assertEqual([("low", "extra")], [(item["severity"], item["column"]) for item in findings])


class ProfileRuleSuggestionTests(unittest.TestCase):
    def test_suggests_editable_rules_from_profile_signals(self) -> None:
        profile = {
            "row_count": 10,
            "columns": {
                "customer_id": {
                    "inferred_type": "number",
                    "null_rate": 0.0,
                    "distinct_count": 10,
                    "min": 1,
                    "max": 3,
                },
                "email": {
                    "inferred_type": "email",
                    "null_rate": 0.0,
                    "distinct_count": 10,
                },
                "status": {
                    "inferred_type": "category",
                    "null_rate": 0.0,
                    "distinct_count": 2,
                    "sample_values": ["ACTIVE", "INACTIVE"],
                    "min_length": 6,
                    "max_length": 8,
                },
                "created_at": {"inferred_type": "date/time", "null_rate": 0.0, "distinct_count": 10},
                "ordernummer": {"inferred_type": "number", "null_rate": 0.0, "distinct_count": 9},
            },
        }

        suggestions = profile_rule_suggestions(profile)
        by_name = {suggestion["name"]: suggestion for suggestion in suggestions}

        self.assertEqual(["customer_id"], by_name["customer_id must be unique"]["config"]["columns"])
        self.assertEqual(1, by_name["customer_id must stay within range"]["config"]["min"])
        self.assertEqual("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", by_name["email must be a valid email"]["config"]["pattern"])
        self.assertEqual(["ACTIVE", "INACTIVE"], by_name["status must use allowed values"]["config"]["values"])
        self.assertEqual(6, by_name["status must have an expected length"]["config"]["min_length"])
        self.assertEqual("date_validity", by_name["created_at must be a valid date"]["rule_type"])
        self.assertEqual(
            ["ordernummer"], by_name["ordernummer must not contain duplicates"]["config"]["columns"]
        )

    def test_does_not_suggest_allowed_values_for_incomplete_samples(self) -> None:
        profile = {
            "row_count": 100,
            "columns": {
                "status": {
                    "inferred_type": "category",
                    "null_rate": 0.0,
                    "distinct_count": 3,
                    "sample_values": ["ACTIVE", "INACTIVE"],
                }
            },
        }

        suggestions = profile_rule_suggestions(profile)

        self.assertNotIn("status must use allowed values", {suggestion["name"] for suggestion in suggestions})


class GdprRiskFindingTests(unittest.TestCase):
    def test_flags_dutch_identifiers_and_special_category_names_without_values(self) -> None:
        findings = gdpr_risk_findings(
            {
                "rijksregisternummer": {"inferred_type": "text"},
                "medische_diagnose": {"inferred_type": "text"},
                "contact_email": {"inferred_type": "email"},
            }
        )

        by_column = {finding["column"]: finding for finding in findings}
        self.assertEqual("high", by_column["rijksregisternummer"]["severity"])
        self.assertIn("Special category", by_column["medische_diagnose"]["category"])
        self.assertIn("email", by_column["contact_email"]["category"])
        self.assertNotIn("value", by_column["contact_email"]["reason"].lower())


if __name__ == "__main__":
    unittest.main()
