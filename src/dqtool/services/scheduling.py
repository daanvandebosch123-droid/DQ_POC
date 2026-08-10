from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dqtool.models.entities import Schedule, ScheduleCadence

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
BRUSSELS_TIMEZONE = ZoneInfo("Europe/Brussels")


def validate_execution_window(start: str | None, end: str | None) -> str | None:
    """Return an error for an invalid daily connection execution window."""
    try:
        start_minutes = _time_of_day_minutes(start)
        end_minutes = _time_of_day_minutes(end)
    except ValueError:
        return "Execution window times must use the HH:MM format."
    if start_minutes == end_minutes:
        return "Execution window start and end times must be different."
    return None


def is_within_execution_window(start: str, end: str, now: datetime | None = None) -> bool:
    """Whether Brussels local time is within [start, end), supporting overnight windows."""
    error = validate_execution_window(start, end)
    if error:
        raise ValueError(error)
    current = (now or datetime.now(BRUSSELS_TIMEZONE)).astimezone(BRUSSELS_TIMEZONE)
    current_minutes = current.hour * 60 + current.minute
    start_minutes = _time_of_day_minutes(start)
    end_minutes = _time_of_day_minutes(end)
    if start_minutes < end_minutes:
        return start_minutes <= current_minutes < end_minutes
    return current_minutes >= start_minutes or current_minutes < end_minutes


def describe_execution_window(start: str, end: str) -> str:
    return f"{start}–{end} Brussels time"


def compute_next_run(schedule: Schedule, after: datetime | None = None) -> datetime:
    """The next datetime a schedule should fire, strictly later than `after` (default: Brussels now).

    HOURLY fires every `interval_hours` hours from `after`. DAILY fires once a day at
    `time_of_day` (HH:MM, Europe/Brussels). WEEKLY fires once a week on `weekday`
    (0=Monday..6=Sunday) at `time_of_day`.
    """
    now = (after or datetime.now(BRUSSELS_TIMEZONE)).astimezone(BRUSSELS_TIMEZONE)
    if schedule.cadence == ScheduleCadence.HOURLY:
        interval = max(1, int(schedule.interval_hours or 1))
        return now + timedelta(hours=interval)

    hour, minute = _parse_time_of_day(schedule.time_of_day)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if schedule.cadence == ScheduleCadence.DAILY:
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if schedule.cadence == ScheduleCadence.WEEKLY:
        target_weekday = max(0, min(6, int(schedule.weekday or 0)))
        days_ahead = (target_weekday - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    raise ValueError(f"Unsupported cadence: {schedule.cadence}")


def describe_cadence(schedule: Schedule) -> str:
    """Short human-readable summary of a schedule's cadence, for list views."""
    if schedule.cadence == ScheduleCadence.HOURLY:
        interval = max(1, int(schedule.interval_hours or 1))
        return "Every hour" if interval == 1 else f"Every {interval} hours"
    hour, minute = _parse_time_of_day(schedule.time_of_day)
    time_text = f"{hour:02d}:{minute:02d} Brussels time"
    if schedule.cadence == ScheduleCadence.DAILY:
        return f"Daily at {time_text}"
    if schedule.cadence == ScheduleCadence.WEEKLY:
        weekday_name = WEEKDAY_NAMES[max(0, min(6, int(schedule.weekday or 0)))]
        return f"Weekly on {weekday_name} at {time_text}"
    return schedule.cadence.value


def _parse_time_of_day(value: str | None) -> tuple[int, int]:
    text = (value or "00:00").strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = max(0, min(23, int(hour_text)))
        minute = max(0, min(59, int(minute_text)))
        return hour, minute
    except (TypeError, ValueError):
        return 0, 0


def _time_of_day_minutes(value: str | None) -> int:
    text = (value or "").strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Invalid time") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Invalid time")
    return hour * 60 + minute
