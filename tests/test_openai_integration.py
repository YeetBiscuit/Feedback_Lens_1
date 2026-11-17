import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from feedback_lens.feedback.llm.config import get_stage_config, resolve_stage_config
from feedback_lens.feedback.llm.openai_provider import OpenAIProvider, OPENAI_BASE_URL
from feedback_lens.feedback.llm.providers import generate_text, validate_feedback_model
from feedback_lens.feedback.pipeline import (
    generate_feedback_for_submission, regenerate_feedback_for_criterion,
)
from feedback_lens.feedback.quality_gate import generate_feedback_with_quality_gate
from feedback_lens.web.staff_portal_service import assignment_feedback_model
from tests.test_feedback_pipeline_modes import (
    _connect_minimal_feedback_db, _feedback_response, _retrieval_result,
)
from tests.test_database_v2 import _legacy_connection, _insert_legacy_sample
from feedback_lens.db.migrations import migrate_database


@contextmanager
def stage_settings(**changes):
    config = {
        stage: {"provider": "openai", "model": f"{stage}-test",
                "request_options": {"reasoning": {"effort": "medium"}}}
        for stage in ("planner", "generator", "revision")
    }
    config.update(changes)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "llm.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        with patch.dict(os.environ, {"FEEDBACK_LENS_LLM_CONFIG": str(path)}):
            yield config


