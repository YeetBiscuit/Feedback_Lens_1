import json
import sqlite3
import unittest
from unittest.mock import patch

from feedback_lens.db.connection import ensure_schema_updates
from feedback_lens.feedback.pipeline import generate_feedback_for_submission
from feedback_lens.paths import SCHEMA_PATH


def _connect_minimal_feedback_db() -> sqlite3.Connection:
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
        INSERT INTO assignments
            (assignment_id, unit_id, assignment_name, assignment_type, description)
        VALUES
            (1, 1, 'Reflection Report', 'report', 'Reflect on computing practice.')
        """
    )
    conn.execute(
        """
        INSERT INTO assignment_specs
            (spec_id, assignment_id, version, cleaned_text, retrieval_cues_json)
        VALUES
            (1, 1, 1, 'Write a reflective report using course concepts.', ?)
        """,
        (json.dumps([{"order": 1, "label": "Task", "text": "reflection report"}]),),
    )
    conn.execute(
        """
        INSERT INTO rubrics (rubric_id, assignment_id, version, cleaned_text)
        VALUES (1, 1, 1, 'Analysis | Evidence | Reflection')
        """
    )
    conn.execute(
        """
        INSERT INTO rubric_criteria
            (criterion_id, rubric_id, criterion_name, criterion_description,
             criterion_order, performance_levels_json)
        VALUES
            (1, 1, 'Analysis', 'Quality of analysis', 1, ?)
        """,
        (json.dumps({"D": "Strong analysis", "C": "Sound analysis"}),),
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


class FeedbackPipelineModeTests(unittest.TestCase):
    @patch("feedback_lens.feedback.pipeline.generate_text")
    def test_direct_mode_skips_retrieval_and_records_direct_run(self, mock_generate_text) -> None:
        mock_generate_text.return_value = json.dumps(
            {
                "overall_feedback": {
                    "overall_comment": "Sound direct feedback.",
                    "key_strengths": ["Clear structure"],
                    "priority_improvements": ["Use more evidence"],
                    "overall_grade_band": "C",
                },
                "criterion_feedback": [
                    {
                        "criterion_id": 1,
                        "criterion_name": "Analysis",
                        "strengths": "Identifies relevant ideas.",
                        "areas_for_improvement": "Needs more depth.",
                        "improvement_suggestion": "Add a specific example.",
                        "suggested_level": "C",
                        "evidence_summary": "Based on the submission and rubric.",
                    }
                ],
            }
        )

        with _connect_minimal_feedback_db() as conn:
            result = generate_feedback_for_submission(
                conn,
                submission_id=1,
                provider="qwen",
                model="test-model",
                context_mode="direct",
            )
            run = conn.execute(
                "SELECT * FROM generation_runs WHERE generation_id = ?",
                (result.generation_id,),
            ).fetchone()
            retrieval_count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM retrieval_records
                WHERE generation_id = ?
                """,
                (result.generation_id,),
            ).fetchone()["count"]

        self.assertEqual(result.context_mode, "direct")
        self.assertEqual(result.retrieval_cue_count, 0)
        self.assertEqual(result.deduplicated_chunk_count, 0)
        self.assertEqual(run["pipeline_version"], "baseline_direct_v1")
        self.assertEqual(run["prompt_template_version"], "baseline_direct_feedback_json_v1")
        self.assertEqual(run["retrieval_strategy"], "none_direct_v1")
        self.assertEqual(run["top_k"], 0)
        self.assertEqual(retrieval_count, 0)
        self.assertNotIn("Retrieved course context:", run["prompt_text"])
        self.assertNotIn("Rubric text:", run["prompt_text"])
        self.assertNotIn("Analysis | Evidence | Reflection", run["prompt_text"])
        self.assertIn("Rubric criteria:", run["prompt_text"])
        self.assertIn("assignment specification, rubric, and student submission", run["prompt_text"])
        mock_generate_text.assert_called_once()


if __name__ == "__main__":
    unittest.main()
