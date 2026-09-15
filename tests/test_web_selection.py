from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from dqtool.models.entities import Rule, RuleRun, RuleType
from dqtool.web_app import DQToolWebApp


class FakeElement:
    def __init__(self, *, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.selected: list[dict] = []
        self.value = None

    def update(self) -> None:
        pass


class WebSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = DQToolWebApp()
        self.rule_row = {"stable_key": "rule:7", "id": 7, "kind": "rule", "name": "Required email"}
        self.app.overview_table = FakeElement(rows=[self.rule_row])

    def test_clicking_overview_rule_row_selects_rule(self) -> None:
        event = SimpleNamespace(args=[{}, self.rule_row, 0])

        self.app._select_overview_row(event)

        self.assertEqual("rule:7", self.app.selected_item_key)
        self.assertEqual([self.rule_row], self.app.overview_table.selected)

    def test_selection_action_buttons_enable_once_something_is_selected(self) -> None:
        button = Mock()
        self.app.selection_action_buttons = [button]
        event = SimpleNamespace(args=[{}, self.rule_row, 0])

        self.app._select_overview_row(event)

        button.enable.assert_called_once_with()
        button.disable.assert_not_called()

    def test_clicking_a_run_chip_selects_that_run(self) -> None:
        # _populate_result_runs is what rebuilds the chips and redraws run details/failed
        # rows for the new selection - _select_run just needs to set state and trigger it.
        self.app._populate_result_runs = Mock()

        self.app._select_run(12)

        self.assertEqual("12", self.app.selected_run_id)
        self.app._populate_result_runs.assert_called_once_with()

    def test_freshness_message_describes_the_newest_value_not_a_failed_row(self) -> None:
        rule = Rule(
            id=7,
            name="Recent loads",
            rule_type=RuleType.DATA_FRESHNESS,
            dataset_id=None,
            owner_username="tester",
            config={"column": "loaded_at", "max_age_days": 1},
        )
        run = RuleRun(
            id=12,
            rule_id=7,
            dataset_id=0,
            status="failed",
            executed_by="tester",
            started_at="2026-08-11T10:00:00+00:00",
            finished_at="2026-08-11T10:00:01+00:00",
            summary_json={
                "latest_value": "2026-08-08T00:00:00+00:00",
                "freshness_age_days": 3.42,
                "max_age_days": 1,
            },
        )

        message = self.app._freshness_run_message(rule, run)

        self.assertIn("newest 'loaded_at' value", message)
        self.assertIn("maximum allowed age is 1 day", message)
        self.assertNotIn("of 1,000 rows", message)


if __name__ == "__main__":
    unittest.main()
