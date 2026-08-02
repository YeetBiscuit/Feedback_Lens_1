from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from feedback_lens.db.connection import connect_db
from feedback_lens.web.config import get_web_settings


def enqueue_job(
    conn: sqlite3.Connection,
    job_type: str,
    *,
    unit_offering_id: int | None = None,
    assessment_plan_id: int | None = None,
    roster_import_id: int | None = None,
    submission_batch_id: int | None = None,
    account_token_id: int | None = None,
    created_by_user_id: int | None = None,
    source_file_path: str | None = None,
    source_content_hash: str | None = None,
    payload: dict | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO processing_jobs
            (job_type, unit_offering_id, assessment_plan_id,
             roster_import_id, submission_batch_id, account_token_id,
             created_by_user_id, source_file_path, source_content_hash,
             payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_type,
            unit_offering_id,
            assessment_plan_id,
            roster_import_id,
            submission_batch_id,
            account_token_id,
            created_by_user_id,
            source_file_path,
            source_content_hash,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def recover_stale_jobs(conn: sqlite3.Connection) -> int:
    stale_seconds = get_web_settings().job_stale_seconds
    cursor = conn.execute(
        """
        UPDATE processing_jobs
        SET status = CASE
                WHEN attempt_count < max_attempts THEN 'queued'
                ELSE 'failed'
            END,
            locked_by = NULL,
            locked_at = NULL,
            heartbeat_at = NULL,
            available_at = CURRENT_TIMESTAMP,
            last_error = COALESCE(
                last_error,
                'Worker heartbeat expired.'
            ),
            completed_at = CASE
                WHEN attempt_count >= max_attempts
                THEN CURRENT_TIMESTAMP
                ELSE completed_at
            END
        WHERE status = 'running'
          AND datetime(COALESCE(heartbeat_at, locked_at))
              <= datetime('now', ?)
        """,
        (f"-{stale_seconds} seconds",),
    )
    return int(cursor.rowcount)


def claim_next_job(
    conn: sqlite3.Connection,
    worker_id: str,
) -> sqlite3.Row | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT *
            FROM processing_jobs
            WHERE status = 'queued'
              AND datetime(available_at) <= datetime('now')
              AND attempt_count < max_attempts
            ORDER BY priority DESC, processing_job_id
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE processing_jobs
            SET status = 'running',
                attempt_count = attempt_count + 1,
                locked_by = ?,
                locked_at = CURRENT_TIMESTAMP,
                heartbeat_at = CURRENT_TIMESTAMP,
                started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                last_error = NULL
            WHERE processing_job_id = ?
              AND status = 'queued'
            """,
            (worker_id, row["processing_job_id"]),
        )
        claimed = conn.execute(
            """
            SELECT *
            FROM processing_jobs
            WHERE processing_job_id = ?
            """,
            (row["processing_job_id"],),
        ).fetchone()
        conn.commit()
        return claimed
    except Exception:
        conn.rollback()
        raise


def update_job_progress(
    conn: sqlite3.Connection,
    processing_job_id: int,
    current: int,
    total: int | None = None,
) -> None:
    conn.execute(
        """
        UPDATE processing_jobs
        SET progress_current = ?,
            progress_total = COALESCE(?, progress_total),
            heartbeat_at = CURRENT_TIMESTAMP
        WHERE processing_job_id = ?
          AND status = 'running'
        """,
        (current, total, processing_job_id),
    )
    conn.commit()


def _dispatch_job(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
) -> dict:
    if job["job_type"] == "account_email":
        from feedback_lens.web.account_service import handle_account_email_job

        return handle_account_email_job(conn, job)
    from feedback_lens.web.upload_service import handle_processing_job

    return handle_processing_job(conn, job)


def run_worker_once(worker_id: str | None = None) -> bool:
    resolved_worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
    with connect_db() as conn:
        recover_stale_jobs(conn)
        conn.commit()
        job = claim_next_job(conn, resolved_worker_id)
    if job is None:
        return False

    job_id = int(job["processing_job_id"])
    try:
        with connect_db() as conn:
            result = _dispatch_job(conn, job)
            conn.execute(
                """
                UPDATE processing_jobs
                SET status = 'succeeded',
                    result_json = ?,
                    progress_current = CASE
                        WHEN progress_total IS NULL
                        THEN progress_current
                        ELSE progress_total
                    END,
                    heartbeat_at = CURRENT_TIMESTAMP,
                    completed_at = CURRENT_TIMESTAMP,
                    locked_by = NULL,
                    locked_at = NULL
                WHERE processing_job_id = ?
                """,
                (json.dumps(result or {}, ensure_ascii=False), job_id),
            )
            conn.commit()
    except Exception as exc:
        with connect_db() as conn:
            current = conn.execute(
                """
                SELECT attempt_count, max_attempts
                FROM processing_jobs
                WHERE processing_job_id = ?
                """,
                (job_id,),
            ).fetchone()
            retry = (
                current is not None
                and int(current["attempt_count"]) < int(current["max_attempts"])
            )
            conn.execute(
                """
                UPDATE processing_jobs
                SET status = ?,
                    available_at = CASE
                        WHEN ? = 1 THEN datetime('now', '+10 seconds')
                        ELSE available_at
                    END,
                    last_error = ?,
                    completed_at = CASE
                        WHEN ? = 1 THEN NULL
                        ELSE CURRENT_TIMESTAMP
                    END,
                    locked_by = NULL,
                    locked_at = NULL,
                    heartbeat_at = NULL
                WHERE processing_job_id = ?
                """,
                (
                    "queued" if retry else "failed",
                    1 if retry else 0,
                    str(exc)[:2000],
                    1 if retry else 0,
                    job_id,
                ),
            )
            conn.commit()
    return True


def run_worker_forever(poll_seconds: float = 1.0) -> None:
    worker_id = f"worker-{uuid.uuid4().hex[:12]}"
    while True:
        worked = run_worker_once(worker_id)
        if not worked:
            time.sleep(poll_seconds)
