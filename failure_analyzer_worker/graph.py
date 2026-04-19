"""LangGraph: preprocess (filter + fingerprint + ES knn) → analyze with/without prior match."""

from __future__ import annotations

import logging
import re
from typing import Any, TypedDict

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import END, StateGraph

from . import log_processor
from .config import settings

logger = logging.getLogger(__name__)


class AnalysisState(TypedDict, total=False):
    # --- inputs (set by the caller) ---
    raw_logs: str
    stage_name: str
    job_name: str
    build_number: int
    build_url: str
    # --- intermediate ---
    filtered_logs: str
    fingerprint: str
    es_matches: list[dict[str, Any]]
    # --- outputs ---
    analysis: str
    suggested_fix: str
    recommendation: str
    matched_solution: str
    match_score: float


_SYSTEM_PROMPT = (
    "You are a senior CI/CD failure analyst. You help developers understand "
    "and resolve Jenkins build failures. Give concise, actionable answers "
    "with step-by-step fix instructions. Include exact commands and file "
    "changes. Keep the total response under 400 words. Always use the exact "
    "markdown headings the user requests (## Analysis, ## Step-by-Step Fix, "
    "## Verify). Never mention retrieval, similarity scores, embeddings, or "
    "whether a past incident \"matched\" the current failure."
)

_WITH_CONTEXT_TEMPLATE = """\
A Jenkins build has failed. Below are the filtered error logs and a \
possibly-related prior incident. The prior incident may or may not apply -- \
judge from the logs.

**Job:** {job_name} #{build_number}
**Stage:** {stage_name}

## Filtered Error Logs
```
{filtered_logs}
```

## Possibly-Related Prior Incident
**Past fingerprint:** {past_fingerprint}
**Resolution from that incident:**
{past_solution}

Respond using exactly these markdown headings, in this order:

## Analysis
A short paragraph explaining what went wrong and why, grounded in the logs \
above. If the prior incident clearly does NOT match (different exception, \
different service, different exit code), ignore it and diagnose from the \
logs alone -- do NOT mention the prior incident, similarity, retrieval, or \
matching.

## Step-by-Step Fix
Numbered steps with exact commands or file changes in code blocks. Each step \
should be clear enough to follow without guessing.

## Verify
One command or check to confirm the fix worked.

Total response under 400 words."""

_FRESH_TEMPLATE = """\
A Jenkins build has failed. Below are the filtered error logs.

**Job:** {job_name} #{build_number}
**Stage:** {stage_name}

## Filtered Error Logs
```
{filtered_logs}
```

Respond using exactly these markdown headings, in this order:

## Analysis
A short paragraph explaining what went wrong and why, with enough technical \
context for the developer to understand the issue. Ground your reasoning in \
the logs above.

## Step-by-Step Fix
Numbered steps with exact commands or file changes in code blocks. Each step \
should be clear enough to follow without guessing.

## Verify
One command or check to confirm the fix worked.

Total response under 400 words."""


def preprocess(state: AnalysisState) -> dict:
    """Filter logs → fingerprint → ES similarity search."""
    raw = state["raw_logs"]
    body = log_processor.filter_logs(raw)
    filtered = log_processor.LogProcessor().format_with_metadata(raw, body)
    fingerprint = log_processor.generate_fingerprint(body, state["stage_name"])
    es_matches = log_processor.search_similar_solutions(fingerprint)

    logger.info(
        "preprocess  job=%s #%d  stage=%s  fp_len=%d  es_hits=%d",
        state.get("job_name", "?"),
        state.get("build_number", 0),
        state.get("stage_name", "?"),
        len(fingerprint),
        len(es_matches),
    )
    return {
        "filtered_logs": filtered,
        "fingerprint": fingerprint,
        "es_matches": es_matches,
    }