class OpenAIProviderTests(unittest.TestCase):
    @patch("feedback_lens.feedback.llm.openai_provider.OpenAI")
    def test_arbitrary_model_id_and_reasoning_options_reach_responses(self, sdk):
        client = sdk.return_value.__enter__.return_value
        client.responses.create.return_value = SimpleNamespace(status="completed", output_text="OK")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            result = generate_text(
                "Test", provider="openai", model="future-text-model",
                temperature=0.2,
                request_options={"reasoning": {"effort": "high"}, "max_output_tokens": 1234},
            )
        self.assertEqual(result, "OK")
        self.assertEqual(validate_feedback_model("openai", "future-text-model"),
                         ("openai", "future-text-model"))
        sdk.assert_called_once_with(api_key="test-key", base_url=OPENAI_BASE_URL,
                                    timeout=120.0, max_retries=2)
        client.responses.create.assert_called_once_with(
            model="future-text-model", input=[{"role": "user", "content": "Test"}],
            store=False, reasoning={"effort": "high"}, max_output_tokens=1234,
        )

    @patch("feedback_lens.feedback.llm.openai_provider.OpenAI")
    def test_completed_empty_and_incomplete_outputs_are_rejected(self, sdk):
        client = sdk.return_value.__enter__.return_value
        for response in (
            SimpleNamespace(status="completed", output_text=" "),
            SimpleNamespace(status="incomplete", output_text='{"partial":',
                            incomplete_details=SimpleNamespace(reason="max_output_tokens")),
        ):
            with self.subTest(response=response), patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                client.responses.create.return_value = response
                with self.assertRaises(RuntimeError):
                    OpenAIProvider().generate("Test")

    def test_missing_key_and_invalid_options_fail_before_a_request(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                OpenAIProvider().generate("Test")
        for options in (
            {"model": "override"},
            {"reasoning": {"effort": "high"}, "temperature": 0.2},
            {"reasoning": "high"},
            {"max_output_tokens": 0},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                OpenAIProvider().generate("Test", request_options=options)

    def test_explicit_legacy_model_does_not_inherit_openai_options(self):
        with stage_settings():
            configured = resolve_stage_config("generator", "deepseek", "deepseek-v4-pro")
            self.assertEqual(configured.provider, "deepseek")
            self.assertEqual(configured.model, "deepseek-v4-pro")
            self.assertEqual(configured.request_options, {})
            changed_model = resolve_stage_config("generator", "openai", "another-model")
            self.assertEqual(changed_model.request_options, {})
            self.assertEqual(get_stage_config("planner").model, "planner-test")


class StageRoutingTests(unittest.TestCase):
    @patch("feedback_lens.feedback.pipeline.generate_text")
    @patch("feedback_lens.feedback.pipeline.retrieve_relevant_chunks")
    def test_planner_and_generator_are_independent_and_record_actual_models(self, retrieve, generate):
        retrieve.return_value = _retrieval_result()
        generate.side_effect = [json.dumps({"retrieval_cues": [{"text": "reflection"}]}),
                                _feedback_response()]
        with stage_settings(planner={"provider": "qwen", "model": "light-planner"}):
            with _connect_minimal_feedback_db() as conn:
                result = generate_feedback_for_submission(conn, 1)
                planning = conn.execute("SELECT provider, model FROM retrieval_planning_records").fetchone()
                self.assertEqual(tuple(planning), ("qwen", "light-planner"))
                self.assertEqual((result.provider, result.model), ("openai", "generator-test"))
                self.assertIsNone(conn.execute("SELECT temperature FROM generation_runs").fetchone()[0])
        self.assertEqual(generate.call_args_list[0].kwargs["model"], "light-planner")
        self.assertNotIn("request_options", generate.call_args_list[0].kwargs)
        self.assertEqual(generate.call_args_list[1].kwargs["model"], "generator-test")

    @patch("feedback_lens.feedback.quality_gate._run_gate_judges")
    @patch("feedback_lens.feedback.pipeline.generate_text")
    @patch("feedback_lens.feedback.pipeline.retrieve_relevant_chunks")
    def test_automatic_revision_uses_revision_model_and_keeps_independent_planner(self, retrieve, generate, judges):
        retrieve.return_value = _retrieval_result()
        planner = json.dumps({"retrieval_cues": [{"text": "reflection"}]})
        generate.side_effect = [planner, _feedback_response(), planner, _feedback_response("Revised.")]
        judges.side_effect = [
            [{"provider": "nvidia", "model": "test-judge", "label": "test-judge",
              "scores": {dimension: {"score": score} for dimension in
                         ("grounding", "specificity", "actionability")}}]
            for score in (2, 5)
        ]
        with stage_settings(), _connect_minimal_feedback_db() as conn:
            result, report = generate_feedback_with_quality_gate(conn, 1)
            self.assertEqual(result.model, "revision-test")
            self.assertTrue(report["passed"])
            self.assertTrue(report["regenerated"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM generation_runs").fetchone()[0], 2)
        self.assertEqual([call.kwargs["model"] for call in generate.call_args_list],
                         ["planner-test", "generator-test", "planner-test", "revision-test"])
        self.assertIn("PREVIOUS DRAFT", generate.call_args_list[3].args[0])

    @patch("feedback_lens.feedback.pipeline.generate_text", return_value=_feedback_response())
    def test_manual_regeneration_follows_original_run_model(self, generate):
        with stage_settings(), _connect_minimal_feedback_db() as conn:
            result = generate_feedback_for_submission(conn, 1, provider="deepseek",
                                                      model="deepseek-v4-pro", context_mode="direct")
            regenerate_feedback_for_criterion(conn, result.generation_id, 1)
            self.assertEqual(generate.call_args.kwargs["model"], "deepseek-v4-pro")
            self.assertEqual(generate.call_args.kwargs["provider"], "deepseek")
            run = conn.execute("SELECT llm_provider, llm_model FROM generation_runs").fetchone()
            self.assertEqual(tuple(run), ("deepseek", "deepseek-v4-pro"))
            audit = conn.execute("SELECT metadata_json FROM audit_events WHERE event_type = 'feedback.criterion_regenerated'").fetchone()
            self.assertEqual(json.loads(audit[0])["model"], "deepseek-v4-pro")

    @patch("feedback_lens.feedback.pipeline.generate_text", return_value=_feedback_response())
    def test_manual_regeneration_follows_revision_record_even_after_config_changes(self, generate):
        with stage_settings(), _connect_minimal_feedback_db() as conn:
            result = generate_feedback_for_submission(
                conn, 1, context_mode="direct", generation_stage="revision",
            )
            with stage_settings(revision={"provider": "openai", "model": "new-revision"}):
                regenerate_feedback_for_criterion(conn, result.generation_id, 1)
            self.assertEqual(generate.call_args.kwargs["provider"], "openai")
            self.assertEqual(generate.call_args.kwargs["model"], "revision-test")
            options = generate.call_args.kwargs["request_options"]
            self.assertEqual(options["reasoning"], {"effort": "medium"})
            self.assertTrue(options["text"]["format"]["strict"])
            self.assertEqual(options["text"]["format"]["name"], "feedback_v1")

    @patch("feedback_lens.feedback.pipeline.generate_text", return_value=_feedback_response())
    def test_legacy_nvidia_record_without_config_audit_keeps_its_model(self, generate):
        with stage_settings(), _connect_minimal_feedback_db() as conn:
            result = generate_feedback_for_submission(
                conn, 1, provider="nvidia", model="deepseek-ai/deepseek-v4.1-flash",
                context_mode="direct",
            )
            conn.execute("DELETE FROM audit_events WHERE event_type = 'feedback.generation_config'")
            regenerate_feedback_for_criterion(conn, result.generation_id, 1)
            self.assertEqual(generate.call_args.kwargs["provider"], "nvidia")
            self.assertEqual(generate.call_args.kwargs["model"], "deepseek-ai/deepseek-v4.1-flash")
            self.assertNotIn("request_options", generate.call_args.kwargs)

    def test_saved_assignment_settings_are_preserved_and_unset_assignment_uses_backend(self):
        with stage_settings(), _legacy_connection() as conn:
            _insert_legacy_sample(conn)
            migrate_database(conn)
            self.assertEqual(assignment_feedback_model(conn, 1), ("deepseek", "deepseek-v4-pro"))
            self.assertEqual(assignment_feedback_model(conn, 999), ("openai", "generator-test"))
            saved = conn.execute("SELECT default_llm_provider, default_llm_model FROM assessment_plans WHERE legacy_assignment_id=1").fetchone()
            self.assertEqual(tuple(saved), ("deepseek", "deepseek-v4-pro"))


if __name__ == "__main__":
    unittest.main()
