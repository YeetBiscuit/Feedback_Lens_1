import argparse

from feedback_lens.db.connection import connect_db
from feedback_lens.feedback.pipeline import generate_feedback_for_submission


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate rubric-aligned feedback for an existing student submission.",
    )
    parser.add_argument("submission_id", type=int)
    parser.add_argument("--provider", default="qwen")
    parser.add_argument("--model")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--temperature", type=float, default=0.2)
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
            top_k=args.top_k,
            temperature=args.temperature,
        )

    print(
        f"Completed generation_run={result.generation_id} using "
        f"{result.provider}:{result.model}. "
        f"retrieval_cues={result.retrieval_cue_count}, "
        f"deduplicated_chunks={result.deduplicated_chunk_count}, "
        f"criterion_count={result.criterion_count}, "
        f"overall_grade_band={result.overall_grade_band or 'None'}."
    )


if __name__ == "__main__":
    main()
