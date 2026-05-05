import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS analysis_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_full_name TEXT NOT NULL,
    build_number INTEGER NOT NULL,
    stage_name TEXT NOT NULL DEFAULT '',
    build_url TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    analysis TEXT NOT NULL DEFAULT '',
    suggested_fix TEXT NOT NULL DEFAULT '',
    filtered_logs TEXT NOT NULL DEFAULT '',
    raw_logs TEXT NOT NULL DEFAULT '',
    matched_solution TEXT NOT NULL DEFAULT '',
    match_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    recommendation TEXT NOT NULL DEFAULT '',
    similar_past JSONB NOT NULL DEFAULT '[]'::jsonb,
    feedback_status TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Additive migration: older databases won't have raw_logs.
ALTER TABLE analysis_sessions ADD COLUMN IF NOT EXISTS raw_logs TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS session_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES analysis_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_session_messages_session
ON session_messages(session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_analysis_sessions_updated_at
ON analysis_sessions(updated_at);
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(settings.database_url, autocommit=True)


def init_schema() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
    logger.info("DB schema ready")


def fetch_session(session_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM analysis_sessions WHERE id = %s",
                (session_id,),
            )
            return cur.fetchone()


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, job_full_name, build_number, stage_name, feedback_status, created_at
                FROM analysis_sessions
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def list_messages(session_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, role, content, created_at
                FROM session_messages
                WHERE session_id = %s
                ORDER BY created_at ASC
                """,
                (session_id,),
            )
            return list(cur.fetchall())


def insert_session(
    job_full_name: str,
    build_number: int,
    stage_name: str,
    build_url: str,
    fingerprint: str,
    analysis: str,
    suggested_fix: str,
    filtered_logs: str,
    matched_solution: str,
    match_score: float,
    recommendation: str,
    similar_past: list[Any],
    raw_logs: str = "",
) -> str:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO analysis_sessions (
                    job_full_name, build_number, stage_name, build_url,
                    fingerprint, analysis, suggested_fix, filtered_logs, raw_logs,
                    matched_solution, match_score, recommendation, similar_past
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb
                )
                RETURNING id::text
                """,
                (
                    job_full_name,
                    build_number,
                    stage_name,
                    build_url,
                    fingerprint,
                    analysis,
                    suggested_fix,
                    filtered_logs,
                    raw_logs,
                    matched_solution,
                    match_score,
                    recommendation,
                    Json(similar_past),
                ),
            )
            out = cur.fetchone()
            assert out
            return str(out[0])


def insert_message(session_id: str, role: str, content: str) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO session_messages (session_id, role, content)
                VALUES (%s, %s, %s)
                """,
                (session_id, role, content),
            )
            cur.execute(
                "UPDATE analysis_sessions SET updated_at = NOW() WHERE id = %s",
                (session_id,),
            )


def set_feedback_status(session_id: str, status: str) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE analysis_sessions
                SET feedback_status = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, session_id),
            )


def list_session_records(
    limit: int = 50,
    job: str | None = None,
) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if job:
                cur.execute(
                    """
                    SELECT id, job_full_name, build_number, stage_name, fingerprint,
                           filtered_logs, analysis, suggested_fix, recommendation,
                           feedback_status, created_at
                    FROM analysis_sessions
                    WHERE job_full_name ILIKE %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (f"%{job}%", limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, job_full_name, build_number, stage_name, fingerprint,
                           filtered_logs, analysis, suggested_fix, recommendation,
                           feedback_status, created_at
                    FROM analysis_sessions
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            return list(cur.fetchall())


def list_feedback_totals() -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT COALESCE(NULLIF(feedback_status, ''), 'new') AS status,
                       COUNT(*) AS count
                FROM analysis_sessions
                GROUP BY COALESCE(NULLIF(feedback_status, ''), 'new')
                """
            )
            return list(cur.fetchall())


# ── Retention / janitor ─────────────────────────────────────────────────────
#
# Strategy:
#   * ``purge_old_messages`` deletes *chat turns* past the message TTL, but
#     keeps the session row so the dashboard can still show the build's
#     analysis + feedback.
#   * ``purge_old_sessions`` deletes sessions past the session TTL (which
#     cascades to any remaining messages). Accepted solutions stay in ES, so
#     deleting the Postgres session does not lose institutional knowledge.


def purge_old_messages(days: int) -> int:
    if days <= 0:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM session_messages
                WHERE created_at < NOW() - make_interval(days => %s)
                """,
                (days,),
            )
            return int(cur.rowcount or 0)


def purge_old_sessions(days: int) -> int:
    if days <= 0:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM analysis_sessions
                WHERE updated_at < NOW() - make_interval(days => %s)
                """,
                (days,),
            )
            return int(cur.rowcount or 0)


def run_retention(session_days: int, message_days: int) -> dict[str, int]:
    msgs_deleted = purge_old_messages(message_days)
    sessions_deleted = purge_old_sessions(session_days)
    return {
        "messages_deleted": msgs_deleted,
        "sessions_deleted": sessions_deleted,
    }
