import logging
from datetime import UTC, datetime
from urllib.parse import urlparse

import psycopg

logger = logging.getLogger(__name__)


def _log_database_target(database_url: str) -> None:
    """Log host/port/db (no password) so you can confirm it matches ``docker exec`` target."""
    try:
        p = urlparse(database_url)
        db = (p.path or "/").lstrip("/") or "(unknown)"
        logger.info(
            "Postgres state store using host=%s port=%s database=%s — "
            "psql in the same DB must use this database on this host/port",
            p.hostname or "(none)",
            p.port or 5432,
            db,
        )
    except Exception:
        logger.warning("Could not parse DATABASE_URL for logging")


class PostgresStateStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        _log_database_target(database_url)
        self._ensure_schema()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, autocommit=True)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jenkins_failure_events (
                        job_full_name TEXT NOT NULL,
                        build_number INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'seen',
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        processed_at TIMESTAMPTZ NULL,
                        last_error TEXT NULL,
                        PRIMARY KEY (job_full_name, build_number)
                    )
                    """
                )

    def release_stale_processing(self, stale_minutes: int = 30) -> int:
        """Reset rows stuck in processing (e.g. worker killed mid-request)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jenkins_failure_events
                    SET status = 'failed',
                        last_error = 'stale processing reclaim'
                    WHERE status = 'processing'
                      AND first_seen_at < NOW() - (%s * INTERVAL '1 minute')
                    """,
                    (stale_minutes,),
                )
                return cur.rowcount

    def get_last_processed_build_number(self, job_full_name: str) -> int:
        """Highest build_number we have tracked (any status) for a job, or 0."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(build_number), 0)
                    FROM jenkins_failure_events
                    WHERE job_full_name = %s
                    """,
                    (job_full_name,),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0

    def claim_for_processing(self, job_full_name: str, build_number: int) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jenkins_failure_events (job_full_name, build_number, status)
                    VALUES (%s, %s, 'seen')
                    ON CONFLICT (job_full_name, build_number) DO NOTHING
                    """,
                    (job_full_name, build_number),
                )
                cur.execute(
                    """
                    UPDATE jenkins_failure_events
                    SET status = 'processing'
                    WHERE job_full_name = %s
                      AND build_number = %s
                      AND status IN ('seen', 'failed')
                    RETURNING status
                    """,
                    (job_full_name, build_number),
                )
                row = cur.fetchone()
                ok = bool(row)
                if ok:
                    logger.info(
                        "State DB: claimed job=%s build=%d (status -> processing)",
                        job_full_name,
                        build_number,
                    )
                return ok

    def mark_processed(self, job_full_name: str, build_number: int) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jenkins_failure_events
                    SET status = 'processed',
                        processed_at = %s,
                        attempt_count = attempt_count + 1,
                        last_error = NULL
                    WHERE job_full_name = %s AND build_number = %s
                    """,
                    (datetime.now(UTC), job_full_name, build_number),
                )
                logger.info(
                    "State DB: marked processed job=%s build=%d",
                    job_full_name,
                    build_number,
                )

    def mark_failed(self, job_full_name: str, build_number: int, error: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jenkins_failure_events
                    SET status = 'failed',
                        attempt_count = attempt_count + 1,
                        last_error = %s
                    WHERE job_full_name = %s AND build_number = %s
                    """,
                    (error[:4000], job_full_name, build_number),
                )

    def purge_processed_older_than_days(self, days: int) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM jenkins_failure_events
                    WHERE status = 'processed'
                      AND processed_at IS NOT NULL
                      AND processed_at < NOW() - (%s * INTERVAL '1 day')
                    """,
                    (days,),
                )
                return cur.rowcount
