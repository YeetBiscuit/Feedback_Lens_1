import argparse

from feedback_lens.db.connection import connect_db
from feedback_lens.feedback.pipeline import generate_feedback_for_submission
from feedback_lens.feedback.retrieval import (
    DEFAULT_MAX_FINAL_CHUNKS,
    DEFAULT_PER_CUE_TOP_K,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate rubric-aligned feedback for an existing student submission.",
    )
    parser.add_argument("submission_id", type=int)
    parser.add_argument("--provider", default="qwen")
    parser.add_argument("--model")
    parser.add_argument(
        "--per-cue-top-k",
        "--top-k",
        dest="per_cue_top_k",
        type=int,
        default=DEFAULT_PER_CUE_TOP_K,
        help=(
            "How many chunks to retrieve for each retrieval cue. "
            "--top-k is kept as a backwards-compatible alias."
        ),
    )
    parser.add_argument(
        "--max-final-chunks",
        type=int,
        default=DEFAULT_MAX_FINAL_CHUNKS,
        help="Maximum deduplicated chunks to pass to the feedback generator.",
    )
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument(
        "--strategy",
        dest="retrieval_strategy",
        choices=[
            "baseline",
            "planned",
            "assignment_spec_multi_cue_v1",
            "llm_planned_cue_v1",
        ],
        default="baseline",
        help=(
            "Retrieval strategy for retrieval mode. baseline uses imported "
            "assignment-spec cues; planned uses an LLM planner to generate "
            "targeted retrieval cues from the spec, rubric, and submission."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["retrieval", "direct"],
        default="retrieval",
        help=(
            "retrieval uses the current course-context retrieval pipeline; "
            "direct sends only the submission, rubric, and assignment spec."
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    with connect_db() as conn:
        result = generate_feedback_for_submission(
            conn,
            submission_id=args.submission_id,
            provider=args.provider,
            model=args.model,
            per_cue_top_k=args.per_cue_top_k,
            max_final_chunks=args.max_final_chunks,
            temperature=args.temperature,
            context_mode=args.mode,
            retrieval_strategy=args.retrieval_strategy,
        )

    print(
        f"Completed generation_run={result.generation_id} using "
        f"{result.provider}:{result.model} in {result.context_mode} mode. "
        f"retrieval_strategy={result.retrieval_strategy}, "
        f"retrieval_cues={result.retrieval_cue_count}, "
        f"per_cue_top_k={result.per_cue_top_k}, "
        f"max_final_chunks={result.max_final_chunks}, "
        f"deduplicated_chunks={result.deduplicated_chunk_count}, "
        f"criterion_count={result.criterion_count}, "
        f"overall_grade_band={result.overall_grade_band or 'None'}."
    )


if __name__ == "__main__":
    main()
