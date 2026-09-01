from __future__ import annotations

import asyncio
import threading
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

    async def test_bounded_tasks_do_not_start_queued_work_after_abort(self) -> None:
        stop = threading.Event()
        release_workers = asyncio.Event()
        started: list[str] = []

        async def worker(target: str) -> str:
            started.append(target)
            await release_workers.wait()
            return target

        async def collect() -> list[tuple[str, object | None, Exception | None]]:
            return [
                result
                async for result in bounded_task_results(
                    ["one", "two", "three", "four"], worker, limit=2, should_stop=stop.is_set
                )
            ]

        task = asyncio.create_task(collect())
        while len(started) < 2:
            await asyncio.sleep(0)
        stop.set()
        release_workers.set()
        results = await task

        self.assertEqual(["one", "two"], started)
        self.assertEqual({"one", "two"}, {item[0] for item in results})

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

    def test_abort_marks_queued_rows_and_requests_active_rows_to_stop(self) -> None:
        app = DQToolWebApp()
        app._anomaly_check_running = True
        app._anomaly_abort_event = threading.Event()
        app._anomaly_aborted_targets = set()
        app.abort_anomaly_button = SimpleNamespace(disable=lambda: None)
        app.anomaly_batch_table = SimpleNamespace(
            rows=[
                {"id": "orders", "status": "Running"},
                {"id": "customers", "status": "Queued"},
            ],
            update=lambda: None,
        )
        app.anomaly_batch_status = SimpleNamespace(text="", update=lambda: None)
        app.anomaly_summary = SimpleNamespace(content="", update=lambda: None)

        app.abort_anomaly_check()

        self.assertTrue(app._anomaly_abort_event.is_set())
        self.assertEqual("Stopping", app.anomaly_batch_table.rows[0]["status"])
        self.assertEqual("Aborted", app.anomaly_batch_table.rows[1]["status"])
        self.assertEqual({"customers"}, app._anomaly_aborted_targets)


if __name__ == "__main__":
    unittest.main()
