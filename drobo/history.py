"""Local SQLite history for the Drobo dashboard.

The Drobo only ever reports *current* values — it never timestamps anything and
has no I/O counters. To offer trends we persist our own observations:

* ``capacity_samples`` — one row per successful poll (used/free/total/unallocated),
  powering the capacity-over-time and data-written-per-day graphs.
* ``error_events`` — appended whenever we first see a drive's ``mErrorCount``
  rise (or a baseline non-zero count when tracking begins). This is how we give
  errors a "when" the device itself won't.
* ``throughput_samples`` — network throughput sampled over SSH (bytes/sec).
* ``slot_error_state`` — last-seen per-drive error count, so increases are
  detected across restarts.
* ``reachability_events`` — appended on every stale/reachable transition
  observed by the poller ("down"/"recovered"), so `/errors`-style timelines and
  an uptime percentage are possible for a device that never timestamps its own
  outages.

All access is guarded by a single lock over one shared connection (WAL mode);
the volumes here are tiny so serialising is simpler and safe across the poller,
throughput, and web-request threads.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capacity_samples (
    ts REAL NOT NULL,
    used INTEGER NOT NULL,
    free INTEGER NOT NULL,
    total INTEGER NOT NULL,
    unallocated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_capacity_ts ON capacity_samples (ts);

CREATE TABLE IF NOT EXISTS error_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    slot INTEGER NOT NULL,
    serial TEXT NOT NULL,
    make TEXT,
    prev_count INTEGER NOT NULL,
    new_count INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    kind TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_error_ts ON error_events (ts);

CREATE TABLE IF NOT EXISTS slot_error_state (
    serial TEXT PRIMARY KEY,
    slot INTEGER NOT NULL,
    last_count INTEGER NOT NULL,
    updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS throughput_samples (
    ts REAL NOT NULL,
    iface TEXT NOT NULL,
    rx_bytes INTEGER NOT NULL,
    tx_bytes INTEGER NOT NULL,
    rx_bps REAL NOT NULL,
    tx_bps REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_throughput_ts ON throughput_samples (ts);

CREATE TABLE IF NOT EXISTS hardware_samples (
    ts REAL NOT NULL,
    cpu_pct REAL,
    iowait_pct REAL,
    mem_used INTEGER,
    mem_total INTEGER,
    load1 REAL,
    disk_r_bps REAL,
    disk_w_bps REAL
);
CREATE INDEX IF NOT EXISTS idx_hardware_ts ON hardware_samples (ts);

CREATE TABLE IF NOT EXISTS reachability_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_reachability_ts ON reachability_events (ts);
"""

# Retention windows (seconds).
_CAPACITY_RETENTION = 180 * 86400
_THROUGHPUT_RETENTION = 14 * 86400
_HARDWARE_RETENTION = 14 * 86400
_PRUNE_EVERY = 3600.0

# daily_written() memo TTL: long enough to coalesce the near-simultaneous
# /api/storage + /api/history/capacity calls one dashboard tick fires, short
# enough that a fresh capacity sample (recorded on every ~15s poll) shows up
# within a poll cycle or two.
_DAILY_WRITTEN_CACHE_TTL = 5.0


