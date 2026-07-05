"""Parse a Drobo NASD ``<ESATMUpdate>`` XML document into models."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from . import codes
from .models import DiskSlot, DroboStatus, human_bytes

# Overall statuses that mean a data relayout ("data protection") is happening.
_RELAYOUT_STATUS_CODES = {0x8240, 0x18240}


# Cap nesting depth (parity with rawdump.py) so a spoofed device cannot feed
# deeply nested DTD-free XML that blows the interpreter stack.
_MAX_DEPTH = 64


class DroboParseError(Exception):
    """Raised when the status XML cannot be parsed into a model."""


def _assert_xml_depth(xml_text: str) -> None:
    """Reject documents whose open-tag nesting exceeds ``_MAX_DEPTH``."""
    depth = 0
    i = 0
    n = len(xml_text)
    while i < n:
        if xml_text[i] != "<":
            i += 1
            continue
        if i + 1 < n and xml_text[i + 1] in ("!", "?"):
            end = xml_text.find(">", i)
            i = (end + 1) if end != -1 else n
            continue
        if i + 1 < n and xml_text[i + 1] == "/":
            depth -= 1
            i += 2
            continue
        depth += 1
        if depth > _MAX_DEPTH:
            raise DroboParseError("status document nested too deeply; refusing to parse")
        i += 1


def _text(el: ET.Element | None, tag: str, default: str = "") -> str:
    if el is None:
        return default
    child = el.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _int(el: ET.Element | None, tag: str, default: int = 0) -> int:
    raw = _text(el, tag, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _split_make(make: str) -> tuple[str, str]:
    """Split a SCSI INQUIRY string into (vendor, model).

    The field is an 8-byte vendor id followed by the product id. In practice the
    vendor is a single token, so splitting on the first run of whitespace is the
    most reliable ("WDC      WD40EFRX-68N", "MICRON M600" and "KINGSTON
    SKC600MSATA" all split correctly). Only for a single-token make with no
    whitespace do we fall back to the fixed 8-byte slice.
    """
    make = make.strip()
    if not make:
        return "", ""
    parts = make.split(None, 1)
    if len(parts) == 2:
        return parts[0], " ".join(parts[1].split())
    if len(make) > 8:
        vendor = make[:8].strip()
        model = " ".join(make[8:].split())
        if vendor and model:
            return vendor, model
    return make, ""


def _parse_slot(node: ET.Element) -> DiskSlot | None:
    """Parse one ``<nX>`` slot node, or None if it isn't a real slot node."""
    if node.find("mSlotNumber") is None:
        return None

    slot_no = _int(node, "mSlotNumber")
    status_code = _int(node, "mStatus")
    status_label, status_severity = codes.slot_status(status_code)
    disk_type_code = _int(node, "mDiskType")
    disk_state = _int(node, "mDiskState")
    capacity = _int(node, "mPhysicalCapacity")
    vendor, model = _split_make(_text(node, "mMake"))
    rotational_speed = _int(node, "RotationalSpeed")
    _rpm, rotational_label = codes.rpm_from_code(rotational_speed)

    present = status_code != 0x80 and capacity > 0
    # On the Drobo 5N the 6th bay is an mSATA cache accelerator, not a data
    # bay. It reports mDiskState 32 (HDDs report 16) and disk type SSD.
    is_accelerator = disk_type_code == 4 and disk_state == 32

    return DiskSlot(
        slot=slot_no,
        present=present,
        is_accelerator=is_accelerator,
        status_code=status_code,
        status_label=status_label,
        status_severity=status_severity,
        disk_type_code=disk_type_code,
        disk_type=codes.disk_type(disk_type_code),
        disk_state=disk_state,
        vendor=vendor,
        model=model,
        serial=_text(node, "mSerial"),
        firmware=_text(node, "mDiskFwRev"),
        capacity_bytes=capacity,
        capacity_human=human_bytes(capacity) if capacity else "-",
        error_count=_int(node, "mErrorCount"),
        rotational_speed=rotational_speed,
        rotational_label=rotational_label,
        disk_state_label=codes.disk_state(disk_state),
        ssd_life_remaining=_int(node, "SSDLifeRemaining"),
        temperature=_int(node, "mTemperature"),
    )


def _parse_slots(root: ET.Element) -> list[DiskSlot]:
    """Parse all slots, de-duplicating repeated ``<nX>`` nodes.

    Some firmware dumps emit the same slot index twice (an empty node plus a
    populated one). Keep whichever is populated.
    """
    container = root.find("mSlotsExp")
    if container is None:
        return []

    by_number: dict[int, DiskSlot] = {}
    for node in list(container):
        slot = _parse_slot(node)
        if slot is None:
            continue
        existing = by_number.get(slot.slot)
        if existing is None or (slot.present and not existing.present):
            by_number[slot.slot] = slot

    return [by_number[n] for n in sorted(by_number)]


def _threshold_pct(raw: int) -> float:
    """Convert a Drobo threshold (``XXYY`` -> ``XX.YY%``) to a float percent."""
    return raw / 100.0


def parse(xml_text: str) -> DroboStatus:
    """Parse a NASD ``<ESATMUpdate>`` document into a :class:`DroboStatus`."""
    # A genuine Drobo document never contains a DTD. Reject one outright so a
    # spoofed/compromised device on this unauthenticated port cannot trigger an
    # entity-expansion ("billion laughs") memory-exhaustion attack.
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        raise DroboParseError("status document contains a DTD; refusing to parse")
    _assert_xml_depth(xml_text)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise DroboParseError(f"invalid status XML: {exc}") from exc

    if root.tag != "ESATMUpdate":
        raise DroboParseError(f"unexpected root element <{root.tag}>")

    status_code = _int(root, "mStatus")
    status_label, status_severity = codes.overall_status(status_code)
    redundancy_code = _int(root, "mFirmwareFeatureStates")
    relayout_count = _int(root, "mRelayoutCount")

    total = _int(root, "mTotalCapacityProtected")
    used = _int(root, "mUsedCapacityProtected")
    free = _int(root, "mFreeCapacityProtected")
    used_pct = round((used / total) * 100, 2) if total else 0.0

    droboapps = root.find("DroboApps")
    droboapps_enabled = _int(droboapps, "DNASDroboAppsEnabled") == 1 if droboapps is not None else False

    model = _text(root, "mModel") or _text(root, "mName")

    return DroboStatus(
        name=_text(root, "mName"),
        model=model,
        device_serial=_text(root, "mSerial"),
        firmware_version=_text(root, "mVersion"),
        firmware_release=_text(root, "mReleaseDate"),
        status_code=status_code,
        status_label=status_label,
        status_severity=status_severity,
        redundancy_code=redundancy_code,
        redundancy_label=codes.redundancy(redundancy_code),
        relayout_count=relayout_count,
        data_protection_in_progress=relayout_count > 0 or status_code in _RELAYOUT_STATUS_CODES,
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
        used_pct=used_pct,
        yellow_threshold_pct=_threshold_pct(_int(root, "mYellowThreshold")),
        red_threshold_pct=_threshold_pct(_int(root, "mRedThreshold")),
        total_human=human_bytes(total),
        used_human=human_bytes(used),
        free_human=human_bytes(free),
        slot_count=_int(root, "mSlotCountExp"),
        slots=_parse_slots(root),
        droboapps_enabled=droboapps_enabled,
    )
