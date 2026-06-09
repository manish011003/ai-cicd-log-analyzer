# Feature: failure_analyzer_worker

The worker is a FastAPI service that receives failed-build events from the
Jenkins listener, runs an LLM-driven analysis, and stores accepted solutions
in Elasticsearch for future kNN retrieval.

## What it does

1. Accepts one or many `stage_failure` events on `POST /ingest/failure`.
2. For each failed stage, runs the four-pass structural log filter
   (`failure_analyzer_worker/filtering/`) with stack-specific detectors
   for Java, Python, and shell. See [`filtering.md`](filtering.md) for
   the full reference.
3. Searches Elasticsearch for the most similar previous failure
   fingerprints (cosine kNN over `all-MiniLM-L6-v2` embeddings).
4. Invokes a LangGraph state machine
   (`failure_analyzer_worker/graph.py`) that calls Groq
   (`llama-3.3-70b-versatile`) to produce:
   - a structured root-cause analysis,
   - a concrete suggested fix,
   - and a `recommendation` (`fresh_analysis` vs reuse a known fix).
5. POSTs a “session” to the web-backend so the UI gets a stable deep-link
   per analysis (`http://<ui>/?session=<uuid>`).
6. On accepted solutions, exposes `POST /store-solution` so the dashboard
   can persist verified fixes back into Elasticsearch (the fingerprint is
   the retrieval key, so we intentionally do not persist the raw log
   excerpt in ES — raw logs live in Postgres with their session).

## Logic flow

```
POST /ingest/failure
        │
        ▼
LogProcessor.process(raw_logs)        ──► [METADATA SUMMARY] + filtered excerpt
        │
        ▼
generate_fingerprint(filtered, stage) ──► stable signature
        │
        ▼
es_search(fingerprint, k=5)           ──► [(score, solution, fingerprint), ...]
        │
        ▼
LangGraph: route → analyze → suggest  ──► (analysis, suggested_fix, recommendation)
        │
        ▼
POST web-backend /api/sessions        ──► UI deep-link
        │
        ▼
return { results: [...] }             ──► back to listener for logging
```

## Public HTTP API

| Method | Path                | Purpose                                                              |
| ------ | ------------------- | -------------------------------------------------------------------- |
| POST   | `/ingest/failure`   | Single event or `{"failures": [...]}` batch from the listener        |
| POST   | `/store-solution`   | Persist an accepted fix into Elasticsearch (kNN training data)       |
| POST   | `/chat/turn`        | One conversational turn for the in-UI assistant                      |
| GET    | `/filter-config`    | Read-only snapshot of the live structural log filter (see [filtering.md](filtering.md)) |
| GET    | `/knowledge-graph`  | Materialised knowledge graph of accepted solutions                   |
| GET    | `/health`           | Liveness probe — returns models / index info                         |

All endpoints require the `X-API-Key` header to match `WORKER_API_KEY`.

`/ingest/failure` response items include a `filter_meta` field (detected
stack, confidence, primary location, compression stats) so the
web-backend can persist it and the UI can render the *Detected* panel.

## Environment

The worker reads its config from `failure_analyzer_worker/.env` (see
`failure_analyzer_worker/.env.example`). The variables that matter most:

| Variable               | Default                             | Notes                                                    |
| ---------------------- | ----------------------------------- | -------------------------------------------------------- |
| `WORKER_API_KEY`       | (required)                          | Shared with listener and web-backend                     |
| `GROQ_API_KEY`         | (required)                          | Free key at https://console.groq.com/keys                |
| `LLM_MODEL`            | `llama-3.3-70b-versatile`           | Any Groq-hosted Llama model works                        |
| `LLM_TLS_VERIFY`       | `1`                                 | Set `0` only behind a corporate MITM proxy (logs warn)   |
| `ELASTICSEARCH_URL`    | `http://elasticsearch:9200`         | In Compose; `http://localhost:9200` for local runs       |
| `ELASTICSEARCH_INDEX`  | `failure_solutions`                 | kNN index for accepted fixes                             |
| `EMBEDDING_MODEL`      | `all-MiniLM-L6-v2`                  | Sentence-Transformers model id                           |
| `WEB_UI_API_URL`       | `http://web-backend:8095`           | Where to POST sessions for the UI deep-link              |

## Run it independently

### 1. Local Python

```bash
cd failure_analyzer_worker
python -m venv .venv
.venv\Scripts\activate            # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env            # then edit GROQ_API_KEY, WORKER_API_KEY
python -m failure_analyzer_worker # listens on 0.0.0.0:8090
```

You will need a reachable Elasticsearch (`docker run -p 9200:9200
docker.elastic.co/elasticsearch/elasticsearch:8.12.0` is enough). The
worker will silently skip ES bootstrap if ES is unreachable — see
`worker.py` lifespan log.

### 2. Docker

```bash
docker compose up -d --build worker elasticsearch
curl http://localhost:8090/health
```

## Test it

The worker’s pytest suite is fully offline (no ES, no LLM, no network):

```bash
pytest failure_analyzer_worker/tests -q
```

To poke the live endpoint with a synthetic event:

```bash
curl -X POST http://localhost:8090/ingest/failure ^
  -H "Content-Type: application/json" ^
  -H "X-API-Key: %WORKER_API_KEY%" ^
  -d "{ \"job_full_name\": \"demo\", \"build_number\": 1, \"failed_stages\": [ { \"stage_name\": \"Build\", \"log_excerpt\": \"ERROR: Connection refused\" } ] }"
```

You should see a 200 with a `results[]` array containing
`fingerprint`, `analysis`, and `suggested_fix`. If `WEB_UI_API_URL` is set
the response is also reflected as a session in the dashboard.
