"""
Internal quality gate for generated feedback.

Runs LLM judges over freshly generated feedback and, if the scores fall
below threshold, regenerates once with judge-derived revision notes.
Judge results are recorded for evaluation but are not surfaced to educators.
"""

import json
import sqlite3

from feedback_lens.feedback.pipeline import (
    FeedbackGenerationResult,
    generate_feedback_for_submission,
)
from feedback_lens.feedback.review import fetch_generation_review, parse_json_text_list


# Any dimension whose mean score across judges falls below this triggers
# one regeneration attempt.
QUALITY_THRESHOLD = 4

# Total generation attempts, including the first one.
MAX_ATTEMPTS = 1

GATE_JUDGES = [
    ("nvidia", "meta/llama-3.3-70b-instruct", "llama-3.3-70b"),
    ("nvidia", "nvidia/nemotron-3-super-120b-a12b", "nemotron-3-super"),
]

DIMENSION_ORDER = ["grounding", "specificity", "actionability"]


def _build_feedback_text(conn: sqlite3.Connection, generation_id: int) -> tuple[str, dict]:
    """Assemble the generated feedback into the flat text the judge expects."""
    data = fetch_generation_review(conn, generation_id)
    overall = dict(data.get("overall_feedback") or {})
    criteria = [dict(c) for c in (data.get("criterion_feedback") or [])]

    parts = []
    if overall.get("overall_comment"):
        parts.append(f"Overall comment: {overall['overall_comment']}")
    strengths = parse_json_text_list(overall.get("key_strengths"))
    if strengths:
        parts.append("Key strengths:\n" + "\n".join("- " + s for s in strengths))
    improvements = parse_json_text_list(overall.get("priority_improvements"))
    if improvements:
        parts.append("Priority improvements:\n" + "\n".join("- " + s for s in improvements))
    if overall.get("overall_grade_band"):
        parts.append(f"Overall grade band: {overall['overall_grade_band']}")

    for c in criteria:
        parts.append(
            f"\nCriterion: {c.get('criterion_name', '')}\n"
            f"Strengths: {c.get('strengths', '')}\n"
            f"Areas for improvement: {c.get('areas_for_improvement', '')}\n"
            f"Improvement suggestion: {c.get('improvement_suggestion', '')}\n"
            f"Suggested level: {c.get('suggested_level', '')}"
        )

    return "\n".join(parts), data


def _load_judge_context(conn: sqlite3.Connection, generation_id: int, run: dict) -> dict:
    """Pull the same context the generator saw, so the judge is not guessing."""
    submission = conn.execute(
        "SELECT cleaned_text FROM student_submissions WHERE submission_id = ?",
        (run["submission_id"],),
    ).fetchone()

    spec = conn.execute(
        """
        SELECT cleaned_text FROM assignment_specs
        WHERE assignment_id = ?
        ORDER BY version DESC LIMIT 1
        """,
        (run["assignment_id"],),
    ).fetchone()

    criteria = conn.execute(
        """
        SELECT rc.criterion_name, rc.criterion_description
        FROM rubric_criteria rc
        JOIN criterion_feedback cf ON cf.criterion_id = rc.criterion_id
        WHERE cf.generation_id = ?
        ORDER BY rc.criterion_order
        """,
        (generation_id,),
    ).fetchall()
    rubric_text = "\n".join(
        f"- {dict(c)['criterion_name']}: {dict(c).get('criterion_description') or ''}"
        for c in criteria
    )

    materials = conn.execute(
        """
        SELECT DISTINCT m.cleaned_text
        FROM retrieval_records rr
        JOIN material_chunks mc ON mc.chunk_id = rr.chunk_id
        JOIN unit_materials m ON m.material_id = mc.material_id
        WHERE rr.generation_id = ? AND rr.used_in_prompt = 1
        LIMIT 8
        """,
        (generation_id,),
    ).fetchall()

    return {
        "submission": submission["cleaned_text"] if submission else "",
        "spec": spec["cleaned_text"] if spec else "",
        "rubric": rubric_text,
        "materials": "\n\n---\n\n".join(dict(m)["cleaned_text"] for m in materials),
    }


def _run_gate_judges(context: dict, feedback_text: str, generator_model: str) -> list[dict]:
    """Run every configured judge. Imported lazily to keep pipeline import light."""
    from llm_judge import DIMENSIONS, judge_feedback, judge_note, resolve_model_name

    results = []
    for provider, model, label in GATE_JUDGES:
        resolved_model = resolve_model_name(provider, model)
        scores = judge_feedback(
            provider,
            model,
            0.0,
            0.0,
            "strict",
            list(DIMENSIONS.keys()),
            label,
            feedback_text,
            context["submission"],
            context["spec"],
            context["rubric"],
            context["materials"],
        )
        results.append({
            "provider": provider,
            "model": resolved_model,
            "label": label,
            "note": judge_note(provider, resolved_model, generator_model),
            "scores": scores,
        })
    return results


