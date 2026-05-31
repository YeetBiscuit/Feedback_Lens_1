import json
import sqlite3
import unittest

from feedback_lens.db.connection import ensure_schema_updates
from feedback_lens.feedback.review import (
    DETAIL_FULL,
    DETAIL_RESULT_ONLY,
    fetch_generation_review,
    format_generation_review_markdown,
    format_generation_reviews_html,
    generation_review_to_export_dict,
    list_generation_run_ids,
)
from feedback_lens.paths import SCHEMA_PATH


def _connect_sample_db() -> sqlite3.Connection:
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
        INSERT INTO assignments (assignment_id, unit_id, assignment_name, assignment_type)
        VALUES (1, 1, 'Reflection Report', 'report')
        """
    )
    conn.execute(
        """
        INSERT INTO assignment_specs (spec_id, assignment_id, version, cleaned_text)
        VALUES (1, 1, 1, 'Write a reflective computing report.')
        """
    )
    conn.execute(
        """
        INSERT INTO rubrics (rubric_id, assignment_id, version, cleaned_text)
        VALUES (1, 1, 1, 'Criterion | HD | D | C | P | N')
        """
    )
    conn.execute(
        """
        INSERT INTO rubric_criteria
            (criterion_id, rubric_id, criterion_name, criterion_description, criterion_order)
        VALUES (1, 1, 'Analysis', 'Quality of analysis', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO student_submissions
            (submission_id, assignment_id, student_identifier, original_file_path, cleaned_text)
        VALUES (1, 1, 'student_001', 'documents/submissions/student_001.txt', 'Submission text')
        """
    )
    conn.execute(
        """
        INSERT INTO generation_runs
            (generation_id, submission_id, assignment_id, rubric_id, pipeline_version,
             llm_provider, llm_model, prompt_template_version, retrieval_strategy,
             temperature, top_k, status, completed_at, prompt_text, raw_response_text)
        VALUES
            (1, 1, 1, 1, 'baseline_direct_v1', 'qwen', 'qwen3.5-plus',
             'baseline_feedback_json_v1', 'assignment_spec_multi_cue_v1',
             0.2, 5, 'completed', '2026-04-20 10:00:00', 'Prompt text', 'Raw response')
        """
    )
    planned_cues = [
        {
            "order": 1,
            "label": "Reflective analysis concepts",
            "text": "course standards for reflective computing analysis",
            "rubric_criterion_ids": [1],
            "rationale": "Needed to judge the analysis criterion fairly.",
        }
    ]
    conn.execute(
        """
        INSERT INTO retrieval_planning_records
            (planning_record_id, generation_id, strategy, provider, model,
             prompt_template_version, prompt_text, raw_response_text,
             planned_cues_json, status, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            1,
            "llm_planned_cue_v1",
            "qwen",
            "qwen3.5-plus",
            "retrieval_planner_json_v1",
            "Planner prompt text",
            json.dumps({"retrieval_cues": planned_cues}),
            json.dumps(planned_cues),
            "completed",
            "2026-04-20 09:59:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO overall_feedback
            (generation_id, overall_comment, key_strengths, priority_improvements,
             overall_grade_band)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            1,
            "Solid work overall.",
            json.dumps(["Clear structure", "Relevant examples"]),
            json.dumps(["Deepen evidence"]),
            "D",
        ),
    )
    conn.execute(
        """
        INSERT INTO criterion_feedback
            (generation_id, criterion_id, strengths, areas_for_improvement,
             improvement_suggestion, suggested_level, evidence_summary)
        VALUES
            (1, 1, 'Strong analysis', 'Needs more citations',
             'Add two specific course references.', 'D', 'Uses one lecture idea.')
        """
    )
    conn.execute(
        """
        INSERT INTO unit_materials
            (material_id, unit_id, material_type, title, week_number, cleaned_text)
        VALUES
            (1, 1, 'lecture_transcript', 'Week 1 Transcript', 1, 'Lecture text')
        """
    )
    conn.execute(
        """
        INSERT INTO material_chunks
            (chunk_id, material_id, chunk_index, chunk_text, page_number_start,
             page_number_end)
        VALUES
            (1, 1, 1, 'Chunk text with   extra whitespace for preview testing.', 2, 3)
        """
    )
    conn.execute(
        """
        INSERT INTO retrieval_records
            (generation_id, query_text, chunk_id, rank_position, similarity_score,
             used_in_prompt)
        VALUES
            (1, 'analysis evidence query', 1, 1, 0.95, 1)
        """
    )
    conn.commit()
    return conn


