import unittest
from types import SimpleNamespace
from unittest.mock import patch

from feedback_lens.feedback import quality_gate


def _judge_results(score):
    """Build mock scores for both judges across all three dimensions."""
    results = []

    for label in ("judge-a", "judge-b"):
        results.append({
            "provider": "test",
            "model": "test-model",
            "label": label,
            "scores": {
                dimension: {
                    "score": score,
                    "reason": "Test assessment",
                    "evidence": "Test evidence",
                    "defects": [],
                    "missing_evidence": [],
                }
                for dimension in quality_gate.DIMENSION_ORDER
            },
        })

    return results


class QualityGateTests(unittest.TestCase):
    def setUp(self):
        self.generator_patcher = patch(
            "feedback_lens.feedback.quality_gate.generate_feedback_for_submission"
        )
        self.mock_generator = self.generator_patcher.start()
        self.addCleanup(self.generator_patcher.stop)

        self.build_text_patcher = patch(
            "feedback_lens.feedback.quality_gate._build_feedback_text",
            return_value=(
                "Mock generated feedback",
                {
                    "run": {
                        "submission_id": 1,
                        "assignment_id": 1,
                    }
                },
            ),
        )
        self.build_text_patcher.start()
        self.addCleanup(self.build_text_patcher.stop)

        self.context_patcher = patch(
            "feedback_lens.feedback.quality_gate._load_judge_context",
            return_value={
                "submission": "Mock submission",
                "spec": "Mock assignment specification",
                "rubric": "Mock rubric",
                "materials": "",
            },
        )
        self.context_patcher.start()
        self.addCleanup(self.context_patcher.stop)

        self.judge_records_patcher = patch(
            "feedback_lens.feedback.quality_gate._record_judge_results"
        )
        self.mock_judge_records = self.judge_records_patcher.start()
        self.addCleanup(self.judge_records_patcher.stop)

        self.verdict_patcher = patch(
            "feedback_lens.feedback.quality_gate._record_verdict"
        )
        self.mock_verdict = self.verdict_patcher.start()
        self.addCleanup(self.verdict_patcher.stop)

        self.mock_generator.side_effect = lambda *args, **kwargs: (
            SimpleNamespace(
                generation_id=self.mock_generator.call_count,
                model="test-model",
                overall_grade_band="C",
            )
        )

    def test_first_attempt_passes_without_regeneration(self):
        with patch(
            "feedback_lens.feedback.quality_gate._run_gate_judges",
            return_value=_judge_results(5),
        ) as mock_judges:
            result, report = quality_gate.generate_feedback_with_quality_gate(
                None, submission_id=1
            )

        self.assertTrue(report["passed"])
        self.assertFalse(report["regenerated"])
        self.assertEqual(len(report["attempts"]), 1)
        self.assertEqual(self.mock_generator.call_count, 1)
        mock_judges.assert_called_once()
        self.mock_verdict.assert_called_once()
        self.assertEqual(self.mock_verdict.call_args.args[2], "passed")

    def test_failed_first_attempt_then_second_attempt_passes(self):
        with patch(
            "feedback_lens.feedback.quality_gate._run_gate_judges",
            side_effect=[
                _judge_results(2),
                _judge_results(5),
            ],
        ):
            result, report = quality_gate.generate_feedback_with_quality_gate(
                None, submission_id=1
            )

        self.assertTrue(report["passed"])
        self.assertTrue(report["regenerated"])
        self.assertEqual(len(report["attempts"]), 2)
        self.assertEqual(self.mock_generator.call_count, 2)
        self.assertEqual(
            self.mock_verdict.call_args.args[2],
            "passed_after_revision",
        )

        second_call = self.mock_generator.call_args_list[1]
        self.assertIsNotNone(second_call.kwargs["revision_notes"])

    def test_all_three_attempts_fail_and_require_review(self):
        with patch(
            "feedback_lens.feedback.quality_gate._run_gate_judges",
            return_value=_judge_results(2),
        ):
            result, report = quality_gate.generate_feedback_with_quality_gate(
                None, submission_id=1
            )

        self.assertFalse(report["passed"])
        self.assertTrue(report["regenerated"])
        self.assertTrue(report["escalated"])
        self.assertEqual(len(report["attempts"]), 3)
        self.assertEqual(self.mock_generator.call_count, 3)
        self.assertEqual(self.mock_verdict.call_args.args[2], "needs_review")

    def test_judge_exception_does_not_mark_feedback_as_approved(self):
        with patch(
            "feedback_lens.feedback.quality_gate._run_gate_judges",
            side_effect=RuntimeError("Mock judge service failure"),
        ):
            result, report = quality_gate.generate_feedback_with_quality_gate(
                None, submission_id=1
            )

        self.assertFalse(report["passed"])
        self.assertTrue(report["judge_failed"])
        self.assertEqual(len(report["attempts"]), 1)
        self.assertEqual(self.mock_generator.call_count, 1)
        self.assertEqual(self.mock_verdict.call_args.args[2], "judge_failed")


if __name__ == "__main__":
    unittest.main()
