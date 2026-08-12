from __future__ import annotations

import unittest

from dqtool.models.entities import RuleRun
from dqtool.web_app import dashboard_daily_metrics, filter_runs_for_rule, format_profile_mean, missing_or_blank_percent


def _run(
    status: str, started_at: str, failed_count: int = 0, runtime_ms: int | None = None, rule_id: int = 1
) -> RuleRun:
    return RuleRun(
        id=None,
        rule_id=rule_id,
        dataset_id=0,
        status=status,
        executed_by="tester",
        started_at=started_at,
        summary_json={"failed_count": failed_count},
        runtime_ms=runtime_ms,
    )


class DashboardTrendTests(unittest.TestCase):
    def test_profile_mean_uses_regular_decimal_notation(self) -> None:
        self.assertEqual("91,430", format_profile_mean(9.143e4))
        self.assertEqual("0.125", format_profile_mean(0.125))

    def test_missing_or_blank_percent_combines_non_overlapping_rates(self) -> None:
        self.assertEqual(37.5, missing_or_blank_percent({"null_rate": 0.125, "blank_rate": 0.25}))

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

    def test_filter_runs_for_rule_excludes_other_rules(self) -> None:
        runs = [
            _run("passed", "2026-07-20T10:00:00+00:00", rule_id=1),
            _run("failed", "2026-07-20T11:00:00+00:00", rule_id=2),
        ]

        selected_runs = filter_runs_for_rule(runs, 1)

        self.assertEqual([1], [run.rule_id for run in selected_runs])
        self.assertEqual([], filter_runs_for_rule(runs, None))


if __name__ == "__main__":
    unittest.main()
