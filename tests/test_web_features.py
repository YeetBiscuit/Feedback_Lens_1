import gc
import hashlib
import io
import os
import re
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import fitz
import app as app_module

from feedback_lens.db.connection import connect_db as open_database
from feedback_lens.db.migrations import migrate_database
from feedback_lens.web.account_service import (
    complete_activation,
    complete_password_reset,
    create_or_rotate_unit_entry,
    current_unit_entry,
    handle_account_email_job,
    request_activation,
    request_password_reset,
    verify_token,
)
from feedback_lens.web.admin_service import (
    commit_roster_import,
    create_assessment,
    create_roster_import,
    create_unit,
    preview_roster_import,
)
from feedback_lens.web.errors import ApiError
from feedback_lens.web.mail import MEMORY_OUTBOX
from feedback_lens.web.storage import StoredUpload
from feedback_lens.web.upload_service import (
    activate_latest_assessment_version,
    create_submission_batch,
    get_submission_batch,
    handle_processing_job,
    resolve_submission_batch_item,
)
from tests.test_database_v2 import (
    _insert_legacy_sample,
    _legacy_connection,
)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stored_upload(path: Path, original_name: str | None = None) -> StoredUpload:
    return StoredUpload(
        original_file_name=original_name or path.name,
        storage_path=path,
        content_hash=_file_hash(path),
        size_bytes=path.stat().st_size,
        extension=path.suffix.lower(),
    )


def _token_from_email(body: str, action: str) -> str:
    match = re.search(
        rf"/account/{action}/([A-Za-z0-9_.-]+)",
        body,
    )
    if match is None:
        raise AssertionError(f"No {action} token found in email.")
    return match.group(1)


class WebFeatureServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_environment = {
            name: os.environ.get(name)
            for name in (
                "FEEDBACK_LENS_ENV",
                "FEEDBACK_LENS_MAIL_BACKEND",
                "FEEDBACK_LENS_PUBLIC_BASE_URL",
                "FEEDBACK_LENS_SECRET_KEY",
                "FEEDBACK_LENS_UPLOAD_ROOT",
            )
        }
        os.environ["FEEDBACK_LENS_MAIL_BACKEND"] = "memory"
        os.environ["FEEDBACK_LENS_PUBLIC_BASE_URL"] = "https://feedback.test"
        os.environ["FEEDBACK_LENS_SECRET_KEY"] = "test-secret"
        MEMORY_OUTBOX.clear()

    def tearDown(self) -> None:
        MEMORY_OUTBOX.clear()
        for name, value in self.previous_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _migrated_connection(self):
        conn = _legacy_connection()
        _insert_legacy_sample(conn)
        migrate_database(conn)
        return conn

    def test_chief_creates_unit_and_assessment_with_unit_admin(self) -> None:
        with self._migrated_connection() as conn:
            unit = create_unit(
                conn,
                1,
                {
                    "course_code": "COMP3999",
                    "course_name": "Honours Project",
                    "academic_year": 2026,
                    "teaching_period": "Semester 2",
                    "unit_admin_user_id": 2,
                },
            )
            offering_id = unit["unit"]["unit_offering_id"]
            assessment = create_assessment(
                conn,
                2,
                offering_id,
                {
                    "title": "Final report",
                    "assessment_code": "A2",
                    "assignment_type": "report",
                    "weight": 60,
                },
            )

            self.assertEqual(assessment["assessment"]["title"], "Final report")
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM unit_role_assignments
                    WHERE unit_offering_id = ?
                      AND user_id = 2
                      AND role = 'unit_admin'
                      AND active = 1
                    """,
                    (offering_id,),
                ).fetchone()[0],
                1,
            )
            self.assertIsNotNone(
                conn.execute(
                    """
                    SELECT legacy_assignment_id
                    FROM assessment_plans
                    WHERE assessment_plan_id = ?
                    """,
                    (assessment["assessment"]["assessment_plan_id"],),
                ).fetchone()[0]
            )

    def test_roster_preview_requires_withdrawal_decision(self) -> None:
        with (
            self._migrated_connection() as conn,
            tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp,
        ):
            offering_id = conn.execute(
                "SELECT unit_offering_id FROM unit_offerings"
            ).fetchone()[0]
            csv_path = Path(tmp) / "roster.csv"
            csv_path.write_text(
                "Student ID,Name,Email\n"
                "87654321,New Student,new.student@example.test\n",
                encoding="utf-8",
            )
            created = create_roster_import(
                conn,
                1,
                offering_id,
                _stored_upload(csv_path),
            )
            roster_id = created["roster_import"]["roster_import_id"]
            preview = preview_roster_import(
                conn,
                1,
                roster_id,
                {
                    "student_id": "Student ID",
                    "name": "Name",
                    "email": "Email",
                },
            )

            self.assertEqual(
                preview["roster_import"]["withdrawal_candidate_count"],
                1,
            )
            with self.assertRaises(ApiError) as raised:
                commit_roster_import(conn, 1, roster_id, None)
            self.assertEqual(raised.exception.status, 409)

            committed = commit_roster_import(conn, 1, roster_id, False)
            self.assertEqual(committed["roster_import"]["status"], "imported")
            self.assertEqual(
                conn.execute(
                    """
                    SELECT status
                    FROM student_enrolments AS enrolment
                    JOIN students AS student
                      ON student.student_id = enrolment.student_id
                    WHERE enrolment.unit_offering_id = ?
                      AND student.institution_student_identifier = '12345678'
                    """,
                    (offering_id,),
                ).fetchone()[0],
                "active",
            )

    def test_activation_email_and_password_reset_invalidate_sessions(self) -> None:
        with self._migrated_connection() as conn:
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
            conn.commit()

            entry = create_or_rotate_unit_entry(conn, 1, offering_id)
            entry_token = entry["activation_url"].rsplit("/", 1)[-1]
            request_activation(
                conn,
                entry_token,
                "12345678",
                "student@example.test",
                "192.0.2.1",
            )
            activation_job = conn.execute(
                """
                SELECT *
                FROM processing_jobs
                WHERE job_type = 'account_email'
                ORDER BY processing_job_id DESC
                LIMIT 1
                """
            ).fetchone()
            handle_account_email_job(conn, activation_job)
            activation_token = _token_from_email(
                MEMORY_OUTBOX[-1].body,
                "activate",
            )
            user_id = complete_activation(
                conn,
                activation_token,
                "a-secure-password",
            )
            self.assertIsNone(
                verify_token(conn, activation_token, "student_activation")
            )

            initial_version = conn.execute(
                "SELECT session_version FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            request_password_reset(
                conn,
                "student@example.test",
                "192.0.2.1",
            )
            reset_job = conn.execute(
                """
                SELECT *
                FROM processing_jobs
                WHERE job_type = 'account_email'
                ORDER BY processing_job_id DESC
                LIMIT 1
                """
            ).fetchone()
            handle_account_email_job(conn, reset_job)
            reset_token = _token_from_email(MEMORY_OUTBOX[-1].body, "reset")
            complete_password_reset(
                conn,
                reset_token,
                "another-secure-password",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT session_version FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()[0],
                initial_version + 1,
            )

    def test_disabled_mail_blocks_activation_only(self) -> None:
        with self._migrated_connection() as conn:
            offering_id = conn.execute(
                "SELECT unit_offering_id FROM unit_offerings"
            ).fetchone()[0]
            os.environ["FEEDBACK_LENS_MAIL_BACKEND"] = "disabled"
            self.assertFalse(
                current_unit_entry(
                    conn,
                    1,
                    offering_id,
                )["mail_configured"]
            )
            with self.assertRaises(ApiError) as raised:
                create_or_rotate_unit_entry(conn, 1, offering_id)
            self.assertEqual(raised.exception.status, 409)

    def test_production_without_mail_configuration_disables_activation(
        self,
    ) -> None:
        with self._migrated_connection() as conn:
            offering_id = conn.execute(
                "SELECT unit_offering_id FROM unit_offerings"
            ).fetchone()[0]
            os.environ["FEEDBACK_LENS_ENV"] = "production"
            os.environ.pop("FEEDBACK_LENS_MAIL_BACKEND", None)
            activation = current_unit_entry(conn, 1, offering_id)
            self.assertFalse(activation["mail_configured"])

    def test_unmatched_valid_pdf_can_be_manually_mapped_and_supersedes_old(self) -> None:
        with (
            self._migrated_connection() as conn,
            tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp,
        ):
            root = Path(tmp)
            os.environ["FEEDBACK_LENS_UPLOAD_ROOT"] = str(root / "uploads")
            offering_id = conn.execute(
                "SELECT unit_offering_id FROM unit_offerings"
            ).fetchone()[0]
            plan_id = conn.execute(
                "SELECT assessment_plan_id FROM assessment_plans"
            ).fetchone()[0]
            student_id = conn.execute(
                """
                SELECT student_id
                FROM students
                WHERE institution_student_identifier = '12345678'
                """
            ).fetchone()[0]
            old_attempt_id = conn.execute(
                """
                SELECT current.submission_attempt_id
                FROM current_summative_attempts AS current
                JOIN assessment_activities AS activity
                  ON activity.assessment_activity_id =
                     current.assessment_activity_id
                WHERE activity.purpose = 'summative'
                  AND current.student_id = ?
                """,
                (student_id,),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO roster_imports
                    (unit_offering_id, uploaded_by_user_id,
                     source_file_name, source_file_path,
                     source_content_hash, status)
                VALUES (?, 1, 'roster.csv', 'roster.csv',
                        'roster-ready', 'imported')
                """,
                (offering_id,),
            )
            conn.commit()

            pdf_path = root / "submission.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text(
                (72, 72),
                "This is the student's replacement database report.",
            )
            document.save(pdf_path)
            document.close()
            zip_path = root / "moodle.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.write(
                    pdf_path,
                    "Careless Student_99999999_assignsubmission_file_"
                    "/submission.pdf",
                )

            created = create_submission_batch(
                conn,
                1,
                plan_id,
                _stored_upload(zip_path),
            )
            job = conn.execute(
                """
                SELECT *
                FROM processing_jobs
                WHERE processing_job_id = ?
                """,
                (created["processing_job_id"],),
            ).fetchone()
            handled = handle_processing_job(conn, job)
            self.assertEqual(handled["status"], "partially_imported")

            batch = get_submission_batch(
                conn,
                1,
                created["submission_batch_id"],
            )
            item = batch["items"][0]
            self.assertEqual(item["item_status"], "unmatched")
            self.assertTrue(item["accepted_file_path"])
            self.assertEqual(
                batch["roster_students"][0][
                    "institution_student_identifier"
                ],
                "12345678",
            )

            resolved = resolve_submission_batch_item(
                conn,
                1,
                item["submission_batch_item_id"],
                student_id=student_id,
            )
            self.assertEqual(resolved["status"], "imported")
            new_attempt_id = resolved["submission_attempt_id"]
            self.assertNotEqual(new_attempt_id, old_attempt_id)
            old_attempt = conn.execute(
                    """
                    SELECT validity_status, superseded_by_attempt_id
                    FROM submission_attempts
                    WHERE submission_attempt_id = ?
                    """,
                    (old_attempt_id,),
                ).fetchone()
            self.assertEqual(
                (old_attempt["validity_status"],
                 old_attempt["superseded_by_attempt_id"]),
                ("superseded", new_attempt_id),
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT submission_attempt_id
                    FROM current_summative_attempts
                    WHERE student_id = ?
                      AND assessment_activity_id = (
                          SELECT assessment_activity_id
                          FROM submission_attempts
                          WHERE submission_attempt_id = ?
                      )
                    """,
                    (student_id, new_attempt_id),
                ).fetchone()[0],
                new_attempt_id,
            )

    def test_new_assessment_version_voids_and_freezes_old_attempts(self) -> None:
        with self._migrated_connection() as conn:
            plan_id = conn.execute(
                "SELECT assessment_plan_id FROM assessment_plans"
            ).fetchone()[0]
            old_attempt_id = conn.execute(
                "SELECT submission_attempt_id FROM submission_attempts"
            ).fetchone()[0]
            result_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM assessment_results
                WHERE submission_attempt_id = ?
                """,
                (old_attempt_id,),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO assignment_specs
                    (assignment_id, version, cleaned_text)
                VALUES (1, 2, 'Updated assignment requirements.')
                """
            )
            conn.execute(
                """
                INSERT INTO rubrics
                    (assignment_id, version, cleaned_text)
                VALUES (1, 2, 'Updated marking criteria.')
                """
            )
            conn.commit()

            activated = activate_latest_assessment_version(
                conn,
                1,
                plan_id,
            )
            self.assertEqual(activated["active_version"]["version"], 2)
            old_attempt = conn.execute(
                """
                SELECT validity_status, invalidated_by_user_id,
                       invalidation_reason
                FROM submission_attempts
                WHERE submission_attempt_id = ?
                """,
                (old_attempt_id,),
            ).fetchone()
            self.assertEqual(old_attempt["validity_status"], "void")
            self.assertEqual(old_attempt["invalidated_by_user_id"], 1)
            self.assertIn("version changed", old_attempt["invalidation_reason"])
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM current_summative_attempts
                    WHERE submission_attempt_id = ?
                    """,
                    (old_attempt_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM assessment_results
                    WHERE submission_attempt_id = ?
                    """,
                    (old_attempt_id,),
                ).fetchone()[0],
                result_count,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    UPDATE submission_attempts
                    SET status = 'processing'
                    WHERE submission_attempt_id = ?
                    """,
                    (old_attempt_id,),
                )


class WebFeatureRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.previous_upload_root = os.environ.get(
            "FEEDBACK_LENS_UPLOAD_ROOT"
        )
        os.environ["FEEDBACK_LENS_UPLOAD_ROOT"] = str(
            Path(self.temporary_directory.name) / "uploads"
        )
        self.database_path = (
            Path(self.temporary_directory.name) / "route-tests.db"
        )
        with _legacy_connection() as source:
            _insert_legacy_sample(source)
            migrate_database(source)
            target = sqlite3.connect(self.database_path)
            try:
                source.backup(target)
            finally:
                target.close()

        def connect_test_database():
            return open_database(self.database_path)

        self.patchers = [
            mock.patch.object(
                app_module,
                "connect_db",
                side_effect=connect_test_database,
            ),
            mock.patch(
                "feedback_lens.web.security.connect_db",
                side_effect=connect_test_database,
            ),
            mock.patch(
                "feedback_lens.web.routes.connect_db",
                side_effect=connect_test_database,
            ),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.previous_testing = app_module.app.config["TESTING"]
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def tearDown(self) -> None:
        app_module.app.config["TESTING"] = self.previous_testing
        self.client = None
        for patcher in reversed(self.patchers):
            patcher.stop()
        if self.previous_upload_root is None:
            os.environ.pop("FEEDBACK_LENS_UPLOAD_ROOT", None)
        else:
            os.environ["FEEDBACK_LENS_UPLOAD_ROOT"] = (
                self.previous_upload_root
            )
        gc.collect()
        self.temporary_directory.cleanup()

    def _authenticate(self, user_id: int = 1) -> None:
        conn = open_database(self.database_path)
        try:
            user = conn.execute(
                """
                SELECT email, role, session_version
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = user_id
            flask_session["email"] = user["email"]
            flask_session["role"] = user["role"]
            flask_session["session_version"] = user["session_version"]
            flask_session["_csrf_token"] = "route-test-csrf"

    def test_admin_routes_enforce_session_scope_and_csrf(self) -> None:
        self.assertEqual(
            self.client.get("/api/admin/units").status_code,
            401,
        )
        self._authenticate(1)
        self.assertEqual(
            self.client.get("/admin/units").status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/admin/unit/1").status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/admin/assessment/1").status_code,
            200,
        )
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as archive:
            archive.writestr(
                "Example Student_12345678_assignsubmission_file_/"
                "submission.pdf",
                b"%PDF-1.4\n",
            )
        zip_buffer.seek(0)
        blocked_batch = self.client.post(
            "/api/admin/assessments/1/submission-batches",
            data={"file": (zip_buffer, "moodle.zip")},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": "route-test-csrf"},
        )
        self.assertEqual(blocked_batch.status_code, 409)
        self.assertEqual(
            list(
                Path(
                    os.environ["FEEDBACK_LENS_UPLOAD_ROOT"]
                ).glob("submission-batches/**/source.zip")
            ),
            [],
        )
        payload = {
            "course_code": "COMP4777",
            "course_name": "Applied Feedback",
            "academic_year": 2026,
            "teaching_period": "Semester 2",
            "unit_admin_user_id": 2,
        }
        self.assertEqual(
            self.client.post("/api/admin/units", json=payload).status_code,
            403,
        )
        response = self.client.post(
            "/api/admin/units",
            json=payload,
            headers={"X-CSRF-Token": "route-test-csrf"},
        )
        self.assertEqual(response.status_code, 201)

        self.client.get("/logout")
        self._authenticate(2)
        forbidden = self.client.post(
            "/api/admin/units",
            json={
                **payload,
                "course_code": "COMP4888",
            },
            headers={"X-CSRF-Token": "route-test-csrf"},
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_roster_upload_preview_and_commit_routes(self) -> None:
        self._authenticate(1)
        offering_id = 1
        previous_limit = app_module.app.config["MAX_CONTENT_LENGTH"]
        app_module.app.config["MAX_CONTENT_LENGTH"] = 64
        try:
            too_large = self.client.post(
                f"/api/admin/unit-offerings/{offering_id}/rosters",
                data={
                    "file": (
                        io.BytesIO(b"x" * 256),
                        "oversized.csv",
                    )
                },
                content_type="multipart/form-data",
                headers={"X-CSRF-Token": "route-test-csrf"},
            )
        finally:
            app_module.app.config["MAX_CONTENT_LENGTH"] = previous_limit
        self.assertEqual(too_large.status_code, 422)
        self.assertEqual(
            too_large.get_json()["error"]["code"],
            "upload_too_large",
        )

        uploaded = self.client.post(
            f"/api/admin/unit-offerings/{offering_id}/rosters",
            data={
                "file": (
                    io.BytesIO(
                        b"ID number,First name,Surname,Email address\n"
                        b"12345678,Example,Student,student@example.test\n"
                    ),
                    "moodle-roster.csv",
                )
            },
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": "route-test-csrf"},
        )
        self.assertEqual(uploaded.status_code, 201)
        roster_id = uploaded.get_json()["roster_import"]["roster_import_id"]
        previewed = self.client.post(
            f"/api/admin/rosters/{roster_id}/preview",
            json={
                "mapping": {
                    "student_id": "ID number",
                    "first_name": "First name",
                    "last_name": "Surname",
                    "email": "Email address",
                }
            },
            headers={"X-CSRF-Token": "route-test-csrf"},
        )
        self.assertEqual(previewed.status_code, 200)
        committed = self.client.post(
            f"/api/admin/rosters/{roster_id}/commit",
            json={"withdraw_missing": False},
            headers={"X-CSRF-Token": "route-test-csrf"},
        )
        self.assertEqual(committed.status_code, 200)
        self.assertEqual(
            committed.get_json()["roster_import"]["status"],
            "imported",
        )


if __name__ == "__main__":
    unittest.main()
