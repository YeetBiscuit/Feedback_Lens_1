import os
import json
import time
from openai import OpenAI


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


def build_dimension_prompt(dimension, criteria, feedback_text, submission_text, assignment_spec, rubric_text, retrieved_context):
    return f"""You are an academic feedback quality evaluator. Your task is to evaluate ONLY the {dimension.upper()} dimension of the AI-generated feedback below.

{dimension.upper()} SCORING CRITERIA:
{criteria}

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
  "evidence": ""
}}

Where:
- score: integer from 1 to 5
- reason: explanation of why you gave this score, referencing specific parts of the feedback
- evidence: specific quotes or examples from the feedback that support your score"""


def run_dimension_judge(client, model, dimension, criteria, feedback_text, submission_text, assignment_spec, rubric_text, retrieved_context):
    prompt = build_dimension_prompt(dimension, criteria, feedback_text, submission_text, assignment_spec, rubric_text, retrieved_context)
    time.sleep(4)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.choices[0].message.content
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


def judge_feedback(client, model, judge_name, feedback_text, submission_text, assignment_spec, rubric_text, retrieved_context):
    print(f"  Running {judge_name}...")
    scores = {}
    for dimension, criteria in DIMENSIONS.items():
        print(f"    Evaluating {dimension}...")
        result = run_dimension_judge(client, model, dimension, criteria, feedback_text, submission_text, assignment_spec, rubric_text, retrieved_context)
        scores[dimension] = result
        print(f"    {dimension}: {result['score']}/5")
    return scores


def process_file(input_file, gemini_client, qwen_client):
    print(f"\nProcessing: {input_file}")
    with open(input_file, "r") as f:
        data = json.load(f)

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

    print(f"\nGemini judge (self-evaluation):")
    result["judges"]["gemini"] = {
        "model": "gemini-3-flash-preview",
        "note": "self-evaluation - same model family, potential bias",
        "scores": judge_feedback(gemini_client, "gemini-3-flash-preview", "Gemini", feedback_text, submission_text, assignment_spec, rubric_text, retrieved_context)
    }

    print(f"\nQwen judge (cross-model):")
    result["judges"]["qwen"] = {
        "model": "qwen-plus",
        "note": "cross-model evaluation",
        "scores": judge_feedback(qwen_client, "qwen-plus", "Qwen", feedback_text, submission_text, assignment_spec, rubric_text, retrieved_context)
    }

    return result


def print_summary(all_results):
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    header = f"{'File':<25} {'AI Grade':<10} {'Judge':<10}"
    for dim in DIMENSIONS:
        header += f" {dim[:8]:>9}"
    print(header)
    print("-" * 80)

    for r in all_results:
        fname = r["input_file"].split("/")[-1].replace("generation_run_", "run_").replace("_full_planner.json", "")
        if fname.startswith("synthetic_"):
            fname = fname
        grade = r["ai_grade_band"]
        for judge_name, judge_data in r["judges"].items():
            row = f"{fname:<25} {grade:<10} {judge_name:<10}"
            for dim in DIMENSIONS:
                score = judge_data["scores"].get(dim, {}).get("score", "-")
                row += f" {str(score):>9}"
            print(row)

    print("\nNote: Gemini evaluating Gemini output may show self-evaluation bias (inflated scores).")
    print("Qwen scores are more objective as a cross-model evaluation.")


def main():
    input_files = [
        "exports/generation_run_6_full_planner.json",
        "exports/generation_run_9_full_planner.json",
        "exports/generation_run_10_full_planner.json",
    ]
    output_file = "exports/llm_judge_results.json"

    gemini_client = OpenAI(
        api_key=os.environ.get("GEMINI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    qwen_client = OpenAI(
        api_key=os.environ.get("QWEN_API_KEY"),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
    )

    all_results = []
    for f in input_files:
        result = process_file(f, gemini_client, qwen_client)
        all_results.append(result)

    print("\n" + "="*50)
    print("TESTING WITH SYNTHETIC LOW/MEDIUM QUALITY FEEDBACK")
    print("="*50)

    with open("exports/generation_run_6_full_planner.json", "r") as f:
        ref_data = json.load(f)
    submission_text, assignment_spec, rubric_text, retrieved_context = extract_context(ref_data)

    for label, fake_feedback in [("LOW_QUALITY", LOW_QUALITY_FEEDBACK), ("MEDIUM_QUALITY", MEDIUM_QUALITY_FEEDBACK)]:
        print(f"\nTesting {label} feedback...")
        synthetic_result = {
            "input_file": f"synthetic_{label}",
            "student_identifier": "synthetic_test",
            "evaluated_model": "human_written",
            "pipeline_version": "synthetic_test",
            "ai_grade_band": "N/A",
            "judges": {}
        }
        print("  Gemini judge...")
        synthetic_result["judges"]["gemini"] = {
            "model": "gemini-3-flash-preview",
            "note": "self-evaluation",
            "scores": judge_feedback(gemini_client, "gemini-3-flash-preview", "Gemini", fake_feedback, submission_text, assignment_spec, rubric_text, retrieved_context)
        }
        print("  Qwen judge...")
        synthetic_result["judges"]["qwen"] = {
            "model": "qwen-plus",
            "note": "cross-model",
            "scores": judge_feedback(qwen_client, "qwen-plus", "Qwen", fake_feedback, submission_text, assignment_spec, rubric_text, retrieved_context)
        }
        all_results.append(synthetic_result)

    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to {output_file}")

    print_summary(all_results)


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