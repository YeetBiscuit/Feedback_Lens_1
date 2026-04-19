import json
import sqlite3
import shutil
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from feedback_lens.curriculum.paths import assignment_slug, collision_safe_path
from feedback_lens.curriculum.pipeline import generate_unit
from feedback_lens.file_management.parsers.rubric_parser import (
    extract_pipe_rubric_tables,
    extract_rubric_criteria,
)
from feedback_lens.file_management.unit_auto_ingestion import (
    ingest_unit_directory,
)
from feedback_lens.db.connection import ensure_schema_updates
from feedback_lens.paths import SCHEMA_PATH


WORKSPACE_TMP = Path.cwd() / "tmp_tests"


@contextmanager
def _temp_dir():
    WORKSPACE_TMP.mkdir(exist_ok=True)
    path = WORKSPACE_TMP / f"case_{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield str(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _connect_temp_db(_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    ensure_schema_updates(conn)
    conn.commit()
    return conn


def _write_minimal_unit(root: Path) -> None:
    root.mkdir(parents=True)
    schema = {
        "course_code": "COMP3001",
        "course_title": "Advanced Computing",
        "level": "undergraduate_year_3",
        "discipline": "Computer Science",
        "credit_points": 6,
        "weeks": 1,
        "learning_outcomes": ["LO1"],
        "topics": [
            {"week": 1, "title": "Foundations", "summary": "Core ideas."}
        ],
        "assignments": [
            {
                "id": "A1",
                "title": "Literature Review",
                "type": "essay",
                "weight": 40,
                "due_week": 1,
                "word_count_or_equivalent": "1000 words",
                "linked_topics": [1],
                "learning_outcomes_assessed": ["LO1"],
            }
        ],
    }
    (root / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    assignment_dir = root / "assignments" / "a1-literature-review"
    submissions_dir = assignment_dir / "submissions"
    submissions_dir.mkdir(parents=True)
    (assignment_dir / "spec.txt").write_text(
        "Assignment specification about literature review and evidence.",
        encoding="utf-8",
    )
    (submissions_dir / "student_001.txt").write_text(
        "Student submission text.",
        encoding="utf-8",
    )


class UnitGenerationIngestionTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        if WORKSPACE_TMP.exists():
            shutil.rmtree(WORKSPACE_TMP, ignore_errors=True)

    def test_schema_contains_new_metadata_tables_and_columns(self) -> None:
        with _temp_dir() as tmp:
            db_path = Path(tmp) / "feedback.db"
            with _connect_temp_db(db_path) as conn:
                unit_cols = {
                    row["name"] for row in conn.execute("PRAGMA table_info(units)")
                }
                self.assertIn("learning_outcomes_json", unit_cols)
                spec_cols = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(assignment_specs)")
                }
                self.assertIn("source_content_hash", spec_cols)
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("curriculum_generation_runs", tables)
                self.assertIn("unit_ingestion_items", tables)

    def test_unit_level_path_helpers(self) -> None:
        assignment = {"id": "A1", "title": "Literature Review"}
        self.assertEqual(assignment_slug(assignment), "a1-literature-review")
        with _temp_dir() as tmp:
            target = Path(tmp) / "spec.pdf"
            target.write_text("first", encoding="utf-8")
            self.assertEqual(collision_safe_path(target).name, "spec_v2.pdf")

    def test_pipe_rubric_parser_fallback(self) -> None:
        text = """
| CRITERION | WEIGHT | HD | D | C | P | FAIL |
| Analysis | 50% | Excellent | Strong | Sound | Basic | Missing |
| Evidence | 50% | Rich | Relevant | Adequate | Limited | Absent |
"""
        tables = extract_pipe_rubric_tables(text)
        criteria = extract_rubric_criteria(tables)
        self.assertEqual(len(criteria), 2)
        self.assertEqual(criteria[0]["criterion_name"], "Analysis")
        self.assertIn("HD", criteria[0]["performance_levels"])

    def test_auto_ingest_dry_run_classifies_without_db_writes(self) -> None:
        with _temp_dir() as tmp:
            db_path = Path(tmp) / "feedback.db"
            unit_dir = Path(tmp) / "documents" / "units" / "COMP3001"
            _write_minimal_unit(unit_dir)
            lecture_dir = unit_dir / "lectures"
            lecture_dir.mkdir()
            (lecture_dir / "week_01_foundations.txt").write_text(
                "Lecture transcript.", encoding="utf-8"
            )
            rubric_path = unit_dir / "assignments" / "a1-literature-review" / "rubric.pdf"
            rubric_path.write_text("not a real pdf, dry run only", encoding="utf-8")

            with _connect_temp_db(db_path) as conn:
                result = ingest_unit_directory(conn, unit_dir, dry_run=True)
                item_types = {item.item_type for item in result.items}
                self.assertIn("assignment_spec", item_types)
                self.assertIn("student_submission", item_types)
                self.assertIn("unit_material", item_types)
                self.assertIn("rubric", item_types)
                unit_count = conn.execute("SELECT COUNT(*) AS count FROM units").fetchone()
                run_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM unit_ingestion_runs"
                ).fetchone()
                self.assertEqual(unit_count["count"], 0)
                self.assertEqual(run_count["count"], 0)

    def test_auto_ingest_skips_unchanged_specs_and_submissions(self) -> None:
        with _temp_dir() as tmp:
            db_path = Path(tmp) / "feedback.db"
            unit_dir = Path(tmp) / "documents" / "units" / "COMP3001"
            _write_minimal_unit(unit_dir)

            with _connect_temp_db(db_path) as conn:
                first = ingest_unit_directory(conn, unit_dir)
                second = ingest_unit_directory(conn, unit_dir)

                self.assertEqual(first.imported_count, 2)
                self.assertEqual(second.skipped_count, 2)
                spec_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM assignment_specs"
                ).fetchone()
                submission_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM student_submissions"
                ).fetchone()
                self.assertEqual(spec_count["count"], 1)
                self.assertEqual(submission_count["count"], 1)

    def test_generated_unit_pipeline_writes_files_without_auto_ingesting(self) -> None:
        responses = []
        schema_response = {
            "course_code": "TEST101",
            "course_title": "Testing Unit",
            "level": "undergraduate_year_1",
            "discipline": "Testing",
            "credit_points": 6,
            "weeks": 1,
            "learning_outcomes": ["LO1"],
            "topics": [
                {"week": 1, "title": "Testing Basics", "summary": "Basics."}
            ],
            "assignments": [
                {
                    "id": "A1",
                    "title": "Test Report",
                    "type": "report",
                    "weight": 100,
                    "due_week": 1,
                    "word_count_or_equivalent": "1000 words",
                    "linked_topics": [1],
                    "learning_outcomes_assessed": ["LO1"],
                }
            ],
        }
        responses.append(json.dumps(schema_response))
        responses.extend(
            [
                "SPEC TEXT",
                "| CRITERION | WEIGHT | HD | D | C | P | FAIL |\n| Quality | 100% | A | B | C | D | E |",
                "LECTURE TEXT",
                "WORKSHEET TEXT",
                "SAMPLE ANSWER TEXT",
                "HD SUBMISSION",
                "D SUBMISSION",
                "C SUBMISSION",
                "P SUBMISSION",
                "AUDIT TEXT",
            ]
        )

        def fake_generate_chat(*args, **kwargs):
            return responses.pop(0)

        with _temp_dir() as tmp:
            db_path = Path(tmp) / "feedback.db"
            unit_root = Path(tmp) / "documents" / "units" / "TEST101"

            def fake_unit_root(course_code: str) -> Path:
                return unit_root

            with _connect_temp_db(db_path) as conn:
                progress_events = []
                with patch(
                    "feedback_lens.feedback.llm.providers.generate_chat",
                    fake_generate_chat,
                ):
                    with patch("feedback_lens.curriculum.pipeline.unit_root", fake_unit_root):
                        result = generate_unit(
                            conn,
                            "A testing unit.",
                            provider="qwen",
                            model="fake-model",
                            progress_callback=progress_events.append,
                        )

                self.assertEqual(result.course_code, "TEST101")
                self.assertTrue(progress_events)
                self.assertIn("course schema generation", "\n".join(progress_events))
                self.assertIn("Completed curriculum generation run", progress_events[-1])
                self.assertTrue((unit_root / "schema.json").exists())
                self.assertTrue(
                    (
                        unit_root
                        / "assignments"
                        / "a1-test-report"
                        / "submissions"
                        / "submission_HD.pdf"
                    ).exists()
                )
                run = conn.execute(
                    "SELECT status FROM curriculum_generation_runs WHERE curriculum_run_id = ?",
                    (result.curriculum_run_id,),
                ).fetchone()
                self.assertEqual(run["status"], "completed")
                self.assertIsNone(getattr(result, "ingestion_result", None))
                unit_count = conn.execute("SELECT COUNT(*) AS count FROM units").fetchone()
                ingestion_run_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM unit_ingestion_runs"
                ).fetchone()
                self.assertEqual(unit_count["count"], 0)
                self.assertEqual(ingestion_run_count["count"], 0)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        if WORKSPACE_TMP.exists():
            shutil.rmtree(WORKSPACE_TMP, ignore_errors=True)
