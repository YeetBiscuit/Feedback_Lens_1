import json
import sqlite3
import unittest
from unittest.mock import patch

from feedback_lens.db.connection import ensure_schema_updates
from feedback_lens.feedback.pipeline import (
    generate_feedback_for_submission,
    regenerate_feedback_for_criterion,
)
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
    conn.execute(
        """
        INSERT INTO unit_materials
            (material_id, unit_id, material_type, title, week_number, cleaned_text)
        VALUES
            (1, 1, 'lecture_transcript', 'Week 2 Concepts', 2,
             'Course context about reflective analysis.')
        """
    )
    conn.execute(
        """
        INSERT INTO material_chunks
            (chunk_id, material_id, chunk_index, chunk_text)
        VALUES
            (1, 1, 1, 'Reflective analysis connects claims to course concepts.')
        """
    )
    conn.commit()
    return conn


def _feedback_response(overall_comment: str = "Sound feedback.") -> str:
    return json.dumps(
        {
            "overall_feedback": {
                "overall_comment": overall_comment,
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


def _criterion_response(
    strengths: str = "Fresh strength.",
    areas_for_improvement: str = "Fresh improvement area.",
    improvement_suggestion: str = "Fresh next step.",
    suggested_level: str = "D",
    evidence_summary: str = "Fresh evidence summary.",
) -> str:
    return json.dumps(
        {
            "overall_feedback": {
                "overall_comment": "Criterion refreshed.",
                "key_strengths": ["Updated"],
                "priority_improvements": ["Focus"],
                "overall_grade_band": "D",
            },
            "criterion_feedback": [
                {
                    "criterion_id": 1,
                    "criterion_name": "Analysis",
                    "strengths": strengths,
                    "areas_for_improvement": areas_for_improvement,
                    "improvement_suggestion": improvement_suggestion,
                    "suggested_level": suggested_level,
                    "evidence_summary": evidence_summary,
                }
            ],
        }
    )


def _retrieval_result(query_text: str = "retrieval query") -> tuple[str, list[dict], list[dict]]:
    retrieved_chunks = [
        {
            "rank_position": 1,
            "chunk_id": 1,
            "title": "Week 2 Concepts",
            "material_type": "lecture_transcript",
            "week_number": 2,
            "page_number_start": None,
            "page_number_end": None,
            "matched_cues": ["Course concept"],
            "chunk_text": "Reflective analysis connects claims to course concepts.",
        }
    ]
    retrieval_hits = [
        {
            "query_text": query_text,
            "chunk_id": 1,
            "rank_position": 1,
            "similarity_score": 0.9,
        }
    ]
    return "comp1001_2026_s1", retrieved_chunks, retrieval_hits


class FeedbackPipelineModeTests(unittest.TestCase):
    @patch("feedback_lens.feedback.pipeline.generate_text")
    def test_direct_mode_skips_retrieval_and_records_direct_run(self, mock_generate_text) -> None:
        mock_generate_text.return_value = _feedback_response("Sound direct feedback.")

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
        self.assertEqual(run["per_cue_top_k"], 0)
        self.assertEqual(run["max_final_chunks"], 0)
        self.assertEqual(result.per_cue_top_k, 0)
        self.assertEqual(result.max_final_chunks, 0)
        self.assertEqual(retrieval_count, 0)
        self.assertNotIn("Retrieved course context:", run["prompt_text"])
        self.assertNotIn("Rubric text:", run["prompt_text"])
        self.assertNotIn("Analysis | Evidence | Reflection", run["prompt_text"])
        self.assertIn("Rubric criteria:", run["prompt_text"])
        self.assertIn("assignment specification, rubric, and student submission", run["prompt_text"])
        mock_generate_text.assert_called_once()

    def test_retrieval_mode_defaults_to_planned_unit_grounded_deepseek(self) -> None:
        planner_response = json.dumps(
            {
                "retrieval_cues": [
                    {
                        "order": 1,
                        "label": "Reflective analysis concepts",
                        "text": "course concepts for reflective analysis",
                        "rubric_criterion_ids": [1],
                        "rationale": "Needed to ground analysis feedback.",
                    }
                ]
            }
        )
        with (
            patch("feedback_lens.feedback.pipeline.generate_text") as mock_generate_text,
            patch(
                "feedback_lens.feedback.pipeline.retrieve_relevant_chunks"
            ) as mock_retrieve,
        ):
            mock_generate_text.side_effect = [
                planner_response,
                _feedback_response("Sound planned feedback."),
            ]
            mock_retrieve.return_value = _retrieval_result(
                "Reflective analysis concepts\ncourse concepts for reflective analysis"
            )

            with _connect_minimal_feedback_db() as conn:
                result = generate_feedback_for_submission(
                    conn,
                    submission_id=1,
                    context_mode="retrieval",
                )
                run = conn.execute(
                    "SELECT * FROM generation_runs WHERE generation_id = ?",
                    (result.generation_id,),
                ).fetchone()
                planning = conn.execute(
                    """
                    SELECT *
                    FROM retrieval_planning_records
                    WHERE generation_id = ?
                    """,
                    (result.generation_id,),
                ).fetchone()

        retrieval_cues = mock_retrieve.call_args.args[2]
        retrieval_kwargs = mock_retrieve.call_args.kwargs
        self.assertEqual(result.context_mode, "retrieval")
        self.assertEqual(result.provider, "nvidia_deepseek")
        self.assertEqual(result.model, "deepseek-ai/deepseek-v4-pro")
        self.assertEqual(result.retrieval_strategy, "llm_planned_cue_v1")
        self.assertEqual(result.retrieval_cue_count, 1)
        self.assertEqual(result.per_cue_top_k, 5)
        self.assertEqual(result.max_final_chunks, 10)
        self.assertEqual(result.prompt_template_version, "unit_grounded_feedback_json_v2")
        self.assertEqual(result.feedback_modifier_mode, "system_default")
        self.assertIsNone(result.feedback_length)
        self.assertIsNone(result.feedback_tone)
        self.assertEqual(run["llm_provider"], "nvidia_deepseek")
        self.assertEqual(run["llm_model"], "deepseek-ai/deepseek-v4-pro")
        self.assertEqual(run["pipeline_version"], "planned_retrieval_v1")
        self.assertEqual(run["prompt_template_version"], "unit_grounded_feedback_json_v2")
        self.assertEqual(run["retrieval_strategy"], "llm_planned_cue_v1")
        self.assertEqual(run["top_k"], 5)
        self.assertEqual(run["per_cue_top_k"], 5)
        self.assertEqual(run["max_final_chunks"], 10)
        self.assertIsNotNone(planning)
        self.assertEqual(planning["status"], "completed")
        self.assertEqual(planning["strategy"], "llm_planned_cue_v1")
        self.assertNotIn("Feedback customisation requirements:", run["prompt_text"])
        self.assertNotIn("- feedback_length:", run["prompt_text"])
        self.assertNotIn("- feedback_tone:", run["prompt_text"])
        self.assertIn("Retrieved-context grounding requirements:", run["prompt_text"])
        self.assertEqual(retrieval_cues[0]["label"], "Reflective analysis concepts")
        self.assertEqual(retrieval_cues[0]["text"], "course concepts for reflective analysis")
        self.assertEqual(retrieval_kwargs["per_cue_top_k"], 5)
        self.assertEqual(retrieval_kwargs["max_final_chunks"], 10)
        self.assertEqual(mock_generate_text.call_count, 2)

    def test_baseline_retrieval_strategy_still_uses_assignment_spec_cues(self) -> None:
        with (
            patch("feedback_lens.feedback.pipeline.generate_text") as mock_generate_text,
            patch(
                "feedback_lens.feedback.pipeline.retrieve_relevant_chunks"
            ) as mock_retrieve,
        ):
            mock_generate_text.return_value = _feedback_response()
            mock_retrieve.return_value = _retrieval_result("Task\nreflection report")

            with _connect_minimal_feedback_db() as conn:
                result = generate_feedback_for_submission(
                    conn,
                    submission_id=1,
                    provider="qwen",
                    model="test-model",
                    context_mode="retrieval",
                    retrieval_strategy="baseline",
                )
                run = conn.execute(
                    "SELECT * FROM generation_runs WHERE generation_id = ?",
                    (result.generation_id,),
                ).fetchone()
                planning_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM retrieval_planning_records"
                ).fetchone()["count"]

        retrieval_cues = mock_retrieve.call_args.args[2]
        self.assertEqual(result.retrieval_strategy, "assignment_spec_multi_cue_v1")
        self.assertEqual(run["pipeline_version"], "baseline_retrieval_v1")
        self.assertEqual(run["retrieval_strategy"], "assignment_spec_multi_cue_v1")
        self.assertEqual(planning_count, 0)
        self.assertEqual(retrieval_cues[0]["label"], "Task")
        self.assertEqual(retrieval_cues[0]["text"], "reflection report")
        mock_generate_text.assert_called_once()

    def test_retrieval_limits_are_configurable(self) -> None:
        with (
            patch("feedback_lens.feedback.pipeline.generate_text") as mock_generate_text,
            patch(
                "feedback_lens.feedback.pipeline.retrieve_relevant_chunks"
            ) as mock_retrieve,
        ):
            mock_generate_text.return_value = _feedback_response()
            mock_retrieve.return_value = _retrieval_result("Task\nreflection report")

            with _connect_minimal_feedback_db() as conn:
                result = generate_feedback_for_submission(
                    conn,
                    submission_id=1,
                    provider="qwen",
                    model="test-model",
                    context_mode="retrieval",
                    retrieval_strategy="baseline",
                    per_cue_top_k=3,
                    max_final_chunks=8,
                )
                run = conn.execute(
                    "SELECT * FROM generation_runs WHERE generation_id = ?",
                    (result.generation_id,),
                ).fetchone()

        retrieval_kwargs = mock_retrieve.call_args.kwargs
        self.assertEqual(retrieval_kwargs["per_cue_top_k"], 3)
        self.assertEqual(retrieval_kwargs["max_final_chunks"], 8)
        self.assertEqual(run["top_k"], 3)
        self.assertEqual(run["per_cue_top_k"], 3)
        self.assertEqual(run["max_final_chunks"], 8)
        self.assertEqual(result.per_cue_top_k, 3)
        self.assertEqual(result.max_final_chunks, 8)

    def test_unit_grounded_prompt_v2_records_version_and_adds_grounding_rules(self) -> None:
        with (
            patch("feedback_lens.feedback.pipeline.generate_text") as mock_generate_text,
            patch(
                "feedback_lens.feedback.pipeline.retrieve_relevant_chunks"
            ) as mock_retrieve,
        ):
            mock_generate_text.return_value = _feedback_response()
            mock_retrieve.return_value = _retrieval_result("Task\nreflection report")

            with _connect_minimal_feedback_db() as conn:
                result = generate_feedback_for_submission(
                    conn,
                    submission_id=1,
                    provider="qwen",
                    model="test-model",
                    context_mode="retrieval",
                    retrieval_strategy="baseline",
                    prompt_template_version="unit-grounded-v2",
                )
                run = conn.execute(
                    "SELECT * FROM generation_runs WHERE generation_id = ?",
                    (result.generation_id,),
                ).fetchone()

        self.assertEqual(result.prompt_template_version, "unit_grounded_feedback_json_v2")
        self.assertEqual(run["prompt_template_version"], "unit_grounded_feedback_json_v2")
        self.assertIn("Retrieved-context grounding requirements:", run["prompt_text"])
        self.assertIn("In `improvement_suggestion`, connect advice", run["prompt_text"])
        self.assertIn("Week 2 Concepts", run["prompt_text"])

    def test_explicit_baseline_retrieval_prompt_v1_still_works(self) -> None:
        with (
            patch("feedback_lens.feedback.pipeline.generate_text") as mock_generate_text,
            patch(
                "feedback_lens.feedback.pipeline.retrieve_relevant_chunks"
            ) as mock_retrieve,
        ):
            mock_generate_text.return_value = _feedback_response()
            mock_retrieve.return_value = _retrieval_result("Task\nreflection report")

            with _connect_minimal_feedback_db() as conn:
                result = generate_feedback_for_submission(
                    conn,
                    submission_id=1,
                    provider="qwen",
                    model="test-model",
                    context_mode="retrieval",
                    retrieval_strategy="baseline",
                    prompt_template_version="baseline_feedback_json_v1",
                )
                run = conn.execute(
                    "SELECT * FROM generation_runs WHERE generation_id = ?",
                    (result.generation_id,),
                ).fetchone()

        self.assertEqual(result.prompt_template_version, "baseline_feedback_json_v1")
        self.assertEqual(run["prompt_template_version"], "baseline_feedback_json_v1")
        self.assertNotIn("Retrieved-context grounding requirements:", run["prompt_text"])

    def test_feedback_length_and_tone_are_configurable(self) -> None:
        with (
            patch("feedback_lens.feedback.pipeline.generate_text") as mock_generate_text,
            patch(
                "feedback_lens.feedback.pipeline.retrieve_relevant_chunks"
            ) as mock_retrieve,
        ):
            mock_generate_text.return_value = _feedback_response()
            mock_retrieve.return_value = _retrieval_result("Task\nreflection report")

            with _connect_minimal_feedback_db() as conn:
                result = generate_feedback_for_submission(
                    conn,
                    submission_id=1,
                    provider="qwen",
                    model="test-model",
                    context_mode="retrieval",
                    retrieval_strategy="baseline",
                    feedback_length="concise",
                    feedback_tone="direct_no_fluff",
                )
                run = conn.execute(
                    "SELECT * FROM generation_runs WHERE generation_id = ?",
                    (result.generation_id,),
                ).fetchone()

        self.assertEqual(result.feedback_length, "concise")
        self.assertEqual(result.feedback_tone, "direct_no_fluff")
        self.assertEqual(result.feedback_modifier_mode, "custom")
        self.assertIn("- feedback_length: concise", run["prompt_text"])
        self.assertIn("Keep feedback short and low-density.", run["prompt_text"])
        self.assertIn("- feedback_tone: direct_no_fluff", run["prompt_text"])
        self.assertIn("Be direct and efficient.", run["prompt_text"])

    def test_regenerate_criterion_reuses_run_context_and_preserves_mark(self) -> None:
        with (
            patch("feedback_lens.feedback.pipeline.generate_text") as mock_generate_text,
            patch(
                "feedback_lens.feedback.pipeline.retrieve_relevant_chunks"
            ) as mock_retrieve,
        ):
            mock_generate_text.return_value = _feedback_response()
            mock_retrieve.return_value = _retrieval_result("Task\nreflection report")

            with _connect_minimal_feedback_db() as conn:
                result = generate_feedback_for_submission(
                    conn,
                    submission_id=1,
                    provider="qwen",
                    model="test-model",
                    context_mode="retrieval",
                    retrieval_strategy="baseline",
                )
                conn.execute(
                    """
                    UPDATE criterion_feedback
                    SET mark = 82
                    WHERE generation_id = ?
                      AND criterion_id = 1
                    """,
                    (result.generation_id,),
                )
                conn.commit()

                mock_generate_text.reset_mock()
                mock_generate_text.return_value = _criterion_response(
                    strengths="Updated criterion strength.",
                    areas_for_improvement="Updated criterion gap.",
                    improvement_suggestion="Updated criterion next step.",
                    suggested_level="D",
                    evidence_summary="Updated evidence from Week 2 Concepts.",
                )

                updated = regenerate_feedback_for_criterion(
                    conn,
                    generation_id=result.generation_id,
                    criterion_id=1,
                    feedback_length="concise",
                    feedback_tone="direct_no_fluff",
                )

        regen_prompt = mock_generate_text.call_args.args[0]
        self.assertEqual(updated["strengths"], "Updated criterion strength.")
        self.assertEqual(updated["areas_for_improvement"], "Updated criterion gap.")
        self.assertEqual(updated["improvement_suggestion"], "Updated criterion next step.")
        self.assertEqual(updated["suggested_level"], "D")
        self.assertEqual(updated["evidence_summary"], "Updated evidence from Week 2 Concepts.")
        self.assertEqual(updated["mark"], 82)
        self.assertIn("- feedback_length: concise", regen_prompt)
        self.assertIn("- feedback_tone: direct_no_fluff", regen_prompt)
        self.assertIn("Week 2 Concepts", regen_prompt)
        self.assertEqual(mock_generate_text.call_count, 1)

    def test_system_default_feedback_modifiers_keep_grounding_without_custom_rules(self) -> None:
        with (
            patch("feedback_lens.feedback.pipeline.generate_text") as mock_generate_text,
            patch(
                "feedback_lens.feedback.pipeline.retrieve_relevant_chunks"
            ) as mock_retrieve,
        ):
            mock_generate_text.return_value = _feedback_response()
            mock_retrieve.return_value = _retrieval_result("Task\nreflection report")

            with _connect_minimal_feedback_db() as conn:
                result = generate_feedback_for_submission(
                    conn,
                    submission_id=1,
                    provider="qwen",
                    model="test-model",
                    context_mode="retrieval",
                    retrieval_strategy="baseline",
                    feedback_modifier_mode="system_default",
                    feedback_length="concise",
                    feedback_tone="direct_no_fluff",
                )
                run = conn.execute(
                    "SELECT * FROM generation_runs WHERE generation_id = ?",
                    (result.generation_id,),
                ).fetchone()

        self.assertEqual(result.feedback_modifier_mode, "system_default")
        self.assertIsNone(result.feedback_length)
        self.assertIsNone(result.feedback_tone)
        self.assertNotIn("Feedback customisation requirements:", run["prompt_text"])
        self.assertNotIn("Keep feedback short and low-density.", run["prompt_text"])
        self.assertNotIn("Be direct and efficient.", run["prompt_text"])
        self.assertIn("Retrieved-context grounding requirements:", run["prompt_text"])

    def test_invalid_feedback_customisation_is_rejected_before_generation_run(self) -> None:
        with _connect_minimal_feedback_db() as conn:
            with self.assertRaisesRegex(ValueError, "feedback_length must be one of"):
                generate_feedback_for_submission(
                    conn,
                    submission_id=1,
                    feedback_length="rambling",
                )
            run_count = conn.execute(
                "SELECT COUNT(*) AS count FROM generation_runs"
            ).fetchone()["count"]

        self.assertEqual(run_count, 0)

    def test_invalid_feedback_modifier_mode_is_rejected_before_generation_run(self) -> None:
        with _connect_minimal_feedback_db() as conn:
            with self.assertRaisesRegex(ValueError, "feedback_modifier_mode must be one of"):
                generate_feedback_for_submission(
                    conn,
                    submission_id=1,
                    feedback_modifier_mode="chaos",
                )
            run_count = conn.execute(
                "SELECT COUNT(*) AS count FROM generation_runs"
            ).fetchone()["count"]

        self.assertEqual(run_count, 0)

    def test_planned_retrieval_generates_and_records_planner_cues(self) -> None:
        planner_response = json.dumps(
            {
                "retrieval_cues": [
                    {
                        "order": 1,
                        "label": "Reflective analysis concepts",
                        "text": "course concepts for reflective analysis in computing practice",
                        "rubric_criterion_ids": [1],
                        "rationale": "Needed to judge the analysis criterion fairly.",
                    }
                ]
            }
        )

        with (
            patch("feedback_lens.feedback.pipeline.generate_text") as mock_generate_text,
            patch(
                "feedback_lens.feedback.pipeline.retrieve_relevant_chunks"
            ) as mock_retrieve,
        ):
            mock_generate_text.side_effect = [
                planner_response,
                _feedback_response("Sound planned feedback."),
            ]
            mock_retrieve.return_value = _retrieval_result(
                "Reflective analysis concepts\n"
                "course concepts for reflective analysis in computing practice"
            )

            with _connect_minimal_feedback_db() as conn:
                result = generate_feedback_for_submission(
                    conn,
                    submission_id=1,
                    provider="qwen",
                    model="test-model",
                    context_mode="retrieval",
                    retrieval_strategy="planned",
                )
                run = conn.execute(
                    "SELECT * FROM generation_runs WHERE generation_id = ?",
                    (result.generation_id,),
                ).fetchone()
                planning = conn.execute(
                    """
                    SELECT *
                    FROM retrieval_planning_records
                    WHERE generation_id = ?
                    """,
                    (result.generation_id,),
                ).fetchone()

        planner_prompt = mock_generate_text.call_args_list[0].args[0]
        retrieval_cues = mock_retrieve.call_args.args[2]
        planned_cues = json.loads(planning["planned_cues_json"])

        self.assertEqual(result.retrieval_strategy, "llm_planned_cue_v1")
        self.assertEqual(result.pipeline_version, "planned_retrieval_v1")
        self.assertEqual(result.retrieval_cue_count, 1)
        self.assertEqual(run["retrieval_strategy"], "llm_planned_cue_v1")
        self.assertEqual(planning["status"], "completed")
        self.assertEqual(planning["strategy"], "llm_planned_cue_v1")
        self.assertEqual(planning["prompt_template_version"], "retrieval_planner_json_v1")
        self.assertIn("Rubric text:", planner_prompt)
        self.assertIn("Student submission text:", planner_prompt)
        self.assertEqual(retrieval_cues[0]["label"], "Reflective analysis concepts")
        self.assertEqual(retrieval_cues[0]["rubric_criterion_ids"], [1])
        self.assertEqual(planned_cues[0]["rationale"], "Needed to judge the analysis criterion fairly.")
        self.assertEqual(mock_generate_text.call_count, 2)

    def test_malformed_planner_response_marks_generation_failed(self) -> None:
        with (
            patch("feedback_lens.feedback.pipeline.generate_text") as mock_generate_text,
            patch(
                "feedback_lens.feedback.pipeline.retrieve_relevant_chunks"
            ) as mock_retrieve,
        ):
            mock_generate_text.return_value = "not json"

            with _connect_minimal_feedback_db() as conn:
                with self.assertRaisesRegex(
                    ValueError,
                    "Retrieval planner response did not contain a JSON object",
                ):
                    generate_feedback_for_submission(
                        conn,
                        submission_id=1,
                        provider="qwen",
                        model="test-model",
                        context_mode="retrieval",
                        retrieval_strategy="planned",
                    )

                run = conn.execute("SELECT * FROM generation_runs").fetchone()
                planning = conn.execute(
                    "SELECT * FROM retrieval_planning_records"
                ).fetchone()

        self.assertEqual(run["status"], "failed")
        self.assertEqual(planning["status"], "failed")
        self.assertEqual(planning["raw_response_text"], "not json")
        self.assertIn(
            "Retrieval planner response did not contain a JSON object",
            planning["error_message"],
        )
        mock_retrieve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
