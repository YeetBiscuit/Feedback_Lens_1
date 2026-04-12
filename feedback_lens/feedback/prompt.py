import json
import sqlite3


def _load_performance_levels_json(raw_json: str | None) -> dict | None:
    if not raw_json:
        return None
    try:
        value = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def build_feedback_prompt(
    assignment_row: sqlite3.Row,
    assignment_spec_row: sqlite3.Row,
    rubric_row: sqlite3.Row,
    rubric_criteria_rows: list[sqlite3.Row],
    submission_row: sqlite3.Row,
    retrieved_chunks: list[dict],
) -> str:
    criteria_payload = [
        {
            "criterion_id": row["criterion_id"],
            "criterion_name": row["criterion_name"],
            "criterion_description": row["criterion_description"],
            "performance_levels": _load_performance_levels_json(
                row["performance_levels_json"]
            ),
        }
        for row in rubric_criteria_rows
    ]

    retrieved_context = [
        {
            "rank_position": chunk["rank_position"],
            "chunk_id": chunk["chunk_id"],
            "title": chunk["title"],
            "material_type": chunk["material_type"],
            "week_number": chunk["week_number"],
            "page_number_start": chunk["page_number_start"],
            "page_number_end": chunk["page_number_end"],
            "matched_cues": chunk.get("matched_cues", []),
            "chunk_text": chunk["chunk_text"],
        }
        for chunk in retrieved_chunks
    ]

    response_schema = {
        "overall_feedback": {
            "overall_comment": "string",
            "key_strengths": ["string"],
            "priority_improvements": ["string"],
            "overall_grade_band": "N|P|C|D|HD",
        },
        "criterion_feedback": [
            {
                "criterion_id": 123,
                "criterion_name": "string",
                "strengths": "string",
                "areas_for_improvement": "string",
                "improvement_suggestion": "string",
                "suggested_level": "N|P|C|D|HD",
                "evidence_summary": "string",
            }
        ],
    }

    return f"""
You are generating personalised, rubric-aligned feedback for a higher-education computing assignment.

Return valid JSON only. Do not wrap the JSON in markdown fences. Do not add commentary before or after the JSON.

Required JSON schema:
{json.dumps(response_schema, ensure_ascii=False, indent=2)}

Rules:
- Include exactly one `criterion_feedback` item for each criterion_id listed below.
- Preserve the provided `criterion_id` values exactly.
- Base the feedback on the assignment specification, rubric, student submission, and retrieved course context.
- If the retrieved course context is weak or incomplete for a point, say that plainly instead of inventing evidence.
- Use concise, tutor-facing academic feedback language.
- `overall_grade_band` and each `suggested_level` must be one of: N, P, C, D, HD.
- `key_strengths` and `priority_improvements` should each contain 2 to 5 short items.

Assignment metadata:
{json.dumps(
    {
        "assignment_id": assignment_row["assignment_id"],
        "assignment_name": assignment_row["assignment_name"],
        "assignment_type": assignment_row["assignment_type"],
        "description": assignment_row["description"],
        "due_date": assignment_row["due_date"],
    },
    ensure_ascii=False,
    indent=2,
)}

Rubric criteria:
{json.dumps(criteria_payload, ensure_ascii=False, indent=2)}

Retrieved course context:
{json.dumps(retrieved_context, ensure_ascii=False, indent=2)}

Assignment specification text:
{assignment_spec_row["cleaned_text"]}

Rubric text:
{rubric_row["cleaned_text"] or rubric_row["raw_text"] or ""}

Student submission text:
{submission_row["cleaned_text"]}
""".strip()
