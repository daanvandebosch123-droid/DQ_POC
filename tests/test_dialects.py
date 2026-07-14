from __future__ import annotations

import unittest

from dqtool.models.entities import Rule, RuleType
from dqtool.services.connectors import ConnectorService
from dqtool.services.execution import ExecutionService


def _rule(rule_type: RuleType, config: dict) -> Rule:
    return Rule(id=1, name="t", rule_type=rule_type, dataset_id=None, owner_username="tester", config=config)


class DialectSqlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connectors = ConnectorService()
        self.service = ExecutionService(self.connectors)

    def test_limited_sql_uses_top_on_sqlserver_and_fetch_first_elsewhere(self) -> None:
        self.assertEqual("SELECT TOP 500 * FROM (SELECT 1) q", self.connectors.limited_sql("SELECT 1", 500, "sqlserver"))
        for dialect in ("oracle", "db2"):
            self.assertEqual(
                "SELECT * FROM (SELECT 1) q FETCH FIRST 500 ROWS ONLY",
                self.connectors.limited_sql("SELECT 1", 500, dialect),
            )

    def test_summary_needs_from_clause_on_oracle_and_db2(self) -> None:
        rule = _rule(RuleType.NOT_NULL, {"column": "id"})
        _, oracle_summary = self.service._build_rule_sql(rule, "tbl", dialect="oracle")
        _, db2_summary = self.service._build_rule_sql(rule, "tbl", dialect="db2")
        _, sqlserver_summary = self.service._build_rule_sql(rule, "tbl", dialect="sqlserver")
        self.assertTrue(oracle_summary.endswith(" FROM dual"))
        self.assertTrue(db2_summary.endswith(" FROM SYSIBM.SYSDUMMY1"))
        self.assertTrue(sqlserver_summary.endswith("AS failed_count"))

    def test_length_rule_uses_len_and_nvarchar_on_sqlserver(self) -> None:
        rule = _rule(RuleType.LENGTH, {"column": "code", "min_length": 1, "max_length": 5})
        failed_sql, _ = self.service._build_rule_sql(rule, "tbl", dialect="sqlserver")
        self.assertIn("LEN(CAST(\"code\" AS NVARCHAR(4000)))", failed_sql)

    def test_regex_rule_supported_per_dialect(self) -> None:
        rule = _rule(RuleType.REGEX, {"column": "email", "pattern": "^a+$"})
        failed_db2, _ = self.service._build_rule_sql(rule, "tbl", dialect="db2")
        self.assertIn("REGEXP_LIKE", failed_db2)
        with self.assertRaises(ValueError):
            self.service._build_rule_sql(rule, "tbl", dialect="sqlserver")

    def test_date_validity_per_dialect(self) -> None:
        rule = _rule(RuleType.DATE_VALIDITY, {"column": "d"})
        failed_sqlserver, _ = self.service._build_rule_sql(rule, "tbl", dialect="sqlserver")
        self.assertIn("TRY_CONVERT(date", failed_sqlserver)
        with self.assertRaises(ValueError):
            self.service._build_rule_sql(rule, "tbl", dialect="db2")


if __name__ == "__main__":
    unittest.main()
