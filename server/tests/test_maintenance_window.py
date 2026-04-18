"""Tests for the mini-cron window evaluator."""

from datetime import datetime, timezone

from app.data_sampler.maintenance import is_within_windows


def _at(hour: int, minute: int = 0, dow: int = 1) -> datetime:
    # 2026-01-05 is a Monday (weekday=0, cron-dow=1).
    base = datetime(2026, 1, 5, hour, minute, tzinfo=timezone.utc)
    offset_days = (dow - 1) % 7
    return base.replace(day=5 + offset_days)


def test_empty_windows_always_allowed():
    assert is_within_windows([], _at(3))
    assert is_within_windows(None, _at(3))


def test_exact_hour_range():
    # Every minute, 02:00-04:59 on any day
    windows = ["* 2-4 * * *"]
    assert is_within_windows(windows, _at(2, 30))
    assert is_within_windows(windows, _at(4, 59))
    assert not is_within_windows(windows, _at(5, 0))


def test_day_of_week_mon_to_fri():
    # Any hour, Mon-Fri only (cron dow: 1-5)
    windows = ["* * * * 1-5"]
    assert is_within_windows(windows, _at(12, 0, dow=1))
    assert is_within_windows(windows, _at(12, 0, dow=5))
    assert not is_within_windows(windows, _at(12, 0, dow=6))  # Sat
    assert not is_within_windows(windows, _at(12, 0, dow=0))  # Sun


def test_step_minutes():
    # Every 15 minutes at hour 1
    windows = ["*/15 1 * * *"]
    assert is_within_windows(windows, _at(1, 0))
    assert is_within_windows(windows, _at(1, 15))
    assert is_within_windows(windows, _at(1, 30))
    assert not is_within_windows(windows, _at(1, 7))


def test_multiple_windows_any_match():
    windows = ["* 2 * * *", "* 14 * * *"]
    assert is_within_windows(windows, _at(2, 0))
    assert is_within_windows(windows, _at(14, 30))
    assert not is_within_windows(windows, _at(8, 0))


def test_bad_expression_ignored():
    # Bad expression should not crash or match spuriously.
    assert not is_within_windows(["not a cron expr"], _at(10))
    # A valid entry alongside a bad one still governs.
    assert is_within_windows(["bogus", "* * * * *"], _at(10))
