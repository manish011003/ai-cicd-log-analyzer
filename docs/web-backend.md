# Feature: web/backend

A small FastAPI service (`web/backend/app/main.py`) that mediates between the
Next.js dashboard, the analyzer worker, and the Jenkins listener. It owns the
session, chat, and feedback tables in Postgres and exposes a REST API the
frontend can consume directly.

## What it does

- Persists every analysis the worker produces as a “session” keyed by UUID.
- Stores per-session chat history so the conversational assistant survives
  page reloads.
- Records accept / reject feedback on each suggested fix and forwards
  accepted fixes to the worker’s `/store-solution` endpoint so they become
  Elasticsearch kNN training data.
- Acts as a thin proxy in front of the listener’s `/poll-once` so the
  dashboard never needs CORS access to the listener directly.
- Serves a structured JSON `/health` for Compose health checks.

## Logic flow

```
Worker  ──POST /api/sessions──►  web-backend  ──INSERT──►  Postgres (sessions)
                                       │
Browser ──GET  /api/sessions/{id}─────►│
                                       │
Browser ──POST /api/sessions/{id}/chat ►│  ──POST /chat/turn──► worker
                                       │
Browser ──POST /api/sessions/{id}/feedback (decision=accept)
                                       │
                                       └──POST /store-solution──► worker → ES
```

## Public HTTP API (selected)

| Method | Path                                       | Purpose                                       |
| ------ | ------------------------------------------ | --------------------------------------------- |
| GET    | `/health`                                  | Liveness probe                                |
| POST   | `/api/sessions`                            | Worker creates a new analysis session         |
| GET    | `/api/sessions`                            | Dashboard list view                           |
| GET    | `/api/sessions/{id}`                       | Single-session detail + history               |
| POST   | `/api/sessions/{id}/chat`                  | Append a user message + LLM reply             |
| POST   | `/api/sessions/{id}/feedback`              | `{ "decision": "accept" \| "reject" }`        |
| GET    | `/api/filter-config`                       | Cached proxy of the worker's `/filter-config` (powers the Settings page; 60s TTL) |
| POST   | `/api/listener/poll-once`                  | Proxy to the listener’s `/poll-once`          |

The full surface area is in `web/backend/app/main.py`.

### Structural-filter telemetry

Sessions carry a `filter_meta` JSONB column (added by an additive
migration in `db.py`). `POST /api/sessions` accepts the worker's
`filter_meta` dict and `GET /api/results` returns a trimmed projection
(confidence, detected stack, primary location, compression stats) so
the dashboard can render a *Detected* panel on each failure card. See
[`filtering.md`](filtering.md) for the schema.

## Environment

Defined in `web/backend/.env.example`. The variables that matter most:

| Variable             | Default                              | Notes                                                          |
| -------------------- | ------------------------------------ | -------------------------------------------------------------- |
| `DATABASE_URL`       | (required)                           | Same Postgres instance as the listener                         |
| `WORKER_BASE_URL`    | `http://worker:8090`                 | Where to forward feedback / chat                               |
| `WORKER_API_KEY`     | (required, must match worker)        | Sent as `X-API-Key`                                            |
| `LISTENER_BASE_URL`  | `http://listener:8088`               | Where to proxy `/poll-once`                                    |
| `CORS_ORIGINS`       | `*`                                  | Comma-separated; pin in production (e.g. `https://ci.example.com`) |

## Run it independently

### 1. Local Python

```bash
cd web/backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env            # then edit DATABASE_URL, WORKER_API_KEY, ...
uvicorn app.main:app --reload --host 0.0.0.0 --port 8095
```

You need a reachable Postgres. The simplest path is to start only the
`postgres` service from Compose: `docker compose up -d postgres`.

### 2. Docker

```bash
docker compose up -d --build web-backend
curl http://localhost:8095/health
```

## Test it

Offline pytest suite — no Postgres, no worker, no network — exercises the
API helpers and the `/health` endpoint:

```bash
pytest web/backend/tests -q
```

For a wired-up smoke test, hit the live endpoint after `docker compose up`:

```bash
curl http://localhost:8095/api/sessions | jq .
```
