"""Tests for drobo.storage: BeyondRAID zone-model capacity breakdown."""

from __future__ import annotations

import time
from pathlib import Path

from drobo.history import History
from drobo.parser import parse
from drobo.storage import days_until_full, protection_reserve, storage_breakdown

SAMPLE_XML = (Path(__file__).parent / "sample_5n.xml").read_text()


def test_storage_breakdown_invariant_on_sample():
    status = parse(SAMPLE_XML)
    breakdown = storage_breakdown(status)

    # Documented invariant (AGENTS.md): protection + protected_total +
    # unallocated == raw, once accounting for the min()/max() clamping storage.py
    # already applies when deriving parity_reserve/unallocated.
    assert (
        breakdown["parity_reserve_bytes"]
        + breakdown["protected_total_bytes"]
        + breakdown["unallocated_bytes"]
        == breakdown["raw_physical_bytes"]
    )


def test_storage_breakdown_used_plus_free_equals_protected_total():
    status = parse(SAMPLE_XML)
    breakdown = storage_breakdown(status)

    assert breakdown["used_bytes"] + breakdown["free_bytes"] == breakdown["protected_total_bytes"]


def test_storage_breakdown_data_bay_count_excludes_accelerator():
    status = parse(SAMPLE_XML)
    breakdown = storage_breakdown(status)

    # Sample has 6 physical bays; slot 5 is the mSATA accelerator, not a data bay.
    assert breakdown["data_bay_count"] == 5
    assert breakdown["redundancy_level"] == 2  # mFirmwareFeatureStates == 7 (dual)


def test_protection_reserve_matched_drives():
    # Four identical 4 TB drives, dual redundancy: one zone/band spanning all
    # four disks, of which 2 disks' worth of height is spent on protection.
    cap = 4_000_000_000_000
    caps = [cap, cap, cap, cap]
    assert protection_reserve(caps, redundancy_level=2) == 2 * cap


def test_protection_reserve_single_disk_protects_nothing():
    # A single disk can never be protected (no redundancy possible).
    assert protection_reserve([4_000_000_000_000], redundancy_level=2) == 0


def test_protection_reserve_mismatched_drives_zone_model():
    # 8, 8, 4, 4, 3 TB dual-redundancy pack (this unit, roughly) — bands below
    # the smallest drive include all 5 disks (protectable), the next band up to
    # the 4 TB drives includes 4 disks (still protectable), and the top band up
    # to the 8 TB drives includes only 2 disks (<= redundancy_level, so that
    # band contributes nothing to protection).
    caps = [
        8_000_000_000_000,
        8_000_000_000_000,
        4_000_000_000_000,
        4_000_000_000_000,
        3_000_000_000_000,
    ]
    protection = protection_reserve(caps, redundancy_level=2)

    band1 = 3_000_000_000_000  # 0 -> 3TB, 5 disks active
    band2 = 4_000_000_000_000 - 3_000_000_000_000  # 3TB -> 4TB, 4 disks active
    # band 4TB -> 8TB has only 2 disks active == redundancy_level, contributes 0
    expected = 2 * band1 + 2 * band2
    assert protection == expected


def test_days_until_full_no_history_returns_none(tmp_path):
    h = History(tmp_path / "history.db")
    assert days_until_full(h, free_bytes=1000) is None


def test_days_until_full_flat_usage_returns_none(tmp_path, monkeypatch):
    h = History(tmp_path / "history.db")
    day0, day1, day2 = 0.0, 86400.0, 2 * 86400.0
    h.record_capacity(used=100, free=900, total=1000, ts=day0)
    h.record_capacity(used=100, free=900, total=1000, ts=day1)
    h.record_capacity(used=100, free=900, total=1000, ts=day2)
    monkeypatch.setattr(time, "time", lambda: day2 + 86400.0)

    assert days_until_full(h, free_bytes=900) is None


def test_days_until_full_shrinking_usage_returns_none(tmp_path, monkeypatch):
    h = History(tmp_path / "history.db")
    day0, day1, day2 = 0.0, 86400.0, 2 * 86400.0
    h.record_capacity(used=200, free=800, total=1000, ts=day0)
    h.record_capacity(used=150, free=850, total=1000, ts=day1)
    h.record_capacity(used=100, free=900, total=1000, ts=day2)
    monkeypatch.setattr(time, "time", lambda: day2 + 86400.0)

    # Net-freed over the lookback window -> never fabricate a countdown.
    assert days_until_full(h, free_bytes=900) is None


def test_days_until_full_positive_growth_matches_hand_computed_average(tmp_path, monkeypatch):
    h = History(tmp_path / "history.db")
    day0, day1, day2 = 0.0, 86400.0, 2 * 86400.0
    h.record_capacity(used=100, free=900, total=1000, ts=day0)
    h.record_capacity(used=200, free=800, total=1000, ts=day1)
    h.record_capacity(used=300, free=700, total=1000, ts=day2)
    monkeypatch.setattr(time, "time", lambda: day2 + 86400.0)

    # daily_written yields two 100-byte/day deltas -> avg 100 bytes/day.
    result = days_until_full(h, free_bytes=700, lookback_days=30)
    assert result == 700 / 100.0


def test_days_until_full_uses_however_many_days_exist(tmp_path, monkeypatch):
    # Early in the app's life there may be far fewer samples than
    # lookback_days; the average should still be computed over what exists
    # rather than requiring the full window.
    h = History(tmp_path / "history.db")
    day0, day1 = 0.0, 86400.0
    h.record_capacity(used=100, free=900, total=1000, ts=day0)
    h.record_capacity(used=150, free=850, total=1000, ts=day1)
    monkeypatch.setattr(time, "time", lambda: day1 + 86400.0)

    result = days_until_full(h, free_bytes=850, lookback_days=14)
    assert result == 850 / 50.0
