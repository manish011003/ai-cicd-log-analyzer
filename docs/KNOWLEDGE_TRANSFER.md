# Knowledge Transfer — AI CI/CD Log Analyzer (Stage Detection)

> One-stop onboarding doc for any developer taking over this project. It covers
> **what the system does**, **how to set it up**, the **high-level design (HLD)**,
> **all HTTP endpoints**, the **data model**, and the **technical depths** of each
> service. Read this top-to-bottom once; afterwards use it as a reference.

For deeper per-feature docs see [`docs/README.md`](README.md) (index),
[`architecture.md`](architecture.md), and [`technical_deets.md`](technical_deets.md).

---

## 1. What this system does (in one paragraph)

When a Jenkins build **fails**, this system automatically: (1) detects the failed
build and the **root-cause stage**, (2) pulls and cleans the stage logs, (3) uses
an **LLM** to produce a root-cause analysis and a step-by-step suggested fix,
(4) checks **Elasticsearch** for similar past failures (so previously accepted
fixes are reused), and (5) surfaces everything in a **Next.js dashboard** where an
engineer can chat with the analysis and **accept/reject** the fix. Accepted fixes
are stored back into Elasticsearch as kNN training data, so the system gets
smarter over time.

**Pipeline:** `Jenkins → Listener → Worker (filter + LLM + kNN) → Web Backend → Next.js UI`,
with **Postgres** for state/sessions and **Elasticsearch** for accepted-solution
vector search.

> **Important:** There is **no message broker** (no Redis / Kafka / RabbitMQ /
> Celery). All inter-service communication is **synchronous HTTP**; durable state
> lives in **Postgres** and **Elasticsearch**.

---

## 2. High-Level Design (HLD)

```
                        ┌────────────────────────┐
                        │        Jenkins         │
                        │  /rssFailed + wfapi    │
                        └───────────┬────────────┘
                                    │ HTTP poll (RSS + wfapi logs)
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  jenkins_failure_listener   (FastAPI :8088  /  run_listener.py headless)   │
│  • Poll RSS → find failed builds                                           │
│  • Resolve failed root-cause stage via wfapi (parallel-block aware)        │
│  • Dedupe via Postgres `jenkins_failure_events`                            │
│  • POST {failures:[...]} → worker /ingest/failure  (X-Api-Key)             │
└──────────────────────────────┬─────────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  failure_analyzer_worker   (FastAPI :8090  +  LangGraph)                   │
│  • UniversalFilter: 4-pass structural log filter + language detectors      │
│  • Fingerprint the failure                                                 │
│  • ES kNN search for similar accepted fixes                                │
│  • LangGraph: route → analyze_with_context | analyze_fresh → LLM (Groq)    │
│  • POST result → web-backend /api/sessions                                 │
└───────────────┬───────────────────────────────────┬───────────────────────┘
                │ kNN search / store                 │ HTTP
                ▼                                     ▼
   ┌──────────────────────────┐        ┌────────────────────────────────────┐
   │     Elasticsearch         │        │  web/backend  (FastAPI :8095)       │
   │  index: failure_solutions │◀───────│  sessions • chat • feedback • stats │
   │  dense_vector kNN (cosine)│        │  Postgres: analysis_sessions +      │
   └──────────────────────────┘        │            session_messages          │
                                        └──────────────────┬───────────────────┘
                                                           │ Next.js rewrites /api/*
                                                           ▼
                                        ┌────────────────────────────────────┐
                                        │  web/frontend  (Next.js 16 :3000)   │
                                        │  /  •  /rca  •  /settings           │
                                        └────────────────────────────────────┘
```

### 2.1 Component responsibilities

| Component | Compose service | Container port | Role |
|-----------|-----------------|----------------|------|
| **Jenkins failure listener** | `listener` (API) / `listener-poller` (headless) | 8088 | Poll Jenkins, detect failed root-cause stage, normalize + dedupe, dispatch to worker |
| **Failure analyzer worker** | `worker` | 8090 | Filter logs, fingerprint, ES kNN, LLM root-cause + fix, create web session |
| **Web backend** | `web-backend` | 8095 | Persist sessions/messages in Postgres, proxy chat/feedback to worker, dashboard APIs |
| **Web frontend** | `web-frontend` | 3000 (host 3080) | Dashboard (`/`), RCA drill-in (`/rca`), Settings (`/settings`), chat + feedback UI |
| **Postgres** | `postgres` | 5432 | Listener dedup state + web sessions/messages (DB `jenkins_listener`) |
| **Elasticsearch** | `elasticsearch` | 9200 | Accepted-solution kNN index `failure_solutions` |
| **Kibana** *(optional)* | `kibana` | 5601 | Inspect ES indices |

