from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from dqtool.models.entities import Rule, RuleRun, RuleType, Schedule, ScheduleCadence, ScheduleTargetKind
from dqtool.services.scheduling import BRUSSELS_TIMEZONE, compute_next_run, describe_cadence
from dqtool.services.storage import Storage

# A fixed Thursday reference instant so cadence math is deterministic regardless of when tests run.
NOW = datetime(2026, 7, 16, 13, 14, tzinfo=UTC)


def _schedule(**overrides: object) -> Schedule:
    defaults: dict[str, object] = dict(
        id=None,
        name="s",
        target_kind=ScheduleTargetKind.RULE,
        target_id=1,
        cadence=ScheduleCadence.HOURLY,
        owner_username="tester",
    )
    defaults.update(overrides)
    return Schedule(**defaults)  # type: ignore[arg-type]


class ComputeNextRunTests(unittest.TestCase):
    def test_hourly_adds_interval(self) -> None:
        schedule = _schedule(cadence=ScheduleCadence.HOURLY, interval_hours=2)
        self.assertEqual(NOW.replace(hour=15), compute_next_run(schedule, after=NOW))

    def test_hourly_defaults_to_one_hour_when_interval_missing(self) -> None:
        schedule = _schedule(cadence=ScheduleCadence.HOURLY, interval_hours=0)
        self.assertEqual(NOW.replace(hour=14), compute_next_run(schedule, after=NOW))

    def test_daily_later_today_stays_today(self) -> None:
        schedule = _schedule(cadence=ScheduleCadence.DAILY, time_of_day="18:00")
        result = compute_next_run(schedule, after=NOW)
        self.assertEqual(datetime(2026, 7, 16, 18, 0, tzinfo=BRUSSELS_TIMEZONE), result)

    def test_daily_already_passed_rolls_to_tomorrow(self) -> None:
        schedule = _schedule(cadence=ScheduleCadence.DAILY, time_of_day="09:00")
        result = compute_next_run(schedule, after=NOW)
        self.assertEqual(datetime(2026, 7, 17, 9, 0, tzinfo=BRUSSELS_TIMEZONE), result)

    def test_weekly_same_weekday_later_today(self) -> None:
        # NOW is a Thursday (weekday index 3).
        schedule = _schedule(cadence=ScheduleCadence.WEEKLY, time_of_day="18:00", weekday=3)
        result = compute_next_run(schedule, after=NOW)
        self.assertEqual(datetime(2026, 7, 16, 18, 0, tzinfo=BRUSSELS_TIMEZONE), result)

    def test_weekly_same_weekday_already_passed_jumps_a_week(self) -> None:
        schedule = _schedule(cadence=ScheduleCadence.WEEKLY, time_of_day="09:00", weekday=3)
        result = compute_next_run(schedule, after=NOW)
        self.assertEqual(datetime(2026, 7, 23, 9, 0, tzinfo=BRUSSELS_TIMEZONE), result)

    def test_weekly_future_weekday_this_week(self) -> None:
        schedule = _schedule(cadence=ScheduleCadence.WEEKLY, time_of_day="09:00", weekday=5)  # Saturday
        result = compute_next_run(schedule, after=NOW)
        self.assertEqual(datetime(2026, 7, 18, 9, 0, tzinfo=BRUSSELS_TIMEZONE), result)

    def test_weekly_past_weekday_next_week(self) -> None:
        schedule = _schedule(cadence=ScheduleCadence.WEEKLY, time_of_day="09:00", weekday=0)  # Monday
        result = compute_next_run(schedule, after=NOW)
        self.assertEqual(datetime(2026, 7, 20, 9, 0, tzinfo=BRUSSELS_TIMEZONE), result)

    def test_malformed_time_of_day_falls_back_to_midnight(self) -> None:
        schedule = _schedule(cadence=ScheduleCadence.DAILY, time_of_day="not-a-time")
        result = compute_next_run(schedule, after=NOW)
        self.assertEqual(0, result.hour)
        self.assertEqual(0, result.minute)


