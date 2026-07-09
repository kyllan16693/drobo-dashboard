"""Tests for drobo.rawdump: full field dump + XML hardening guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from drobo.rawdump import _MAX_DEPTH, RawDumpError, raw_dump

SAMPLE_XML = (Path(__file__).parent / "sample_5n.xml").read_text()


def test_raw_dump_returns_dict_with_expected_shape():
    dump = raw_dump(SAMPLE_XML)

    assert isinstance(dump, dict)
    assert dump["mSerial"] == "drb125101a00578"
    assert dump["mModel"] == "Drobo 5N"
    # mSlotsExp's nX children collapse to an ordered list.
    assert isinstance(dump["mSlotsExp"], list)
    assert len(dump["mSlotsExp"]) == 6
    assert dump["mSlotsExp"][0]["mSlotNumber"] == "0"


def test_raw_dump_rejects_doctype():
    malicious = '<?xml version="1.0"?><!DOCTYPE foo><ESATMUpdate></ESATMUpdate>'
    with pytest.raises(RawDumpError):
        raw_dump(malicious)


def test_raw_dump_rejects_entity():
    malicious = (
        '<?xml version="1.0"?>'
        '<!ENTITY xxe SYSTEM "file:///etc/passwd">'
        "<ESATMUpdate>&xxe;</ESATMUpdate>"
    )
    with pytest.raises(RawDumpError):
        raw_dump(malicious)


def test_raw_dump_rejects_excessive_nesting():
    depth = _MAX_DEPTH + 10
    nested = "<a>" * depth + "</a>" * depth
    malicious = f'<?xml version="1.0"?><ESATMUpdate>{nested}</ESATMUpdate>'
    with pytest.raises(RawDumpError):
        raw_dump(malicious)


def test_raw_dump_rejects_bad_root_element():
    with pytest.raises(RawDumpError):
        raw_dump('<?xml version="1.0"?><NotADrobo></NotADrobo>')
