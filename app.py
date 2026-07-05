"""Flask web app for the Drobo 5N dashboard.

Serves a single dashboard page plus a JSON API backed by a background
:class:`~drobo.poller.Poller`. Configuration is via environment variables:

    DROBO_HOST      Drobo IP/hostname          (required in .env)
    DROBO_PORT      NASD status port           (default 5000)
    POLL_INTERVAL   seconds between polls       (default 15)
    WEB_HOST        address to bind the server  (default 127.0.0.1)
    WEB_PORT        port to bind the server     (default 8765)
"""

from __future__ import annotations

import math
import os
import secrets
import threading
import time
from pathlib import Path

# Load the .env sitting next to this file BEFORE any os.environ.get(...) config
# reads below, so DROBO_HOST/DROBO_PORT/POLL_INTERVAL/WEB_PORT/etc. (and the
# DROBO_* credentials) take effect regardless of the current working directory.
# override=False keeps real environment variables winning over the .env file.
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

from flask import Flask, jsonify, render_template, request

# Additive imports for the /stats details page + /api/raw endpoint.
# Additive imports for the /settings control page (port-5001 command channel).
from drobo import DEFAULT_PORT, codes, control

# Trends: local SQLite history, capacity breakdown, reference tables, and the
# SSH-based network throughput monitor.
from drobo import reference as reference_mod
from drobo import storage as storage_mod
from drobo.client import DroboUnreachable, read_raw
from drobo.hardware import HardwareMonitor
from drobo.history import History
from drobo.models import human_bytes
from drobo.poller import Poller
from drobo.rawdump import RawDumpError, raw_dump
from drobo.throughput import ThroughputMonitor


def _format_uptime_human(sec: float | None) -> str | None:
    """Compact uptime string for the Homepage widget (matches static/app.js)."""
    if sec is None:
        return None
    total = int(sec)
    days = total // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_bps_human(bps: float | None) -> str | None:
    """Human bytes/sec for the Homepage widget (matches static/charts.js fmtBps)."""
    if bps is None:
        return None
    return f"{human_bytes(int(bps))}/s"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _req_hours(default: float, max_hours: float = 24 * 180) -> float:
    """A finite, positive ``hours`` query param, clamped to a sane window.

    Guards the history endpoints against ``nan``/``inf`` (which ``float()``
    happily parses and which then serialise as spec-invalid JSON tokens) and
    against non-positive windows.
    """
    val = request.args.get("hours", default=default, type=float)
    if val is None or not math.isfinite(val) or val <= 0:
        val = default
    return min(val, max_hours)


def _req_int(name: str, default: int, lo: int, hi: int) -> int:
    """A bounded positive integer query param (rejects <=0 / out-of-range)."""
    val = request.args.get(name, default=default, type=int)
    if val is None or val <= 0:
        val = default
    return max(lo, min(val, hi))


DROBO_HOST = os.environ.get("DROBO_HOST", "")
DROBO_PORT = _env_int("DROBO_PORT", DEFAULT_PORT)
POLL_INTERVAL = _env_float("POLL_INTERVAL", 15.0)
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT = _env_int("WEB_PORT", 8765)

# SSH network-throughput config. Credentials come from the .env
# (DROBO_USERNAME/DROBO_PASSWORD); the monitor stays disabled without them and
# degrades gracefully if auth fails or the device is offline.
DROBO_USERNAME = os.environ.get("DROBO_USERNAME") or None
DROBO_PASSWORD = os.environ.get("DROBO_PASSWORD") or None
SSH_THROUGHPUT = _env_bool("DROBO_SSH_THROUGHPUT", "1")
THROUGHPUT_INTERVAL = _env_float("DROBO_THROUGHPUT_INTERVAL", 5.0)
NET_IFACE = os.environ.get("DROBO_NET_IFACE") or None

# SSH hardware telemetry (CPU/RAM/load/disk-IO). Same creds; same graceful
# degradation. Enabled by default when credentials are present.
SSH_HARDWARE = _env_bool("DROBO_SSH_HARDWARE", "1")
HARDWARE_INTERVAL = _env_float("DROBO_HARDWARE_INTERVAL", 5.0)

