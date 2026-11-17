"""Opt-in paid API smoke test using synthetic data and an in-memory database.

Run: FEEDBACK_LENS_RUN_LIVE_LLM_TESTS=1 python -m unittest tests.test_openai_live
Retrieval is a fixed synthetic chunk and judge verdicts are simulated to exercise
Revision deterministically. This is not a live vector-store or judge evaluation.
"""

import json
import os
import unittest
from unittest.mock import patch

from feedback_lens.feedback.llm.config import get_stage_config
from feedback_lens.feedback.llm.providers import generate_text
from feedback_lens.feedback.pipeline import regenerate_feedback_for_criterion
from feedback_lens.feedback.quality_gate import generate_feedback_with_quality_gate
from tests.test_feedback_pipeline_modes import _connect_minimal_feedback_db, _retrieval_result


@unittest.skipUnless(os.getenv("FEEDBACK_LENS_RUN_LIVE_LLM_TESTS") == "1",
                     "Live API requests require explicit opt-in.")
class OpenAILiveSmokeTests(unittest.TestCase):
    def test_planning_generation_revision_and_teacher_regeneration(self):
        contracts = []

        def checked_generate(*args, **kwargs):
            fmt = kwargs["request_options"]["text"]["format"]
            self.assertEqual(fmt["type"], "json_schema")
            self.assertTrue(fmt["strict"])
            contracts.append(fmt["name"])
            raw = generate_text(*args, **kwargs)
            self.assertIsInstance(json.loads(raw), dict)
            return raw

        for stage in ("planner", "generator", "revision"):
            self.assertEqual(get_stage_config(stage).provider, "openai")
        verdicts = [
            [{"provider": "synthetic", "model": "smoke-test", "label": "smoke-test",
              "scores": {name: {"score": score, "reason": "Synthetic routing check."}
                         for name in ("grounding", "specificity", "actionability")}}]
            for score in (2, 5)
        ]
        with (
            _connect_minimal_feedback_db() as conn,
            patch("feedback_lens.feedback.pipeline.retrieve_relevant_chunks",
                  return_value=_retrieval_result()),
            patch("feedback_lens.feedback.quality_gate._run_gate_judges",
                  side_effect=verdicts),
            patch("feedback_lens.feedback.pipeline.generate_text", side_effect=checked_generate),
        ):
            result, report = generate_feedback_with_quality_gate(conn, 1)
            self.assertTrue(report["regenerated"])
            self.assertTrue(report["passed"])
            self.assertEqual(result.criterion_count, 1)
            self.assertEqual(result.model, get_stage_config("revision").model)
            regenerated = regenerate_feedback_for_criterion(conn, result.generation_id, 1)
            self.assertTrue(regenerated["strengths"])
            self.assertTrue(regenerated["improvement_suggestion"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM retrieval_planning_records WHERE status='completed'").fetchone()[0], 2)
            self.assertEqual(contracts, ["retrieval_plan_v1", "feedback_v1",
                                         "retrieval_plan_v1", "feedback_v1", "feedback_v1"])
            print("Live strict-output smoke test passed: Planner, Generator, Revision, teacher regeneration.")


if __name__ == "__main__":
    unittest.main()
