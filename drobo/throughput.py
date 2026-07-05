"""Live network throughput for the Drobo, sampled over SSH.

The NASD status stream has no I/O counters, so real throughput has to come from
the device's own kernel. We SSH in on a timer, read ``/proc/net/dev``, and turn
the monotonically-increasing byte counters into a bytes/sec rate.

The connection is kept open between samples and reconnected on failure. If the
credentials are wrong we detect it once and back off hard (rather than hammering
the SSH server), surfacing an ``auth_failed`` state the UI can show. Everything
degrades gracefully: no creds -> ``disabled``; device offline -> ``unreachable``;
the rest of the dashboard is unaffected.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import paramiko
from paramiko.ssh_exception import AuthenticationException

logger = logging.getLogger(__name__)

# Interfaces that never represent real NAS traffic.
_SKIP_IFACES = {"lo", "sit0"}


def parse_proc_net_dev(text: str) -> dict[str, tuple[int, int]]:
    """Parse /proc/net/dev into {iface: (rx_bytes, tx_bytes)}."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        fields = rest.split()
        if len(fields) < 16:
            continue
        try:
            rx = int(fields[0])
            tx = int(fields[8])
        except ValueError:
            continue
        out[name] = (rx, tx)
    return out


def pick_interface(counters: dict[str, tuple[int, int]]) -> str | None:
    """Choose the busiest real interface (most total bytes)."""
    best, best_bytes = None, -1
    for name, (rx, tx) in counters.items():
        if name in _SKIP_IFACES:
            continue
        total = rx + tx
        if total > best_bytes:
            best, best_bytes = name, total
    return best


class ThroughputMonitor:
    def __init__(
        self,
        host: str,
        username: str | None,
        password: str | None,
        history=None,
        interval: float = 5.0,
        iface: str | None = None,
        buffer_size: int = 360,
        connect_timeout: float = 6.0,
        enabled: bool = True,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.history = history
        self.interval = max(1.0, interval)
        self.connect_timeout = connect_timeout
        self._configured_iface = iface
        self._want_enabled = enabled

        self._lock = threading.Lock()
        self._samples: deque[dict] = deque(maxlen=buffer_size)
        self._state = "idle"
        self._last_error: str | None = None
        self._iface: str | None = iface
        self._prev: tuple[float, int, int] | None = None
        self._client: paramiko.SSHClient | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._want_enabled and self.username and self.password)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if not self.enabled:
            with self._lock:
                self._state = "disabled"
                self._last_error = None if self._want_enabled else "throughput disabled"
                if self._want_enabled and not (self.username and self.password):
                    self._last_error = "no SSH credentials (DROBO_USERNAME/DROBO_PASSWORD)"
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="drobo-throughput", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._disconnect()
        if self._thread:
            self._thread.join(timeout=2)

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        while not self._stop.is_set():
            wait = self.interval
            try:
                self._tick()
                with self._lock:
                    self._state = "ok"
                    self._last_error = None
            except AuthenticationException:
                self._disconnect()
                with self._lock:
                    self._state = "auth_failed"
                    self._last_error = "SSH authentication failed — check DROBO_PASSWORD in .env"
                self._prev = None
                wait = 60.0  # do not hammer the SSH server on bad creds
            except Exception as exc:  # noqa: BLE001 - degrade, never crash the thread
                self._disconnect()
                with self._lock:
                    self._state = "unreachable"
                    self._last_error = f"{type(exc).__name__}: {exc}"
                self._prev = None
                wait = min(30.0, self.interval * 3)
            self._stop.wait(wait)

    def _connect(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.host,
            username=self.username,
            password=self.password,
            timeout=self.connect_timeout,
            banner_timeout=self.connect_timeout,
            auth_timeout=self.connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        self._client = client

    def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _read(self, cmd: str) -> str:
        assert self._client is not None
        _stdin, stdout, _stderr = self._client.exec_command(cmd, timeout=self.connect_timeout)
        return stdout.read().decode("utf-8", errors="replace")

    def _tick(self) -> None:
        if self._client is None:
            self._connect()
        counters = parse_proc_net_dev(self._read("cat /proc/net/dev"))
        if not counters:
            raise RuntimeError("no interfaces in /proc/net/dev")

        iface = self._configured_iface or self._iface or pick_interface(counters)
        if iface not in counters:
            iface = pick_interface(counters)
        if iface is None or iface not in counters:
            raise RuntimeError("could not determine a network interface")
        self._iface = iface

        rx, tx = counters[iface]
        now = time.time()

        if self._prev is not None:
            p_ts, p_rx, p_tx = self._prev
            dt = now - p_ts
            if dt > 0:
                # Guard against counter resets (reboot) producing huge spikes.
                d_rx = rx - p_rx if rx >= p_rx else 0
                d_tx = tx - p_tx if tx >= p_tx else 0
                rx_bps = d_rx / dt
                tx_bps = d_tx / dt
                sample = {"ts": now, "rx_bps": rx_bps, "tx_bps": tx_bps}
                with self._lock:
                    self._samples.append(sample)
                if self.history is not None:
                    try:
                        self.history.record_throughput(iface, rx, tx, rx_bps, tx_bps, ts=now)
                    except Exception:
                        logger.exception("failed to record throughput sample")
        self._prev = (now, rx, tx)

    # ------------------------------------------------------------------ #
    def snapshot(self, include_samples: bool = True) -> dict:
        with self._lock:
            samples = list(self._samples)
            state = self._state
            last_error = self._last_error
            iface = self._iface
        latest = samples[-1] if samples else None
        out = {
            "enabled": self.enabled,
            "state": state,
            "iface": iface,
            "interval": self.interval,
            "last_error": last_error,
            "latest": latest,
        }
        if include_samples:
            out["samples"] = samples
        return out
