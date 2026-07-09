"""Tests for drobo.history.History: SQLite persistence + error-sync logic."""

from __future__ import annotations

import time

from drobo.history import History


def _slot(serial: str, slot: int, error_count: int, present: bool = True) -> dict:
    return {
        "serial": serial,
        "slot": slot,
        "error_count": error_count,
        "vendor": "WDC",
        "model": "WD40EFRX",
        "present": present,
    }


def test_record_capacity_and_series(tmp_path):
    h = History(tmp_path / "history.db")
    h.record_capacity(used=100, free=900, total=1000, unallocated=0, ts=1000.0)
    h.record_capacity(used=200, free=800, total=1000, unallocated=0, ts=2000.0)

    series = h.capacity_series(since_ts=0.0)
    assert len(series) == 2
    assert series[0]["used"] == 100
    assert series[1]["used"] == 200


def test_daily_written(tmp_path, monkeypatch):
    h = History(tmp_path / "history.db")
    day0 = 0.0
    day1 = 86400.0
    day2 = 2 * 86400.0
    h.record_capacity(used=100, free=900, total=1000, ts=day0)
    h.record_capacity(used=150, free=850, total=1000, ts=day1)
    h.record_capacity(used=140, free=860, total=1000, ts=day2)

    # daily_written windows on time.time(), so give it a generous horizon.
    monkeypatch.setattr(time, "time", lambda: day2 + 86400.0)
    rows = h.daily_written(days=30)
    deltas = {r["date"]: r["delta_bytes"] for r in rows}
    assert any(d == 50 for d in deltas.values())
    assert any(d == -10 for d in deltas.values())


def test_sync_errors_baseline_then_increase_then_reset(tmp_path):
    h = History(tmp_path / "history.db")

    # First sync: baseline (non-zero count) records a baseline event.
    events = h.sync_errors([_slot("SN1", slot=0, error_count=3)], ts=1.0)
    assert len(events) == 1
    assert events[0]["kind"] == "baseline"
    assert events[0]["new_count"] == 3

    # Second sync: no change -> no events, no needless write is implied (we
    # can't directly observe "no write" here, but slot/last_count shouldn't move).
    events = h.sync_errors([_slot("SN1", slot=0, error_count=3)], ts=2.0)
    assert events == []

    # Third sync: count increases -> "increase" event.
    events = h.sync_errors([_slot("SN1", slot=0, error_count=5)], ts=3.0)
    assert len(events) == 1
    assert events[0]["kind"] == "increase"
    assert events[0]["prev_count"] == 3
    assert events[0]["new_count"] == 5
    assert events[0]["delta"] == 2

    # Fourth sync: count decreases -> "reset" event (drive replaced/counter reset).
    events = h.sync_errors([_slot("SN1", slot=0, error_count=0)], ts=4.0)
    assert len(events) == 1
    assert events[0]["kind"] == "reset"
    assert events[0]["prev_count"] == 5
    assert events[0]["new_count"] == 0


def test_sync_errors_first_seen_zero_count_no_baseline_event(tmp_path):
    h = History(tmp_path / "history.db")
    events = h.sync_errors([_slot("SN2", slot=1, error_count=0)], ts=1.0)
    assert events == []


def test_sync_errors_ignores_absent_or_blank_serial(tmp_path):
    h = History(tmp_path / "history.db")
    events = h.sync_errors(
        [
            _slot("", slot=0, error_count=5),
            _slot("SN3", slot=1, error_count=5, present=False),
        ],
        ts=1.0,
    )
    assert events == []


def test_record_reachability_and_log_round_trip(tmp_path):
    h = History(tmp_path / "history.db")
    down = h.record_reachability("down", detail="boom", ts=100.0)
    recovered = h.record_reachability("recovered", ts=200.0)

    assert down["kind"] == "down"
    assert down["detail"] == "boom"
    assert down["ts"] == 100.0
    assert recovered["kind"] == "recovered"
    assert recovered["detail"] is None

    log = h.reachability_log()
    # Most recent first.
    assert [e["kind"] for e in log] == ["recovered", "down"]
    assert log[0]["ts"] == 200.0


def test_reachability_log_respects_limit(tmp_path):
    h = History(tmp_path / "history.db")
    for i in range(5):
        h.record_reachability("down" if i % 2 == 0 else "recovered", ts=float(i))
    log = h.reachability_log(limit=2)
    assert len(log) == 2
    assert log[0]["ts"] == 4.0
    assert log[1]["ts"] == 3.0


