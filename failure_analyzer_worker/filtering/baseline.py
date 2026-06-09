"""Pass 3 — baseline-aware drop.

The filter compares the failing log's template hashes against a baseline of
"what's normal for this job/stage." Three sketch implementations live behind
one :class:`BaselineSketch` protocol so the rest of the pipeline doesn't care
where the baseline came from:

* :class:`EmptySketch` — no baseline data (cold start). Pass 3 becomes a
  no-op; the structural passes carry the compression on their own.
* :class:`SelfSketch` — derive a baseline from the **first 30 %** of the
  *same* log. Works on every input, no persistence required. Catches
  "setup repeats inside the failure region" patterns.
* :class:`CounterSketch` — true per-``(job, stage)`` success sketch with a
  fraction-of-builds threshold. Backed by Elasticsearch in PR 3.

Two safety rules sit *outside* the sketch protocol because they apply to
every backend:

1. **Severity ≥ ERROR is exempt.** A line classified as an error or worse
   is never dropped by the baseline even if its template is "common".
   Defends against sketch poisoning where a chronically-broken job teaches
   the baseline that its real errors are normal.
2. **Singleton WARN is exempt.** A WARN whose template hash appears only
   once in the current log is also kept — it's structurally interesting
   regardless of what the baseline says.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .base import Line, Severity


@runtime_checkable
class BaselineSketch(Protocol):
    """Cheap read-only "is this template normal?" oracle.

    Implementations should be hashable-key dict-of-floats internally so
    ``is_common`` and ``is_very_common`` are O(1). The orchestrator caches
    instances per ``(job, stage)`` so this protocol is never on a hot path.
    """

    def is_common(self, template_hash: int) -> bool:  # pragma: no cover - protocol
        ...

    def is_very_common(self, template_hash: int) -> bool:  # pragma: no cover - protocol
        ...

    def version(self) -> str:  # pragma: no cover - protocol
        ...


class EmptySketch:
    """No baseline data — :meth:`apply` becomes a no-op for this run."""

    __slots__ = ()

    def is_common(self, template_hash: int) -> bool:
        return False

    def is_very_common(self, template_hash: int) -> bool:
        return False

    def version(self) -> str:
        return "empty"


class SelfSketch:
    """Baseline B: hash the prefix of the *same* log, treat repeats as normal.

    Useful when no persistent baseline exists yet (new job, fresh deploy).
    The intuition: in a real failure log the first ~30 % is overwhelmingly
    setup output (clone, install, env dump, banner) — anything that recurs
    there ≥ ``min_count`` times is safe to treat as background noise in the
    rest of the log.

    Only INFO-severity hashes are eligible; WARN and above are excluded
    from the sketch construction itself so the rule "WARN/ERROR exempt
    from drop" is enforced at *both* layers.
    """

    __slots__ = ("_common", "_prefix_frac")

    def __init__(
        self,
        lines: Sequence[Line],
        *,
        prefix_frac: float = 0.30,
        min_count: int = 3,
    ) -> None:
        self._prefix_frac = prefix_frac
        cut = max(20, int(len(lines) * prefix_frac))
        c: Counter[int] = Counter()
        for ln in lines[:cut]:
            if ln.template_hash and ln.severity == Severity.INFO and not ln.drop:
                c[ln.template_hash] += 1
        self._common: frozenset[int] = frozenset(
            h for h, n in c.items() if n >= min_count
        )

    def is_common(self, template_hash: int) -> bool:
        return template_hash in self._common

    def is_very_common(self, template_hash: int) -> bool:
        # Self-baseline has no "frequency across runs" signal, only
        # "frequency within this run". Treat any repeat as one level —
        # WARN-tier drops require a more authoritative sketch.
        return False

    def version(self) -> str:
        return f"self/{self._prefix_frac:.2f}/{len(self._common)}"


class CounterSketch:
    """Baseline A: fraction-of-successful-builds-per-template oracle.

    Built from a Counter persisted across builds. Two thresholds:

    * ``drop_thr`` (default 0.6) — template seen in this fraction of recent
      successful builds → treated as common; INFO lines with this template
      are eligible for drop.
    * ``very_thr`` (default 0.9) — template seen in this fraction → WARN
      lines with this template are *also* eligible for drop.

    Construction is decoupled from storage; the ES-backed
    ``BaselineRepository`` (PR 3) is responsible for building a
    :class:`CounterSketch` from its persisted state.
    """

    __slots__ = ("_fractions", "_drop_thr", "_very_thr", "_ver")

    def __init__(
        self,
        fractions: dict[int, float],
        *,
        drop_thr: float = 0.6,
        very_thr: float = 0.9,
        version_tag: str = "counter",
    ) -> None:
        self._fractions = fractions
        self._drop_thr = drop_thr
        self._very_thr = very_thr
        self._ver = version_tag

    def is_common(self, template_hash: int) -> bool:
        return self._fractions.get(template_hash, 0.0) >= self._drop_thr

    def is_very_common(self, template_hash: int) -> bool:
        return self._fractions.get(template_hash, 0.0) >= self._very_thr

    def version(self) -> str:
        return self._ver


# ── Application ───────────────────────────────────────────────────────────


def apply(lines: list[Line], sketch: BaselineSketch) -> dict[str, int | str]:
    """Pass 3: drop common INFO lines, boost novel ones. Mutates ``lines``.

    Returns counters for the observability payload (how many drops, how
    many novel boosts, which sketch version was consulted).
    """
    dropped = 0
    novel_boost = 0

    # Pre-pass: count WARN template occurrences in this log so the
    # "singleton WARN" safety rule has the data it needs.
    warn_counts: Counter[int] = Counter()
    for ln in lines:
        if not ln.drop and ln.severity == Severity.WARN and ln.template_hash:
            warn_counts[ln.template_hash] += 1

    for ln in lines:
        if ln.drop or not ln.template_hash:
            continue

        if ln.severity >= Severity.ERROR:
            # Exempt — real errors never disappear into baseline drops.
            continue

        if ln.severity == Severity.WARN:
            if (
                warn_counts[ln.template_hash] >= 2
                and sketch.is_very_common(ln.template_hash)
            ):
                ln.drop = True
                ln.reason = "baseline-warn-common"
                dropped += 1
            # Singleton WARN: always interesting, leave alone.
            continue

        # INFO / DEBUG: full baseline rule.
        if sketch.is_common(ln.template_hash):
            ln.drop = True
            ln.reason = "baseline-common"
            dropped += 1
        else:
            ln.score += 2  # rarity boost, picked up by Pass 4 scoring
            novel_boost += 1

    return {
        "baseline_dropped": dropped,
        "baseline_novel": novel_boost,
        "baseline_version": sketch.version(),
    }
