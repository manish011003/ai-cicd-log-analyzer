"""Entry point: ``py -m failure_analyzer_worker`` from analyzer_project/."""

import uvicorn

from .config import settings

uvicorn.run(
    "failure_analyzer_worker.worker:app",
    host=settings.worker_host,
    port=settings.worker_port,
    log_level="info",
)
