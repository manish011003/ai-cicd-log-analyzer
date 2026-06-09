"""Factory that builds a :class:`Filter` from :class:`WorkerSettings`.

Same shape as :func:`failure_analyzer_worker.llm.factory.create_llm` and
``create_solution_repository``: the rest of the worker holds the protocol
type only and asks this factory for a concrete instance once during
:func:`build_deps`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .base import Detector, Filter
from .detectors import bundled_names, load_detectors
from .orchestrator import FilterConfig, UniversalFilter

if TYPE_CHECKING:
    from ..config import WorkerSettings

logger = logging.getLogger(__name__)


def create_filter(settings: "WorkerSettings") -> Filter:
    """Return a :class:`Filter` wired with detectors and the local baseline.

    ``FILTER_DETECTORS`` controls plug-and-play activation:

    * ``"auto"`` (default) — every bundled detector is loaded; each
      detector's own ``activates_on`` decides whether it runs per log.
    * ``"none"`` — universal core only; no detectors.
    * ``"a,b,c"`` — explicit allowlist by detector ``name``.

    Per the design pattern, this function is the *only* place detector
    registration happens — callers receive a wired :class:`Filter` and
    have no business poking at the registry directly.
    """
    selection = getattr(settings, "filter_detectors", "auto") or "auto"
    detectors = load_detectors(selection)
    logger.info(
        "Creating filter  selection=%s  loaded=%d  budget=%d tokens",
        selection,
        len(detectors),
        getattr(settings, "log_body_max_tokens", 1500),
    )

    cfg = FilterConfig(
        log_body_max_tokens=int(getattr(settings, "log_body_max_tokens", 1500)),
        log_body_max_chars=int(getattr(settings, "log_body_max_chars", 8000)),
        max_active_detectors=int(getattr(settings, "filter_max_active_detectors", 5)),
        use_self_baseline=True,
    )
    return UniversalFilter(cfg, detectors=detectors)


# ── Introspection (used by /filter-config and the UI Settings page) ──────────


def _detector_description(det: Detector) -> str:
    """Return a one-line human description for ``det``.

    Pulled from the detector class' docstring so adding a new detector
    automatically gets its description on the Settings page — no second
    place to remember to update.
    """
    doc = getattr(det.__class__, "__doc__", None) or ""
    first = next((ln.strip() for ln in doc.splitlines() if ln.strip()), "")
    return first or det.__class__.__name__


def _describe_detector(det: Detector, *, active: bool) -> dict[str, Any]:
    return {
        "name": getattr(det, "name", det.__class__.__name__),
        "priority": int(getattr(det, "priority", 0)),
        "description": _detector_description(det),
        "active": active,
    }


def describe_filter(settings: "WorkerSettings", flt: Filter) -> dict[str, Any]:
    """Return a JSON-safe snapshot of the live filter configuration.

    This is what powers the worker's ``GET /filter-config`` endpoint and,
    through the web-backend proxy, the platform's Settings page. The
    payload covers three orthogonal slices:

    * ``settings`` — the tweakable knobs (env-var driven; changing them
      requires a worker restart).
    * ``detectors.active`` — detectors actually loaded by the running
      filter, sorted by priority.
    * ``detectors.available`` — every bundled detector the worker *could*
      load, so the UI can show what the user is opting out of.
    """
    cfg = getattr(flt, "config", None)
    active_detectors = tuple(getattr(flt, "detectors", ()) or ())
    active_names = {getattr(d, "name", "") for d in active_detectors}

    # Build a one-shot inventory of bundled detectors. Failures here are
    # intentionally swallowed: introspection must never break the route.
    available: list[dict[str, Any]] = []
    try:
        for name in bundled_names():
            try:
                inst = load_detectors(name)
            except Exception:  # noqa: BLE001
                logger.debug("describe_filter: failed to inspect %s", name, exc_info=True)
                continue
            if not inst:
                continue
            available.append(_describe_detector(inst[0], active=name in active_names))
    except Exception:  # noqa: BLE001
        logger.exception("describe_filter: bundled inventory failed")

    return {
        "settings": {
            "log_body_max_tokens": int(
                getattr(cfg, "log_body_max_tokens", 0)
                or getattr(settings, "log_body_max_tokens", 0)
            ),
            "log_body_max_chars": int(
                getattr(cfg, "log_body_max_chars", 0)
                or getattr(settings, "log_body_max_chars", 0)
            ),
            "filter_detectors": str(getattr(settings, "filter_detectors", "auto")),
            "filter_max_active_detectors": int(
                getattr(cfg, "max_active_detectors", 0)
                or getattr(settings, "filter_max_active_detectors", 0)
            ),
            "use_self_baseline": bool(getattr(cfg, "use_self_baseline", True)),
        },
        "detectors": {
            "active": [_describe_detector(d, active=True) for d in active_detectors],
            "available": available,
        },
        "implementation": flt.__class__.__name__,
    }
