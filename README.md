# AI CI/CD Log Analyzer

An end-to-end pipeline that turns failed Jenkins builds into LLM-generated
root-cause analyses, surfaces them in a Next.js dashboard, and learns from
operator feedback by storing accepted fixes in a vector index for future
similarity search.

```
Jenkins ─► jenkins_failure_listener ─► failure_analyzer_worker ─► Elasticsearch (kNN)
                                              │
                                              └─► web/backend (Postgres) ─► web/frontend (Next.js)
```

The full architecture, per-feature walkthroughs, and operational reference
live under [`docs/`](docs/README.md). New operators should follow
[`docs/getting-started.md`](docs/getting-started.md) — a linear runbook
that goes from `git clone` to a stored, accepted solution. For the
30-second internals tour read [`docs/architecture.md`](docs/architecture.md).

---

## Tech stack

| Layer        | Choice                                                                  |
| ------------ | ----------------------------------------------------------------------- |
| LLM          | Groq, model `llama-3.3-70b-versatile` (overridable via `LLM_MODEL`)     |
| Embeddings   | Sentence-Transformers `all-MiniLM-L6-v2`                                |
| Orchestration| LangGraph state machine inside the worker                               |
| APIs         | FastAPI (worker, listener, web-backend) on Uvicorn                      |
| UI           | Next.js 16 + React 19 + Tailwind 4 (App Router)                         |
| Storage      | Postgres 16 (sessions / state) + Elasticsearch 8.12 (kNN solutions)     |
| Packaging    | Docker + Docker Compose, one Dockerfile per service in `docker/`        |

---

## Quick start (Docker — recommended)

> Requires Docker Desktop with Compose v2 (Windows / macOS / Linux).

```bash
git clone <this-repo>
cd ai-cicd-log-analyzer-log-analysis-web-ss

cp .env.example .env                 # PowerShell: Copy-Item .env.example .env
# Edit .env — set at minimum:
#   GROQ_API_KEY     → from https://console.groq.com/keys
#   WORKER_API_KEY   → any shared secret
#   JENKINS_BASE_URL / JENKINS_USER / JENKINS_API_TOKEN

docker compose up -d --build
docker compose ps
```

| Surface          | URL                            | Notes                                   |
| ---------------- | ------------------------------ | --------------------------------------- |
| Web dashboard    | http://localhost:3080          | Mapped from container port 3000         |
| Web API          | http://localhost:8095/health   | FastAPI bridge for the UI               |
| Worker API       | http://localhost:8090/health   | LangGraph + Groq + Elasticsearch        |
| Listener API     | http://localhost:8088/health   | `POST /poll-once` triggers a poll       |
| Kibana           | http://localhost:5601          | Optional ES inspector                   |

Override any of the host ports via the `*_PUBLISH_PORT` variables in `.env`
— see [`docs/docker.md`](docs/docker.md).

### Optional Compose profiles

```bash
docker compose --profile poll    up -d   # headless listener-poller (no HTTP)
docker compose --profile dev-ui  up -d   # Next.js dev server with bind-mount
docker compose --profile jenkins up -d   # in-cluster Jenkins for full demos
```

---

## Local development (no Docker)

Each service is a self-contained Python or Node project with its own
`requirements.txt` / `package.json` and `.env.example`. Pick the service you
want to iterate on:

- [Worker](docs/failure-analyzer-worker.md#run-it-independently)
- [Listener](docs/jenkins-failure-listener.md#run-it-independently)
- [Web backend](docs/web-backend.md#run-it-independently)
- [Web frontend](docs/web-frontend.md#run-it-independently)

You will typically still want Postgres and Elasticsearch from Compose:

```bash
docker compose up -d postgres elasticsearch
```

---

## Tests

All Python tests are offline (no Postgres, no Elasticsearch, no LLM):

```bash
pytest --import-mode=importlib ^
       failure_analyzer_worker/tests ^
       jenkins_failure_listener/tests ^
       web/backend/tests -q
```

Frontend type-check:

```bash
cd web/frontend
npx tsc --noEmit -p tsconfig.json
```

See [`docs/testing.md`](docs/testing.md) for CI suggestions and live
end-to-end smoke tests.

---

## Repository layout

```
.
├── docker/                          # one Dockerfile per service
├── docker-compose.yml               # orchestration (default + 3 profiles)
├── .env.example                     # canonical env template for Compose
├── failure_analyzer_worker/         # LangGraph + FastAPI worker (port 8090)
├── jenkins_failure_listener/        # Jenkins → worker bridge (port 8088)
├── web/
│   ├── backend/                     # CI Analyzer Web API (port 8095)
│   └── frontend/                    # Next.js 16 dashboard (port 3000)
└── docs/                            # architecture + per-feature docs
```

A justified breakdown is in [`docs/folder-structure.md`](docs/folder-structure.md).

---

## Recent stabilization

The bugs fixed during the latest stabilization pass and the test coverage
that locks them down are documented in
[`docs/stabilization-notes.md`](docs/stabilization-notes.md).

---

## License

Internal / TBD. Add a license file before publishing.
