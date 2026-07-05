# Production deploy (Docker)

## Prerequisites

- Docker with Compose v2
- Network access to the Drobo NASD port (TCP 5000) and optionally SSH (22)

## First-time setup

```sh
git clone git@github.com:kyllan16693/drobo-dashboard.git
cd drobo-dashboard
cp deploy/.env.example .env
# Edit .env — set DROBO_HOST, DROBO_PASSWORD, and any host-specific values.
mkdir -p data
chmod 777 data   # Linux: app runs as non-root; host dir must be writable
cd deploy
docker compose up --build -d
```

The compose file bind-mounts `../data` so SQLite history survives container
recreates. Environment is loaded from `../.env`. Published port follows
`WEB_PORT` in `.env` (default 8765).

## Verify

```sh
curl -s http://localhost:8765/healthz
curl -s http://localhost:8765/api/widget
```

## Updates

```sh
cd /opt/drobo-dashboard   # or your clone path
git pull
cd deploy
docker compose up -d --build
```

`.env` and `data/` persist across pulls — do not overwrite them.

## Secrets policy

| File | In git? | Created how |
|---|---|---|
| `.env` | **No** | `cp deploy/.env.example .env` then edit by hand |
| `data/` | **No** | auto-created on first run; bind-mounted |
| `deploy/.env.example` | Yes | template only, no secrets |

Before pushing to GitHub, confirm no secrets are tracked:

```sh
git ls-files | grep -E '\.env$|\.db|/data/' && echo "STOP: sensitive files tracked" || echo "OK"
```

## Homepage widget

See [`homepage/README.md`](homepage/README.md) for the gethomepage customapi
snippet and CT 402 integration steps.
