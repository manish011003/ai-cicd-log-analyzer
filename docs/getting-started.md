# Getting started — deploy and run the project end-to-end

A linear walkthrough: from a freshly cloned repo to seeing your first
Jenkins failure analysed in the dashboard and an accepted solution
indexed back into Elasticsearch.

If you only want the 30-second overview, read
[`architecture.md`](architecture.md) first. For per-service deep-dives
follow the links in [`docs/README.md`](README.md). This guide is the
runbook that ties them together.

---

## 0. Prerequisites

| What | Why | Quick check |
|---|---|---|
| **Docker Desktop** with Compose v2 (Linux engine) | Runs every service | `docker compose version` ≥ 2.x |
| **8 GB free RAM** | Elasticsearch + worker + listener + UI | Task Manager / `free -h` |
| **A Jenkins controller** with the Pipeline + RSS feeds enabled | Source of failed builds | `curl -u user:token https://your-jenkins/rssFailed` returns Atom XML |
| **A Jenkins API token** for that user | Listener auth | Jenkins → *People* → your user → *Configure* → *Add new token* |
| **A Groq API key** (default LLM provider) | Generates RCAs | `https://console.groq.com/keys` |
| **Free TCP ports** `3080, 8095, 8090, 8088, 9200, 5601, 5432` | Default publish ports | See [Port hijack troubleshooting](#port-9200-not-reaching-the-elasticsearch-container) |

> Windows: WSL2 backend is required. Don't mix Hyper-V backend +
> WSL2 backend on the same machine — port forwarding gets weird.

---

## 1. Clone and configure the environment file

```bash
git clone https://github.com/manish011003/ai-cicd-log-analyzer.git
cd ai-cicd-log-analyzer
```

```bash
cp .env.example .env                         # Linux / macOS
```

```powershell
Copy-Item .env.example .env                  # Windows PowerShell
```

Edit `.env` and set, at minimum, these four values:

| Variable | Value |
|---|---|
| `GROQ_API_KEY` | Your Groq API key (`gsk_…`). |
| `WORKER_API_KEY` | Any shared secret string (worker ↔ listener ↔ web-backend use it as `X-API-Key`). Treat as a password. |
| `JENKINS_BASE_URL` | URL the **listener container** uses to reach Jenkins. Default `http://host.docker.internal:8080` works for Jenkins running on the same host. |
| `JENKINS_USER` / `JENKINS_API_TOKEN` | Credentials for the API token you minted above. |

Optional knobs you'll likely care about later:

- `LLM_PROVIDER` — `groq` (default) / `openai` / `anthropic` / `ollama`.
  Different providers also read `LLM_API_KEY` / `LLM_API_BASE`.
- `POLL_INTERVAL_SECONDS` — how often the listener checks Jenkins.
- `MAX_BUILDS_PER_POLL` — throttle if your `/rssFailed` returns hundreds
  of historical builds on first run.
- `WEB_FRONTEND_PUBLISH_PORT`, `WORKER_PUBLISH_PORT`, … — change any host
  port if the default conflicts with something on your machine.

> Never commit `.env`. The repo's `.gitignore` already excludes it, but
> double-check after `git status`.

---

## 2. Bring up the full stack

```bash
docker compose up -d --build
```

First run downloads Postgres / Elasticsearch / Kibana / Node images and
builds the four custom service images (`worker`, `listener`,
`web-backend`, `web-frontend`). Expect 5–15 minutes on a cold cache;
subsequent rebuilds are near-instant.

Verify everything is up:

```bash
docker compose ps
```

You should see all of: `analyzer-worker`, `analyzer-listener`,
`analyzer-web-backend`, `analyzer-web-frontend`, `elastic-test`,
`jenkins-listener-postgres`, `kibana` — all `Up` with `0.0.0.0:<port>->…`
in the **Ports** column.

> If `elastic-test` shows up without a host port mapping
> (just `9200/tcp`), see
> [Port hijack troubleshooting](#port-9200-not-reaching-the-elasticsearch-container).

---

## 3. Smoke-test each service

```bash
curl http://localhost:8090/health           # worker
curl http://localhost:8088/health           # listener
curl http://localhost:8095/health           # web-backend
curl http://localhost:9200/_cluster/health  # Elasticsearch
```

Each should return JSON with `"status": "ok"` (or, for Elasticsearch,
`"status": "yellow"` or `"green"`). The worker `/health` payload also
echoes which LLM, embedder, and vector store it has been configured with
— a fast way to confirm your `.env` made it into the container.

Open the dashboard in a browser:

- **Web UI**: <http://localhost:3080>
- **Kibana** (optional): <http://localhost:5601>

The dashboard will be empty until the listener processes its first
failure (next step).

---

## 4. Feed Jenkins failures into the pipeline

There are two ways to make the listener pick up a build.

### 4.a Manual one-shot poll (recommended for the first run)

```bash
curl -X POST http://localhost:8088/poll-once
```

This forces the listener to:

1. Hit Jenkins `/rssFailed` and read the latest failed builds.
2. For each, call `…/wfapi/describe` to enumerate the pipeline's stages.
3. Identify root-cause failures using the **flow-graph parallel-block
   detector** (with a time-overlap fallback) — see
   [`jenkins-failure-listener.md`](jenkins-failure-listener.md) for the
   algorithm.
4. Pull stage logs via `…/wfapi/log`, strip HTML / timestamps / noise,
   and crop to `MAX_STAGE_LOG_CHARS`.
5. Dedupe against Postgres (`processed_builds` table) so we don't
   reprocess builds we've already seen.
6. POST the normalised payload to the worker's `/ingest/failure`.

Tail the listener while you call it:

```bash
docker compose logs -f listener
```

### 4.b Continuous polling

The default `listener` service runs in **API mode** (you POST
`/poll-once` to trigger). For a long-running poller without HTTP, opt in
to the dedicated profile:

```bash
docker compose --profile poll up -d listener-poller
```

The poller container runs `python run_listener.py` in a loop with
`POLL_INTERVAL_SECONDS` between cycles. Logs:
`docker compose logs -f listener-poller`.

---

## 5. Watch the worker analyse the failure

In another terminal:

```bash
docker compose logs -f worker
```

For every failed stage the listener forwards, the worker's LangGraph
pipeline:

1. **`filter_logs`** — strips boilerplate, scores error anchors, keeps a
   compact excerpt + raw tail.
2. **`generate_fingerprint`** — builds a semantic key from typed
   exceptions / stack frames.
3. **`search_solutions`** — embeds the fingerprint
   (`sentence-transformers/all-MiniLM-L6-v2` by default) and runs a kNN
   query against the `failure_solutions` Elasticsearch index. Hits
   above `SIMILARITY_THRESHOLD` short-circuit the LLM call.
4. **`analyze_fresh`** or **`reuse_with_context`** — Groq (or whichever
   `LLM_PROVIDER` you picked) returns an RCA + suggested fix. Prompts
   live under `failure_analyzer_worker/prompts/templates/` and can be
   overridden by mounting a directory and setting `PROMPTS_DIR`.
5. **`register_web_session`** — POSTs the result to the web-backend so
   the user has a stable URL like
   `http://localhost:3080/?session=<uuid>`. The worker prints that URL
   to its log on success.

The worker also persists the raw + filtered logs alongside the analysis
in Postgres via the web-backend so the chat panel can refer to them
later.

---

## 6. Use the dashboard

Open <http://localhost:3080>. Each failed stage appears as a card with:

- Job, build number, stage name, and detected error class.
- The LLM-generated **analysis** and **suggested fix**.
- A **chat panel** for follow-up clarifications (uses the same model;
  toggle *"Include raw log excerpt"* to give the LLM more context).
- **Accept** / **Reject** buttons and a "Reset feedback" link.

Click into a card to see filtered + raw logs, the matched past solution
(if any), and any similar past failures.

### Accepting a solution

1. Click **Accept** on a card.
2. The frontend POSTs `/api/sessions/<id>/feedback` to the web-backend.
3. The web-backend forwards to the worker's `/store-solution`, which
   indexes a new document in `failure_solutions` keyed by the Postgres
   session id (so re-clicks are idempotent — no duplicate kNN rows).

Verify it landed:

```bash
curl 'http://localhost:9200/failure_solutions/_count'
curl 'http://localhost:9200/failure_solutions/_search?pretty&size=2'
```

The next time the worker sees a fingerprint close to this one, it will
reuse the accepted fix instead of calling the LLM from scratch.

### Rejecting a solution

Same flow without the ES write; the session is marked `rejected` and
locked from further changes. Reset via the database if you need to
re-evaluate.

---

## 7. Inspect the data stores

| Store | Where | Useful command |
|---|---|---|
| Postgres (sessions, chat, processed-builds) | `jenkins-listener-postgres` container | `docker compose exec postgres psql -U postgres -d jenkins_listener -c '\dt'` |
| Elasticsearch (accepted solutions) | `elastic-test` container | `curl http://localhost:9200/_cat/indices?v` |
| Worker stage logs (raw / filtered, capped) | Postgres `sessions.raw_logs`, `filtered_logs` | `SELECT id, job_full_name, length(raw_logs) FROM sessions LIMIT 5;` |

Kibana on <http://localhost:5601> is a friendlier way to browse the
`failure_solutions` index — *Stack Management → Index Management → Discover*.

---

## 8. Day-2 operations

### Tail one service

```bash
docker compose logs -f worker             # or listener, web-backend, web-frontend
```

### Rebuild after editing a single Dockerfile

```bash
docker compose build worker
docker compose up -d worker
```

### Recreate a data service after changing its compose ports

`docker compose up -d` won't always re-bind ports on already-running
containers. After editing `ports:` on `elasticsearch`, `kibana`, or
`postgres`:

```bash
docker compose up -d --force-recreate elasticsearch kibana postgres
```

### Reset everything (DESTRUCTIVE)

```bash
docker compose down -v          # ⚠ drops Postgres + ES data + stored fixes
docker compose up -d --build
```

### Manually trigger one analysis cycle

```bash
curl -X POST http://localhost:8088/poll-once
```

### Stop / start the whole stack

```bash
docker compose stop             # stop containers, keep volumes
docker compose start            # bring them back without rebuild
```

### Run the test suites

```bash
pytest --import-mode=importlib \
  failure_analyzer_worker/tests \
  jenkins_failure_listener/tests \
  web/backend/tests -q
```

Frontend type-check:

```bash
cd web/frontend && npx tsc --noEmit -p tsconfig.json
```

Both are fully offline (no LLM / ES / Postgres needed).

---

## 9. Troubleshooting

### Worker `/health` returns `provider: groq` but every analysis fails with `401`

`GROQ_API_KEY` (or `LLM_API_KEY`) isn't reaching the container. Check
that `.env` has the key, then `docker compose up -d worker` to re-roll
the env. Confirm with:

```bash
docker compose exec worker printenv GROQ_API_KEY
```

### Listener logs show `connection refused` against Jenkins

The container can't reach the URL in `JENKINS_BASE_URL`. From inside the
listener:

```bash
docker compose exec listener wget -qO- http://host.docker.internal:8080
```

`host.docker.internal` only resolves on Docker Desktop; on Linux servers
either run Jenkins in the same compose project (use the `jenkins`
profile) or point at the LAN/DNS address.

### Dashboard says "No sessions yet" after `poll-once`

Either:

1. Jenkins genuinely has no failed builds since you started, or
2. The listener is dedup'ing builds it processed earlier. Reset its
   state: `docker compose exec postgres psql -U postgres -d
   jenkins_listener -c 'TRUNCATE processed_builds;'`, then re-poll.

### Port 9200 not reaching the Elasticsearch container

Symptom: `curl http://localhost:9200/_cat/indices?v` answers, but the
node name doesn't match `docker exec elastic-test hostname`.

Cause: another process (often a stray ES inside a WSL2 distro)
is bound to host port 9200, so Compose silently dropped the port
mapping. Verify with:

```bash
docker port elastic-test                   # shows nothing → not bound
docker exec elastic-test curl -s http://localhost:9200/_cluster/health
```

Fix: stop the rogue service holding 9200, then
`docker compose up -d --force-recreate elasticsearch`. On Windows,
check WSL: `wsl -e bash -c "ps -ef | grep -i elastic | grep -v grep"`.

### `Execute in parallel` builds report only one failed sibling

You hit the bounded edge-scan budget. The default
`PARALLEL_BLOCK_EDGE_SCAN_IDS=8` covers typical Jenkins layouts; very
deep parallel branches with many step nodes between siblings can
require raising it. Try `PARALLEL_BLOCK_EDGE_SCAN_IDS=16` in `.env` and
restart the listener. The slack backup
(`PARALLEL_STAGE_OVERLAP_MS=2000`) will catch siblings the primary path
gives up on; tune higher if your agents take > 2 s to spin up between
parallel branches.

### Browser shows "Failed to fetch" on every UI action

Usually a CORS mismatch. The default `CORS_ORIGINS=*` should be fine;
if you tightened it, make sure the host (`http://localhost:3080`) is in
the list and rerun `docker compose up -d web-backend`.

---

## 10. Optional profiles

| Profile | Command | When to use |
|---|---|---|
| `poll` | `docker compose --profile poll up -d` | Long-running background polling instead of API mode. |
| `dev-ui` | `docker compose --profile dev-ui up -d` | Bind-mounted Next.js dev server on `5173` for hot reload. |
| `jenkins` | `docker compose --profile jenkins up -d` | Stand up an in-cluster Jenkins (`my-jenkins:2.541.1`) for end-to-end demos without an external controller. |

---

## What to read next

- [`architecture.md`](architecture.md) — service map and request lifecycle.
- [`jenkins-failure-listener.md`](jenkins-failure-listener.md) — RSS
  polling, wfapi traversal, parallel-block detection.
- [`failure-analyzer-worker.md`](failure-analyzer-worker.md) — LangGraph
  pipeline, prompt slots, vector-store contract.
- [`web-backend.md`](web-backend.md) — sessions / chat / feedback
  endpoints, idempotency.
- [`web-frontend.md`](web-frontend.md) — dashboard data model and
  feedback flow.
- [`docker.md`](docker.md) — compose reference and per-service Dockerfile conventions.
- [`testing.md`](testing.md) — running every test suite locally and in CI.
