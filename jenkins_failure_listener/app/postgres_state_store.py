from datetime import UTC, datetime

import psycopg


class PostgresStateStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
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
                return bool(row)

    def get_status(self, job_full_name: str, build_number: int) -> str | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status
                    FROM jenkins_failure_events
                    WHERE job_full_name = %s AND build_number = %s
                    """,
                    (job_full_name, build_number),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return str(row[0])

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
                      AND processed_at < NOW() - (%s || ' days')::interval
                    """,
                    (days,),
                )
                return cur.rowcount