### 2.2 End-to-end request lifecycle (happy path)

1. A Jenkins build fails → appears in `/rssFailed`.
2. Listener polls, filters builds newer than the last processed per job, and **dedupes** against `jenkins_failure_events`.
3. Listener resolves the **failed root-cause stage** via `wfapi/describe`, fetches the stage log (HTML stripped, truncated), and assembles a `FailureEvent`.
4. Listener `POST`s a batch to `http://worker:8090/ingest/failure` with header `X-Api-Key: <WORKER_API_KEY>`.
5. Worker runs the **4-pass structural filter**, generates a **fingerprint**, and runs an **ES kNN search** for similar past fixes.
6. LangGraph routes: strong match (`score ≥ SIMILARITY_THRESHOLD`, default 0.75) → `analyze_with_context`; otherwise → `analyze_fresh`. Either way the **LLM** (Groq by default) produces analysis + suggested fix.
7. Worker `POST`s the result to `http://web-backend:8095/api/sessions` → a row in `analysis_sessions`.
8. Engineer opens the dashboard, drills into `/rca?run=<run_id>`, optionally **chats** (UI → web-backend `/agent/chat` → worker `/chat/turn`).
9. Engineer **accepts** the fix → web-backend calls worker `/store-solution` → the fix is indexed into ES for future kNN reuse.

---

## 3. Setup & run

### 3.1 Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Docker Engine + Compose v2 | 24+ | Recommended path |
| Python | 3.11+ (3.12 in Docker) | Only for local non-Docker service dev |
| Node.js | 20 LTS (22 in Docker) | Only for local frontend dev |
| RAM | ~6 GB free | Elasticsearch reserves a 512 MB JVM heap |
| Disk | ~5 GB | Images + Postgres + ES indices |
| Groq API key | — | Default LLM provider (`GROQ_API_KEY`) |
| Jenkins | reachable + read-only user/token | For real failure ingestion |

### 3.2 Quick start (Docker — recommended)

```bash
git clone <repo>
cd ai-cicd-log-analyzer-stage-detection

# PowerShell:  Copy-Item .env.example .env
cp .env.example .env
# Edit .env — minimum: WORKER_API_KEY, GROQ_API_KEY (or LLM_API_KEY), JENKINS_*

docker compose up -d --build
docker compose ps
```

> A deprecated alias `docker-compose.env.example` still mirrors `.env.example`;
> prefer `.env.example`.

### 3.3 Default URLs & ports (host → container)

| Surface | URL | Service | Host port | Container port | Override env var |
|---------|-----|---------|-----------|----------------|------------------|
| Web dashboard | http://localhost:3080 | `web-frontend` | **3080** | 3000 | `WEB_FRONTEND_PUBLISH_PORT` |
| Web API | http://localhost:8095/health | `web-backend` | **8095** | 8095 | `WEB_BACKEND_PUBLISH_PORT` |
| Worker API | http://localhost:8090/health | `worker` | **8090** | 8090 | `WORKER_PUBLISH_PORT` |
| Listener API | http://localhost:8088/health | `listener` | **8088** | 8088 | `LISTENER_PUBLISH_PORT` |
| Postgres | localhost:5432 | `postgres` | **5432** | 5432 | `POSTGRES_PUBLISH_PORT` |
| Elasticsearch | http://localhost:9200 | `elasticsearch` | **9200** | 9200 | `ELASTICSEARCH_PUBLISH_PORT` |
| Kibana | http://localhost:5601 | `kibana` | **5601** | 5601 | `KIBANA_PUBLISH_PORT` |
| Dev UI *(profile)* | http://localhost:5173 | `web-frontend-dev` | **5173** | 5173 | — |
| Jenkins *(profile)* | http://localhost:8080 | `jenkins` | **8080** | 8080 | — |

> The frontend host port is **3080** (not 3000) on purpose — Windows/Hyper-V often
> reserves ports 2971–3070.

### 3.4 Compose profiles

