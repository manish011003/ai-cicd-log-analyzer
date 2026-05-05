"""Factory that turns ``CI_PROVIDER`` into a concrete :class:`CISource`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

    from .base import CISource

logger = logging.getLogger(__name__)


def create_ci_source(settings: "Settings") -> "CISource":
    """Return the CI source matching ``settings.ci_provider``."""
    provider = (settings.ci_provider or "").strip().lower()
    logger.info("Creating CI source  provider=%s", provider)

    if provider == "jenkins":
        from .providers.jenkins import JenkinsCISource

        return JenkinsCISource(settings)

    raise ValueError(
        f"Unknown CI_PROVIDER={provider!r}. Supported: jenkins.",
    )
