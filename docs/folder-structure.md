# Repository folder structure

This project keeps each independently deployable service in its own top-level
package so they can be built, tested, and released in isolation, while the
shared `docker/` and `docs/` folders provide the cross-cutting plumbing.

```
.
├── docker/                          # 4 service-specific Dockerfiles, all using context = repo root
├── docs/                            # architecture + per-feature markdown
├── failure_analyzer_worker/         # LangGraph + FastAPI worker (port 8090)
│   ├── __main__.py                  # `python -m failure_analyzer_worker`
│   ├── config.py                    # Pydantic Settings (reads .env)
│   ├── graph.py                     # LangGraph nodes + Groq client
│   ├── log_processor.py             # filtering, fingerprint, ES helpers
│   ├── worker.py                    # FastAPI app (lifespan-based startup)
│   ├── try_filter.py                # local one-off CLI for filter debugging
│   ├── tests/                       # offline pytest suite
│   ├── requirements.txt
│   └── .env.example                 # service-local example (Groq + ES)
├── jenkins_failure_listener/        # Jenkins polling bridge (port 8088)
│   ├── app/
│   │   ├── main.py                  # FastAPI app (POST /poll-once, /health)
│   │   ├── service.py               # poll-and-dispatch orchestration
│   │   ├── jenkins_client.py        # RSS + wfapi parsing, log truncation
│   │   ├── dispatcher.py            # HTTP POST to worker
│   │   ├── postgres_state_store.py  # idempotency table
│   │   ├── config.py                # Pydantic Settings
│   │   └── models.py
│   ├── run_listener.py              # headless poller (no HTTP)
│   ├── tests/                       # pure-function pytest suite
│   ├── requirements.txt
│   └── .env.example                 # service-local example (Jenkins, DB, worker URL)
├── web/
│   ├── backend/                     # CI Analyzer Web API (port 8095)
│   │   ├── app/
│   │   │   ├── main.py              # FastAPI app (sessions, chat, feedback)
│   │   │   ├── db.py                # psycopg helpers + schema bootstrap
│   │   │   └── config.py            # Pydantic Settings
│   │   ├── tests/                   # FastAPI TestClient smoke tests
│   │   ├── requirements.txt
│   │   └── .env.example
│   └── frontend/                    # Next.js 16 dashboard (port 3000)
│       ├── app/                     # App Router pages (/, /rca, /settings)
│       ├── components/              # React components (MainDashboard, RcaView, ...)
│       ├── lib/                     # shared TS utilities
│       ├── public/
│       ├── package.json
│       └── tsconfig.json
├── docker-compose.yml               # default + `poll`, `jenkins`, `dev-ui` profiles
├── docker-compose.env.example       # legacy alias of .env.example (kept for compat)
└── .env.example                     # canonical environment template for Compose
```

## Why this shape

- **One process = one folder.** Each service has its own `requirements.txt`,
  `.env.example`, and `tests/`, so a contributor can `cd` into it, build a
  venv, and iterate without touching the other services. This also matches
  how `docker/Dockerfile.<service>` is structured.
- **`/docs` at the repo root.** Documentation that spans multiple services
  lives next to the code, but separate from any single one. Each service still
  has the option to keep service-specific notes (e.g.
  [`jenkins_failure_listener/ingestion.md`](../jenkins_failure_listener/ingestion.md)).
- **`/docker` at the repo root.** All Dockerfiles use the repo root as their
  build context (so they can `COPY` shared files when needed), so they live
  outside any individual service to make that explicit.
- **No `src/` layer.** Adding `src/<service>/...` would force changing every
  import path and break the existing `docker/Dockerfile.*` `COPY` lines for
  zero functional benefit — the per-service folders already give us the
  isolation a `src/` layout aims for.
- **Tests beside their service.** Running
  `pytest failure_analyzer_worker/tests` (or any subset) does not require any
  other service’s dependencies, so CI can fan out per-service.

## Conventions

- Every Python service exposes `app.config.settings` (a Pydantic BaseSettings
  instance) and reads its `.env` via that single object.
- Every service writes to stdout with `logging.basicConfig(level=INFO)` so
  Compose / Docker captures structured logs without extra setup.
- Every FastAPI service exposes `GET /health` returning `{"status": "ok"}` so
  Compose health checks and reverse proxies can probe it uniformly.
