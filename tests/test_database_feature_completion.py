import sqlite3
import unittest

from feedback_lens.db.migrations import migrate_database
from tests.test_database_v2 import (
    _insert_legacy_sample,
    _legacy_connection,
)


class DatabaseFeatureCompletionTests(unittest.TestCase):
    def test_final_supplement_adds_only_frozen_workflow_state(self) -> None:
        with _legacy_connection() as conn:
            _insert_legacy_sample(conn)
            migrate_database(conn)

            table_names = {
                row["name"]
                for row in conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                )
            }
            self.assertTrue(
                {
                    "roster_imports",
                    "roster_import_rows",
                    "account_tokens",
                    "processing_jobs",
                    "submission_batch_items",
                    "current_summative_attempts",
                }.issubset(table_names)
            )

            student_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(students)")
            }
            self.assertTrue(
                {"full_name", "institution_email"}.issubset(student_columns)
            )
            self.assertIn(
                "session_version",
                {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(users)")
                },
            )
            self.assertTrue(
                {
                    "validity_status",
                    "superseded_by_attempt_id",
                    "invalidated_by_user_id",
                    "invalidated_at",
                    "invalidation_reason",
                }.issubset(
                    {
                        row["name"]
                        for row in conn.execute(
                            "PRAGMA table_info(submission_attempts)"
                        )
                    }
                )
            )
            self.assertEqual(
                conn.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_roster_account_tokens_and_jobs_enforce_boundaries(self) -> None:
        with _legacy_connection() as conn:
            _insert_legacy_sample(conn)
            migrate_database(conn)

            offering_id = conn.execute(
                "SELECT unit_offering_id FROM unit_offerings"
            ).fetchone()[0]
            student_id = conn.execute(
                """
                SELECT student_id
                FROM students
                WHERE institution_student_identifier = '12345678'
                """
            ).fetchone()[0]

            conn.execute(
                """
                UPDATE students
                SET full_name = 'Example Student',
                    institution_email = 'student@example.test'
                WHERE student_id = ?
                """,
                (student_id,),
            )
            conn.execute(
                """
                INSERT INTO users
                    (email, password_hash, role, display_name,
                     student_identifier)
                VALUES (
                    'student@example.test', 'unused', 'student',
                    'Example Student', '12345678'
                )
                """
            )
            user_id = conn.execute(
                """
                SELECT user_id
                FROM users
                WHERE email = 'student@example.test'
                """
            ).fetchone()[0]
            conn.execute(
                "UPDATE students SET user_id = ? WHERE student_id = ?",
                (user_id, student_id),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO users
                        (email, password_hash, role)
                    VALUES ('STUDENT@example.test', 'unused', 'student')
                    """
                )

            roster_import_id = conn.execute(
                """
                INSERT INTO roster_imports
                    (unit_offering_id, uploaded_by_user_id,
                     source_file_name, source_file_path,
                     source_content_hash, column_mapping_json,
                     status)
                VALUES (
                    ?, 1, 'roster.csv', 'uploads/roster.csv',
                    'roster-hash',
                    '{"student_id":"ID","name":"Name","email":"Email"}',
                    'previewed'
                )
                """,
                (offering_id,),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO roster_import_rows
                    (roster_import_id, source_row_number,
                     raw_data_json, institution_student_identifier,
                     full_name, institution_email, student_id,
                     action)
                VALUES (
                    ?, 2, '{"ID":"12345678"}', '12345678',
                    'Example Student', 'student@example.test', ?,
                    'unchanged'
                )
                """,
                (roster_import_id, student_id),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO roster_import_rows
                        (roster_import_id, source_row_number,
                         raw_data_json)
                    VALUES (?, 3, 'not-json')
                    """,
                    (roster_import_id,),
                )

            conn.execute(
                """
                INSERT INTO account_tokens
                    (public_id, token_hash, token_type,
                     unit_offering_id, issued_by_user_id)
                VALUES (
                    'unit-entry-public', 'unit-entry-hash',
                    'unit_activation_entry', ?, 1
                )
                """,
                (offering_id,),
            )
            activation_token_id = conn.execute(
                """
                INSERT INTO account_tokens
                    (public_id, token_hash, token_type,
                     unit_offering_id, student_id, expires_at)
                VALUES (
                    'activation-public', 'activation-hash',
                    'student_activation', ?, ?,
                    '2099-01-01 00:00:00'
                )
                """,
                (offering_id, student_id),
            ).lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO account_tokens
                        (public_id, token_hash, token_type,
                         unit_offering_id, student_id, expires_at)
                    VALUES (
                        'activation-public-2', 'activation-hash-2',
                        'student_activation', ?, ?,
                        '2099-01-01 00:00:00'
                    )
                    """,
                    (offering_id, student_id),
                )

            conn.execute(
                """
                INSERT INTO processing_jobs
                    (job_type, account_token_id)
                VALUES ('account_email', ?)
                """,
                (activation_token_id,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO processing_jobs(job_type)
                    VALUES ('submission_batch_ingest')
                    """
                )

    def test_replacement_attempt_invalidates_and_freezes_history(self) -> None:
        with _legacy_connection() as conn:
            _insert_legacy_sample(conn)
            migrate_database(conn)

            old_attempt = conn.execute(
                """
                SELECT submission_attempt_id, assessment_activity_id
                FROM submission_attempts
                WHERE legacy_submission_id = 1
                """
            ).fetchone()
            student_id = conn.execute(
                """
                SELECT student_id
                FROM submission_participants
                WHERE submission_attempt_id = ?
                  AND participant_role = 'primary'
                """,
                (old_attempt["submission_attempt_id"],),
            ).fetchone()[0]
            new_attempt_id = conn.execute(
                """
                INSERT INTO submission_attempts
                    (assessment_activity_id, purpose, attempt_number,
                     source_system, visibility, status)
                VALUES (
                    ?, 'summative', 2, 'staff_upload',
                    'assigned_staff', 'ready'
                )
                """,
                (old_attempt["assessment_activity_id"],),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO submission_participants
                    (submission_attempt_id, student_id, participant_role)
                VALUES (?, ?, 'primary')
                """,
                (new_attempt_id, student_id),
            )

            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    UPDATE submission_attempts
                    SET validity_status = 'superseded',
                        superseded_by_attempt_id = ?,
                        invalidated_by_user_id = 1,
                        invalidated_at = CURRENT_TIMESTAMP,
                        invalidation_reason = 'Special consideration'
                    WHERE submission_attempt_id = ?
                    """,
                    (new_attempt_id, old_attempt["submission_attempt_id"]),
                )

            conn.execute(
                """
                UPDATE current_summative_attempts
                SET submission_attempt_id = ?,
                    set_by_user_id = 1,
                    set_at = CURRENT_TIMESTAMP
                WHERE assessment_activity_id = ?
                  AND student_id = ?
                """,
                (
                    new_attempt_id,
                    old_attempt["assessment_activity_id"],
                    student_id,
                ),
            )
            conn.execute(
                """
                UPDATE submission_attempts
                SET validity_status = 'superseded',
                    superseded_by_attempt_id = ?,
                    invalidated_by_user_id = 1,
                    invalidated_at = CURRENT_TIMESTAMP,
                    invalidation_reason = 'Special consideration'
                WHERE submission_attempt_id = ?
                """,
                (new_attempt_id, old_attempt["submission_attempt_id"]),
            )

            old_validity = conn.execute(
                """
                SELECT validity_status, superseded_by_attempt_id
                FROM submission_attempts
                WHERE submission_attempt_id = ?
                """,
                (old_attempt["submission_attempt_id"],),
            ).fetchone()
            self.assertEqual(old_validity["validity_status"], "superseded")
            self.assertEqual(
                old_validity["superseded_by_attempt_id"],
                new_attempt_id,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    UPDATE submission_attempts
                    SET status = 'processing'
                    WHERE submission_attempt_id = ?
                    """,
                    (old_attempt["submission_attempt_id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    UPDATE feedback_revisions
                    SET final_total_mark = 99
                    WHERE submission_attempt_id = ?
                    """,
                    (old_attempt["submission_attempt_id"],),
                )

    def test_scoping_material_deactivation_is_one_way_and_audited(self) -> None:
        with _legacy_connection() as conn:
            _insert_legacy_sample(conn)
            migrate_database(conn)

            material_id = conn.execute(
                """
                INSERT INTO unit_materials
                    (unit_id, material_type, title, cleaned_text)
                VALUES (
                    1, 'scoping_note', 'Scope', 'Relevant course context.'
                )
                """
            ).lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    UPDATE unit_materials
                    SET is_active = 0
                    WHERE material_id = ?
                    """,
                    (material_id,),
                )
            conn.execute(
                """
                UPDATE unit_materials
                SET is_active = 0,
                    deactivated_by_user_id = 1,
                    deactivated_at = CURRENT_TIMESTAMP,
                    deactivation_reason = 'Outdated note'
                WHERE material_id = ?
                """,
                (material_id,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    UPDATE unit_materials
                    SET is_active = 1
                    WHERE material_id = ?
                    """,
                    (material_id,),
                )


if __name__ == "__main__":
    unittest.main()
