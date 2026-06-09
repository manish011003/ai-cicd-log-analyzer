# Documentation index

Start with [`architecture.md`](architecture.md) for the 30-second tour, then
drop into the per-feature page that matches what you’re working on.

| Document                                                       | What it covers                                              |
| -------------------------------------------------------------- | ----------------------------------------------------------- |
| [architecture.md](architecture.md)                             | Service map, data stores, request lifecycle                 |
| [folder-structure.md](folder-structure.md)                     | Why the repo is laid out the way it is                      |
| [failure-analyzer-worker.md](failure-analyzer-worker.md)       | LangGraph + Groq + ES worker (port 8090)                    |
| [jenkins-failure-listener.md](jenkins-failure-listener.md)     | Jenkins → worker bridge (port 8088)                         |
| [web-backend.md](web-backend.md)                               | Sessions / chat / feedback API (port 8095)                  |
| [web-frontend.md](web-frontend.md)                             | Next.js 16 dashboard (port 3080)                            |
| [filtering.md](filtering.md)                                   | Structural log filter, detectors, Settings page             |
| [docker.md](docker.md)                                         | Compose services, profiles, ports, operational tasks        |
| [testing.md](testing.md)                                       | How to run every test suite locally and in CI               |
| [stabilization-notes.md](stabilization-notes.md)               | Bugs fixed in the current engagement and the matching tests |

The legacy long-form ingestion deep-dive at
[`../jenkins_failure_listener/ingestion.md`](../jenkins_failure_listener/ingestion.md)
is still kept for historical context; it’s linked from
`jenkins-failure-listener.md`.
