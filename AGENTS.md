# AGENTS.md — working on the Drobo 5N dashboard

Orientation for AI agents and new contributors. Read this + [`docs/DROBO-5N.md`](docs/DROBO-5N.md)
before making changes.

## What this is

A Flask app that monitors a network-only **Drobo 5N** (`192.168.1.144`). Data
comes from the device's unauthenticated **NASD XML stream (TCP 5000)** and,
for CPU/RAM/throughput, from **SSH into the box** (`admin` account, reading
`/proc`). See the README for features and the full route/module map.

## Run & poke

```sh
cd drobo-dashboard
uv run app.py                 # serves http://127.0.0.1:8765
curl -s localhost:8765/api/status  | head       # parsed snapshot
curl -s localhost:8765/api/storage | head       # capacity + breakdown
curl -s localhost:8765/api/hardware| head        # live SSH telemetry
curl -s localhost:8765/api/widget | head       # flat JSON for Homepage customapi
curl -s localhost:8765/healthz                    # 200 fresh / 503 stale
```

- Config is env / `.env` (auto-loaded). See the table in the README.
- **Production:** CT407 on homelab1 at `http://192.168.1.155:8765` — see `deploy/README.md`
  and [`deploy/homepage/`](deploy/homepage/) for Docker + Homepage widget setup.
- **Do not enable `DROBO_ENABLE_CONTROL=1` in prod** without explicit approval (identify/restart
  touch the physical NAS).
- Offline work: parse `tests/sample_5n.xml` with `drobo.parse()` — no device
  needed.

## Where things live

- **Parsing core** (`drobo/client.py`, `parser.py`, `models.py`, `codes.py`) —
  pure stdlib, reusable, no Flask. Change label/code mappings in `codes.py`.
- **Capacity math** — `drobo/storage.py` (BeyondRAID zone model; see §8 of the
  knowledge base). Preserve its invariants: `used + free == protected_total`
  and `protection + protected_total + unallocated == raw`.
- **Telemetry** — `drobo/throughput.py` + `drobo/hardware.py` (SSH `/proc`).
  Must **degrade gracefully** when SSH creds are missing/wrong (report a state,
  don't crash).
- **Persistence** — `drobo/history.py` (SQLite in `data/`, git-ignored).
- **Control** — `drobo/control.py` (DIRNETTM, port 5001). Only safe/reversible
  actions; gated behind `DROBO_ENABLE_CONTROL` + CSRF. **Keep control disabled in
  prod** (`DROBO_ENABLE_CONTROL=0`) unless the user explicitly asks.
- **Homepage widget** — `GET /api/widget` returns flat JSON for gethomepage
  `customapi` (unauthenticated, LAN-only read-only telemetry). Example config in
  `deploy/homepage/widget.example.yaml`.
- **Frontend** — one `templates/*.html` + `static/*.{js,css}` per page, sharing
  `base.css` and the dependency-free `charts.js`.

## Non-negotiables

- **Never print, log, echo, or commit secrets.** Reference `DROBO_PASSWORD` /
  `DROBO_USERNAME` by name only. `.env`, `data/`, and `*.db*` are git-ignored —
  keep them that way.
- **HTML-escape everything device-derived** in the frontend (XSS). All existing
  render paths do this — match them.
- **Reject DTDs** when parsing XML (XML-bomb DoS). Don't loosen `parser.py`.
- **Sanitize query params** on history endpoints (finite/positive/bounded) —
  see `_req_hours` / `_req_int` in `app.py`.
- **Don't fake data.** Temperature and per-drive SMART are genuinely
  unavailable on this device (knowledge base §7) — show "not reported", never
  invent values.

## Do NOT do without asking (device safety)

These touch the physical NAS holding real data:

- Any **write/format/redundancy-change** on the Drobo, or expanding
  `control.py` beyond identify/restart.
- **Installing anything on the Drobo** (DroboApps, smartctl, cron, services) —
  researched and declined; see `docs/RESEARCH-extraction.md`.
- **Restarting the Drobo mid-relayout** (guard already exists — don't bypass).
- Pulling/reordering drives, or anything in the parent repo's
  [`../.cursor/rules/homelab.mdc`](../.cursor/rules/homelab.mdc) denylist
  (backups, network/firewall, credentials).

## Before you finish

- Lint touched Python: `uv run ruff check . && uv run ruff format --check .`
- Typecheck: `uv run mypy`
- If you changed parsing or capacity math, validate against
  `tests/sample_5n.xml` and re-check the invariants above.
- If you changed device facts, update [`docs/DROBO-5N.md`](docs/DROBO-5N.md).
