"""Detector registry + auto-import of bundled detectors.

Detectors register themselves at import time via the :func:`register`
decorator. ``factory.create_filter`` calls :func:`load_detectors` which
selectively imports the bundled detector modules (so registration runs)
and returns the configured subset.

Pattern matches ``llm/providers/`` and ``vectorstore/providers/``: one
file per provider, lazy imports so the universal core has no transitive
dependency on any specific stack's libraries.
"""

from __future__ import annotations

import importlib
import logging
from typing import Callable

from ..base import Detector

logger = logging.getLogger(__name__)


# ── Registry ───────────────────────────────────────────────────────────────


_REGISTRY: dict[str, Detector] = {}


def register(detector: Detector) -> Detector:
    """Register a detector instance. Idempotent — last registration wins.

    Designed to be used as ``@register`` on a module-level instance::

        _DET = MyDetector()
        register(_DET)
    """
    if detector.name in _REGISTRY:
        logger.debug("re-registering detector %s", detector.name)
    _REGISTRY[detector.name] = detector
    return detector


def registered() -> dict[str, Detector]:
    """Return the live registry (callers should treat it as read-only)."""
    return dict(_REGISTRY)


# ── Bundled detectors (must be importable from a fresh interpreter) ────────


# Order is *not* significant — `factory.create_filter` re-sorts by
# `Detector.priority` before invocation. This tuple is the discovery list,
# not the run order.
_BUNDLED: tuple[str, ...] = (
    "java_stack",
    "python_traceback",
    "generic_shell",
)


def load_detectors(
    selection: str | None = "auto",
    *,
    importer: Callable[[str], object] = importlib.import_module,
) -> list[Detector]:
    """Resolve a settings string to a list of active :class:`Detector` instances.

    Accepted values for ``selection``:

    * ``"auto"`` (default) — import every bundled detector. Each detector's
      own ``activates_on`` then decides whether it runs on a given log.
    * ``"none"`` — no detectors. Only the universal core runs.
    * ``"a,b,c"`` — explicit allowlist (comma-separated). Order in the
      string is preserved in the returned list (priority tie-breaker).
    """
    if not selection or selection.strip().lower() == "none":
        return []

    sel = selection.strip().lower()

    if sel == "auto":
        wanted = list(_BUNDLED)
    else:
        wanted = [s.strip() for s in sel.split(",") if s.strip()]

    out: list[Detector] = []
    for name in wanted:
        try:
            importer(f"failure_analyzer_worker.filtering.detectors.{name}")
        except Exception:  # noqa: BLE001 - one bad detector cannot break the pipeline
            logger.exception("detector %r failed to import; skipping", name)
            continue
        det = _REGISTRY.get(name)
        if det is None:
            logger.warning(
                "detector %r imported but did not register itself; skipping",
                name,
            )
            continue
        out.append(det)

    return out


def bundled_names() -> tuple[str, ...]:
    """Return the canonical list of detectors that ship with the worker.

    Stable, immutable, and decoupled from the live registry — the
    ``/filter-config`` endpoint uses this to render the "available" list
    even when some detectors are disabled via ``FILTER_DETECTORS``.
    """
    return _BUNDLED


def reset_for_tests() -> None:
    """Clear the registry. Tests use this to start from a known empty state."""
    _REGISTRY.clear()
