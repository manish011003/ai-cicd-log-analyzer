# Jenkins failure listener (no Jenkinsfile changes)

This service detects failed Jenkins builds without requiring `post` blocks in each pipeline.
It polls Jenkins failed-build RSS, resolves failed stages via Pipeline APIs, and forwards a normalized failure payload to your Stage-2 worker endpoint.

## Why this design

- No per-job Jenkinsfile changes
- Only failure events are ingested
- Stage-level context is still recovered using Jenkins APIs
- Idempotent processing through Postgres state tracking

## Flow

1. Poll `JENKINS_BASE_URL + JENKINS_FAILED_RSS_PATH` (default `/rssFailed`)
2. Parse failed `job_full_name` and `build_number`
3. Skip if build already processed (Postgres state table)
4. Fetch:
   - Build metadata: `.../<build>/api/json`
   - Stage summary: `.../<build>/wfapi/describe`
   - Failed stage logs: `.../execution/node/<id>/wfapi/log`
5. Build one normalized event per new failed build (earliest failed stage only), then POST to `WORKER_INGEST_URL`.

By default (`WORKER_SEND_BATCH=true`) the listener sends **one HTTP request per poll** with body:

```json
{
  "failures": [
    {
      "event_type": "stage_failure",
      "jenkins_url": "https://jenkins.example.com",
      "job_full_name": "folder/job-name",
      "build_number": 123,
      "build_url": "https://jenkins.example.com/job/folder/job/job-name/123/",
      "build_result": "FAILURE",
      "timestamp": "2026-03-26T10:20:30Z",
      "correlation_id": "uuid",
      "failed_stages": [
        {
          "stage_name": "Unit Tests",
          "stage_id": "45",
          "status": "FAILED",
          "log_excerpt": "tail of failed stage log..."
        }
      ]
    }
  ]
}
```

`failed_stages` contains **at most one** stage (the chronologically first failure). Set `WORKER_SEND_BATCH=false` to POST each event separately (legacy workers).

`POST /poll-once` returns `processed`, `forwarded`, and `failures` (same objects as above) in **one JSON response**.

## Setup

1. Create virtual env and install dependencies:
   - `python -m venv .venv`
   - `.venv\\Scripts\\activate`
   - `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill values.

## Run

- Continuous listener:
  - `python run_listener.py`
- API mode (optional):
  - `uvicorn app.main:app --reload --port 8088`
  - `POST /poll-once` for manual trigger

## Operational notes

- Keep `DATABASE_URL` configured to a durable Postgres instance in production.
- Use a service Jenkins user with read-only permissions to jobs and pipeline metadata.
- Put this service behind process supervisor (systemd, container restart policy, etc).
- If RSS misses data in your Jenkins setup, you can add an API fallback poller in `JenkinsClient`.
