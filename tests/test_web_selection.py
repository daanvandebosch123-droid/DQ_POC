from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

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
        self.app.rules_table = FakeElement(rows=[{"id": 7, "name": "Required email"}])
        self.app.results_table = FakeElement(rows=[{"id": 12, "status": "FAILED"}])
        self.app.rule_select = FakeElement()
        self.app.result_select = FakeElement()

    def test_clicking_rule_row_selects_rule(self) -> None:
        event = SimpleNamespace(args=[{}, {"id": 7, "name": "Required email"}, 0])

        self.app._select_rule_row(event)

        self.assertEqual("7", self.app.selected_rule_id)
        self.assertEqual("7", self.app.rule_select.value)
        self.assertEqual([{"id": 7, "name": "Required email"}], self.app.rules_table.selected)

    def test_clicking_result_row_selects_result_and_opens_details(self) -> None:
        self.app.view_selected_result = Mock()
        event = SimpleNamespace(args=[{}, {"id": 12, "status": "FAILED"}, 0])

        self.app._select_result_row(event)

        self.assertEqual("12", self.app.selected_run_id)
        self.assertEqual("12", self.app.result_select.value)
        self.assertEqual([{"id": 12, "status": "FAILED"}], self.app.results_table.selected)
        self.app.view_selected_result.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
