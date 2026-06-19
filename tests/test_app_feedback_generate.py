import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as app_module
from feedback_lens.db.connection import ensure_schema_updates
from feedback_lens.paths import SCHEMA_PATH


def _connect_app_feedback_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    ensure_schema_updates(conn)

    conn.execute(
        """
        INSERT INTO units (unit_id, unit_code, unit_name, semester, year)
        VALUES (1, 'COMP1001', 'Computing Foundations', 'S1', 2026)
        """
    )
    conn.execute(
        """
        INSERT INTO tutors (tutor_id, institution_identifier, full_name, email)
        VALUES
            (1, 'TUTOR-001', 'Allowed Educator', 'allowed@example.test'),
            (2, 'TUTOR-002', 'Other Educator', 'other@example.test')
        """
    )
    conn.execute(
        """
        INSERT INTO users
            (user_id, email, password_hash, role, display_name, tutor_id)
        VALUES
            (1, 'allowed@example.test', 'unused', 'educator', 'Allowed Educator', 1),
            (2, 'other@example.test', 'unused', 'educator', 'Other Educator', 2)
        """
    )
    conn.execute(
        """
        INSERT INTO unit_tutors (unit_id, tutor_id, role)
        VALUES (1, 1, 'educator')
        """
    )
    conn.execute(
        """
        INSERT INTO assignments
            (assignment_id, unit_id, assignment_name, assignment_type, description)
        VALUES
            (1, 1, 'Reflection Report', 'report', 'Reflect on computing practice.')
        """
    )
    conn.execute(
        """
        INSERT INTO rubrics (rubric_id, assignment_id, version, cleaned_text)
        VALUES (1, 1, 1, 'Analysis')
        """
    )
    conn.execute(
        """
        INSERT INTO student_submissions
            (submission_id, assignment_id, student_identifier, cleaned_text)
        VALUES (1, 1, 'student_001', 'My submission analyses the task.')
        """
    )
    conn.commit()
    return conn


