# Stabilization notes (this engagement)

This file records the bugs introduced since the last known-green commit, the
fix that was applied, and the test coverage that was added so the regression
cannot silently come back. It is intentionally short — the goal is a paper
trail, not prose.

## Bugs fixed

| # | Symptom                                                                                       | Root cause                                                                  | Fix                                                                                                        |
| - | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| 1 | Worker booted but raised `KeyError: GROQ_API_KEY` when the example env was used as-is         | `failure_analyzer_worker/.env.example` still listed `GOOGLE_API_KEY`        | Rewrote the LLM section to declare `GROQ_API_KEY` + `LLM_MODEL=llama-3.3-70b-versatile`                    |
| 2 | `DeprecationWarning: on_event is deprecated` on every worker boot                             | `failure_analyzer_worker/worker.py` used `@app.on_event("startup")`         | Migrated to `@asynccontextmanager`-based `lifespan` and passed it to `FastAPI(lifespan=...)`               |
| 3 | LLM HTTPS calls silently bypassed certificate validation                                      | `failure_analyzer_worker/graph.py` hard-coded `httpx.Client(verify=False)`  | Added `LLM_TLS_VERIFY` setting (default `1`); explicit `0`/`false`/`no`/`off` to disable, with WARN log    |
| 4 | Importing `failure_analyzer_worker.__main__` started a Uvicorn server as a side effect        | Top-level `uvicorn.run(...)` without an entry-point guard                   | Wrapped in `def main()` + `if __name__ == "__main__": main()`                                              |
| 5 | `.next/dev/*` build artifacts kept reappearing in git diffs                                   | `.gitignore` only covered `.next/` (the production build dir)               | Added `.next/dev/`, `.turbo/`, `*.tsbuildinfo`, `next-env.d.ts`, and removed the cached files via `git rm` |
| 6 | Docker images included megabytes of dev artifacts (e.g. `.venv*`, `.next/dev`, IDE metadata)  | Sparse `.dockerignore`                                                      | Rewrote `.dockerignore` to comprehensively exclude venvs, build dirs, IDE/OS metadata, all `.env*`         |
| 7 | Web backend always allowed `*` for CORS even when `CORS_ORIGINS` was set                      | `web/backend/app/main.py` hard-coded `allow_origins=["*"]`                  | Reads `settings.cors_origins` (comma-separated) and falls back to `*` only when unset                      |
| 8 | `docker compose build web-frontend` failed with `Failed to fetch Inter from Google Fonts`     | `next/font/google` fetches WOFF2 from `fonts.googleapis.com` at build time, blocked by corporate / Docker network | Self-hosted Inter + JetBrains Mono variable WOFF2 under `web/frontend/app/fonts/`, switched `app/layout.tsx` to `next/font/local` |

## Tests added

| Suite                                                  | Files                                                                                                |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| Worker pipeline (offline)                              | `failure_analyzer_worker/tests/test_log_processor.py`                                                |
| Listener parsing helpers (offline)                     | `jenkins_failure_listener/tests/test_jenkins_client_parsing.py`                                      |
| Web-backend `/health` + helpers (offline, TestClient)  | `web/backend/tests/test_main_health.py`                                                              |

All three suites pass with one command:

```bash
pytest --import-mode=importlib ^
       failure_analyzer_worker/tests ^
       jenkins_failure_listener/tests ^
       web/backend/tests -q
```

The `--import-mode=importlib` flag is required because every service names
its test folder `tests/` and pytest’s default rootdir mode collides on the
package name. The `web/backend` test additionally evicts any cached `app.*`
modules from `sys.modules` so it does not import the listener’s `app`
package by accident.

## Documentation added

- `docs/architecture.md` — service map + request lifecycle
- `docs/folder-structure.md` — proposed hierarchy + rationale
- `docs/failure-analyzer-worker.md` — feature deep-dive
- `docs/jenkins-failure-listener.md` — feature deep-dive
- `docs/web-backend.md` — feature deep-dive
- `docs/web-frontend.md` — feature deep-dive
- `docs/docker.md` — Compose / Docker reference
- `docs/testing.md` — how to run every suite
- `docs/stabilization-notes.md` — this file

## Files NOT changed

To honor the “do not delete existing logic” constraint, the following were
deliberately left in place even though they are partially redundant with
the new top-level `.env.example`:

- `docker-compose.env.example` (still works, marked DEPRECATED in a header
  comment)
- `failure_analyzer_worker/.env.example`,
  `jenkins_failure_listener/.env.example`,
  `web/backend/.env.example` (per-service templates kept for users who run
  a single service outside Docker)
- `failure_analyzer_worker/try_filter.py` (developer CLI for filter tuning)
- `jenkins_failure_listener/ingestion.md` (legacy long-form doc, linked from
  `docs/jenkins-failure-listener.md`)
