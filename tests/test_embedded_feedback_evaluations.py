import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module

from feedback_lens.db.connection import connect_db as open_database
from feedback_lens.db.migrations import migrate_database
from tests.test_database_v2 import (
    _insert_legacy_sample,
    _legacy_connection,
)


class EmbeddedFeedbackEvaluationRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "evaluation-route-tests.db"
        )
        with _legacy_connection() as source:
            _insert_legacy_sample(source)
            migrate_database(source)
            source.execute(
                """
                INSERT INTO users
                    (
                        user_id,
                        email,
                        password_hash,
                        role,
                        display_name,
                        student_identifier
                    )
                VALUES
                    (
                        3,
                        'student@example.test',
                        'unused',
                        'student',
                        'Sample Student',
                        '12345678'
                    ),
                    (
                        4,
                        'other-student@example.test',
                        'unused',
                        'student',
                        'Other Student',
                        '87654321'
                    )
                """
            )
            source.execute(
                """
                INSERT INTO tutors
                    (tutor_id, institution_identifier, full_name, email)
                VALUES
                    (
                        2,
                        'STAFF-002',
                        'Second Marker',
                        'second-marker@example.test'
                    )
                """
            )
            source.execute(
                """
                INSERT INTO users
                    (
                        user_id,
                        email,
                        password_hash,
                        role,
                        display_name,
                        tutor_id
                    )
                VALUES
                    (
                        5,
                        'second-marker@example.test',
                        'unused',
                        'educator',
                        'Second Marker',
                        2
                    )
                """
            )
            source.execute(
                """
                INSERT INTO unit_tutors(unit_id, tutor_id, role)
                VALUES (1, 2, 'educator')
                """
            )
            source.commit()
            target = sqlite3.connect(self.database_path)
            try:
                source.backup(target)
            finally:
                target.close()

        def connect_test_database():
            return open_database(self.database_path)

        self.patcher = mock.patch.object(
            app_module,
            "connect_db",
            side_effect=connect_test_database,
        )
        self.patcher.start()
        self.previous_testing = app_module.app.config["TESTING"]
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    def tearDown(self) -> None:
        app_module.app.config["TESTING"] = self.previous_testing
        self.client = None
        self.patcher.stop()
        gc.collect()
        self.temporary_directory.cleanup()

    def _authenticate(self, user_id: int) -> None:
        with open_database(self.database_path) as conn:
            user = conn.execute(
                """
                SELECT email, role, session_version
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = user_id
            flask_session["email"] = user["email"]
            flask_session["role"] = user["role"]
            flask_session["session_version"] = user["session_version"]

    def test_student_can_create_update_read_and_withdraw_evaluation(
        self,
    ) -> None:
        endpoint = "/api/feedback/1/embedded-evaluation"
        self.assertEqual(self.client.get(endpoint).status_code, 401)

        self._authenticate(3)
        missing_consent = self.client.post(
            endpoint,
            json={"rating_usefulness": 5},
        )
        self.assertEqual(missing_consent.status_code, 400)

        created = self.client.post(
            endpoint,
            json={
                "rating_usefulness": 4,
                "comment": "Clear and actionable.",
                "consent_confirmed": True,
            },
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(
            created.get_json()["evaluation"]["rating_usefulness"],
            4,
        )
        self.assertEqual(
            created.get_json()["participant_role"],
            "student",
        )

        updated = self.client.post(
            endpoint,
            json={
                "rating_usefulness": 5,
                "comment": "The priorities were especially useful.",
                "consent_confirmed": True,
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(
            updated.get_json()["evaluation"]["rating_usefulness"],
            5,
        )

        read_back = self.client.get(endpoint)
        self.assertEqual(read_back.status_code, 200)
        self.assertIn(
            "helping you understand",
            read_back.get_json()["question"],
        )

        withdrawn = self.client.delete(endpoint)
        self.assertEqual(withdrawn.status_code, 200)
        self.assertTrue(withdrawn.get_json()["deleted"])
        self.assertIsNone(
            self.client.get(endpoint).get_json()["evaluation"]
        )

    def test_student_cannot_evaluate_another_students_feedback(self) -> None:
        self._authenticate(4)
        response = self.client.post(
            "/api/feedback/1/embedded-evaluation",
            json={
                "rating_usefulness": 4,
                "consent_confirmed": True,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_educator_evaluation_is_stored_separately(self) -> None:
        endpoint = "/api/feedback/1/embedded-evaluation"
        self._authenticate(3)
        self.assertEqual(
            self.client.post(
                endpoint,
                json={
                    "rating_usefulness": 4,
                    "consent_confirmed": True,
                },
            ).status_code,
            200,
        )

        self.client.get("/logout")
        self._authenticate(2)
        response = self.client.post(
            endpoint,
            json={
                "rating_usefulness": 3,
                "comment": "Useful starting point for review.",
                "consent_confirmed": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["participant_role"], "educator")
        self.assertIn("supporting your review", payload["question"])

        with open_database(self.database_path) as conn:
            rows = conn.execute(
                """
                SELECT participant_role, rating_usefulness
                FROM embedded_feedback_evaluations
                WHERE generation_id = 1
                ORDER BY participant_role
                """
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("educator", 3), ("student", 4)],
        )

    def test_multiple_educators_do_not_overwrite_each_other(self) -> None:
        endpoint = "/api/feedback/1/embedded-evaluation"
        self._authenticate(2)
        first = self.client.post(
            endpoint,
            json={
                "rating_usefulness": 3,
                "consent_confirmed": True,
            },
        )
        self.assertEqual(first.status_code, 200)

        self.client.get("/logout")
        self._authenticate(5)
        second = self.client.post(
            endpoint,
            json={
                "rating_usefulness": 5,
                "consent_confirmed": True,
            },
        )
        self.assertEqual(second.status_code, 200)

        with open_database(self.database_path) as conn:
            rows = conn.execute(
                """
                SELECT rating_usefulness, rater_key_hash
                FROM embedded_feedback_evaluations
                WHERE generation_id = 1
                  AND participant_role = 'educator'
                ORDER BY rating_usefulness
                """
            ).fetchall()
        self.assertEqual(
            [row["rating_usefulness"] for row in rows],
            [3, 5],
        )
        self.assertNotEqual(
            rows[0]["rater_key_hash"],
            rows[1]["rater_key_hash"],
        )

        withdrawn = self.client.delete(endpoint)
        self.assertEqual(withdrawn.status_code, 200)
        self.assertTrue(withdrawn.get_json()["deleted"])

        with open_database(self.database_path) as conn:
            remaining = conn.execute(
                """
                SELECT rating_usefulness
                FROM embedded_feedback_evaluations
                WHERE generation_id = 1
                  AND participant_role = 'educator'
                """
            ).fetchall()
        self.assertEqual(
            [row["rating_usefulness"] for row in remaining],
            [3],
        )

    def test_comment_length_and_rating_are_validated(self) -> None:
        self._authenticate(3)
        endpoint = "/api/feedback/1/embedded-evaluation"
        self.assertEqual(
            self.client.post(
                endpoint,
                json={
                    "rating_usefulness": 0,
                    "consent_confirmed": True,
                },
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                endpoint,
                json={
                    "rating_usefulness": 5,
                    "comment": "x" * 1001,
                    "consent_confirmed": True,
                },
            ).status_code,
            400,
        )


if __name__ == "__main__":
    unittest.main()