```bash
docker compose --profile poll    up -d   # headless listener-poller (continuous, no HTTP)
docker compose --profile dev-ui  up -d   # Next.js dev server with bind-mount (port 5173)
docker compose --profile jenkins up -d   # in-cluster Jenkins (image: my-jenkins:2.541.1)
```

### 3.5 Infra defaults (from `docker-compose.yml`)

| Service | Image | Container name | Config |
|---------|-------|----------------|--------|
| `postgres` | `postgres:16-alpine` | `jenkins-listener-postgres` | user `postgres` / pass `postgres` / db `jenkins_listener` |
| `elasticsearch` | `elasticsearch:8.12.0` | `elastic-test` | single-node, security disabled, 512 MB heap |
| `kibana` | `kibana:8.12.0` | `kibana` | points at `http://elasticsearch:9200` |

**Volumes:** `jenkins_listener_pgdata`, `elasticsearch_data`, `jenkins_home`.
**Network:** `analyzer` (bridge).

### 3.6 Per-service startup commands

| Service | Docker CMD | Local dev command |
|---------|-----------|-------------------|
| Worker | `python -m failure_analyzer_worker` | `cd failure_analyzer_worker && python -m failure_analyzer_worker` |
| Listener (API) | `uvicorn app.main:app --host 0.0.0.0 --port 8088` | same from `jenkins_failure_listener/` |
| Listener (headless) | `python run_listener.py` | same |
| Web backend | `uvicorn app.main:app --host 0.0.0.0 --port 8095` | same from `web/backend/` |
| Web frontend (prod) | `node server.js` (standalone build) | — |
| Web frontend (dev) | — | `cd web/frontend && npm run dev` |

**Infra-only for local Python development:**

```bash
docker compose up -d postgres elasticsearch
```

### 3.7 Common operational commands

```bash
docker compose logs -f worker            # tail worker logs
docker compose restart web-backend       # restart one service
docker compose up -d --build worker      # rebuild + restart after code change
curl -X POST http://localhost:8088/poll-once   # trigger one listener poll cycle
docker compose down -v                   # ⚠ STOP + WIPE Postgres + ES volumes
```

---

## 4. Environment variables

The single most important secret is **`WORKER_API_KEY`** — it is the shared secret
sent as the **`X-Api-Key`** header between **listener → worker** and
**web-backend → worker**. Set it once in `.env`; all services read the same value.

### 4.1 Minimum required to bring the stack up

| Variable | Purpose |
|----------|---------|
| `WORKER_API_KEY` | Shared secret across worker / listener / web-backend |
| `GROQ_API_KEY` or `LLM_API_KEY` | LLM auth (Groq by default) |
| `JENKINS_BASE_URL` | Jenkins root reachable from containers (default `http://host.docker.internal:8080`) |
| `JENKINS_USER` | Jenkins read-only user |
| `JENKINS_API_TOKEN` | Jenkins API **token** (not password) |

### 4.2 Notable optional variables (with defaults)

| Variable | Default | Notes |
|----------|---------|-------|
| `LLM_PROVIDER` | `groq` | also `openai`, `anthropic`, `ollama` (SDK must be installed in worker image) |
| `LLM_MODEL` | `llama-3.3-70b-versatile` | |
| `LLM_API_BASE` | — | override for OpenAI-compatible gateways / Azure / Ollama |
| `LLM_TLS_VERIFY` | `1` | set `0` only behind a corporate MITM proxy |
| `EMBEDDING_PROVIDER` | `sentence_transformers` | model `all-MiniLM-L6-v2` (local) |
| `EMBEDDING_DIMENSIONS` | `0` | `0` = auto-detect from model |
| `EMBEDDING_TLS_VERIFY` | `1` | set `0` if HuggingFace CDN download fails TLS on first boot |
| `VECTOR_STORE_PROVIDER` | `elasticsearch` | |
| `ELASTICSEARCH_INDEX` | `failure_solutions` | kNN index name |
| `SIMILARITY_THRESHOLD` | `0.75` | min kNN score to treat as a "verified past solution" |
| `POLL_INTERVAL_SECONDS` | `20` | listener poll cadence (headless poller) |
| `STATE_RETENTION_DAYS` | `30` | listener `jenkins_failure_events` TTL |
| `WORKER_SEND_BATCH` | `true` | batch all failures in one POST |
| `MAX_STAGE_LOG_CHARS` | `50000` | per-stage log truncation in listener |
| `RAW_LOG_MAX_CHARS` | `200000` | cap on raw log persisted per session |
| `LOG_BODY_MAX_TOKENS` | `1500` | filter Pass-4 token budget (controls LLM cost) |
| `FILTER_DETECTORS` | `auto` | `auto` / `none` / CSV allowlist |
| `FILTER_MAX_ACTIVE_DETECTORS` | `5` | cap concurrently active detectors |
| `WEB_UI_PUBLIC_URL` | `http://localhost:3080` | deep-link printed in worker logs |
| `CORS_ORIGINS` | `*` | **pin in production** |
| `SESSION_RETENTION_DAYS` | `180` | Postgres session TTL (web-backend janitor) |
| `MESSAGE_RETENTION_DAYS` | `90` | chat message TTL |
| `*_PUBLISH_PORT` | see §3.3 | remap host ports if defaults conflict |

