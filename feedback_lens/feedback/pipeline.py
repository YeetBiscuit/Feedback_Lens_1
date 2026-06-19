import json
import re
import sqlite3
from dataclasses import dataclass

from feedback_lens.db.connection import ensure_schema_updates, fetch_latest_version_row
from feedback_lens.feedback.llm.providers import generate_text, resolve_model_name
from feedback_lens.feedback.prompt import (
    CUSTOM_FEEDBACK_MODIFIER_MODE,
    build_feedback_prompt,
    default_feedback_prompt_template_version,
    validate_feedback_length,
    validate_feedback_modifier_mode,
    validate_feedback_prompt_template_version,
    validate_feedback_tone,
)
from feedback_lens.feedback.retrieval import (
    DEFAULT_MAX_FINAL_CHUNKS,
    DEFAULT_PER_CUE_TOP_K,
    load_assignment_spec_cues,
    retrieve_relevant_chunks,
)
from feedback_lens.feedback.retrieval_planner import (
    DEFAULT_MAX_RETRIEVAL_CUES,
    RETRIEVAL_PLANNER_STRATEGY,
    build_retrieval_planner_prompt,
    parse_retrieval_planner_response,
)


VALID_GRADE_BANDS = {"N", "P", "C", "D", "HD"}
VALID_CONTEXT_MODES = {"retrieval", "direct"}
DEFAULT_FEEDBACK_PROVIDER = "nvidia_deepseek"
NO_RETRIEVAL_STRATEGY = "none_direct_v1"
ASSIGNMENT_SPEC_RETRIEVAL_STRATEGY = "assignment_spec_multi_cue_v1"
VALID_RETRIEVAL_STRATEGIES = {
    ASSIGNMENT_SPEC_RETRIEVAL_STRATEGY,
    RETRIEVAL_PLANNER_STRATEGY,
}


@dataclass(slots=True)
class FeedbackGenerationResult:
    generation_id: int
    overall_grade_band: str | None
    criterion_count: int
    retrieval_cue_count: int
    deduplicated_chunk_count: int
    provider: str
    model: str
    context_mode: str
    pipeline_version: str
    prompt_template_version: str
    retrieval_strategy: str
    per_cue_top_k: int
    max_final_chunks: int
    feedback_modifier_mode: str
    feedback_length: str | None
    feedback_tone: str | None


def _normalise_context_mode(value: str) -> str:
    context_mode = value.strip().lower()
    aliases = {
        "rag": "retrieval",
        "retrieved": "retrieval",
        "no-retrieval": "direct",
        "none": "direct",
    }
    context_mode = aliases.get(context_mode, context_mode)
    if context_mode not in VALID_CONTEXT_MODES:
        raise ValueError(
            "context_mode must be one of: "
            f"{', '.join(sorted(VALID_CONTEXT_MODES))}"
        )
    return context_mode


def _normalise_retrieval_strategy(
    value: str | None,
    context_mode: str,
) -> str:
    if context_mode == "direct":
        return NO_RETRIEVAL_STRATEGY

    strategy = (value or RETRIEVAL_PLANNER_STRATEGY).strip().lower()
    aliases = {
        "assignment-spec": ASSIGNMENT_SPEC_RETRIEVAL_STRATEGY,
        "assignment_spec": ASSIGNMENT_SPEC_RETRIEVAL_STRATEGY,
        "baseline": ASSIGNMENT_SPEC_RETRIEVAL_STRATEGY,
        "spec": ASSIGNMENT_SPEC_RETRIEVAL_STRATEGY,
        "llm-planned": RETRIEVAL_PLANNER_STRATEGY,
        "planned": RETRIEVAL_PLANNER_STRATEGY,
        "planner": RETRIEVAL_PLANNER_STRATEGY,
    }
    strategy = aliases.get(strategy, strategy)
    if strategy not in VALID_RETRIEVAL_STRATEGIES:
        raise ValueError(
            "retrieval_strategy must be one of: "
            f"{', '.join(sorted(VALID_RETRIEVAL_STRATEGIES))}"
        )
    return strategy


