from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from dqtool.models.entities import Connection, ConnectionType
from dqtool.web_app import DQToolWebApp, bounded_task_results


class AnomalyBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_tasks_limit_concurrency_and_isolate_failures(self) -> None:
        active = 0
        peak = 0

        async def worker(target: str) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                if target == "broken":
                    raise RuntimeError("profile failed")
                return target.upper()
            finally:
                active -= 1

        results = []
        async for result in bounded_task_results(["one", "two", "broken", "three", "four"], worker, limit=3):
            results.append(result)

        self.assertEqual(3, peak)
        self.assertEqual({"one", "two", "broken", "three", "four"}, {item[0] for item in results})
        failed = next(item for item in results if item[0] == "broken")
        self.assertIsNone(failed[1])
        self.assertIsInstance(failed[2], RuntimeError)
        self.assertEqual("FOUR", next(item[1] for item in results if item[0] == "four"))

    def test_selected_targets_are_normalized_and_deduplicated(self) -> None:
        app = DQToolWebApp()
        app.anomaly_target_select = SimpleNamespace(value=[" customers ", "orders", "customers", ""])

        self.assertEqual(["customers", "orders"], app._selected_anomaly_targets())

    def test_anomaly_source_config_keeps_connection_and_target(self) -> None:
        connection = Connection(id=7, name="Sales", connection_type=ConnectionType.ORACLE, owner_username="tester")

        config = DQToolWebApp._anomaly_source_config(connection, "SALES.ORDERS")

        self.assertEqual(7, config["source_connection_id"])
        self.assertEqual("oracle_table", config["source_kind"])
        self.assertEqual("SALES.ORDERS", config["source_name"])

    def test_batch_progress_updates_percentage_stage_and_remaining_value(self) -> None:
        app = DQToolWebApp()
        app.anomaly_batch_table = SimpleNamespace(
            rows=[{"id": "orders", "status": "Running"}],
            update=lambda: None,
        )

        app._update_anomaly_batch_progress("orders", 0.425, "Content analysis: email")

        row = app.anomaly_batch_table.rows[0]
        self.assertEqual("42%", row["progress"])
        self.assertEqual(0.425, row["progress_value"])
        self.assertEqual("Content analysis: email", row["details"])


if __name__ == "__main__":
    unittest.main()
