from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dqtool.models.entities import Rule, RuleRun, RuleType
from dqtool.services.storage import Storage


class RunHistoryStorageTests(unittest.TestCase):
    """A busy rule used to crowd every other rule out of the newest-runs window, which made
    the Results tree report quiet rules as never having run."""

    def setUp(self) -> None:
        self.storage = Storage(Path(tempfile.mkdtemp()) / "test.sqlite")
        self.storage.initialize()
        self.busy_rule = self.storage.save_rule(
            Rule(id=None, name="busy", rule_type=RuleType.NOT_NULL, dataset_id=None, owner_username="tester")
        )
        self.quiet_rule = self.storage.save_rule(
            Rule(id=None, name="quiet", rule_type=RuleType.NOT_NULL, dataset_id=None, owner_username="tester")
        )
        # The quiet rule failed first, then the busy rule ran often enough to bury it.
        self._save_run(self.quiet_rule, "failed", "2026-08-01T10:00:00+00:00", failed_count=7)
        for index in range(150):
            self._save_run(self.busy_rule, "passed", f"2026-08-02T{index % 24:02d}:00:00+00:00")

    def _save_run(self, rule_id: int, status: str, started_at: str, failed_count: int = 0) -> int:
        return self.storage.save_rule_run(
            RuleRun(
                id=None,
                rule_id=rule_id,
                dataset_id=0,
                status=status,
                executed_by="tester",
                started_at=started_at,
                finished_at=started_at,
                summary_json={"checked_count": 100, "failed_count": failed_count, "source_label": "orders.csv"},
            )
        )

    def test_quiet_rule_keeps_its_latest_run_however_busy_other_rules_are(self) -> None:
        aggregates = self.storage.rule_run_aggregates()

        self.assertEqual("failed", aggregates[self.quiet_rule]["status"])
        self.assertEqual(1, aggregates[self.quiet_rule]["runs"])
        self.assertEqual(7, aggregates[self.quiet_rule]["summary_json"]["failed_count"])
        self.assertEqual(150, aggregates[self.busy_rule]["runs"])
        self.assertEqual("passed", aggregates[self.busy_rule]["status"])

    def test_aggregates_report_the_newest_run_not_an_arbitrary_one(self) -> None:
        self._save_run(self.quiet_rule, "error", "2026-08-09T09:00:00+00:00")

        aggregates = self.storage.rule_run_aggregates()

        self.assertEqual("error", aggregates[self.quiet_rule]["status"])
        self.assertEqual(2, aggregates[self.quiet_rule]["runs"])

    def test_listing_runs_for_one_rule_is_not_capped_by_other_rules(self) -> None:
        runs = self.storage.list_rule_runs(limit=500, rule_ids=[self.quiet_rule])

        self.assertEqual([self.quiet_rule], sorted({run.rule_id for run in runs}))
        self.assertEqual(1, len(runs))

    def test_listing_runs_for_no_rules_returns_nothing(self) -> None:
        self.assertEqual([], self.storage.list_rule_runs(limit=500, rule_ids=[]))

    def test_unfiltered_listing_still_applies_a_global_limit(self) -> None:
        self.assertEqual(5, len(self.storage.list_rule_runs(limit=5)))

    def test_an_old_run_is_still_fetchable_by_id(self) -> None:
        # The quiet rule's run is far outside the newest-runs window; looking it up by id
        # has to keep working, or the details panel silently shows a stale run.
        old_run_id = self.storage.list_rule_runs(limit=500, rule_ids=[self.quiet_rule])[0].id
        self.assertNotIn(old_run_id, [run.id for run in self.storage.list_rule_runs()])

        run = self.storage.get_rule_run(old_run_id)

        self.assertIsNotNone(run)
        self.assertEqual(self.quiet_rule, run.rule_id)
        self.assertEqual("failed", run.status)

    def test_fetching_a_missing_run_returns_none(self) -> None:
        self.assertIsNone(self.storage.get_rule_run(999_999))


if __name__ == "__main__":
    unittest.main()
