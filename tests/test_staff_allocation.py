import unittest
from unittest.mock import patch

import app as app_module

from feedback_lens.db.migrations import migrate_database
from feedback_lens.web.allocation_service import (
    add_unit_staff,
    confirm_allocation,
    list_staff_candidates,
    preview_allocation,
    remove_unit_staff,
    reopen_submission,
)
from feedback_lens.web.errors import ApiError
from tests.test_database_v2 import _insert_legacy_sample, _legacy_connection


def _allocation_connection():
    conn = _legacy_connection()
    _insert_legacy_sample(conn)
    conn.execute(
        """
        INSERT INTO tutors
            (tutor_id, institution_identifier, full_name, email)
        VALUES (2, 'STAFF-002', 'Second Staff', 'second@example.test')
        """
    )
    conn.execute(
        """
        INSERT INTO users
            (user_id, email, password_hash, role, display_name, tutor_id)
        VALUES (3, 'second@example.test', 'unused', 'educator',
                'Second Staff', 2)
        """
    )
    conn.execute(
        """
        INSERT INTO unit_tutors(unit_id, tutor_id, role)
        VALUES (1, 2, 'educator')
        """
    )
    conn.executemany(
        """
        INSERT INTO student_submissions
            (assignment_id, student_identifier, original_file_path,
             cleaned_text, version)
        VALUES (1, ?, 'submission.pdf', 'Additional submission.', 1)
        """,
        [("11111111",), ("22222222",), ("33333333",), ("44444444",)],
    )
    conn.commit()
    migrate_database(conn)
    return conn


def _attempt_ids(conn):
    return [
        int(row["submission_attempt_id"])
        for row in conn.execute(
            """
            SELECT current.submission_attempt_id
            FROM current_summative_attempts AS current
            JOIN students AS student ON student.student_id = current.student_id
            ORDER BY student.institution_student_identifier
            """
        )
    ]


class StaffAllocationTests(unittest.TestCase):
    def test_organization_member_is_searchable_without_an_existing_unit_role(self):
        with _allocation_connection() as conn:
            conn.execute(
                "DELETE FROM unit_role_assignments WHERE user_id = 3"
            )
            candidates = list_staff_candidates(conn, 1, 1, "SECOND@")
            self.assertEqual([row["user_id"] for row in candidates], [3])
            self.assertEqual(candidates[0]["already_staff"], 0)

    def test_partial_email_search_and_staff_reactivation(self):
        with _allocation_connection() as conn:
            candidates = list_staff_candidates(conn, 1, 1, "SECOND@")
            self.assertEqual([row["user_id"] for row in candidates], [3])

            remove_unit_staff(conn, 1, 1, 3)
            self.assertEqual(
                conn.execute(
                    """
                    SELECT active FROM unit_role_assignments
                    WHERE unit_offering_id = 1 AND user_id = 3
                      AND role = 'staff'
                    """
                ).fetchone()[0],
                0,
            )
            add_unit_staff(conn, 1, 1, 3)
            self.assertEqual(
                conn.execute(
                    """
                    SELECT active FROM unit_role_assignments
                    WHERE unit_offering_id = 1 AND user_id = 3
                      AND role = 'staff'
                    """
                ).fetchone()[0],
                1,
            )


