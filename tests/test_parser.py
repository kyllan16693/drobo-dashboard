"""Tests for drobo.parser: sample-document parsing + XML hardening guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from drobo.parser import _MAX_DEPTH, DroboParseError, parse

SAMPLE_XML = (Path(__file__).parent / "sample_5n.xml").read_text()


def test_parse_sample_populates_key_fields():
    status = parse(SAMPLE_XML)

    assert status.device_serial == "drb125101a00578"
    assert status.model == "Drobo 5N"
    assert status.slot_count == 6
    assert len(status.slots) == 6


def test_parse_sample_capacity_invariant():
    status = parse(SAMPLE_XML)

    assert status.used_bytes + status.free_bytes == status.total_bytes


def test_parse_rejects_doctype():
    malicious = '<?xml version="1.0"?><!DOCTYPE foo><ESATMUpdate></ESATMUpdate>'
    with pytest.raises(DroboParseError):
        parse(malicious)


def test_parse_rejects_entity():
    malicious = (
        '<?xml version="1.0"?>'
        '<!ENTITY xxe SYSTEM "file:///etc/passwd">'
        "<ESATMUpdate>&xxe;</ESATMUpdate>"
    )
    with pytest.raises(DroboParseError):
        parse(malicious)


def test_parse_rejects_excessive_nesting():
    # Build a DTD-free document whose open-tag nesting exceeds _MAX_DEPTH, so it
    # slips past the DTD guard but must still be rejected by the depth guard.
    depth = _MAX_DEPTH + 10
    nested = "<a>" * depth + "</a>" * depth
    malicious = f'<?xml version="1.0"?><ESATMUpdate>{nested}</ESATMUpdate>'
    with pytest.raises(DroboParseError):
        parse(malicious)


def test_parse_rejects_bad_root_element():
    with pytest.raises(DroboParseError):
        parse('<?xml version="1.0"?><NotADrobo></NotADrobo>')


def test_parse_rejects_malformed_xml():
    with pytest.raises(DroboParseError):
        parse("<ESATMUpdate><unclosed></ESATMUpdate>")
