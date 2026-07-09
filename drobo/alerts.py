"""In-memory alert-transition tracking for the dashboard's ntfy notifications.

Pure logic with no Flask/network coupling (beyond ``notify()``, which already
no-ops/swallows on its own), so it's unit-testable in isolation: feed it a
sequence of snapshot dicts and assert a transition fires exactly once, not
once per poll while the state persists.
"""

from __future__ import annotations

from . import notify as notify_mod


class AlertState:
    """Tracks the last-seen reachability/capacity state for one process.

    Deliberately just in-memory/per-process (like the poller's own cache) —
    a container restart re-seeds it from the first snapshot without alerting;
    see ``check_reachability``.
    """

    def __init__(self) -> None:
        self.initialized = False
        self.stale = False
        self.severity = "ok"


def check_reachability(state: AlertState, snap: dict, history) -> None:
    """Detect a stale/reachable transition in a poller snapshot.

    Called after every poll (success or failure). On a transition, persists it
    to ``history`` and fires an ntfy alert — exactly once per transition, not
    once per poll while the state persists. The very first call only seeds
    ``state`` without alerting, so a process restart doesn't fire a spurious
    alert just because the tracked state was previously unknown.
    """
    stale = bool(snap.get("stale"))
    if not state.initialized:
        state.initialized = True
        state.stale = stale
        return

    if stale and not state.stale:
        history.record_reachability("down", snap.get("last_error"))
        notify_mod.notify(
            "Drobo Dashboard",
            f"Drobo unreachable ({snap.get('last_error') or 'no response'})",
            tags="warning",
        )
    elif not stale and state.stale:
        history.record_reachability("recovered")
        notify_mod.notify("Drobo Dashboard", "Drobo reachable again", tags="white_check_mark")
    state.stale = stale


def check_capacity_severity(state: AlertState, severity: str, used_pct: float) -> None:
    """Fire an ntfy alert only on the transition *into* "critical" severity.

    ``state.severity`` is updated on every call regardless, so the alert fires
    once per transition into critical, not repeatedly while it stays critical.
    """
    if severity == "critical" and state.severity != "critical":
        notify_mod.notify(
            "Drobo Dashboard", f"Capacity critical: {used_pct}% used", tags="rotating_light"
        )
    state.severity = severity


def notify_error_events(events: list[dict]) -> None:
    """Fire one ntfy alert per new/baseline drive-error event.

    ``events`` is the return value of ``History.sync_errors()``. "reset" kind
    events (drive replacement / counter reset) are skipped — benign.
    """
    for ev in events:
        if ev["kind"] in ("increase", "baseline"):
            notify_mod.notify(
                "Drobo Dashboard",
                f"Drive error count increased: slot {ev['slot']} {ev['make']} "
                f"{ev['prev_count']} -> {ev['new_count']}",
                tags="warning",
            )
