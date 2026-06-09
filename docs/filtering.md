# Feature: structural log filter

The worker never sends the raw Jenkins log to the LLM. Every failed
stage is first compressed by a four-pass **structural filter** in
`failure_analyzer_worker/filtering/` that drops scaffolding, collapses
repetition, and pins down the failure location with stack-specific
detectors. The goal is a **5-10× token reduction with the root cause
still intact**.

This page is the single source of truth for what the filter does, what
its outputs mean, and which knobs control it.

---

## The four passes

```
raw stage log
    │
    ▼
Pass 1 — tokenize.py        # strip timestamps/PIDs, classify Kind,
    │                       # rate severity, template-hash each line
    ▼
Pass 2 — structural.py      # collapse stacks, JSON blobs, progress
    │                       # bursts, and consecutive repeats
    ▼
Pass 3 — baseline.py        # drop lines that look like normal "noise"
    │                       # for this job (self-baseline today; ES-
    │                       # backed CounterSketch is a future PR)
    ▼
[detector phase]            # plug-in detectors contribute drops, score
    │                       # boosts, and structured FailureLocations
    ▼
Pass 4 — anchors.py         # score, cluster, greedily fill the token
    │                       # budget while preserving file order
    ▼
locator.py + render.py      # pick the winning FailureLocation, render
                            # the body with [METADATA] header + L<idx>
                            # citation prefixes for the LLM
```

The output is a `FilterResult` (see
`failure_analyzer_worker/filtering/base.py`) with:

- `body` — the LLM-facing string (this is what lands in
  `analysis_sessions.filtered_logs`).
- `primary_location` — `FailureLocation` (file/line, test, command, or
  `unknown`), the best-ranked candidate from all detectors.
- `confidence` — `HIGH` / `MEDIUM` / `LOW`.
- `metadata` — full telemetry (per-pass counts, detector activations,
  compression ratios, baseline version). This is what flows into
  `analysis_sessions.filter_meta` and powers the UI's *Detected*
  ribbon on each failure card and the Settings page detector list.

---

## Detectors (plug-and-play)

Detectors are **pure functions** of the tokenized line list. They never
mutate the input — they return immutable `Contribution` records
(`score_deltas`, `drop_marks`, `keep_marks`, `locations`) which the
orchestrator merges in an order-independent way. That commutativity is
what lets us add detectors freely without worrying about ordering bugs.

| Name                | Priority | What it does                                                                              |
| ------------------- | -------: | ----------------------------------------------------------------------------------------- |
| `java_stack`        | 50       | Picks the topmost project frame from Java/Kotlin/Scala stack traces (filters out framework frames such as `java.*`, `org.springframework.*`). |
| `python_traceback`  | 50       | Picks the bottom-most project frame in Python tracebacks; understands chained `during handling of` blocks. |
| `generic_shell`     | 0        | Stack-agnostic floor: finds the last command before a non-zero exit and points there.    |

Each detector exposes a one-line description (its class docstring) that
shows up on the Settings page. Adding a new detector means dropping a
file under `failure_analyzer_worker/filtering/detectors/` and listing it
in `_BUNDLED`; the UI inventory updates automatically.

### Activation flow

1. **Cheap probe** — every detector implements `activates_on(probe)`
   against the first 4 KB of the log. False positives are fine; they
   just trigger Step 2.
2. **Contribution** — the real work. Returns `None` if the cheap probe
   was a false positive (so probe noise never pollutes results).
3. The orchestrator caps active detectors at
   `FILTER_MAX_ACTIVE_DETECTORS` per run and runs them in priority
   order, so the activation cap keeps the most relevant ones.

### Confidence rules

The orchestrator labels each run as:

- **HIGH** — a surviving ERROR+ line is selected and a detector
  produced a primary location at confidence ≥ 0.6, **or** a stack-trace
  detector returned a high-confidence source/test location (the
  detectors are authoritative for their own runtimes — exception
  classes like `NoResultFound` would otherwise miss the generic
  severity gate).
- **MEDIUM** — the filter found *something* worth showing but no
  detector returned a confident location.
- **LOW** — no anchor cluster scored above the floor. The renderer
  emits a `LOW CONFIDENCE` banner plus a tail excerpt so the LLM still
  has *some* raw context to look at.

The UI maps these to the green / amber / red badge on each failure
card.

---

## Configuration

