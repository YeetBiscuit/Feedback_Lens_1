import argparse
import glob
import json
import re
from pathlib import Path
import time

from feedback_lens.feedback.llm.providers import generate_chat, list_provider_names, resolve_model_name


GROUNDING_CRITERIA = """
Grounding assesses whether the feedback is properly grounded in both the assessment context and the course context. In other words, the feedback should align with the assignment specification, rubric, and marking criteria, while also reflecting relevant course materials, taught concepts, and unit scope.

1 - Very Poor: Feedback is not meaningfully connected to the assignment specification, rubric, or course materials. It relies mainly on generic judgement, unsupported assumptions, or expectations outside the unit scope.
2 - Poor: Feedback shows limited connection to the assignment specification or rubric, but the link is vague or inconsistent. References to course materials are absent, superficial, or not clearly relevant.
3 - Adequate: Feedback is generally aligned with the assignment specification and rubric, and shows some awareness of relevant course materials. However, the connection to unit-level content may be incomplete, uneven, or not clearly used to justify the feedback.
4 - Good: Feedback is clearly grounded in both the assessment specification/rubric and relevant course materials. It evaluates the student work against the stated criteria while mostly staying within the taught unit scope.
5 - Excellent: Feedback is strongly grounded in both assessment requirements and course materials. It accurately connects rubric criteria, task expectations, and relevant unit concepts, while avoiding unsupported claims, hallucinated references, or out-of-scope expectations.
"""

SPECIFICITY_CRITERIA = """
Specificity assesses whether the feedback is tailored to the student's actual submission. High-quality feedback should identify concrete strengths, weaknesses, and performance gaps, rather than giving generic comments that could apply to any student.

1 - Very Poor: Feedback is highly generic and could apply to almost any student submission. It does not identify concrete strengths, weaknesses, or performance gaps in the student work.
2 - Poor: Feedback identifies broad strengths or weaknesses, but the comments remain vague. It gives limited indication of where issues occur or how they relate to the assessment criteria.
3 - Adequate: Feedback identifies some specific strengths, weaknesses, or gaps in the student submission. However, the explanation may be uneven, with some comments remaining generic or insufficiently connected to particular parts of the work.
4 - Good: Feedback clearly identifies concrete aspects of the student submission, including specific strengths, weaknesses, and performance gaps. It explains how these relate to the relevant rubric criteria or task expectations.
5 - Excellent: Feedback provides precise, student-specific analysis of performance. It clearly explains what the student did well, what is missing or underdeveloped, where this appears in the submission, and how it affects achievement of the assessment criteria.
"""

ACTIONABILITY_CRITERIA = """
Actionability assesses whether the feedback gives students clear and practical guidance for improvement. Good feedback should help students understand what to revise, why it matters, and how they can improve in a way that aligns with the rubric and unit expectations.

1 - Very Poor: Feedback provides little or no usable guidance for improvement. The student would not know what to do next based on the feedback.
2 - Poor: Feedback offers improvement suggestions, but they are vague or difficult to apply, such as "add more detail" or "improve clarity" without explaining how.
3 - Adequate: Feedback provides some useful suggestions for improvement, but they may be incomplete, generic, or not clearly prioritised. The student would have a partial understanding of how to improve.
4 - Good: Feedback provides clear and practical guidance that the student could realistically apply. Suggestions are connected to the identified weaknesses and mostly aligned with the assessment requirements.
5 - Excellent: Feedback provides highly concrete, prioritised, and assessment-relevant improvement steps. The student would clearly understand what to revise, why it matters, and how to improve in a way that aligns with the rubric and unit expectations.
"""

DIMENSIONS = {
    "grounding": GROUNDING_CRITERIA,
    "specificity": SPECIFICITY_CRITERIA,
    "actionability": ACTIONABILITY_CRITERIA,
}

JUDGE_SYSTEM_MESSAGE = """You are a strict academic feedback quality auditor.
Your job is to find meaningful differences in feedback quality, not to reward
polished language by default. Use the full score range when the evidence supports it."""

LEGACY_SYSTEM_MESSAGE = """You are an academic feedback quality evaluator.
Respond with valid JSON only."""

STRICT_SCORING_PROTOCOL = """
CALIBRATED SCORING PROTOCOL:
- Start from 3, then move up or down based on concrete evidence.
- A score of 5 is rare. Use 5 only when the feedback has no material weakness on this dimension.
- Do not give 5 if the feedback contains generic advice, weak evidence, missing links to the submission, unsupported grounding, or unprioritised improvement steps relevant to this dimension.
- Use 4 for clearly strong feedback that still has minor gaps or uneven coverage.
- Use 3 for acceptable feedback that is useful but partly generic, incomplete, or uneven.
- Use 2 for feedback with serious omissions, vague claims, or limited usefulness.
- Use 1 for feedback that is mostly generic, unsupported, or not useful for this dimension.
- Look for grade-limiting defects before assigning the score. If you cannot name why the feedback deserves 5, it is not a 5.
- Reward direct evidence from the student's submission, rubric, assignment specification, and retrieved unit materials only when the feedback actually uses that evidence.
"""

DEFAULT_JUDGES = [
    {
        "key": "gemini",
        "provider": "gemini",
        "model": None,
        "display_name": "Gemini",
    },
    {
        "key": "qwen",
        "provider": "qwen",
        "model": None,
        "display_name": "Qwen",
    },
]

JUDGE_TEMPERATURE = 0.2
JUDGE_CALL_DELAY_SECONDS = 4
DEFAULT_INPUT_FILES = [
    "exports/generation_run_6_full_planner.json",
    "exports/generation_run_9_full_planner.json",
    "exports/generation_run_10_full_planner.json",
]
DEFAULT_OUTPUT_FILE = "exports/llm_judge_results.json"


def load_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def truncate_text(text, limit):
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def parse_json_response(raw):
    clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start >= 0 and end > start:
            return json.loads(clean[start:end + 1])
        raise

