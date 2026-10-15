"""Charge/discharge schedule from getsysmode (no Home Assistant imports).

getsysmode (§8.2 of the spec) holds the schedule in one of two forms:
  * `peakTime` / `valleyTime`: lists of "HHmmssHHmmss" windows, every day;
  * `schedule`: weekly plans [{"W": [1..7], "peakTime": [...], "valleyTime": [...]}],
    1 = Monday ... 7 = Sunday. When present, the flat lists are empty.
Valley windows charge the battery, peak windows discharge it. A window whose end
is before its start runs past midnight.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

CHARGE = "Charge"
DISCHARGE = "Discharge"

ALL_DAYS = frozenset(range(7))  # Python weekdays, 0 = Monday

# Modes in which the PCS follows the schedule: 0 peak-valley, 3 advanced
# (mixed), 6 AI (the schedule holds the AI's plan).
SCHEDULED_MODES = frozenset({"0", "3", "6"})


@dataclass(frozen=True)
class Window:
    """A recurring charge or discharge window."""

    kind: str               # CHARGE or DISCHARGE
    weekdays: frozenset[int]  # Python weekdays the window starts on
    start: time
    end: time

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start


@dataclass(frozen=True)
class Event:
    """One occurrence of a window."""

    kind: str
    start: datetime
    end: datetime


def _parse_range(value: Any) -> tuple[time, time] | None:
    """'024500121500' -> (02:45, 12:15); None for malformed or empty windows."""
    text = str(value).strip()
    if not text.isdigit() or len(text) > 12:
        return None
    text = text.zfill(12)
    try:
        start = time(int(text[0:2]), int(text[2:4]), int(text[4:6]))
        end = time(int(text[6:8]) % 24, int(text[8:10]), int(text[10:12]))
    except ValueError:
        return None
    if start == end:
        return None
    return start, end


def _weekdays(value: Any) -> frozenset[int]:
    days = set()
    for item in value or []:
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= day <= 7:
            days.add(day - 1)
    return frozenset(days)


def _windows(kind: str, ranges: Any, weekdays: frozenset[int]) -> list[Window]:
    out = []
    for value in ranges or []:
        parsed = _parse_range(value)
        if parsed:
            out.append(Window(kind, weekdays, *parsed))
    return out


def schedule_windows(sysmode: dict[str, Any] | None) -> list[Window]:
    """All windows in a getsysmode payload, whatever the current mode."""
    if not sysmode:
        return []
    windows: list[Window] = []
    plans = sysmode.get("schedule")
    if isinstance(plans, list) and plans:
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            days = _weekdays(plan.get("W"))
            if not days:
                continue
            windows += _windows(CHARGE, plan.get("valleyTime"), days)
            windows += _windows(DISCHARGE, plan.get("peakTime"), days)
    else:
        windows += _windows(CHARGE, sysmode.get("valleyTime"), ALL_DAYS)
        windows += _windows(DISCHARGE, sysmode.get("peakTime"), ALL_DAYS)
    return windows


def active_windows(sysmode: dict[str, Any] | None) -> list[Window]:
    """Windows the PCS follows now: none unless the mode uses the schedule."""
    if not sysmode or str(sysmode.get("mode", "")).strip() not in SCHEDULED_MODES:
        return []
    return schedule_windows(sysmode)


def expand(
    windows: list[Window], start: datetime, end: datetime, tz: tzinfo
) -> list[Event]:
    """Occurrences overlapping [start, end), sorted by start time."""
    if not windows or end <= start:
        return []
    events: list[Event] = []
    # Start a day early to catch windows that run past midnight into the range.
    day: date = start.astimezone(tz).date() - timedelta(days=1)
    last: date = end.astimezone(tz).date()
    while day <= last:
        for window in windows:
            if day.weekday() not in window.weekdays:
                continue
            begin = datetime.combine(day, window.start, tz)
            finish_day = day + timedelta(days=1) if window.crosses_midnight else day
            finish = datetime.combine(finish_day, window.end, tz)
            if begin < end and finish > start:
                events.append(Event(window.kind, begin, finish))
        day += timedelta(days=1)
    events.sort(key=lambda event: (event.start, event.kind))
    return events


def format_window(window: Window) -> str:
    """'Charge 02:45-12:15' for logs and descriptions."""
    return f"{window.kind} {window.start:%H:%M}-{window.end:%H:%M}"
