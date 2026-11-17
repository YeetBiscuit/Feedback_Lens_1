import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from feedback_lens.feedback.llm.config import StageModelConfig
from feedback_lens.feedback.llm.openai_provider import OpenAIProvider
from feedback_lens.feedback.llm.structured_output import with_structured_output
from feedback_lens.feedback.pipeline import (
    generate_feedback_for_submission, regenerate_feedback_for_criterion,
)
from feedback_lens.feedback.quality_gate import generate_feedback_with_quality_gate
from tests.test_feedback_pipeline_modes import (
    _connect_minimal_feedback_db, _feedback_response, _retrieval_result,
)
from tests.test_openai_integration import stage_settings


PLANNER_RESPONSE = json.dumps({"retrieval_cues": [{
    "order": 1, "label": "Analysis", "text": "reflective analysis",
    "rubric_criterion_ids": [1], "rationale": "Check analysis against course concepts.",
}]})


def completed(text):
    return SimpleNamespace(status="completed", output_text=text)


class StructuredOutputTests(unittest.TestCase):
    def test_contract_overrides_format_preserves_settings_and_does_not_mutate_them(self):
        config = StageModelConfig("openai", "saved-model", {
            "reasoning": {"effort": "medium"}, "max_output_tokens": 1234,
            "text": {"verbosity": "low", "format": {"type": "text"}},
        })
        result = with_structured_output(config, "feedback_v1")
        self.assertEqual(result.model, "saved-model")
        self.assertEqual(result.request_options["max_output_tokens"], 1234)
        self.assertEqual(result.request_options["reasoning"], {"effort": "medium"})
        self.assertEqual(result.request_options["text"]["verbosity"], "low")
        self.assertTrue(result.request_options["text"]["format"]["strict"])
        result.request_options["reasoning"]["effort"] = "high"
        self.assertEqual(config.request_options["reasoning"]["effort"], "medium")
        self.assertEqual(config.request_options["text"]["format"], {"type": "text"})

    def test_other_providers_are_unchanged(self):
        config = StageModelConfig("nvidia", "existing-model", {})
        self.assertIs(with_structured_output(config, "feedback_v1"), config)

    def test_invalid_text_options_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "text request options"):
            with_structured_output(StageModelConfig("openai", "model", {"text": "bad"}),
                                   "feedback_v1")

    @patch("feedback_lens.feedback.llm.openai_provider.OpenAI")
    @patch("feedback_lens.feedback.pipeline.retrieve_relevant_chunks", return_value=_retrieval_result())
    @patch("feedback_lens.feedback.quality_gate._run_gate_judges")
    def test_contracts_reach_api_for_planner_generator_revision_and_manual_regeneration(
        self, judges, retrieve, sdk,
    ):
        client = sdk.return_value.__enter__.return_value
        client.responses.create.side_effect = [completed(t) for t in (
            PLANNER_RESPONSE, _feedback_response(), PLANNER_RESPONSE,
            _feedback_response("Revised."), _feedback_response("Manual."),
        )]
        judges.side_effect = [
            [{"provider": "synthetic", "model": "test", "label": "test",
              "scores": {d: {"score": score} for d in
                         ("grounding", "specificity", "actionability")}}]
            for score in (2, 5)
        ]
        with stage_settings(), patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
                _connect_minimal_feedback_db() as conn:
            result, report = generate_feedback_with_quality_gate(conn, 1)
            self.assertTrue(report["passed"])
            self.assertTrue(report["regenerated"])
            regenerate_feedback_for_criterion(conn, result.generation_id, 1)
            audits = [json.loads(r[0]) for r in conn.execute(
                "SELECT metadata_json FROM audit_events WHERE event_type IN "
                "('feedback.generation_config', 'feedback.planner_config', "
                "'feedback.criterion_regenerated')"
            )]
            self.assertEqual(len(audits), 5)
            self.assertTrue(all(a["request_options"]["text"]["format"]["strict"] for a in audits))
        calls = client.responses.create.call_args_list
        self.assertEqual([c.kwargs["model"] for c in calls],
                         ["planner-test", "generator-test", "planner-test", "revision-test", "revision-test"])
        self.assertEqual([c.kwargs["text"]["format"]["name"] for c in calls],
                         ["retrieval_plan_v1", "feedback_v1", "retrieval_plan_v1", "feedback_v1", "feedback_v1"])
        for call in calls:
            fmt = call.kwargs["text"]["format"]
            self.assertEqual(fmt["type"], "json_schema")
            self.assertTrue(fmt["strict"])
            self.assertFalse(call.kwargs["store"])
            self._assert_closed_objects(fmt["schema"])
        feedback = calls[1].kwargs["text"]["format"]["schema"]["properties"]
        self.assertEqual(set(feedback), {"overall_feedback", "criterion_feedback"})
        criterion = feedback["criterion_feedback"]["items"]["properties"]
        self.assertEqual(criterion["criterion_id"], {"type": "integer"})
        self.assertEqual(criterion["suggested_level"]["enum"], ["N", "P", "C", "D", "HD"])
        planner = calls[0].kwargs["text"]["format"]["schema"]["properties"]
        self.assertEqual(set(planner), {"retrieval_cues"})

    def _assert_closed_objects(self, schema):
        if schema["type"] == "object":
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
            for child in schema["properties"].values():
                self._assert_closed_objects(child)
        elif schema["type"] == "array":
            self._assert_closed_objects(schema["items"])

    @patch("feedback_lens.feedback.llm.openai_provider.OpenAI")
    def test_direct_generation_and_old_run_regeneration_also_use_contract(self, sdk):
        client = sdk.return_value.__enter__.return_value
        client.responses.create.return_value = completed(_feedback_response())
        with stage_settings(), patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
                _connect_minimal_feedback_db() as conn:
            result = generate_feedback_for_submission(conn, 1, context_mode="direct")
            conn.execute("DELETE FROM audit_events WHERE event_type='feedback.generation_config'")
            regenerate_feedback_for_criterion(conn, result.generation_id, 1)
        self.assertEqual(client.responses.create.call_count, 2)
        for call in client.responses.create.call_args_list:
            self.assertEqual(call.kwargs["text"]["format"]["name"], "feedback_v1")

    @patch("feedback_lens.feedback.llm.openai_provider.OpenAI")
    def test_invalid_json_is_recorded_as_failed_without_feedback_rows(self, sdk):
        client = sdk.return_value.__enter__.return_value
        raw = '{"overall_feedback": {},}'
        client.responses.create.return_value = completed(raw)
        with stage_settings(), patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
                _connect_minimal_feedback_db() as conn:
            with self.assertRaisesRegex(ValueError, "Failed to parse LLM JSON"):
                generate_feedback_for_submission(conn, 1, context_mode="direct")
            row = conn.execute("SELECT status,raw_response_text FROM generation_runs").fetchone()
            self.assertEqual(tuple(row), ("failed", raw))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM criterion_feedback").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM overall_feedback").fetchone()[0], 0)

    @patch("feedback_lens.feedback.llm.openai_provider.OpenAI")
    def test_failed_manual_regeneration_preserves_existing_feedback(self, sdk):
        client = sdk.return_value.__enter__.return_value
        client.responses.create.side_effect = [completed(_feedback_response()), completed('{"bad":,}')]
        with stage_settings(), patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), \
                _connect_minimal_feedback_db() as conn:
            result = generate_feedback_for_submission(conn, 1, context_mode="direct")
            before = tuple(conn.execute("SELECT * FROM criterion_feedback").fetchone())
            with self.assertRaises(ValueError):
                regenerate_feedback_for_criterion(conn, result.generation_id, 1)
            self.assertEqual(tuple(conn.execute("SELECT * FROM criterion_feedback").fetchone()), before)

    @patch("feedback_lens.feedback.llm.openai_provider.OpenAI")
    def test_explicit_refusal_is_rejected_even_if_text_is_present(self, sdk):
        client = sdk.return_value.__enter__.return_value
        client.responses.create.return_value = SimpleNamespace(
            status="completed", output_text=_feedback_response(),
            output=[SimpleNamespace(content=[SimpleNamespace(type="refusal")])],
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            with self.assertRaisesRegex(RuntimeError, "refused"):
                OpenAIProvider().generate("test")


if __name__ == "__main__":
    unittest.main()
