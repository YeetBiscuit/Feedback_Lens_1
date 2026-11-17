"""Output contracts for OpenAI pipeline calls, independent of model settings."""

from copy import deepcopy
from dataclasses import replace

from feedback_lens.feedback.llm.config import StageModelConfig


def _object(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _strings() -> dict:
    return {"type": "array", "items": {"type": "string"}}


GRADE_BAND = {"type": "string", "enum": ["N", "P", "C", "D", "HD"]}

FEEDBACK_SCHEMA = _object({
    "overall_feedback": _object({
        "overall_comment": {"type": "string"},
        "key_strengths": _strings(),
        "priority_improvements": _strings(),
        "overall_grade_band": GRADE_BAND,
    }),
    "criterion_feedback": {
        "type": "array",
        "items": _object({
            "criterion_id": {"type": "integer"},
            "criterion_name": {"type": "string"},
            "strengths": {"type": "string"},
            "areas_for_improvement": {"type": "string"},
            "improvement_suggestion": {"type": "string"},
            "suggested_level": GRADE_BAND,
            "evidence_summary": {"type": "string"},
        }),
    },
})

PLANNER_SCHEMA = _object({
    "retrieval_cues": {
        "type": "array",
        "items": _object({
            "order": {"type": "integer"},
            "label": {"type": "string"},
            "text": {"type": "string"},
            "rubric_criterion_ids": {"type": "array", "items": {"type": "integer"}},
            "rationale": {"type": "string"},
        }),
    },
})

OUTPUT_SCHEMAS = {
    "feedback_v1": FEEDBACK_SCHEMA,
    "retrieval_plan_v1": PLANNER_SCHEMA,
}


def with_structured_output(config: StageModelConfig, contract: str) -> StageModelConfig:
    """Enforce the operation's contract while preserving model-specific options.

    Apply after resolving saved settings too, so older runs gain the contract
    when regenerated. Other providers keep their existing request signatures.
    """
    if config.provider != "openai":
        return config
    schema = OUTPUT_SCHEMAS[contract]
    options = deepcopy(config.request_options)
    text = options.get("text", {})
    if not isinstance(text, dict):
        raise ValueError("OpenAI text request options must be an object.")
    options["text"] = {
        **text,
        "format": {
            "type": "json_schema",
            "name": contract,
            "strict": True,
            "schema": deepcopy(schema),
        },
    }
    return replace(config, request_options=options)
