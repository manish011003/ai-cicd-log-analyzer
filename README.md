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
live under [`docs/`](docs/README.md). Start with
[`docs/architecture.md`](docs/architecture.md) for the 30-second tour.

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

## Prerequisites

| Tool                    | Minimum version | Notes                                                       |
| ----------------------- | --------------- | ----------------------------------------------------------- |
| Docker Engine + Compose | 24+ / v2        | Compose v2 ships with Docker Desktop on Windows / macOS     |
| Git                     | 2.30+           | Long-path support is helpful on Windows (`core.longpaths`)  |
| Python                  | 3.11            | Only for running Python services outside Docker             |
| Node.js                 | 20 LTS          | Only for running the frontend outside Docker                |
| Free RAM                | ~6 GB           | Elasticsearch alone reserves a 1 GB JVM heap                |
| Free disk               | ~5 GB           | Container images + Postgres data + ES indices               |

A reachable Jenkins instance is required to ingest real failures. For
local-only experiments without Jenkins, start the in-cluster Jenkins via the
`jenkins` Compose profile (see below) or replay sessions through the web UI.

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

### Required environment variables

The full list lives in [`.env.example`](.env.example) (heavily commented).
At minimum, fill these in before bringing the stack up:

| Variable             | Purpose                                                              | How to get it                                  |
| -------------------- | -------------------------------------------------------------------- | ---------------------------------------------- |
| `WORKER_API_KEY`     | Shared secret between listener, worker, and web-backend              | Any random string (e.g. `openssl rand -hex 24`) |
| `LLM_API_KEY` / `GROQ_API_KEY` | Auth token for the active LLM provider                     | https://console.groq.com/keys for Groq         |
| `JENKINS_BASE_URL`   | Jenkins root reachable from the worker / listener containers         | `http://host.docker.internal:8080` on Win/mac  |
| `JENKINS_USER`       | Jenkins username with read access to failed builds                   | Your Jenkins login                             |
| `JENKINS_API_TOKEN`  | Jenkins API token (not the user password)                            | Jenkins → People → you → Configure → API Token |

Useful optional knobs:

- `LLM_PROVIDER` / `LLM_MODEL` — swap providers (`groq`, `openai`, `anthropic`, `ollama`).
- `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` — swap embeddings (defaults are fully local).
- `LOG_BODY_MAX_TOKENS`, `FILTER_DETECTORS` — control the structural log filter that compresses each failed-stage excerpt before it reaches the LLM. Full reference in [`docs/filtering.md`](docs/filtering.md); live values are visible at **Settings** in the dashboard.
- `*_PUBLISH_PORT` — remap host ports if defaults clash.
- `WEB_UI_PUBLIC_URL` — deep-link printed in worker logs.
- `SESSION_RETENTION_DAYS`, `MESSAGE_RETENTION_DAYS` — Postgres janitor TTLs.

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

## Common development tasks

| Task                                  | Command                                                                 |
| ------------------------------------- | ----------------------------------------------------------------------- |
| Tail one service's logs               | `docker compose logs -f failure_analyzer_worker`                        |
| Restart a single service              | `docker compose restart web_backend`                                    |
| Rebuild after editing a Dockerfile    | `docker compose up -d --build worker`                                   |
| Open a shell inside a container       | `docker compose exec failure_analyzer_worker bash`                      |
| Trigger a one-shot Jenkins poll       | `curl -X POST http://localhost:8088/poll-once`                          |
| Check every service is healthy        | `for p in 8088 8090 8095; do curl -s localhost:$p/health; echo; done`   |
| Wipe and re-create Postgres data      | `docker compose down -v postgres && docker compose up -d postgres`      |
| Reset the Elasticsearch kNN index     | `curl -XDELETE http://localhost:9200/failure_solutions`                 |
| Front-end hot reload (dev profile)    | `docker compose --profile dev-ui up -d web_frontend_dev`                |
| Reinstall frontend deps locally       | `cd web/frontend && npm install`                                        |
| Run frontend lint                     | `cd web/frontend && npm run lint`                                       |
| Run frontend type-check               | `cd web/frontend && npx tsc --noEmit -p tsconfig.json`                  |

> Heads-up: `docker compose down -v` removes named volumes — you will lose all
> Postgres sessions and ES indices. Use it intentionally.

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

## Troubleshooting

