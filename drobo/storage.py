"""Derive an old-Drobo-Dashboard-style capacity breakdown from a snapshot.

The device reports the protected pool exactly (used + free = total protected).
The classic dashboard also split the *non-usable* capacity into two very
different things, and getting that split right is the whole point of this
module:

* **Used for protection** — capacity actively holding redundancy (parity /
  mirror) data for the protected pool.
* **Reserved for expansion** (a.k.a. "unallocated") — capacity that physically
  exists on the larger drives but can't be protected because the drives are
  mismatched. It's unlocked only by swapping a smaller drive for a larger one.

The naive "usable = raw − the two largest drives" rule (which Drobo's own
calculator uses) gets the *usable* number right, but it lumps protection and
reserved-for-expansion together — so on a mismatched pack it wildly overstates
"protection". BeyondRAID actually partitions the disks into horizontal *zones*
at each distinct drive size; each zone gets its own redundancy. A zone with
``k`` active disks spends ``min(redundancy, k)`` disks' worth of that zone's
height on protection — but only if ``k > redundancy``. A zone with too few
disks to protect (``k <= redundancy``) contributes nothing usable and nothing
to protection; its whole height is reserved for expansion.

For this 5N (8+8+4+4+3 TB, dual redundancy) that yields ~8 TB of real
protection and ~8 TB reserved-for-expansion — not 16 TB of "protection" and
~0 reserved, which is what the two-largest-drives shortcut implied.
"""

from __future__ import annotations

from .models import DroboStatus, human_bytes


def _severity(used_pct: float, yellow: float, red: float) -> str:
    if used_pct >= red:
        return "critical"
    if used_pct >= yellow:
        return "warning"
    return "ok"


def protection_reserve(caps: list[int], redundancy_level: int) -> int:
    """Capacity actively used for redundancy, via the BeyondRAID zone model.

    ``caps`` is the list of data-bay physical capacities (any order).
    Zones are the height bands between successive distinct drive sizes; a band
    reaching ``k`` disks spends ``redundancy_level`` disks' worth of that band
    on protection when ``k > redundancy_level``, and nothing otherwise (that
    band is unprotectable and becomes reserved-for-expansion instead).
    """
    caps = sorted(caps)  # ascending, so we peel bands off the bottom
    n = len(caps)
    protection = 0
    prev = 0
    for i, cap in enumerate(caps):
        height = cap - prev
        if height > 0:
            active = n - i  # disks that reach this height band
            if active > redundancy_level:
                protection += redundancy_level * height
        prev = cap
    return protection


def storage_breakdown(status: DroboStatus) -> dict:
    """Return the capacity pie segments + parity/raw context for a status."""
    data_bays = [s for s in status.slots if s.present and not s.is_accelerator]
    caps = sorted((s.capacity_bytes for s in data_bays), reverse=True)
    raw_physical = sum(caps)

    # Redundancy level: single (6) tolerates one failure, dual (7) two. Default
    # to single if the mode is unknown (conservative).
    redundancy_level = 2 if status.redundancy_code == 0x7 else 1

    protected_total = status.total_bytes
    used = status.used_bytes
    free = status.free_bytes

    # Split the non-usable capacity into real protection (zone model) and
    # everything else (reserved-for-expansion + a little management overhead we
    # can't separate). Anchor "usable" to the device's own protected total so
    # used+free is always exact, and derive the two remaining slices from it.
    parity_reserve = min(
        protection_reserve(caps, redundancy_level), max(0, raw_physical - protected_total)
    )
    unallocated = max(0, raw_physical - parity_reserve - protected_total)
    pie_total = protected_total + unallocated

    used_pct = round((used / protected_total) * 100, 2) if protected_total else 0.0
    yellow = status.yellow_threshold_pct
    red = status.red_threshold_pct

    return {
        # Pie segments (always show used + free; unallocated is toggleable).
        "used_bytes": used,
        "free_bytes": free,
        "unallocated_bytes": unallocated,
        "pie_total_bytes": pie_total,
        "used_human": human_bytes(used),
        "free_human": human_bytes(free),
        "unallocated_human": human_bytes(unallocated),
        "pie_total_human": human_bytes(pie_total),
        # Protected pool + fullness.
        "protected_total_bytes": protected_total,
        "protected_total_human": human_bytes(protected_total),
        "used_pct": used_pct,
        "free_pct": round(100 - used_pct, 2) if protected_total else 0.0,
        "yellow_threshold_pct": yellow,
        "red_threshold_pct": red,
        "severity": _severity(used_pct, yellow, red),
        # Context for the "how the disks are used" breakdown.
        "raw_physical_bytes": raw_physical,
        "raw_physical_human": human_bytes(raw_physical),
        "parity_reserve_bytes": parity_reserve,
        "parity_reserve_human": human_bytes(parity_reserve),
        "redundancy_level": redundancy_level,
        "redundancy_label": status.redundancy_label,
        "data_bay_count": len(data_bays),
    }
