"""Human-readable mappings for Drobo NASD numeric codes.

Sources: the droboports NASD-XML-format wiki
(https://github.com/droboports/droboports.github.io/wiki/NASD-XML-format) and
the status-set map in cosmouser/drobo_exporter. Severities are ours; they drive
the dashboard colour scheme.

Severity vocabulary: "ok" | "warning" | "critical" | "info" | "empty" | "unknown".
"""

from __future__ import annotations

# Overall device status (``mStatus``). Keyed by the decimal value the device
# reports; hex shown for reference.
OVERALL_STATUS: dict[int, tuple[str, str]] = {
    0x8000: ("Healthy", "ok"),  # 32768
    0x18000: ("Healthy", "ok"),  # 98304  (dashboard: OK)
    0x8004: ("Low on space (yellow)", "warning"),  # 32772
    0x8046: ("A drive was removed", "warning"),  # 32838
    0x18240: ("Data protection in progress - do not remove drives", "warning"),  # 98880
    0x8240: ("Data protection in progress", "info"),  # 33344
    0x8006: ("Out of space (red)", "critical"),  # 32774
    0x18006: ("Out of space (red)", "critical"),  # 98310
    0x8010: ("Bad drive", "critical"),  # 32784
    0x18010: ("Bad drive", "critical"),  # 98320
}

# Per-slot status (``mSlotsExp/nX/mStatus``).
SLOT_STATUS: dict[int, tuple[str, str]] = {
    0x01: ("Full - replace/add a drive", "critical"),  # 1
    0x02: ("Filling up - add a drive soon", "warning"),  # 2
    0x03: ("Healthy", "ok"),  # 3
    0x04: ("Data relayout in progress", "info"),  # 4
    0x80: ("Empty", "empty"),  # 128
    0x81: ("Drive removed", "warning"),  # 129
    0x86: ("Drive failure", "critical"),  # 134
}

# Redundancy mode (``mFirmwareFeatureStates``).
REDUNDANCY: dict[int, str] = {
    0x4: "Unknown",
    0x6: "Single-drive redundancy",
    0x7: "Dual-drive redundancy",
}

# Disk type (``mDiskType``).
DISK_TYPE: dict[int, str] = {
    0: "HDD",
    4: "SSD",
}

# Per-slot disk state (``mSlotsExp/nX/mDiskState``). Best-effort: the field is
# undocumented, but on the 5N data drives report 16 and the mSATA cache reports
# 32, and empty bays report 0. Anything else is surfaced verbatim.
DISK_STATE: dict[int, str] = {
    0: "Empty / no drive",
    16: "In use — data pack",
    32: "In use — mSATA cache",
}

# Rotational speed (``mSlotsExp/nX/RotationalSpeed``). This field is NOT the raw
# RPM and is undocumented in the community NASD-XML wiki (newer firmware added
# it). It decodes cleanly as RPM / 200, cross-checked against the actual drives:
#   1  -> SSD (non-rotating)   27 -> 5400 RPM   36 -> 7200 RPM
# so we treat it as ``code * 200`` with 1 meaning "solid state".
RPM_CODE_FACTOR = 200
KNOWN_RPM_CODES: dict[int, str] = {
    0: "Unknown",
    1: "SSD (no rotation)",
    27: "5400 RPM",
    33: "6600 RPM",
    36: "7200 RPM",
    54: "10800 RPM",
    75: "15000 RPM",
}


def overall_status(code: int) -> tuple[str, str]:
    """Return (label, severity) for an overall ``mStatus`` code."""
    return OVERALL_STATUS.get(code, (f"Unknown status (code {code})", "unknown"))


def slot_status(code: int) -> tuple[str, str]:
    """Return (label, severity) for a slot ``mStatus`` code."""
    return SLOT_STATUS.get(code, (f"Unknown (code {code})", "unknown"))


def redundancy(code: int) -> str:
    """Return the human label for a ``mFirmwareFeatureStates`` code."""
    return REDUNDANCY.get(code, f"Unknown (code {code})")


def disk_type(code: int) -> str:
    """Return "HDD"/"SSD"/other for a ``mDiskType`` code."""
    return DISK_TYPE.get(code, f"Type {code}")


def disk_state(code: int) -> str:
    """Return a human label for a ``mDiskState`` code."""
    return DISK_STATE.get(code, f"State {code}")


def rpm_from_code(code: int) -> tuple[int | None, str]:
    """Decode a ``RotationalSpeed`` code into ``(rpm, label)``.

    ``rpm`` is ``None`` for SSD / unknown. The label is display-ready
    ("5400 RPM", "SSD (no rotation)", ...).
    """
    if code in KNOWN_RPM_CODES:
        label = KNOWN_RPM_CODES[code]
        if code <= 1:
            return None, label
        return code * RPM_CODE_FACTOR, label
    if code <= 0:
        return None, "Unknown"
    return code * RPM_CODE_FACTOR, f"{code * RPM_CODE_FACTOR} RPM"
