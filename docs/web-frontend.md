# Feature: web/frontend

A Next.js 16 (App Router, React 19, Tailwind 4) dashboard that visualizes the
analyzer’s output, lets users chat with the LLM about a specific failure, and
records accept/reject feedback that becomes future training data.

## What it does

The UI is split into two pages: a **main Overview dashboard** (`/`) and a
focused **Root Cause Analysis** page (`/rca?run=<run_id>`).

**Overview (`/`)** — the landing page, for scanning the fleet of failures:

- Shows a summary **matrix** (total failures, accepted, rejected, unresolved)
  plus an **error-class breakdown**, all derived from the currently-filtered
  set.
- A **filter bar**: free-text search (job / stage / error class / signature),
  date-range presets (Today / Yesterday / This Week / Last Week) or a custom
  From–To range, and a status filter (unresolved / accepted / rejected). The
  matrix, the breakdown badges, and the table all stay in sync with the same
  predicate.
- A **table of failed stages**, grouped by build (newest first). Clicking a
  row opens that failure's RCA page.
- An **Overview ⇄ Knowledge Map** toggle. The Knowledge Map visualizes the
  accepted-solutions corpus and its similarity links.
- A **Refresh / Auto-poll** control that proxies to the listener.

**Root Cause Analysis (`/rca?run=<run_id>`)** — the drill-in for one failure:

- Renders the LangGraph output: analysis, suggested fix, filtered log excerpt.
- Surfaces the structural filter's *Detected* panel — confidence badge,
  activated detector chips, primary file:line anchor, and compression ratio.
  See [`filtering.md`](filtering.md).
- Hosts the conversational assistant (`ChatPanel`).
- Lets the user accept or reject the suggested fix; accepting triggers the
  backend to write the solution to Elasticsearch via the worker.
- A **Back to dashboard** button returns to the Overview.

**Settings (`/settings`)** — read-only view of the live filter configuration:
detector inventory, token/character budgets, and the env var that controls
each knob. Changes still require editing `.env` and restarting the worker.

Throughout the UI, long explainer paragraphs have been replaced with hover
**ⓘ info icons** (`components/ui/info-hint.tsx`) to keep the surface clean.

## Logic flow

```
/  (Overview)
        │
        ▼
app/page.tsx ──► <MainDashboard>
        │            │  fetch ──► web-backend GET /api/results   (failed stages)
        │            │  fetch ──► web-backend GET /api/diagnostics
        │            │  poll  ──► web-backend POST /api/listener/poll-once
        │            ├─► <StatsBar>        matrix + error breakdown (client-derived)
        │            ├─► <FilterBar>       search · date range · status
        │            ├─► <FailuresTable>   build-grouped rows → /rca?run=<run_id>
        │            └─► <KnowledgeMap>    GET /api/knowledge-graph (toggle)
        ▼
/rca?run=<run_id>
        │
        ▼
app/rca/page.tsx ──► <RcaView>
        │                fetch ──► web-backend GET /api/results (find run_id)
        ├─► <FailureCard>        analysis + suggested fix
        │       └─► <FilterMetaPanel>  detector chips + confidence + location
        ├─► <ChatPanel>          POST /agent/chat  ·  GET /api/sessions/<id>
        └─► feedback buttons ──► POST /api/sessions/<id>/feedback

/settings
        │
        ▼
app/settings/page.tsx ──fetch──► web-backend GET /api/filter-config
        │
        └─► <SettingsPanel>       knobs + detector inventory (read-only)
```

## Environment

The only build/runtime variable is the API target:

| Variable                  | Default                              | Notes                                                     |
| ------------------------- | ------------------------------------ | --------------------------------------------------------- |
| `NEXT_PUBLIC_API_TARGET`  | `http://web-backend:8095` (in Docker)| Browser-visible URL for the web-backend                   |

For local dev outside Docker the default proxy target is
`http://127.0.0.1:8095`. See `web/frontend/next.config.*` for the rewrite
rules.

## Fonts

The UI uses **Inter** (variable) and **JetBrains Mono** (variable), both
**self-hosted** under `web/frontend/app/fonts/` and wired up via
`next/font/local` in `app/layout.tsx`. The Docker build never reaches
`fonts.googleapis.com`, which keeps the build deterministic, offline-safe,
and friendly to corporate proxies / air-gapped CI.

If you want to add or update a font face:

1. Drop the WOFF2 (variable preferred) into `web/frontend/app/fonts/`.
   The path must stay inside `app/` — Turbopack’s `next/font/local`
   resolver refuses `..` traversal.
2. Add a new `localFont({ src: [...], variable: "--font-..." })` block in
   `app/layout.tsx`.
3. Reference the CSS variable from Tailwind / `globals.css` as needed.

## Run it independently

### 1. Local Node

```bash
cd web/frontend
npm install
npm run dev               # http://localhost:3000
```

### 2. Docker (production build)

```bash
docker compose up -d --build web-frontend
# UI at http://localhost:3080  (mapped from container port 3000)
```

### 3. Docker (live-reload dev server)

```bash
docker compose --profile dev-ui up -d web-frontend-dev
# UI at http://localhost:5173 with bind-mounted source
```

## Test it

The frontend currently relies on TypeScript as its primary safety net:

```bash
cd web/frontend
npx tsc --noEmit -p tsconfig.json
```

The end-to-end smoke test is to load the dashboard in a browser, click
**Refresh** (which calls the listener `/poll-once`), and verify a new
session card appears with analysis + suggested fix.
