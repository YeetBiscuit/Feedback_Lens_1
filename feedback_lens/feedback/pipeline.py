import json
import re
import sqlite3
from dataclasses import dataclass

from feedback_lens.db.connection import ensure_schema_updates, fetch_latest_version_row
from feedback_lens.feedback.llm.providers import generate_text, resolve_model_name
from feedback_lens.feedback.prompt import build_feedback_prompt
from feedback_lens.feedback.retrieval import (
    load_assignment_spec_cues,
    retrieve_relevant_chunks,
)


VALID_GRADE_BANDS = {"N", "P", "C", "D", "HD"}
VALID_CONTEXT_MODES = {"retrieval", "direct"}


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


def _default_pipeline_version(context_mode: str) -> str:
    if context_mode == "direct":
        return "baseline_direct_v1"
    return "baseline_retrieval_v1"


def _default_prompt_template_version(context_mode: str) -> str:
    if context_mode == "direct":
        return "baseline_direct_feedback_json_v1"
    return "baseline_feedback_json_v1"


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


def generate_feedback_for_submission(
    conn: sqlite3.Connection,
    submission_id: int,
    provider: str = "qwen",
    model: str | None = None,
    top_k: int = 5,
    temperature: float = 0.2,
    pipeline_version: str | None = None,
    prompt_template_version: str | None = None,
    context_mode: str = "retrieval",
) -> FeedbackGenerationResult:
    ensure_schema_updates(conn)
    generation_id = None
    resolved_context_mode = _normalise_context_mode(context_mode)
    resolved_pipeline_version = pipeline_version or _default_pipeline_version(
        resolved_context_mode
    )
    resolved_prompt_template_version = (
        prompt_template_version
        or _default_prompt_template_version(resolved_context_mode)
    )
    retrieval_strategy = (
        "none_direct_v1"
        if resolved_context_mode == "direct"
        else "assignment_spec_multi_cue_v1"
    )
    resolved_model = resolve_model_name(provider, model)
    prompt = None
    raw_response = None

    try:
        inputs = _load_generation_inputs(conn, submission_id)
        retrieval_cues = (
            []
            if resolved_context_mode == "direct"
            else load_assignment_spec_cues(inputs["assignment_spec"])
        )

        cur = conn.execute(
            """
            INSERT INTO generation_runs
                (submission_id, assignment_id, rubric_id, pipeline_version,
                 llm_provider, llm_model, prompt_template_version, retrieval_strategy,
                 temperature, top_k, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
            """,
            (
                inputs["submission"]["submission_id"],
                inputs["assignment"]["assignment_id"],
                inputs["rubric"]["rubric_id"],
                resolved_pipeline_version,
                provider,
                resolved_model,
                resolved_prompt_template_version,
                retrieval_strategy,
                temperature,
                0 if resolved_context_mode == "direct" else top_k,
            ),
        )
        generation_id = cur.lastrowid

        retrieved_chunks = []
        retrieval_hits = []
        if resolved_context_mode == "retrieval":
            collection_name, retrieved_chunks, retrieval_hits = retrieve_relevant_chunks(
                conn,
                inputs["assignment"],
                retrieval_cues,
                top_k=top_k,
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
            retrieval_strategy=retrieval_strategy,
        )
    except Exception as err:
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