class StaffAllocationRouteTests(unittest.TestCase):
    def setUp(self):
        self.previous_testing = app_module.app.config.get("TESTING")
        app_module.app.config["TESTING"] = True

    def tearDown(self):
        app_module.app.config["TESTING"] = self.previous_testing

    @staticmethod
    def _client(user_id, email, role="educator"):
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
            session["email"] = email
            session["role"] = role
            session["session_version"] = 1
            session["_csrf_token"] = "route-csrf"
        return client

    def test_admin_pages_and_allocation_api_render(self):
        conn = _allocation_connection()
        client = self._client(1, "chief@example.test", "admin")
        with (
            patch("app.connect_db", return_value=conn),
            patch(
                "feedback_lens.web.routes.connect_db",
                return_value=conn,
            ),
            patch(
                "feedback_lens.web.security.connect_db",
                return_value=conn,
            ),
        ):
            page = client.get("/admin/assessment/1")
            staff_workspace = client.get("/educator")
            allocation = client.get("/api/admin/assessments/1/allocation")
            preview = client.post(
                "/api/admin/assessments/1/allocation/preview",
                json={
                    "mode": "manual",
                    "submission_attempt_ids": [_attempt_ids(conn)[0]],
                    "staff_user_id": 2,
                },
                headers={"X-CSRF-Token": "route-csrf"},
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn(b'href="/educator">Switch to Staff Workspace</a>', page.data)
        self.assertEqual(staff_workspace.status_code, 200)
        self.assertIn(b"Staff Dashboard", staff_workspace.data)
        self.assertIn(b"Feedback generation model", page.data)
        self.assertIn(b"Change model", page.data)
        self.assertIn(b"Staff allocation", page.data)
        self.assertIn(b'role="tablist"', page.data)
        self.assertIn(b'data-allocation-mode="manual"', page.data)
        self.assertIn(b'data-allocation-mode="equal"', page.data)
        self.assertIn(b'data-allocation-mode="tutorial_groups"', page.data)
        self.assertIn(b"Preview manual allocation", page.data)
        self.assertIn(b"Preview equal distribution", page.data)
        self.assertIn(b"Preview Tutorial allocation", page.data)
        self.assertEqual(allocation.status_code, 200)
        self.assertGreaterEqual(len(allocation.get_json()["candidates"]), 3)
        self.assertEqual(preview.status_code, 200)

    def test_admin_changes_only_the_assignment_default_feedback_model(self):
        conn = _allocation_connection()
        client = self._client(1, "chief@example.test", "admin")
        with (
            patch(
                "feedback_lens.web.routes.connect_db",
                return_value=conn,
            ),
            patch(
                "feedback_lens.web.security.connect_db",
                return_value=conn,
            ),
        ):
            before = client.get("/api/admin/assessments/1")
            changed = client.patch(
                "/api/admin/assessments/1/feedback-model",
                json={"provider": "qwen", "model": "qwen3.7-max"},
                headers={"X-CSRF-Token": "route-csrf"},
            )
            after = client.get("/api/admin/assessments/1")

        self.assertEqual(before.status_code, 200)
        self.assertEqual(
            before.get_json()["assessment"]["default_llm_model"],
            "deepseek-v4-pro",
        )
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.get_json()["existing_feedback_unchanged"])
        self.assertEqual(changed.get_json()["generated_feedback_count"], 1)
        self.assertEqual(
            after.get_json()["assessment"]["default_llm_model"],
            "qwen3.7-max",
        )
        self.assertEqual(
            after.get_json()["feedback_model_history"][0]["old"]["model"],
            "deepseek-v4-pro",
        )
        self.assertEqual(
            conn.execute(
                "SELECT llm_model FROM generation_runs WHERE generation_id = 1"
            ).fetchone()[0],
            "sample-model",
        )

    def test_tutor_cannot_change_assignment_feedback_model(self):
        conn = _allocation_connection()
        client = self._client(2, "marker@example.test")
        with (
            patch(
                "feedback_lens.web.routes.connect_db",
                return_value=conn,
            ),
            patch(
                "feedback_lens.web.security.connect_db",
                return_value=conn,
            ),
        ):
            response = client.patch(
                "/api/admin/assessments/1/feedback-model",
                json={"provider": "qwen", "model": "qwen3.7-max"},
                headers={"X-CSRF-Token": "route-csrf"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            conn.execute(
                """
                SELECT default_llm_model
                FROM assessment_plans
                WHERE assessment_plan_id = 1
                """
            ).fetchone()[0],
            "deepseek-v4-pro",
        )

    def test_staff_workspace_shows_only_current_users_assignments(self):
        conn = _allocation_connection()
        attempt_id = _attempt_ids(conn)[0]
        payload = {
            "mode": "manual",
            "submission_attempt_ids": [attempt_id],
            "staff_user_id": 2,
        }
        preview = preview_allocation(conn, 1, 1, payload)
        payload["preview_hash"] = preview["preview_hash"]
        confirm_allocation(conn, 1, 1, payload)
        staff_client = self._client(2, "marker@example.test")
        other_client = self._client(3, "second@example.test")
        with patch("app.connect_db", return_value=conn):
            staff = staff_client.get("/api/educator/unit/1/submissions")
            other = other_client.get("/api/educator/unit/1/submissions")

        self.assertEqual(staff.status_code, 200)
        self.assertEqual(len(staff.get_json()["submissions"]), 1)
        self.assertEqual(staff.get_json()["submissions"][0]["can_generate"], 1)
        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.get_json()["submissions"], [])

    def test_staff_dashboard_identifies_admin_workspace_access(self):
        conn = _allocation_connection()
        chief_client = self._client(1, "chief@example.test", "admin")
        staff_client = self._client(2, "marker@example.test")
        with patch("app.connect_db", return_value=conn):
            chief = chief_client.get("/api/educator/dashboard")
            staff = staff_client.get("/api/educator/dashboard")

        self.assertEqual(chief.status_code, 200)
        self.assertTrue(chief.get_json()["can_access_admin"])
        self.assertEqual(staff.status_code, 200)
        self.assertFalse(staff.get_json()["can_access_admin"])

    def test_equal_preview_is_deterministic_and_confirmed(self):
        with _allocation_connection() as conn:
            attempts = [
                attempt_id
                for attempt_id in _attempt_ids(conn)
                if conn.execute(
                    """
                    SELECT marking_status
                    FROM submission_workflow_states
                    WHERE submission_attempt_id = ?
                    """,
                    (attempt_id,),
                ).fetchone()[0] != "marker_confirmed"
            ]
            payload = {
                "mode": "equal",
                "submission_attempt_ids": attempts,
                "staff_user_ids": [3, 2],
            }
            first = preview_allocation(conn, 1, 1, payload)
            second = preview_allocation(conn, 1, 1, payload)
            self.assertEqual(first["preview_hash"], second["preview_hash"])
            self.assertEqual(
                [row["new_staff_user_id"] for row in first["operations"]],
                [2, 3, 2, 3],
            )
            payload["preview_hash"] = first["preview_hash"]
            result = confirm_allocation(conn, 1, 1, payload)
            self.assertEqual(result["change_count"], 4)
            assignments = conn.execute(
                """
                SELECT marker_user_id, COUNT(*) AS total
                FROM marker_assignments
                WHERE active = 1
                GROUP BY marker_user_id
                ORDER BY marker_user_id
                """
            ).fetchall()
            self.assertEqual(
                [(row["marker_user_id"], row["total"]) for row in assignments],
                [(2, 2), (3, 2)],
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM user_notifications
                    WHERE event_type = 'submissions_assigned'
                    """
                ).fetchone()[0],
                2,
            )

    def test_incomplete_work_blocks_removal_and_reassignment_notifies_both(self):
        with _allocation_connection() as conn:
            attempt_id = _attempt_ids(conn)[0]
            first_payload = {
                "mode": "manual",
                "submission_attempt_ids": [attempt_id],
                "staff_user_id": 2,
            }
            first = preview_allocation(conn, 1, 1, first_payload)
            first_payload["preview_hash"] = first["preview_hash"]
            confirm_allocation(conn, 1, 1, first_payload)

            with self.assertRaises(ApiError) as blocked:
                remove_unit_staff(conn, 1, 1, 2)
            self.assertEqual(
                blocked.exception.code,
                "staff_has_incomplete_assignments",
            )

            second_payload = {
                "mode": "manual",
                "submission_attempt_ids": [attempt_id],
                "staff_user_id": 3,
            }
            second = preview_allocation(conn, 1, 1, second_payload)
            second_payload["preview_hash"] = second["preview_hash"]
            confirm_allocation(conn, 1, 1, second_payload)
            event_types = [
                row["event_type"]
                for row in conn.execute(
                    """
                    SELECT event_type FROM user_notifications
                    WHERE user_id IN (2, 3)
                    ORDER BY notification_id
                    """
                )
            ]
            self.assertIn("submissions_reassigned_away", event_types)
            self.assertGreaterEqual(event_types.count("submissions_assigned"), 2)

    def test_confirmed_assignment_locks_until_admin_reopens(self):
        with _allocation_connection() as conn:
            attempt_id = _attempt_ids(conn)[0]
            payload = {
                "mode": "manual",
                "submission_attempt_ids": [attempt_id],
                "staff_user_id": 2,
            }
            preview = preview_allocation(conn, 1, 1, payload)
            payload["preview_hash"] = preview["preview_hash"]
            confirm_allocation(conn, 1, 1, payload)
            conn.execute(
                """
                UPDATE submission_workflow_states
                SET marking_status = 'marker_confirmed',
                    marker_confirmed_by_user_id = 2,
                    marker_confirmed_at = CURRENT_TIMESTAMP
                WHERE submission_attempt_id = ?
                """,
                (attempt_id,),
            )
            conn.commit()

            reassign = {
                "mode": "manual",
                "submission_attempt_ids": [attempt_id],
                "staff_user_id": 3,
            }
            with self.assertRaises(ApiError) as locked:
                preview_allocation(conn, 1, 1, reassign)
            self.assertEqual(
                locked.exception.code,
                "allocation_confirmed_locked",
            )

            reopen_submission(conn, 1, 1, attempt_id)
            reopened = preview_allocation(conn, 1, 1, reassign)
            self.assertEqual(reopened["operations"][0]["change_type"], "reassigned")

    def test_completed_assignment_does_not_block_staff_removal(self):
        with _allocation_connection() as conn:
            attempt_id = _attempt_ids(conn)[0]
            payload = {
                "mode": "manual",
                "submission_attempt_ids": [attempt_id],
                "staff_user_id": 2,
            }
            preview = preview_allocation(conn, 1, 1, payload)
            payload["preview_hash"] = preview["preview_hash"]
            confirm_allocation(conn, 1, 1, payload)
            conn.execute(
                """
                UPDATE submission_workflow_states
                SET marking_status = 'marker_confirmed'
                WHERE submission_attempt_id = ?
                """,
                (attempt_id,),
            )
            conn.commit()

            remove_unit_staff(conn, 1, 1, 2)
            self.assertEqual(
                conn.execute(
                    """
                    SELECT active FROM unit_role_assignments
                    WHERE unit_offering_id = 1 AND user_id = 2
                      AND role = 'staff'
                    """
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT active FROM marker_assignments
                    WHERE submission_attempt_id = ?
                    """,
                    (attempt_id,),
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