### 4.3 Critical inter-service wiring (already set in Compose)

| Direction | Env var | Value |
|-----------|---------|-------|
| Listener → Worker | `WORKER_INGEST_URL` | `http://worker:8090/ingest/failure` |
| Worker → Web Backend | `WEB_UI_API_URL` | `http://web-backend:8095` |
| Web Backend → Worker | `WORKER_BASE_URL` | `http://worker:8090` |
| Web Backend → Listener | `LISTENER_BASE_URL` | `http://listener:8088` |
| Frontend → Web Backend | `NEXT_PUBLIC_API_TARGET` (build arg) | `http://web-backend:8095` |

> Inside Compose, web-backend must reach listener/worker on container ports
> **8088 / 8090** — never 8080 (that is Jenkins on the host).

---

## 5. HTTP API reference

There are **no Next.js API routes**. The frontend `next.config.ts` rewrites
`/api/*`, `/agent/*`, and `/health` to the FastAPI web-backend. All backend HTTP
lives in three FastAPI apps.

### 5.1 Web Backend — `web/backend/app/main.py` (port 8095)

**Auth:** none (gated by `CORS_ORIGINS`).

| Method | Path | Params / Body | Purpose |
|--------|------|---------------|---------|
| `GET` | `/health` | — | Liveness `{"status":"ok"}` |
| `GET` | `/api/diagnostics` | — | Redacted wiring snapshot; probes listener + worker `/health` |
| `POST` | `/api/maintenance/purge` | query `session_days?`, `message_days?` | Manual retention sweep |
| `GET` | `/api/sessions` | — | List sessions (id, job, build, stage, feedback, created_at) |
| `POST` | `/api/sessions` | `SessionCreate` (see §6.3) | **Called by worker** to persist an analysis session |
| `GET` | `/api/sessions/{id}` | — | Full session + chat messages |
| `POST` | `/api/sessions/{id}/messages` | `{content, use_full_log?}` | Append user msg → worker `/chat/turn` → store assistant reply |
| `POST` | `/api/sessions/{id}/feedback` | `{decision:"accept"\|"reject"}` | Accept → worker `/store-solution` + ES; idempotent |
| `GET` | `/api/stats` | — | `{total, by_error_class, by_feedback_status}` |
| `GET` | `/api/results` | query `limit` (1–200), `job?`, `error_class?` | Trimmed failure list for dashboard |
| `GET` | `/api/results/{run_id}` | — | Single result by UUID |
| `GET` | `/api/knowledge-graph` | query `limit`, `similarity`, `max_neighbours`, `refresh?` | Worker graph + Postgres enrichment (60s TTL cache) |
| `GET` | `/api/filter-config` | query `refresh?` | Proxy worker filter config (60s TTL) |
| `POST` | `/api/listener/poll-once` | — | Proxy to listener `/poll-once` |
| `POST` | `/agent/chat` | `{message, run_id?, use_full_log?}` | UI chat entry point (optional session context) |

### 5.2 Failure Analyzer Worker — `failure_analyzer_worker/worker.py` (port 8090)

**Auth:** all routes except `/health` require header **`X-Api-Key: <WORKER_API_KEY>`**.

| Method | Path | Params / Body | Purpose |
|--------|------|---------------|---------|
| `POST` | `/ingest/failure` | single event **or** `{"failures":[...]}` | Main ingest from listener; runs LangGraph per failed stage |
| `POST` | `/store-solution` | `StoreSolutionRequest` | Index an accepted fix into ES (kNN training data) |
| `POST` | `/chat/turn` | `ChatTurnRequest` | One LLM conversational turn |
| `GET` | `/filter-config` | — | Live filter settings `{settings, detectors{active, available}, implementation}` |
| `GET` | `/knowledge-graph` | query `limit`, `similarity`, `max_neighbours` | Accepted-solutions graph `{nodes, edges, stats}` |
| `GET` | `/health` | — | Liveness + config snapshot (LLM/embedding/ES status) |