| Symptom                                                                 | Likely cause & fix                                                                                                                                                                  |
| ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docker compose up` fails: port already in use                          | Set the matching `*_PUBLISH_PORT` in `.env` (e.g. `WEB_FRONTEND_PUBLISH_PORT=3081`) and rerun.                                                                                       |
| Worker logs `LLM_API_KEY missing` or `401 Unauthorized` from Groq       | `.env` not picked up. Re-run `docker compose up -d --build worker` after editing, or `docker compose config` to confirm the value is being injected.                                |
| Listener can't reach Jenkins (`Connection refused` / `getaddrinfo`)     | From inside containers, `localhost` is the container itself. Use `http://host.docker.internal:8080` on Windows / macOS, or your LAN IP on Linux.                                    |
| Elasticsearch container restarts forever                                | Linux requires `vm.max_map_count >= 262144`. Run `sudo sysctl -w vm.max_map_count=262144` (and persist via `/etc/sysctl.conf`).                                                     |
| `failed to load preset image ... sentence-transformers`                 | First worker boot downloads the embedding model. It can take a couple of minutes — watch progress with `docker compose logs -f failure_analyzer_worker`.                            |
| Web UI loads but every API call is CORS-blocked                         | Set `CORS_ORIGINS` in `.env` to the exact origin you're loading the UI from (e.g. `http://localhost:3080`) and restart `web_backend`.                                               |
| Postgres connection errors on first boot                                | The web-backend may start before Postgres finishes initialising. `docker compose restart web_backend` after a few seconds.                                                          |
| Sessions disappear after a few months                                   | The Postgres janitor is sweeping past `SESSION_RETENTION_DAYS` / `MESSAGE_RETENTION_DAYS`. Bump those in `.env`. Accepted solutions remain in Elasticsearch (separate TTL).          |
| `pytest` complains about Postgres/ES not running                        | The Python test suites are intentionally offline — make sure you're invoking them with `--import-mode=importlib` (see [`docs/testing.md`](docs/testing.md)).                        |
| Jenkins API token rejected                                              | Tokens, not passwords. Generate at *Jenkins → People → \<you\> → Configure → API Token → Add new token*.                                                                            |

Still stuck? Skim the per-service docs under [`docs/`](docs/README.md) — each
one has a *Run it independently* section with the exact env vars and ports
that service expects.

---

## Contributing

We keep the repo small and the change cycle tight. Before opening a PR:

1. **Branch off `main`** with a descriptive prefix:
   `feat/<short-name>`, `fix/<short-name>`, `chore/<short-name>`,
   `docs/<short-name>`. Avoid long-lived branches.
2. **Keep commits focused.** Each commit should compile, pass tests, and
   describe *why* (not just *what*) in the subject + body. Sign-off is
   not required.
3. **Run the relevant tests locally** (see the [Tests](#tests) section).
   At a minimum, run the suite for any module you touched plus the frontend
   type-check if you edited TypeScript.
4. **Lint the frontend** (`cd web/frontend && npm run lint`) when you touch
   `.ts` / `.tsx` files.
5. **Update the docs.** If you change a public API, env var, port, or compose
   service, update both [`.env.example`](.env.example) and the matching
   page under [`docs/`](docs/README.md). README only needs to change for
   onboarding-level differences.
6. **No secrets.** `.env`, real tokens, customer logs, and screenshots with
   credentials never go into commits. The repo's `.gitignore` covers `.env`
   already — keep it that way.
7. **Open the PR against `main`** with a short summary, screenshots for UI
   work, and a *Test plan* checklist of what you actually exercised.

### Code style

- **Python**: 4-space indent, type hints on new public functions, no
  unused imports. Match the surrounding service's style.
- **TypeScript / React**: follow the rules enforced by `next lint`. Prefer
  server components; reach for client components only when interactivity or
  browser APIs are needed.
- **Shell / Compose**: keep `.env.example` the source of truth. Every new
  variable lands there with a one-line comment explaining its purpose.

---

## Where to read next

- New to the codebase? → [`docs/architecture.md`](docs/architecture.md)
- Working on the worker? → [`docs/failure-analyzer-worker.md`](docs/failure-analyzer-worker.md)
- Tuning log compression / detectors? → [`docs/filtering.md`](docs/filtering.md)
- Working on ingestion? → [`docs/jenkins-failure-listener.md`](docs/jenkins-failure-listener.md)
- Working on the API? → [`docs/web-backend.md`](docs/web-backend.md)
- Working on the UI? → [`docs/web-frontend.md`](docs/web-frontend.md)
- Tweaking Compose / Docker? → [`docs/docker.md`](docs/docker.md)

---

## License

Internal / TBD. Add a license file before publishing.
