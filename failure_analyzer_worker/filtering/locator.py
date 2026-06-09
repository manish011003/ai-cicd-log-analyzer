"""Locator — turn evidence into a structured ``FailureLocation``.

The locator is *the* answer to goal 2 (give the accurate place where the
issue happened). Detectors emit candidate locations; the universal core
emits a generic "log line N" location as a fallback. This module picks the
primary and returns the ranked list.

Algorithm:

1. **Filter out package-manager frames** unless they're the *only* candidate.
   ``java.base/sun.nio.ch.Net.pollConnect`` is not where the user's bug is.
2. **Cluster by ``(file, line)``** so two detectors pointing at the same
   source location corroborate each other; corroboration adds confidence.
3. **Sort by corroborated confidence**, break ties by detector priority,
   then by location specificity (source > test > command > log > unknown).
4. **Demote PKG_MGR frames** to the back of the list if any project-frame
   location exists.

The locator also has its own **fallback path**: when no detector
contributed a location, it scans the surviving lines for the highest-
scoring anchor and returns ``kind="log"`` with ``log_line_idx`` set so the
UI can still deep-link to the relevant raw line.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from .base import FailureLocation, Line


_KIND_RANK: dict[str, int] = {
    "source": 5,
    "test": 4,
    "command": 3,
    "log": 2,
    "unknown": 0,
}


_PKG_MGR_HINTS: tuple[str, ...] = (
    "/site-packages/",
    "/node_modules/",
    "/.m2/",
    "/.gradle/",
    "/.cargo/",
    "/usr/lib/",
    "/usr/local/",
    "/opt/",
    "vendor/",
    "/gems/",
    "java.base/",
    "java.lang.",
    "sun.nio.",
    "jdk.internal.",
)


def _is_framework_file(file: str) -> bool:
    if not file:
        return False
    return any(hint in file for hint in _PKG_MGR_HINTS)


def _cluster_key(loc: FailureLocation) -> tuple[str, str, int, str]:
    """A loc is "the same place" as another iff this tuple matches."""
    return (loc.kind, loc.file, loc.line, loc.test_name)


def pick(
    candidates: Sequence[FailureLocation],
    lines: Sequence[Line],
) -> tuple[FailureLocation | None, tuple[FailureLocation, ...]]:
    """Return ``(primary, ranked_list)``.

    ``ranked_list`` contains all candidates (after dedupe + corroboration),
    sorted best-first. ``primary`` is the first entry, or ``None`` if no
    candidate met the minimum confidence threshold and the fallback also
    found nothing useful.
    """
    if not candidates:
        fallback = _log_fallback(lines)
        return (fallback, (fallback,) if fallback else ())

    # Cluster by location identity, summing confidences (capped at 1.0).
    clusters: dict[tuple[str, str, int, str], list[FailureLocation]] = defaultdict(list)
    for c in candidates:
        clusters[_cluster_key(c)].append(c)

    merged: list[FailureLocation] = []
    for key, group in clusters.items():
        # Pick the representative: highest individual confidence wins.
        rep = max(group, key=lambda x: x.confidence)
        # Corroboration boost: each *additional* distinct detector adds
        # 0.10 to the cluster's effective confidence (capped at 1.0).
        distinct = {g.detector for g in group}
        boost = 0.10 * (len(distinct) - 1)
        merged.append(
            FailureLocation(
                kind=rep.kind,
                file=rep.file,
                line=rep.line,
                column=rep.column,
                function=rep.function,
                test_name=rep.test_name,
                command=rep.command,
                detector=",".join(sorted(distinct)),
                confidence=min(1.0, rep.confidence + boost),
                log_line_idx=rep.log_line_idx,
                message=rep.message,
            )
        )

    # Sort: prefer project frames, then kind rank, then confidence.
    has_project = any(
        not _is_framework_file(m.file) and m.kind == "source" for m in merged
    )

    def _sort_key(m: FailureLocation) -> tuple[int, int, float, int]:
        framework_penalty = 1 if (has_project and _is_framework_file(m.file)) else 0
        return (
            framework_penalty,  # 0 first — project frames win
            -_KIND_RANK.get(m.kind, 0),  # source > test > command > log
            -m.confidence,  # higher confidence first
            -m.log_line_idx,  # later in log first (most-recent failure)
        )

    ranked = tuple(sorted(merged, key=_sort_key))
    primary = ranked[0] if ranked else _log_fallback(lines)
    return (primary, ranked)


def _log_fallback(lines: Sequence[Line]) -> FailureLocation | None:
    """Best-effort ``kind="log"`` pick when no detector localized the failure.

    Used both when zero detectors contributed (truly unknown stack) and
    when every detector contributed only low-confidence guesses. Honest:
    we admit we couldn't pin a file:line, but we still deep-link the UI
    to the most-likely log line so the human can take it from there.
    """
    best_i = -1
    best_score = -1
    best_msg = ""
    for i, ln in enumerate(lines):
        if ln.drop or not ln.text:
            continue
        if ln.score > best_score:
            best_score = ln.score
            best_i = i
            best_msg = ln.text[:140]
    if best_i < 0:
        return None
    return FailureLocation(
        kind="log",
        log_line_idx=lines[best_i].idx,
        confidence=min(0.5, 0.10 + 0.05 * best_score),
        detector="core",
        message=best_msg,
    )
