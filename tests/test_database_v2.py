import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from feedback_lens.db.connection import connect_db
from feedback_lens.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    DatabaseSchemaError,
    get_schema_version,
    migrate_database,
    require_current_schema,
)
from feedback_lens.paths import SCHEMA_PATH
from feedback_lens.setup.build import initialise_database


def _legacy_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _insert_legacy_sample(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO units
            (unit_id, unit_code, unit_name, semester, year)
        VALUES (1, 'COMP2001', 'Database Systems', 'Semester 1', 2026)
        """
    )
    conn.execute(
        """
        INSERT INTO tutors
            (tutor_id, institution_identifier, full_name, email)
        VALUES (1, 'STAFF-001', 'Sample Marker', 'marker@example.test')
        """
    )
    conn.execute(
        """
        INSERT INTO users
            (user_id, email, password_hash, role, display_name, tutor_id)
        VALUES
            (1, 'chief@example.test', 'unused', 'admin', 'Chief Admin', NULL),
            (2, 'marker@example.test', 'unused', 'educator',
             'Sample Marker', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO unit_tutors(unit_id, tutor_id, role)
        VALUES (1, 1, 'educator')
        """
    )
    conn.execute(
        """
        INSERT INTO assignments
            (assignment_id, unit_id, assignment_name, assignment_code,
             assignment_type, version)
        VALUES (1, 1, 'Database Design Report', 'A1', 'report', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO assignment_specs
            (spec_id, assignment_id, version, cleaned_text)
        VALUES (1, 1, 1, 'Design a normalized relational database.')
        """
    )
    conn.execute(
        """
        INSERT INTO rubrics
            (rubric_id, assignment_id, version, cleaned_text)
        VALUES (1, 1, 1, 'Schema quality and rationale.')
        """
    )
    conn.execute(
        """
        INSERT INTO rubric_criteria
            (criterion_id, rubric_id, criterion_name, criterion_order)
        VALUES (1, 1, 'Schema quality', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO student_submissions
            (submission_id, assignment_id, student_identifier,
             original_file_path, cleaned_text, version)
        VALUES
            (1, 1, '12345678', 'submission.pdf',
             'A student database design.', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO generation_runs
            (generation_id, submission_id, assignment_id, rubric_id,
             pipeline_version, llm_model, prompt_template_version,
             retrieval_strategy, status, completed_at)
        VALUES
            (1, 1, 1, 1, 'baseline_direct_v1', 'sample-model',
             'baseline_direct_feedback_json_v1', 'none_direct_v1',
             'completed', CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO criterion_feedback
            (generation_id, criterion_id, strengths, mark)
        VALUES (1, 1, 'Clear schema.', 75)
        """
    )
    conn.execute(
        """
        INSERT INTO overall_feedback
            (generation_id, overall_comment, final_mark)
        VALUES (1, 'A sound submission.', 75)
        """
    )
    conn.execute(
        """
        INSERT INTO human_reviews
            (generation_id, tutor_id, review_type, approved)
        VALUES (1, 1, 'tutor_review', 1)
        """
    )
    conn.commit()


class DatabaseV2Tests(unittest.TestCase):
    def test_migrations_are_explicit_versioned_and_idempotent(self) -> None:
        with _legacy_connection() as conn:
            self.assertEqual(get_schema_version(conn), 0)
            with self.assertRaises(DatabaseSchemaError):
                require_current_schema(conn)

            self.assertEqual(migrate_database(conn), CURRENT_SCHEMA_VERSION)
            self.assertEqual(get_schema_version(conn), CURRENT_SCHEMA_VERSION)
            self.assertEqual(migrate_database(conn), CURRENT_SCHEMA_VERSION)

            migrations = conn.execute(
                """
                SELECT version, name
                FROM schema_migrations
                ORDER BY version
                """
            ).fetchall()
            self.assertEqual(
                [(row["version"], row["name"]) for row in migrations],
                [
                    (1, "legacy_stabilization"),
                    (2, "database_v2"),
                    (3, "feature_completion"),
                    (4, "embedded_feedback_evaluations"),
                ],
            )

    def test_existing_records_are_backfilled_into_v2(self) -> None:
        with _legacy_connection() as conn:
            _insert_legacy_sample(conn)
            migrate_database(conn)

            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM unit_offerings"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM organization_role_assignments
                    WHERE role = 'chief_admin'
                    """
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM unit_role_assignments
                    WHERE role = 'staff'
                    """
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM students").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM student_enrolments"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM assessment_plans"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM assessment_activities
                    WHERE purpose IN ('formative', 'summative')
                    """
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM submission_attempts"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM submission_participants"
                ).fetchone()[0],
                1,
            )
            revision = conn.execute(
                """
                SELECT status, calculated_total_mark, final_total_mark
                FROM feedback_revisions
                """
            ).fetchone()
            self.assertEqual(revision["status"], "marker_confirmed")
            self.assertEqual(revision["calculated_total_mark"], 75)
            self.assertEqual(revision["final_total_mark"], 75)

            run = conn.execute(
                """
                SELECT spec_id, submission_attempt_id,
                       assessment_plan_version_id, feedback_purpose
                FROM generation_runs
                WHERE generation_id = 1
                """
            ).fetchone()
            self.assertEqual(run["spec_id"], 1)
            self.assertIsNotNone(run["submission_attempt_id"])
            self.assertIsNotNone(run["assessment_plan_version_id"])
            self.assertEqual(run["feedback_purpose"], "summative")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM generation_input_snapshots"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_v2_constraints_support_one_marker_and_future_groups(self) -> None:
        with _legacy_connection() as conn:
            _insert_legacy_sample(conn)
            migrate_database(conn)
            attempt_id = conn.execute(
                "SELECT submission_attempt_id FROM submission_attempts"
            ).fetchone()[0]

            conn.execute(
                """
                INSERT INTO marker_assignments
                    (submission_attempt_id, marker_user_id,
                     assigned_by_user_id)
                VALUES (?, 2, 1)
                """,
                (attempt_id,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO marker_assignments
                        (submission_attempt_id, marker_user_id,
                         assigned_by_user_id)
                    VALUES (?, 1, 1)
                    """,
                    (attempt_id,),
                )

            conn.execute(
                """
                INSERT INTO students(institution_student_identifier)
                VALUES ('87654321')
                """
            )
            second_student_id = conn.execute(
                """
                SELECT student_id
                FROM students
                WHERE institution_student_identifier = '87654321'
                """
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO submission_participants
                    (submission_attempt_id, student_id, participant_role)
                VALUES (?, ?, 'member')
                """,
                (attempt_id, second_student_id),
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM submission_participants
                    WHERE submission_attempt_id = ?
                    """,
                    (attempt_id,),
                ).fetchone()[0],
                2,
            )

            formative_activity_id = conn.execute(
                """
                SELECT assessment_activity_id
                FROM assessment_activities
                WHERE purpose = 'formative'
                """
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO submission_batches
                        (assessment_activity_id, uploaded_by_user_id)
                    VALUES (?, 1)
                    """,
                    (formative_activity_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO submission_attempts
                        (assessment_activity_id, purpose, attempt_number,
                         source_system, visibility)
                    VALUES (?, 'formative', 1, 'student_portal',
                            'assigned_staff')
                    """,
                    (formative_activity_id,),
                )
            formative_attempt_id = conn.execute(
                """
                INSERT INTO submission_attempts
                    (assessment_activity_id, purpose, attempt_number,
                     source_system, visibility)
                VALUES (?, 'formative', 1, 'student_portal',
                        'student_private')
                """,
                (formative_activity_id,),
            ).lastrowid
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO marker_assignments
                        (submission_attempt_id, marker_user_id,
                         assigned_by_user_id)
                    VALUES (?, 2, 1)
                    """,
                    (formative_attempt_id,),
                )

    def test_connection_validates_without_applying_migrations(self) -> None:
        tmp_root = Path.cwd() / "tmp_tests"
        tmp_root.mkdir(exist_ok=True)
        db_path = tmp_root / "database_v2_connection_test.db"
        sidecars = [
            db_path,
            Path(f"{db_path}-journal"),
            Path(f"{db_path}-wal"),
            Path(f"{db_path}-shm"),
        ]
        for path in sidecars:
            path.unlink(missing_ok=True)
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                conn.executescript(
                    SCHEMA_PATH.read_text(encoding="utf-8")
                )

            with self.assertRaises(DatabaseSchemaError):
                connect_db(db_path)

            initialise_database(
                db_path=db_path,
                seed_demo_accounts=False,
            )
            with closing(connect_db(db_path)) as conn:
                self.assertEqual(
                    get_schema_version(conn),
                    CURRENT_SCHEMA_VERSION,
                )
        finally:
            for path in sidecars:
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
