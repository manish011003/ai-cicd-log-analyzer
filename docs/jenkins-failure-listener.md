# Feature: jenkins_failure_listener

The listener is the bridge between Jenkins and the analyzer worker. It detects
failed builds without requiring `post` blocks in any Jenkinsfile, normalizes
them into a single payload shape, and dispatches them to the worker.

The original deep-dive lives at
[`jenkins_failure_listener/ingestion.md`](../jenkins_failure_listener/ingestion.md);
this file is the operator-facing summary.

## What it does

1. Polls `JENKINS_BASE_URL + JENKINS_FAILED_RSS_PATH` (default `/rssFailed`)
   on `POLL_INTERVAL_SECONDS`.
2. Parses every entry into `(job_full_name, build_number)` and skips builds
   already recorded in the Postgres state table — guaranteeing
   exactly-once dispatch.
3. For each new failure, calls Jenkins:
   - `<build>/api/json` for build metadata,
   - `<build>/wfapi/describe` for the pipeline stage list,
   - `<build>/execution/node/<id>/wfapi/log` for the failed stage log.
4. Strips HTML, removes Jenkins time prefixes, drops noise lines, and
   truncates head + tail to fit `MAX_LOG_CHARS`.
5. Builds one `stage_failure` event per build (the chronologically first
   failed stage) and POSTs them as a single `failures: [...]` batch (or one
   request per event when `WORKER_SEND_BATCH=false`).

## Two run modes

| Mode      | Entry point                    | HTTP exposed?    | Use it when…                                                        |
| --------- | ------------------------------ | ---------------- | ------------------------------------------------------------------- |
| API       | `uvicorn app.main:app`         | yes (`:8088`)    | The web UI must be able to trigger `POST /poll-once`                |
| Headless  | `python run_listener.py`       | no               | You only need the background polling loop (e.g. ops-only deployment) |

In Docker Compose, the default `listener` service runs in API mode; the
optional `listener-poller` service (profile `poll`) runs the headless loop in
parallel for jobs that prefer continuous polling.

## Logic flow

```
┌──────────────┐   poll   ┌────────────────────────────┐
│ run_listener │ ───────► │ JenkinsClient.poll_failed  │
│ /poll-once   │          │  → parses RSS, dedupes via │
│  endpoint    │          │   Postgres state store     │
└──────┬───────┘          └────────────┬───────────────┘
       │                               │ new failures
       │                               ▼
       │                  ┌────────────────────────────┐
       │                  │ resolve failed stages      │
       │                  │ (wfapi/describe + log)     │
       │                  └────────────┬───────────────┘
       │                               │
       │                               ▼
       │                  ┌────────────────────────────┐
       │                  │ Dispatcher.send_batch()    │
       │                  │ POST  worker /ingest/...   │
       │                  └────────────────────────────┘
       ▼
returns { processed, forwarded, failures: [...] }
```

## Public HTTP API (when running with uvicorn)

| Method | Path           | Purpose                                                        |
| ------ | -------------- | -------------------------------------------------------------- |
| POST   | `/poll-once`   | Run one full poll cycle and return the dispatched events       |
| GET    | `/health`      | Liveness probe — `{ "status": "ok" }`                          |

`POST /poll-once` is what the web-backend proxies on
`/api/listener/poll-once` for the dashboard “Refresh / Auto-poll” buttons.

## Environment

Defined in `jenkins_failure_listener/.env.example`. Highlights:

| Variable                  | Default                                       | Notes                                                  |
| ------------------------- | --------------------------------------------- | ------------------------------------------------------ |
| `DATABASE_URL`            | (required)                                    | Postgres connection string for the state table         |
| `JENKINS_BASE_URL`        | (required)                                    | e.g. `http://host.docker.internal:8080` from a container |
| `JENKINS_USER` / `_TOKEN` | (required)                                    | Read-only Jenkins user is enough                       |
| `JENKINS_FAILED_RSS_PATH` | `/rssFailed`                                  | Override only if you proxy Jenkins on a subpath        |
| `POLL_INTERVAL_SECONDS`   | `20`                                          | How often to poll the RSS                              |
| `WORKER_INGEST_URL`       | (required)                                    | `http://worker:8090/ingest/failure` in Compose         |
| `WORKER_INGEST_API_KEY`   | (required, must match worker `WORKER_API_KEY`)|                                                        |
| `WORKER_SEND_BATCH`       | `true`                                        | Set `false` for legacy workers expecting one event per request |
| `STATE_RETENTION_DAYS`    | `30`                                          | Auto-purges the Postgres state table                   |

## Run it independently

### 1. Local Python (API mode)

```bash
cd jenkins_failure_listener
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --host 127.0.0.1 --port 8088
```

### 2. Local Python (headless poller)

```bash
cd jenkins_failure_listener
python run_listener.py
```

### 3. Docker

```bash
docker compose up -d --build listener
curl http://localhost:8088/health
```

To enable the headless poller alongside the API:

```bash
docker compose --profile poll up -d listener-poller
```

## Test it

The pytest suite covers all pure-function helpers (no HTTP, no Postgres):

```bash
pytest jenkins_failure_listener/tests -q
```

Manual end-to-end check (assumes the worker is up and the API key matches):

```bash
curl -X POST http://localhost:8088/poll-once
```

You should see a JSON response with `processed`, `forwarded`, and
`failures: [...]` fields. The worker’s logs will show the matching
`Received N event(s) from listener` line.