class DescribeCadenceTests(unittest.TestCase):
    def test_hourly_singular(self) -> None:
        self.assertEqual("Every hour", describe_cadence(_schedule(cadence=ScheduleCadence.HOURLY, interval_hours=1)))

    def test_hourly_plural(self) -> None:
        self.assertEqual(
            "Every 6 hours", describe_cadence(_schedule(cadence=ScheduleCadence.HOURLY, interval_hours=6))
        )

    def test_daily(self) -> None:
        self.assertEqual(
            "Daily at 09:00 Brussels time", describe_cadence(_schedule(cadence=ScheduleCadence.DAILY, time_of_day="09:00"))
        )

    def test_weekly(self) -> None:
        self.assertEqual(
            "Weekly on Monday at 07:30 Brussels time",
            describe_cadence(_schedule(cadence=ScheduleCadence.WEEKLY, time_of_day="07:30", weekday=0)),
        )


class ScheduleStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = Storage(Path(tempfile.mkdtemp()) / "test.sqlite")
        self.storage.initialize()
        self.rule_id = self.storage.save_rule(
            Rule(id=None, name="a", rule_type=RuleType.NOT_NULL, dataset_id=None, owner_username="tester")
        )

    def test_save_and_list_round_trip(self) -> None:
        schedule_id = self.storage.save_schedule(
            _schedule(name="nightly", target_id=self.rule_id, cadence=ScheduleCadence.DAILY, time_of_day="02:00")
        )
        schedules = self.storage.list_schedules()
        self.assertEqual(1, len(schedules))
        self.assertEqual(schedule_id, schedules[0].id)
        self.assertEqual("nightly", schedules[0].name)
        self.assertEqual(ScheduleCadence.DAILY, schedules[0].cadence)
        self.assertTrue(schedules[0].enabled)

    def test_list_due_schedules_respects_enabled_and_next_run(self) -> None:
        due_id = self.storage.save_schedule(
            _schedule(name="due", target_id=self.rule_id, next_run_at="2026-01-01T00:00:00+00:00")
        )
        self.storage.save_schedule(
            _schedule(name="future", target_id=self.rule_id, next_run_at="2099-01-01T00:00:00+00:00")
        )
        self.storage.save_schedule(
            _schedule(name="disabled-but-due", target_id=self.rule_id, next_run_at="2026-01-01T00:00:00+00:00", enabled=False)
        )
        due = self.storage.list_due_schedules("2026-06-01T00:00:00+00:00")
        self.assertEqual([due_id], [item.id for item in due])

    def test_record_schedule_run_updates_timestamps_and_status(self) -> None:
        schedule_id = self.storage.save_schedule(_schedule(name="s", target_id=self.rule_id))
        self.storage.record_schedule_run(schedule_id, "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "passed")
        schedule = self.storage.get_schedule(schedule_id)
        assert schedule is not None
        self.assertEqual("2026-01-01T00:00:00+00:00", schedule.last_run_at)
        self.assertEqual("2026-01-02T00:00:00+00:00", schedule.next_run_at)
        self.assertEqual("passed", schedule.last_status)

    def test_rule_run_keeps_its_originating_schedule(self) -> None:
        schedule_id = self.storage.save_schedule(_schedule(name="s", target_id=self.rule_id))
        self.storage.save_rule_run(
            RuleRun(
                id=None,
                rule_id=self.rule_id,
                dataset_id=0,
                status="passed",
                executed_by="scheduler",
                started_at="2026-01-01T00:00:00+00:00",
                schedule_id=schedule_id,
                runtime_ms=125,
            )
        )
        run = self.storage.list_rule_runs()[0]
        self.assertEqual(schedule_id, run.schedule_id)
        self.assertEqual(125, run.runtime_ms)

    def test_delete_schedule(self) -> None:
        schedule_id = self.storage.save_schedule(_schedule(name="s", target_id=self.rule_id))
        self.storage.delete_schedule(schedule_id)
        self.assertEqual([], self.storage.list_schedules())


if __name__ == "__main__":
    unittest.main()
