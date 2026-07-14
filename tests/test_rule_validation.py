from __future__ import annotations

import unittest

from dqtool.models.entities import RuleType
from dqtool.services.rules import validate_rule_config


class RuleValidationTests(unittest.TestCase):
    def test_new_rule_requires_an_explicit_source_connection(self) -> None:
        errors = validate_rule_config(
            RuleType.NOT_NULL,
            {"column": "customer_id"},
            require_source=True,
        )

        self.assertIn("Source connection is required.", errors)
        self.assertIn("Source type is required.", errors)

    def test_source_reference_requires_a_table_file_or_sql(self) -> None:
        table_errors = validate_rule_config(
            RuleType.NOT_NULL,
            {"column": "customer_id", "source_connection_id": 1, "source_kind": "csv_file"},
            require_source=True,
        )
        sql_errors = validate_rule_config(
            RuleType.NOT_NULL,
            {"column": "customer_id", "source_connection_id": 1, "source_kind": "oracle_sql"},
            require_source=True,
        )

        self.assertIn("Source table or CSV file is required.", table_errors)
        self.assertIn("Source SQL is required for a custom SQL source.", sql_errors)

    def test_referential_integrity_requires_a_complete_target_reference(self) -> None:
        errors = validate_rule_config(
            RuleType.REFERENTIAL_INTEGRITY,
            {
                "source_connection_id": 1,
                "source_kind": "csv_file",
                "source_name": "orders.csv",
                "source_key": "customer_id",
                "target_key": "id",
            },
            require_source=True,
        )

        self.assertIn("Target connection is required.", errors)
        self.assertIn("Target type is required.", errors)

    def test_fail_threshold_count_cannot_be_negative(self) -> None:
        errors = validate_rule_config(
            RuleType.NOT_NULL,
            {"column": "customer_id", "fail_threshold_count": -1},
        )

        self.assertIn("Fail threshold (rows) cannot be negative.", errors)

    def test_fail_threshold_percent_must_be_between_0_and_100(self) -> None:
        too_high = validate_rule_config(
            RuleType.NOT_NULL,
            {"column": "customer_id", "fail_threshold_percent": 150},
        )
        too_low = validate_rule_config(
            RuleType.NOT_NULL,
            {"column": "customer_id", "fail_threshold_percent": -5},
        )

        self.assertIn("Fail threshold (%) must be between 0 and 100.", too_high)
        self.assertIn("Fail threshold (%) must be between 0 and 100.", too_low)

    def test_fail_threshold_defaults_are_valid(self) -> None:
        errors = validate_rule_config(RuleType.NOT_NULL, {"column": "customer_id"})

        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
