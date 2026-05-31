import os
import json
from openai import OpenAI


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


def build_prompt(feedback_text):
    return f"""You are an academic feedback quality evaluator. Evaluate the AI-generated feedback below using three criteria from Hattie and Timperley (2007), each scored 1-5.

CRITERIA AND SCORING RUBRIC (Hattie and Timperley, 2007):

1. Grounding (1-5):
   1 = Feedback is not meaningfully connected to the assignment specification, rubric, or course materials.
   2 = Feedback shows limited connection to the assignment specification or rubric, but the link is vague.
   3 = Feedback is generally aligned with the assignment specification and rubric, and shows some awareness of relevant course materials.
   4 = Feedback is clearly grounded in both the assessment specification/rubric and relevant course materials.
   5 = Feedback is strongly grounded in both assessment requirements and course materials. It accurately connects rubric criteria, task expectations, and relevant unit concepts.

2. Specificity (1-5):
   1 = Feedback is highly generic and could apply to almost any student submission.
   2 = Feedback identifies broad strengths or weaknesses, but the comments remain vague.
   3 = Feedback identifies some specific strengths, weaknesses, or gaps in the student submission.
   4 = Feedback clearly identifies concrete aspects of the student submission, including specific strengths, weaknesses, and performance gaps.
   5 = Feedback provides precise, student-specific analysis of performance. It clearly explains what the student did well, what is missing, and how it affects achievement of the assessment criteria.

3. Actionability (1-5):
   1 = Feedback provides little or no usable guidance for improvement.
   2 = Feedback offers improvement suggestions, but they are vague or difficult to apply.
   3 = Feedback provides some useful suggestions for improvement, but they may be incomplete or not clearly prioritised.
   4 = Feedback provides clear and practical guidance that the student could realistically apply.
   5 = Feedback provides highly concrete, prioritised, and assessment-relevant improvement steps. The student would clearly understand what to revise, why it matters, and how to improve.
    
SCORING: 1=Very Poor, 2=Poor, 3=Adequate, 4=Good, 5=Excellent

AI-GENERATED FEEDBACK TO EVALUATE:
{feedback_text}

Respond in this exact JSON format with no markdown fences:
{{
  "grounding": {{"score": 0, "reason": ""}},
  "specificity": {{"score": 0, "reason": ""}},
  "actionability": {{"score": 0, "reason": ""}},
  "overall_quality": ""
}}"""


def run_judge(client, model, prompt):
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.choices[0].message.content
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


def main():
    input_file = "exports/generation_run_6_full_planner.json"
    output_file = "exports/llm_judge_results.json"

    print(f"Loading data from {input_file}...")
    with open(input_file, "r") as f:
        data = json.load(f)

    feedback_text = build_feedback_text(data)
    prompt = build_prompt(feedback_text)

    results = {
        "evaluated_file": input_file,
        "evaluated_model": data["generation_run"]["llm_model"],
        "pipeline_version": data["generation_run"]["pipeline_version"],
        "student_identifier": data["generation_run"]["student_identifier"],
    }

    # Gemini self-evaluation
    print("\nRunning Gemini judge (self-evaluation - same model family)...")
    gemini_client = OpenAI(
        api_key=os.environ.get("GEMINI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    gemini_result = run_judge(gemini_client, "gemini-3-flash-preview", prompt)
    results["gemini_judge"] = {
        "judge_model": "gemini-3-flash-preview",
        "note": "self-evaluation - same model, potential bias",
        "scores": gemini_result
    }
    print(f"Gemini scores: Grounding={gemini_result['grounding']['score']}, "
          f"Specificity={gemini_result['specificity']['score']}, "
          f"Actionability={gemini_result['actionability']['score']}")

    # Qwen cross-model evaluation
    print("\nRunning Qwen judge (cross-model evaluation)...")
    qwen_client = OpenAI(
        api_key=os.environ.get("QWEN_API_KEY"),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    qwen_result = run_judge(qwen_client, "qwen-plus", prompt)
    results["qwen_judge"] = {
        "judge_model": "qwen-plus",
        "note": "cross-model evaluation - different model family",
        "scores": qwen_result
    }
    print(f"Qwen scores: Grounding={qwen_result['grounding']['score']}, "
          f"Specificity={qwen_result['specificity']['score']}, "
          f"Actionability={qwen_result['actionability']['score']}")

    # Save results
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {output_file}")

    # Summary
    print("\n=== SUMMARY ===")
    print(f"{'Judge':<20} {'Grounding':>10} {'Specificity':>12} {'Actionability':>14}")
    print("-" * 60)
    for judge_key, judge_data in results.items():
        if not isinstance(judge_data, dict) or "scores" not in judge_data:
            continue
        s = judge_data["scores"]
        print(f"{judge_data['judge_model']:<20} "
              f"{s['grounding']['score']:>10} "
              f"{s['specificity']['score']:>12} "
              f"{s['actionability']['score']:>14}")
    print("\nNote: Self-evaluation (same model) tends to give higher scores.")
    print("Cross-model evaluation is more objective.")


if __name__ == "__main__":
    main()