class FeedbackReviewExportTests(unittest.TestCase):
    def test_export_dict_defaults_to_result_only(self) -> None:
        with _connect_sample_db() as conn:
            self.assertEqual(list_generation_run_ids(conn), [1])
            review = fetch_generation_review(conn, 1)
            payload = generation_review_to_export_dict(review)

        run = payload["generation_run"]
        self.assertEqual(payload["export_version"], 2)
        self.assertEqual(payload["export_mode"], DETAIL_RESULT_ONLY)
        self.assertEqual(len(review["retrieval_planning_records"]), 1)
        self.assertNotIn("prompt_text", run)
        self.assertNotIn("raw_response_text", run)
        self.assertEqual(payload["overall_feedback"]["key_strengths"], ["Clear structure", "Relevant examples"])
        self.assertEqual(payload["overall_feedback"]["priority_improvements"], ["Deepen evidence"])
        self.assertNotIn("retrieval_records", payload)
        self.assertNotIn("retrieval_planning_records", payload)

    def test_export_dict_full_includes_planner_and_raw_generation_details(self) -> None:
        with _connect_sample_db() as conn:
            review = fetch_generation_review(conn, 1)
            payload = generation_review_to_export_dict(
                review,
                detail_mode=DETAIL_FULL,
            )

        run = payload["generation_run"]
        planner = payload["retrieval_planning_records"][0]
        self.assertEqual(run["prompt_text"], "Prompt text")
        self.assertEqual(run["raw_response_text"], "Raw response")
        self.assertEqual(
            payload["retrieval_records"][0]["chunk_text"],
            "Chunk text with   extra whitespace for preview testing.",
        )
        self.assertEqual(planner["prompt_text"], "Planner prompt text")
        self.assertIn("retrieval_cues", planner["raw_response_text"])
        self.assertEqual(planner["planned_cues"][0]["label"], "Reflective analysis concepts")

    def test_export_dict_rejects_unknown_detail_mode(self) -> None:
        with _connect_sample_db() as conn:
            review = fetch_generation_review(conn, 1)

        with self.assertRaises(ValueError):
            generation_review_to_export_dict(review, detail_mode="everything")

    def test_markdown_export_contains_feedback_sections(self) -> None:
        with _connect_sample_db() as conn:
            review = fetch_generation_review(conn, 1)
            payload = generation_review_to_export_dict(review)

        markdown = format_generation_review_markdown(payload)
        self.assertIn("# Feedback Generation Run 1", markdown)
        self.assertIn("## Overall Feedback", markdown)
        self.assertIn("- Clear structure", markdown)
        self.assertIn("### 1. Analysis", markdown)
        self.assertNotIn("Feedback Generation Prompt", markdown)
        self.assertNotIn("Retrieval Planner", markdown)

    def test_markdown_export_full_contains_planner_and_prompt_sections(self) -> None:
        with _connect_sample_db() as conn:
            review = fetch_generation_review(conn, 1)
            payload = generation_review_to_export_dict(
                review,
                detail_mode=DETAIL_FULL,
            )

        markdown = format_generation_review_markdown(payload)
        self.assertIn("## Retrieval Planner", markdown)
        self.assertIn("Retrieval planner prompt:", markdown)
        self.assertIn("Planner prompt text", markdown)
        self.assertIn("## Retrieved Chunks", markdown)
        self.assertIn("Chunk text with   extra whitespace", markdown)
        self.assertIn("## Feedback Generation Prompt", markdown)

    def test_html_export_contains_theme_controls_and_feedback_sections(self) -> None:
        with _connect_sample_db() as conn:
            review = fetch_generation_review(conn, 1)
            payload = generation_review_to_export_dict(review)

        html = format_generation_reviews_html([payload])

        self.assertIn("<!doctype html>", html)
        self.assertIn('data-theme-choice="system"', html)
        self.assertIn('data-theme-choice="light"', html)
        self.assertIn('data-theme-choice="dark"', html)
        self.assertIn("Feedback Generation Run 1", html)
        self.assertIn("Overall Feedback", html)
        self.assertIn("Criterion Feedback", html)
        self.assertIn("Clear structure", html)
        self.assertNotIn("Retrieved Chunks", html)
        self.assertNotIn("Feedback Generation Prompt", html)

    def test_html_export_full_contains_planner_and_raw_sections(self) -> None:
        with _connect_sample_db() as conn:
            review = fetch_generation_review(conn, 1)
            payload = generation_review_to_export_dict(
                review,
                detail_mode=DETAIL_FULL,
            )

        html = format_generation_reviews_html([payload])

        self.assertIn("Retrieval Planner", html)
        self.assertIn("<summary>Retrieval Planner</summary>", html)
        self.assertIn("Retrieval Planner Prompt", html)
        self.assertIn("Retrieval Planner Raw Response", html)
        self.assertIn("Reflective analysis concepts", html)
        self.assertIn("Retrieved Chunks", html)
        self.assertIn("<summary>Retrieved Chunks</summary>", html)
        self.assertIn("<summary>Raw LLM Details</summary>", html)
        self.assertIn("Feedback Generation Prompt", html)
        self.assertIn("Feedback Generation Raw Response", html)
        self.assertIn("Chunk text with   extra whitespace", html)

    def test_html_export_escapes_dynamic_content(self) -> None:
        with _connect_sample_db() as conn:
            conn.execute(
                """
                UPDATE overall_feedback
                SET overall_comment = '<script>alert("x")</script>'
                WHERE generation_id = 1
                """
            )
            review = fetch_generation_review(conn, 1)
            payload = generation_review_to_export_dict(review)

        html = format_generation_reviews_html([payload])

        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", html)
        self.assertNotIn('<script>alert("x")</script>', html)


if __name__ == "__main__":
    unittest.main()
