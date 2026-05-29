import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

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


@dataclass(slots=True)
class SyntheticSubmissionGenerationResult:
    curriculum_run_id: int
    course_code: str
    output_root: Path
    provider: str
    model: str
    written_paths: list[Path]

    @property
    def generated_count(self) -> int:
        return len(self.written_paths)


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


def _load_json_file(path: Path, required: bool = True) -> dict:
    if not path.exists():
        if required:
            raise ValueError(f"Required JSON file does not exist: {path}")
        return {}
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _read_generated_document(path: Path) -> str:
    from feedback_lens.file_management.document_io import extract_document

    document = extract_document(path)
    text = document["cleaned_text"]
    if not text:
        raise ValueError(f"No readable text found in {path}")
    return text


def _find_generated_document(
    exact_paths: Sequence[Path],
    fallback_paths: Sequence[Path],
    label: str,
) -> Path:
    for path in exact_paths:
        if path.exists() and path.is_file() and path.suffix.lower() in {".pdf", ".txt"}:
            return path
    candidates = [
        path
        for path in fallback_paths
        if path.exists() and path.is_file() and path.suffix.lower() in {".pdf", ".txt"}
    ]
    if candidates:
        return sorted(candidates, key=lambda item: (item.stat().st_mtime, item.name))[-1]
    raise ValueError(f"Could not find {label}.")