# Local history DB (machine-local state; gitignored).
DB_PATH = os.environ.get("DROBO_DB_PATH") or str(Path(__file__).with_name("data") / "history.db")
history = History(DB_PATH)


def _record_history(status) -> None:
    """Poller hook: persist a capacity sample + detect new drive errors."""
    breakdown = storage_mod.storage_breakdown(status)
    history.record_capacity(
        breakdown["used_bytes"],
        breakdown["free_bytes"],
        breakdown["protected_total_bytes"],
        breakdown["unallocated_bytes"],
        ts=status.fetched_at,
    )
    events = history.sync_errors([s.to_dict() for s in status.slots], ts=status.fetched_at)
    for ev in events:
        if ev["kind"] == "increase":
            print(
                f"[drobo] NEW drive errors: slot {ev['slot']} {ev['make']} "
                f"{ev['prev_count']} -> {ev['new_count']} (+{ev['delta']})"
            )


poller = Poller(DROBO_HOST, DROBO_PORT, interval=POLL_INTERVAL, on_poll=_record_history)

throughput = ThroughputMonitor(
    DROBO_HOST,
    DROBO_USERNAME,
    DROBO_PASSWORD,
    history=history,
    interval=THROUGHPUT_INTERVAL,
    iface=NET_IFACE,
    enabled=SSH_THROUGHPUT,
)

hardware = HardwareMonitor(
    DROBO_HOST,
    DROBO_USERNAME,
    DROBO_PASSWORD,
    history=history,
    interval=HARDWARE_INTERVAL,
    enabled=SSH_HARDWARE,
)

# The /settings page can send commands to the Drobo's unauthenticated control
# port (5001). It is OFF by default: with the flag unset the page is a read-only
# viewer and no command socket is ever opened. A per-process token is required
# on every write POST as a basic same-origin guard.
ENABLE_CONTROL = _env_bool("DROBO_ENABLE_CONTROL", "0")
CONTROL_TOKEN = secrets.token_urlsafe(24)


def _current_serial() -> str | None:
    """The Drobo serial from the cached snapshot (control commands need it)."""
    st = poller.snapshot().get("status")
    return st.get("device_serial") if st else None


# /api/raw does a live per-request fetch. Serialise + briefly cache it so a
# burst of requests (or a hung device tying up a worker for the full socket
# timeout) can't pile up concurrent connections: only one fetch runs at a
# time and callers within the TTL reuse the last good dump.
_RAW_TTL = 5.0
_raw_lock = threading.Lock()
_raw_cache: dict = {"at": 0.0, "payload": None}


