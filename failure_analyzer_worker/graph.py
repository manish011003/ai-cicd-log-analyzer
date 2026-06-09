"""LangGraph analysis pipeline wired to pluggable LLM + vector-store providers.

The state machine is unchanged (preprocess → {analyze_with_context |
analyze_fresh} → END); what changed is that the nodes no longer import
Groq, Elasticsearch, or sentence-transformers directly. They receive a
:class:`~failure_analyzer_worker.deps.Deps` bundle and call the abstract
:class:`~failure_analyzer_worker.llm.LLMClient` / :class:`~failure_analyzer_worker.vectorstore.SolutionRepository`
interfaces, so swapping any backend is a config change.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, StateGraph

from .llm import ChatMessage

if TYPE_CHECKING:
    from .deps import Deps

logger = logging.getLogger(__name__)


class AnalysisState(TypedDict, total=False):
    # --- inputs (set by the caller) ---
    raw_logs: str
    stage_name: str
    job_name: str
    build_number: int
    build_url: str
    # --- intermediate ---
    filtered_logs: str  # wire-format: plain string fed into the LLM prompt
    filter_meta: dict[str, Any]  # structured FilterResult payload (locations, confidence, ...)
    fingerprint: str
    es_matches: list[dict[str, Any]]
    # --- outputs ---
    analysis: str
    suggested_fix: str
    recommendation: str
    matched_solution: str
    match_score: float


# ── Response post-processing ──────────────────────────────────────────────────


def _split_response(content: str) -> tuple[str, str]:
    """Split on the first ``Step-by-Step Fix`` marker into (analysis, suggested_fix).

    Tolerates either a markdown heading (``## Step-by-Step Fix``) or a bold
    list item (``2. **Step-by-Step Fix**``) so the splitter still works if
    the LLM drifts from the requested heading style.
    """
    pattern = re.compile(
        r"(?im)^(?:#{1,6}\s*|\d+\.\s*\**)Step[-\s]?by[-\s]?Step\s+Fix\**",
    )
    match = pattern.search(content)
    if match:
        return content[: match.start()].strip(), content[match.start() :].strip()
    return content.strip(), ""


def _strip_match_status_preamble(text: str) -> str:
    """Remove LLM boilerplate announcing retrieval / similarity status (not user-facing)."""
    t = (text or "").strip()
    if not t:
        return t

    for _ in range(6):
        if "\n\n" in t:
            para, rest = t.split("\n\n", 1)
        else:
            para, rest = t, ""

        candidate = para.strip()
        if not candidate:
            t = rest.strip()
            continue

        cl = candidate.lower()
        has_similarity_pct = bool(re.search(r"similarity\s*\d+\s*%", cl))
        is_noise = (
            ("exact match" in cl and ("similarity" in cl or "%" in candidate))
            or "returning previously accepted" in cl
            or (
                has_similarity_pct
                and ("match" in cl or "returning" in cl or "accepted" in cl)
            )
            or bool(
                re.match(
                    r"(?is)^(?:#+\s*)?(?:\*\*)?\s*exact\s+match\s+found\b",
                    candidate,
                ),
            )
        )

        if is_noise:
            t = rest.strip()
            continue
        break

    return t.strip()


# ── Graph assembly ────────────────────────────────────────────────────────────


class AnalysisGraph:
    """Stateful LangGraph compiled against a specific :class:`Deps` bundle.

    Construct once per worker process (usually in the FastAPI lifespan) and
    call :meth:`invoke` for each failure event.
    """

    def __init__(self, deps: "Deps") -> None:
        self._deps = deps
        self._graph = self._build()

    # ── public API ──

    def invoke(self, state: AnalysisState | dict) -> dict:
        return self._graph.invoke(state)

    def chat_clarification(
        self,
        messages: list[dict[str, str]],
        *,
        fingerprint: str = "",
        log_excerpt: str = "",
        job_name: str = "",
        stage_name: str = "",
        build_number: int = 0,
        analysis: str = "",
        suggested_fix: str = "",
        use_full_log: bool = False,
    ) -> str:
        """Follow-up chat after initial analysis (e.g. user rejected the suggested fix).

        ``use_full_log=True`` signals the caller already fetched the raw log
        excerpt (stored in Postgres) and wants a larger slice injected into
        the system prompt. The cap is still bounded by
        ``settings.chat_log_excerpt_max_chars_full`` so we don't accidentally
        overflow the LLM context window.
        """
        settings = self._deps.settings
        cap = (
            settings.chat_log_excerpt_max_chars_full
            if use_full_log
            else settings.chat_log_excerpt_max_chars
        )

        ctx_parts: list[str] = []
        if job_name or build_number:
            ctx_parts.append(f"Job: {job_name} #{build_number}  stage: {stage_name}")
        if analysis:
            ctx_parts.append(f"## Original Analysis\n{analysis}")
        if suggested_fix:
            ctx_parts.append(f"## Suggested Fix\n{suggested_fix}")
        if fingerprint:
            ctx_parts.append(f"Fingerprint:\n{fingerprint}")
        if log_excerpt and log_excerpt.strip():
            label = "Raw log excerpt" if use_full_log else "Filtered log excerpt"
            ctx_parts.append(f"{label}:\n{log_excerpt[:cap]}")

        ctx = "\n\n".join(ctx_parts)
        system_prompt = self._deps.prompts.load("clarify_system")
        sys_content = system_prompt.strip() + ("\n\n" + ctx if ctx else "")

        chat_messages: list[ChatMessage] = [ChatMessage(role="system", content=sys_content)]
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "assistant":
                chat_messages.append(ChatMessage(role="assistant", content=content))
            elif role == "system":
                chat_messages.append(ChatMessage(role="system", content=content))
            else:
                chat_messages.append(ChatMessage(role="user", content=content))

        logger.info(
            "chat_clarification  msgs=%d  ctx_len=%d", len(messages), len(sys_content),
        )
        return self._deps.llm.invoke(chat_messages)

    # ── graph construction ──

    def _build(self):
        builder = StateGraph(AnalysisState)
        builder.add_node("preprocess", self._preprocess)
        builder.add_node("analyze_with_context", self._analyze_with_context)
        builder.add_node("analyze_fresh", self._analyze_fresh)

        builder.set_entry_point("preprocess")
        builder.add_conditional_edges(
            "preprocess",
            self._route,
            {
                "analyze_with_context": "analyze_with_context",
                "analyze_fresh": "analyze_fresh",
            },
        )
        builder.add_edge("analyze_with_context", END)
        builder.add_edge("analyze_fresh", END)
        return builder.compile()

    # ── nodes ──

    def _preprocess(self, state: AnalysisState) -> dict:
        """Filter logs → fingerprint → vector-store similarity search.

        Uses the structured :class:`Filter` protocol so the locator,
        confidence flag, and per-pass observability flow into
        ``state["filter_meta"]``. ``state["filtered_logs"]`` stays a plain
        string (the wire format the Web UI persists today).
        """
        raw = state["raw_logs"]
        stage_name = state.get("stage_name", "")
        job_name = state.get("job_name", "")

        result = self._deps.filter.filter(raw, stage_name=stage_name, job_name=job_name)

        try:
            matches = self._deps.solutions.search(result.fingerprint)
        except Exception:
            logger.exception("solution repo search failed")
            matches = []

        logger.info(
            "preprocess  job=%s #%d  stage=%s  confidence=%s  loc=%s  "
            "raw=%d→body=%d (%d tok)  detectors=%s  matches=%d",
            job_name or "?",
            state.get("build_number", 0),
            stage_name or "?",
            result.confidence,
            result.primary_location.as_anchor() if result.primary_location else "unknown",
            result.raw_chars,
            result.body_chars,
            result.body_tokens,
            ",".join(result.metadata.get("activated_detectors", [])) or "none",
            len(matches),
        )

        return {
            "filtered_logs": result.body,
            "filter_meta": result.metadata,
            "fingerprint": result.fingerprint,
            # Normalize to plain dicts so downstream state/JSON stays simple.
            "es_matches": [m.to_dict() if hasattr(m, "to_dict") else m for m in matches],
        }

    def _route(self, state: AnalysisState) -> str:
        """Only use a past solution if it confidently clears the threshold.

        ``SolutionRepository.search`` already filters by
        ``SIMILARITY_THRESHOLD``, so a non-empty list here means at least one
        strong neighbour. Re-check defensively in case a future backend ever
        returns weaker hits.
        """
        matches = state.get("es_matches") or []
        threshold = self._deps.settings.similarity_threshold
        if matches and float(matches[0].get("score") or 0.0) >= threshold:
            return "analyze_with_context"
        return "analyze_fresh"

    def _analyze_with_context(self, state: AnalysisState) -> dict:
        best = state["es_matches"][0]
        prompt = self._deps.prompts.render(
            "with_context",
            job_name=state.get("job_name", "unknown"),
            build_number=state.get("build_number", 0),
            stage_name=state.get("stage_name", "unknown"),
            filtered_logs=state.get("filtered_logs", ""),
            past_fingerprint=best.get("fingerprint_text", ""),
            past_solution=best.get("solution", ""),
        )
        system = self._deps.prompts.load("system")

        content = self._deps.llm.invoke(
            [
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=prompt),
            ],
        )
        analysis, suggested_fix = _split_response(content)
        analysis = _strip_match_status_preamble(analysis)
        if not analysis.strip():
            analysis = (
                "The build failed in this stage; the logs point to the issue described "
                "in the step-by-step fix below."
            )
        return {
            "analysis": analysis,
            "suggested_fix": suggested_fix,
            "matched_solution": best.get("solution", ""),
            "match_score": best.get("score", 0.0),
            "recommendation": "verified_past_solution",
        }

    def _analyze_fresh(self, state: AnalysisState) -> dict:
        prompt = self._deps.prompts.render(
            "fresh",
            job_name=state.get("job_name", "unknown"),
            build_number=state.get("build_number", 0),
            stage_name=state.get("stage_name", "unknown"),
            filtered_logs=state.get("filtered_logs", ""),
        )
        system = self._deps.prompts.load("system")

        content = self._deps.llm.invoke(
            [
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=prompt),
            ],
        )
        analysis, suggested_fix = _split_response(content)
        return {
            "analysis": analysis,
            "suggested_fix": suggested_fix,
            "matched_solution": "",
            "match_score": 0.0,
            "recommendation": "fresh_analysis",
        }


# ── Public factory + lazy default graph for backward-compatible callers ──────


def build_graph(deps: "Deps") -> AnalysisGraph:
    """Preferred entry point: build an :class:`AnalysisGraph` for explicit deps."""
    return AnalysisGraph(deps)


_default_graph: AnalysisGraph | None = None


def get_default_graph() -> AnalysisGraph:
    """Lazily build the default graph from module-level ``settings``.

    Preserves the old ``analysis_graph = build_graph()`` import site for
    CLI helpers (``try_worker.py``) without forcing them to know about DI.
    The FastAPI worker uses explicit :func:`build_graph` in its lifespan.
    """
    global _default_graph
    if _default_graph is None:
        from .config import settings
        from .deps import build_deps

        _default_graph = build_graph(build_deps(settings))
    return _default_graph


def reset_default_graph() -> None:
    """Drop the cached default graph (tests + post-config reloads)."""
    global _default_graph
    _default_graph = None


class _DeferredGraph:
    """Lazy shim so ``from failure_analyzer_worker.graph import analysis_graph``
    keeps working without building the graph at import time."""

    def invoke(self, state: AnalysisState | dict) -> dict:
        return get_default_graph().invoke(state)


analysis_graph = _DeferredGraph()


def chat_clarification_reply(
    messages: list[dict[str, str]],
    *,
    fingerprint: str = "",
    log_excerpt: str = "",
    job_name: str = "",
    stage_name: str = "",
    build_number: int = 0,
    analysis: str = "",
    suggested_fix: str = "",
    use_full_log: bool = False,
) -> str:
    """Backward-compat wrapper — delegates to the default graph's chat method."""
    return get_default_graph().chat_clarification(
        messages,
        fingerprint=fingerprint,
        log_excerpt=log_excerpt,
        job_name=job_name,
        stage_name=stage_name,
        build_number=build_number,
        analysis=analysis,
        suggested_fix=suggested_fix,
        use_full_log=use_full_log,
    )