def _lecture_week_from_path(path: Path) -> int | None:
    match = re.search(r"week[_ -]?0*(\d+)", path.stem, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _load_lecture_texts(root: Path) -> dict[int, str]:
    lecture_texts: dict[int, str] = {}
    lectures_dir = root / "lectures"
    if not lectures_dir.exists():
        return lecture_texts
    for path in sorted(lectures_dir.glob("*.txt")):
        week = _lecture_week_from_path(path)
        if week is not None:
            lecture_texts[week] = path.read_text(encoding="utf-8").strip()
    return lecture_texts


def _selected_assignments(schema: dict, assignment_codes: Sequence[str] | None) -> list[dict]:
    assignments = [item for item in schema.get("assignments", []) if isinstance(item, dict)]
    if not assignment_codes:
        return assignments

    wanted = {str(code).strip().upper() for code in assignment_codes if str(code).strip()}
    selected = [
        assignment
        for assignment in assignments
        if str(assignment.get("id") or assignment.get("assignment_code") or "").upper()
        in wanted
    ]
    found = {
        str(assignment.get("id") or assignment.get("assignment_code") or "").upper()
        for assignment in selected
    }
    missing = sorted(wanted - found)
    if missing:
        raise ValueError(f"Unknown assignment code(s): {', '.join(missing)}")
    return selected


def _normalise_grade_bands(grade_bands: Sequence[str] | None) -> list[str]:
    if not grade_bands:
        return list(GRADE_BANDS)
    normalised = [str(band).strip().upper() for band in grade_bands]
    invalid = sorted({band for band in normalised if band not in GRADE_BANDS})
    if invalid:
        raise ValueError(
            f"Unsupported grade band(s): {', '.join(invalid)}. "
            f"Use one of: {', '.join(GRADE_BANDS)}"
        )
    return normalised


def _manifest_assignment_entry(manifest: dict, assignment: dict) -> tuple[str, dict]:
    slug = assignment_slug(assignment)
    assignments = manifest.setdefault("assignments", {})
    if not isinstance(assignments, dict):
        assignments = {}
        manifest["assignments"] = assignments
    entry = assignments.setdefault(slug, {})
    if not isinstance(entry, dict):
        entry = {}
        assignments[slug] = entry
    entry.setdefault("assignment_code", assignment.get("id"))
    entry.setdefault("title", assignment.get("title"))
    entry.setdefault("type", assignment.get("type"))
    entry.setdefault("weight", assignment.get("weight"))
    entry.setdefault("due_week", assignment.get("due_week"))
    return slug, entry


def _next_extra_submission_index(
    submission_dir: Path,
    grade_band: str,
    assignment_manifest: dict,
) -> int:
    pattern = re.compile(
        rf"^submission_{re.escape(grade_band)}_extra_(\d+)",
        re.IGNORECASE,
    )
    seen: set[int] = set()
    if submission_dir.exists():
        for path in submission_dir.iterdir():
            match = pattern.match(path.stem)
            if match:
                seen.add(int(match.group(1)))
    identifiers = assignment_manifest.get("student_identifiers") or {}
    if isinstance(identifiers, dict):
        for filename in identifiers:
            match = pattern.match(Path(str(filename)).stem)
            if match:
                seen.add(int(match.group(1)))
    return max(seen, default=0) + 1


def _write_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_synthetic_submissions(
    conn: sqlite3.Connection,
    unit_directory: str | Path,
    assignment_codes: Sequence[str] | None = None,
    grade_bands: Sequence[str] | None = None,
    count_per_band: int = 1,
    provider: str = "qwen",
    model: str | None = None,
    temperature: float = 0.2,
    progress_callback: ProgressCallback | None = None,
) -> SyntheticSubmissionGenerationResult:
    """Generate extra synthetic student submissions for an existing unit package."""
    from feedback_lens.feedback.llm.providers import resolve_model_name

    if count_per_band < 1:
        raise ValueError("count_per_band must be at least 1.")

    unit_dir = Path(unit_directory)
    if not unit_dir.exists() or not unit_dir.is_dir():
        raise ValueError(f"Unit directory does not exist: {unit_dir}")

    schema = _load_json_file(unit_dir / "schema.json")
    manifest_path = unit_dir / "unit_manifest.json"
    manifest = _load_json_file(manifest_path, required=False)
    manifest.setdefault(
        "unit",
        {
            "course_code": schema.get("course_code"),
            "course_title": schema.get("course_title"),
        },
    )
    manifest.setdefault("materials", {})

    course_code = str(schema.get("course_code") or "").upper()
    if not course_code:
        raise ValueError("schema.json must include course_code.")
    selected = _selected_assignments(schema, assignment_codes)
    if not selected:
        raise ValueError("No assignments were found in schema.json.")
    selected_bands = _normalise_grade_bands(grade_bands)

    resolved_model = resolve_model_name(provider, model)
    run_id = _insert_run(
        conn,
        f"Additional synthetic submissions for {course_code} from {normalise_source_path(unit_dir)}",
        provider,
        resolved_model,
        temperature,
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
            normalise_source_path(unit_dir),
            json.dumps(schema, ensure_ascii=False),
            run_id,
        ),
    )
    conn.commit()
    _emit(
        progress_callback,
        f"Created extra synthetic submission run {run_id} using {provider}:{resolved_model}.",
    )

    written_paths: list[Path] = []

    try:
        lecture_texts = _load_lecture_texts(unit_dir)
        for assignment in selected:
            assignment_id = str(assignment.get("id") or assignment.get("assignment_code") or "")
            slug, assignment_manifest = _manifest_assignment_entry(manifest, assignment)
            assignment_dir = unit_dir / "assignments" / slug
            if not assignment_dir.exists():
                raise ValueError(f"Assignment directory does not exist: {assignment_dir}")

            spec_path = _find_generated_document(
                [assignment_dir / "spec.pdf", assignment_dir / "spec.txt"],
                list(assignment_dir.glob("spec.*")),
                f"{assignment_id} assignment specification",
            )
            rubric_path = _find_generated_document(
                [assignment_dir / "rubric.pdf", assignment_dir / "rubric.txt"],
                list(assignment_dir.glob("rubric.*")),
                f"{assignment_id} rubric",
            )
            tutorials_dir = unit_dir / "tutorials"
            worksheet_path = _find_generated_document(
                [
                    tutorials_dir / f"{slug}_worksheet.pdf",
                    tutorials_dir / f"{slug}_worksheet.txt",
                ],
                list(tutorials_dir.glob(f"{slug}*worksheet.*")),
                f"{assignment_id} tutorial worksheet",
            )
            sample_path = _find_generated_document(
                [
                    tutorials_dir / f"{slug}_sample_answers.pdf",
                    tutorials_dir / f"{slug}_sample_answers.txt",
                ],
                list(tutorials_dir.glob(f"{slug}*sample_answers.*")),
                f"{assignment_id} sample answers",
            )

            spec_text = _read_generated_document(spec_path)
            rubric_text = _read_generated_document(rubric_path)
            worksheet_text = _read_generated_document(worksheet_path)
            sample_text = _read_generated_document(sample_path)
            transcripts = _transcripts_for_assignment(assignment, lecture_texts)
            submission_dir = assignment_dir / "submissions"
            submission_dir.mkdir(parents=True, exist_ok=True)

            for grade_band in selected_bands:
                next_index = _next_extra_submission_index(
                    submission_dir,
                    grade_band,
                    assignment_manifest,
                )
                for offset in range(count_per_band):
                    variant_index = next_index + offset
                    variant_label = f"{variant_index:02d}"
                    variation_note = (
                        f"Create variant {variant_label} for testing. Make it distinct from "
                        "other synthetic submissions by changing the student's angle, examples, "
                        "evidence choices, structure, and mistake pattern while staying plausible "
                        f"for the {grade_band} grade band. Do not mention this variant instruction."
                    )
                    submission_step, submission_text, _ = _run_step(
                        conn,
                        run_id,
                        "stage_4_student_submission_extra",
                        prompts.submission_messages(
                            spec_text,
                            rubric_text,
                            transcripts,
                            worksheet_text,
                            sample_text,
                            grade_band,
                            variation_note=variation_note,
                        ),
                        provider,
                        resolved_model,
                        temperature,
                        assignment_code=assignment_id,
                        grade_band=grade_band,
                        progress_callback=progress_callback,
                        progress_label=(
                            f"{assignment_id} extra synthetic {grade_band} "
                            f"submission {variant_label}"
                        ),
                    )
                    submission_path = _write_pdf(
                        submission_dir / f"submission_{grade_band}_extra_{variant_label}.pdf",
                        submission_text,
                    )
                    identifiers = assignment_manifest.setdefault("student_identifiers", {})
                    if not isinstance(identifiers, dict):
                        identifiers = {}
                        assignment_manifest["student_identifiers"] = identifiers
                    identifiers[submission_path.name] = (
                        f"{slug}_{grade_band}_synthetic_extra_{variant_label}"
                    )
                    _write_manifest(manifest_path, manifest)
                    _insert_artifact(
                        conn,
                        run_id,
                        submission_step,
                        "student_submission",
                        f"{assignment_id} {grade_band} extra submission {variant_label}",
                        submission_path,
                        submission_text,
                    )
                    written_paths.append(submission_path)
                    _emit(progress_callback, f"Wrote student_submission: {submission_path}")

        _insert_artifact(
            conn,
            run_id,
            None,
            "manifest",
            "Updated unit manifest",
            manifest_path,
            manifest_path.read_text(encoding="utf-8"),
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
        _emit(progress_callback, f"Completed extra synthetic submission run {run_id}.")
        return SyntheticSubmissionGenerationResult(
            curriculum_run_id=run_id,
            course_code=course_code,
            output_root=unit_dir,
            provider=provider,
            model=resolved_model,
            written_paths=written_paths,
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
