import json
import sqlite3
from typing import Any


def list_generation_runs(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            gr.generation_id,
            gr.status,
            gr.started_at,
            gr.completed_at,
            gr.llm_provider,
            gr.llm_model,
            gr.top_k,
            gr.temperature,
            ss.student_identifier,
            a.assignment_name,
            u.unit_code,
            of.overall_grade_band
        FROM generation_runs AS gr
        JOIN student_submissions AS ss ON ss.submission_id = gr.submission_id
        JOIN assignments AS a ON a.assignment_id = gr.assignment_id
        JOIN units AS u ON u.unit_id = a.unit_id
        LEFT JOIN overall_feedback AS of ON of.generation_id = gr.generation_id
        ORDER BY gr.generation_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def list_generation_run_ids(
    conn: sqlite3.Connection,
    limit: int | None = None,
) -> list[int]:
    query = """
        SELECT generation_id
        FROM generation_runs
        ORDER BY generation_id DESC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)

    return [row["generation_id"] for row in conn.execute(query, params).fetchall()]


def fetch_generation_review(
    conn: sqlite3.Connection,
    generation_id: int,
) -> dict:
    run = conn.execute(
        """
        SELECT
            gr.*,
            ss.student_identifier,
            ss.original_file_path,
            a.assignment_name,
            a.assignment_type,
            a.description AS assignment_description,
            a.due_date,
            u.unit_code,
            u.unit_name,
            u.semester,
            u.year,
            r.version AS rubric_version,
            s.version AS spec_version
        FROM generation_runs AS gr
        JOIN student_submissions AS ss ON ss.submission_id = gr.submission_id
        JOIN assignments AS a ON a.assignment_id = gr.assignment_id
        JOIN units AS u ON u.unit_id = a.unit_id
        LEFT JOIN rubrics AS r ON r.rubric_id = gr.rubric_id
        LEFT JOIN assignment_specs AS s
            ON s.assignment_id = gr.assignment_id
           AND s.version = (
                SELECT MAX(version)
                FROM assignment_specs
                WHERE assignment_id = gr.assignment_id
           )
        WHERE gr.generation_id = ?
        """,
        (generation_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"No generation run found with generation_id={generation_id}")

    overall_feedback = conn.execute(
        """
        SELECT *
        FROM overall_feedback
        WHERE generation_id = ?
        """,
        (generation_id,),
    ).fetchone()

    criterion_feedback = conn.execute(
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
            cf.evidence_summary
        FROM criterion_feedback AS cf
        JOIN rubric_criteria AS rc ON rc.criterion_id = cf.criterion_id
        WHERE cf.generation_id = ?
        ORDER BY rc.criterion_order, rc.criterion_id
        """,
        (generation_id,),
    ).fetchall()

    retrieval_records = conn.execute(
        """
        SELECT
            rr.retrieval_record_id,
            rr.query_text,
            rr.chunk_id,
            rr.rank_position,
            rr.similarity_score,
            rr.used_in_prompt,
            mc.chunk_index,
            mc.chunk_text,
            mc.page_number_start,
            mc.page_number_end,
            um.material_id,
            um.title AS material_title,
            um.material_type,
            um.week_number,
            um.source_file_path
        FROM retrieval_records AS rr
        JOIN material_chunks AS mc ON mc.chunk_id = rr.chunk_id
        JOIN unit_materials AS um ON um.material_id = mc.material_id
        WHERE rr.generation_id = ?
        ORDER BY rr.rank_position, rr.retrieval_record_id
        """,
        (generation_id,),
    ).fetchall()

    return {
        "run": run,
        "overall_feedback": overall_feedback,
        "criterion_feedback": criterion_feedback,
        "retrieval_records": retrieval_records,
    }


def parse_json_text_list(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []

    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return [raw_value]

    if not isinstance(parsed, list):
        return [str(parsed)]

    return [str(item) for item in parsed]


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _preview_text(text: str | None, limit: int, full_text: bool) -> str | None:
    if text is None:
        return None
    cleaned = " ".join(text.split())
    if full_text or len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit].rstrip()}..."


def generation_review_to_export_dict(
    review: dict,
    include_prompt: bool = False,
    include_response: bool = False,
    include_chunk_text: bool = False,
    full_chunks: bool = False,
    chunk_chars: int = 240,
) -> dict[str, Any]:
    run = row_to_dict(review["run"]) or {}
    prompt_text = run.pop("prompt_text", None)
    raw_response_text = run.pop("raw_response_text", None)

    if include_prompt:
        run["prompt_text"] = prompt_text
    if include_response:
        run["raw_response_text"] = raw_response_text

    overall = row_to_dict(review["overall_feedback"])
    if overall is not None:
        overall["key_strengths"] = parse_json_text_list(overall.get("key_strengths"))
        overall["priority_improvements"] = parse_json_text_list(
            overall.get("priority_improvements")
        )

    criteria = [row_to_dict(row) for row in review["criterion_feedback"]]

    retrievals = []
    for row in review["retrieval_records"]:
        item = row_to_dict(row) or {}
        chunk_text = item.pop("chunk_text", None)
        if include_chunk_text:
            item["chunk_text"] = _preview_text(chunk_text, chunk_chars, full_chunks)
        retrievals.append(item)

    return {
        "export_version": 1,
        "generation_run": run,
        "overall_feedback": overall,
        "criterion_feedback": criteria,
        "retrieval_records": retrievals,
    }


