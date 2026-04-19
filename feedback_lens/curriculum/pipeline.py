import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from feedback_lens.curriculum import prompts
from feedback_lens.curriculum.paths import (
    assignment_slug,
    collision_safe_path,
    ensure_unit_layout,
    topic_slug,
    unit_root,
)
from feedback_lens.curriculum.schema import extract_json_object, validate_course_schema


GRADE_BANDS = ("HD", "D", "C", "P")
ProgressCallback = Callable[[str], None]


def hash_file(path: str | Path) -> str:
    from feedback_lens.file_management.document_io import hash_file as _hash_file

    return _hash_file(path)


def normalise_source_path(path: str | Path) -> str:
    from feedback_lens.file_management.document_io import (
        normalise_source_path as _normalise_source_path,
    )

    return _normalise_source_path(path)


@dataclass(slots=True)
class CurriculumGenerationResult:
    curriculum_run_id: int
    course_code: str
    output_root: Path
    provider: str
    model: str


def _emit(progress_callback: ProgressCallback | None, message: str) -> None:
    if progress_callback is not None:
        progress_callback(message)


def _insert_run(
    conn: sqlite3.Connection,
    description: str,
    provider: str,
    model: str,
    temperature: float,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO curriculum_generation_runs
            (source_description, provider, model, temperature, status)
        VALUES (?, ?, ?, ?, 'running')
        """,
        (description, provider, model, temperature),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_step(
    conn: sqlite3.Connection,
    run_id: int,
    stage_key: str,
    messages: list[dict[str, str]],
    assignment_code: str | None = None,
    week_number: int | None = None,
    grade_band: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO curriculum_generation_steps
            (curriculum_run_id, stage_key, assignment_code, week_number,
             grade_band, prompt_messages_json, status)
        VALUES (?, ?, ?, ?, ?, ?, 'running')
        """,
        (
            run_id,
            stage_key,
            assignment_code,
            week_number,
            grade_band,
            json.dumps(messages, ensure_ascii=False),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _complete_step(
    conn: sqlite3.Connection,
    step_id: int,
    raw_response: str,
    parsed_output: object | None = None,
) -> None:
    conn.execute(
        """
        UPDATE curriculum_generation_steps
        SET raw_response = ?,
            parsed_output_json = ?,
            status = 'completed',
            locked_at = CURRENT_TIMESTAMP,
            completed_at = CURRENT_TIMESTAMP
        WHERE curriculum_step_id = ?
        """,
        (
            raw_response,
            json.dumps(parsed_output, ensure_ascii=False) if parsed_output is not None else None,
            step_id,
        ),
    )
    conn.commit()


def _fail_step(conn: sqlite3.Connection, step_id: int, error: Exception) -> None:
    conn.execute(
        """
        UPDATE curriculum_generation_steps
        SET status = 'failed',
            error_message = ?,
            completed_at = CURRENT_TIMESTAMP
        WHERE curriculum_step_id = ?
        """,
        (str(error), step_id),
    )
    conn.commit()


def _run_step(
    conn: sqlite3.Connection,
    run_id: int,
    stage_key: str,
    messages: list[dict[str, str]],
    provider: str,
    model: str,
    temperature: float,
    assignment_code: str | None = None,
    week_number: int | None = None,
    grade_band: str | None = None,
    parse_json: bool = False,
    progress_callback: ProgressCallback | None = None,
    progress_label: str | None = None,
) -> tuple[int, str, object | None]:
    from feedback_lens.feedback.llm.providers import generate_chat

    step_id = _insert_step(
        conn,
        run_id,
        stage_key,
        messages,
        assignment_code=assignment_code,
        week_number=week_number,
        grade_band=grade_band,
    )
    label = progress_label or stage_key
    _emit(progress_callback, f"Starting {label} (step_id={step_id}).")
    try:
        response = generate_chat(
            messages,
            provider=provider,
            model=model,
            temperature=temperature,
        )
        parsed = extract_json_object(response) if parse_json else None
        _complete_step(conn, step_id, response, parsed)
        _emit(progress_callback, f"Completed {label} (step_id={step_id}).")
        return step_id, response, parsed
    except Exception as err:
        _fail_step(conn, step_id, err)
        _emit(progress_callback, f"Failed {label} (step_id={step_id}): {err}")
        raise


def _write_text(path: Path, text: str) -> Path:
    target = collision_safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _write_pdf(path: Path, text: str) -> Path:
    from feedback_lens.curriculum.pdf import write_plain_pdf

    return write_plain_pdf(text, collision_safe_path(path))


def _write_rubric_pdf(path: Path, text: str) -> Path:
    from feedback_lens.curriculum.pdf import write_rubric_table_pdf

    return write_rubric_table_pdf(text, collision_safe_path(path))


def _insert_artifact(
    conn: sqlite3.Connection,
    run_id: int,
    step_id: int | None,
    artifact_type: str,
    title: str,
    path: Path,
    text_content: str,
) -> None:
    conn.execute(
        """
        INSERT INTO curriculum_artifacts
            (curriculum_run_id, curriculum_step_id, artifact_type, title,
             file_path, content_hash, text_content)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            step_id,
            artifact_type,
            title,
            normalise_source_path(path),
            hash_file(path),
            text_content,
        ),
    )
    conn.commit()


def _assignment_by_id(schema: dict) -> dict[str, dict]:
    return {
        str(assignment["id"]): assignment
        for assignment in schema.get("assignments", [])
    }


def _topic_by_week(schema: dict) -> dict[int, dict]:
    return {
        int(topic["week"]): topic
        for topic in schema.get("topics", [])
    }


def _transcripts_for_assignment(
    assignment: dict,
    lecture_texts: dict[int, str],
) -> str:
    parts = []
    for week in assignment.get("linked_topics") or []:
        week_int = int(week)
        if week_int in lecture_texts:
            parts.append(f"WEEK {week_int}\n{lecture_texts[week_int]}")
    return "\n\n".join(parts)


def _write_unit_manifest(root: Path, schema: dict, year: int | None, semester: str | None) -> Path:
    manifest = {
        "unit": {
            "course_code": schema["course_code"],
            "course_title": schema["course_title"],
            "year": year,
            "semester": semester,
        },
        "assignments": {},
        "materials": {},
    }
    for assignment in schema.get("assignments", []):
        manifest["assignments"][assignment_slug(assignment)] = {
            "assignment_code": assignment.get("id"),
            "title": assignment.get("title"),
            "type": assignment.get("type"),
            "weight": assignment.get("weight"),
            "due_week": assignment.get("due_week"),
        }
    path = root / "unit_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def generate_unit(
    conn: sqlite3.Connection,
    description: str,
    year: int | None = None,
    semester: str | None = None,
    provider: str = "qwen",
    model: str | None = None,
    temperature: float = 0.2,
    progress_callback: ProgressCallback | None = None,
) -> CurriculumGenerationResult:
    from feedback_lens.feedback.llm.providers import resolve_model_name

    resolved_model = resolve_model_name(provider, model)
    run_id = _insert_run(conn, description, provider, resolved_model, temperature)
    _emit(
        progress_callback,
        f"Created curriculum generation run {run_id} using {provider}:{resolved_model}.",
    )

    def record_artifact(
        step_id: int | None,
        artifact_type: str,
        title: str,
        path: Path,
        text_content: str,
    ) -> None:
        _insert_artifact(
            conn,
            run_id,
            step_id,
            artifact_type,
            title,
            path,
            text_content,
        )
        _emit(progress_callback, f"Wrote {artifact_type}: {path}")

    try:
        schema_step, _schema_response, parsed_schema = _run_step(
            conn,
            run_id,
            "stage_1_schema",
            prompts.schema_messages(description),
            provider,
            resolved_model,
            temperature,
            parse_json=True,
            progress_callback=progress_callback,
            progress_label="course schema generation",
        )
        schema = parsed_schema
        if not isinstance(schema, dict):
            raise ValueError("Stage 1 did not produce a JSON object.")
        validate_course_schema(schema)

        course_code = str(schema["course_code"]).upper()
        schema["course_code"] = course_code
        root = unit_root(course_code)
        _emit(
            progress_callback,
            (
                f"Schema resolved for {course_code}: "
                f"{len(schema.get('assignments', []))} assignment(s), "
                f"{len(schema.get('topics', []))} topic week(s)."
            ),
        )
        _emit(progress_callback, f"Preparing unit folder: {root}")
        ensure_unit_layout(root)
        schema_path = _write_text(
            root / "schema.json",
            json.dumps(schema, ensure_ascii=False, indent=2),
        )
        record_artifact(
            schema_step,
            "schema",
            "Course schema",
            schema_path,
            json.dumps(schema, ensure_ascii=False, indent=2),
        )
        manifest_path = _write_unit_manifest(root, schema, year, semester)
        record_artifact(
            None,
            "manifest",
            "Unit manifest",
            manifest_path,
            manifest_path.read_text(encoding="utf-8"),
        )
        conn.execute(
            """
            UPDATE curriculum_generation_runs
            SET course_code = ?,
                output_root = ?,
                schema_json = ?
            WHERE curriculum_run_id = ?
            """,
            (
                course_code,
                normalise_source_path(root),
                json.dumps(schema, ensure_ascii=False),
                run_id,
            ),
        )
        conn.commit()

        specs: dict[str, str] = {}
        rubrics: dict[str, str] = {}
        worksheets: dict[str, str] = {}
        samples: dict[str, str] = {}
        submissions: dict[str, dict[str, str]] = {}
        assignment_payloads: list[dict] = []
        lecture_texts: dict[int, str] = {}

        for assignment in schema.get("assignments", []):
            assignment_id = str(assignment["id"])
            slug = assignment_slug(assignment)
            assignment_dir = root / "assignments" / slug
            assignment_dir.mkdir(parents=True, exist_ok=True)
            assignment_title = str(assignment.get("title") or slug)
            _emit(
                progress_callback,
                f"Generating assignment package for {assignment_id}: {assignment_title}",
            )

            spec_step, spec_text, _ = _run_step(
                conn,
                run_id,
                "stage_2a_assignment_spec",
                prompts.assignment_spec_messages(schema, assignment_id),
                provider,
                resolved_model,
                temperature,
                assignment_code=assignment_id,
                progress_callback=progress_callback,
                progress_label=f"{assignment_id} assignment specification",
            )
            spec_path = _write_pdf(assignment_dir / "spec.pdf", spec_text)
            specs[assignment_id] = spec_text
            record_artifact(
                spec_step,
                "assignment_spec",
                f"{assignment_id} specification",
                spec_path,
                spec_text,
            )

            rubric_step, rubric_text, _ = _run_step(
                conn,
                run_id,
                "stage_2b_rubric",
                prompts.rubric_messages(spec_text),
                provider,
                resolved_model,
                temperature,
                assignment_code=assignment_id,
                progress_callback=progress_callback,
                progress_label=f"{assignment_id} rubric",
            )
            rubric_path = _write_rubric_pdf(assignment_dir / "rubric.pdf", rubric_text)
            rubrics[assignment_id] = rubric_text
            record_artifact(
                rubric_step,
                "rubric",
                f"{assignment_id} rubric",
                rubric_path,
                rubric_text,
            )

        for topic in schema.get("topics", []):
            week = int(topic["week"])
            topic_title = str(topic.get("title") or f"Week {week}")
            lecture_step, lecture_text, _ = _run_step(
                conn,
                run_id,
                "stage_3a_lecture_transcript",
                prompts.lecture_messages(schema, topic),
                provider,
                resolved_model,
                temperature,
                week_number=week,
                progress_callback=progress_callback,
                progress_label=f"week {week} lecture transcript ({topic_title})",
            )
            lecture_path = _write_text(
                root / "lectures" / f"week_{week:02d}_{topic_slug(topic)}.txt",
                lecture_text,
            )
            lecture_texts[week] = lecture_text
            record_artifact(
                lecture_step,
                "lecture_transcript",
                f"Week {week} lecture",
                lecture_path,
                lecture_text,
            )

        for assignment in schema.get("assignments", []):
            assignment_id = str(assignment["id"])
            slug = assignment_slug(assignment)
            transcripts = _transcripts_for_assignment(assignment, lecture_texts)
            _emit(
                progress_callback,
                f"Generating tutorial and submissions for {assignment_id}.",
            )

            worksheet_step, worksheet_text, _ = _run_step(
                conn,
                run_id,
                "stage_3b_tutorial_worksheet",
                prompts.worksheet_messages(specs[assignment_id], transcripts),
                provider,
                resolved_model,
                temperature,
                assignment_code=assignment_id,
                progress_callback=progress_callback,
                progress_label=f"{assignment_id} tutorial worksheet",
            )
            worksheet_path = _write_pdf(
                root / "tutorials" / f"{slug}_worksheet.pdf",
                worksheet_text,
            )
            worksheets[assignment_id] = worksheet_text
            record_artifact(
                worksheet_step,
                "tutorial_sheet",
                f"{assignment_id} worksheet",
                worksheet_path,
                worksheet_text,
            )

            sample_step, sample_text, _ = _run_step(
                conn,
                run_id,
                "stage_3c_sample_answers",
                prompts.sample_answer_messages(worksheet_text, transcripts),
                provider,
                resolved_model,
                temperature,
                assignment_code=assignment_id,
                progress_callback=progress_callback,
                progress_label=f"{assignment_id} sample answers",
            )
            sample_path = _write_pdf(
                root / "tutorials" / f"{slug}_sample_answers.pdf",
                sample_text,
            )
            samples[assignment_id] = sample_text
            record_artifact(
                sample_step,
                "sample_solution",
                f"{assignment_id} sample answers",
                sample_path,
                sample_text,
            )

            submission_dir = root / "assignments" / slug / "submissions"
            submission_dir.mkdir(parents=True, exist_ok=True)
            submissions[assignment_id] = {}
            for grade_band in GRADE_BANDS:
                submission_step, submission_text, _ = _run_step(
                    conn,
                    run_id,
                    "stage_4_student_submission",
                    prompts.submission_messages(
                        specs[assignment_id],
                        rubrics[assignment_id],
                        transcripts,
                        worksheet_text,
                        sample_text,
                        grade_band,
                    ),
                    provider,
                    resolved_model,
                    temperature,
                    assignment_code=assignment_id,
                    grade_band=grade_band,
                    progress_callback=progress_callback,
                    progress_label=f"{assignment_id} synthetic {grade_band} submission",
                )
                submission_path = _write_pdf(
                    submission_dir / f"submission_{grade_band}.pdf",
                    submission_text,
                )
                submissions[assignment_id][grade_band] = submission_text
                record_artifact(
                    submission_step,
                    "student_submission",
                    f"{assignment_id} {grade_band} submission",
                    submission_path,
                    submission_text,
                )

            assignment_payloads.append(
                {
                    "assignment_id": assignment_id,
                    "spec": specs[assignment_id],
                    "rubric": rubrics[assignment_id],
                    "submissions": submissions[assignment_id],
                }
            )

        audit_step, audit_text, _ = _run_step(
            conn,
            run_id,
            "stage_6_consistency_audit",
            prompts.audit_messages(schema, assignment_payloads),
            provider,
            resolved_model,
            temperature,
            progress_callback=progress_callback,
            progress_label="consistency audit",
        )
        audit_path = _write_text(root / "consistency_audit.txt", audit_text)
        record_artifact(
            audit_step,
            "consistency_audit",
            "Consistency audit",
            audit_path,
            audit_text,
        )

        conn.execute(
            """
            UPDATE curriculum_generation_runs
            SET status = 'completed',
                completed_at = CURRENT_TIMESTAMP
            WHERE curriculum_run_id = ?
            """,
            (run_id,),
        )
        conn.commit()
        _emit(progress_callback, f"Completed curriculum generation run {run_id}.")

        return CurriculumGenerationResult(
            curriculum_run_id=run_id,
            course_code=course_code,
            output_root=root,
            provider=provider,
            model=resolved_model,
        )
    except Exception as err:
        conn.execute(
            """
            UPDATE curriculum_generation_runs
            SET status = 'failed',
                error_message = ?,
                completed_at = CURRENT_TIMESTAMP
            WHERE curriculum_run_id = ?
            """,
            (str(err), run_id),
        )
        conn.commit()
        raise
