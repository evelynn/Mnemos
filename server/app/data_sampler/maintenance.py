"""Maintenance-window evaluator for per-project DBs (spec §12.2).

Windows are stored as a list of cron expressions. When non-empty, the
scheduler only dispatches DMV-heavy verbs (``live_schema``, ``data_access``)
during those windows. An empty list means ``always allowed`` so that
greenfield projects don't need to fill in a schedule before running.

We keep a small, intentionally limited cron parser (5 fields: minute,
hour, day-of-month, month, day-of-week) rather than pulling in a full
cron library — Phase-A windows are coarse and operator-set.
"""

from __future__ import annotations

from datetime import datetime, timezone


def is_within_windows(windows: list[str] | None, now: datetime | None = None) -> bool:
    """Return True when ``now`` matches at least one window, or when the
    list is empty/None (no schedule == always allowed).

    Unparseable entries are ignored (with the door defaulting closed for
    that specific entry — other valid entries still govern).
    """
    if not windows:
        return True
    now = now or datetime.now(tz=timezone.utc)
    for expr in windows:
        if _matches(expr, now):
            return True
    return False


def _matches(cron_expr: str, when: datetime) -> bool:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields

    return (
        _field_matches(minute, when.minute, 0, 59)
        and _field_matches(hour, when.hour, 0, 23)
        and _field_matches(dom, when.day, 1, 31)
        and _field_matches(month, when.month, 1, 12)
        # datetime.weekday(): Monday=0..Sunday=6; cron: Sunday=0..Saturday=6.
        and _field_matches(dow, (when.weekday() + 1) % 7, 0, 6)
    )


def _field_matches(token: str, value: int, lo: int, hi: int) -> bool:
    if token == "*":
        return True
    # Support comma lists ("0,15,30,45"), ranges ("9-17"), and simple
    # step syntax ("*/5"). No L/W/# / named months.
    for part in token.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            if not step_str.isdigit():
                continue
            step = int(step_str)
            part = base or "*"
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            try:
                start_s, end_s = part.split("-", 1)
                start, end = int(start_s), int(end_s)
            except ValueError:
                continue
        else:
            if not part.isdigit():
                continue
            start = end = int(part)
        if start > end:
            continue
        if not (lo <= start <= hi and lo <= end <= hi):
            continue
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False
