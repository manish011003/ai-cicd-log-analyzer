from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app import db
from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _json_safe(val: Any) -> Any:
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, dict):
        return {k: _json_safe(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_json_safe(v) for v in val]
    return val


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_schema()
    yield


app = FastAPI(title="CI Analyzer Web API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SessionCreate(BaseModel):
    job_full_name: str
    build_number: int
    stage_name: str = ""
    build_url: str = ""
    fingerprint: str = ""
    analysis: str = ""
    suggested_fix: str = ""
    filtered_logs: str = ""
    matched_solution: str = ""
    match_score: float = 0.0
    recommendation: str = ""
    similar_past: list[dict[str, Any]] = Field(default_factory=list)


class MessageIn(BaseModel):
    content: str


class FeedbackIn(BaseModel):
    decision: str  # accept | reject


class AgentChatIn(BaseModel):
    message: str
    run_id: str = ""


_ERR_CLASS_PATTERNS = [
    re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Exception|Error|Failure))\b"),
    re.compile(r"\b(error|failed|fatal)\b", re.IGNORECASE),
]


def _extract_error_class(row: dict[str, Any]) -> str:
    for field in ("fingerprint", "analysis", "suggested_fix", "filtered_logs"):
        text = str(row.get(field) or "")
        if not text:
            continue
        for pat in _ERR_CLASS_PATTERNS:
            m = pat.search(text)
            if m:
                token = m.group(1) if m.lastindex else m.group(0)
                return token[:80]
    return "UnknownError"


def _to_result_item(row: dict[str, Any]) -> dict[str, Any]:
    feedback_status = str(row.get("feedback_status") or "")
    return {
        "run_id": str(row.get("id")),
        "job_full_name": row.get("job_full_name") or "",
        "build_number": int(row.get("build_number") or 0),
        "stage_name": row.get("stage_name") or "",
        "stage_id": None,
        "error_class": _extract_error_class(row),
        "signature": (row.get("fingerprint") or "")[:500],
        "cleaned_log": row.get("filtered_logs") or "",
        "source": row.get("recommendation") or "session",
        "timestamp": row.get("created_at"),
        "analysis": row.get("analysis") or "",
        "suggested_fix": row.get("suggested_fix") or "",
        "feedback_status": feedback_status or "new",
    }


def _build_chat_payload(
    row: dict[str, Any] | None,
    payload: list[dict[str, str]],
) -> dict[str, Any]:
    if not row:
        return {
            "messages": payload,
            "fingerprint": "",
            "log_excerpt": "",
            "job_name": "",
            "stage_name": "",
            "build_number": 0,
            "analysis": "",
            "suggested_fix": "",
        }
    return {
        "messages": payload,
        "fingerprint": row.get("fingerprint") or "",
        "log_excerpt": row.get("filtered_logs") or "",
        "job_name": row.get("job_full_name") or "",
        "stage_name": row.get("stage_name") or "",
        "build_number": int(row.get("build_number") or 0),
        "analysis": row.get("analysis") or "",
        "suggested_fix": row.get("suggested_fix") or "",
    }


