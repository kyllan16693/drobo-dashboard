"""Tests for drobo.codes: numeric-code -> label/severity decoding."""

from __future__ import annotations

from drobo import codes


def test_overall_status_known_code():
    label, severity = codes.overall_status(0x8000)
    assert label == "Healthy"
    assert severity == "ok"


def test_overall_status_unknown_code_falls_back():
    label, severity = codes.overall_status(0x1234)
    assert "Unknown" in label
    assert severity == "unknown"


def test_slot_status_known_code():
    label, severity = codes.slot_status(0x03)
    assert label == "Healthy"
    assert severity == "ok"


def test_slot_status_unknown_code_falls_back():
    label, severity = codes.slot_status(0x99)
    assert "Unknown" in label
    assert severity == "unknown"


def test_redundancy_known_and_unknown():
    assert codes.redundancy(0x7) == "Dual-drive redundancy"
    assert "Unknown" in codes.redundancy(0x99)


def test_disk_type_known_and_unknown():
    assert codes.disk_type(0) == "HDD"
    assert codes.disk_type(4) == "SSD"
    assert codes.disk_type(99) == "Type 99"


def test_disk_state_known_and_unknown():
    assert codes.disk_state(16) == "In use — data pack"
    assert codes.disk_state(999) == "State 999"


def test_rpm_from_code_ssd_and_unknown():
    rpm, label = codes.rpm_from_code(1)
    assert rpm is None
    assert "SSD" in label

    rpm, label = codes.rpm_from_code(27)
    assert rpm == 5400
    assert label == "5400 RPM"

    rpm, label = codes.rpm_from_code(0)
    assert rpm is None
    assert label == "Unknown"
