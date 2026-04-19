"""Entry point: ``python -m failure_analyzer_worker`` from the repository root."""

from __future__ import annotations

import uvicorn

from .config import settings


def main() -> None:
    uvicorn.run(
        "failure_analyzer_worker.worker:app",
        host=settings.worker_host,
        port=settings.worker_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
