from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dqtool.models.entities import Connection, ConnectionType
from dqtool.services.connectors import ConnectorService
from dqtool.services.profiling import (
    ProfilingService,
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
        self.assertEqual(0.0, id_stats["null_rate"])
        self.assertTrue(profile["profiled_at"])

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
