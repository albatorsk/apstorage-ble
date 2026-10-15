"""Tests for the getsysmode schedule parser and expansion."""

from datetime import datetime, time, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

MODULE_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "apstorage_ble" / "schedule.py"
SPEC = importlib.util.spec_from_file_location("apstorage_schedule", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
SCHEDULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCHEDULE
SPEC.loader.exec_module(SCHEDULE)

CHARGE, DISCHARGE = SCHEDULE.CHARGE, SCHEDULE.DISCHARGE
UTC = timezone.utc

# Real getsysmode reply from an ELT-12 in AI mode (spec §8.2).
AI_SYSMODE = {
    "mode": "6", "backupSOC": "10", "socMin": "10", "peakTime": [], "valleyTime": [],
    "schedule": [{"peakTime": [], "valleyTime": ["024500121500"], "W": [5]}],
    "sellingFirst": "0", "peakPower": "1500", "area": "SE4",
}


def _week(windows, start=datetime(2026, 10, 12, tzinfo=UTC)):  # a Monday
    return SCHEDULE.expand(windows, start, start + timedelta(days=7), UTC)


def test_weekly_plan_from_real_reply() -> None:
    windows = SCHEDULE.active_windows(AI_SYSMODE)
    assert len(windows) == 1
    window = windows[0]
    assert window.kind == CHARGE and window.weekdays == {4}  # W 5 = Friday
    assert (window.start, window.end) == (time(2, 45), time(12, 15))
    events = _week(windows)
    assert [(e.kind, e.start, e.end) for e in events] == [
        (CHARGE, datetime(2026, 10, 16, 2, 45, tzinfo=UTC), datetime(2026, 10, 16, 12, 15, tzinfo=UTC))
    ]


def test_daily_peak_valley_lists() -> None:
    sysmode = {"mode": "0", "peakTime": ["170000210000"], "valleyTime": ["010000050000"], "schedule": []}
    windows = SCHEDULE.active_windows(sysmode)
    assert {w.kind for w in windows} == {CHARGE, DISCHARGE}
    assert all(w.weekdays == SCHEDULE.ALL_DAYS for w in windows)
    events = _week(windows)
    assert len(events) == 14
    assert events[0].kind == CHARGE and events[0].start.hour == 1
    assert events[1].kind == DISCHARGE and events[1].start.hour == 17


def test_window_past_midnight() -> None:
    sysmode = {"mode": "3", "valleyTime": ["220000060000"], "peakTime": []}
    start = datetime(2026, 10, 13, 3, 0, tzinfo=UTC)
    events = SCHEDULE.expand(SCHEDULE.active_windows(sysmode), start, start + timedelta(hours=1), UTC)
    # The window that began at 22:00 the evening before is still running.
    assert [(e.start, e.end) for e in events] == [
        (datetime(2026, 10, 12, 22, 0, tzinfo=UTC), datetime(2026, 10, 13, 6, 0, tzinfo=UTC))
    ]


def test_end_of_day_as_240000() -> None:
    windows = SCHEDULE.schedule_windows({"valleyTime": ["180000240000"]})
    event = _week(windows)[0]
    assert event.end - event.start == timedelta(hours=6)


def test_no_events_in_modes_without_schedule() -> None:
    for mode in ("1", "2", "4", "5"):
        assert SCHEDULE.active_windows({**AI_SYSMODE, "mode": mode}) == []
    assert SCHEDULE.active_windows(None) == []
    assert SCHEDULE.active_windows({"mode": "6", "schedule": [], "peakTime": [], "valleyTime": []}) == []


def test_ignores_malformed_entries() -> None:
    sysmode = {"schedule": [
        {"W": [], "valleyTime": ["010000020000"]},  # no days
        {"W": [9, "x"], "valleyTime": ["010000020000"]},  # invalid days
        {"W": [1], "valleyTime": ["0100", "abc", "990000020000", "050000050000"], "peakTime": None},
        "not a plan",
    ]}
    windows = SCHEDULE.schedule_windows(sysmode)
    # Only "0100" survives: zero-padded like the app does, 00:00:00-00:01:00.
    assert [(w.start, w.end) for w in windows] == [(time(0, 0), time(0, 1))]


def test_several_plans_and_windows() -> None:
    sysmode = {"mode": "3", "schedule": [
        {"W": [1, 2, 3, 4, 5], "valleyTime": ["010000050000", "120000140000"], "peakTime": ["170000200000"]},
        {"W": [6, 7], "valleyTime": ["020000060000"], "peakTime": []},
    ]}
    events = _week(SCHEDULE.active_windows(sysmode))
    assert len(events) == 5 * 3 + 2 * 1
    assert {e.start.weekday() for e in events if e.start.hour == 2} == {5, 6}


def test_weekly_plans_as_written_by_set_advanced_schedule() -> None:
    # setsysmode payload produced by set_advanced_schedule with
    # [{weekdays: [mon..fri], charge: ["01:00-05:00"], discharge: ["17:00-21:00"]},
    #  {weekdays: [sat, sun], charge: ["22:00-06:00"]}].
    sysmode = {
        "mode": "3", "peakTime": [], "valleyTime": [],
        "schedule": [
            {"W": [1, 2, 3, 4, 5], "peakTime": ["170000210000"], "valleyTime": ["010000050000"]},
            {"W": [6, 7], "peakTime": [], "valleyTime": ["220000060000"]},
        ],
    }
    windows = SCHEDULE.active_windows(sysmode)
    assert sorted(SCHEDULE.format_window(w) for w in windows) == [
        "Charge 01:00-05:00", "Charge 22:00-06:00", "Discharge 17:00-21:00",
    ]
    events = _week(windows)
    # Weekdays, plus last Sunday's overnight charge running into Monday.
    assert len(events) == 5 + 5 + 2 + 1
    assert events[0].start == datetime(2026, 10, 11, 22, tzinfo=UTC)
    weekend = [e for e in events[1:] if e.start.weekday() >= 5]
    assert [(e.kind, e.start, e.end) for e in weekend] == [
        (CHARGE, datetime(2026, 10, 17, 22, tzinfo=UTC), datetime(2026, 10, 18, 6, tzinfo=UTC)),
        (CHARGE, datetime(2026, 10, 18, 22, tzinfo=UTC), datetime(2026, 10, 19, 6, tzinfo=UTC)),
    ]
