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
    _rubric_row: sqlite3.Row,
    rubric_criteria_rows: list[sqlite3.Row],
    submission_row: sqlite3.Row,
    retrieved_chunks: list[dict] | None = None,
    include_retrieved_context: bool = True,
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
        for chunk in (retrieved_chunks or [])
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

    context_rule = (
        "- Base the feedback on the assignment specification, rubric, student submission, and retrieved course context.\n"
        "- If the retrieved course context is weak or incomplete for a point, say that plainly instead of inventing evidence."
        if include_retrieved_context
        else "- Base the feedback only on the assignment specification, rubric, and student submission.\n"
        "- Do not assume additional course context that is not present in those inputs."
    )

    retrieved_context_section = (
        f"""
Retrieved course context that could be helpful:
{json.dumps(retrieved_context, ensure_ascii=False, indent=2)}
""".strip()
        if include_retrieved_context
        else ""
    )

    return f"""
You are generating personalised, rubric-aligned feedback for a higher-education assignment.

Return valid JSON only. Do not wrap the JSON in markdown fences. Do not add commentary before or after the JSON.

Required JSON schema:
{json.dumps(response_schema, ensure_ascii=False, indent=2)}

Rules:
- Include exactly one `criterion_feedback` item for each criterion_id listed below.
- Preserve the provided `criterion_id` values exactly.
{context_rule}
- Use concise, tutor-facing academic feedback language.
- `overall_grade_band` and each `suggested_level` must be one of: N, P, C, D, HD.
- `key_strengths` and `priority_improvements` should each contain 2 to 5 short items.

Evidence hierarchy:
- The assignment specification and rubric are the highest authority for grading.
- The student submission text is the main evidence of the student's work.
- Retrieved course context may explain relevant concepts, methods, standards, and unit expectations.
- Retrieved course context must not create new mandatory requirements beyond the assignment specification or rubric.
- If there is tension between general best practice and the assignment specification/rubric, follow the assignment specification/rubric.

Capability boundary:
- You can only assess evidence available in the provided text.
- You cannot open, inspect, or verify external links, Figma prototypes, videos, images, screenshots, diagrams, files, code repositories, or interactive artefacts.
- Do not claim that an external artefact is broken, inaccessible, missing, non-functional, incomplete, low quality, correct, incorrect, or verified unless the provided text explicitly states this.
- Do not infer that a link is broken merely because it looks like an example, placeholder, or external URL.
- Use cautious wording such as "based on the written description", "not verifiable from the provided text", or "requires human inspection".

Artefact-dependent criteria:
- Some criteria may depend partly or fully on external or non-text artefacts, such as prototypes, videos, images, screenshots, diagrams, visual designs, demos, or live links.
- For such criteria, assess only the written evidence available in the student submission.
- If the artefact itself cannot be inspected, clearly state this limitation in the `evidence_summary`.
- Do not assign a fail-level grade solely because an artefact cannot be inspected by this system.
- You may assign a lower grade if the written description itself provides limited evidence of the required quality, complexity, functionality, or alignment.
- Assign N/Fail for an artefact-dependent criterion only if the provided text explicitly says the artefact is missing, non-functional, incomplete, or not submitted, or if there is no textual evidence at all for that required component.
- When a criterion cannot be fully judged from text alone, make the suggested level provisional by saying so in the `evidence_summary`.

Good examples of acceptable wording:
- "Based on the written description, the prototype appears to support a core ordering flow, but the prototype itself is not verifiable from the provided text."
- "A human marker would need to inspect the visual artefacts before finalising this criterion."
- "The written report provides limited evidence of complex branching flows or advanced interactive states."

Bad examples of unsupported wording:
- "The Figma link is broken."
- "The video is inaccessible."
- "The visual artefacts are missing."
- "The screenshot shows poor visual design."
- "The diagram is incorrect."
- "The visual artefacts are fully functional."

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

{retrieved_context_section}

Assignment specification text:
{assignment_spec_row["cleaned_text"]}

Student submission text:
{submission_row["cleaned_text"]}
""".strip()
