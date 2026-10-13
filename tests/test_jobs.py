import sqlite3
import unittest
from unittest.mock import patch

from feedback_lens.web.jobs import (
    claim_next_job,
    recover_stale_jobs,
    run_worker_forever,
)


def _job_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE processing_jobs (
            processing_job_id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL DEFAULT 'queued',
            priority INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            locked_by TEXT,
            locked_at TEXT,
            heartbeat_at TEXT,
            last_error TEXT,
            started_at TEXT,
            completed_at TEXT
        )
        """
    )
    conn.commit()
    return conn


class JobLockHandlingTests(unittest.TestCase):
    def test_idle_claim_does_not_begin_a_write_transaction(self) -> None:
        with _job_connection() as conn:
            statements = []
            conn.set_trace_callback(statements.append)

            job = claim_next_job(conn, "worker-test")

            self.assertIsNone(job)
            self.assertFalse(conn.in_transaction)
            self.assertFalse(
                any("BEGIN IMMEDIATE" in statement for statement in statements)
            )

    def test_stale_recovery_does_not_write_when_nothing_is_stale(self) -> None:
        with _job_connection() as conn:
            statements = []
            conn.set_trace_callback(statements.append)

            recovered = recover_stale_jobs(conn)

            self.assertEqual(recovered, 0)
            self.assertFalse(
                any(
                    statement.lstrip().upper().startswith(
                        "UPDATE PROCESSING_JOBS"
                    )
                    for statement in statements
                )
            )

    def test_queued_job_is_claimed_inside_immediate_transaction(self) -> None:
        with _job_connection() as conn:
            conn.execute("INSERT INTO processing_jobs DEFAULT VALUES")
            conn.commit()

            job = claim_next_job(conn, "worker-test")

            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["attempt_count"], 1)
            self.assertEqual(job["locked_by"], "worker-test")

    def test_forever_worker_retries_temporary_database_lock(self) -> None:
        with (
            patch(
                "feedback_lens.web.jobs.run_worker_once",
                side_effect=[
                    sqlite3.OperationalError("database is locked"),
                    SystemExit("stop test loop"),
                ],
            ) as run_once,
            patch("feedback_lens.web.jobs.time.sleep") as sleep,
        ):
            with self.assertRaises(SystemExit):
                run_worker_forever(poll_seconds=0.25)

        self.assertEqual(run_once.call_count, 2)
        sleep.assert_called_once_with(0.25)


if __name__ == "__main__":
    unittest.main()