`/ingest/failure` returns `{status:"analyzed", count, results[]}`; each result
includes `job_name, build_number, build_url, correlation_id, stage_name,
fingerprint, filtered_logs, raw_logs, analysis, suggested_fix, recommendation,
match_score, matched_solution, filter_meta, similar_past[]`, and (after web
session registration) `web_session_id, web_session_url`.

### 5.3 Jenkins Failure Listener — `jenkins_failure_listener/app/main.py` (port 8088)

**Auth:** none.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness `{status, ci_provider}` |
| `POST` | `/poll-once` | One poll cycle → `{processed, forwarded, failures[], analysis}` |
| `POST` | `/maintenance/purge-state` | Purge old `jenkins_failure_events` rows |

---

## 6. Data model

### 6.1 Postgres — `analysis_sessions` (web-backend, DB `jenkins_listener`)

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | session / run_id |
| `job_full_name` | TEXT | |
| `build_number` | INTEGER | |
| `stage_name` | TEXT | |
| `build_url` | TEXT | |
| `fingerprint` | TEXT | semantic key for kNN |
| `analysis` | TEXT | LLM root-cause narrative |
| `suggested_fix` | TEXT | step-by-step fix section |
| `filtered_logs` | TEXT | worker-filtered excerpt |
| `raw_logs` | TEXT | pre-filter excerpt (capped) |
| `matched_solution` | TEXT | ES hit text, if any |
| `match_score` | DOUBLE | kNN score |
| `recommendation` | TEXT | `verified_past_solution` / `fresh_analysis` |
| `similar_past` | JSONB | array of past matches |
| `feedback_status` | TEXT | `""`, `accepted`, `rejected` |
| `filter_meta` | JSONB | detector telemetry |
| `created_at`, `updated_at` | TIMESTAMPTZ | |

Index: `idx_analysis_sessions_updated_at`.

### 6.2 Postgres — `session_messages` & `jenkins_failure_events`

**`session_messages`** — chat history: `id (UUID PK)`, `session_id (FK → analysis_sessions ON DELETE CASCADE)`, `role` (`user`/`assistant`), `content`, `created_at`. Index `(session_id, created_at)`.

**`jenkins_failure_events`** (listener dedup, `app/postgres_state_store.py`) — PK `(job_full_name, build_number)`; `status` (`seen → processing → processed`/`failed`), `attempt_count`, `first_seen_at`, `processed_at`, `last_error`.

### 6.3 Elasticsearch — index `failure_solutions`

Auto-created by `ElasticsearchSolutionRepository.ensure_ready()`.

| Field | ES type |
|-------|---------|
| `fingerprint_text` | text |
| `fingerprint_vector` | dense_vector (cosine, dims = embedder) |
| `solution` | text |
| `solution_score` | float |
| `job_name`, `stage_name` | keyword |
| `build_number` | integer |
| `created_at` | date |

Store doc id = lowercase `session_id` (upsert) or SHA256 of `job|build|stage|fingerprint`.

`SessionCreate` body for `POST /api/sessions`:
`job_full_name, build_number, stage_name, build_url, fingerprint, analysis,
suggested_fix, filtered_logs, raw_logs, matched_solution, match_score,
recommendation, similar_past[], filter_meta{}`.

---

## 7. Technical depths

### 7.1 Worker internals (`failure_analyzer_worker/`)

**Startup wiring** (FastAPI lifespan): `build_deps(settings)` → `build_graph(deps)`
→ `deps.solutions.ensure_ready()` + `prune()`. `Deps` holds `settings`, `llm`,
`embedder`, `solutions` (ES repo), `prompts`, `filter`. Provider slots are
env-driven via factories: `llm/factory.py`, `embeddings/factory.py`,
`vectorstore/factory.py`, `filtering/factory.py`.

**LangGraph state machine** (`graph.py`):

```
preprocess → route → (analyze_with_context | analyze_fresh) → END
```

