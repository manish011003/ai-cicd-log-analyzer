# Feature: web/frontend

A Next.js 16 (App Router, React 19, Tailwind 4) dashboard that visualizes the
analyzer’s output, lets users chat with the LLM about a specific failure, and
records accept/reject feedback that becomes future training data.

## What it does

- Lists every session the worker has produced (newest first).
- Renders the LangGraph output: filtered logs, suggested fix, similar past
  failures with their kNN scores.
- Surfaces the structural filter's *Detected* panel on each failure card —
  confidence badge, activated detector chips, primary file:line anchor,
  and compression ratio. See [`filtering.md`](filtering.md).
- Hosts the conversational assistant (`ChatPanel`) — every message round-
  trips through the backend → worker `/chat/turn` endpoint.
- Lets the user accept or reject the suggested fix; accepting triggers the
  backend to write the solution to Elasticsearch via the worker.
- Provides a “Refresh / Auto-poll” control that proxies to the listener.
- Exposes a **Settings** page (`/settings`) that surfaces the live filter
  configuration: detector inventory, token/character budgets, and the
  env var that controls each knob. Read-only — changes still require
  editing `.env` and restarting the worker.

## Logic flow

```
URL ?session=<uuid>
        │
        ▼
app/page.tsx ──fetch──► web-backend GET /api/sessions/<uuid>
        │
        ├─► <Dashboard>           list view + selection
        ├─► <FailureCard>         analysis + suggested fix
        │       └─► <FilterMetaPanel>   detector chips + confidence + location
        ├─► <ChatPanel>           POST /api/sessions/<id>/chat
        ├─► <StatsBar>            kNN match metadata
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
