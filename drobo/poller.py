"""Background poller that keeps a cached snapshot of the Drobo status.

Polling on a timer (rather than fetching on every web request) keeps the
dashboard snappy, avoids hammering the aging device, and lets us keep serving
the last known-good data with a "stale" flag when the Drobo goes unreachable.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .client import DroboUnreachable, read_raw
from .models import DroboStatus
from .parser import DroboParseError, parse

logger = logging.getLogger(__name__)


class Poller:
    """Polls a Drobo on an interval in a daemon thread and caches the result."""

    def __init__(
        self,
        host: str,
        port: int = 5000,
        interval: float = 15.0,
        timeout: float = 5.0,
        stale_after: float | None = None,
        on_poll: Callable[[DroboStatus], None] | None = None,
        on_result: Callable[[dict], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.interval = interval
        self.timeout = timeout
        # Consider data stale once it is older than a few poll intervals.
        self.stale_after = stale_after if stale_after is not None else max(interval * 3, 30.0)
        # Optional hook called with the fresh status after every successful
        # poll (used to record history). Failures here never break polling.
        self.on_poll = on_poll
        # Optional hook called with self.snapshot() after EVERY poll_once()
        # call, success or failure (used to detect reachability transitions).
        # Independent of on_poll; failures here never break polling either.
        self.on_result = on_result

        self._lock = threading.Lock()
        self._status: DroboStatus | None = None
        self._last_success: float | None = None
        self._last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> None:
        """Fetch and parse once; update cache on success, record error on failure."""
        try:
            status = parse(read_raw(self.host, self.port, self.timeout))
        except (DroboUnreachable, DroboParseError) as exc:
            with self._lock:
                self._last_error = str(exc)
        else:
            status.fetched_at = time.time()
            with self._lock:
                self._status = status
                self._last_success = status.fetched_at
                self._last_error = None
            if self.on_poll is not None:
                try:
                    self.on_poll(status)
                except Exception:  # a history hiccup must never stop polling
                    logger.exception("on_poll hook failed")

        if self.on_result is not None:
            try:
                self.on_result(self.snapshot())
            except Exception:  # same contract as on_poll: never break polling
                logger.exception("on_result hook failed")

    def start(self) -> None:
        """Start the background polling thread (no-op if already running)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="drobo-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval)

    def current_status(self) -> DroboStatus | None:
        """Return the last-parsed status object (for server-side derivations)."""
        with self._lock:
            return self._status

    def snapshot(self) -> dict:
        """Return the current cached state as a JSON-serialisable dict."""
        now = time.time()
        with self._lock:
            status = self._status
            last_success = self._last_success
            last_error = self._last_error

        age = (now - last_success) if last_success else None
        stale = age is None or age > self.stale_after
        return {
            "host": self.host,
            "port": self.port,
            "poll_interval": self.interval,
            "reachable": status is not None and last_error is None,
            "stale": stale,
            "age_seconds": round(age, 1) if age is not None else None,
            "last_success": last_success,
            "last_error": last_error,
            "status": status.to_dict() if status else None,
        }
