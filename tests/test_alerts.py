"""Tests for drobo.alerts: reachability/severity transition tracking.

Pure logic (no Flask, no real network) — notify() is mocked and a real
History against a tmp_path db is used for the reachability-log side effects.
"""

from __future__ import annotations

from unittest.mock import Mock

from drobo import alerts as alerts_mod
from drobo.history import History


def _snap(stale: bool, last_error: str | None = None) -> dict:
    return {"stale": stale, "last_error": last_error}


def test_check_reachability_first_call_seeds_without_alerting(tmp_path, monkeypatch):
    mock_notify = Mock()
    monkeypatch.setattr(alerts_mod.notify_mod, "notify", mock_notify)
    h = History(tmp_path / "history.db")
    state = alerts_mod.AlertState()

    alerts_mod.check_reachability(state, _snap(stale=True), h)

    assert state.initialized is True
    assert state.stale is True
    mock_notify.assert_not_called()
    assert h.reachability_log() == []


def test_check_reachability_fires_once_per_transition_not_per_poll(tmp_path, monkeypatch):
    mock_notify = Mock()
    monkeypatch.setattr(alerts_mod.notify_mod, "notify", mock_notify)
    h = History(tmp_path / "history.db")
    state = alerts_mod.AlertState()

    # First poll: healthy, seeds state, no alert.
    alerts_mod.check_reachability(state, _snap(stale=False), h)
    # Stays healthy for a while: still no alert.
    alerts_mod.check_reachability(state, _snap(stale=False), h)
    alerts_mod.check_reachability(state, _snap(stale=False), h)
    assert mock_notify.call_count == 0

    # Goes stale: exactly one "down" alert, even across repeated stale polls.
    alerts_mod.check_reachability(state, _snap(stale=True, last_error="timeout"), h)
    alerts_mod.check_reachability(state, _snap(stale=True, last_error="timeout"), h)
    alerts_mod.check_reachability(state, _snap(stale=True, last_error="timeout"), h)
    assert mock_notify.call_count == 1
    assert "unreachable" in mock_notify.call_args[0][1]

    # Recovers: exactly one "recovered" alert.
    alerts_mod.check_reachability(state, _snap(stale=False), h)
    alerts_mod.check_reachability(state, _snap(stale=False), h)
    assert mock_notify.call_count == 2
    assert "reachable again" in mock_notify.call_args[0][1]

    # History got exactly one down + one recovered event.
    log = h.reachability_log()
    assert [e["kind"] for e in log] == ["recovered", "down"]


def test_check_capacity_severity_fires_once_on_transition_into_critical(monkeypatch):
    mock_notify = Mock()
    monkeypatch.setattr(alerts_mod.notify_mod, "notify", mock_notify)
    state = alerts_mod.AlertState()

    alerts_mod.check_capacity_severity(state, "ok", used_pct=10.0)
    alerts_mod.check_capacity_severity(state, "warning", used_pct=80.0)
    assert mock_notify.call_count == 0

    alerts_mod.check_capacity_severity(state, "critical", used_pct=96.0)
    alerts_mod.check_capacity_severity(state, "critical", used_pct=97.0)
    alerts_mod.check_capacity_severity(state, "critical", used_pct=98.0)
    assert mock_notify.call_count == 1
    assert "96.0" in mock_notify.call_args[0][1]

    # Drops back below critical then re-enters critical -> a second alert.
    alerts_mod.check_capacity_severity(state, "warning", used_pct=85.0)
    alerts_mod.check_capacity_severity(state, "critical", used_pct=99.0)
    assert mock_notify.call_count == 2


def test_notify_error_events_skips_reset_fires_for_increase_and_baseline(monkeypatch):
    mock_notify = Mock()
    monkeypatch.setattr(alerts_mod.notify_mod, "notify", mock_notify)

    events = [
        {"kind": "baseline", "slot": 0, "make": "WDC", "prev_count": 0, "new_count": 3},
        {"kind": "increase", "slot": 1, "make": "Seagate", "prev_count": 3, "new_count": 5},
        {"kind": "reset", "slot": 2, "make": "WDC", "prev_count": 5, "new_count": 0},
    ]
    alerts_mod.notify_error_events(events)

    assert mock_notify.call_count == 2
    messages = [c[0][1] for c in mock_notify.call_args_list]
    assert any("slot 0" in m for m in messages)
    assert any("slot 1" in m for m in messages)
    assert not any("slot 2" in m for m in messages)