def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            drobo_host=DROBO_HOST,
            poll_interval=POLL_INTERVAL,
        )

    @app.route("/api/status")
    def api_status():
        return jsonify(poller.snapshot())

    @app.route("/healthz")
    def healthz():
        snap = poller.snapshot()
        healthy = snap["reachable"] and not snap["stale"]
        payload = {"reachable": snap["reachable"], "stale": snap["stale"]}
        return jsonify(payload), (200 if healthy else 503)

    @app.route("/api/widget")
    def api_widget():
        """Flat JSON for Homepage customapi — capacity + SSH telemetry, no extra polls."""
        snap = poller.snapshot()
        status = snap.get("status")
        hw_latest = (hardware.snapshot(include_samples=False).get("latest")) or {}
        tp_latest = (throughput.snapshot(include_samples=False).get("latest")) or {}

        payload = {
            "reachable": snap["reachable"],
            "stale": snap["stale"],
            "used_pct": status.get("used_pct") if status else None,
            "used_human": status.get("used_human") if status else None,
            "free_human": status.get("free_human") if status else None,
            "status_label": status.get("status_label") if status else None,
            "uptime_human": _format_uptime_human(hw_latest.get("uptime_sec")),
            "load1": hw_latest.get("load1"),
            "rx_human": _format_bps_human(tp_latest.get("rx_bps")),
            "tx_human": _format_bps_human(tp_latest.get("tx_bps")),
        }
        return jsonify(payload)

    @app.route("/stats")
    def stats():
        # The overview page decodes numeric codes client-side; hand it the same
        # maps from drobo.codes (single source of truth) so the details page can
        # label them while still showing every raw value.
        code_maps = {
            "overall_status": {str(k): list(v) for k, v in codes.OVERALL_STATUS.items()},
            "slot_status": {str(k): list(v) for k, v in codes.SLOT_STATUS.items()},
            "redundancy": {str(k): v for k, v in codes.REDUNDANCY.items()},
            "disk_type": {str(k): v for k, v in codes.DISK_TYPE.items()},
        }
        return render_template(
            "stats.html",
            drobo_host=DROBO_HOST,
            poll_interval=POLL_INTERVAL,
            code_maps=code_maps,
        )

    @app.route("/api/raw")
    def api_raw():
        # Live, read-only fetch of the full NASD document for the on-demand
        # details page. Bounded by the client socket timeout and coalesced via a
        # short TTL cache + lock (see _raw_cache). Every failure path returns a
        # JSON error, never an HTML 500 stack trace.
        now = time.time()
        with _raw_lock:
            cached = _raw_cache["payload"]
            if cached is not None and (now - _raw_cache["at"]) < _RAW_TTL:
                return jsonify(cached)
            try:
                raw = raw_dump(read_raw(DROBO_HOST, DROBO_PORT))
            except (DroboUnreachable, RawDumpError) as exc:
                return jsonify({"error": str(exc), "host": DROBO_HOST, "port": DROBO_PORT}), 502
            except Exception:  # never leak an HTML 500 on this unauth endpoint
                app.logger.exception("unexpected error building /api/raw dump")
                return jsonify(
                    {
                        "error": "internal error building status dump",
                        "host": DROBO_HOST,
                        "port": DROBO_PORT,
                    }
                ), 502
            payload = {
                "host": DROBO_HOST,
                "port": DROBO_PORT,
                "fetched_at": now,
                "raw": raw,
            }
            _raw_cache["payload"] = payload
            _raw_cache["at"] = now
            return jsonify(payload)

    @app.route("/storage")
    def storage_page():
        return render_template(
            "storage.html",
            drobo_host=DROBO_HOST,
            poll_interval=POLL_INTERVAL,
        )

    @app.route("/errors")
    def errors_page():
        return render_template(
            "errors.html",
            drobo_host=DROBO_HOST,
            poll_interval=POLL_INTERVAL,
        )

    @app.route("/api/storage")
    def api_storage():
        status_obj = poller.current_status()
        snap = poller.snapshot()
        if status_obj is None:
            return jsonify(
                {
                    "available": False,
                    "reachable": snap["reachable"],
                    "stale": snap["stale"],
                    "last_error": snap["last_error"],
                }
            )
        return jsonify(
            {
                "available": True,
                "reachable": snap["reachable"],
                "stale": snap["stale"],
                "fetched_at": snap["last_success"],
                "breakdown": storage_mod.storage_breakdown(status_obj),
            }
        )

    @app.route("/api/history/capacity")
    def api_history_capacity():
        hours = _req_hours(24.0)
        days = _req_int("days", 14, lo=1, hi=180)
        since = time.time() - hours * 3600
        return jsonify(
            {
                "since": since,
                "hours": hours,
                "series": history.capacity_series(since),
                "daily_written": history.daily_written(days=days),
            }
        )

    @app.route("/api/history/errors")
    def api_history_errors():
        status_obj = poller.current_status()
        current = []
        if status_obj is not None:
            for s in status_obj.slots:
                if not s.present:
                    continue
                current.append(
                    {
                        "slot": s.slot,
                        "serial": s.serial,
                        "make": " ".join(x for x in (s.vendor, s.model) if x).strip(),
                        "error_count": s.error_count,
                        "status_label": s.status_label,
                        "status_severity": s.status_severity,
                        "is_accelerator": s.is_accelerator,
                        "rotational_label": s.rotational_label,
                    }
                )
        return jsonify(
            {
                "current": current,
                "events": history.error_log(limit=_req_int("limit", 200, lo=1, hi=1000)),
                "totals": history.error_totals(),
            }
        )

    @app.route("/api/reference")
    def api_reference():
        return jsonify(reference_mod.reference_tables())

    @app.route("/api/throughput")
    def api_throughput():
        return jsonify(throughput.snapshot(include_samples=True))

    @app.route("/api/history/throughput")
    def api_history_throughput():
        hours = _req_hours(1.0)
        since = time.time() - hours * 3600
        return jsonify(
            {
                "since": since,
                "hours": hours,
                "series": history.throughput_series(since),
                "status": throughput.snapshot(include_samples=False),
            }
        )

    @app.route("/hardware")
    def hardware_page():
        return render_template(
            "hardware.html",
            drobo_host=DROBO_HOST,
            poll_interval=POLL_INTERVAL,
        )

    @app.route("/api/hardware")
    def api_hardware():
        return jsonify(hardware.snapshot(include_samples=True))

    @app.route("/api/history/hardware")
    def api_history_hardware():
        hours = _req_hours(1.0)
        since = time.time() - hours * 3600
        return jsonify(
            {
                "since": since,
                "hours": hours,
                "series": history.hardware_series(since),
                "status": hardware.snapshot(include_samples=False),
            }
        )

    @app.route("/settings")
    def settings():
        return render_template(
            "settings.html",
            drobo_host=DROBO_HOST,
            poll_interval=POLL_INTERVAL,
            control_enabled=ENABLE_CONTROL,
            csrf_token=CONTROL_TOKEN,
        )

    def _guard_control():
        """Reject control writes unless enabled and carrying the page token."""
        if not ENABLE_CONTROL:
            return jsonify({"error": "controls disabled; start with DROBO_ENABLE_CONTROL=1"}), 403
        if request.headers.get("X-Drobo-Token") != CONTROL_TOKEN:
            return jsonify({"error": "missing or invalid control token"}), 403
        return None

    @app.route("/settings/identify", methods=["POST"])
    def settings_identify():
        blocked = _guard_control()
        if blocked:
            return blocked
        data = request.get_json(silent=True) or {}
        try:
            seconds = int(data.get("seconds", 900))
        except (TypeError, ValueError):
            seconds = 900
        seconds = max(0, min(seconds, 3600))
        try:
            control.identify(DROBO_HOST, seconds=seconds, serial=_current_serial())
        except control.DroboControlError as exc:
            return jsonify({"error": str(exc)}), 502
        app.logger.info("drobo control: identify for %ss", seconds)
        return jsonify({"ok": True, "action": "identify", "seconds": seconds})

    @app.route("/settings/stop-identify", methods=["POST"])
    def settings_stop_identify():
        blocked = _guard_control()
        if blocked:
            return blocked
        try:
            control.stop_identify(DROBO_HOST, serial=_current_serial())
        except control.DroboControlError as exc:
            return jsonify({"error": str(exc)}), 502
        app.logger.info("drobo control: stop identify")
        return jsonify({"ok": True, "action": "stop-identify"})

    @app.route("/settings/restart", methods=["POST"])
    def settings_restart():
        blocked = _guard_control()
        if blocked:
            return blocked
        data = request.get_json(silent=True) or {}
        if data.get("confirm") != "RESTART":
            return jsonify({"error": "type RESTART to confirm"}), 400
        # Pre-flight: never reboot mid-relayout (drives must not drop out then).
        st = poller.snapshot().get("status")
        if st and st.get("data_protection_in_progress"):
            return jsonify(
                {"error": "data protection/relayout in progress — refusing to restart"}
            ), 409
        try:
            control.restart(DROBO_HOST, serial=_current_serial())
        except control.DroboControlError as exc:
            return jsonify({"error": str(exc)}), 502
        app.logger.warning("drobo control: RESTART issued")
        return jsonify({"ok": True, "action": "restart"})

    return app


app = create_app()


def main() -> None:
    if not DROBO_HOST:
        raise SystemExit("DROBO_HOST must be set (see deploy/.env.example)")
    # Prime the cache synchronously so the first page load has data (bounded by
    # the client timeout), then keep polling in the background.
    poller.poll_once()
    poller.start()
    throughput.start()
    hardware.start()
    tp = throughput.snapshot(include_samples=False)
    hw = hardware.snapshot(include_samples=False)
    tp_err = f" ({tp['last_error']})" if tp.get("last_error") else ""
    hw_err = f" ({hw['last_error']})" if hw.get("last_error") else ""
    print(f"Drobo dashboard: http://{WEB_HOST}:{WEB_PORT}  (polling {DROBO_HOST}:{DROBO_PORT})")
    print(f"  throughput: {tp['state']}{tp_err}")
    print(f"  hardware:   {hw['state']}{hw_err}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
