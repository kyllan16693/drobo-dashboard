"""Live hardware telemetry for the Drobo, sampled over SSH.

The Drobo runs a full Linux (kernel 3.2, a 3-core Marvell Armada XP, ~868 MB
RAM). None of that is in the NASD status feed, so — exactly like the throughput
monitor — we SSH in on a timer and read ``/proc`` directly:

* ``/proc/stat``      → CPU utilisation (overall + per core), incl. iowait
* ``/proc/meminfo``   → RAM + swap usage
* ``/proc/loadavg``   → 1/5/15-minute load + process counts
* ``/proc/uptime``    → uptime
* ``/proc/diskstats`` → real I/O on the virtual volume (sda), read/write bytes/s
* busybox ``top``     → the busiest processes (best-effort)

CPU% and disk-I/O are rates derived from deltas between successive samples, so
the first tick only establishes a baseline. Everything degrades gracefully:
no creds → ``disabled``; bad creds → ``auth_failed`` (hard back-off); device
offline → ``unreachable``. The rest of the dashboard is unaffected either way.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import paramiko
from paramiko.ssh_exception import AuthenticationException

logger = logging.getLogger(__name__)

_SECTOR = 512  # /proc/diskstats counts 512-byte sectors regardless of block size

# One round-trip pulls everything; markers let us split it back apart.
_BUNDLE = (
    "echo @@LOAD; cat /proc/loadavg; "
    "echo @@UP; cat /proc/uptime; "
    "echo @@STAT; grep '^cpu' /proc/stat; "
    "echo @@MEM; cat /proc/meminfo; "
    "echo @@DISK; awk '$3==\"sda\"{print}' /proc/diskstats; "
    "echo @@TOP; top -bn1 2>/dev/null | head -14"
)

_INFO_CMD = (
    "echo @@CORES; grep -c '^processor' /proc/cpuinfo; "
    "echo @@MODEL; grep -m1 -i -E 'model name|Processor' /proc/cpuinfo | cut -d: -f2-; "
    "echo @@KERNEL; uname -r; "
    "echo @@MEMTOTAL; awk '/MemTotal/{print $2}' /proc/meminfo"
)


def _split_sections(text: str) -> dict[str, list[str]]:
    """Split marker-delimited output (``@@NAME`` lines) into {name: [lines]}."""
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for line in text.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return out


def _parse_cpu_line(line: str) -> tuple[int, int, int] | None:
    """Return (total_jiffies, idle_jiffies, iowait_jiffies) for a /proc/stat cpu line."""
    parts = line.split()
    if len(parts) < 5 or not parts[0].startswith("cpu"):
        return None
    try:
        nums = [int(x) for x in parts[1:]]
    except ValueError:
        return None
    idle = nums[3] if len(nums) > 3 else 0
    iowait = nums[4] if len(nums) > 4 else 0
    return sum(nums), idle + iowait, iowait


def _parse_meminfo(lines: list[str]) -> dict[str, int]:
    """/proc/meminfo → {key: bytes}."""
    out: dict[str, int] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        val = rest.strip().split()
        if not val:
            continue
        try:
            out[key.strip()] = int(val[0]) * 1024  # values are in kB
        except ValueError:
            continue
    return out


def _parse_diskstats(lines: list[str]) -> tuple[int, int] | None:
    """sda line → (sectors_read, sectors_written)."""
    for line in lines:
        f = line.split()
        if len(f) >= 10 and f[2] == "sda":
            try:
                return int(f[5]), int(f[9])  # sectors read (f[5]), written (f[9])
            except ValueError:
                return None
    return None


def _parse_top(lines: list[str], limit: int = 6) -> list[dict]:
    """busybox `top -bn1` process rows → [{pid, user, cpu_pct, mem_pct, cmd}]."""
    procs: list[dict] = []
    header_idx = None
    for i, line in enumerate(lines):
        if "PID" in line and "COMMAND" in line:
            header_idx = i
            break
    if header_idx is None:
        return procs
    for line in lines[header_idx + 1 :]:
        f = line.split()
        if len(f) < 9:
            continue
        try:
            procs.append(
                {
                    "pid": int(f[0]),
                    "user": f[2],
                    "mem_pct": float(f[5]),
                    "cpu_pct": float(f[7]),
                    "cmd": " ".join(f[8:])[:60],
                }
            )
        except (ValueError, IndexError):
            continue
        if len(procs) >= limit:
            break
    return procs


class HardwareMonitor:
    def __init__(
        self,
        host: str,
        username: str | None,
        password: str | None,
        history=None,
        interval: float = 5.0,
        buffer_size: int = 360,
        connect_timeout: float = 6.0,
        enabled: bool = True,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.history = history
        self.interval = max(2.0, interval)
        self.connect_timeout = connect_timeout
        self._want_enabled = enabled

        self._lock = threading.Lock()
        self._samples: deque[dict] = deque(maxlen=buffer_size)
        self._state = "idle"
        self._last_error: str | None = None
        self._info: dict = {}
        self._top: list[dict] = []
        self._prev_cpu: dict[str, tuple[int, int, int]] | None = None
        self._prev_disk: tuple[float, int, int] | None = None
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
                self._last_error = (
                    "no SSH credentials (DROBO_USERNAME/DROBO_PASSWORD)"
                    if self._want_enabled
                    else "hardware monitor disabled"
                )
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="drobo-hardware", daemon=True)
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
                self._prev_cpu = None
                self._prev_disk = None
                wait = 60.0
            except Exception as exc:  # noqa: BLE001 - degrade, never kill the thread
                self._disconnect()
                with self._lock:
                    self._state = "unreachable"
                    self._last_error = f"{type(exc).__name__}: {exc}"
                self._prev_cpu = None
                self._prev_disk = None
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
        self._load_info()

    def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _read(self, cmd: str) -> str:
        assert self._client is not None
        _in, out, _err = self._client.exec_command(cmd, timeout=self.connect_timeout + 6)
        return out.read().decode("utf-8", errors="replace")

    def _load_info(self) -> None:
        try:
            sec = _split_sections(self._read(_INFO_CMD))
            cores = int((sec.get("CORES") or ["0"])[0] or 0)
            model = " ".join(sec.get("MODEL") or []).strip()
            kernel = " ".join(sec.get("KERNEL") or []).strip()
            memtotal = int((sec.get("MEMTOTAL") or ["0"])[0] or 0) * 1024
            with self._lock:
                self._info = {
                    "cores": cores or None,
                    "cpu_model": model or None,
                    "kernel": kernel or None,
                    "mem_total_bytes": memtotal or None,
                }
        except Exception:  # noqa: BLE001 - static info is best-effort
            logger.debug("failed to read Drobo hardware info", exc_info=True)

    def _tick(self) -> None:
        if self._client is None:
            self._connect()
        now = time.time()
        sec = _split_sections(self._read(_BUNDLE))

        # --- CPU (overall + per core) ---------------------------------- #
        cur_cpu: dict[str, tuple[int, int, int]] = {}
        for line in sec.get("STAT", []):
            parsed = _parse_cpu_line(line)
            if parsed:
                cur_cpu[line.split()[0]] = parsed
        cpu_pct = iowait_pct = None
        per_core: list[float] = []
        if self._prev_cpu:
            cpu_pct, iowait_pct = self._cpu_delta("cpu", cur_cpu)
            i = 0
            while f"cpu{i}" in cur_cpu:
                core_pct, _ = self._cpu_delta(f"cpu{i}", cur_cpu)
                if core_pct is not None:
                    per_core.append(round(core_pct, 1))
                i += 1
        self._prev_cpu = cur_cpu

        # --- Memory ---------------------------------------------------- #
        mem = _parse_meminfo(sec.get("MEM", []))
        mem_total = mem.get("MemTotal", 0)
        mem_free = mem.get("MemFree", 0)
        mem_cache = mem.get("Buffers", 0) + mem.get("Cached", 0)
        mem_used = max(0, mem_total - mem_free - mem_cache)
        swap_total = mem.get("SwapTotal", 0)
        swap_used = max(0, swap_total - mem.get("SwapFree", 0))

        # --- Load / uptime --------------------------------------------- #
        load1 = load5 = load15 = None
        procs_running = procs_total = None
        if sec.get("LOAD"):
            try:
                lf = sec["LOAD"][0].split()
                if len(lf) >= 3:
                    load1, load5, load15 = (float(lf[0]), float(lf[1]), float(lf[2]))
                if len(lf) >= 4 and "/" in lf[3]:
                    r, _, t = lf[3].partition("/")
                    procs_running, procs_total = int(r), int(t)
            except (ValueError, IndexError):
                pass  # degrade this field like uptime; keep the rest of the sample
        uptime_sec = None
        if sec.get("UP"):
            try:
                uptime_sec = float(sec["UP"][0].split()[0])
            except (ValueError, IndexError):
                pass

        # --- Disk I/O on the virtual volume ---------------------------- #
        disk_r_bps = disk_w_bps = None
        ds = _parse_diskstats(sec.get("DISK", []))
        if ds is not None:
            sr, sw = ds
            if self._prev_disk is not None:
                p_ts, p_sr, p_sw = self._prev_disk
                dt = now - p_ts
                if dt > 0:
                    d_r = (sr - p_sr) if sr >= p_sr else 0
                    d_w = (sw - p_sw) if sw >= p_sw else 0
                    disk_r_bps = d_r * _SECTOR / dt
                    disk_w_bps = d_w * _SECTOR / dt
            self._prev_disk = (now, sr, sw)

        # --- Top processes (best-effort) ------------------------------- #
        top = _parse_top(sec.get("TOP", []))

        sample = {
            "ts": now,
            "cpu_pct": round(cpu_pct, 1) if cpu_pct is not None else None,
            "iowait_pct": round(iowait_pct, 1) if iowait_pct is not None else None,
            "per_core": per_core,
            "mem_used": mem_used,
            "mem_total": mem_total,
            "mem_cache": mem_cache,
            "mem_free": mem_free,
            "mem_used_pct": round(100 * mem_used / mem_total, 1) if mem_total else None,
            "swap_used": swap_used,
            "swap_total": swap_total,
            "load1": load1,
            "load5": load5,
            "load15": load15,
            "procs_running": procs_running,
            "procs_total": procs_total,
            "disk_r_bps": disk_r_bps,
            "disk_w_bps": disk_w_bps,
            "uptime_sec": uptime_sec,
        }
        with self._lock:
            if top:
                self._top = top
            # Only buffer samples that carry computed rates (skip the baseline).
            if sample["cpu_pct"] is not None:
                self._samples.append(sample)
        if self.history is not None and sample["cpu_pct"] is not None:
            try:
                self.history.record_hardware(sample)
            except Exception:
                logger.exception("failed to record hardware sample")

    def _cpu_delta(self, key: str, cur: dict) -> tuple[float | None, float | None]:
        prev = self._prev_cpu.get(key) if self._prev_cpu else None
        now = cur.get(key)
        if not prev or not now:
            return None, None
        d_total = now[0] - prev[0]
        if d_total <= 0:
            return None, None
        d_idle = now[1] - prev[1]
        d_iowait = now[2] - prev[2]
        busy = 100.0 * (d_total - d_idle) / d_total
        iowait = 100.0 * d_iowait / d_total
        return max(0.0, min(100.0, busy)), max(0.0, min(100.0, iowait))

    # ------------------------------------------------------------------ #
    def snapshot(self, include_samples: bool = True) -> dict:
        with self._lock:
            samples = list(self._samples)
            state = self._state
            last_error = self._last_error
            info = dict(self._info)
            top = list(self._top)
        latest = samples[-1] if samples else None
        out = {
            "enabled": self.enabled,
            "state": state,
            "interval": self.interval,
            "last_error": last_error,
            "info": info,
            "latest": latest,
            "top": top,
        }
        if include_samples:
            out["samples"] = samples
        return out