| Node | Does |
|------|------|
| `preprocess` | filter logs → fingerprint → ES kNN search |
| `route` | top match score ≥ `SIMILARITY_THRESHOLD` (0.75) → with-context, else fresh |
| `analyze_with_context` | LLM with `with_context.md` prompt + past solution |
| `analyze_fresh` | LLM with `fresh.md` prompt, no prior solution |

LLM output is split on a `Step-by-Step Fix` marker (`_split_response`) into
`analysis` + `suggested_fix`. Recommendation = `verified_past_solution` vs
`fresh_analysis`.

**4-pass structural log filter** (`filtering/orchestrator.py → UniversalFilter`):

| Pass | Module | Purpose |
|------|--------|---------|
| 1 | `tokenize.py` | split raw log into `Line` objects (never drops) |
| 2 | `structural.py` | collapse banners, stacks, JSON, progress, repeats |
| detectors | `detectors/*` | language/shell-specific contributions (capped at `FILTER_MAX_ACTIVE_DETECTORS`) |
| 3 | `baseline.py` | self-baseline diff — drop "normal" lines |
| 4 | `anchors.py` | score + token-budget selection (`LOG_BODY_MAX_TOKENS`) |
| locator | `locator.py` | pick primary `FailureLocation` |
| render | `render.py` | metadata header + `L<idx>:` citations + fingerprint |

Confidence routing (HIGH/MEDIUM/LOW): on LOW it emits a banner + tail excerpt
rather than fabricating a root cause.

**Bundled detectors** (`detectors/__init__.py`): `java_stack`, `python_traceback`,
`node_stack`, `go_panic`, `generic_shell` (always-on, finds last command before a
non-zero exit). `generic_stack.py` is the parameterized engine (`GenericStackDetector`
+ `StackLanguage`) reused by Node/Go. Each detector implements
`activates_on(probe) → contribute(lines) → Contribution`.

> **Do not change the fingerprint shape** (`render.generate_fingerprint`: Caused-by →
> exception types → ERROR line → stage name). The ES kNN index was built on it;
> changing it silently breaks similarity matching for all historical solutions.

**Legacy note:** `log_processor.py` is a back-compat shim; the production path calls
`deps.filter.filter()` directly. CLI helpers `try_filter.py` / `try_worker.py` use
the shim for local debugging.

### 7.2 Listener internals (`jenkins_failure_listener/`)

**Poll cycle** (`FailureMonitorService.poll_once`, `app/service.py`):
1. `release_stale_processing(30)` — reclaim stuck `processing` rows.
2. `ci_source.list_failed_builds()` — parse Jenkins RSS `/rssFailed`.
3. Keep only builds with `build_number > last_processed` per job; dedupe by `(job, build)`.
4. `claim_for_processing` — Postgres optimistic lock (`seen → processing`).
5. `build_failure_event(...)` — fetch stages + logs; on error `mark_failed`.
6. `dispatcher.send_failure_events(events)` → worker `/ingest/failure` (batch if `WORKER_SEND_BATCH`).
7. `mark_processed` on success.

**Stage detection** (`jenkins_client.py`): `GET .../wfapi/describe` lists pipeline
stages; `_select_root_failure_stages` picks the **earliest** failure group so
downstream cascade failures are excluded. Parallel blocks are grouped first via
flow-graph parallel markers (`_group_by_flow_graph_parallel_blocks`), falling back
to time-overlap (`_group_by_time_overlap_with_slack`, `PARALLEL_STAGE_OVERLAP_MS`
default 2000ms). Per-stage logs come from
`.../execution/node/{stage_id}/wfapi/log` (HTML stripped) with console fallbacks;
non-pipeline jobs get one synthetic `"Build"` stage. Logs truncated at
`MAX_STAGE_LOG_CHARS` (50k).

**CI abstraction:** `app/ci/base.py` defines a `CISource` protocol; only
`JenkinsCISource` exists today (`create_ci_source(settings)` keyed on `CI_PROVIDER`).

**Two run modes:** API (`uvicorn app.main:app`, triggered by `/poll-once` or the
dashboard) vs headless (`run_listener.py`, loops every `POLL_INTERVAL_SECONDS`).

### 7.3 Web backend internals (`web/backend/app/`)

`main.py` exposes the REST surface above; `db.py` owns the Postgres schema +
queries; `config.py` reads settings. A background retention loop
(`_retention_loop`) starts on app startup and deletes expired messages then
sessions (accepted solutions persist in ES, so knowledge is not lost).
Chat is proxied to the worker via `_worker_chat` → `httpx.post(worker/chat/turn)`.
`knowledge-graph` and `filter-config` results are cached with a 60s TTL; accepting
a fix invalidates the knowledge-graph cache.