def _worker_chat(messages: list[dict[str, str]], row: dict[str, Any] | None = None) -> str:
    url = f"{settings.worker_base_url.rstrip('/')}/chat/turn"
    try:
        r = httpx.post(
            url,
            headers={"X-Api-Key": settings.worker_api_key},
            json=_build_chat_payload(row, messages),
            timeout=300.0,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text
        logger.error("worker chat HTTP %s: %s", exc.response.status_code, body)
        status = 429 if exc.response.status_code == 429 else 502
        raise HTTPException(status_code=status, detail=body or str(exc)) from exc
    except httpx.HTTPError as exc:
        logger.exception("worker chat failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    data = r.json()
    return str(data.get("content", ""))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _db_target(url: str) -> dict[str, Any]:
    try:
        p = urlparse(url)
        dbname = (p.path or "").lstrip("/").split("?", 1)[0] or ""
        return {
            "scheme": p.scheme,
            "host": p.hostname,
            "port": p.port,
            "dbname": dbname,
        }
    except Exception:
        return {"error": "could_not_parse_database_url"}


@app.get("/api/diagnostics")
def diagnostics() -> dict[str, Any]:
    """Redacted wiring snapshot for debugging multi-service setups."""
    listener_reachable = False
    listener_detail: str | None = None
    try:
        r = httpx.get(
            f"{settings.listener_base_url.rstrip('/')}/health",
            timeout=3.0,
        )
        listener_reachable = r.status_code == 200
        listener_detail = r.text[:200] if not listener_reachable else None
    except Exception as exc:
        listener_detail = str(exc)

    worker_reachable = False
    worker_detail: str | None = None
    try:
        r = httpx.get(f"{settings.worker_base_url.rstrip('/')}/health", timeout=3.0)
        worker_reachable = r.status_code == 200
        worker_detail = r.text[:200] if not worker_reachable else None
    except Exception as exc:
        worker_detail = str(exc)

    return {
        "database_target": _db_target(settings.database_url),
        "listener_base_url": settings.listener_base_url,
        "listener_http_reachable": listener_reachable,
        "listener_http_error": listener_detail if not listener_reachable else None,
        "worker_base_url": settings.worker_base_url,
        "worker_http_reachable": worker_reachable,
        "worker_http_error": worker_detail if not worker_reachable else None,
    }


@app.get("/api/sessions")
def list_sessions() -> list[dict[str, Any]]:
    rows = db.list_sessions()
    return [_json_safe(dict(r)) for r in rows]


@app.post("/api/sessions")
def create_session(body: SessionCreate) -> dict[str, Any]:
    sid = db.insert_session(
        job_full_name=body.job_full_name,
        build_number=body.build_number,
        stage_name=body.stage_name,
        build_url=body.build_url,
        fingerprint=body.fingerprint,
        analysis=body.analysis,
        suggested_fix=body.suggested_fix,
        filtered_logs=body.filtered_logs,
        matched_solution=body.matched_solution,
        match_score=body.match_score,
        recommendation=body.recommendation,
        similar_past=body.similar_past,
    )
    row = db.fetch_session(sid)
    return {"id": sid, "session": _json_safe(dict(row)) if row else {}}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    row = db.fetch_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    msgs = db.list_messages(session_id)
    return {
        "session": _json_safe(dict(row)),
        "messages": [_json_safe(dict(m)) for m in msgs],
    }


@app.post("/api/sessions/{session_id}/messages")
def post_message(session_id: str, body: MessageIn) -> dict[str, Any]:
    row = db.fetch_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    db.insert_message(session_id, "user", body.content)
    msgs = db.list_messages(session_id)
    payload = [{"role": m["role"], "content": m["content"]} for m in msgs]
    text = _worker_chat(payload, dict(row))
    db.insert_message(session_id, "assistant", text)
    return {"role": "assistant", "content": text}


@app.post("/api/sessions/{session_id}/feedback")
def post_feedback(session_id: str, body: FeedbackIn) -> dict[str, Any]:
    row = db.fetch_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    d = body.decision.lower().strip()
    if d not in ("accept", "reject"):
        raise HTTPException(status_code=400, detail="decision must be accept or reject")

    if d == "accept":
        solution = (row["suggested_fix"] or row["analysis"] or "").strip()
        if not solution:
            raise HTTPException(status_code=400, detail="nothing to store as solution")
        url = f"{settings.worker_base_url.rstrip('/')}/store-solution"
        try:
            r = httpx.post(
                url,
                headers={"X-Api-Key": settings.worker_api_key},
                json={
                    "fingerprint": row["fingerprint"] or "",
                    "solution": solution,
                    "job_name": row["job_full_name"] or "",
                    "stage_name": row["stage_name"] or "",
                    "build_number": int(row["build_number"] or 0),
                    "solution_score": 1.0,
                },
                timeout=120.0,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            logger.exception("worker store-solution failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        db.set_feedback_status(session_id, "accepted")
        return {"status": "accepted", "worker": r.json()}

    db.set_feedback_status(session_id, "rejected")
    return {"status": "rejected"}


@app.get("/api/stats")
def get_stats() -> dict[str, Any]:
    rows = db.list_session_records(limit=1000)
    by_error: dict[str, int] = {}
    for row in rows:
        cls = _extract_error_class(row)
        by_error[cls] = by_error.get(cls, 0) + 1

    feedback_rows = db.list_feedback_totals()
    by_feedback: dict[str, int] = {}
    for item in feedback_rows:
        status = str(item.get("status") or "new").lower()
        by_feedback[status] = int(item.get("count") or 0)

    return {
        "total": len(rows),
        "by_error_class": dict(sorted(by_error.items(), key=lambda kv: kv[1], reverse=True)),
        "by_feedback_status": by_feedback,
    }


@app.get("/api/results")
def list_results(
    limit: int = Query(default=50, ge=1, le=200),
    job: str | None = Query(default=None),
    error_class: str | None = Query(default=None),
) -> dict[str, Any]:
    rows = db.list_session_records(limit=limit, job=job)
    results = [_to_result_item(dict(r)) for r in rows]
    if error_class:
        wanted = error_class.strip().lower()
        results = [r for r in results if str(r.get("error_class", "")).lower() == wanted]
    return {"count": len(results), "results": _json_safe(results)}


@app.get("/api/results/{run_id}")
def get_result_by_run_id(run_id: str) -> dict[str, Any]:
    row = db.fetch_session(run_id)
    if not row:
        return {"count": 0, "results": []}
    return {"count": 1, "results": [_json_safe(_to_result_item(dict(row)))]}


@app.post("/api/listener/poll-once")
def trigger_listener_poll() -> dict[str, Any]:
    try:
        r = httpx.post(
            f"{settings.listener_base_url.rstrip('/')}/poll-once",
            timeout=60.0,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("listener poll proxy failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    payload = r.json() if r.content else {}
    return {"status": "ok", "listener": payload}


@app.post("/agent/chat")
def agent_chat(body: AgentChatIn) -> dict[str, Any]:
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    run_id = body.run_id.strip()
    row: dict[str, Any] | None = None
    if run_id:
        fetched = db.fetch_session(run_id)
        if not fetched:
            raise HTTPException(status_code=404, detail="session not found")
        row = dict(fetched)
        db.insert_message(run_id, "user", message)
        msgs = db.list_messages(run_id)
        payload = [{"role": m["role"], "content": m["content"]} for m in msgs]
        answer = _worker_chat(payload, row)
        db.insert_message(run_id, "assistant", answer)
    else:
        answer = _worker_chat([{"role": "user", "content": message}], None)

    source_counts = {
        "ci_logs": 1 if row and row.get("filtered_logs") else 0,
        "analyses": 1 if row and row.get("analysis") else 0,
        "solutions": 1 if row and row.get("suggested_fix") else 0,
    }
    return {"answer": answer, "sources": source_counts}