All knobs are env-var driven. The Settings page reads the live values
from the worker via `GET /filter-config`; changes still require a
worker restart.

| Variable                       | Default | Purpose                                                                                 |
| ------------------------------ | ------: | --------------------------------------------------------------------------------------- |
| `LOG_BODY_MAX_TOKENS`          | `1500`  | Soft cap on tokens emitted to the LLM. Pass 4 fills lines round-robin until exhausted.  |
| `LOG_BODY_MAX_CHARS`           | `8000`  | Hard ceiling fallback in case the token budget overshoots.                              |
| `FILTER_DETECTORS`             | `auto`  | `auto` loads every bundled detector; `none` disables them; or pass a comma-list to allowlist by name (e.g. `java_stack,generic_shell`). |
| `FILTER_MAX_ACTIVE_DETECTORS`  | `5`     | Activation ceiling per log. Detectors are sorted by priority before the cap is applied. |
| `RAW_LOG_MAX_CHARS`            | `200000`| Cap on the raw log persisted alongside each session (defence against pathological builds). |

---

## Observability — what the UI shows

### On every failure card (`/`)

The dashboard renders a *Detected by structural filter* panel under the
signature on each card with:

- **Confidence badge** (HIGH/MEDIUM/LOW).
- **Detector chips** (Java / Python / Shell ...). The shell floor is
  hidden when stack-specific detectors also fired — it would just be
  noise.
- **Compression ratio** (`raw_chars / body_chars`) and approximate
  token count so operators can see how much the filter saved.
- **Primary location** (e.g. `PaymentClient.processPayment @
  PaymentClient.java:84 -- ConnectException: Connection refused`).

These fields come from `ProcessedStage.filter_meta` (the web-backend's
projection of the worker's `metadata` dict).

### On the Settings page (`/settings`)

Three sections, all read-only:

1. **Tweakable knobs** — current values + the env var that controls
   each.
2. **Active detectors** — what's loaded right now, with priority and
   one-line description.
3. **All bundled detectors** — full catalog with an "active/disabled"
   indicator so you can see what `FILTER_DETECTORS` excluded.

---

## HTTP endpoints

### Worker (`failure_analyzer_worker`)

| Method | Path             | Purpose                                                                                     |
| ------ | ---------------- | ------------------------------------------------------------------------------------------- |
| GET    | `/filter-config` | JSON snapshot of the live filter settings + detector inventory. Requires `X-API-Key`.       |

`/ingest/failure` also includes a `filter_meta` field on each result
item so the web-backend can persist it.

### Web backend (`web/backend`)

| Method | Path                | Purpose                                                            |
| ------ | ------------------- | ------------------------------------------------------------------ |
| GET    | `/api/filter-config`| Cached proxy in front of the worker's `/filter-config` (60 s TTL). |

Sessions stored via `POST /api/sessions` carry `filter_meta` (JSONB)
through to Postgres, where the dashboard's `/api/results` endpoint
surfaces a trimmed projection on each `ProcessedStage`.

---

## Persistence

```sql
ALTER TABLE analysis_sessions
  ADD COLUMN IF NOT EXISTS filter_meta JSONB NOT NULL DEFAULT '{}'::jsonb;
```

The migration is applied automatically on web-backend startup
(`web/backend/app/db.py::SCHEMA_SQL`). Legacy rows simply carry `{}` —
the UI treats an empty object as "no telemetry" and hides the
*Detected* panel.

---

## Adding a new detector

1. Drop a module under
   `failure_analyzer_worker/filtering/detectors/<your_name>.py`.
2. Define a class with `name`, `priority`, `activates_on`, and
   `contribute`. The class docstring's first line becomes the
   description shown on the Settings page.
3. Call `register(YourDetector())` at module scope.
4. Add `"<your_name>"` to `_BUNDLED` in `detectors/__init__.py`.

Unit tests live under `failure_analyzer_worker/tests/` — see
`test_filtering_universal.py` for examples that exercise the
end-to-end `FilterResult` shape (locations, confidence, body).

---

## Where to look next

- Module-level docstrings in `failure_analyzer_worker/filtering/` —
  every pass explains its responsibility at the top of the file.
- `failure_analyzer_worker/tests/test_filtering_universal.py` — pinned
  behaviour the filter must preserve.
- `docs/failure-analyzer-worker.md` — how the filter fits into the
  worker's broader request lifecycle.