### 7.4 Web frontend internals (`web/frontend/`)

Next.js 16 App Router. Pages: `/` (`app/page.tsx`, overview dashboard), `/rca`
(`app/rca/page.tsx`, drill-in by `?run=<run_id>`), `/settings`
(`app/settings/page.tsx`, filter-config viewer). Key components: `MainDashboard`,
`RcaView`, `FailuresTable`, `FilterBar`, `ChatPanel`, `KnowledgeMap`. API access
goes through `lib/api.ts`; `next.config.ts` uses `output: 'standalone'` and rewrites
`/api/*`, `/agent/*`, `/health` to `NEXT_PUBLIC_API_TARGET`.

### 7.5 Idempotency & dedup layers

1. **Listener:** `(job, build_number)` PK + optimistic `claim_for_processing`.
2. **Feedback:** web-backend skips re-store if `feedback_status` is already terminal.
3. **ES store:** doc id = Postgres `session_id` → upsert, never duplicates.

---

## 8. Dependencies

**Python** (per-service `requirements.txt`; no `pyproject.toml`):
- **Worker:** `fastapi`, `uvicorn[standard]`, `pydantic(-settings)`, `httpx`, `langgraph`, `langchain-core`, `langchain-groq`, `sentence-transformers`, `elasticsearch`. (Optional commented: `langchain-openai/anthropic/ollama`, `openai`.)
- **Listener:** `fastapi`, `uvicorn[standard]`, `httpx`, `pydantic(-settings)`, `psycopg[binary]`.
- **Web backend:** FastAPI + uvicorn + pydantic + httpx + psycopg.

**Frontend** (`web/frontend/package.json`): `next@16`, `react@19`, `tailwindcss@4`,
`@radix-ui/*`, `lucide-react`, `react-markdown` + `remark-gfm` + `rehype-highlight`,
`react-force-graph-2d` (knowledge map), `next-themes`, `typescript`.

---

## 9. Testing

All Python tests are **offline** (no live Jenkins/ES/LLM needed):

```bash
pytest --import-mode=importlib \
  failure_analyzer_worker/tests \
  jenkins_failure_listener/tests \
  web/backend/tests -q

cd web/frontend && npx tsc --noEmit -p tsconfig.json   # frontend type-check
```

See [`docs/testing.md`](testing.md) for details.

---

## 10. Troubleshooting cheatsheet

| Symptom | Likely cause / fix |
|---------|--------------------|
| Worker hangs on first boot at "Load pretrained SentenceTransformer" | HuggingFace CDN TLS failing → set `EMBEDDING_TLS_VERIFY=0` (only behind corporate MITM) |
| LLM calls fail with TLS errors | corporate MITM proxy → set `LLM_TLS_VERIFY=0` and trust the root CA |
| Listener can't reach Jenkins | check `JENKINS_BASE_URL` (use `http://host.docker.internal:8080` from containers) + `JENKINS_API_TOKEN` |
| Worker returns 401/403 to listener | `WORKER_API_KEY` mismatch between services |
| Dashboard empty after a failure | trigger `POST /api/listener/poll-once`; check `docker compose logs -f listener worker` |
| ES kNN never matches old fixes | fingerprint shape changed, or index wiped via `docker compose down -v` |
| Frontend port 3000 conflict on Windows | it's intentionally published on **3080** |
| `docker compose down -v` lost all data | `-v` wipes `pgdata` + `elasticsearch_data` volumes — avoid in shared/staging envs |

---

## 11. Where to go next

| Need | Doc |
|------|-----|
| 30-second service map | [`architecture.md`](architecture.md) |
| Full HLD + LLD deep-dive | [`technical_deets.md`](technical_deets.md) |
| Repo layout rationale | [`folder-structure.md`](folder-structure.md) |
| Worker feature guide | [`failure-analyzer-worker.md`](failure-analyzer-worker.md) |
| Listener feature guide | [`jenkins-failure-listener.md`](jenkins-failure-listener.md) |
| Web API guide | [`web-backend.md`](web-backend.md) |
| UI guide | [`web-frontend.md`](web-frontend.md) |
| Filtering internals | [`filtering.md`](filtering.md) |
| Docker/Compose ops | [`docker.md`](docker.md) |
