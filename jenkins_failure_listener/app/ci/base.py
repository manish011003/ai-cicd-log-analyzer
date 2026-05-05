"""CI source protocol.

Two operations are all the listener needs:

  1. :meth:`CISource.list_failed_builds` — enumerate recent failed builds.
  2. :meth:`CISource.build_failure_event` — fetch + extract a ``FailureEvent``
     payload for one specific build.

The neutral return types live in :mod:`app.models`, so the Postgres state
store and the worker dispatcher never learn anything about the underlying
CI system.
"""

from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable

from app.models import FailureEvent


class FailedBuildRef(TypedDict):
    """Minimal reference to a failed build; what the RSS / API listing returns."""

    job_full_name: str
    build_number: int
    build_url: str


@runtime_checkable
class CISource(Protocol):
    """Everything the failure-monitor service needs from a CI system."""

    def list_failed_builds(self) -> list[FailedBuildRef]:  # pragma: no cover - protocol
        ...

    def build_failure_event(
        self,
        job_full_name: str,
        build_number: int,
        build_url: str,
    ) -> FailureEvent:  # pragma: no cover - protocol
        ...
