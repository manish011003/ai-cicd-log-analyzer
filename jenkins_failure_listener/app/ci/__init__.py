"""Pluggable CI-source providers.

The failure-monitor service only depends on :class:`CISource`. Swapping
Jenkins for GitLab CI, GitHub Actions, CircleCI, etc. means writing one
adapter under ``providers/`` and wiring it up in ``factory.py`` — no
changes to the polling loop, dispatcher, or Postgres state store.
"""

from .base import CISource
from .factory import create_ci_source

__all__ = ["CISource", "create_ci_source"]