class History:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._last_prune = 0.0
        # Short-TTL memo for daily_written(): /api/storage (via
        # days_until_full()) and /api/history/capacity both call this with
        # the same `days` on every dashboard tick, fired within the same
        # browser Promise.all — cache the (fairly expensive, GROUP-BY-day
        # over capacity_samples) result briefly rather than compute it twice
        # per tick. Same idea as app.py's _raw_cache TTL-coalescing pattern.
        self._daily_written_cache: dict[int, tuple[float, list[dict]]] = {}

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def record_capacity(
        self, used: int, free: int, total: int, unallocated: int = 0, ts: float | None = None
    ) -> None:
        ts = time.time() if ts is None else ts
        with self._lock:
            self._conn.execute(
                "INSERT INTO capacity_samples (ts, used, free, total, unallocated) "
                "VALUES (?,?,?,?,?)",
                (ts, int(used), int(free), int(total), int(unallocated)),
            )
            self._conn.commit()
            self._maybe_prune(ts)

    def record_throughput(
        self,
        iface: str,
        rx_bytes: int,
        tx_bytes: int,
        rx_bps: float,
        tx_bps: float,
        ts: float | None = None,
    ) -> None:
        ts = time.time() if ts is None else ts
        with self._lock:
            self._conn.execute(
                "INSERT INTO throughput_samples "
                "(ts, iface, rx_bytes, tx_bytes, rx_bps, tx_bps) VALUES (?,?,?,?,?,?)",
                (ts, iface, int(rx_bytes), int(tx_bytes), float(rx_bps), float(tx_bps)),
            )
            self._conn.commit()

    def record_hardware(self, sample: dict) -> None:
        ts = sample.get("ts") or time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO hardware_samples (ts, cpu_pct, iowait_pct, mem_used, mem_total,"
                " load1, disk_r_bps, disk_w_bps) VALUES (?,?,?,?,?,?,?,?)",
                (
                    float(ts),
                    sample.get("cpu_pct"),
                    sample.get("iowait_pct"),
                    sample.get("mem_used"),
                    sample.get("mem_total"),
                    sample.get("load1"),
                    sample.get("disk_r_bps"),
                    sample.get("disk_w_bps"),
                ),
            )
            self._conn.commit()

    def sync_errors(self, slots: list[dict], ts: float | None = None) -> list[dict]:
        """Compare each slot's error count to the stored state; log increases.

        ``slots`` is a list of DiskSlot dicts (needs slot, serial, error_count,
        vendor, model, present). Returns the list of newly-recorded events.
        """
        ts = time.time() if ts is None else ts
        new_events: list[dict] = []
        with self._lock:
            for s in slots:
                serial = (s.get("serial") or "").strip()
                if not serial or not s.get("present"):
                    continue
                slot = int(s.get("slot", -1))
                count = int(s.get("error_count", 0))
                make = " ".join(x for x in (s.get("vendor"), s.get("model")) if x).strip()
                row = self._conn.execute(
                    "SELECT slot, last_count FROM slot_error_state WHERE serial=?", (serial,)
                ).fetchone()

                if row is None:
                    # First time we've seen this drive. Record a baseline event
                    # if it already carries errors so the existing count has a
                    # visible starting point in the log.
                    if count > 0:
                        new_events.append(
                            self._insert_event(
                                ts,
                                slot,
                                serial,
                                make,
                                0,
                                count,
                                count,
                                "baseline",
                                f"{count} error(s) already present when tracking began",
                            )
                        )
                    self._conn.execute(
                        "INSERT INTO slot_error_state (serial, slot, last_count, updated) "
                        "VALUES (?,?,?,?)",
                        (serial, slot, count, ts),
                    )
                else:
                    last = int(row["last_count"])
                    if count > last:
                        new_events.append(
                            self._insert_event(
                                ts,
                                slot,
                                serial,
                                make,
                                last,
                                count,
                                count - last,
                                "increase",
                                None,
                            )
                        )
                    elif count < last:
                        # Counter dropped — usually a drive swap or firmware reset.
                        new_events.append(
                            self._insert_event(
                                ts,
                                slot,
                                serial,
                                make,
                                last,
                                count,
                                count - last,
                                "reset",
                                "error count decreased (drive replaced or counter reset)",
                            )
                        )
                    if count != last or slot != int(row["slot"]):
                        self._conn.execute(
                            "UPDATE slot_error_state SET slot=?, last_count=?, updated=? "
                            "WHERE serial=?",
                            (slot, count, ts, serial),
                        )
            self._conn.commit()
        return new_events

    def _insert_event(self, ts, slot, serial, make, prev, new, delta, kind, note) -> dict:
        cur = self._conn.execute(
            "INSERT INTO error_events "
            "(ts, slot, serial, make, prev_count, new_count, delta, kind, note)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, slot, serial, make, prev, new, delta, kind, note),
        )
        return {
            "id": cur.lastrowid,
            "ts": ts,
            "slot": slot,
            "serial": serial,
            "make": make,
            "prev_count": prev,
            "new_count": new,
            "delta": delta,
            "kind": kind,
            "note": note,
        }

    def record_reachability(
        self, kind: str, detail: str | None = None, ts: float | None = None
    ) -> dict:
        """Log a reachability transition ("down" or "recovered").

        Returns the recorded row as a dict (same pattern as ``_insert_event``
        for ``error_events``).
        """
        ts = time.time() if ts is None else ts
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reachability_events (ts, kind, detail) VALUES (?,?,?)",
                (ts, kind, detail),
            )
            self._conn.commit()
            row_id = cur.lastrowid
        return {"id": row_id, "ts": ts, "kind": kind, "detail": detail}

    def _maybe_prune(self, now: float) -> None:
        if now - self._last_prune < _PRUNE_EVERY:
            return
        self._last_prune = now
        self._conn.execute(
            "DELETE FROM capacity_samples WHERE ts < ?", (now - _CAPACITY_RETENTION,)
        )
        self._conn.execute(
            "DELETE FROM throughput_samples WHERE ts < ?", (now - _THROUGHPUT_RETENTION,)
        )
        self._conn.execute(
            "DELETE FROM hardware_samples WHERE ts < ?", (now - _HARDWARE_RETENTION,)
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def capacity_series(self, since_ts: float, max_points: int = 600) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, used, free, total, unallocated FROM capacity_samples"
                " WHERE ts >= ? ORDER BY ts",
                (since_ts,),
            ).fetchall()
        return _downsample([dict(r) for r in rows], max_points)

    def throughput_series(self, since_ts: float, max_points: int = 600) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, rx_bps, tx_bps FROM throughput_samples WHERE ts >= ? ORDER BY ts",
                (since_ts,),
            ).fetchall()
        return _downsample([dict(r) for r in rows], max_points)

    def hardware_series(self, since_ts: float, max_points: int = 600) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, cpu_pct, iowait_pct, mem_used, mem_total, load1, disk_r_bps, disk_w_bps"
                " FROM hardware_samples WHERE ts >= ? ORDER BY ts",
                (since_ts,),
            ).fetchall()
        return _downsample([dict(r) for r in rows], max_points)

    def daily_written(self, days: int = 14) -> list[dict]:
        """Net change in used bytes per calendar day (local time).

        Positive = data net-written that day, negative = net-freed. Computed as
        the difference between each day's last sample and the previous day's.

        Memoized for a few seconds per distinct ``days`` value — see the
        ``_daily_written_cache`` note in ``__init__``.
        """
        now = time.time()
        cached = self._daily_written_cache.get(days)
        if cached is not None and (now - cached[0]) < _DAILY_WRITTEN_CACHE_TTL:
            return cached[1]

        since = now - (days + 1) * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS day,"
                "       used, MAX(ts) AS mts"
                " FROM capacity_samples WHERE ts >= ?"
                " GROUP BY day ORDER BY day",
                (since,),
            ).fetchall()
        out: list[dict] = []
        prev_used: int | None = None
        for r in rows:
            used = int(r["used"])
            if prev_used is not None:
                out.append({"date": r["day"], "delta_bytes": used - prev_used, "used_end": used})
            prev_used = used
        result = out[-days:]
        self._daily_written_cache[days] = (now, result)
        return result

    def error_log(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, slot, serial, make, prev_count, new_count, delta, kind, note"
                " FROM error_events ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def error_totals(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS events,"
                " COALESCE(SUM(CASE WHEN kind='increase' THEN delta ELSE 0 END),0) AS added"
                " FROM error_events"
            ).fetchone()
            first = self._conn.execute("SELECT MIN(ts) AS since FROM capacity_samples").fetchone()
        return {
            "event_count": int(row["events"]),
            "errors_added_since_tracking": int(row["added"]),
            "tracking_since": first["since"] if first else None,
        }

    def reachability_log(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, kind, detail FROM reachability_events"
                " ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def uptime_pct(self, since_ts: float) -> float | None:
        """Percent of ``[since_ts, now]`` the Drobo was considered reachable.

        Walks ``reachability_events`` with ``ts >= since_ts`` in order, pairing
        each "down" with the next "recovered" (or "now" if still down) to total
        up the down-seconds in the window; everything else is implicitly "up".
        An outage already in progress when the window opens (its "down" event
        is before ``since_ts``, with no "recovered" before ``since_ts`` either)
        must still count as down from ``since_ts`` onward — otherwise a window
        that starts mid-outage looks like 100% uptime just because the "down"
        event itself falls outside it. We check the most recent event strictly
        before ``since_ts`` to seed the correct starting state.

        Returns ``None`` rather than fabricating a number if tracking hadn't
        even started by ``since_ts`` (mirrors ``daily_written()``/
        ``error_totals()``: don't fake data for a window we have no visibility
        into). We use the earliest ``capacity_samples`` row as the "tracking
        began" marker, since a capacity sample is written on every successful
        poll from process start.
        """
        now = time.time()
        window = now - since_ts
        if window <= 0:
            return None
        with self._lock:
            first = self._conn.execute(
                "SELECT MIN(ts) AS first_ts FROM capacity_samples"
            ).fetchone()
            first_ts = first["first_ts"] if first else None
            if first_ts is None or first_ts > since_ts:
                return None
            prior = self._conn.execute(
                "SELECT kind FROM reachability_events WHERE ts < ?"
                " ORDER BY ts DESC, id DESC LIMIT 1",
                (since_ts,),
            ).fetchone()
            rows = self._conn.execute(
                "SELECT ts, kind FROM reachability_events WHERE ts >= ? ORDER BY ts, id",
                (since_ts,),
            ).fetchall()

        down_seconds = 0.0
        down_since: float | None = since_ts if prior and prior["kind"] == "down" else None
        for r in rows:
            if r["kind"] == "down" and down_since is None:
                down_since = r["ts"]
            elif r["kind"] == "recovered" and down_since is not None:
                down_seconds += r["ts"] - down_since
                down_since = None
        if down_since is not None:
            down_seconds += now - down_since

        down_seconds = min(down_seconds, window)
        return max(0.0, min(100.0, (window - down_seconds) / window * 100))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _downsample(rows: list[dict], max_points: int) -> list[dict]:
    """Keep at most ``max_points`` rows with an even stride, always the last."""
    n = len(rows)
    if n <= max_points or max_points <= 0:
        return rows
    step = n / max_points
    picked = [rows[int(i * step)] for i in range(max_points)]
    if picked[-1] is not rows[-1]:
        picked[-1] = rows[-1]
    return picked