def salvage_dimension_fields(raw: str, dimension: str) -> dict:
    """Last-resort extraction when a judge returns malformed JSON.

    Models occasionally emit unescaped quotes inside string values, which
    breaks strict parsing. Pull the fields out with regexes rather than
    discard an otherwise usable judgement.
    """
    score_match = re.search(r'"score"\s*:\s*(\d+)', raw)
    if not score_match:
        raise ValueError(f"No recoverable score in judge response for {dimension}.")

    result = {"dimension": dimension, "score": int(score_match.group(1))}

    for field in ("reason", "evidence"):
        match = re.search(
            rf'"{field}"\s*:\s*"(.*?)"\s*,\s*"(?:score|reason|evidence|defects|missing_evidence)"',
            raw,
            re.DOTALL,
        )
        result[field] = match.group(1).strip() if match else ""

    for field in ("defects", "missing_evidence"):
        match = re.search(rf'"{field}"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
        if match:
            items = re.findall(r'"(.*?)"', match.group(1), re.DOTALL)
            result[field] = [i.strip() for i in items if i.strip()]
        else:
            result[field] = []

    return result


def normalize_dimension_result(result, dimension):
    if not isinstance(result, dict):
        raise ValueError(f"Judge result for {dimension} must be a JSON object.")
    score = int(result.get("score"))
    if score < 1 or score > 5:
        raise ValueError(f"Judge score for {dimension} must be between 1 and 5, got {score}.")
    normalized = dict(result)
    normalized["dimension"] = normalized.get("dimension") or dimension
    normalized["score"] = score
    normalized.setdefault("reason", "")
    normalized.setdefault("evidence", "")
    normalized.setdefault("defects", [])
    normalized.setdefault("missing_evidence", [])
    return normalized


def judge_system_message(scoring_mode):
    return JUDGE_SYSTEM_MESSAGE if scoring_mode == "strict" else LEGACY_SYSTEM_MESSAGE


def expand_input_files(input_groups):
    if not input_groups:
        return list(DEFAULT_INPUT_FILES)

    files = []
    seen = set()
    for group in input_groups:
        for pattern in group:
            matches = sorted(glob.glob(pattern))
            candidates = matches or [pattern]
            for candidate in candidates:
                if candidate not in seen:
                    seen.add(candidate)
                    files.append(candidate)
    return files


def judge_key(provider, model):
    key = provider if model is None else f"{provider}_{model}"
    return key.replace("/", "_").replace(":", "_").replace(".", "_").replace("-", "_")


def parse_judge_spec(spec):
    provider, separator, model = spec.partition(":")
    provider = provider.strip().lower()
    model = model.strip() if separator else None
    if not provider:
        raise ValueError("Judge spec must include a provider name.")
    available_providers = list_provider_names()
    if provider not in available_providers:
        raise ValueError(
            f"Unsupported judge provider '{provider}'. Available providers: {', '.join(available_providers)}"
        )
    return {
        "key": judge_key(provider, model),
        "provider": provider,
        "model": model or None,
        "display_name": provider if not model else f"{provider}:{model}",
    }


def build_judge_configs(args, parser):
    if args.judge and args.provider:
        parser.error("Use either --judge for one or more judges, or --provider/--model for a single judge.")
    if args.judge:
        try:
            return [parse_judge_spec(spec) for spec in args.judge]
        except ValueError as exc:
            parser.error(str(exc))
    if args.provider:
        return [
            {
                "key": judge_key(args.provider, args.model),
                "provider": args.provider,
                "model": args.model,
                "display_name": args.provider if not args.model else f"{args.provider}:{args.model}",
            }
        ]
    if args.model:
        parser.error("--model requires --provider unless you use --judge provider:model.")
    return list(DEFAULT_JUDGES)


def normalize_synthetic_selection(args, parser):
    if args.no_synthetic:
        return []
    values = args.synthetic or ["all"]
    if "none" in values and len(values) > 1:
        parser.error("--synthetic none cannot be combined with other synthetic choices.")
    if "none" in values:
        return []

    tier_expand = {
        "all": [
            "low_1", "low_2", "low_3",
            "medium_1", "medium_2", "medium_3", "medium_4",
            "high_1", "high_2", "high_3",
        ],
        "low": ["low_1", "low_2", "low_3"],
        "medium": ["medium_1", "medium_2", "medium_3", "medium_4"],
        "high": ["high_1", "high_2", "high_3"],
    }

    selected = []
    for value in values:
        expanded = tier_expand.get(value, [value])
        for key in expanded:
            if key not in selected:
                selected.append(key)
    return selected


def extract_context(data):
    prompt_text = data["generation_run"]["prompt_text"]

    try:
        submission_text = prompt_text.split("Student submission text:")[-1].strip()[:3000]
    except Exception:
        submission_text = ""

    try:
        assignment_spec = prompt_text.split("Assignment specification text:")[-1].split("Student submission text:")[0].strip()[:2000]
    except Exception:
        assignment_spec = ""

    try:
        rubric_text = prompt_text.split("Rubric criteria:")[-1].split("Retrieved course context")[0].strip()[:2000]
    except Exception:
        rubric_text = ""

    try:
        retrieved_context = ""
        records = data.get("retrieval_records", [])
        used = [r for r in records if r.get("used_in_prompt") == 1]
        seen = set()
        for r in used:
            chunk_id = r.get("chunk_id")
            if chunk_id not in seen:
                seen.add(chunk_id)
                title = r.get("material_title", "")
                material_type = r.get("material_type", "")
                chunk_text = r.get("chunk_text", "")[:500]
                retrieved_context += f"\n[{material_type}] {title}:\n{chunk_text}\n"
        retrieved_context = retrieved_context.strip()[:3000]
    except Exception:
        retrieved_context = ""

    return submission_text, assignment_spec, rubric_text, retrieved_context


def build_feedback_text(data):
    overall = data["overall_feedback"]
    overall_text = f"""Overall comment: {overall["overall_comment"]}
Key strengths:
{chr(10).join("- " + s for s in overall["key_strengths"])}
Priority improvements:
{chr(10).join("- " + s for s in overall["priority_improvements"])}
Overall grade band: {overall["overall_grade_band"]}"""

    criterion_text = ""
    for c in data["criterion_feedback"]:
        criterion_text += f"""
Criterion: {c["criterion_name"]}
Strengths: {c["strengths"]}
Areas for improvement: {c["areas_for_improvement"]}
Improvement suggestion: {c["improvement_suggestion"]}
Suggested level: {c["suggested_level"]}
"""
    return overall_text + criterion_text


def build_dimension_prompt(
    dimension,
    criteria,
    feedback_text,
    submission_text,
    assignment_spec,
    rubric_text,
    retrieved_context,
    scoring_mode="strict",
):
    scoring_protocol = STRICT_SCORING_PROTOCOL if scoring_mode == "strict" else ""
    return f"""You are an academic feedback quality evaluator. Your task is to evaluate ONLY the {dimension.upper()} dimension of the AI-generated feedback below.

{dimension.upper()} SCORING CRITERIA:
{criteria}

{scoring_protocol}

ASSIGNMENT SPECIFICATION:
{assignment_spec}

RUBRIC CRITERIA:
{rubric_text}

RETRIEVED COURSE MATERIALS (used when generating the feedback):
{retrieved_context}

STUDENT SUBMISSION:
{submission_text}

AI-GENERATED FEEDBACK TO EVALUATE:
{feedback_text}

Evaluate only the {dimension} dimension. Do not comment on other dimensions.

Respond in this exact JSON format with no markdown fences:
{{
  "dimension": "{dimension}",
  "score": 0,
  "reason": "",
  "evidence": "",
  "defects": [],
  "missing_evidence": []
}}

Where:
- score: integer from 1 to 5
- reason: explanation of why you gave this score, referencing specific parts of the feedback
- evidence: specific quotes or examples from the feedback that support your score
- defects: concrete weaknesses that prevented a higher score; use an empty list only for a genuine 5
- missing_evidence: evidence the feedback should have used but did not use for this dimension"""


def judge_note(provider, resolved_model, evaluated_model):
    evaluated = str(evaluated_model or "").lower()
    provider_key = str(provider or "").lower()
    model_key = str(resolved_model or "").lower()
    if evaluated == "human_written":
        return "synthetic baseline evaluation"
    if model_key and model_key in evaluated:
        return "same model evaluation - potential bias"
    if provider_key and provider_key in evaluated:
        return "same provider evaluation - potential bias"
    return "cross-provider evaluation"


def run_dimension_judge(
    provider,
    model,
    temperature,
    call_delay,
    scoring_mode,
    dimension,
    criteria,
    feedback_text,
    submission_text,
    assignment_spec,
    rubric_text,
    retrieved_context,
):
    prompt = build_dimension_prompt(
        dimension,
        criteria,
        feedback_text,
        submission_text,
        assignment_spec,
        rubric_text,
        retrieved_context,
        scoring_mode=scoring_mode,
    )
    last_error = None
    for attempt in range(3):
        if call_delay > 0:
            time.sleep(call_delay)
        raw = generate_chat(
            [
                {"role": "system", "content": judge_system_message(scoring_mode)},
                {"role": "user", "content": prompt},
            ],
            provider=provider,
            model=model,
            temperature=temperature,
        )
        try:
            return normalize_dimension_result(parse_json_response(raw), dimension)
        except (json.JSONDecodeError, ValueError) as err:
            last_error = err
        try:
            return normalize_dimension_result(
                salvage_dimension_fields(raw, dimension), dimension
            )
        except ValueError:
            pass
        print(f"    (malformed response, retry {attempt + 1}/3)")

    raise ValueError(
        f"Judge returned unusable JSON for {dimension} after 3 attempts: {last_error}"
    )


def judge_feedback(
    provider,
    model,
    temperature,
    call_delay,
    scoring_mode,
    dimensions,
    judge_name,
    feedback_text,
    submission_text,
    assignment_spec,
    rubric_text,
    retrieved_context,
):
    print(f"  Running {judge_name}...")
    scores = {}
    for dimension in dimensions:
        criteria = DIMENSIONS[dimension]
        print(f"    Evaluating {dimension}...")
        try:
            result = run_dimension_judge(
                provider,
                model,
                temperature,
                call_delay,
                scoring_mode,
                dimension,
                criteria,
                feedback_text,
                submission_text,
                assignment_spec,
                rubric_text,
                retrieved_context,
            )
        except Exception as err:
            # One bad response should not discard a 60-call study.
            print(f"    {dimension}: FAILED ({err})")
            scores[dimension] = {
                "dimension": dimension,
                "score": None,
                "reason": f"judge call failed: {err}",
                "evidence": "",
                "defects": [],
                "missing_evidence": [],
                "failed": True,
            }
            continue
        scores[dimension] = result
        
        print(f"    {dimension}: {result['score']}/5")
    return scores


def comparison_candidate_id(index):
    if index < 26:
        return chr(ord("A") + index)
    return f"CANDIDATE_{index + 1}"


def build_comparison_prompt(
    dimension,
    criteria,
    candidates,
    submission_text,
    assignment_spec,
    rubric_text,
    retrieved_context,
    scoring_mode="strict",
):
    scoring_protocol = STRICT_SCORING_PROTOCOL if scoring_mode == "strict" else ""
    candidate_blocks = []
    for candidate in candidates:
        context_block = ""
        if candidate.get("submission_text") or candidate.get("assignment_spec") or candidate.get("rubric_text"):
            context_block = f"""
Candidate-specific assignment specification:
{truncate_text(candidate.get('assignment_spec', ''), 1500)}
Candidate-specific rubric criteria:
{truncate_text(candidate.get('rubric_text', ''), 1500)}
Candidate-specific retrieved course materials:
{truncate_text(candidate.get('retrieved_context', ''), 1500)}
Candidate-specific student submission:
{truncate_text(candidate.get('submission_text', ''), 2500)}
"""
        candidate_blocks.append(
            f"""CANDIDATE {candidate['candidate_id']}
{context_block}
Feedback:
{truncate_text(candidate['feedback_text'], 6000)}"""
        )

    return f"""You are comparing multiple AI-generated feedback outputs.
Evaluate ONLY the {dimension.upper()} dimension.

Your task is comparative, not generous absolute grading. Identify meaningful differences between candidates and rank them.
Avoid ties unless the candidates are genuinely indistinguishable after examining concrete evidence.
This is a blind comparison. You are not given source filenames, model names, pipeline names, generation strategies, or prior grade bands. Do not infer or speculate about them.
If candidates come from different submissions, judge each candidate against its own candidate-specific context and mention comparability limits in tie_notes or pairwise_differences.

{dimension.upper()} SCORING CRITERIA:
{criteria}

{scoring_protocol}

ASSIGNMENT SPECIFICATION:
{assignment_spec}

RUBRIC CRITERIA:
{rubric_text}

SHARED RETRIEVED COURSE MATERIALS:
{retrieved_context}

SHARED STUDENT SUBMISSION:
{submission_text}

AI-GENERATED FEEDBACK CANDIDATES:
{chr(10).join(candidate_blocks)}

Respond in this exact JSON format with no markdown fences:
{{
  "dimension": "{dimension}",
  "winner": "",
  "ranking": [
    {{
      "candidate_id": "A",
      "rank": 1,
      "relative_score": 0,
      "reason": "",
      "decisive_evidence": "",
      "defects": []
    }}
  ],
  "pairwise_differences": [],
  "tie_notes": ""
}}

Where:
- winner: candidate_id of the best candidate, or "tie" only if no meaningful distinction exists
- ranking: one entry per candidate; rank 1 is best
- relative_score: integer from 1 to 5 using the calibrated scoring protocol
- pairwise_differences: concrete differences that explain the ranking"""


def run_comparison_dimension_judge(
    provider,
    model,
    temperature,
    call_delay,
    scoring_mode,
    dimension,
    criteria,
    candidates,
    submission_text,
    assignment_spec,
    rubric_text,
    retrieved_context,
):
    prompt = build_comparison_prompt(
        dimension,
        criteria,
        candidates,
        submission_text,
        assignment_spec,
        rubric_text,
        retrieved_context,
        scoring_mode=scoring_mode,
    )
    if call_delay > 0:
        time.sleep(call_delay)
    raw = generate_chat(
        [
            {"role": "system", "content": judge_system_message(scoring_mode)},
            {"role": "user", "content": prompt},
        ],
        provider=provider,
        model=model,
        temperature=temperature,
    )
    result = parse_json_response(raw)
    if not isinstance(result, dict):
        raise ValueError(f"Comparison result for {dimension} must be a JSON object.")
    result["dimension"] = result.get("dimension") or dimension
    return result


def comparable_group_key(data):
    run = data.get("generation_run", {})
    return (run.get("assignment_id"), run.get("student_identifier"))


def run_comparisons(
    input_files,
    judge_configs,
    dimensions,
    temperature=JUDGE_TEMPERATURE,
    call_delay=JUDGE_CALL_DELAY_SECONDS,
    scoring_mode="strict",
    explicit_files=None,
):
    grouped = {}
    if explicit_files:
        members = [(input_file, load_json_file(input_file)) for input_file in explicit_files]
        grouped[("explicit_pair", "chosen_files")] = members
    else:
        for input_file in input_files:
            data = load_json_file(input_file)
            grouped.setdefault(comparable_group_key(data), []).append((input_file, data))

    comparisons = []
    for (assignment_id, student_identifier), members in grouped.items():
        if len(members) < 2:
            continue

        print(f"\nComparing {len(members)} candidates for student={student_identifier}, assignment={assignment_id}")
        first_data = members[0][1]
        submission_text, assignment_spec, rubric_text, retrieved_context = extract_context(first_data)
        candidates = []
        for index, (input_file, data) in enumerate(members):
            run = data["generation_run"]
            candidate = {
                "candidate_id": comparison_candidate_id(index),
                "input_file": input_file,
                "evaluated_model": run.get("llm_model"),
                "pipeline_version": run.get("pipeline_version"),
                "ai_grade_band": data.get("overall_feedback", {}).get("overall_grade_band"),
                "feedback_text": build_feedback_text(data),
            }
            if explicit_files:
                (
                    candidate["submission_text"],
                    candidate["assignment_spec"],
                    candidate["rubric_text"],
                    candidate["retrieved_context"],
                ) = extract_context(data)
            candidates.append(candidate)

        comparison_result = {
            "assignment_id": assignment_id,
            "student_identifier": student_identifier,
            "candidates": [
                {
                    "candidate_id": c["candidate_id"],
                    "input_file": c["input_file"],
                    "evaluated_model": c["evaluated_model"],
                    "pipeline_version": c["pipeline_version"],
                    "ai_grade_band": c["ai_grade_band"],
                }
                for c in candidates
            ],
            "judges": {},
        }

        for judge in judge_configs:
            provider = judge["provider"]
            model = judge.get("model")
            resolved_model = resolve_model_name(provider, model)
            display_name = judge.get("display_name") or provider
            key = judge.get("key") or provider
            print(f"  {display_name} comparative judge ({provider}:{resolved_model})...")
            dimension_results = {}
            for dimension in dimensions:
                print(f"    Comparing {dimension}...")
                dimension_results[dimension] = run_comparison_dimension_judge(
                    provider,
                    model,
                    temperature,
                    call_delay,
                    scoring_mode,
                    dimension,
                    DIMENSIONS[dimension],
                    candidates,
                    submission_text,
                    assignment_spec,
                    rubric_text,
                    retrieved_context,
                )
                print(f"    {dimension} winner: {dimension_results[dimension].get('winner', '-')}")

            comparison_result["judges"][key] = {
                "provider": provider,
                "model": resolved_model,
                "scores": dimension_results,
            }

        comparisons.append(comparison_result)

    if not comparisons:
        print("\nNo comparable groups found for --compare. Need at least two real input files with the same assignment_id and student_identifier, or pass exactly two files after --compare.")
    return comparisons


def process_file(
    input_file,
    judge_configs,
    dimensions,
    temperature=JUDGE_TEMPERATURE,
    call_delay=JUDGE_CALL_DELAY_SECONDS,
    scoring_mode="strict",
):
    print(f"\nProcessing: {input_file}")
    data = load_json_file(input_file)

    student_id = data["generation_run"]["student_identifier"]
    evaluated_model = data["generation_run"]["llm_model"]
    pipeline = data["generation_run"]["pipeline_version"]
    grade_band = data["overall_feedback"]["overall_grade_band"]

    print(f"Student: {student_id}")
    print(f"AI grade: {grade_band}")

    feedback_text = build_feedback_text(data)
    submission_text, assignment_spec, rubric_text, retrieved_context = extract_context(data)

    result = {
        "input_file": input_file,
        "student_identifier": student_id,
        "evaluated_model": evaluated_model,
        "pipeline_version": pipeline,
        "ai_grade_band": grade_band,
        "judges": {}
    }

    for judge in judge_configs:
        provider = judge["provider"]
        model = judge.get("model")
        resolved_model = resolve_model_name(provider, model)
        display_name = judge.get("display_name") or provider
        key = judge.get("key") or provider
        print(f"\n{display_name} judge ({provider}:{resolved_model}):")
        result["judges"][key] = {
            "provider": provider,
            "model": resolved_model,
            "note": judge_note(provider, resolved_model, evaluated_model),
            "scores": judge_feedback(
                provider,
                model,
                temperature,
                call_delay,
                scoring_mode,
                dimensions,
                display_name,
                feedback_text,
                submission_text,
                assignment_spec,
                rubric_text,
                retrieved_context,
            ),
        }

    return result


def print_summary(all_results, dimensions):
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    header = f"{'File':<25} {'AI Grade':<10} {'Judge':<10}"
    for dim in dimensions:
        header += f" {dim[:8]:>9}"
    print(header)
    print("-" * 80)

    for r in all_results:
        fname = Path(r["input_file"]).name.replace("generation_run_", "run_").replace("_full_planner.json", "")
        if fname.startswith("synthetic_"):
            fname = fname
        grade = r["ai_grade_band"]
        for judge_name, judge_data in r["judges"].items():
            row = f"{fname:<25} {grade:<10} {judge_name:<10}"
            for dim in dimensions:
                score = judge_data["scores"].get(dim, {}).get("score", "-")
                row += f" {str(score):>9}"
            print(row)

    print("\nNote: Same-model or same-provider judging may show self-evaluation bias.")
    print("Cross-provider scores are generally more useful for independent comparison.")


def print_comparison_summary(comparisons, dimensions):
    if not comparisons:
        return
    print("\n" + "=" * 80)
    print("COMPARATIVE SUMMARY")
    print("=" * 80)
    header = f"{'Student':<28} {'Judge':<10}"
    for dim in dimensions:
        header += f" {dim[:8]:>12}"
    print(header)
    print("-" * 80)
    for comparison in comparisons:
        student = str(comparison["student_identifier"])[:28]
        for judge_name, judge_data in comparison["judges"].items():
            row = f"{student:<28} {judge_name:<10}"
            for dim in dimensions:
                result = judge_data["scores"].get(dim, {})
                row += f" {str(result.get('winner', '-')):>12}"
            print(row)
        for candidate in comparison["candidates"]:
            print(
                f"  {candidate['candidate_id']}: {Path(candidate['input_file']).name} "
                f"({candidate['evaluated_model']}, {candidate['pipeline_version']}, {candidate['ai_grade_band']})"
            )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run LLM-as-judge evaluation over exported feedback generation runs.",
    )
    parser.add_argument(
        "--input",
        "--inputs",
        dest="input_files",
        action="append",
        nargs="+",
        metavar="PATH_OR_GLOB",
        help=(
            "Feedback export JSON file(s) to judge. Can be repeated and accepts glob patterns. "
            "Defaults to the three hard-coded export files used by the original script."
        ),
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT_FILE)
    parser.add_argument(
        "--judge",
        action="append",
        metavar="PROVIDER[:MODEL]",
        help=(
            "Judge provider to use. Repeat for multiple judges. "
            "Examples: --judge qwen --judge gemini --judge nvidia:nvidia/nemotron-3-super-120b-a12b"
        ),
    )
    parser.add_argument(
        "--provider",
        choices=list_provider_names(),
        help="Single judge provider, matching the style used by the other CLI tools.",
    )
    parser.add_argument("--model", help="Model to use with --provider.")
    parser.add_argument(
        "--dimensions",
        nargs="+",
        choices=list(DIMENSIONS),
        default=list(DIMENSIONS),
        help="Feedback quality dimensions to judge.",
    )
    parser.add_argument(
        "--synthetic",
        nargs="+",
        choices=[
            "all", "low", "medium", "high", "none",
            "low_1", "low_2", "low_3",
            "medium_1", "medium_2", "medium_3", "medium_4",
            "high_1", "high_2", "high_3",
        ],
        default=["all"],
        help="Synthetic baseline feedback to judge. Use 'none' to skip synthetic baselines.",
    )
    parser.add_argument(
        "--no-synthetic",
        action="store_true",
        help="Skip synthetic baseline feedback. Equivalent to --synthetic none.",
    )
    parser.add_argument(
        "--no-real",
        action="store_true",
        help="Skip exported generation-run files and judge only selected synthetic baselines.",
    )
    parser.add_argument(
        "--reference-file",
        help=(
            "Generation export JSON used to provide assignment, rubric, submission, and retrieval "
            "context for synthetic baseline judging. Defaults to the first selected input file."
        ),
    )
    parser.add_argument("--temperature", type=float, default=JUDGE_TEMPERATURE)
    parser.add_argument(
        "--call-delay",
        type=float,
        default=JUDGE_CALL_DELAY_SECONDS,
        help="Seconds to wait before each judge call. Use 0 for no local delay.",
    )
    parser.add_argument(
        "--scoring-mode",
        choices=["strict", "legacy"],
        default="strict",
        help=(
            "strict uses calibrated, defect-seeking scoring to reduce all-5 inflation; "
            "legacy uses the original simpler rubric prompt."
        ),
    )
    parser.add_argument(
        "--compare",
        nargs="*",
        metavar="FILE",
        help=(
            "Add comparative ranking. With no files, compare groups from --input that share "
            "assignment_id and student_identifier. With two files, compare exactly those files."
        ),
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.call_delay < 0:
        parser.error("--call-delay cannot be negative.")
    if args.compare is not None and len(args.compare) not in (0, 2):
        parser.error("--compare takes either no file arguments or exactly two file paths.")

    explicit_compare_files = args.compare if args.compare else []
    if explicit_compare_files and args.no_real:
        parser.error("--compare FILE_A FILE_B cannot be combined with --no-real.")
    if explicit_compare_files:
        input_files = list(explicit_compare_files)
    elif args.no_real:
        input_files = []
    else:
        input_files = expand_input_files(args.input_files)
    synthetic_labels = normalize_synthetic_selection(args, parser)
    if not input_files and not synthetic_labels:
        parser.error("Nothing to judge. Provide --input files, --compare FILE_A FILE_B, or enable synthetic baselines.")

    judge_configs = build_judge_configs(args, parser)
    output_file = args.output

    all_results = []
    for f in input_files:
        result = process_file(
            f,
            judge_configs,
            args.dimensions,
            temperature=args.temperature,
            call_delay=args.call_delay,
            scoring_mode=args.scoring_mode,
        )
        all_results.append(result)

    if synthetic_labels:
        print("\n" + "="*50)
        print("TESTING WITH SYNTHETIC FEEDBACK")
        print("="*50)

        reference_file = args.reference_file or (input_files[0] if input_files else DEFAULT_INPUT_FILES[0])
        ref_data = load_json_file(reference_file)
        submission_text, assignment_spec, rubric_text, retrieved_context = extract_context(ref_data)

        synthetic_feedback = SYNTHETIC_BASELINES

        for label in synthetic_labels:
            fake_feedback = synthetic_feedback[label]
            print(f"\nTesting synthetic '{label}' feedback...")
            synthetic_result = {
                "input_file": f"synthetic_{label}",
                "student_identifier": f"synthetic_{label}",
                "evaluated_model": "human_written",
                "pipeline_version": "synthetic_test",
                "ai_grade_band": "N/A",
                "judges": {}
            }
            for judge in judge_configs:
                provider = judge["provider"]
                model = judge.get("model")
                resolved_model = resolve_model_name(provider, model)
                display_name = judge.get("display_name") or provider
                key = judge.get("key") or provider
                print(f"  {display_name} judge ({provider}:{resolved_model})...")
                synthetic_result["judges"][key] = {
                    "provider": provider,
                    "model": resolved_model,
                    "note": judge_note(provider, resolved_model, "human_written"),
                    "scores": judge_feedback(
                        provider,
                        model,
                        args.temperature,
                        args.call_delay,
                        args.scoring_mode,
                        args.dimensions,
                        display_name,
                        fake_feedback,
                        submission_text,
                        assignment_spec,
                        rubric_text,
                        retrieved_context,
                    ),
                }
            all_results.append(synthetic_result)

    comparisons = []
    if args.compare is not None:
        comparisons = run_comparisons(
            input_files,
            judge_configs,
            args.dimensions,
            temperature=args.temperature,
            call_delay=args.call_delay,
            scoring_mode=args.scoring_mode,
            explicit_files=explicit_compare_files,
        )

    output_payload = (
        {"absolute_results": all_results, "comparisons": comparisons}
        if args.compare is not None
        else all_results
    )
    save_json_file(output_file, output_payload)
    print(f"\nResults saved to {output_file}")

    print_summary(all_results, args.dimensions)
    print_comparison_summary(comparisons, args.dimensions)


LOW_QUALITY_FEEDBACK_1 = """Overall comment: The student did an okay job on this assignment. There are some areas that could be improved. Overall a decent submission.
Key strengths:
- Good effort
- Completed the assignment
Priority improvements:
- Could be better
- Needs more detail
Overall grade band: C

Criterion: Context and Methodological Framework
Strengths: The student described the app.
Areas for improvement: Could add more context.
Improvement suggestion: Add more detail.
Suggested level: C

Criterion: Usability Issue Analysis and Evidence
Strengths: Issues were identified.
Areas for improvement: The analysis could be deeper.
Improvement suggestion: Be more specific.
Suggested level: C

Criterion: Design Recommendations and Theory Application
Strengths: Recommendations were provided.
Areas for improvement: Link to theory better.
Improvement suggestion: Use more theory.
Suggested level: C

Criterion: Academic Structure and Referencing
Strengths: The report has structure.
Areas for improvement: References could be better.
Improvement suggestion: Fix references.
Suggested level: C"""

LOW_QUALITY_FEEDBACK_2 = """Overall comment: The student wrote too much and included too many things. The report would be stronger if it were shorter and simpler. Removing extra sections and focusing on fewer issues will make it more professional.
Key strengths:
- Report is not too long
- Some issues are described
Priority improvements:
- Cut down to three issues instead of five
- Remove the appendices to make the report cleaner
- Delete the references to Week 2 cognition since it distracts from usability
Overall grade band: P

Criterion: Context and Methodological Framework
Strengths: The student picked a food app which is a common choice.
Areas for improvement: The introduction is too focused on ISO 9241 and this makes it feel textbook. Drop the ISO reference.
Improvement suggestion: Remove the paragraph on ISO 9241 and just say the app is being evaluated. This will simplify the introduction.
Suggested level: P

Criterion: Usability Issue Analysis and Evidence
Strengths: The student identified some issues.
Areas for improvement: Five issues is too many and the report becomes repetitive. Reduce to the three most important ones.
Improvement suggestion: Delete two of the weaker issues (like the search bar and button styles) so the report focuses on stronger issues only.
Suggested level: P

Criterion: Design Recommendations and Theory Application
Strengths: There are recommendations at the end.
Areas for improvement: The recommendations reference cognitive load and mental models which is not needed for this task. Take those references out.
Improvement suggestion: Rewrite each recommendation as a single sentence without any theory citations. Keep it practical only.
Suggested level: P

Criterion: Academic Structure and Referencing
Strengths: Report has headings.
Areas for improvement: The reference list has too many entries.
Improvement suggestion: Remove the ISO and Shneiderman references and keep only Nielsen.
Suggested level: P"""

LOW_QUALITY_FEEDBACK_3 = """Overall comment: The submission shows reasonable technical implementation. Database queries appear well structured and the user interface follows standard design patterns. Test coverage could be improved and the software architecture should be documented more clearly.
Key strengths:
- Implementation is functional
- Code appears to run
Priority improvements:
- Improve test coverage using unit tests
- Add architecture diagrams
- Consider using a design pattern such as MVC or repository pattern
Overall grade band: C

Criterion: Context and Methodological Framework
Strengths: The context of the problem is stated.
Areas for improvement: The requirements engineering section could benefit from user stories written in the standard "As a user, I want..." format.
Improvement suggestion: Add a section on non-functional requirements and quality attributes such as scalability and maintainability.
Suggested level: C

Criterion: Usability Issue Analysis and Evidence
Strengths: The report identifies several concerns.
Areas for improvement: The analysis lacks quantitative metrics such as time-on-task or completion rates.
Improvement suggestion: Include performance benchmarks and A/B testing results to strengthen the empirical basis.
Suggested level: C

Criterion: Design Recommendations and Theory Application
Strengths: Some recommendations are provided.
Areas for improvement: The recommendations should be evaluated against agile prioritisation frameworks such as MoSCoW.
Improvement suggestion: Prioritise the recommendations using RICE scoring or WSJF and present them in a product backlog format.
Suggested level: C

Criterion: Academic Structure and Referencing
Strengths: The report has sections.
Areas for improvement: The report should follow IEEE referencing style consistent with software engineering conventions.
Improvement suggestion: Convert all citations to IEEE format and add a systematic literature review section.
Suggested level: C"""


MEDIUM_QUALITY_FEEDBACK_1 = """Overall comment: The student demonstrated adequate understanding of heuristic evaluation. Five usability issues were identified in the QuickEats app with severity ratings. Some recommendations were provided but lack theoretical depth.
Key strengths:
- Five issues identified and mapped to heuristics
- Severity ratings provided with basic justification
Priority improvements:
- Deepen connection to course theory
- Improve specificity of recommendations
Overall grade band: C

Criterion: Context and Methodological Framework
Strengths: The QuickEats app context and target audience were described. Nielsen heuristics stated as framework.
Areas for improvement: Primary tasks not explicitly listed as required by methodology section.
Improvement suggestion: Explicitly define three primary tasks before evaluation.
Suggested level: C

Criterion: Usability Issue Analysis and Evidence
Strengths: Five issues identified with heuristic mappings and severity ratings.
Areas for improvement: Issue 1 mapping to Visibility of System Status is inaccurate.
Improvement suggestion: Remap Issue 1 to Recognition rather than Recall.
Suggested level: C

Criterion: Design Recommendations and Theory Application
Strengths: Each issue has a corresponding recommendation.
Areas for improvement: Cognitive theory connections are surface level.
Improvement suggestion: Specify how recommendations reduce cognitive load.
Suggested level: C

Criterion: Academic Structure and Referencing
Strengths: Report follows required structure.
Areas for improvement: Some APA formatting errors present.
Improvement suggestion: Review APA guidelines for web sources.
Suggested level: C"""

MEDIUM_QUALITY_FEEDBACK_2 = """Overall comment: The report addresses the assignment requirements. Context is described, heuristics are applied, and recommendations are provided. The academic structure follows the expected format. Further refinement of the analysis and theory application would strengthen the submission.
Key strengths:
- The report follows the required structure
- Heuristic framework is applied
- Recommendations are included for each issue
Priority improvements:
- Strengthen the methodological framing
- Deepen the theory application
- Improve the depth of usability analysis
Overall grade band: C

Criterion: Context and Methodological Framework
Strengths: The interface context is described and the heuristic framework is stated.
Areas for improvement: The contextual alignment with the heuristic framework could be more developed.
Improvement suggestion: Expand the methodology to strengthen the link between the target audience context and the evaluation framework.
Suggested level: C

Criterion: Usability Issue Analysis and Evidence
Strengths: Multiple issues are identified with heuristic mappings and severity ratings.
Areas for improvement: The justification of severity ratings and heuristic mappings could be more rigorous.
Improvement suggestion: Provide stronger justification for each heuristic mapping and severity rating decision.
Suggested level: C

Criterion: Design Recommendations and Theory Application
Strengths: Recommendations are provided for the identified issues.
Areas for improvement: The connection to usability theory could be deeper.
Improvement suggestion: Ground each recommendation more explicitly in relevant usability theory to demonstrate deeper theoretical understanding.
Suggested level: C

Criterion: Academic Structure and Referencing
Strengths: Structure and referencing follow the required conventions.
Areas for improvement: Minor consistency issues may be present.
Improvement suggestion: Review the report for any minor formatting inconsistencies before final submission.
Suggested level: C"""

MEDIUM_QUALITY_FEEDBACK_3 = """Overall comment: The QuickEats evaluation identifies five usability issues in the app and provides recommendations for each. The Search Bar Visibility, Unclear Error Messages, and No Confirmation Before Order issues are the strongest parts of the analysis. Severity ratings are reasonable. Some heuristic mappings should be reconsidered.
Key strengths:
- Five issues identified across search, error handling, checkout, consistency, and confirmation
- Severity ratings from 2 to 4 are reasonably justified
- Recommendations directly address each identified issue
Priority improvements:
- Reconsider the Issue 1 heuristic mapping
- Provide more detail on the Issue 3 checkout flow analysis
- Strengthen the justification for the Severity 4 rating on Issue 5
Overall grade band: C

Criterion: Context and Methodological Framework
Strengths: The QuickEats app is described as a food delivery service for young adults and busy professionals in distracted contexts. Nielsen's Ten Heuristics is stated as the framework.
Areas for improvement: The three primary tasks (search, add to cart, checkout) are mentioned in the methodology but not framed as a structured list up front.
Improvement suggestion: Present the three tasks as a numbered list in the introduction so the evaluation scope is clear before the findings begin.
Suggested level: C

Criterion: Usability Issue Analysis and Evidence
Strengths: Issue 2 (unclear error message on the payment screen) and Issue 5 (no confirmation before order) are well-described with clear justification for severity.
Areas for improvement: Issue 1 mapping to Visibility of System Status is arguably incorrect because the problem is about element placement rather than system state feedback.
Improvement suggestion: Consider remapping Issue 1 to a heuristic that better fits hidden interface elements. The mapping should match the nature of the violation.
Suggested level: C

Criterion: Design Recommendations and Theory Application
Strengths: The recommendation for Issue 2 (move error message next to the field and name the invalid field) is specific and directly resolves the violation.
Areas for improvement: The connections to human cognition are stated in passing but not developed. The mention of cognitive load in the Issue 2 recommendation is not explained.
Improvement suggestion: When cognitive load is mentioned, briefly explain which type of load is reduced and why the design change reduces it.
Suggested level: C

Criterion: Academic Structure and Referencing
Strengths: The report includes all required sections and uses APA referencing consistently.
Areas for improvement: The Evans (2023) lecture note reference is not the standard way to cite unit content.
Improvement suggestion: Cite specific lecture slides or reading pack items instead of a general lecture notes reference.
Suggested level: C"""

MEDIUM_QUALITY_FEEDBACK_4 = """Overall comment: The heuristic evaluation report meets the basic requirements of the task. All required sections are present, the framework is stated, five issues are identified with severity ratings, and recommendations follow. There is room for improvement across all criteria.
Key strengths:
- Complete report structure
- Five issues identified
- Recommendations for each issue
- APA references included
Priority improvements:
- Improve the context section
- Improve the issue analysis
- Improve the recommendations
- Improve the referencing
Overall grade band: C

Criterion: Context and Methodological Framework
Strengths: Context and methodology are described.
Areas for improvement: Depth of contextual analysis could be increased.
Improvement suggestion: Add more detail on the target audience and their usability requirements.
Suggested level: C

Criterion: Usability Issue Analysis and Evidence
Strengths: Five issues identified with heuristic mappings and severity ratings, and visual evidence is referenced in the appendices.
Areas for improvement: Depth of analysis for each issue could be increased.
Improvement suggestion: Provide more detailed analysis for each issue.
Suggested level: C

Criterion: Design Recommendations and Theory Application
Strengths: Recommendations are present and connect to the identified issues.
Areas for improvement: Theory application could be strengthened.
Improvement suggestion: Include more theoretical grounding in the recommendations.
Suggested level: C

Criterion: Academic Structure and Referencing
Strengths: Structure follows the required format and referencing is present.
Areas for improvement: Referencing could be more thorough.
Improvement suggestion: Include additional academic sources.
Suggested level: C"""

HIGH_QUALITY_FEEDBACK_1 = """Overall comment: The report demonstrates solid grasp of heuristic evaluation as an expert-review method, correctly establishing Nielsen's Ten Heuristics as the framework and grounding usability in ISO 9241's effectiveness, efficiency, and satisfaction. The five-issue set covers a reasonable spread of Nielsen principles and severity ratings are justified using frequency and impact, matching the criteria taught in Week 4 and the Sample Answer Guide. The main gap is that some heuristic mappings could be sharper, and the Week 2 cognition link is stated but under-developed. On the rubric, this sits at the boundary between C and D: the structure and evidence are D-level, but the theoretical depth in Criterion 3 currently reads as C-level.
Key strengths:
- Nielsen's Ten Heuristics is explicitly established as the framework in the Methodology, with the correct distinction from user testing (matching the Week 4 lecture on expert review as a "discount usability engineering method").
- Five distinct issues are identified with 0-4 severity ratings and justifications based on frequency and impact, aligned with the Task 3 guidance in the A1 Worksheet.
- Recommendations are concrete and directly resolve each violation, e.g. moving the search bar to the top for Issue 1, and adding a confirmation dialog for Issue 5.
Priority improvements:
- First, revise the Issue 1 heuristic mapping: the student's own recommendation cites "recognition over recall", which is a stronger fit than "Visibility of System Status" for a hidden search element. Fixing this mapping also strengthens Criterion 3's theory link.
- Second, develop the Week 2 cognition connection with a specific mechanism: for Issue 2, name the type of cognitive load (extraneous) that the vague error message imposes, and explain why field-adjacent, specific error messages reduce it.
- Third, explicitly list the three primary tasks as a numbered set in the Methodology, as the A1 Worksheet Task 1 requires, so the evaluation scope is visible before Findings.
Overall grade band: D

Criterion: Context and Methodological Framework
Strengths: The QuickEats app is described with its purpose (food delivery) and target audience (young adults and busy professionals in distracted contexts), and the context-of-use argument correctly links to why usability matters here. Nielsen's Ten Heuristics is stated as the framework and ISO 9241's three metrics are correctly cited from Week 1.
Areas for improvement: The three primary tasks (searching for a restaurant, adding items to the cart, checking out) appear inside the Methodology narrative but are not listed as a discrete set. The rubric for Distinction requires the framework to be "clearly described" with minor omissions only, and the missing primary-task list is one such omission.
Improvement suggestion: In the Methodology, add a short numbered list of the three primary tasks before describing the evaluation process. This mirrors the Sample Answer Guide's Task 1 answer format and satisfies the tutorial worksheet's explicit requirement.
Suggested level: D

Criterion: Usability Issue Analysis and Evidence
Strengths: Five distinct issues are identified across five different heuristics, avoiding clustering. Severity ratings from 2 to 4 are justified using both frequency and impact, matching the rubric's Distinction descriptor. Visual evidence is referenced through Appendices A-E, and Issues 2, 3, and 5 include clear articulation of user consequences.
Areas for improvement: The Issue 1 mapping to Visibility of System Status is inconsistent with Nielsen's definition of that heuristic (which concerns system state feedback rather than element placement). The student's own recommendation for Issue 1 cites "recognition over recall", which suggests the correct mapping is Recognition Rather Than Recall, or Aesthetic and Minimalist Design.
Improvement suggestion: Remap Issue 1 to Recognition Rather Than Recall and update the justification paragraph to describe the recognition-versus-recall trade-off. This aligns the mapping with the actual violation and strengthens the internal consistency of the Findings section.
Suggested level: D

Criterion: Design Recommendations and Theory Application
Strengths: Each recommendation directly resolves its violation. The Issue 2 recommendation (field-adjacent, specific error text) and Issue 5 recommendation (confirmation dialog for a Severity 4 error prevention violation) are well-targeted and would be actionable for a development team.
Areas for improvement: The Week 2 cognition references (cognitive load, mental models, recognition over recall) appear as labels rather than as developed arguments. The rubric's Distinction descriptor requires "a solid connection to usability theory", but currently the theory is invoked briefly and not explained.
Improvement suggestion: For Issues 2 and 3, add one sentence per recommendation identifying which specific cognitive limit the design change addresses (extraneous cognitive load for Issue 2, working-memory span across five checkout screens for Issue 3) and why the change reduces that limit. This lifts the theory application from C to D territory.
Suggested level: C

Criterion: Academic Structure and Referencing
Strengths: The report includes all required sections (Introduction, Methodology, Findings, Recommendations, Conclusion) in the correct order, adheres to the specified structure, and uses APA style consistently. Nielsen (1994, 1995) and ISO 9241 are cited correctly.
Areas for improvement: The Evans (2023) reference to lecture notes is not the standard way to cite unit content and would typically be replaced by citing specific lecture readings or slides.
Improvement suggestion: Replace the Evans (2023) reference with citations to the specific Week 1, 2, and 4 readings actually used, so the reference list reflects primary academic sources rather than a general LMS note.
Suggested level: D"""

HIGH_QUALITY_FEEDBACK_2 = """Overall comment: A capable Distinction-standard report with three fixable issues holding it back from a stronger result. The framework, severity method, and recommendations are on target, but the Issue 1 heuristic mapping contradicts the student's own recommendation, the Week 2 cognition link is named without being developed, and the primary-task list required by the Worksheet is absent from the Methodology.
Key strengths:
- Nielsen framework and expert-review method correctly framed, matching the Week 4 lecture.
- Severity ratings (0-4) justified by frequency and impact per the Worksheet.
- Recommendations for Issues 2 and 5 are precise and directly resolve the violations.
Priority improvements:
- Remap Issue 1 to Recognition Rather Than Recall (the student already cites this concept in their own recommendation).
- Name the specific cognitive load type for Issues 2 and 3 and explain the reduction mechanism.
- Add a numbered three-task list to the Methodology as required by A1 Worksheet Task 1.
Overall grade band: D

Criterion: Context and Methodological Framework
Strengths: QuickEats context, distracted user model, Nielsen framework, and ISO 9241 metrics are all present and correctly used.
Areas for improvement: The three primary tasks are described in prose but not listed as a discrete set, which the Worksheet Task 1 requires.
Improvement suggestion: Add a numbered list of the three primary tasks in the Methodology before the Findings begin.
Suggested level: D

Criterion: Usability Issue Analysis and Evidence
Strengths: Five distinct heuristics, justified severity ratings, and appendix screenshots for each issue.
Areas for improvement: Issue 1's mapping to Visibility of System Status describes element placement, not system-state feedback, and contradicts the student's own recognition-over-recall recommendation.
Improvement suggestion: Remap Issue 1 to Recognition Rather Than Recall and revise the justification paragraph to match. The internal consistency this creates will strengthen the whole Findings section.
Suggested level: D

Criterion: Design Recommendations and Theory Application
Strengths: Recommendations for Issue 2 (field-adjacent specific error text) and Issue 5 (confirmation dialog for a Severity 4 error-prevention gap) are actionable and correctly targeted.
Areas for improvement: Cognitive load and mental models are named but not explained, which limits this criterion to C.
Improvement suggestion: For Issue 2, name extraneous cognitive load and explain why a specific, field-adjacent message reduces it. For Issue 3, connect five sequential checkout screens to working-memory span.
Suggested level: C

Criterion: Academic Structure and Referencing
Strengths: All required sections in order, consistent APA style, correct primary citations to Nielsen and ISO 9241.
Areas for improvement: Evans (2023) as a lecture-note reference is not a standard academic source.
Improvement suggestion: Replace it with the specific Week 1, 2, and 4 readings used.
Suggested level: D"""

HIGH_QUALITY_FEEDBACK_3 = """Overall comment: A capable Distinction-band report with the strongest sections in Methodology and Findings and the weakest in Theory Application. Two observations are worth flagging that go beyond routine rubric-checking. First, the report contains an internal contradiction the student appears not to notice: Issue 1 is mapped to Visibility of System Status in the Findings, but the paired recommendation invokes "recognition over recall", indicating the student already understood the correct heuristic but did not update the mapping. Second, the Severity 4 rating on Issue 5 is well-justified as a Usability Catastrophe under the Worksheet's frequency-times-impact criterion, but its recommendation is comparatively brief for the highest-severity issue in the set.
Key strengths:
- Nielsen framework, ISO 9241 metrics, and Week 4 distinction between expert review and user testing are correctly applied.
- Five distinct heuristics covered with 0-4 severity justification following the Worksheet Task 3 method.
- Issue 2 and Issue 5 recommendations are concrete and directly address the violations.
Priority improvements:
- First, resolve the Issue 1 mapping-recommendation contradiction by remapping to Recognition Rather Than Recall. This is a targeted edit with disproportionate benefit for internal consistency.
- Second, expand the Issue 5 recommendation to match its Severity 4 weight: describe the confirmation-dialog wording, the cancel path, and how it aligns with Nielsen's Error Prevention principle from the Week 4 lecture.
- Third, develop the Week 2 cognition link on Issues 2 and 3 by naming the specific cognitive limit (extraneous load and working-memory span respectively).
Overall grade band: D

Criterion: Context and Methodological Framework
Strengths: Context of use, distracted-mobile user model, Nielsen framework, and ISO 9241 effectiveness-efficiency-satisfaction are correctly established, which the rubric's D descriptor treats as "clearly described".
Areas for improvement: The three primary tasks are described but not listed as a numbered set, which the A1 Worksheet Task 1 explicitly requires.
Improvement suggestion: Introduce the three tasks as a short numbered list in the Methodology before the Findings begin, matching the Sample Answer Guide's Task 1 format.
Suggested level: D

Criterion: Usability Issue Analysis and Evidence
Strengths: Five distinct heuristics covered with severity ratings justified through frequency and impact. Appendices A-E are referenced for visual evidence per the assignment specification.
Areas for improvement: Issue 1 is mapped to Visibility of System Status, which concerns system-state feedback rather than element placement. Notably, the student's own recommendation for Issue 1 cites "recognition over recall", indicating internal awareness of the correct heuristic that never made it back into the Findings mapping.
Improvement suggestion: Remap Issue 1 to Recognition Rather Than Recall, and rewrite the Findings paragraph to describe the recall-cost imposed by hiding the search bar below the fold. This one edit removes an internal contradiction and improves the theory link.
Suggested level: D

Criterion: Design Recommendations and Theory Application
Strengths: Recommendations for Issue 2 and Issue 5 correctly resolve the identified violations. The Issue 2 fix (field-adjacent, named-field error text) is textbook-quality.
Areas for improvement: The Week 2 cognition references appear as labels rather than mechanisms. Additionally, the Issue 5 recommendation is the shortest in the set despite Issue 5 carrying the highest severity (4, Usability Catastrophe), which weakens the theory-to-action link where it matters most.
Improvement suggestion: For Issue 2, name extraneous cognitive load and explain why a specific, field-adjacent error message reduces it. For Issue 3, connect the five sequential checkout screens to working-memory span and the checkout-abandonment risk. For Issue 5, expand the recommendation with confirmation-dialog wording and a cancel path, explicitly grounding it in Nielsen's Error Prevention principle from the Week 4 lecture.
Suggested level: C

Criterion: Academic Structure and Referencing
Strengths: All required sections present in the correct order, APA style used consistently, Nielsen (1994, 1995) and ISO 9241 cited correctly.
Areas for improvement: The Evans (2023) reference to lecture notes is not a standard academic source and should be replaced with the specific readings that were actually used.
Improvement suggestion: Substitute Evans (2023) with citations to the specific Week 1, 2, and 4 primary readings used in the analysis.
Suggested level: D"""


SYNTHETIC_BASELINES = {
    "low_1": LOW_QUALITY_FEEDBACK_1,
    "low_2": LOW_QUALITY_FEEDBACK_2,
    "low_3": LOW_QUALITY_FEEDBACK_3,
    "medium_1": MEDIUM_QUALITY_FEEDBACK_1,
    "medium_2": MEDIUM_QUALITY_FEEDBACK_2,
    "medium_3": MEDIUM_QUALITY_FEEDBACK_3,
    "medium_4": MEDIUM_QUALITY_FEEDBACK_4,
    "high_1": HIGH_QUALITY_FEEDBACK_1,
    "high_2": HIGH_QUALITY_FEEDBACK_2,
    "high_3": HIGH_QUALITY_FEEDBACK_3,
}

def judge_single_feedback(
    student_submission: str,
    assignment_spec: str,
    rubric_text: str,
    course_materials: str,
    feedback_text: str,
    ai_grade_band: str = "unknown",
) -> dict:
    """
    Evaluate a single piece of AI-generated feedback and return judge scores.
    Designed to be called from the web app (app.py) rather than the CLI.

    Returns a dict shaped like:
    {
      "judges": {
        "gemini": {
          "provider": "gemini",
          "model": "...",
          "scores": {
            "grounding": {"score": 3, "reason": "...", "evidence": "...", "defects": [...], "missing_evidence": [...]},
            "specificity": {...},
            "actionability": {...}
          }
        },
        "qwen": {...}
      }
    }
    """
    context = {
        "student_submission": student_submission,
        "assignment_spec": assignment_spec,
        "rubric": rubric_text,
        "course_materials": course_materials,
    }

    result = {"judges": {}}
    for provider_key in ("gemini", "qwen"):
        judge_result = {
            "provider": provider_key,
            "model": JUDGE_MODELS[provider_key],
            "scores": {},
        }
        for dimension in DIMENSIONS:
            score_data = run_dimension_judge(
                provider=provider_key,
                dimension=dimension,
                context=context,
                feedback_text=feedback_text,
                evaluated_model="ai_generated",
                ai_grade_band=ai_grade_band,
            )
            judge_result["scores"][dimension] = score_data
        result["judges"][provider_key] = judge_result

    return result


def judge_single_feedback(
    student_submission: str,
    assignment_spec: str,
    rubric_text: str,
    course_materials: str,
    feedback_text: str,
    ai_grade_band: str = "unknown",
) -> dict:
    """
    Evaluate a single piece of AI-generated feedback and return judge scores.
    Designed to be called from the web app (app.py) rather than the CLI.

    Runs the default two-judge setup (Gemini + Qwen) with strict scoring mode
    across all three dimensions (grounding, specificity, actionability), and
    returns the results in a structure the frontend can render directly.
    """
    dimensions = list(DIMENSIONS.keys())
    judge_configs = [
        {"provider": "gemini", "model": None, "display_name": "Gemini", "key": "gemini"},
        {"provider": "qwen", "model": None, "display_name": "Qwen", "key": "qwen"},
    ]

    result = {"judges": {}}
    for judge in judge_configs:
        provider = judge["provider"]
        model = judge["model"]
        resolved_model = resolve_model_name(provider, model)
        display_name = judge["display_name"]
        key = judge["key"]

        result["judges"][key] = {
            "provider": provider,
            "model": resolved_model,
            "note": judge_note(provider, resolved_model, ai_grade_band),
            "scores": judge_feedback(
                provider,
                model,
                temperature=0.0,
                call_delay=0.0,
                scoring_mode="strict",
                dimensions=dimensions,
                judge_name=display_name,
                feedback_text=feedback_text,
                submission_text=student_submission,
                assignment_spec=assignment_spec,
                rubric_text=rubric_text,
                retrieved_context=course_materials,
            ),
        }
    return result

if __name__ == "__main__":
    main()