def _route(state: AnalysisState) -> str:
    """Only use a past solution if it confidently clears the threshold.

    ``search_similar_solutions`` already filters by ``SIMILARITY_THRESHOLD``,
    so a non-empty ``es_matches`` here means at least one strong neighbour.
    Re-check defensively in case the search ever returns weaker hits.
    """
    matches = state.get("es_matches") or []
    if matches and float(matches[0].get("score") or 0.0) >= settings.similarity_threshold:
        return "analyze_with_context"
    return "analyze_fresh"


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
    """Remove LLM boilerplate that announces ES/retrieval match status (not user-facing)."""
    t = (text or "").strip()
    if not t:
        return t

    # Peel off leading paragraph(s) that only describe match mechanics / scores.
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
                )
            )
        )

        if is_noise:
            t = rest.strip()
            continue
        break

    return t.strip()


def _get_llm() -> ChatGroq:
    # Honor LLM_TLS_VERIFY=0/false to allow corporate proxies with self-signed certs
    # (defaults to verifying). Never silently disable verification.
    verify_env = (settings.llm_tls_verify or "").strip().lower()
    verify: bool = verify_env not in {"0", "false", "no", "off"}
    if not verify:
        logger.warning(
            "LLM_TLS_VERIFY is disabled — Groq TLS certificate will not be checked. "
            "Use this only behind a trusted MITM proxy.",
        )
    http_client = httpx.Client(verify=verify)
    return ChatGroq(
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        groq_api_key=settings.groq_api_key,
        http_client=http_client,
    )


def analyze_with_context(state: AnalysisState) -> dict:
    """Ask the LLM to verify whether a retrieved past solution applies."""
    best = state["es_matches"][0]
    prompt = _WITH_CONTEXT_TEMPLATE.format(
        job_name=state.get("job_name", "unknown"),
        build_number=state.get("build_number", 0),
        stage_name=state.get("stage_name", "unknown"),
        filtered_logs=state.get("filtered_logs", ""),
        past_fingerprint=best.get("fingerprint_text", ""),
        past_solution=best.get("solution", ""),
    )

    resp = _get_llm().invoke([
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ])
    analysis, suggested_fix = _split_response(resp.content)
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


CLARIFY_SYSTEM = (
    "You help refine CI/CD failure analysis. The user may reject or question the suggested fix. "
    "Ask concise follow-up questions or propose a revised fix. Keep under 350 words."
)


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
) -> str:
    """Follow-up chat after initial analysis (e.g. user rejected the suggested fix)."""
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
        ctx_parts.append(f"Filtered log excerpt:\n{log_excerpt[:12000]}")
    ctx = "\n\n".join(ctx_parts)
    sys_content = CLARIFY_SYSTEM + ("\n\n" + ctx if ctx else "")
    lc_messages: list = [SystemMessage(content=sys_content)]
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "assistant":
            lc_messages.append(AIMessage(content=content))
        else:
            lc_messages.append(HumanMessage(content=content))

    logger.info("chat_clarification_reply  msgs=%d  ctx_len=%d", len(messages), len(sys_content))
    resp = _get_llm().invoke(lc_messages)
    out = resp.content
    return out if isinstance(out, str) else str(out)


def analyze_fresh(state: AnalysisState) -> dict:
    """No prior match — ask the LLM to diagnose from scratch."""
    prompt = _FRESH_TEMPLATE.format(
        job_name=state.get("job_name", "unknown"),
        build_number=state.get("build_number", 0),
        stage_name=state.get("stage_name", "unknown"),
        filtered_logs=state.get("filtered_logs", ""),
    )

    resp = _get_llm().invoke([
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ])
    analysis, suggested_fix = _split_response(resp.content)
    return {
        "analysis": analysis,
        "suggested_fix": suggested_fix,
        "matched_solution": "",
        "match_score": 0.0,
        "recommendation": "fresh_analysis",
    }


def build_graph():
    builder = StateGraph(AnalysisState)

    builder.add_node("preprocess", preprocess)
    builder.add_node("analyze_with_context", analyze_with_context)
    builder.add_node("analyze_fresh", analyze_fresh)

    builder.set_entry_point("preprocess")
    builder.add_conditional_edges(
        "preprocess",
        _route,
        {"analyze_with_context": "analyze_with_context",
         "analyze_fresh": "analyze_fresh"},
    )
    builder.add_edge("analyze_with_context", END)
    builder.add_edge("analyze_fresh", END)

    return builder.compile()


analysis_graph = build_graph()
