from __future__ import annotations

import unittest

from dqtool.models.entities import RuleRun
from dqtool.web_app import dashboard_daily_metrics


def _run(status: str, started_at: str, failed_count: int = 0, runtime_ms: int | None = None) -> RuleRun:
    return RuleRun(
        id=None,
        rule_id=1,
        dataset_id=0,
        status=status,
        executed_by="tester",
        started_at=started_at,
        summary_json={"failed_count": failed_count},
        runtime_ms=runtime_ms,
    )


class DashboardTrendTests(unittest.TestCase):
    def test_daily_metrics_include_errors_in_volume_but_not_pass_rate(self) -> None:
        runs = [
            _run("passed", "2026-07-20T10:00:00+00:00", runtime_ms=100),
            _run("failed", "2026-07-20T11:00:00+00:00", failed_count=4, runtime_ms=300),
            _run("error", "2026-07-20T12:00:00+00:00"),
            _run("error", "2026-07-21T10:00:00+00:00"),
        ]

        days, pass_rates, volumes, failed_rows, runtimes = dashboard_daily_metrics(runs)

        self.assertEqual(["2026-07-20", "2026-07-21"], days)
        self.assertEqual([50.0, None], pass_rates)
        self.assertEqual([3, 1], volumes)
        self.assertEqual([4, 0], failed_rows)
        self.assertEqual([200, None], runtimes)

    def test_daily_metrics_keep_only_the_latest_thirty_days(self) -> None:
        runs = [_run("passed", f"2026-06-{day:02d}T10:00:00+00:00") for day in range(1, 31)]
        runs.append(_run("passed", "2026-07-01T10:00:00+00:00"))

        days, *_metrics = dashboard_daily_metrics(runs)

        self.assertEqual(30, len(days))
        self.assertEqual("2026-06-02", days[0])
        self.assertEqual("2026-07-01", days[-1])


if __name__ == "__main__":
    unittest.main()