def _generation_result(**overrides):
    values = {
        "generation_id": 123,
        "overall_grade_band": "C",
        "criterion_count": 2,
        "retrieval_cue_count": 1,
        "deduplicated_chunk_count": 4,
        "provider": "nvidia_deepseek",
        "model": "deepseek-ai/deepseek-v4-pro",
        "context_mode": "retrieval",
        "pipeline_version": "planned_retrieval_v1",
        "prompt_template_version": "unit_grounded_feedback_json_v2",
        "retrieval_strategy": "llm_planned_cue_v1",
        "per_cue_top_k": 5,
        "max_final_chunks": 10,
        "feedback_modifier_mode": "system_default",
        "feedback_length": None,
        "feedback_tone": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FeedbackGenerateRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_testing = app_module.app.config.get("TESTING")
        app_module.app.config["TESTING"] = True

    def tearDown(self) -> None:
        app_module.app.config["TESTING"] = self.previous_testing

    def _client_with_user(self, user_id: int, email: str):
        client = app_module.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = user_id
            flask_session["email"] = email
            flask_session["role"] = "educator"
        return client

    def test_generate_feedback_defaults_to_planned_unit_grounded_deepseek(self) -> None:
        conn = _connect_app_feedback_db()
        client = self._client_with_user(1, "allowed@example.test")

        with (
            patch("app.connect_db", return_value=conn),
            patch(
                "app.generate_feedback_for_submission",
                return_value=_generation_result(),
            ) as mock_generate,
        ):
            response = client.post(
                "/api/feedback/generate",
                json={"submission_id": 1},
            )

        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["generation_id"], 123)
        self.assertEqual(payload["provider"], "nvidia_deepseek")
        self.assertEqual(payload["retrieval_strategy"], "llm_planned_cue_v1")
        self.assertEqual(payload["prompt_template_version"], "unit_grounded_feedback_json_v2")
        self.assertEqual(payload["feedback_modifier_mode"], "system_default")
        self.assertIsNone(payload["feedback_length"])
        self.assertIsNone(payload["feedback_tone"])
        mock_generate.assert_called_once()
        _, kwargs = mock_generate.call_args
        self.assertEqual(kwargs["submission_id"], 1)
        self.assertEqual(kwargs["provider"], "nvidia_deepseek")
        self.assertIsNone(kwargs["model"])
        self.assertEqual(kwargs["context_mode"], "retrieval")
        self.assertEqual(kwargs["retrieval_strategy"], "planned")
        self.assertEqual(kwargs["prompt_template_version"], "unit-grounded-v2")
        self.assertEqual(kwargs["feedback_modifier_mode"], "system_default")
        self.assertIsNone(kwargs["feedback_length"])
        self.assertIsNone(kwargs["feedback_tone"])

    def test_generate_feedback_preserves_explicit_overrides(self) -> None:
        conn = _connect_app_feedback_db()
        client = self._client_with_user(1, "allowed@example.test")

        with (
            patch("app.connect_db", return_value=conn),
            patch(
                "app.generate_feedback_for_submission",
                return_value=_generation_result(
                    provider="qwen",
                    model="test-model",
                    retrieval_strategy="assignment_spec_multi_cue_v1",
                    prompt_template_version="baseline_feedback_json_v1",
                    feedback_modifier_mode="custom",
                    feedback_length="concise",
                    feedback_tone="direct_no_fluff",
                ),
            ) as mock_generate,
        ):
            response = client.post(
                "/api/feedback/generate",
                json={
                    "submission_id": 1,
                    "provider": "qwen",
                    "model": "test-model",
                    "strategy": "baseline",
                    "prompt": "baseline_feedback_json_v1",
                    "per_cue_top_k": "3",
                    "max_final_chunks": "8",
                    "temperature": "0.3",
                    "feedback_length": "concise",
                    "feedback_tone": "direct_no_fluff",
                },
            )

        self.assertEqual(response.status_code, 200)
        _, kwargs = mock_generate.call_args
        self.assertEqual(kwargs["provider"], "qwen")
        self.assertEqual(kwargs["model"], "test-model")
        self.assertEqual(kwargs["retrieval_strategy"], "baseline")
        self.assertEqual(kwargs["prompt_template_version"], "baseline_feedback_json_v1")
        self.assertEqual(kwargs["per_cue_top_k"], 3)
        self.assertEqual(kwargs["max_final_chunks"], 8)
        self.assertEqual(kwargs["temperature"], 0.3)
        self.assertEqual(kwargs["feedback_modifier_mode"], "custom")
        self.assertEqual(kwargs["feedback_length"], "concise")
        self.assertEqual(kwargs["feedback_tone"], "direct_no_fluff")

    def test_generate_feedback_uses_direct_prompt_default_for_direct_mode(self) -> None:
        conn = _connect_app_feedback_db()
        client = self._client_with_user(1, "allowed@example.test")

        with (
            patch("app.connect_db", return_value=conn),
            patch(
                "app.generate_feedback_for_submission",
                return_value=_generation_result(
                    context_mode="direct",
                    pipeline_version="baseline_direct_v1",
                    prompt_template_version="baseline_direct_feedback_json_v1",
                    retrieval_strategy="none_direct_v1",
                    per_cue_top_k=0,
                    max_final_chunks=0,
                ),
            ) as mock_generate,
        ):
            response = client.post(
                "/api/feedback/generate",
                json={"submission_id": 1, "mode": "direct"},
            )

        self.assertEqual(response.status_code, 200)
        _, kwargs = mock_generate.call_args
        self.assertEqual(kwargs["context_mode"], "direct")
        self.assertIsNone(kwargs["retrieval_strategy"])
        self.assertIsNone(kwargs["prompt_template_version"])

    def test_generate_feedback_requires_submission_id(self) -> None:
        conn = _connect_app_feedback_db()
        client = self._client_with_user(1, "allowed@example.test")

        with patch("app.connect_db", return_value=conn):
            response = client.post("/api/feedback/generate", json={})

        self.assertEqual(response.status_code, 400)
        self.assertIn("submission_id", response.get_json()["error"])

    def test_generate_feedback_rejects_unassigned_educator(self) -> None:
        conn = _connect_app_feedback_db()
        client = self._client_with_user(2, "other@example.test")

        with (
            patch("app.connect_db", return_value=conn),
            patch("app.generate_feedback_for_submission") as mock_generate,
        ):
            response = client.post(
                "/api/feedback/generate",
                json={"submission_id": 1},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.get_json()["error"],
            "Submission not found or not authorised",
        )
        mock_generate.assert_not_called()

    def test_generate_feedback_maps_pipeline_validation_error_to_bad_request(self) -> None:
        conn = _connect_app_feedback_db()
        client = self._client_with_user(1, "allowed@example.test")

        with (
            patch("app.connect_db", return_value=conn),
            patch(
                "app.generate_feedback_for_submission",
                side_effect=ValueError("Invalid generation settings"),
            ),
        ):
            response = client.post(
                "/api/feedback/generate",
                json={"submission_id": 1},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Invalid generation settings")

    def test_generate_feedback_maps_provider_runtime_error_to_bad_gateway(self) -> None:
        conn = _connect_app_feedback_db()
        client = self._client_with_user(1, "allowed@example.test")

        with (
            patch("app.connect_db", return_value=conn),
            patch(
                "app.generate_feedback_for_submission",
                side_effect=RuntimeError("Missing API key. Please set environment variable NVIDIA_API_KEY."),
            ),
        ):
            response = client.post(
                "/api/feedback/generate",
                json={"submission_id": 1},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("NVIDIA_API_KEY", response.get_json()["error"])

    def test_regenerate_criterion_route_uses_feedback_settings(self) -> None:
        conn = _connect_app_feedback_db()
        conn.execute(
            """
            INSERT INTO generation_runs
                (generation_id, submission_id, assignment_id, rubric_id,
                 pipeline_version, llm_provider, llm_model,
                 prompt_template_version, retrieval_strategy, status)
            VALUES
                (123, 1, 1, 1, 'baseline_retrieval_v1', 'qwen', 'test-model',
                 'baseline_feedback_json_v1', 'assignment_spec_multi_cue_v1',
                 'completed')
            """
        )
        conn.commit()
        client = self._client_with_user(1, "allowed@example.test")

        regenerated = {
            "criterion_id": 1,
            "criterion_name": "Analysis",
            "strengths": "Updated strength.",
            "areas_for_improvement": "Updated improvement.",
            "improvement_suggestion": "Updated suggestion.",
            "suggested_level": "D",
            "evidence_summary": "Updated evidence.",
            "mark": 82,
        }
        with (
            patch("app.connect_db", return_value=conn),
            patch(
                "app.regenerate_feedback_for_criterion",
                return_value=regenerated,
            ) as mock_regenerate,
        ):
            response = client.post(
                "/api/feedback/123/criterion/1/regenerate",
                json={
                    "feedback_length": "concise",
                    "feedback_tone": "direct_no_fluff",
                },
            )

        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["criterion_feedback"], regenerated)
        mock_regenerate.assert_called_once()
        _, kwargs = mock_regenerate.call_args
        self.assertEqual(kwargs["generation_id"], 123)
        self.assertEqual(kwargs["criterion_id"], 1)
        self.assertEqual(kwargs["feedback_modifier_mode"], "custom")
        self.assertEqual(kwargs["feedback_length"], "concise")
        self.assertEqual(kwargs["feedback_tone"], "direct_no_fluff")

    def test_regenerate_criterion_route_accepts_system_default_modifiers(self) -> None:
        conn = _connect_app_feedback_db()
        conn.execute(
            """
            INSERT INTO generation_runs
                (generation_id, submission_id, assignment_id, rubric_id,
                 pipeline_version, llm_provider, llm_model,
                 prompt_template_version, retrieval_strategy, status)
            VALUES
                (123, 1, 1, 1, 'baseline_retrieval_v1', 'qwen', 'test-model',
                 'baseline_feedback_json_v1', 'assignment_spec_multi_cue_v1',
                 'completed')
            """
        )
        conn.commit()
        client = self._client_with_user(1, "allowed@example.test")

        regenerated = {
            "criterion_id": 1,
            "criterion_name": "Analysis",
            "strengths": "Updated strength.",
            "areas_for_improvement": "Updated improvement.",
            "improvement_suggestion": "Updated suggestion.",
            "suggested_level": "D",
            "evidence_summary": "Updated evidence.",
            "mark": 82,
        }
        with (
            patch("app.connect_db", return_value=conn),
            patch(
                "app.regenerate_feedback_for_criterion",
                return_value=regenerated,
            ) as mock_regenerate,
        ):
            response = client.post(
                "/api/feedback/123/criterion/1/regenerate",
                json={"feedback_modifier_mode": "system_default"},
            )

        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["feedback_modifier_mode"], "system_default")
        self.assertIsNone(payload["feedback_length"])
        self.assertIsNone(payload["feedback_tone"])
        _, kwargs = mock_regenerate.call_args
        self.assertEqual(kwargs["feedback_modifier_mode"], "system_default")
        self.assertIsNone(kwargs["feedback_length"])
        self.assertIsNone(kwargs["feedback_tone"])

    def test_regenerate_criterion_route_rejects_unassigned_educator(self) -> None:
        conn = _connect_app_feedback_db()
        conn.execute(
            """
            INSERT INTO generation_runs
                (generation_id, submission_id, assignment_id, rubric_id,
                 pipeline_version, llm_provider, llm_model,
                 prompt_template_version, retrieval_strategy, status)
            VALUES
                (123, 1, 1, 1, 'baseline_retrieval_v1', 'qwen', 'test-model',
                 'baseline_feedback_json_v1', 'assignment_spec_multi_cue_v1',
                 'completed')
            """
        )
        conn.commit()
        client = self._client_with_user(2, "other@example.test")

        with (
            patch("app.connect_db", return_value=conn),
            patch("app.regenerate_feedback_for_criterion") as mock_regenerate,
        ):
            response = client.post(
                "/api/feedback/123/criterion/1/regenerate",
                json={},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.get_json()["error"],
            "Generation not found or not authorised",
        )
        mock_regenerate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