def _markdown_list(items: list[str]) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"- {item}" for item in items)


def _markdown_value(value: Any) -> str:
    if value is None or value == "":
        return "None"
    return str(value)


def format_generation_review_markdown(export_payload: dict[str, Any]) -> str:
    run = export_payload["generation_run"]
    overall = export_payload["overall_feedback"]
    criteria = export_payload["criterion_feedback"]
    retrievals = export_payload["retrieval_records"]

    lines = [
        f"# Feedback Generation Run {run['generation_id']}",
        "",
        "## Metadata",
        "",
        f"- Unit: {_markdown_value(run.get('unit_code'))} - {_markdown_value(run.get('unit_name'))}",
        f"- Assignment: {_markdown_value(run.get('assignment_name'))}",
        f"- Student: {_markdown_value(run.get('student_identifier'))}",
        f"- Status: {_markdown_value(run.get('status'))}",
        f"- Overall grade band: {_markdown_value((overall or {}).get('overall_grade_band'))}",
        f"- Provider: {_markdown_value(run.get('llm_provider'))}:{_markdown_value(run.get('llm_model'))}",
        f"- Pipeline: {_markdown_value(run.get('pipeline_version'))}",
        f"- Prompt template: {_markdown_value(run.get('prompt_template_version'))}",
        f"- Retrieval strategy: {_markdown_value(run.get('retrieval_strategy'))}",
        f"- Top K: {_markdown_value(run.get('top_k'))}",
        f"- Temperature: {_markdown_value(run.get('temperature'))}",
        f"- Started: {_markdown_value(run.get('started_at'))}",
        f"- Completed: {_markdown_value(run.get('completed_at'))}",
        f"- Submission file: {_markdown_value(run.get('original_file_path'))}",
    ]

    if run.get("error_message"):
        lines.append(f"- Error: {run['error_message']}")

    lines.extend(["", "## Overall Feedback", ""])
    if overall is None:
        lines.append("(none)")
    else:
        lines.extend(
            [
                f"Grade band: {_markdown_value(overall.get('overall_grade_band'))}",
                "",
                "### Overall Comment",
                "",
                _markdown_value(overall.get("overall_comment")),
                "",
                "### Key Strengths",
                "",
                _markdown_list(overall.get("key_strengths") or []),
                "",
                "### Priority Improvements",
                "",
                _markdown_list(overall.get("priority_improvements") or []),
            ]
        )

    lines.extend(["", "## Criterion Feedback", ""])
    if not criteria:
        lines.append("(none)")
    else:
        for item in criteria:
            lines.extend(
                [
                    f"### {item.get('criterion_order')}. {item.get('criterion_name')}",
                    "",
                    f"Suggested level: {_markdown_value(item.get('suggested_level'))}",
                    "",
                    f"Strengths: {_markdown_value(item.get('strengths'))}",
                    "",
                    f"Areas for improvement: {_markdown_value(item.get('areas_for_improvement'))}",
                    "",
                    f"Improvement suggestion: {_markdown_value(item.get('improvement_suggestion'))}",
                    "",
                    f"Evidence summary: {_markdown_value(item.get('evidence_summary'))}",
                    "",
                ]
            )

    lines.extend(["## Retrieved Chunks", ""])
    if not retrievals:
        lines.append("(none)")
    else:
        for item in retrievals:
            source_bits = [
                item.get("material_title"),
                item.get("material_type"),
            ]
            if item.get("week_number") is not None:
                source_bits.append(f"week {item['week_number']}")
            source = " | ".join(str(bit) for bit in source_bits if bit)
            lines.extend(
                [
                    f"### Chunk {item.get('chunk_id')}",
                    "",
                    f"- Rank: {_markdown_value(item.get('rank_position'))}",
                    f"- Similarity score: {_markdown_value(item.get('similarity_score'))}",
                    f"- Used in prompt: {_markdown_value(item.get('used_in_prompt'))}",
                    f"- Source: {_markdown_value(source)}",
                    f"- Pages: {_markdown_value(item.get('page_number_start'))}-{_markdown_value(item.get('page_number_end'))}",
                    "",
                    "Query:",
                    "",
                    _markdown_value(item.get("query_text")),
                    "",
                ]
            )
            if "chunk_text" in item:
                lines.extend(["Chunk text:", "", _markdown_value(item.get("chunk_text")), ""])

    if "prompt_text" in run:
        lines.extend(["## Prompt Text", "", "```text", run.get("prompt_text") or "", "```", ""])

    if "raw_response_text" in run:
        lines.extend(
            [
                "## Raw Response Text",
                "",
                "```text",
                run.get("raw_response_text") or "",
                "```",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"
