# Technical Deep-Dive: AI CI/CD Log Analyzer 

> Companion to [`architecture.md`](architecture.md), [`failure-analyzer-worker.md`](failure-analyzer-worker.md), [`jenkins-failure-listener.md`](jenkins-failure-listener.md), [`web-backend.md`](web-backend.md), [`web-frontend.md`](web-frontend.md), [`filtering.md`](filtering.md), [`docker.md`](docker.md), and [`folder-structure.md`](folder-structure.md).
>
> This document is the **single source of truth for HLD + LLD**. The per-feature `docs/*.md` pages stay short and link here for the "why".

---

## Table of Contents

1. [Problem statement & design goals](#1-problem-statement--design-goals)
2. [High-Level Design (HLD)](#2-high-level-design-hld)
3. [Pipeline walkthrough — end to end](#3-pipeline-walkthrough--end-to-end)
4. [Low-Level Design (LLD)](#4-low-level-design-lld)
   - 4.1 [`jenkins_failure_listener`](#41-jenkins_failure_listener)
   - 4.2 [`failure_analyzer_worker`](#42-failure_analyzer_worker)
   - 4.3 [`web/backend`](#43-webbackend)
   - 4.4 [`web/frontend`](#44-webfrontend)
5. [Data model](#5-data-model)
6. [Wire formats](#6-wire-formats)
7. [Cross-cutting concerns](#7-cross-cutting-concerns)
8. [Operations](#8-operations)
9. [Extension cookbook](#9-extension-cookbook)
10. [Failure & idempotency matrix](#10-failure--idempotency-matrix)
11. [Performance characteristics](#11-performance-characteristics)
12. [Glossary](#12-glossary)

---

## 1. Problem statement & design goals

### 1.1 Problem

CI/CD failures in Jenkins (and other CI systems) generate large, noisy logs. Engineers spend a disproportionate share of MTTR (mean time to recovery) just **reading the log to find the root cause**. Two compounding problems:

1. The interesting 5–20 lines are buried under 10,000s of lines of progress output, dependency resolution, and framework chatter.
2. The same root cause recurs across builds — "yet another flaky `ConnectionRefused` on staging-3" — but the institutional memory of how it was fixed last time isn't indexed anywhere.

### 1.2 Design goals

| Goal | How it shows up |
|---|---|
| **Token economy** | Never send raw logs to the LLM; pre-compress with the structural filter to 5–10× smaller bodies while preserving the root cause. |
| **Learning loop** | Operator-accepted fixes are persisted in a kNN vector store keyed by fingerprint embeddings, so future occurrences route through `analyze_with_context`. |
| **Provider neutrality** | LLM, embedder, vector store, and CI source are all interface-driven (`Protocol` types) with factory functions. Switch vendors via `.env`. |
| **Composable filters** | Stack-specific detectors (Java, Python, Node, Go, shell) are pure functions returning immutable `Contribution`s — order-independent, individually fault-tolerant. |
| **Operational honesty** | Filter emits `HIGH / MEDIUM / LOW` confidence with explicit `low_reason`; LOW path prepends a banner and tail excerpt so the LLM is told **not** to fabricate. |
| **Idempotency end-to-end** | Listener Postgres claims; web-backend idempotent `accept`; worker uses session UUID as ES doc id; ES upsert never duplicates. |
| **Per-service isolation** | One folder per process, each with its own `requirements.txt`, `.env.example`, and tests. No `src/` layer. |

### 1.3 Non-goals (current scope)

- Not a Jenkins replacement — we are read-only consumers of Jenkins.
- Not a log shipper — we pull only the failed-stage excerpt, not full job logs to Elastic.
- No multi-tenant access control — `X-API-Key` is a single shared secret; SSO and RBAC are out of scope for this iteration.
- No streaming LLM output — chat responses are single-shot.

---

## 2. High-Level Design (HLD)

### 2.1 System context diagram

```
                              ┌──────────────────────┐
                              │       Jenkins        │
                              │  rssFailed + wfapi   │
                              └──────────┬───────────┘
                                         │ HTTP poll (POLL_INTERVAL_SECONDS)
                                         ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  jenkins_failure_listener        FastAPI :8088   (run_listener.py loop)  │
 │  - Polls /rssFailed                                                      │
 │  - Resolves failed stage via /wfapi/describe + /wfapi/log                │
 │  - Cleans HTML, strips Jenkins TS, drops noise, caps to MAX_LOG_CHARS    │
 │  - Dedupes via Postgres state store (claim_for_processing / mark_*)      │
 │  - POSTs `{failures:[...]}` batch → worker /ingest/failure (X-API-Key)   │
 └────────────────────────────┬─────────────────────────────────────────────┘
                              │
                              ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  failure_analyzer_worker          FastAPI :8090   (LangGraph)            │
 │  1. UniversalFilter.filter()   4-pass structural compression             │
 │  2. generate_fingerprint()     stable semantic signature                 │
 │  3. SolutionRepository.search() kNN over fingerprint embeddings (ES)     │
 │  4. AnalysisGraph.invoke()      preprocess → route → analyze_with_ctx |  │
 │                                       analyze_fresh → END                │
 │  5. POST /api/sessions → web-backend (UI deep-link)                      │
 └─────┬────────────────────────────────────────────────────┬───────────────┘
       │ ensure_ready / index / search                      │ POST session
       ▼                                                    ▼
 ┌────────────────────────┐                  ┌──────────────────────────────┐
 │   Elasticsearch        │                  │ web/backend  FastAPI :8095   │
 │   index:               │ ◀──── /store-────│ - sessions / chat / feedback │
 │   failure_solutions    │      solution    │ - proxy /api/listener/...    │
 │   (kNN dense_vector)   │                  │ - filter-config TTL cache    │
 └────────────────────────┘                  │ - retention janitor (asyncio)│
                                             └──────────────┬───────────────┘
                                                            │ psycopg
                                                            ▼
                                             ┌──────────────────────────────┐
                                             │   Postgres                   │
                                             │   analysis_sessions          │
                                             │   session_messages           │
                                             │   processed_builds (listener)│
                                             └──────────────┬───────────────┘
                                                            │ HTTP/JSON
                                                            ▼
                                             ┌──────────────────────────────┐
                                             │ web/frontend  Next.js 16     │
                                             │  - Dashboard, FailureCard    │
                                             │  - ChatPanel, FilterMeta     │
                                             │  - Knowledge graph viewer    │
                                             │  - /settings (filter-config) │
                                             └──────────────────────────────┘
```

### 2.2 Service inventory

| Service | Host port | Container port | Image / module | Compose profile |
|---|---|---|---|---|
| `jenkins_failure_listener` (API) | 8088 | 8088 | `docker/Dockerfile.listener` | default |
| `listener-poller` (headless) | — | — | reuses listener image, `python run_listener.py` | `poll` |
| `failure_analyzer_worker` | 8090 | 8090 | `docker/Dockerfile.worker` | default |
| `web/backend` | 8095 | 8095 | `docker/Dockerfile.web-backend` | default |
| `web/frontend` (prod) | 3080 | 3000 | `docker/Dockerfile.web-frontend` | default |
| `web-frontend-dev` (live reload) | 5173 | 5173 | `node:22-slim` bind-mount | `dev-ui` |
| `postgres` | 5432 | 5432 | `postgres:16-alpine` | default |
| `elasticsearch` | 9200 | 9200 | `elasticsearch:8.12.0` | default |
| `kibana` | 5601 | 5601 | `kibana:8.12.0` | default |
| `jenkins` (in-cluster demo) | 8080, 50000 | 8080, 50000 | `my-jenkins:2.541.1` | `jenkins` |

> Port 3080 (not 3000) is the default UI host port because Windows / Hyper-V reserves 2971–3070. Override with `WEB_FRONTEND_PUBLISH_PORT=3000` if your host allows it.

### 2.3 Architectural principles

1. **One process, one folder.** Each service has its own `requirements.txt`, `.env.example`, and `tests/`. Tests can run per-service in CI without cross-service deps. See [`folder-structure.md`](folder-structure.md).
2. **Provider slots, not vendor names.** Pydantic `WorkerSettings` reads generic keys (`LLM_API_KEY`, `LLM_API_BASE`, `LLM_MODEL`); the factory layer maps `LLM_PROVIDER ∈ {groq, openai, anthropic, ollama}` to a concrete `LLMClient`. Same for `EMBEDDING_PROVIDER` and `VECTOR_STORE_PROVIDER`. Adding a vendor is a single `providers/<name>.py` file + a branch in `factory.py`.
3. **Composition root per service.** FastAPI `lifespan` builds a `Deps` dataclass once and stores it on `app.state`. Routes pull collaborators through `_deps(request) / _graph(request)` helpers. Tests override `app.state.*` directly; no module-level singletons.
4. **Token economy.** The four-pass structural filter compresses raw logs by 5–10× before they ever reach the LLM. The `[METADATA]` header tells the LLM exactly what was dropped, and `L<idx>:` citation prefixes let the LLM ground its analysis in specific lines.
5. **Learning loop.** Accepted fixes go to Elasticsearch keyed by **fingerprint embedding** (not raw log). Future occurrences with similar fingerprints get routed through `analyze_with_context`, which references the past solution in the prompt.
6. **Idempotency end-to-end.** Detailed in [§10](#10-failure--idempotency-matrix).
7. **Confidence routing.** Filter emits `HIGH / MEDIUM / LOW`. LOW path prepends a banner so the LLM is explicitly told not to fabricate. The UI maps confidence to green/amber/red.
8. **Pure detectors.** Every detector is a pure function of the line list; it returns immutable `Contribution`s. The orchestrator merges them commutatively, so detector order never affects output and a buggy detector cannot corrupt other detectors' results.

### 2.4 Data stores

| Store | Owner | Tables / indices | Retention | Why this store |
|---|---|---|---|---|
| Postgres | listener, web-backend | `processed_builds`, `analysis_sessions`, `session_messages` | listener `STATE_RETENTION_DAYS=30`, sessions `SESSION_RETENTION_DAYS=180`, messages `MESSAGE_RETENTION_DAYS=90` | Strong transactional semantics for claim-based dedup and session writes; first-class JSONB for `filter_meta` / `similar_past` |
| Elasticsearch | worker | `failure_solutions` (dense_vector, 384-D for MiniLM) | `ELASTICSEARCH_RETENTION_SOLUTIONS_DAYS=1095` (~36 months), pruned at worker startup | Native kNN over fingerprint embeddings; also lets ops inspect the corpus in Kibana |

### 2.5 Process model

- Every service is **single-process FastAPI/uvicorn** (or `next start` for the frontend prod build).
- Web-backend spawns **one asyncio task** in `lifespan` for the retention janitor; if `RETENTION_RUN_INTERVAL_HOURS=0`, it runs once on startup and exits.
- Worker runs the **entire LangGraph synchronously** inside the request thread — no background workers. This keeps the failure path easy to reason about; throughput is bounded by Groq/OpenAI rate limits, not by us.
- Listener `/poll-once` is **synchronous and short** (single HTTP roundtrip per failed build), making it safe for a UI button to invoke.
- Headless poller (`run_listener.py`, profile `poll`) is a plain `while True: poll_once(); sleep(POLL_INTERVAL_SECONDS)` loop.

### 2.6 Trust boundaries & authentication

| Boundary | Mechanism |
|---|---|
| Listener → Worker | `X-API-Key: WORKER_API_KEY` (shared secret) |
| Web-backend → Worker | `X-API-Key: WORKER_API_KEY` |
| Web-backend → Listener | None (internal Docker network); guarded by `LISTENER_BASE_URL` resolving to a private hostname |
| Browser → Web-backend | CORS via `CORS_ORIGINS` (default `*`, pin in production); no auth — assumes platform sits behind corporate SSO / reverse-proxy |
| Jenkins → Listener | Listener calls Jenkins outbound with `JENKINS_USER` + `JENKINS_API_TOKEN` (read-only token sufficient) |

> Secrets are **never** baked into images. Every credential is injected at runtime via `environment:` in `docker-compose.yml`. The repo only ships `.env.example` files.

---

## 3. Pipeline walkthrough — end to end

### 3.1 Stages

```
[1] CI emits failure
       │
[2] Listener polls + claims + resolves stage log
       │
[3] Listener POSTs `{failures:[…]}` → worker /ingest/failure
       │
[4] Worker filter compresses + fingerprints + locates
       │
[5] Worker queries ES kNN
       │
[6] Worker LangGraph routes + LLM analyzes
       │
[7] Worker POSTs session → web-backend
       │
[8] UI renders + user chats
       │
[9] User Accepts/Rejects → web-backend → worker /store-solution → ES
       │
[10] Next occurrence of same fingerprint → analyze_with_context (loop closes)
```

### 3.2 Detailed step-by-step

#### [1] Jenkins emits failure
A pipeline build `team/web-build #N` fails. Jenkins emits an entry on `/rssFailed` containing the title (`team » web-build #N`) and a link to the build page.

#### [2] Listener poll
`FailureMonitorService.poll_once()`:
1. `state_store.release_stale_processing(stale_minutes=30)` — recovers builds whose previous claim crashed.
2. `ci_source.list_failed_builds()` → parses the RSS into `[{job_full_name, build_number, build_url}]`.
3. For each unique job in the feed, fetches `state_store.get_last_processed_build_number(job)`; filters to only builds with `build_number > last_processed`.
4. Sorts ascending by `(job, build_number)`, dedupes.
5. For each new failure: `state_store.claim_for_processing(job, n)` — atomic insert-with-conflict; if already claimed (by another listener instance), skip.
6. `ci_source.build_failure_event(job, n, url)`:
   - `GET <build>/api/json` for build metadata.
   - `GET <build>/wfapi/describe` for the pipeline stage list — picks the chronologically first failed stage.
   - `GET <build>/execution/node/<id>/wfapi/log` for the stage log HTML.
   - Cleans: `_strip_html` removes `<…>` and `&entity;`, `_JENKINS_TS_PREFIX` strips `HH:MM:SS ` line prefixes, the noise filter drops Maven progress / Spring Cloud retry chatter, then head + tail truncate to `MAX_LOG_CHARS` (default ~50KB).
   - Builds a `FailureEvent` Pydantic model.
7. `dispatcher.send_failure_events(events)` — POSTs to `WORKER_INGEST_URL` (default `http://worker:8090/ingest/failure`) with `X-API-Key`. `WORKER_SEND_BATCH=true` sends all events in one request; `false` sends one per request (legacy worker support).
8. On 2xx → `state_store.mark_processed(job, n)`. On exception → `state_store.mark_failed(job, n, reason)`; next poll retries because the row is no longer "processing".

#### [3] Worker ingest
`POST /ingest/failure` (worker.py):
1. `_verify_api_key(x_api_key)`.
2. Parses body as either single `FailureEventPayload` or `{failures: [FailureEventPayload, ...]}`.
3. For each event × each stage with non-empty `log_excerpt`, calls `graph.invoke(state)`.

#### [4] Filter (UniversalFilter.filter)

The four passes:

**Pass 1 — `tokenize.py`** (never drops):
- Strip ANSI (`\x1b\[[0-9;]*[A-Za-z]`).
- Strip leading timestamp (ISO-8601, bare wall-clock, human date).
- Mask high-cardinality tokens **in this order**: UUID → HEX → multi-digit NUMS → POSIX path → Windows path → URL → size/duration → horizontal whitespace.
- Classify `Kind ∈ {PLAIN, CMD, KV, JSON, STACK, PROGRESS, BANNER}` by shape only (indentation, `at …`, `:`, `{`, decorative chars). **Never by framework name** — adding Bazel doesn't need a code change.
- Rate `Severity ∈ {INFO, DEBUG, WARN, ERROR, FATAL, CAUSE, EXIT}` — the gap between `WARN=1` and `ERROR=3` is deliberate: anything `≥ ERROR` is exempt from baseline-driven drops.
- Compute `template_hash` over the normalized text (used by Pass 2 repeat collapsing).
- Defensive `_MAX_LINE_CHARS=2000` cap so a base64 blob can't blow up regex engines.

**Pass 2 — `structural.py`** (drops decoration):
- Collapse decorative banners (`=========`, `:: Spring Boot ::`).
- Collapse non-anchor stack frames (`stack-frame-elided`), keeping `Caused by:` and the topmost project frame.
- Fold JSON blobs to a one-line summary.
- Elide progress bursts (`Downloading …`).
- Elide consecutive repeats (`(repeated 14×)` note).

**Detector phase** (between Pass 2 and Pass 3 — runs against post-collapse `Line`s):
- Probe phase: `det.activates_on(probe[:4096])` — cheap regex over first 4 KB. False positives are fine.
- Sort detectors by `priority` descending; cap at `FILTER_MAX_ACTIVE_DETECTORS=5`.
- For each activated detector, `c = det.contribute(lines)`:
  - May return `None` (cheap probe was a false positive — detector self-vetoes).
  - Returns immutable `Contribution(detector, score_deltas, drop_marks, keep_marks, locations, notes)`.
- Trust boundary: each `contribute` runs under try/except; an exception logs and skips that detector.
- `_merge_contributions(lines, contribs)`:
  - Score deltas: additive, non-negative (a detector can boost a line's score but never lower it).
  - Drop marks: unioned, **gated by `Severity ≥ ERROR` and not `keep`** (we never drop a real error line).
  - Keep marks: override drops.
  - Notes: first-writer-wins per index.
- This is **commutative** by design — detector order can be reshuffled (or parallelized) without changing output.

**Pass 3 — `baseline.py`**:
- `SelfSketch(lines)`: builds an in-log frequency sketch of `template_hash`es and drops lines whose template appears so often within this same log that it's almost certainly framework noise.
- External `sketch_provider(job, stage)` is a hook for a future ES-backed CounterSketch ("Baseline A") so we can compare against historical builds of the same `(job, stage)`. Not wired yet — the `sketch_provider` parameter is optional.

**Pass 4 — `anchors.py`**:
- `score(lines)`: combines severity, position bonus (errors near end matter more), detector deltas.
- `select(lines, token_budget=LOG_BODY_MAX_TOKENS)`:
  - Identify anchor clusters (high-scoring contiguous regions).
  - Greedy round-robin fill across clusters under the token budget (~4 chars/token).
  - Returns the kept indices in original file order.

**Locator** (`locator.py`):
- Aggregate all `FailureLocation` candidates from all detectors.
- `pick(all_locations, lines) → (primary, ranked)`: ranks by `(detector_priority, confidence, kind preference)`. Kind preference: `source > test > command > log > unknown`.

**Confidence routing** (`_confidence`):
- `LOW` if no anchor lines met the minimum score threshold.
- Otherwise check `max(severity among selected)`. If `< ERROR` **and** we don't have a high-confidence `source/test` location, → `LOW` with reason "no surviving line scored at ERROR severity or above".
- If `primary` is None or `kind == "unknown"`, → `MEDIUM`.
- If `primary.kind == "log"`, → `MEDIUM` (we have a line number but no structured anchor).
- If `primary.confidence < 0.6`, → `MEDIUM`.
- Otherwise → `HIGH`.

**Render** (`render.py`):
- `render_body(lines, selected, with_citation=True)`: emits `L<idx>: <text>` for each selected line. Citation prefixes are crucial — they let the LLM reference exact lines in its analysis.
- LOW path prepends `render_low_confidence_banner(reason)` + `_tail_excerpt(lines, max_chars)`.
- Hard char ceiling `LOG_BODY_MAX_CHARS=8000` defends against pathological inputs that slip past the token budget.
- `render_metadata_header(...)`: top-of-body `[METADATA]` block carrying raw→body chars/tokens, activated detectors, baseline_version, collapse_stats, confidence, primary_location.as_anchor.
- `primary_error`: prefers `primary.message` (detector-supplied, like `ConnectException: Connection refused`); falls back to a regex-based extractor.
- `exit_code`: pulled from anywhere in `(raw + body)` by regex.
- `generate_fingerprint(body, stage)`: deterministic hash over the canonical body + stage. **This** is the kNN retrieval key.

Returns a `FilterResult` dataclass.

#### [5] kNN retrieval
`deps.solutions.search(fingerprint)`:
1. Embed the fingerprint via `deps.embedder.embed_one(fingerprint)`.
2. ES `knn` query on `failure_solutions` index, `k=SIMILARITY_TOP_K`, `num_candidates=SIMILARITY_NUM_CANDIDATES`.
3. Filter hits by `_score >= SIMILARITY_THRESHOLD`.
4. Return `list[SolutionMatch]` sorted by score descending.

ES failures are caught in `graph._preprocess` and downgrade to "no matches" — analysis still proceeds.

#### [6] LangGraph

```
preprocess
   │ {filtered_logs, filter_meta, fingerprint, es_matches}
   ▼
route                        # if es_matches[0].score ≥ SIMILARITY_THRESHOLD
   │
   ├─► analyze_with_context  # prompt template `with_context`, includes past_fingerprint + past_solution
   │       │
   │       └─► recommendation = "verified_past_solution"
   │
   └─► analyze_fresh         # prompt template `fresh`
           │
           └─► recommendation = "fresh_analysis"
```

Both leaves call `deps.llm.invoke([ChatMessage(role="system", ...), ChatMessage(role="user", ...)])`, then:
- `_split_response(content)`: splits on `Step-by-Step Fix` heading (tolerates `## Step-by-Step Fix` or `2. **Step-by-Step Fix**`).
- `_strip_match_status_preamble(analysis)`: removes LLM boilerplate like "Exact match found, similarity 87%, returning previously accepted fix…" (this is signal we already display, not narrative).

#### [7] Session POST
`_register_web_sessions(rows)`:
- For each row, POSTs to `WEB_UI_API_URL/api/sessions`.
- Body carries everything the UI needs: job/build/stage/url, fingerprint, analysis, suggested_fix, filtered_logs, raw_logs (capped at `RAW_LOG_MAX_CHARS=200000`), matched_solution, match_score, recommendation, similar_past, **filter_meta** (full dict including primary_location, collapse_stats, etc.).
- Response carries the session UUID → worker stamps `web_session_url = f"{WEB_UI_PUBLIC_URL}/?session={sid}"` in the row.
- Failures are logged but don't fail the ingest — the analysis is still returned to the listener.

#### [8] UI render & chat
- Browser hits `/?session=<uuid>` → Next.js page fetches `/api/sessions/<uuid>`.
- `Dashboard` lists sessions; `FailureCard` shows analysis + suggested_fix; `FilterMetaPanel` shows confidence badge + detector chips + primary location + compression ratio; `ChatPanel` shows message history.
- User posts a message → web-backend `/api/sessions/{id}/messages`:
  1. `db.insert_message(session_id, "user", content)`.
  2. Pull the entire conversation: `db.list_messages(session_id)`.
  3. Build chat payload via `_build_chat_payload(row, payload, use_full_log=...)` — picks `raw_logs` vs `filtered_logs` based on `use_full_log` toggle.
  4. POST to worker `/chat/turn`.
  5. Worker `AnalysisGraph.chat_clarification` constructs a system prompt with original analysis + fix + log excerpt (capped by `CHAT_LOG_EXCERPT_MAX_CHARS=12000` or `CHAT_LOG_EXCERPT_MAX_CHARS_FULL=60000`), calls `deps.llm.invoke`.
  6. `db.insert_message(session_id, "assistant", reply)`; return to UI.

#### [9] Accept / Reject
`POST /api/sessions/{id}/feedback {decision:"accept"|"reject"}`:
1. Fetch session row; if `feedback_status ∈ {accepted, rejected}`, return `{idempotent: true}` — short-circuit (defends against double-click).
2. If `accept`:
   - `solution = suggested_fix or analysis`.
   - POST `/store-solution` to worker with `{fingerprint, solution, job_name, stage_name, build_number, session_id}`.
   - Worker `_solution_doc_id(req)`: prefers `session_id` (lowercased); falls back to `sha256(job|build|stage|fingerprint)`.
   - Worker `deps.solutions.store(Solution(...), doc_id=...)` upserts the ES doc — same doc_id means overwrite, never duplicate.
   - On 2xx: `db.set_feedback_status(session_id, "accepted")`; invalidate `_kg_cache` (knowledge graph re-derives next request).
3. If `reject`: `db.set_feedback_status(session_id, "rejected")`. No worker call.

#### [10] Loop closes
On the next analysis of a stage producing a similar fingerprint:
- kNN search returns the accepted solution above `SIMILARITY_THRESHOLD`.
- LangGraph routes to `analyze_with_context`.
- The prompt explicitly references the past fingerprint + solution.
- LLM responds with `recommendation=verified_past_solution` and a short reuse-style analysis.

---

## 4. Low-Level Design (LLD)

### 4.1 `jenkins_failure_listener`

#### 4.1.1 Folder layout

```
jenkins_failure_listener/
├── app/
│   ├── main.py                  FastAPI app: POST /poll-once, POST /maintenance/purge-state, GET /health
│   ├── service.py               FailureMonitorService.poll_once() — pure orchestration
│   ├── ci/
│   │   ├── base.py              CISource Protocol + FailedBuildRef value object
│   │   ├── factory.py           create_ci_source(settings) — switches on CI_PROVIDER
│   │   ├── __init__.py
│   │   └── providers/jenkins.py JenkinsCISource adapter
│   ├── jenkins_client.py        Low-level Jenkins HTTP: RSS, wfapi/describe, wfapi/log, HTML strip, log scoring
│   ├── dispatcher.py            WorkerDispatcher.send_failure_events(events) — httpx POST with X-API-Key
│   ├── postgres_state_store.py  PostgresStateStore — claim/mark/release/purge
│   ├── config.py                Pydantic Settings (Jenkins creds, DB URL, worker URL, intervals)
│   └── models.py                FailureEvent, FailedStage Pydantic models
├── run_listener.py              Headless `while True: poll_once(); sleep` loop
└── tests/                       Pure-function pytest suite (no DB, no HTTP)
```

#### 4.1.2 Key classes / functions

**`CISource` (Protocol, `app/ci/base.py`)**

```python
class CISource(Protocol):
    def list_failed_builds(self) -> list[dict]: ...
    def build_failure_event(self, job: str, number: int, url: str) -> FailureEvent: ...
```

Two operations are all the listener needs from a CI system. Adding GitLab is a new `providers/gitlab.py`.

**`JenkinsCISource` (`app/ci/providers/jenkins.py`)**

- Composes `JenkinsClient` for HTTP and `_log_error_pattern_bundle` for log scoring.
- `list_failed_builds`: GETs `JENKINS_FAILED_RSS_PATH` (default `/rssFailed`), parses Atom/RSS, extracts `(job_full_name, build_number)` via `_parse_title_and_link`.
- `build_failure_event`: orchestrates `wfapi/describe` → pick first failed stage → `wfapi/log` → clean → truncate → bundle into `FailureEvent`.

**`JenkinsClient` (`app/jenkins_client.py`)**

- `_strip_html`, `_JENKINS_TS_PREFIX`, `_HTML_TAG`, `_log_error_pattern_bundle` (LRU-cached compiled regex bundle: `typed_throwable`, `failure_signal`, `boilerplate_anchor`, `operational_hint`).
- `_score_line_as_error_anchor(line)`: integer score for "is this an error anchor"; used to rank log lines when picking the most informative tail slice.
- `_group_by_time_overlap_with_slack()`: groups parallel-branch failures.
- `_has_real_error_signals(text)`: gate that suppresses dispatching builds whose logs are all warnings.

**`PostgresStateStore` (`app/postgres_state_store.py`)**

Schema (created on first `connect()`):
```sql
processed_builds (
  job_full_name TEXT, build_number INT,
  status TEXT,             -- 'processing' | 'processed' | 'failed'
  attempts INT, last_error TEXT,
  claimed_at TIMESTAMPTZ, processed_at TIMESTAMPTZ,
  PRIMARY KEY (job_full_name, build_number)
)
```

API:
- `claim_for_processing(job, n)` → bool. `INSERT ... ON CONFLICT (...) DO NOTHING`; returns whether the insert took effect.
- `mark_processed(job, n)` / `mark_failed(job, n, reason)` — updates status + timestamps.
- `release_stale_processing(stale_minutes)` — recovers crashed claims.
- `get_last_processed_build_number(job)` — `MAX(build_number)` filter for "new" detection.
- `purge_processed_older_than_days(days)` — `DELETE WHERE status='processed' AND processed_at < NOW() - INTERVAL`.

**`WorkerDispatcher` (`app/dispatcher.py`)**

- `send_batch: bool` toggle. `true` (default) → single POST with `{failures:[…]}`. `false` → one POST per event (legacy compat).
- Uses `httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)` with `X-API-Key` header.
- Returns the worker's JSON response as a dict so the service can log analysis counts.

**`FailureMonitorService.poll_once()` (`app/service.py`)**

Pseudocode:
```
release_stale_processing(30m)
all_failed = ci_source.list_failed_builds()
last_processed = { job → max build_number processed } for jobs in feed
new_failures = sort+dedupe([item for item in all_failed
                            if item.build_number > last_processed[item.job]])
claimed, events = [], []
for item in new_failures:
    if not state_store.claim_for_processing(item.job, item.n): continue
    try:    event = ci_source.build_failure_event(item.job, item.n, item.url)
    except: state_store.mark_failed(item.job, item.n, str(exc)); raise
    events.append(event); claimed.append((item.job, item.n, item.url))
if events:
    try:    resp = dispatcher.send_failure_events(events)
    except: for j,n,_ in claimed: mark_failed(j,n,reason); raise
    for j,n,_ in claimed: state_store.mark_processed(j,n)
return { processed, forwarded, failures, analysis }
```

#### 4.1.3 HTTP API

| Method | Path | Body | Returns | Auth |
|---|---|---|---|---|
| POST | `/poll-once` | — | `{processed, forwarded, failures:[FailureEvent...], analysis:{status,count,results:[…]}}` | none |
| POST | `/maintenance/purge-state` | — | `{deleted, retention_days}` | none |
| GET | `/health` | — | `{status:"ok", ci_provider}` | none |

`/poll-once` catches `httpx.HTTPStatusError` (→ 502 with `body_preview`), `httpx.RequestError` (→ 502 `upstream_connect_error`), and unexpected exceptions (→ 500 with type+message). Also runs `jsonable_encoder` to defensively catch unserialisable responses.

#### 4.1.4 Environment

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | required | Postgres connection string |
| `CI_PROVIDER` | `jenkins` | switches `ci_source` factory |
| `JENKINS_BASE_URL` | required | `http://host.docker.internal:8080` for Docker → host |
| `JENKINS_USER` / `JENKINS_API_TOKEN` | required | Read-only Jenkins user is enough |
| `JENKINS_FAILED_RSS_PATH` | `/rssFailed` | Override only if Jenkins is proxied on a subpath |
| `POLL_INTERVAL_SECONDS` | 20 | Headless loop sleep |
| `STATE_RETENTION_DAYS` | 30 | Auto-purges `processed_builds` |
| `WORKER_INGEST_URL` | required | e.g. `http://worker:8090/ingest/failure` |
| `WORKER_INGEST_API_KEY` | required | Must match worker `WORKER_API_KEY` |
| `WORKER_SEND_BATCH` | `true` | `false` for legacy workers |
| `REQUEST_TIMEOUT_SECONDS` | 300 | httpx timeout |

---

### 4.2 `failure_analyzer_worker`

#### 4.2.1 Folder layout

```
failure_analyzer_worker/
├── __main__.py                  uvicorn entry — `python -m failure_analyzer_worker`
├── worker.py                    FastAPI app + lifespan composition root
├── config.py                    WorkerSettings (Pydantic) — provider slots
├── deps.py                      Deps dataclass + build_deps(settings)
├── graph.py                     AnalysisGraph (LangGraph) — preprocess → route → analyze_*
├── log_processor.py             Legacy CLI helper (filter_logs) — used by try_filter.py
├── knowledge_graph.py           build_knowledge_graph(records) — pure numpy cosine + WCC
├── try_filter.py / try_worker.py  Local CLI helpers for manual testing
├── filtering/
│   ├── base.py                  Line, Kind, Severity, Contribution, FailureLocation,
│   │                            FilterResult, Filter Protocol, Detector Protocol
│   ├── factory.py               create_filter(settings) + describe_filter() introspection
│   ├── orchestrator.py          UniversalFilter + FilterConfig — orchestrates four passes
│   ├── tokenize.py              Pass 1
│   ├── structural.py            Pass 2
│   ├── baseline.py              Pass 3 (BaselineSketch, SelfSketch, EmptySketch)
│   ├── anchors.py               Pass 4
│   ├── locator.py               Rank + pick primary FailureLocation
│   ├── render.py                Body render, [METADATA] header, fingerprint
│   └── detectors/
│       ├── __init__.py          register / load_detectors / bundled_names
│       ├── java_stack.py        Hand-written
│       ├── python_traceback.py  Hand-written
│       ├── node_stack.py        Data-driven (GenericStackDetector + StackLanguage)
│       ├── go_panic.py          Data-driven
│       ├── generic_stack.py     GenericStackDetector class + StackLanguage dataclass
│       └── generic_shell.py     Stack-agnostic floor
├── llm/
│   ├── base.py                  LLMClient Protocol + ChatMessage dataclass
│   ├── factory.py               create_llm(settings) — provider switch
│   ├── _lc.py                   Shared LangChain glue
│   ├── __init__.py
│   └── providers/{groq,openai,anthropic,ollama}.py
├── embeddings/
│   ├── base.py                  Embedder Protocol
│   ├── factory.py
│   ├── __init__.py
│   └── providers/{sentence_transformers,openai}.py
├── vectorstore/
│   ├── base.py                  Solution / SolutionMatch / SolutionRecord / SolutionRepository
│   ├── factory.py               create_solution_repository(settings, embedder)
│   ├── __init__.py
│   └── providers/elasticsearch.py — kNN dense_vector
├── prompts/
│   ├── PromptLoader (resolves PROMPTS_DIR overrides)
│   └── templates/{system,fresh,with_context,clarify_system}.md
└── tests/                       Offline pytest — no ES, no LLM, no network
```

#### 4.2.2 `WorkerSettings` (config.py) — full table

| Group | Key | Default | Purpose |
|---|---|---|---|
| Vector store | `VECTOR_STORE_PROVIDER` | `elasticsearch` | Switches `create_solution_repository` |
| | `SIMILARITY_THRESHOLD` | 0.75 | Min cosine score for `analyze_with_context` |
| | `SIMILARITY_TOP_K` | 3 | Max hits to return from kNN |
| | `SIMILARITY_NUM_CANDIDATES` | 50 | HNSW exploration size |
| ES | `ELASTICSEARCH_URL` | `http://localhost:9200` | |
| | `ELASTICSEARCH_INDEX` | `failure_solutions` | |
| | `ELASTICSEARCH_API_KEY` | `""` | |
| | `ELASTICSEARCH_REQUEST_TIMEOUT_SECONDS` | 30 | |
| | `ELASTICSEARCH_RETENTION_SOLUTIONS_DAYS` | 1095 | ~36 months |
| Embed | `EMBEDDING_PROVIDER` | `sentence_transformers` | |
| | `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | 384-dim |
| | `EMBEDDING_DIMENSIONS` | 0 | 0 = auto-discover |
| | `EMBEDDING_API_KEY` / `_API_BASE` | `""` | For OpenAI-style providers |
| | `EMBEDDING_TLS_VERIFY` | `1` | Set `0` only behind a corp MITM proxy |
| LLM | `LLM_PROVIDER` | `groq` | `groq|openai|anthropic|ollama` |
| | `LLM_MODEL` | `llama-3.3-70b-versatile` | |
| | `LLM_TEMPERATURE` | 0.1 | Low temp for deterministic RCAs |
| | `LLM_MAX_TOKENS` | 2048 | |
| | `LLM_API_KEY` / `_API_BASE` | `""` | |
| | `LLM_TLS_VERIFY` | `1` | |
| | `GROQ_API_KEY` | `""` | Legacy fallback for Groq |
| Prompts | `PROMPTS_DIR` | `""` | Bind-mount to override templates |
| HTTP | `WORKER_HOST` | `127.0.0.1` | |
| | `WORKER_PORT` | 8090 | |
| | `WORKER_API_KEY` | `replace-me` | Shared with listener + web-backend |
| Web UI | `WEB_UI_API_URL` | `""` | Where to POST sessions (`http://web-backend:8095`) |
| | `WEB_UI_PUBLIC_URL` | `http://127.0.0.1:3000` | Used to stamp deep-links in logs |
| | `WEB_UI_SESSION_TIMEOUT_SECONDS` | 20.0 | |
| Filter | `LOG_BODY_MAX_TOKENS` | 1500 | Soft budget for Pass 4 |
| | `LOG_BODY_MAX_CHARS` | 8000 | Hard ceiling fallback |
| | `FILTER_DETECTORS` | `auto` | `auto|none|<comma-list>` |
| | `FILTER_MAX_ACTIVE_DETECTORS` | 5 | Activation cap per run |
| | `RAW_LOG_MAX_CHARS` | 200_000 | Cap on raw log persisted to Postgres |
| | `CHAT_LOG_EXCERPT_MAX_CHARS` | 12_000 | Default chat context cap |
| | `CHAT_LOG_EXCERPT_MAX_CHARS_FULL` | 60_000 | When `use_full_log=true` |

#### 4.2.3 `Deps` (deps.py)

```python
@dataclass
class Deps:
    settings: WorkerSettings
    llm: LLMClient
    embedder: Embedder
    solutions: SolutionRepository
    prompts: PromptLoader
    filter: Filter
```

Built once in `lifespan`; stored on `app.state.deps`. Tests inject by assigning a custom `Deps` directly.

#### 4.2.4 `AnalysisGraph` (graph.py)

`AnalysisState : TypedDict` (all keys `total=False`):
- Inputs: `raw_logs, stage_name, job_name, build_number, build_url`.
- Intermediates: `filtered_logs, filter_meta, fingerprint, es_matches`.
- Outputs: `analysis, suggested_fix, recommendation, matched_solution, match_score`.

State machine:
```
START → preprocess → route ─┬─→ analyze_with_context → END
                            └─→ analyze_fresh        → END
```

Methods:
- `invoke(state)` — single-shot graph execution.
- `chat_clarification(messages, *, fingerprint, log_excerpt, ..., use_full_log)` — builds system prompt with context, calls `deps.llm.invoke`; cap selection: `use_full_log ? CHAT_LOG_EXCERPT_MAX_CHARS_FULL : CHAT_LOG_EXCERPT_MAX_CHARS`.

Helpers:
- `_split_response(content)`: regex `^(?:#{1,6}\s*|\d+\.\s*\**)Step[-\s]?by[-\s]?Step\s+Fix\**` — tolerates `## Step-by-Step Fix` or `2. **Step-by-Step Fix**`.
- `_strip_match_status_preamble(text)`: removes "Exact match found, similarity 87%, returning previously accepted fix" boilerplate the LLM sometimes prepends to context-routed analyses.

Backward-compat shims at module scope:
- `get_default_graph() / reset_default_graph() / _DeferredGraph` — keep legacy `from .graph import analysis_graph` callers working without forcing them to know about DI.
- `chat_clarification_reply(...)` — wraps `get_default_graph().chat_clarification`.

#### 4.2.5 `UniversalFilter` (filtering/orchestrator.py)

`FilterConfig` (frozen dataclass):
```python
log_body_max_tokens: int = 1500
log_body_max_chars: int = 8000
max_active_detectors: int = 5
use_self_baseline: bool = True
```

Constructor:
```python
def __init__(self, config, detectors=(), sketch_provider=None):
    self._cfg = config
    self._detectors = sorted(detectors, key=lambda d: -d.priority)
    self._sketch_provider = sketch_provider  # (job, stage) -> BaselineSketch | None
```

`filter(raw_logs, *, stage_name="", job_name="")` returns `FilterResult` — the canonical entry point. Stateless across calls; safe to share one instance across worker threads.

Introspection:
- `.config` — read-only view of active `FilterConfig`.
- `.detectors` — tuple of active detectors sorted by descending priority.

Used by `/filter-config` for the Settings page.

#### 4.2.6 `Detector` protocol & registry

```python
class Detector(Protocol):
    name: str
    priority: int
    def activates_on(self, probe: str) -> bool: ...
    def contribute(self, lines: Sequence[Line]) -> Contribution | None: ...
```

Bundled detectors (current):

| Name | Priority | Description |
|---|---|---|
| `java_stack` | 50 | Picks topmost project frame from JVM stacks; filters JPMS prefixes + ~30-entry framework allowlist; walks `Caused-by:` chain. |
| `python_traceback` | 50 | Picks bottom-most project frame; understands "during handling of the above exception" chained tracebacks; extracts source snippet. |
| `node_stack` | 50 | `GenericStackDetector(StackLanguage(name="node_stack", ...))`. Skips `node_modules` and `node:internal`. Carries `TypeError`/`Error` class through. |
| `go_panic` | 50 | `GenericStackDetector(StackLanguage(name="go_panic", ...))`. Skips `GOROOT` and `/go/pkg/mod/`. Recovers function name from line above each `file.go:N` frame. |
| `generic_shell` | 0 | Stack-agnostic floor: finds last command before a non-zero exit. Hidden in UI when stack-specific detectors also fired. |

Registry (`detectors/__init__.py`):
- `register(detector_instance)` — module-scope side effect at import time.
- `load_detectors(selection)`:
  - `"auto"` → all bundled.
  - `"none"` → empty.
  - `"a,b,c"` → allowlist by name.
- `bundled_names()` — for the Settings page inventory.

Settings page resolution order for descriptions (`factory.py::_detector_description`):
1. Instance-level `description` attribute (used by `GenericStackDetector` so two detectors sharing one class get distinct one-liners).
2. Class docstring's first non-blank line.
3. Class name (last-resort).

#### 4.2.7 Vector store (vectorstore/)

Protocol:
```python
class SolutionRepository(Protocol):
    def ensure_ready(self) -> None: ...                 # idempotent index create
    def search(self, fingerprint: str) -> list[SolutionMatch]: ...
    def store(self, solution: Solution, *, doc_id: str | None = None) -> str: ...
    def prune(self, older_than_days: int) -> None: ...  # delete_by_query on created_at
    def list_all(self, *, limit=2000, include_vectors=False) -> list[SolutionRecord]: ...
```

ES provider (`providers/elasticsearch.py`):
- `ensure_ready`: idempotent `indices.create` with mapping
  - `fingerprint_text: text + keyword`
  - `solution: text`
  - `job_name / stage_name: keyword`
  - `build_number: integer`
  - `embedding: dense_vector` (dims discovered from embedder)
  - `created_at: date`
- `search(fp)`: `knn={field:embedding, query_vector:embed(fp), k:TOP_K, num_candidates:NUM_CANDIDATES}`; filters hits ≥ `SIMILARITY_THRESHOLD`.
- `store(sol, doc_id)`: builds doc, `index(id=doc_id)` — upserts.
- `prune(days)`: `delete_by_query({range:{created_at:{lt:"now-Nd"}}})`.

#### 4.2.8 LLM (llm/)

Protocol:
```python
class LLMClient(Protocol):
    def invoke(self, messages: list[ChatMessage]) -> str: ...
```

`ChatMessage(role: Literal["system","user","assistant"], content: str)`.

`create_llm(settings)` switches on `LLM_PROVIDER`. Providers are imported **lazily** — you only need the SDK of the vendor you selected.

#### 4.2.9 Embeddings (embeddings/)

```python
class Embedder(Protocol):
    @property
    def dimensions(self) -> int: ...
    def embed_one(self, text: str) -> list[float]: ...
    def embed_many(self, texts: Sequence[str]) -> list[list[float]]: ...
```

Default `sentence_transformers` provider downloads `all-MiniLM-L6-v2` (384-D) on first use; cached under `~/.cache/huggingface`. Set `EMBEDDING_TLS_VERIFY=0` only behind a corporate MITM proxy.

#### 4.2.10 Prompts (prompts/)

```python
class PromptLoader:
    def __init__(self, dir: str | None = None): ...   # falls back to package templates/
    def load(self, name: str) -> str: ...             # raw template string
    def render(self, name: str, **vars) -> str: ...   # str.format-style substitution
```

Built-in templates: `system.md`, `fresh.md`, `with_context.md`, `clarify_system.md`. Bind-mount a directory and set `PROMPTS_DIR` to override.

#### 4.2.11 Worker HTTP API

| Method | Path | Body | Returns | Auth |
|---|---|---|---|---|
| POST | `/ingest/failure` | One event or `{failures:[…]}` | `{status:"analyzed", count, results:[{job_name, build_number, build_url, correlation_id, stage_name, fingerprint, filtered_logs, raw_logs, analysis, suggested_fix, recommendation, match_score, matched_solution, filter_meta, similar_past}]}` | `X-API-Key` |
| POST | `/store-solution` | `{fingerprint, solution, job_name, stage_name, build_number, solution_score, session_id}` | `{status:"stored", doc_id}` | `X-API-Key` |
| POST | `/chat/turn` | `{messages, fingerprint, log_excerpt, job_name, stage_name, build_number, analysis, suggested_fix, use_full_log}` | `{role:"assistant", content}` | `X-API-Key` |
| GET | `/filter-config` | — | `{settings:{...}, detectors:{active:[...], available:[...]}, implementation}` | `X-API-Key` |
| GET | `/knowledge-graph?limit=&similarity=&max_neighbours=` | — | `{nodes:[...], edges:[...], stats:{communities, total_solutions, total_similarity_edges}}` | `X-API-Key` |
| GET | `/health` | — | `{status, listener_link, llm_provider, llm_model, embedding_*, vector_store_provider, ES url/index, retention_days, web_ui_api_url}` | none |

#### 4.2.12 Knowledge graph (knowledge_graph.py)

`build_knowledge_graph(records: list[SolutionRecord], *, similarity_threshold, max_neighbours)`:

Nodes (typed by `kind`):
- `solution` — one per accepted fix.
- `error_class` — extracted via `_ERROR_CLASS_PATTERNS` from fingerprint.
- `job` — Jenkins job full name.
- `stage` — pipeline stage name.

Edges (typed by `kind`):
- `similar` — solution ↔ solution; top-K cosine neighbours above threshold over normalized fingerprint vectors.
- `resolves` — solution → error_class.
- `occurs_in` — solution → stage.
- `in_job` — stage → job.

Communities are **weakly-connected components** over the `similar` subgraph (no Louvain/Leiden dep — pure stdlib BFS). Implementation prefers numpy for the cosine block but falls back to pure Python so unit tests don't need numpy.

Web-backend `/api/knowledge-graph`:
- TTL-caches the worker response (60s).
- Enriches with Postgres `pg_session_count` per solution + KB metrics (`accepted_sessions`, `matched_sessions`, `total_sessions`, `kb_coverage`).
- Cache invalidated on every `accept` (KB changes).

---

### 4.3 `web/backend`

#### 4.3.1 Folder layout

```
web/backend/app/
├── main.py     FastAPI app (lifespan, CORS, retention loop, all routes)
├── db.py       psycopg helpers + SCHEMA_SQL + retention/aggregate queries
└── config.py   Pydantic Settings
web/backend/tests/   FastAPI TestClient smoke tests
```

#### 4.3.2 `Settings` (config.py)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | required | Same Postgres as listener |
| `WORKER_BASE_URL` | `http://worker:8090` | Forward feedback/chat |
| `WORKER_API_KEY` | required | Sent as `X-API-Key` |
| `LISTENER_BASE_URL` | `http://listener:8088` | Proxy `/poll-once` |
| `CORS_ORIGINS` | `*` | Comma-separated; pin in production |
| `SESSION_RETENTION_DAYS` | 180 | Janitor TTL |
| `MESSAGE_RETENTION_DAYS` | 90 | Janitor TTL |
| `RETENTION_RUN_INTERVAL_HOURS` | 24 | Set 0 to run once on startup |

#### 4.3.3 Schema (db.py `SCHEMA_SQL`)

```sql
CREATE TABLE IF NOT EXISTS analysis_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_full_name TEXT NOT NULL,
    build_number INTEGER NOT NULL,
    stage_name TEXT NOT NULL DEFAULT '',
    build_url TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    analysis TEXT NOT NULL DEFAULT '',
    suggested_fix TEXT NOT NULL DEFAULT '',
    filtered_logs TEXT NOT NULL DEFAULT '',
    raw_logs TEXT NOT NULL DEFAULT '',
    matched_solution TEXT NOT NULL DEFAULT '',
    match_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    recommendation TEXT NOT NULL DEFAULT '',
    similar_past JSONB NOT NULL DEFAULT '[]'::jsonb,
    feedback_status TEXT NOT NULL DEFAULT '',
    filter_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE analysis_sessions ADD COLUMN IF NOT EXISTS raw_logs TEXT NOT NULL DEFAULT '';
ALTER TABLE analysis_sessions ADD COLUMN IF NOT EXISTS filter_meta JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS session_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES analysis_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_analysis_sessions_updated_at ON analysis_sessions(updated_at);
```

Migrations are **additive `ADD COLUMN IF NOT EXISTS`** — legacy rows just carry default values; no manual SQL ever required on upgrade.

#### 4.3.4 Public API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | `{status:"ok"}` |
| GET | `/api/diagnostics` | Wiring snapshot (DB target, listener/worker reachability + error preview) |
| POST | `/api/maintenance/purge` | Manual retention sweep with optional `session_days` / `message_days` |
| POST | `/api/sessions` | Worker writes a new analysis row |
| GET | `/api/sessions` | List (UUID, job, build, stage, feedback_status, created_at) |
| GET | `/api/sessions/{id}` | Detail + messages |
| POST | `/api/sessions/{id}/messages` | Append user msg + LLM reply |
| POST | `/api/sessions/{id}/feedback` | `accept`/`reject` (idempotent) |
| GET | `/api/stats` | Counts by error_class + feedback_status |
| GET | `/api/results` | Trimmed projection (`_to_result_item` + `_summarize_filter_meta`) |
| GET | `/api/results/{run_id}` | Single result by UUID |
| GET | `/api/filter-config` | TTL 60s proxy of worker `/filter-config` |
| GET | `/api/knowledge-graph` | TTL 60s proxy of worker `/knowledge-graph` + Postgres enrichment |
| POST | `/api/listener/poll-once` | Proxy → listener `/poll-once` |

#### 4.3.5 Background tasks

- `_retention_loop` (asyncio task spawned in `lifespan`):
  ```python
  while True:
      try: db.run_retention(session_days, message_days)
      except: logger.exception("Retention sweep failed")
      if interval_hours <= 0: return
      await asyncio.sleep(interval_hours * 3600)
  ```
- `_kg_cache` (in-process dict): `{key, value, expires}`; checked on every `/api/knowledge-graph` hit; invalidated on every `accept`.

#### 4.3.6 Selected helpers

- `_extract_error_class(row)`: scans `fingerprint → analysis → suggested_fix → filtered_logs` for `\b([A-Za-z_]\w*(?:Exception|Error|Failure))\b`; falls back to `error|failed|fatal`; returns `"UnknownError"` otherwise.
- `_summarize_filter_meta(meta)`: lossy projection of the worker's filter metadata to the UI-friendly fields (`confidence`, `activated_detectors`, `primary_location`, `raw_chars`, `body_chars`, `body_tokens`, `selected_count`, `baseline_version`, `collapse_stats`). Empties stripped so the UI can branch on `Object.keys`.
- `_build_chat_payload(row, payload, use_full_log)`: picks `raw_logs` vs `filtered_logs` based on the toggle.
- `_worker_chat(messages, row, use_full_log)`: `httpx.post(worker /chat/turn)` with `X-API-Key`; maps worker 429 → 429, other HTTP errors → 502.

---

### 4.4 `web/frontend`

#### 4.4.1 Folder layout

```
web/frontend/
├── app/
│   ├── layout.tsx            Self-hosted Inter + JetBrains Mono via next/font/local
│   ├── page.tsx              Home — Dashboard + ChatPanel + StatsBar
│   ├── settings/page.tsx     SettingsPage — reads /api/filter-config
│   ├── globals.css
│   └── fonts/                Inter & JetBrains Mono WOFF2
├── components/
│   ├── Dashboard.tsx         Session list + selection
│   ├── FailureCard.tsx       Analysis + suggested fix + Accept/Reject
│   ├── FilterMetaPanel.tsx   "Detected" panel
│   ├── ChatPanel.tsx         /api/sessions/{id}/messages — survives reloads
│   ├── StatsBar.tsx          kNN match metadata
│   ├── SettingsPanel.tsx     Read-only knobs + detector inventory
│   ├── ErrorClassBadge.tsx, theme-provider.tsx, theme-toggle.tsx
│   └── ui/                   shadcn-style primitives (button, scroll-area, …)
├── lib/
│   ├── fetchResults.ts, fetchSessionDetail.ts, fetchStats.ts,
│   ├── fetchFilterConfig.ts, fetchKnowledgeGraph.ts, fetchDiagnostics.ts,
│   ├── pollListenerOnce.ts
│   ├── sanitizeAnalysis.ts   stripMatchStatusFromAnalysis()
│   ├── types.ts              Shared TS shapes
│   └── utils.ts              cn() class-name merge
├── next.config.ts
├── postcss.config.mjs
└── tsconfig.json
```

#### 4.4.2 Pages

- `/?session=<uuid>` — Home page deep-link. `app/page.tsx` reads the query param, fetches `/api/sessions/<uuid>`, renders `<Dashboard>` (list view + selection), `<FailureCard>` (analysis + suggested fix + Accept/Reject), `<FilterMetaPanel>` (confidence + detectors + primary location + compression), `<ChatPanel>` (chat history + send), `<StatsBar>` (kNN metadata).
- `/settings` — `app/settings/page.tsx` fetches `/api/filter-config`, renders `<SettingsPanel>` with three sections: tweakable knobs, active detectors, all bundled detectors (with active/disabled indicator).

#### 4.4.3 Auto-poll
The "Refresh / Auto-poll" control in the Dashboard calls `pollListenerOnce()` → `POST /api/listener/poll-once` → web-backend → listener `/poll-once`. The UI never talks to the listener directly (avoids CORS + isolates the network topology).

#### 4.4.4 Fonts
Inter + JetBrains Mono are **self-hosted** under `app/fonts/` and wired via `next/font/local`. No `fonts.googleapis.com` access at build time → builds are deterministic, offline-safe, and friendly to corp proxies / air-gapped CI.

#### 4.4.5 Environment

| Variable | Default | Notes |
|---|---|---|
| `NEXT_PUBLIC_API_TARGET` | `http://web-backend:8095` (Docker) / `http://127.0.0.1:8095` (local) | Browser-visible URL of the web-backend |

---

## 5. Data model

### 5.1 Postgres tables

**`processed_builds`** (listener)
| Column | Type | Notes |
|---|---|---|
| job_full_name | TEXT | PK part |
| build_number | INT | PK part |
| status | TEXT | `processing|processed|failed` |
| attempts | INT | retry counter |
| last_error | TEXT | reason on `failed` |
| claimed_at | TIMESTAMPTZ | |
| processed_at | TIMESTAMPTZ | |

**`analysis_sessions`** (web-backend) — see [§4.3.3](#433-schema-dbpy-schema_sql).

**`session_messages`** (web-backend) — FK → `analysis_sessions.id ON DELETE CASCADE`.

### 5.2 Elasticsearch index

**`failure_solutions`** mapping:
| Field | Type | Notes |
|---|---|---|
| `fingerprint_text` | text + keyword | Original signature string |
| `solution` | text | Accepted fix body |
| `job_name` | keyword | Filterable |
| `stage_name` | keyword | Filterable |
| `build_number` | integer | |
| `solution_score` | float | Operator-graded quality (default 1.0) |
| `embedding` | dense_vector | dims = embedder dim (384 for MiniLM); cosine |
| `created_at` | date | Retention pruning key |

Doc id: lowercased Postgres `session_id` if present, else `sha256(job|build|stage|fingerprint)`.

### 5.3 In-memory value objects (worker filter)

```python
@dataclass(slots=True)
class Line:
    idx: int                       # original 0-based index
    raw: str                       # original line
    text: str                      # normalized (timestamps + tokens masked)
    kind: Kind = Kind.PLAIN
    severity: Severity = Severity.INFO
    indent: int = 0
    template_hash: int = 0
    score: int = 0
    drop: bool = False
    keep: bool = False             # explicit force-keep wins over drop
    reason: str = ""               # anchor/context/summary/banner/...
    note: str = ""                 # "(repeated 14×)"
    var_tokens: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class FailureLocation:
    kind: Literal["source","test","command","log","unknown"]
    file: str = ""
    line: int = 0
    column: int = 0
    function: str = ""
    test_name: str = ""
    command: str = ""
    detector: str = "core"
    confidence: float = 0.0
    log_line_idx: int = -1
    message: str = ""

@dataclass(frozen=True, slots=True)
class Contribution:
    detector: str
    score_deltas: dict[int, int] = {}
    drop_marks: frozenset[int] = frozenset()
    keep_marks: frozenset[int] = frozenset()
    locations: tuple[FailureLocation, ...] = ()
    notes: dict[int, str] = {}

@dataclass(frozen=True, slots=True)
class FilterResult:
    body: str
    fingerprint: str
    primary_error: str
    primary_location: FailureLocation | None
    locations: tuple[FailureLocation, ...]
    confidence: Literal["HIGH","MEDIUM","LOW"]
    metadata: dict
    raw_chars: int
    body_chars: int
    body_tokens: int
```

### 5.4 Vector store value objects

```python
@dataclass
class Solution:            # written on accept
    fingerprint_text: str
    solution: str
    job_name: str = ""
    stage_name: str = ""
    build_number: int = 0
    solution_score: float = 1.0

@dataclass
class SolutionMatch:       # returned by search()
    score: float
    solution: str
    fingerprint_text: str
    job_name: str = ""
    stage_name: str = ""
    build_number: int = 0
    raw: dict[str, Any] = {}

@dataclass
class SolutionRecord:      # returned by list_all() — includes embedding for KG
    doc_id: str
    fingerprint_text: str
    solution: str
    job_name: str = ""
    stage_name: str = ""
    build_number: int = 0
    solution_score: float = 1.0
    created_at: str = ""
    vector: list[float] = []
```

---

## 6. Wire formats

### 6.1 Listener → Worker (`/ingest/failure`)

```json
{
  "failures": [
    {
      "event_type": "stage_failure",
      "jenkins_url": "http://jenkins.example.com",
      "job_full_name": "team/web-build",
      "build_number": 42,
      "build_url": "http://.../job/team/job/web-build/42/",
      "build_result": "FAILURE",
      "timestamp": "2026-06-18T07:14:22Z",
      "correlation_id": "1c9c…",
      "failed_stages": [
        {
          "stage_name": "Build",
          "stage_id": "7",
          "status": "FAILED",
          "log_excerpt": "…HTML-stripped, TS-stripped, head+tail truncated…"
        }
      ]
    }
  ]
}
```

### 6.2 Worker → Web-backend (`POST /api/sessions`)

```json
{
  "job_full_name": "team/web-build",
  "build_number": 42,
  "stage_name": "Build",
  "build_url": "...",
  "fingerprint": "stage=Build|err=ConnectException|loc=PaymentClient.java:84",
  "analysis": "…markdown…",
  "suggested_fix": "## Step-by-Step Fix\n1. …",
  "filtered_logs": "[METADATA …]\nL12: …\nL34: …",
  "raw_logs": "…up to RAW_LOG_MAX_CHARS=200000…",
  "matched_solution": "",
  "match_score": 0.82,
  "recommendation": "verified_past_solution",
  "similar_past": [{"score": 0.82, "solution": "…", "fingerprint_text": "…"}],
  "filter_meta": {
    "confidence": "HIGH",
    "activated_detectors": ["java_stack"],
    "primary_location": {"kind":"source","file":"PaymentClient.java","line":84,"function":"processPayment","confidence":0.85,"message":"ConnectException: Connection refused"},
    "raw_chars": 53412,
    "body_chars": 5980,
    "body_tokens": 1495,
    "selected_count": 47,
    "baseline_version": "self",
    "collapse_stats": {"banner": 6, "stack-frame-elided": 22, "json-folded": 1, "progress-elided": 9, "repeat-elided": 4}
  }
}
```

### 6.3 Worker → ES (kNN store doc)

```json
{
  "fingerprint_text": "stage=Build|err=ConnectException|loc=PaymentClient.java:84",
  "solution": "Set spring.cloud.config.fail-fast=false ...",
  "job_name": "team/web-build",
  "stage_name": "Build",
  "build_number": 42,
  "solution_score": 1.0,
  "embedding": [0.0123, -0.0455, "…", 0.0091],
  "created_at": "2026-06-18T07:15:42Z"
}
```

### 6.4 Worker `/filter-config` response

```json
{
  "settings": {
    "log_body_max_tokens": 1500,
    "log_body_max_chars": 8000,
    "filter_detectors": "auto",
    "filter_max_active_detectors": 5,
    "use_self_baseline": true
  },
  "detectors": {
    "active":   [{"name":"java_stack","priority":50,"description":"…","active":true}],
    "available":[{"name":"java_stack","priority":50,"description":"…","active":true},
                 {"name":"node_stack","priority":50,"description":"…","active":true}]
  },
  "implementation": "UniversalFilter"
}
```

---

## 7. Cross-cutting concerns

### 7.1 Observability

- **Logging:** every service uses `logging.basicConfig(level=INFO)`. Worker logs structured filter telemetry per analysis:
  ```
  preprocess  job=team/web-build #42  stage=Build  confidence=HIGH
              loc=PaymentClient.processPayment @ PaymentClient.java:84
              raw=53412→body=5980 (1495 tok)  detectors=java_stack  matches=2
  ```
- **Filter telemetry on UI:** every `FailureCard` shows the "Detected" panel (confidence badge, detector chips, compression ratio, primary location).
- **Diagnostics endpoint:** `web-backend GET /api/diagnostics` returns redacted wiring snapshot (DB target + listener/worker reachability with error preview).

### 7.2 Security

- `X-API-Key` on every worker route (except `/health`).
- CORS pinned via `CORS_ORIGINS`. Default `*` for local dev; production must set explicit origins.
- TLS verification toggles for LLM (`LLM_TLS_VERIFY`) and embedder (`EMBEDDING_TLS_VERIFY`) — only set `0` behind corporate MITM proxies. Both log a warning when disabled.
- No secrets baked into images. `.dockerignore` excludes `.env` (except `.env.example`).

### 7.3 Health checks

Every FastAPI service exposes `GET /health` returning at minimum `{"status":"ok"}`. Postgres uses `pg_isready`; Elasticsearch uses `_cluster/health`. Compose health-check policies gate `depends_on: { condition: service_healthy }` so the worker waits for ES, etc.

### 7.4 Retention

| Store | Policy |
|---|---|
| Postgres `processed_builds` | Listener `STATE_RETENTION_DAYS=30`, `POST /maintenance/purge-state` manual |
| Postgres `analysis_sessions` | `SESSION_RETENTION_DAYS=180`, swept by `_retention_loop` every `RETENTION_RUN_INTERVAL_HOURS=24` |
| Postgres `session_messages` | `MESSAGE_RETENTION_DAYS=90`, same loop |
| ES `failure_solutions` | `ELASTICSEARCH_RETENTION_SOLUTIONS_DAYS=1095` (~36 months); pruned on worker `lifespan` startup |

---

## 8. Operations

### 8.1 First run

```bash
cp .env.example .env
# Edit GROQ_API_KEY (or LLM_API_KEY), WORKER_API_KEY, JENKINS_*
docker compose up -d --build
docker compose ps
```

Smoke checks:
```bash
curl http://localhost:8090/health   # worker
curl http://localhost:8088/health   # listener
curl http://localhost:8095/health   # web-backend
start http://localhost:3080         # dashboard (Windows)
```

### 8.2 Compose profiles

| Profile | Adds |
|---|---|
| `poll` | `listener-poller` — headless `python run_listener.py` alongside the API |
| `dev-ui` | `web-frontend-dev` — Next dev server with bind-mounted source on port 5173 |
| `jenkins` | In-cluster Jenkins (`my-jenkins:2.541.1`) for full-stack demos |

### 8.3 Common ops tasks

| Task | Command |
|---|---|
| Tail one service's logs | `docker compose logs -f worker` |
| Rebuild after Dockerfile edit | `docker compose build worker && docker compose up -d worker` |
| Reset Postgres + ES volumes | `docker compose down -v`  (destroys all sessions + ES docs) |
| Open a shell inside worker | `docker compose exec worker /bin/sh` |
| Trigger manual poll | `curl -X POST http://localhost:8088/poll-once` |
| Manual retention sweep | `curl -X POST 'http://localhost:8095/api/maintenance/purge?message_days=30'` |

### 8.4 Testing

| Service | Command | Notes |
|---|---|---|
| Worker | `pytest failure_analyzer_worker/tests -q` | Fully offline (no ES, no LLM, no network) |
| Listener | `pytest jenkins_failure_listener/tests -q` | Pure-function suite |
| Web-backend | `pytest web/backend/tests -q` | FastAPI TestClient smoke tests |
| Frontend | `cd web/frontend && npx tsc --noEmit -p tsconfig.json` | TS as primary safety net |

---

## 9. Extension cookbook

### 9.1 Add a new LLM vendor

1. Create `failure_analyzer_worker/llm/providers/<name>.py` implementing the `LLMClient` Protocol.
2. Add a branch in `llm/factory.py::create_llm`.
3. Set `LLM_PROVIDER=<name>` in `.env`.

### 9.2 Add a new vector store (e.g. Qdrant)

1. Create `failure_analyzer_worker/vectorstore/providers/qdrant.py` implementing `SolutionRepository`.
2. Add a branch in `vectorstore/factory.py`.
3. Set `VECTOR_STORE_PROVIDER=qdrant` in `.env`.

### 9.3 Add a new embedding model

1. Create `failure_analyzer_worker/embeddings/providers/<name>.py`.
2. Add a branch in `embeddings/factory.py`.
3. Set `EMBEDDING_PROVIDER=<name>` / `EMBEDDING_MODEL=<id>`.

### 9.4 Add a new language detector (Ruby example)

Data-driven (preferred):

```python
# failure_analyzer_worker/filtering/detectors/ruby_stack.py
from . import register
from .generic_stack import GenericStackDetector, StackLanguage

register(GenericStackDetector(StackLanguage(
    name="ruby_stack",
    priority=50,
    description="Localizes Ruby failures to the topmost project frame.",
    probe_pattern=r"^\s+from\s+\S+\.rb:\d+",
    frame_pattern=r"^\s+from\s+(?P<file>\S+\.rb):(?P<line>\d+)(?::in\s+`(?P<function>[^']+)')?",
    framework_paths=("/usr/lib/ruby/", "/gems/"),
    bug_site="top",
    cause_pattern=r"^(?P<cls>[\w:]*[A-Z]\w*(?:Error|Exception))(?::\s*(?P<msg>.*))?$",
    cause_position="above",
)))
```

Then add `"ruby_stack"` to `_BUNDLED` in `detectors/__init__.py`. The Settings page picks it up automatically.

Hand-written (only when regex can't express the structure — e.g. chained tracebacks, JPMS prefixes):

1. Define a class with `name`, `priority`, `activates_on`, `contribute`.
2. `register(YourDetector())` at module scope.
3. Add to `_BUNDLED`.
4. The class docstring's first line becomes the Settings-page description.

### 9.5 Add a new CI source (GitLab CI)

1. Create `jenkins_failure_listener/app/ci/providers/gitlab.py` implementing `CISource`.
2. Add a branch in `ci/factory.py::create_ci_source`.
3. Set `CI_PROVIDER=gitlab` in `.env` + relevant `GITLAB_*` creds.

### 9.6 Add a new dashboard page

1. Create `web/frontend/app/<path>/page.tsx`.
2. Add a `lib/fetch<X>.ts` helper.
3. (Optional) Add a route in `web/backend/app/main.py` for new server data.

### 9.7 Add a new Postgres column on sessions

1. Append to `SCHEMA_SQL` in `web/backend/app/db.py`:
   ```sql
   ALTER TABLE analysis_sessions ADD COLUMN IF NOT EXISTS my_new_col TEXT NOT NULL DEFAULT '';
   ```
2. Update `SessionCreate` Pydantic model + `insert_session` signature.
3. Restart web-backend; the migration runs on startup.

---

## 10. Failure & idempotency matrix

| Failure mode | Where caught | Recovery |
|---|---|---|
| Jenkins HTTP 5xx during RSS poll | `JenkinsCISource` → `httpx` raise | `service.poll_once` re-raises; next poll retries; claim still held → released by `release_stale_processing(30m)` |
| Worker unreachable from listener | `WorkerDispatcher` → `httpx.HTTPError` | `mark_failed(job, n)`; next poll re-tries (status no longer 'processing') |
| Worker filter exception in one detector | `UniversalFilter._run_detectors` try/except | Logged, detector skipped, run continues with remaining detectors |
| ES unreachable on worker startup | `worker.py` lifespan try/except | Logged warning; `/health` still 200; first analysis returns empty matches; `prune()` skipped |
| LLM rate-limit / 429 | `_worker_chat` in web-backend | Propagated as HTTP 429 to UI with a friendly retry-after message |
| Duplicate Accept click | `post_feedback` idempotency guard | Short-circuit returns existing status, no second `/store-solution` |
| Same fingerprint re-accepted | Worker `_solution_doc_id` uses `session_id` | ES `index(id=session_id)` upserts — no duplicate kNN doc |
| Pathological 10MB log | Listener `MAX_STAGE_LOG_CHARS` head/tail + worker `RAW_LOG_MAX_CHARS=200000` hard cap | Bounded Postgres rows |
| Single line >2KB (base64 blob) | `tokenize.py::_MAX_LINE_CHARS=2000` | Truncated before regex, defending the engine |
| Filter LOW confidence | `_confidence` returns LOW + reason | Renderer prepends banner + tail; prompt instructs LLM not to fabricate |
| Web-backend can't reach worker | `_worker_chat` → 502 | UI surfaces toast; user can retry |
| Listener row stuck in 'processing' (crash) | `release_stale_processing(stale_minutes=30)` | Periodic recovery on every poll |

---

## 11. Performance characteristics

### 11.1 Hot path budget (per failed stage)

| Step | Typical cost |
|---|---|
| Listener: RSS parse + wfapi/describe + wfapi/log | ~200–600 ms (Jenkins-bound) |
| Worker: Pass 1 tokenize on ~50KB log | < 50 ms |
| Worker: Pass 2 structural collapse | < 20 ms |
| Worker: Detector phase (5 detectors, 4KB probe each + contribute) | < 30 ms total |
| Worker: Pass 3 SelfSketch baseline | < 10 ms |
| Worker: Pass 4 anchors + locator + render | < 20 ms |
| Worker: Embed fingerprint via MiniLM (cold start: model load ~3 s once; warm: ~5 ms) | ~5 ms warm |
| Worker: ES kNN search | 20–80 ms |
| Worker: LLM call (Groq llama-3.3-70b-versatile, 1500 token in / 500 token out) | 2–4 s |
| Worker: `/api/sessions` POST to web-backend | ~30 ms |

**End-to-end (warm):** dominated by the LLM call. Filter overhead is < 200 ms.

### 11.2 Filter compression target

`raw_chars / body_chars` typically lands at **5–10×** for stack-trace-heavy logs and **2–4×** for command-output logs. The metadata header in the body explicitly reports these numbers so operators can spot regressions.

### 11.3 Throughput limits

- **Listener:** bounded by Jenkins's `wfapi` response time (~500 ms per build). With `POLL_INTERVAL_SECONDS=20`, comfortable for 100 failures/hour.
- **Worker:** bounded by LLM rate limits. Groq's free tier (~30 req/min) is the typical bottleneck. Increase by upgrading the LLM plan or running multiple worker replicas behind a load balancer.
- **ES kNN:** sub-100ms up to ~100k accepted solutions per index with `num_candidates=50`. Bumping `num_candidates` to 200 trades latency for recall.

### 11.4 Memory

- Sentence-Transformers `all-MiniLM-L6-v2` resident model ≈ 90 MB.
- Per-request peak ≈ raw_log + line list + body ≈ 5–10× raw log size; with `RAW_LOG_MAX_CHARS=200000`, that's ≈ 2 MB peak per analysis.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **Fingerprint** | Deterministic hash of the filtered body + stage. Stable across cosmetic log differences. The kNN retrieval key. |
| **Filter** | The four-pass structural compression pipeline (`UniversalFilter`). Reduces raw logs by 5–10× while preserving the root cause. |
| **Detector** | Stack-specific plug-in (Java, Python, Node, Go, shell) that contributes drops, score deltas, keep marks, and `FailureLocation`s. |
| **Contribution** | Immutable record returned by a detector; merged commutatively into the line list. |
| **FailureLocation** | Structured "where did it break" — `kind ∈ {source, test, command, log, unknown}` + file/line/function/message. |
| **Confidence** | `HIGH / MEDIUM / LOW` label on a filter run. LOW prepends a banner so the LLM is told not to fabricate. |
| **kNN store** | Elasticsearch `failure_solutions` index — dense_vector with cosine similarity over fingerprint embeddings. |
| **Session** | One row in Postgres `analysis_sessions` representing one failed-stage analysis. UUID is the dashboard deep-link. |
| **Recommendation** | `fresh_analysis` vs `verified_past_solution` — set by the LangGraph route. |
| **Composition root** | The single place that wires providers — `failure_analyzer_worker/deps.py::build_deps`. |
| **Provider slot** | A `_PROVIDER` env var (`LLM_PROVIDER`, `EMBEDDING_PROVIDER`, `VECTOR_STORE_PROVIDER`, `CI_PROVIDER`) that selects a concrete implementation through a factory. |

---

**End of document.**
