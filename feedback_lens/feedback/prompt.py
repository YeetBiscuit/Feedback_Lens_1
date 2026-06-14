import json
import sqlite3


BASELINE_FEEDBACK_PROMPT_JSON_V1 = "baseline_feedback_json_v1"
BASELINE_DIRECT_FEEDBACK_PROMPT_JSON_V1 = "baseline_direct_feedback_json_v1"
UNIT_GROUNDED_FEEDBACK_PROMPT_JSON_V2 = "unit_grounded_feedback_json_v2"
DEFAULT_FEEDBACK_LENGTH = "standard"
DEFAULT_FEEDBACK_TONE = "clear_supportive"

FEEDBACK_LENGTH_OPTIONS = {
    "concise",
    DEFAULT_FEEDBACK_LENGTH,
    "detailed",
}
FEEDBACK_TONE_OPTIONS = {
    DEFAULT_FEEDBACK_TONE,
    "gentle_encouraging",
    "direct_no_fluff",
}

RETRIEVAL_FEEDBACK_PROMPT_TEMPLATE_VERSIONS = {
    BASELINE_FEEDBACK_PROMPT_JSON_V1,
    UNIT_GROUNDED_FEEDBACK_PROMPT_JSON_V2,
}
DIRECT_FEEDBACK_PROMPT_TEMPLATE_VERSIONS = {
    BASELINE_DIRECT_FEEDBACK_PROMPT_JSON_V1,
}
FEEDBACK_PROMPT_TEMPLATE_VERSIONS = sorted(
    RETRIEVAL_FEEDBACK_PROMPT_TEMPLATE_VERSIONS
    | DIRECT_FEEDBACK_PROMPT_TEMPLATE_VERSIONS
)
FEEDBACK_PROMPT_TEMPLATE_ALIASES = {
    "retrieval-v1": BASELINE_FEEDBACK_PROMPT_JSON_V1,
    "direct-v1": BASELINE_DIRECT_FEEDBACK_PROMPT_JSON_V1,
    "unit-grounded-v2": UNIT_GROUNDED_FEEDBACK_PROMPT_JSON_V2,
}
FEEDBACK_PROMPT_TEMPLATE_CHOICES = sorted(
    set(FEEDBACK_PROMPT_TEMPLATE_VERSIONS)
    | set(FEEDBACK_PROMPT_TEMPLATE_ALIASES)
)


def default_feedback_prompt_template_version(context_mode: str) -> str:
    if context_mode == "direct":
        return BASELINE_DIRECT_FEEDBACK_PROMPT_JSON_V1
    return UNIT_GROUNDED_FEEDBACK_PROMPT_JSON_V2


def validate_feedback_length(feedback_length: str | None) -> str:
    value = (feedback_length or DEFAULT_FEEDBACK_LENGTH).strip().lower()
    aliases = {
        "short": "concise",
        "medium": DEFAULT_FEEDBACK_LENGTH,
        "long": "detailed",
    }
    value = aliases.get(value, value)
    if value not in FEEDBACK_LENGTH_OPTIONS:
        raise ValueError(
            "feedback_length must be one of: "
            f"{', '.join(sorted(FEEDBACK_LENGTH_OPTIONS))}"
        )
    return value


def validate_feedback_tone(feedback_tone: str | None) -> str:
    value = (feedback_tone or DEFAULT_FEEDBACK_TONE).strip().lower()
    aliases = {
        "supportive": DEFAULT_FEEDBACK_TONE,
        "clear": DEFAULT_FEEDBACK_TONE,
        "gentle": "gentle_encouraging",
        "direct": "direct_no_fluff",
    }
    value = aliases.get(value, value)
    if value not in FEEDBACK_TONE_OPTIONS:
        raise ValueError(
            "feedback_tone must be one of: "
            f"{', '.join(sorted(FEEDBACK_TONE_OPTIONS))}"
        )
    return value


def validate_feedback_prompt_template_version(
    prompt_template_version: str,
    context_mode: str,
) -> str:
    version = prompt_template_version.strip()
    version = FEEDBACK_PROMPT_TEMPLATE_ALIASES.get(version, version)
    if context_mode == "direct":
        valid_versions = DIRECT_FEEDBACK_PROMPT_TEMPLATE_VERSIONS
    else:
        valid_versions = RETRIEVAL_FEEDBACK_PROMPT_TEMPLATE_VERSIONS

    if version not in valid_versions:
        raise ValueError(
            "prompt_template_version must be one of "
            f"{', '.join(sorted(valid_versions))} for {context_mode} mode."
        )
    return version


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
    prompt_template_version: str | None = None,
    feedback_length: str | None = None,
    feedback_tone: str | None = None,
) -> str:
    context_mode = "retrieval" if include_retrieved_context else "direct"
    resolved_prompt_template_version = validate_feedback_prompt_template_version(
        prompt_template_version or default_feedback_prompt_template_version(context_mode),
        context_mode,
    )
    resolved_feedback_length = validate_feedback_length(feedback_length)
    resolved_feedback_tone = validate_feedback_tone(feedback_tone)

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

    unit_grounding_rules = (
        """
Retrieved-context grounding requirements:
- When retrieved course context is relevant, use it to make the feedback visibly grounded in the unit's lectures, tutorials, examples, methods, or terminology.
- In `improvement_suggestion`, connect advice to specific retrieved unit concepts, methods, examples, or terminology when they genuinely support the point.
- In `evidence_summary`, briefly mention the relevant retrieved material by week number, title, or material type when it informed the judgement.
- Do not mention retrieved material if it is only loosely related to the criterion or student work.
- Do not force a retrieved-material reference into every criterion; use it where it improves feedback quality.
- Do not turn retrieved course context into extra mandatory requirements beyond the assignment specification or rubric.
""".strip()
        if resolved_prompt_template_version == UNIT_GROUNDED_FEEDBACK_PROMPT_JSON_V2
        else ""
    )

    length_rules = {
        "concise": (
            "- Keep feedback short and low-density.\n"
            "- Use 2 to 3 focused sentences for each strengths, areas_for_improvement, and improvement_suggestion field.\n"
            "- Keep lists to 2 or 3 high-priority items."
        ),
        "standard": (
            "- Use moderate detail.\n"
            "- Give enough context to be useful without overwhelming the student.\n"
            "- Prefer 3 to 5 concise sentences in each criterion feedback field."
        ),
        "detailed": (
            "- Give fuller explanation, including concrete examples or next-step guidance where useful.\n"
            "- Use 5 to 7 short items for key_strengths and priority_improvements when supported by the evidence.\n"
            "- Keep the structure scannable even when adding detail."
        ),
    }
    tone_rules = {
        "clear_supportive": (
            "- Be clear, specific, and supportive.\n"
            "- Avoid vague praise, but frame improvement advice as achievable next steps."
        ),
        "gentle_encouraging": (
            "- Use a gentler, encouraging tone.\n"
            "- Be careful with deficit language; name improvements without making the student feel personally judged."
        ),
        "direct_no_fluff": (
            "- Be direct and efficient.\n"
            "- Reduce warm-up phrasing and avoid unnecessary reassurance while staying respectful."
        ),
    }
    customisation_rules = f"""
Feedback customisation requirements:
- feedback_length: {resolved_feedback_length}
{length_rules[resolved_feedback_length]}
- feedback_tone: {resolved_feedback_tone}
{tone_rules[resolved_feedback_tone]}
- These settings affect wording only; do not change the required JSON schema.
""".strip()

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

{customisation_rules}

{unit_grounding_rules}

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
