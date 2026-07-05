"""Structured data models for a Drobo status snapshot.

These dataclasses hold both the raw values pulled from the XML and a few
derived, display-friendly fields (human sizes, percentages, status labels).
``to_dict`` produces plain JSON-serialisable dicts for the web API.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


def human_bytes(num: int, binary: bool = False) -> str:
    """Format a byte count as a short human string.

    Decimal units (TB/GB, base 1000) by default, matching how the Drobo
    Dashboard reports capacity. Pass ``binary=True`` for TiB/GiB (base 1024).
    """
    if num is None:
        return "-"
    step = 1024.0 if binary else 1000.0
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB") if binary else (
        "B", "KB", "MB", "GB", "TB", "PB"
    )
    value = float(num)
    for unit in units:
        if abs(value) < step or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= step
    return f"{value:.2f} {units[-1]}"


@dataclass
class DiskSlot:
    """A single physical drive bay on the Drobo."""

    slot: int
    present: bool
    is_accelerator: bool
    status_code: int
    status_label: str
    status_severity: str
    disk_type_code: int
    disk_type: str
    disk_state: int
    vendor: str
    model: str
    serial: str
    firmware: str
    capacity_bytes: int
    capacity_human: str
    error_count: int
    rotational_speed: int
    rotational_label: str
    disk_state_label: str
    ssd_life_remaining: int
    temperature: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DroboStatus:
    """A full snapshot of a Drobo's health and capacity."""

    name: str
    model: str
    device_serial: str
    firmware_version: str
    firmware_release: str

    status_code: int
    status_label: str
    status_severity: str

    redundancy_code: int
    redundancy_label: str

    relayout_count: int
    data_protection_in_progress: bool

    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_pct: float
    yellow_threshold_pct: float
    red_threshold_pct: float

    total_human: str
    used_human: str
    free_human: str

    slot_count: int
    slots: list[DiskSlot] = field(default_factory=list)

    droboapps_enabled: bool = False

    # Populated by the poller; None straight out of the parser.
    fetched_at: float | None = None

    def to_dict(self) -> dict:
        data = asdict(self)  # recurses into DiskSlot entries
        return data
