"""LangGraph workflow for failure analysis.

Nodes
─────
preprocess          → filter logs, build fingerprint, search ES
analyze_with_context→ LLM receives filtered logs + retrieved past solution
analyze_fresh       → LLM receives only filtered logs (no prior match)

The routing edge after *preprocess* picks the right analysis node based on
whether Elasticsearch returned any solutions above the similarity threshold.
"""

from __future__ import annotations

import logging
import re
from typing import Any, TypedDict

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import END, StateGraph

from . import log_processor
from .config import settings

logger = logging.getLogger(__name__)


# ===================================================================
# State flowing through the graph
# ===================================================================

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


# ===================================================================
# Prompts
# ===================================================================

_SYSTEM_PROMPT = (
    "You are a senior CI/CD failure analyst. You help developers understand "
    "and resolve Jenkins build failures. Give concise, actionable answers "
    "with step-by-step fix instructions. Include exact commands and file "
    "changes. Keep the total response under 400 words. Use markdown headings."
)

_WITH_CONTEXT_TEMPLATE = """\
A Jenkins build has failed. Below are the filtered error logs and a similar \
past error with its known resolution.

**Job:** {job_name} #{build_number}
**Stage:** {stage_name}

## Filtered Error Logs
```
{filtered_logs}
```

## Similar Past Error & Resolution (similarity {match_score:.0%})
**Past fingerprint:** {past_fingerprint}
**Resolution:**
{past_solution}

Instructions:
1. **Analysis** – a short paragraph explaining what went wrong, why it happened, \
and whether the past resolution applies (note any adjustments needed).
2. **Step-by-Step Fix** – numbered steps with exact commands or file changes \
in code blocks. Each step should be clear enough to follow without guessing.
3. **Verify** – one command or check to confirm the fix worked.

Keep the total response under 400 words."""

_FRESH_TEMPLATE = """\
A Jenkins build has failed. Below are the filtered error logs.

**Job:** {job_name} #{build_number}
**Stage:** {stage_name}

## Filtered Error Logs
```
{filtered_logs}
```

Analyze the failure and provide:

1. **Analysis** – a short paragraph explaining what went wrong and why, with \
enough technical context for the developer to understand the issue.
2. **Step-by-Step Fix** – numbered steps with exact commands or file changes \
in code blocks. Each step should be clear enough to follow without guessing.
3. **Verify** – one command or check to confirm the fix worked.

Keep the total response under 400 words."""


# ===================================================================
# Node implementations
# ===================================================================

def preprocess(state: AnalysisState) -> dict:
    """Filter logs → fingerprint → ES similarity search."""
    filtered = log_processor.filter_logs(state["raw_logs"])
    fingerprint = log_processor.generate_fingerprint(
        filtered, state["stage_name"]
    )
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
    """Conditional edge: pick the right analysis node."""
    if state.get("es_matches"):
        return "analyze_with_context"
    return "analyze_fresh"


def _split_response(content: str) -> tuple[str, str]:
    """Split LLM response into (analysis, suggested_fix).

    Looks for the first '## Step-by-Step Fix' or '## Step-by-step Fix' heading
    and splits there. Everything before is analysis, everything from that
    heading onward is the suggested fix.
    """
    pattern = re.compile(
        r"(?m)^#{1,6}\s*Step-by-Step\s+Fix",
        re.IGNORECASE,
    )
    match = pattern.search(content)
    if match:
        return content[:match.start()].strip(), content[match.start():].strip()
    return content.strip(), ""


def _get_llm() -> ChatGroq:
    http_client = httpx.Client(verify=False)
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
        match_score=best.get("score", 0),
        past_fingerprint=best.get("fingerprint_text", ""),
        past_solution=best.get("solution", ""),
    )

    resp = _get_llm().invoke([
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ])
    analysis, suggested_fix = _split_response(resp.content)
    return {
        "analysis": analysis,
        "suggested_fix": suggested_fix,
        "matched_solution": best.get("solution", ""),
        "match_score": best.get("score", 0.0),
        "recommendation": "verified_past_solution",
    }


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


# ===================================================================
# Graph assembly
# ===================================================================

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