def test_uptime_pct_none_when_tracking_does_not_cover_window(tmp_path):
    h = History(tmp_path / "history.db")
    # No capacity samples at all -> no idea if the window is covered.
    assert h.uptime_pct(since_ts=time.time() - 3600) is None

    # Tracking only started recently; asking about an earlier window -> None.
    h.record_capacity(used=1, free=1, total=2, ts=time.time() - 10)
    assert h.uptime_pct(since_ts=time.time() - 3600) is None


def test_uptime_pct_one_outage_in_one_hour_window(tmp_path, monkeypatch):
    h = History(tmp_path / "history.db")
    now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)

    since = now - 3600.0
    # Tracking began well before the window.
    h.record_capacity(used=1, free=1, total=2, ts=since - 100.0)

    # A single 10-minute outage inside the window.
    down_at = since + 1000.0
    h.record_reachability("down", ts=down_at)
    h.record_reachability("recovered", ts=down_at + 600.0)

    pct = h.uptime_pct(since_ts=since)
    assert pct is not None
    assert round(pct, 1) == 83.3


def test_uptime_pct_still_down_counts_until_now(tmp_path, monkeypatch):
    h = History(tmp_path / "history.db")
    now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)

    since = now - 3600.0
    h.record_capacity(used=1, free=1, total=2, ts=since - 100.0)

    # Down for the last 30 minutes and never recovered.
    h.record_reachability("down", ts=now - 1800.0)

    pct = h.uptime_pct(since_ts=since)
    assert pct is not None
    assert round(pct, 1) == 50.0


def test_uptime_pct_ongoing_outage_that_started_before_the_window(tmp_path, monkeypatch):
    h = History(tmp_path / "history.db")
    now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)

    since = now - 3600.0
    # Tracking began well before both the outage and the window.
    h.record_capacity(used=1, free=1, total=2, ts=since - 1000.0)

    # Outage started BEFORE the window opened and never recovered — the
    # "down" event itself is outside [since, now], but the device has been
    # unreachable for the device's entire visible window.
    h.record_reachability("down", ts=since - 500.0)

    pct = h.uptime_pct(since_ts=since)
    assert pct == 0.0  # must NOT report 100% just because no event fell inside the window


def test_uptime_pct_outage_started_before_window_recovers_inside_it(tmp_path, monkeypatch):
    h = History(tmp_path / "history.db")
    now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)

    since = now - 3600.0
    h.record_capacity(used=1, free=1, total=2, ts=since - 1000.0)

    # Outage started before the window, recovers 600s after the window opens.
    h.record_reachability("down", ts=since - 500.0)
    h.record_reachability("recovered", ts=since + 600.0)

    pct = h.uptime_pct(since_ts=since)
    # 600 of the 3600-second window were still down (from since to recovery).
    assert round(pct, 1) == round((3600.0 - 600.0) / 3600.0 * 100, 1)


def test_sync_errors_tracks_drive_moved_to_different_bay(tmp_path):
    """Regression test for the slot-comparison bug in History.sync_errors.

    Previously the code compared the *current* slot against the literal -1
    (always false for a present drive), so it never actually detected a drive
    moving bays, and the row was rewritten unconditionally as a side effect.
    The fix compares against the *previously stored* slot.
    """
    h = History(tmp_path / "history.db")

    # Drive first seen in bay 0 with a baseline error count.
    h.sync_errors([_slot("SN-MOVED", slot=0, error_count=2)], ts=1.0)
    row = h._conn.execute(
        "SELECT slot, last_count FROM slot_error_state WHERE serial=?", ("SN-MOVED",)
    ).fetchone()
    assert row["slot"] == 0
    assert row["last_count"] == 2

    # Same error count, but now reported in bay 2 (physically moved). No error
    # event should fire (the count itself didn't change), but the stored slot
    # must be updated to reflect the drive's new bay.
    events = h.sync_errors([_slot("SN-MOVED", slot=2, error_count=2)], ts=2.0)
    assert events == []

    row = h._conn.execute(
        "SELECT slot, last_count FROM slot_error_state WHERE serial=?", ("SN-MOVED",)
    ).fetchone()
    assert row["slot"] == 2
    assert row["last_count"] == 2
