from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
    url = f"{settings.worker_base_url.rstrip('/')}/chat/turn"
    try:
        r = httpx.post(
            url,
            headers={"X-Api-Key": settings.worker_api_key},
            json={
                "messages": payload,
                "fingerprint": row["fingerprint"] or "",
                "log_excerpt": row["filtered_logs"] or "",
                "job_name": row["job_full_name"] or "",
                "stage_name": row["stage_name"] or "",
                "build_number": int(row["build_number"] or 0),
                "analysis": row["analysis"] or "",
                "suggested_fix": row["suggested_fix"] or "",
            },
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
    text = data.get("content", "")
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