def _normalise_positive_int(value: int | None, default: int, label: str) -> int:
    resolved_value = default if value is None else value
    try:
        resolved_int = int(resolved_value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{label} must be a positive integer.") from err
    if resolved_int < 1:
        raise ValueError(f"{label} must be a positive integer.")
    return resolved_int


def _resolve_feedback_modifier_settings(
    feedback_modifier_mode: str | None,
    feedback_length: str | None,
    feedback_tone: str | None,
) -> tuple[str, str | None, str | None]:
    if feedback_modifier_mode is None and (
        feedback_length is not None or feedback_tone is not None
    ):
        feedback_modifier_mode = CUSTOM_FEEDBACK_MODIFIER_MODE

    resolved_mode = validate_feedback_modifier_mode(feedback_modifier_mode)
    if resolved_mode != CUSTOM_FEEDBACK_MODIFIER_MODE:
        return resolved_mode, None, None

    return (
        resolved_mode,
        validate_feedback_length(feedback_length),
        validate_feedback_tone(feedback_tone),
    )


def _default_pipeline_version(context_mode: str, retrieval_strategy: str) -> str:
    if context_mode == "direct":
        return "baseline_direct_v1"
    if retrieval_strategy == RETRIEVAL_PLANNER_STRATEGY:
        return "planned_retrieval_v1"
    return "baseline_retrieval_v1"


def _default_prompt_template_version(context_mode: str) -> str:
    return default_feedback_prompt_template_version(context_mode)


def _load_generation_inputs(
    conn: sqlite3.Connection,
    submission_id: int,
) -> dict:
    submission = conn.execute(
        """
        SELECT *
        FROM student_submissions
        WHERE submission_id = ?
        """,
        (submission_id,),
    ).fetchone()
    if submission is None:
        raise ValueError(f"No student submission found with submission_id={submission_id}")

    assignment = conn.execute(
        """
        SELECT a.*, u.unit_code, u.unit_name, u.semester, u.year
        FROM assignments AS a
        JOIN units AS u ON u.unit_id = a.unit_id
        WHERE a.assignment_id = ?
        """,
        (submission["assignment_id"],),
    ).fetchone()
    if assignment is None:
        raise ValueError(
            f"No assignment found with assignment_id={submission['assignment_id']}"
        )

    assignment_spec = fetch_latest_version_row(
        conn,
        "assignment_specs",
        "assignment_id",
        assignment["assignment_id"],
    )
    if assignment_spec is None:
        raise ValueError(
            f"No assignment specification found for assignment_id={assignment['assignment_id']}"
        )

    rubric = fetch_latest_version_row(
        conn,
        "rubrics",
        "assignment_id",
        assignment["assignment_id"],
    )
    if rubric is None:
        raise ValueError(
            f"No rubric found for assignment_id={assignment['assignment_id']}"
        )

    criteria = conn.execute(
        """
        SELECT *
        FROM rubric_criteria
        WHERE rubric_id = ?
        ORDER BY criterion_order, criterion_id
        """,
        (rubric["rubric_id"],),
    ).fetchall()
    if not criteria:
        raise ValueError(f"No rubric criteria found for rubric_id={rubric['rubric_id']}")

    return {
        "submission": submission,
        "assignment": assignment,
        "assignment_spec": assignment_spec,
        "rubric": rubric,
        "criteria": criteria,
    }


def _load_generation_inputs_for_run(
    conn: sqlite3.Connection,
    generation_id: int,
) -> dict:
    run = conn.execute(
        """
        SELECT *
        FROM generation_runs
        WHERE generation_id = ?
        """,
        (generation_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"No generation run found with generation_id={generation_id}")

    submission = conn.execute(
        """
        SELECT *
        FROM student_submissions
        WHERE submission_id = ?
        """,
        (run["submission_id"],),
    ).fetchone()
    if submission is None:
        raise ValueError(f"No student submission found with submission_id={run['submission_id']}")

    assignment = conn.execute(
        """
        SELECT a.*, u.unit_code, u.unit_name, u.semester, u.year
        FROM assignments AS a
        JOIN units AS u ON u.unit_id = a.unit_id
        WHERE a.assignment_id = ?
        """,
        (run["assignment_id"],),
    ).fetchone()
    if assignment is None:
        raise ValueError(f"No assignment found with assignment_id={run['assignment_id']}")

    assignment_spec = fetch_latest_version_row(
        conn,
        "assignment_specs",
        "assignment_id",
        run["assignment_id"],
    )
    if assignment_spec is None:
        raise ValueError(
            f"No assignment specification found for assignment_id={run['assignment_id']}"
        )

    rubric = conn.execute(
        """
        SELECT *
        FROM rubrics
        WHERE rubric_id = ?
        """,
        (run["rubric_id"],),
    ).fetchone()
    if rubric is None:
        raise ValueError(f"No rubric found for rubric_id={run['rubric_id']}")

    criteria = conn.execute(
        """
        SELECT *
        FROM rubric_criteria
        WHERE rubric_id = ?
        ORDER BY criterion_order, criterion_id
        """,
        (run["rubric_id"],),
    ).fetchall()
    if not criteria:
        raise ValueError(f"No rubric criteria found for rubric_id={run['rubric_id']}")

    return {
        "run": run,
        "submission": submission,
        "assignment": assignment,
        "assignment_spec": assignment_spec,
        "rubric": rubric,
        "criteria": criteria,
    }


def _load_retrieved_prompt_chunks_for_run(
    conn: sqlite3.Connection,
    generation_id: int,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            rr.query_text,
            rr.chunk_id,
            rr.rank_position,
            mc.chunk_text,
            mc.page_number_start,
            mc.page_number_end,
            um.title AS material_title,
            um.material_type,
            um.week_number
        FROM retrieval_records AS rr
        JOIN material_chunks AS mc ON mc.chunk_id = rr.chunk_id
        JOIN unit_materials AS um ON um.material_id = mc.material_id
        WHERE rr.generation_id = ?
          AND rr.used_in_prompt = 1
        ORDER BY rr.rank_position, rr.retrieval_record_id
        """,
        (generation_id,),
    ).fetchall()

    chunks_by_id: dict[int, dict] = {}
    for row in rows:
        chunk_id = int(row["chunk_id"])
        chunk = chunks_by_id.get(chunk_id)
        if chunk is None:
            chunk = {
                "rank_position": len(chunks_by_id) + 1,
                "chunk_id": chunk_id,
                "title": row["material_title"],
                "material_type": row["material_type"],
                "week_number": row["week_number"],
                "page_number_start": row["page_number_start"],
                "page_number_end": row["page_number_end"],
                "matched_cues": [],
                "chunk_text": row["chunk_text"],
            }
            chunks_by_id[chunk_id] = chunk

        query_text = _coerce_text(row["query_text"])
        if query_text and query_text not in chunk["matched_cues"]:
            chunk["matched_cues"].append(query_text)

    return list(chunks_by_id.values())


def _extract_json_payload(response_text: str) -> dict:
    match = re.search(r"\{.*\}", response_text.strip(), re.DOTALL)
    if match is None:
        raise ValueError("LLM response did not contain a JSON object.")

    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as err:
        raise ValueError(f"Failed to parse LLM JSON response: {err}") from err

    if not isinstance(payload, dict):
        raise ValueError("LLM response JSON must be an object.")

    return payload


def _normalise_grade_band(value: object) -> str | None:
    if value is None:
        return None

    text = str(value).strip().upper()
    aliases = {
        "FAIL": "N",
        "NOT SATISFACTORY": "N",
        "PASS": "P",
        "CREDIT": "C",
        "DISTINCTION": "D",
        "HIGH DISTINCTION": "HD",
    }
    text = aliases.get(text, text)

    if text in VALID_GRADE_BANDS:
        return text
    return None


def _coerce_text(value: object) -> str | None:
    if value is None:
        return None

    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        return "\n".join(parts) if parts else None

    text = str(value).strip()
    return text or None


def _coerce_json_list(value: object) -> str | None:
    if value is None:
        return None

    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return json.dumps(items, ensure_ascii=False) if items else None

    text = str(value).strip()
    if not text:
        return None

    return json.dumps([text], ensure_ascii=False)


def _map_criterion_feedback(
    returned_items: object,
    expected_criteria: list[sqlite3.Row],
) -> dict[int, dict]:
    if not isinstance(returned_items, list):
        raise ValueError("criterion_feedback must be a JSON array.")

    criteria_by_id = {row["criterion_id"]: row for row in expected_criteria}
    criteria_by_name = {
        row["criterion_name"].casefold(): row["criterion_id"] for row in expected_criteria
    }
    mapped: dict[int, dict] = {}

    for item in returned_items:
        if not isinstance(item, dict):
            continue

        criterion_id = item.get("criterion_id")
        if criterion_id is not None:
            try:
                criterion_id = int(criterion_id)
            except (TypeError, ValueError):
                criterion_id = None

        if criterion_id is None:
            criterion_name = _coerce_text(item.get("criterion_name"))
            if criterion_name is not None:
                criterion_id = criteria_by_name.get(criterion_name.casefold())

        if criterion_id not in criteria_by_id or criterion_id in mapped:
            continue

        mapped[criterion_id] = item

    missing_ids = [
        row["criterion_id"]
        for row in expected_criteria
        if row["criterion_id"] not in mapped
    ]
    if missing_ids:
        raise ValueError(
            f"LLM response did not include feedback for criterion_id(s): {missing_ids}"
        )

    return mapped


def _create_retrieval_planning_record(
    conn: sqlite3.Connection,
    generation_id: int,
    strategy: str,
    provider: str,
    model: str,
    prompt_template_version: str,
    prompt_text: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO retrieval_planning_records
            (generation_id, strategy, provider, model, prompt_template_version,
             prompt_text, status)
        VALUES (?, ?, ?, ?, ?, ?, 'running')
        """,
        (
            generation_id,
            strategy,
            provider,
            model,
            prompt_template_version,
            prompt_text,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _record_retrieval_planner_response(
    conn: sqlite3.Connection,
    planning_record_id: int,
    raw_response_text: str,
) -> None:
    conn.execute(
        """
        UPDATE retrieval_planning_records
        SET raw_response_text = ?
        WHERE planning_record_id = ?
        """,
        (raw_response_text, planning_record_id),
    )
    conn.commit()


def _complete_retrieval_planning_record(
    conn: sqlite3.Connection,
    planning_record_id: int,
    retrieval_cues: list[dict],
) -> None:
    conn.execute(
        """
        UPDATE retrieval_planning_records
        SET planned_cues_json = ?,
            status = 'completed',
            completed_at = CURRENT_TIMESTAMP
        WHERE planning_record_id = ?
        """,
        (
            json.dumps(retrieval_cues, ensure_ascii=False),
            planning_record_id,
        ),
    )
    conn.commit()


def _fail_retrieval_planning_record(
    conn: sqlite3.Connection,
    planning_record_id: int,
    error_message: str,
) -> None:
    conn.execute(
        """
        UPDATE retrieval_planning_records
        SET status = 'failed',
            error_message = ?,
            completed_at = CURRENT_TIMESTAMP
        WHERE planning_record_id = ?
          AND status != 'completed'
        """,
        (error_message, planning_record_id),
    )
    conn.commit()


def generate_feedback_for_submission(
    conn: sqlite3.Connection,
    submission_id: int,
    provider: str = DEFAULT_FEEDBACK_PROVIDER,
    model: str | None = None,
    top_k: int | None = None,
    per_cue_top_k: int | None = None,
    max_final_chunks: int | None = None,
    temperature: float = 0.2,
    pipeline_version: str | None = None,
    prompt_template_version: str | None = None,
    context_mode: str = "retrieval",
    retrieval_strategy: str | None = None,
    feedback_modifier_mode: str | None = None,
    feedback_length: str | None = None,
    feedback_tone: str | None = None,
    planner_max_cues: int = DEFAULT_MAX_RETRIEVAL_CUES,
) -> FeedbackGenerationResult:
    ensure_schema_updates(conn)
    generation_id = None
    planning_record_id = None
    resolved_context_mode = _normalise_context_mode(context_mode)
    resolved_retrieval_strategy = _normalise_retrieval_strategy(
        retrieval_strategy,
        resolved_context_mode,
    )
    resolved_pipeline_version = pipeline_version or _default_pipeline_version(
        resolved_context_mode,
        resolved_retrieval_strategy,
    )
    resolved_prompt_template_version = (
        prompt_template_version
        or _default_prompt_template_version(resolved_context_mode)
    )
    resolved_prompt_template_version = validate_feedback_prompt_template_version(
        resolved_prompt_template_version,
        resolved_context_mode,
    )
    (
        resolved_feedback_modifier_mode,
        resolved_feedback_length,
        resolved_feedback_tone,
    ) = _resolve_feedback_modifier_settings(
        feedback_modifier_mode,
        feedback_length,
        feedback_tone,
    )
    resolved_model = resolve_model_name(provider, model)
    if resolved_context_mode == "direct":
        resolved_per_cue_top_k = 0
        resolved_max_final_chunks = 0
    else:
        resolved_per_cue_top_k = _normalise_positive_int(
            per_cue_top_k if per_cue_top_k is not None else top_k,
            DEFAULT_PER_CUE_TOP_K,
            "per_cue_top_k",
        )
        resolved_max_final_chunks = _normalise_positive_int(
            max_final_chunks,
            DEFAULT_MAX_FINAL_CHUNKS,
            "max_final_chunks",
        )
    prompt = None
    raw_response = None

    try:
        inputs = _load_generation_inputs(conn, submission_id)
        retrieval_cues = []

        cur = conn.execute(
            """
            INSERT INTO generation_runs
                (submission_id, assignment_id, rubric_id, pipeline_version,
                 llm_provider, llm_model, prompt_template_version, retrieval_strategy,
                 temperature, top_k, per_cue_top_k, max_final_chunks, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
            """,
            (
                inputs["submission"]["submission_id"],
                inputs["assignment"]["assignment_id"],
                inputs["rubric"]["rubric_id"],
                resolved_pipeline_version,
                provider,
                resolved_model,
                resolved_prompt_template_version,
                resolved_retrieval_strategy,
                temperature,
                resolved_per_cue_top_k,
                resolved_per_cue_top_k,
                resolved_max_final_chunks,
            ),
        )
        generation_id = cur.lastrowid

        if resolved_context_mode == "retrieval":
            if resolved_retrieval_strategy == RETRIEVAL_PLANNER_STRATEGY:
                planner_prompt = build_retrieval_planner_prompt(
                    inputs["assignment"],
                    inputs["assignment_spec"],
                    inputs["rubric"],
                    inputs["criteria"],
                    inputs["submission"],
                    max_cues=planner_max_cues,
                )
                planning_record_id = _create_retrieval_planning_record(
                    conn,
                    generation_id,
                    resolved_retrieval_strategy,
                    provider,
                    resolved_model,
                    planner_prompt.prompt_template_version,
                    planner_prompt.prompt_text,
                )
                planner_raw_response = generate_text(
                    planner_prompt.prompt_text,
                    provider=provider,
                    model=resolved_model,
                    temperature=temperature,
                )
                _record_retrieval_planner_response(
                    conn,
                    planning_record_id,
                    planner_raw_response,
                )
                retrieval_cues = parse_retrieval_planner_response(
                    planner_raw_response,
                    max_cues=planner_max_cues,
                )
                _complete_retrieval_planning_record(
                    conn,
                    planning_record_id,
                    retrieval_cues,
                )
            else:
                retrieval_cues = load_assignment_spec_cues(inputs["assignment_spec"])

        retrieved_chunks = []
        retrieval_hits = []
        if resolved_context_mode == "retrieval":
            collection_name, retrieved_chunks, retrieval_hits = retrieve_relevant_chunks(
                conn,
                inputs["assignment"],
                retrieval_cues,
                per_cue_top_k=resolved_per_cue_top_k,
                max_final_chunks=resolved_max_final_chunks,
            )
            if not retrieved_chunks:
                raise ValueError(
                    f"No course material chunks were retrieved from collection '{collection_name}'. "
                    "Ingest unit materials before generating feedback."
                )

            prompt_chunk_ids = {chunk["chunk_id"] for chunk in retrieved_chunks}
            for hit in retrieval_hits:
                conn.execute(
                    """
                    INSERT INTO retrieval_records
                        (generation_id, criterion_id, query_text, chunk_id,
                         rank_position, similarity_score, used_in_prompt)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generation_id,
                        None,
                        hit["query_text"],
                        hit["chunk_id"],
                        hit["rank_position"],
                        hit["similarity_score"],
                        1 if hit["chunk_id"] in prompt_chunk_ids else 0,
                    ),
                )
            conn.commit()

        prompt = build_feedback_prompt(
            inputs["assignment"],
            inputs["assignment_spec"],
            inputs["rubric"],
            inputs["criteria"],
            inputs["submission"],
            retrieved_chunks,
            include_retrieved_context=resolved_context_mode == "retrieval",
            prompt_template_version=resolved_prompt_template_version,
            feedback_modifier_mode=resolved_feedback_modifier_mode,
            feedback_length=resolved_feedback_length,
            feedback_tone=resolved_feedback_tone,
        )
        conn.execute(
            """
            UPDATE generation_runs
            SET prompt_text = ?
            WHERE generation_id = ?
            """,
            (prompt, generation_id),
        )
        conn.commit()

        raw_response = generate_text(
            prompt,
            provider=provider,
            model=resolved_model,
            temperature=temperature,
        )
        conn.execute(
            """
            UPDATE generation_runs
            SET raw_response_text = ?
            WHERE generation_id = ?
            """,
            (raw_response, generation_id),
        )
        conn.commit()

        payload = _extract_json_payload(raw_response)
        overall_feedback = payload.get("overall_feedback")
        if not isinstance(overall_feedback, dict):
            raise ValueError("LLM response is missing an overall_feedback object.")

        mapped_criteria = _map_criterion_feedback(
            payload.get("criterion_feedback"),
            inputs["criteria"],
        )

        for criterion in inputs["criteria"]:
            item = mapped_criteria[criterion["criterion_id"]]
            conn.execute(
                """
                INSERT INTO criterion_feedback
                    (generation_id, criterion_id, strengths, areas_for_improvement,
                     improvement_suggestion, suggested_level, evidence_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_id,
                    criterion["criterion_id"],
                    _coerce_text(item.get("strengths")),
                    _coerce_text(item.get("areas_for_improvement")),
                    _coerce_text(item.get("improvement_suggestion")),
                    _normalise_grade_band(item.get("suggested_level")),
                    _coerce_text(item.get("evidence_summary")),
                ),
            )

        overall_grade_band = _normalise_grade_band(
            overall_feedback.get("overall_grade_band")
        )
        conn.execute(
            """
            INSERT INTO overall_feedback
                (generation_id, overall_comment, key_strengths,
                 priority_improvements, overall_grade_band)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                generation_id,
                _coerce_text(overall_feedback.get("overall_comment")),
                _coerce_json_list(overall_feedback.get("key_strengths")),
                _coerce_json_list(overall_feedback.get("priority_improvements")),
                overall_grade_band,
            ),
        )
        conn.execute(
            """
            UPDATE generation_runs
            SET status = 'completed',
                completed_at = CURRENT_TIMESTAMP
            WHERE generation_id = ?
            """,
            (generation_id,),
        )
        conn.commit()

        return FeedbackGenerationResult(
            generation_id=generation_id,
            overall_grade_band=overall_grade_band,
            criterion_count=len(inputs["criteria"]),
            retrieval_cue_count=len(retrieval_cues),
            deduplicated_chunk_count=len(retrieved_chunks),
            provider=provider,
            model=resolved_model,
            context_mode=resolved_context_mode,
            pipeline_version=resolved_pipeline_version,
            prompt_template_version=resolved_prompt_template_version,
            retrieval_strategy=resolved_retrieval_strategy,
            per_cue_top_k=resolved_per_cue_top_k,
            max_final_chunks=resolved_max_final_chunks,
            feedback_modifier_mode=resolved_feedback_modifier_mode,
            feedback_length=resolved_feedback_length,
            feedback_tone=resolved_feedback_tone,
        )
    except Exception as err:
        if planning_record_id is not None:
            _fail_retrieval_planning_record(conn, planning_record_id, str(err))
        if generation_id is not None:
            conn.execute(
                """
                UPDATE generation_runs
                SET status = 'failed',
                    prompt_text = COALESCE(prompt_text, ?),
                    raw_response_text = COALESCE(raw_response_text, ?),
                    error_message = ?,
                    completed_at = CURRENT_TIMESTAMP
                WHERE generation_id = ?
                """,
                (prompt, raw_response, str(err), generation_id),
            )
            conn.commit()
        raise


def regenerate_feedback_for_criterion(
    conn: sqlite3.Connection,
    generation_id: int,
    criterion_id: int,
    feedback_modifier_mode: str | None = None,
    feedback_length: str | None = None,
    feedback_tone: str | None = None,
) -> dict:
    ensure_schema_updates(conn)
    (
        resolved_feedback_modifier_mode,
        resolved_feedback_length,
        resolved_feedback_tone,
    ) = _resolve_feedback_modifier_settings(
        feedback_modifier_mode,
        feedback_length,
        feedback_tone,
    )
    inputs = _load_generation_inputs_for_run(conn, generation_id)
    run = inputs["run"]
    criteria = [
        row for row in inputs["criteria"] if int(row["criterion_id"]) == int(criterion_id)
    ]
    if not criteria:
        raise ValueError(
            f"criterion_id={criterion_id} is not part of generation_id={generation_id}"
        )

    existing_feedback = conn.execute(
        """
        SELECT criterion_feedback_id
        FROM criterion_feedback
        WHERE generation_id = ?
          AND criterion_id = ?
        """,
        (generation_id, criterion_id),
    ).fetchone()
    if existing_feedback is None:
        raise ValueError(
            f"No criterion feedback found for generation_id={generation_id}, "
            f"criterion_id={criterion_id}"
        )

    retrieval_strategy = run["retrieval_strategy"]
    context_mode = (
        "direct"
        if retrieval_strategy == NO_RETRIEVAL_STRATEGY
        else "retrieval"
    )
    prompt_template_version = validate_feedback_prompt_template_version(
        run["prompt_template_version"],
        context_mode,
    )
    retrieved_chunks = (
        []
        if context_mode == "direct"
        else _load_retrieved_prompt_chunks_for_run(conn, generation_id)
    )
    prompt = build_feedback_prompt(
        inputs["assignment"],
        inputs["assignment_spec"],
        inputs["rubric"],
        criteria,
        inputs["submission"],
        retrieved_chunks,
        include_retrieved_context=context_mode == "retrieval",
        prompt_template_version=prompt_template_version,
        feedback_modifier_mode=resolved_feedback_modifier_mode,
        feedback_length=resolved_feedback_length,
        feedback_tone=resolved_feedback_tone,
    )
    provider = run["llm_provider"] or DEFAULT_FEEDBACK_PROVIDER
    model = run["llm_model"]
    raw_response = generate_text(
        prompt,
        provider=provider,
        model=model,
        temperature=run["temperature"] if run["temperature"] is not None else 0.2,
    )
    payload = _extract_json_payload(raw_response)
    mapped_criteria = _map_criterion_feedback(
        payload.get("criterion_feedback"),
        criteria,
    )
    item = mapped_criteria[int(criterion_id)]

    conn.execute(
        """
        UPDATE criterion_feedback
        SET strengths = ?,
            areas_for_improvement = ?,
            improvement_suggestion = ?,
            suggested_level = ?,
            evidence_summary = ?
        WHERE generation_id = ?
          AND criterion_id = ?
        """,
        (
            _coerce_text(item.get("strengths")),
            _coerce_text(item.get("areas_for_improvement")),
            _coerce_text(item.get("improvement_suggestion")),
            _normalise_grade_band(item.get("suggested_level")),
            _coerce_text(item.get("evidence_summary")),
            generation_id,
            criterion_id,
        ),
    )
    conn.commit()

    updated = conn.execute(
        """
        SELECT
            rc.criterion_id,
            rc.criterion_name,
            rc.criterion_description,
            rc.criterion_order,
            cf.strengths,
            cf.areas_for_improvement,
            cf.improvement_suggestion,
            cf.suggested_level,
            cf.evidence_summary,
            cf.mark
        FROM criterion_feedback AS cf
        JOIN rubric_criteria AS rc ON rc.criterion_id = cf.criterion_id
        WHERE cf.generation_id = ?
          AND cf.criterion_id = ?
        """,
        (generation_id, criterion_id),
    ).fetchone()
    if updated is None:
        raise ValueError(
            f"No criterion feedback found for generation_id={generation_id}, "
            f"criterion_id={criterion_id}"
        )

    return dict(updated)
