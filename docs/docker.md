# Docker / Compose reference

The full stack is orchestrated by `docker-compose.yml` at the repo root.
Every Python service has a dedicated Dockerfile under `docker/` that uses
the **repo root** as its build context, so each image is built with
`docker compose build`.

## Services

| Compose service     | Image / build                              | Purpose                                                      |
| ------------------- | ------------------------------------------ | ------------------------------------------------------------ |
| `postgres`          | `postgres:16-alpine`                       | Listener state + web-backend sessions/chat/feedback          |
| `elasticsearch`     | `elasticsearch:8.12.0`                     | kNN store of accepted solutions + filtered context           |
| `kibana`            | `kibana:8.12.0`                            | Optional UI for inspecting the ES indices                    |
| `worker`            | `docker/Dockerfile.worker`                 | LangGraph + Groq + ES; receives events from the listener     |
| `listener`          | `docker/Dockerfile.listener`               | API mode (POST /poll-once)                                   |
| `listener-poller`   | `docker/Dockerfile.listener` (`profile: poll`) | Headless polling loop; runs `python run_listener.py`     |
| `web-backend`       | `docker/Dockerfile.web-backend`            | FastAPI bridge between the UI and worker / listener          |
| `web-frontend`      | `docker/Dockerfile.web-frontend`           | Production-built Next.js dashboard                           |
| `web-frontend-dev`  | `node:22-slim` (`profile: dev-ui`)         | Live-reload Next dev server (bind-mounted source)            |
| `jenkins`           | `my-jenkins:2.541.1` (`profile: jenkins`)  | Optional self-hosted Jenkins for full-stack demos            |

## Default ports (host → container)

| Service        | Host port | Container port | Override variable                |
| -------------- | --------- | -------------- | -------------------------------- |
| Web UI         | 3080      | 3000           | `WEB_FRONTEND_PUBLISH_PORT`      |
| Web API        | 8095      | 8095           | `WEB_BACKEND_PUBLISH_PORT`       |
| Worker         | 8090      | 8090           | `WORKER_PUBLISH_PORT`            |
| Listener       | 8088      | 8088           | `LISTENER_PUBLISH_PORT`          |
| Postgres       | 5432      | 5432           | `POSTGRES_PUBLISH_PORT`          |
| Elasticsearch  | 9200      | 9200           | `ELASTICSEARCH_PUBLISH_PORT`     |
| Kibana         | 5601      | 5601           | `KIBANA_PUBLISH_PORT`            |
| Web UI dev     | 5173      | 5173           | (no override; only with `dev-ui`)|
| Jenkins        | 8080      | 8080           | (only with `jenkins` profile)    |

> The web UI host port is **3080**, not 3000, because Windows / Hyper-V
> reserves the 2971–3070 range. Override with
> `WEB_FRONTEND_PUBLISH_PORT=3000` if your host allows it.

## First run

```bash
cp .env.example .env       # PowerShell: Copy-Item .env.example .env
# Edit .env:
#   GROQ_API_KEY=<from https://console.groq.com/keys>
#   WORKER_API_KEY=<any shared secret>
#   JENKINS_BASE_URL / JENKINS_USER / JENKINS_API_TOKEN

docker compose up -d --build
docker compose ps
```

Smoke checks:

```bash
curl http://localhost:8090/health          # worker
curl http://localhost:8088/health          # listener
curl http://localhost:8095/health          # web-backend
start http://localhost:3080                # dashboard (Windows)
```

## Compose profiles

Profiles let you keep noisy or optional services off by default. Activate
them with `--profile <name>`:

```bash
docker compose --profile poll    up -d   # add the headless listener-poller
docker compose --profile dev-ui  up -d   # add the Next.js dev server
docker compose --profile jenkins up -d   # bring up an in-cluster Jenkins
```

## Image-level conventions

- **Build context = repo root** so every Dockerfile can `COPY` from any
  service folder without needing a per-service context.
- **`.dockerignore`** at the repo root excludes `node_modules`, `.next`,
  `.next/dev`, every venv, every `.env` (except `.env.example`), git, IDE,
  and OS metadata so image layers stay lean and reproducible.
- **No secrets baked in.** Every credential is injected at runtime via
  `environment:` mapping in `docker-compose.yml`. The repository carries
  only `.env.example` files.
- **Health endpoints uniformly at `/health`** so Compose health checks and
  reverse proxies can probe each service the same way.

## Common operational tasks

| Task                                     | Command                                                              |
| ---------------------------------------- | -------------------------------------------------------------------- |
| Tail one service’s logs                  | `docker compose logs -f worker`                                      |
| Rebuild after editing a Dockerfile       | `docker compose build worker && docker compose up -d worker`         |
| Reset Postgres + ES volumes              | `docker compose down -v` (⚠ deletes all sessions and stored fixes)   |
| Open a shell inside the worker           | `docker compose exec worker /bin/sh`                                 |
| Trigger a manual Jenkins poll            | `curl -X POST http://localhost:8088/poll-once`                       |
