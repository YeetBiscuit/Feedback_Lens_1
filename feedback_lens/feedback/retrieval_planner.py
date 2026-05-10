import json
import re
import sqlite3
from dataclasses import dataclass

from feedback_lens.feedback.retrieval import normalize_retrieval_cues


RETRIEVAL_PLANNER_STRATEGY = "llm_planned_cue_v1"
RETRIEVAL_PLANNER_PROMPT_TEMPLATE_VERSION = "retrieval_planner_json_v1"
DEFAULT_MAX_RETRIEVAL_CUES = 8


@dataclass(slots=True)
class RetrievalPlannerPrompt:
    prompt_template_version: str
    prompt_text: str


def _load_json_dict(raw_json: str | None) -> dict | None:
    if not raw_json:
        return None

    try:
        value = json.loads(raw_json)
    except json.JSONDecodeError:
        return None

    return value if isinstance(value, dict) else None


def _criteria_payload(rubric_criteria_rows: list[sqlite3.Row]) -> list[dict]:
    return [
        {
            "criterion_id": row["criterion_id"],
            "criterion_name": row["criterion_name"],
            "criterion_description": row["criterion_description"],
            "performance_levels": _load_json_dict(row["performance_levels_json"]),
        }
        for row in rubric_criteria_rows
    ]


def build_retrieval_planner_prompt(
    assignment_row: sqlite3.Row,
    assignment_spec_row: sqlite3.Row,
    rubric_row: sqlite3.Row,
    rubric_criteria_rows: list[sqlite3.Row],
    submission_row: sqlite3.Row,
    max_cues: int = DEFAULT_MAX_RETRIEVAL_CUES,
) -> RetrievalPlannerPrompt:
    response_schema = {
        "retrieval_cues": [
            {
                "order": 1,
                "label": "short descriptive cue label",
                "text": "specific query text for retrieving relevant course materials",
                "rubric_criterion_ids": [123],
                "rationale": "why this course context is needed for fair judgement",
            }
        ]
    }

    assignment_payload = {
        "assignment_id": assignment_row["assignment_id"],
        "assignment_name": assignment_row["assignment_name"],
        "assignment_type": assignment_row["assignment_type"],
        "description": assignment_row["description"],
        "due_date": assignment_row["due_date"],
        "unit_code": assignment_row["unit_code"],
        "unit_name": assignment_row["unit_name"],
        "semester": assignment_row["semester"],
        "year": assignment_row["year"],
    }

    prompt = f"""
You are a retrieval planner for a higher-education feedback RAG system.

Your task is not to generate feedback. Your task is to decide what course-material context is needed so a later feedback generator can judge this student's work fairly under the rubric and within the unit scope.

Return valid JSON only. Do not wrap the JSON in markdown fences. Do not add commentary before or after the JSON.

Required JSON schema:
{json.dumps(response_schema, ensure_ascii=False, indent=2)}

Rules:
- Generate 1 to {max(max_cues, 1)} targeted retrieval cues.
- Each cue must be useful as a vector-search query against course materials such as lecture slides, lecture transcripts, tutorials, readings, worksheets, or sample solutions.
- Write cue text as a search request for relevant course knowledge, not as feedback to the student.
- Prefer course concepts, methods, standards, examples, assumptions, and terminology that are needed to evaluate the submission.
- Cover the assignment requirements, rubric criteria, and any important claims, omissions, or methods visible in the submission.
- Do not request administrative material such as due dates, file formats, late penalties, or submission instructions.
- Do not invent unit content. If the needed context is uncertain, phrase the cue as the concept or standard that should be retrieved.
- Use the provided criterion_id values when a cue is tied to one or more rubric criteria.

Assignment metadata:
{json.dumps(assignment_payload, ensure_ascii=False, indent=2)}

Rubric criteria:
{json.dumps(_criteria_payload(rubric_criteria_rows), ensure_ascii=False, indent=2)}

Rubric text:
{rubric_row["cleaned_text"] or ""}

Assignment specification text:
{assignment_spec_row["cleaned_text"]}

Student submission text:
{submission_row["cleaned_text"]}
""".strip()

    return RetrievalPlannerPrompt(
        prompt_template_version=RETRIEVAL_PLANNER_PROMPT_TEMPLATE_VERSION,
        prompt_text=prompt,
    )


def _extract_json_payload(response_text: str) -> dict:
    match = re.search(r"\{.*\}", response_text.strip(), re.DOTALL)
    if match is None:
        raise ValueError("Retrieval planner response did not contain a JSON object.")

    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as err:
        raise ValueError(f"Failed to parse retrieval planner JSON response: {err}") from err

    if not isinstance(payload, dict):
        raise ValueError("Retrieval planner JSON response must be an object.")

    return payload


def parse_retrieval_planner_response(
    response_text: str,
    max_cues: int = DEFAULT_MAX_RETRIEVAL_CUES,
) -> list[dict]:
    payload = _extract_json_payload(response_text)
    cues = normalize_retrieval_cues(
        payload.get("retrieval_cues"),
        max_cues=max(max_cues, 1),
    )
    if not cues:
        raise ValueError("Retrieval planner did not return any usable retrieval cues.")
    return cues
