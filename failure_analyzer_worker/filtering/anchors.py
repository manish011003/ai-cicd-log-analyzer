"""Pass 4 — score, cluster, budget-fill.

Replaces the old ``LOG_BODY_MAX_CHARS`` tail-truncation with a token-budgeted
anchor selector. Three steps:

1. **Score** every surviving line. The score combines severity, structural
   position bonuses (line after a CMD, line before an EXIT), and any
   per-line ``score`` already accumulated from Pass 3 (baseline novelty) or
   the detector phase.
2. **Cluster** anchor lines (``score >= MIN_ANCHOR_SCORE``) by proximity so
   we ship contiguous context regions, not isolated lines.
3. **Round-robin fill** under a soft token budget. Each cluster gets its
   anchor before any cluster gets its second context line — that's the
   property that keeps multi-failure logs (Maven reactor, parallel test
   runs) from being starved by one high-scoring cluster.

Token accounting is approximated as ``len(text) // 4`` to keep this module
dependency-free. Swap in ``tiktoken`` later if exact counts ever matter.
"""

from __future__ import annotations

from collections.abc import Sequence

from .base import Kind, Line, Severity

# Tuned on synthetic + small real corpora. Conservative — false positives
# (too many anchors) are corrected by the token budget; false negatives
# (missing anchors) are corrected by the LOW-confidence fallback.
MIN_ANCHOR_SCORE = 3
CONTEXT_BEFORE = 2
CONTEXT_AFTER = 4
CLUSTER_MERGE_GAP = 3


_SEV_WEIGHT: dict[int, int] = {
    Severity.INFO: 0,
    Severity.WARN: 1,
    Severity.ERROR: 3,
    Severity.FATAL: 4,
    Severity.CAUSE: 5,
    Severity.EXIT: 5,
}


def _position_bonus(lines: Sequence[Line], i: int) -> int:
    """Cheap structural bonuses for "interesting position in the log"."""
    bonus = 0
    n = len(lines)

    # The line right after a CMD echo is usually the failing command's
    # output ("+ mvn verify\nERROR …").
    for k in range(i - 1, max(-1, i - 4), -1):
        if k < 0 or lines[k].drop:
            continue
        if lines[k].kind == Kind.CMD:
            bonus += 1
        break

    # The line just before an EXIT marker is usually the cause of the exit.
    for k in range(i + 1, min(n, i + 4)):
        if lines[k].drop:
            continue
        if lines[k].severity == Severity.EXIT:
            bonus += 2
        break

    return bonus


def _approx_tokens(text: str, note: str = "") -> int:
    # ~4 chars per token across all common BPE tokenizers; adequate for a
    # soft budget. Worst case we overshoot a few hundred tokens, which is
    # cheaper than adding a Rust dependency.
    return max(1, (len(text) + len(note) + 6) // 4)  # +6 for "L<idx>: "


def score(lines: list[Line]) -> None:
    """Mutate ``Line.score`` in place. Skips dropped lines."""
    n = len(lines)
    for i, ln in enumerate(lines):
        if ln.drop or not ln.text:
            continue
        s = _SEV_WEIGHT.get(int(ln.severity), 0) + _position_bonus(lines, i)
        # Carry forward any boost accumulated from baseline novelty or
        # detector contributions.
        s += ln.score
        # De-emphasize the deep interior of a stack run; the throwing line
        # above the stack already scores high on severity.
        if ln.kind == Kind.STACK and "elided" not in ln.note:
            s -= 1
        ln.score = max(0, s)


# ── Cluster anchors ───────────────────────────────────────────────────────


def _cluster(
    indices: list[int], merge_gap: int = CLUSTER_MERGE_GAP
) -> list[tuple[int, int]]:
    """Return ``[(lo_idx, hi_idx), ...]`` of anchor index ranges."""
    if not indices:
        return []
    clusters: list[tuple[int, int]] = []
    lo = hi = indices[0]
    for x in indices[1:]:
        if x - hi <= merge_gap:
            hi = x
        else:
            clusters.append((lo, hi))
            lo = hi = x
    clusters.append((lo, hi))
    return clusters


def _expand(
    cluster: tuple[int, int],
    n: int,
    before: int = CONTEXT_BEFORE,
    after: int = CONTEXT_AFTER,
) -> tuple[int, int]:
    lo, hi = cluster
    return (max(0, lo - before), min(n - 1, hi + after))


def _peak(lines: Sequence[Line], lo: int, hi: int) -> int:
    return max(
        (lines[i].score for i in range(lo, hi + 1) if not lines[i].drop),
        default=0,
    )


def _center(lines: Sequence[Line], lo: int, hi: int) -> int:
    """Highest-scoring line in the cluster — the anchor we expand around."""
    best_i, best_s = lo, -1
    for i in range(lo, hi + 1):
        if lines[i].drop:
            continue
        if lines[i].score > best_s:
            best_s, best_i = lines[i].score, i
    return best_i


# ── Budget fill ────────────────────────────────────────────────────────────


def select(
    lines: list[Line],
    *,
    token_budget: int,
    min_score: int = MIN_ANCHOR_SCORE,
) -> list[int]:
    """Pick line indices to emit, in original order, under ``token_budget``.

    Strategy: collect anchor lines, cluster + expand, then round-robin
    across clusters picking one line at a time outward from each cluster's
    center. This guarantees every detected failure cluster gets at least
    its central line emitted before any cluster gets its second context
    line — the property that protects multi-failure logs.
    """
    n = len(lines)
    if n == 0:
        return []

    anchors = [
        i
        for i, ln in enumerate(lines)
        if not ln.drop and ln.text and ln.score >= min_score
    ]

    # Fallback: no scored anchors — return a budget-bounded tail of
    # surviving lines so the LLM at least sees the end of the log. The
    # confidence flag (set elsewhere) tells the prompt this is a weak case.
    if not anchors:
        return _fallback_tail(lines, token_budget)

    clusters_raw = _cluster(anchors)
    clusters = [_expand(c, n) for c in clusters_raw]

    # Sort clusters by their internal peak score so the round-robin starts
    # from the strongest cluster — keeps anchor priority intact.
    ranked = sorted(
        clusters,
        key=lambda c: -_peak(lines, c[0], c[1]),
    )
    centers = {c: _center(lines, c[0], c[1]) for c in ranked}
    radii = {c: 0 for c in ranked}

    selected: set[int] = set()
    remaining = token_budget
    progress = True
    while remaining > 0 and progress:
        progress = False
        for c in ranked:
            lo, hi = c
            r = radii[c]
            ctr = centers[c]
            for cand in (ctr - r, ctr + r) if r > 0 else (ctr,):
                if not (lo <= cand <= hi):
                    continue
                if cand in selected:
                    continue
                ln = lines[cand]
                if ln.drop or not ln.text:
                    continue
                cost = _approx_tokens(ln.text, ln.note)
                if cost > remaining:
                    continue
                selected.add(cand)
                remaining -= cost
                progress = True
            radii[c] += 1

    return sorted(selected)


def _fallback_tail(lines: Sequence[Line], token_budget: int) -> list[int]:
    """Pick the tail of surviving lines up to the token budget."""
    survivors = [i for i, ln in enumerate(lines) if not ln.drop and ln.text]
    if not survivors:
        return []
    picked: list[int] = []
    remaining = token_budget
    for i in reversed(survivors):
        cost = _approx_tokens(lines[i].text, lines[i].note)
        if cost > remaining:
            break
        picked.append(i)
        remaining -= cost
    return sorted(picked)
