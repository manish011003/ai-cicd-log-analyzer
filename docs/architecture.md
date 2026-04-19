# Architecture overview

This repository contains four cooperating services that turn a failed Jenkins
build into an LLM-generated root-cause analysis served back through a Next.js
dashboard.

## Service map

```
                        ┌────────────────────────┐
                        │        Jenkins         │
                        │  (rssFailed + wfapi)   │
                        └──────────┬─────────────┘
                                   │ poll
                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│  jenkins_failure_listener     (FastAPI + run_listener.py)          │
│  - polls /rssFailed                                                │
│  - resolves failed stages with /wfapi/describe + /wfapi/log        │
│  - dedupes with Postgres state store                               │
│  - POSTs normalized batch → worker /ingest/failure                 │
└──────────────────────────┬─────────────────────────────────────────┘
                           │ HTTP (X-API-Key)
                           ▼
┌────────────────────────────────────────────────────────────────────┐
│  failure_analyzer_worker      (FastAPI + LangGraph)                │
│  - filter_logs() / fingerprint() pipeline                          │
│  - kNN search of past solutions in Elasticsearch                   │
│  - Groq LLM (llama-3.3-70b-versatile) generates RCA + fix          │
│  - posts a “session” to web-backend so the UI gets a deep-link     │
└─────────────┬──────────────────────────────────┬────────────────────┘
              │ kNN store                        │ POST /api/sessions
              ▼                                  ▼
   ┌──────────────────────┐         ┌────────────────────────────────┐
   │   Elasticsearch      │         │  web/backend (FastAPI)          │
   │  failure_solutions   │◀────────│  - sessions, chat, feedback     │
   │  failure_context     │         │  - proxies /poll-once → listener│
   └──────────────────────┘         │  - reads/writes Postgres         │
                                    └──────────────┬─────────────────┘
                                                   │ HTTP (JSON)
                                                   ▼
                                    ┌────────────────────────────────┐
                                    │  web/frontend (Next.js 16)      │
                                    │  - dashboard, chat, feedback    │
                                    └────────────────────────────────┘
```

## Data stores

| Store          | Used by                        | What it holds                                                |
| -------------- | ------------------------------ | ------------------------------------------------------------ |
| Postgres       | listener + web-backend         | processed-build state, web sessions, chat history, feedback  |
| Elasticsearch  | worker                         | `failure_solutions` (kNN) + `failure_context` (filtered logs) |

Both are provisioned automatically by `docker-compose.yml`. Kibana is included
for ad-hoc inspection of the Elasticsearch indices on `http://localhost:5601`.

## Request lifecycle (happy path)

1. Jenkins build #N of `team/web-build` fails.
2. Listener’s next `/rssFailed` poll picks it up; it has not been seen before.
3. Listener resolves the failed stage(s) via `wfapi/describe` and pulls the
   stage log via `wfapi/log` (HTML stripped, time-prefixes removed,
   noise-filtered, head/tail truncated).
4. Listener POSTs `{ "failures": [ { event_type:"stage_failure", ... } ] }`
   to `http://worker:8090/ingest/failure` with `X-API-Key`.
5. Worker:
   - Runs `LogProcessor.process()` to produce a filtered excerpt + metadata
     summary.
   - Calls `generate_fingerprint()` to form a stable, semantic key.
   - Searches Elasticsearch for similar fingerprints (cosine kNN).
   - Asks Groq for an analysis + suggested fix (LangGraph state machine).
   - POSTs a new “session” row to the web-backend so the user can land on a
     stable URL like `http://localhost:3080/?session=<uuid>`.
6. Web-backend persists everything in Postgres and exposes it under
   `/api/sessions/...`.
7. Web-frontend (Next.js) renders the dashboard, lets the user chat, accept,
   or reject the suggested fix; an accepted fix is sent back to the worker
   `/store-solution` endpoint and indexed in Elasticsearch for future kNN
   matches.

## Repository layout

```
.
├── docker/                         # one Dockerfile per service
├── docker-compose.yml              # full-stack orchestration (default + profiles)
├── .env.example                    # copy to .env before `docker compose up`
├── failure_analyzer_worker/        # LangGraph + FastAPI worker
│   ├── tests/                      # offline pytest suite (no ES, no LLM)
│   └── ...
├── jenkins_failure_listener/       # Jenkins → worker bridge
│   ├── app/                        # FastAPI app
│   ├── run_listener.py             # headless polling loop (no HTTP)
│   ├── tests/                      # pure-function pytest suite
│   └── ingestion.md                # legacy doc kept for context
├── web/
│   ├── backend/                    # FastAPI bridge between UI and worker/listener
│   │   ├── app/
│   │   └── tests/                  # FastAPI TestClient smoke tests
│   └── frontend/                   # Next.js 16 dashboard (App Router)
└── docs/                           # this folder — feature & ops docs
```

See [`folder-structure.md`](folder-structure.md) for the full proposed
hierarchy and a brief justification for each top-level directory.
