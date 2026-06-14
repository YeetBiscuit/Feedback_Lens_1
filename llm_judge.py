import argparse
import glob
import json
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
    if "all" in values:
        return ["low", "medium"]
    selected = []
    for value in values:
        if value not in selected:
            selected.append(value)
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
    return normalize_dimension_result(parse_json_response(raw), dimension)


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
            "Examples: --judge qwen --judge gemini --judge nvidia_deepseek:deepseek-ai/deepseek-v4-pro"
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
        choices=["all", "low", "medium", "none"],
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
        synthetic_feedback = {
            "low": LOW_QUALITY_FEEDBACK,
            "medium": MEDIUM_QUALITY_FEEDBACK,
        }

        for label in synthetic_labels:
            display_label = f"{label.upper()}_QUALITY"
            fake_feedback = synthetic_feedback[label]
            print(f"\nTesting {display_label} feedback...")
            synthetic_result = {
                "input_file": f"synthetic_{display_label}",
                "student_identifier": "synthetic_test",
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


LOW_QUALITY_FEEDBACK = """Overall comment: The student did an okay job on this assignment. There are some areas that could be improved. Overall a decent submission.
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

MEDIUM_QUALITY_FEEDBACK = """Overall comment: The student demonstrated adequate understanding of heuristic evaluation. Five usability issues were identified in the QuickEats app with severity ratings. Some recommendations were provided but lack theoretical depth.
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

if __name__ == "__main__":
    main()
