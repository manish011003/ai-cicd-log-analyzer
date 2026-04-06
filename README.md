# Analyzer testbench (safe, outside your repo)

This folder helps you test your listener in `analyzer_project` without modifying that repo.

## What you can validate

1. Jenkins APIs are reachable and return the expected structure.
2. Polling result from your listener (`/poll-once`).
3. Exact payload forwarded to Python worker endpoint.

## Files

- `mock_worker_server.py`: local fake worker on `http://127.0.0.1:8090`
- `jenkins_api_probe.py`: direct Jenkins API probe for one job/build
- `captured_events.jsonl`: created at runtime, stores forwarded payloads

## Test flow

### 1) Start fake worker receiver

```powershell
cd C:\Users\sohansa\analyzer_testbench
py mock_worker_server.py
```

Keep this terminal running.

### 2) Configure your listener in analyzer_project (no code edits)

In your existing `analyzer_project\jenkins_failure_listener\.env` set:

- `WORKER_INGEST_URL=http://127.0.0.1:8090/ingest/failure`
- `WORKER_INGEST_API_KEY=replace-me`
- Jenkins creds/base url as valid values

### 3) Probe Jenkins APIs directly

```powershell
cd C:\Users\sohansa\analyzer_testbench
py jenkins_api_probe.py --base-url "https://jenkins.example.com" --user "service-user" --token "your-token" --job "folder/job-name" --build 123
```

What to expect:
- `rss_failed.status_code` is `200`
- `build_api.status_code` is `200`
- `wfapi_describe.status_code` is `200` (for pipeline jobs)
- `failed_stage_logs` should include failed stage log samples when present

### 4) Start your listener API from analyzer_project

```powershell
cd C:\Users\sohansa\analyzer_project\jenkins_failure_listener
py -m uvicorn app.main:app --reload --port 8088
```

### 5) Trigger one poll

```powershell
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8088/poll-once"
```

Expected response:
- `{ processed, forwarded, failures: [...] }` — one array entry per newly forwarded build; each build includes a single `failed_stages[0]` when pipeline data exists.
- With `WORKER_SEND_BATCH=true` (default), the mock worker appends **one** JSONL line per poll (`batch: true`, top-level `failures` list), not one line per build.

### 6) Inspect exactly what worker received

```powershell
Invoke-RestMethod -Method GET -Uri "http://127.0.0.1:8090/events" | ConvertTo-Json -Depth 8
```

(`GET /events` returns **all** lines from `captured_events.jsonl`. Use `?last=5` for only the five most recent.)

```powershell
Invoke-RestMethod -Method GET -Uri "http://127.0.0.1:8090/events?last=5" | ConvertTo-Json -Depth 8
```

or open `captured_events.jsonl`.

## Notes / common issues

- If `forwarded=0` but you expected events, checkpoints may already include that build.
  - Temporarily move/delete `CHECKPOINT_FILE` used by listener and poll again.
- If `/wfapi/describe` is 404, job may not be pipeline type or plugin not available.
- If fake worker returns 401, ensure API key matches `WORKER_INGEST_API_KEY`.