def _coerce_judge_text(value) -> str:
    """Judges occasionally return a list where the schema asks for a string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return str(value)


def _coerce_judge_list(value) -> list:
    """Mirror image: sometimes a single string arrives where a list is expected."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _record_judge_results(
    conn: sqlite3.Connection,
    generation_id: int,
    judge_results: list[dict],
    attempt_number: int,
    accepted: bool,
) -> None:
    for judge_result in judge_results:
        for dimension, score_data in judge_result["scores"].items():
            conn.execute(
                """
                INSERT INTO judge_evaluations
                (generation_id, judge_provider, judge_model, dimension,
                 score, reason, evidence, defects_json, missing_evidence_json,
                 attempt_number, accepted, gate_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pipeline')
                """,
                (
                    generation_id,
                    judge_result["provider"],
                    judge_result["model"],
                    dimension,
                    score_data.get("score", 0),
                    _coerce_judge_text(score_data.get("reason")),
                    _coerce_judge_text(score_data.get("evidence")),
                    json.dumps(_coerce_judge_list(score_data.get("defects"))),
                    json.dumps(_coerce_judge_list(score_data.get("missing_evidence"))),
                    attempt_number,
                    1 if accepted else 0,
                ),
            )
    conn.commit()


def _aggregate_scores(judge_results: list[dict]) -> dict[str, float]:
    """Mean score per dimension across all judges.

    Averaging rather than requiring unanimity keeps a single judge's
    idiosyncratic low score from forcing an unnecessary regeneration.
    """
    aggregated = {}
    for dimension in DIMENSION_ORDER:
        values = []
        for j in judge_results:
            entry = (j.get("scores") or {}).get(dimension) or {}
            score = entry.get("score")
            if score is not None:
                values.append(score)
        aggregated[dimension] = sum(values) / len(values) if values else 0.0
    return aggregated


def _passes_threshold(judge_results: list[dict]) -> bool:
    aggregated = _aggregate_scores(judge_results)
    return all(score >= QUALITY_THRESHOLD for score in aggregated.values())


def _build_revision_notes(judge_results: list[dict]) -> str:
    """Merge every judge's diagnosis on the failing dimensions into instructions."""
    aggregated = _aggregate_scores(judge_results)
    blocks = []

    for dimension in DIMENSION_ORDER:
        if aggregated[dimension] >= QUALITY_THRESHOLD:
            continue

        lines = [f"{dimension.capitalize()} (mean {aggregated[dimension]:.1f}/5):"]
        for judge_result in judge_results:
            entry = judge_result["scores"].get(dimension)
            if not entry:
                continue
            lines.append(f"  {judge_result['label']} scored {entry.get('score')}/5.")
            if entry.get("reason"):
                lines.append(f"    Assessment: {_coerce_judge_text(entry['reason'])}")
            for defect in _coerce_judge_list(entry.get("defects")):
                lines.append(f"    Defect: {defect}")
            for missing in _coerce_judge_list(entry.get("missing_evidence")):
                lines.append(f"    Missing evidence: {missing}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def generate_feedback_with_quality_gate(
    conn: sqlite3.Connection,
    submission_id: int,
    **generation_kwargs,
) -> tuple[FeedbackGenerationResult, dict]:
    """
    Generate feedback, judge it, and regenerate once if it falls below threshold.

    Returns the accepted generation result plus a gate report describing what
    happened. The gate report is for logging and evaluation, not for educators.
    """
    gate_report = {
        "attempts": [],
        "threshold": QUALITY_THRESHOLD,
        "judge_providers": [label for _, _, label in GATE_JUDGES],
        "passed": False,
        "regenerated": False,
    }

    result = None
    revision_notes = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = generate_feedback_for_submission(
            conn,
            submission_id=submission_id,
            revision_notes=revision_notes,
            **generation_kwargs,
        )

        try:
            feedback_text, data = _build_feedback_text(conn, result.generation_id)
            context = _load_judge_context(conn, result.generation_id, dict(data["run"]))
            judge_results = _run_gate_judges(context, feedback_text, result.model)
        except Exception as err:
            # A judge failure must never block feedback reaching the educator.
            gate_report["attempts"].append(
                {"attempt": attempt, "error": str(err), "generation_id": result.generation_id}
            )
            gate_report["judge_failed"] = True
            return result, gate_report

        passed = _passes_threshold(judge_results)
        is_final = passed or attempt == MAX_ATTEMPTS
        _record_judge_results(conn, result.generation_id, judge_results, attempt, is_final)

        gate_report["attempts"].append({
            "attempt": attempt,
            "generation_id": result.generation_id,
            "mean_scores": _aggregate_scores(judge_results),
            "per_judge": {
                j["label"]: {d: j["scores"].get(d, {}).get("score") for d in DIMENSION_ORDER}
                for j in judge_results
            },
            "passed": passed,
        })

        if passed:
            gate_report["passed"] = True
            break

        if attempt < MAX_ATTEMPTS:
            revision_notes = _build_revision_notes(judge_results)
            gate_report["regenerated"] = True

    return result, gate_report
