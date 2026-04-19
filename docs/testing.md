# Testing guide

The repository ships with three offline pytest suites — one per Python
service — and a TypeScript type-check for the frontend. Together they form a
fast, dependency-free CI gate that does **not** require Docker, Postgres,
Elasticsearch, Jenkins, or the LLM to run.

## Suites at a glance

| Suite                              | What it covers                                                          |
| ---------------------------------- | ----------------------------------------------------------------------- |
| `failure_analyzer_worker/tests`    | Log normalization, filtering, fingerprint generation, ES client reset   |
| `jenkins_failure_listener/tests`   | RSS title parsing, HTML stripping, log truncation, error scoring        |
| `web/backend/tests`                | `/health` endpoint, db-target parsing, error-class extraction, helpers   |
| `web/frontend` (`tsc --noEmit`)    | Strict TypeScript type-check across the entire UI                       |

All Python tests deliberately avoid network calls: they monkey-patch
`app.db.init_schema`, set placeholder env vars, and exercise pure functions.

## Run everything

The fastest way (assumes one venv with `pytest`, `psycopg`, and the worker’s
dependencies installed):

```bash
pytest --import-mode=importlib ^
       failure_analyzer_worker/tests ^
       jenkins_failure_listener/tests ^
       web/backend/tests -q
```

`--import-mode=importlib` is required because all three services name their
test directory `tests/`; without it pytest’s default rootdir-import mode
collides on the package name.

## Run one suite

```bash
pytest failure_analyzer_worker/tests   -q
pytest jenkins_failure_listener/tests  -q
pytest web/backend/tests               -q
```

## Frontend type-check

```bash
cd web/frontend
npx tsc --noEmit -p tsconfig.json
```

## Smoke-test in Docker

After `docker compose up -d`, verify each service is healthy:

```bash
curl http://localhost:8090/health      # worker
curl http://localhost:8088/health      # listener
curl http://localhost:8095/health      # web-backend
curl -I http://localhost:3080          # frontend (expect HTTP/1.1 200)
```

To exercise the full pipeline end-to-end without a real Jenkins, POST a
synthetic event straight to the worker:

```bash
curl -X POST http://localhost:8090/ingest/failure ^
  -H "Content-Type: application/json" ^
  -H "X-API-Key: %WORKER_API_KEY%" ^
  -d "{ \"job_full_name\": \"demo\", \"build_number\": 1, \"failed_stages\": [ { \"stage_name\": \"Build\", \"log_excerpt\": \"ERROR: Connection refused\" } ] }"
```

Then refresh the dashboard at http://localhost:3080 — a new session card
should appear with a generated analysis and suggested fix.

## CI suggestions

A minimal CI matrix worth running on every PR:

1. `pip install -r failure_analyzer_worker/requirements.txt pytest psycopg`
2. `pytest --import-mode=importlib failure_analyzer_worker/tests jenkins_failure_listener/tests web/backend/tests -q`
3. `cd web/frontend && npm ci && npx tsc --noEmit -p tsconfig.json`
4. `docker compose build` (does not start services; catches Dockerfile drift)
