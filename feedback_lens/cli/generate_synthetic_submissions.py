import argparse

from feedback_lens.curriculum.pipeline import GRADE_BANDS, generate_synthetic_submissions
from feedback_lens.db.connection import connect_db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate extra synthetic submissions for an existing unit package.",
    )
    parser.add_argument("unit_directory")
    parser.add_argument(
        "--assignment",
        action="append",
        dest="assignment_codes",
        help="Assignment code to generate for, such as A1. Repeat for multiple. Defaults to all.",
    )
    parser.add_argument(
        "--grade-band",
        action="append",
        type=str.upper,
        choices=GRADE_BANDS,
        help="Target grade band. Repeat for multiple. Defaults to HD, D, C, and P.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of extra submissions to generate per selected assignment and grade band.",
    )
    parser.add_argument("--provider", default="qwen")
    parser.add_argument("--model")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print the final summary, not live generation progress.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    progress = None
    if not args.quiet:
        progress = lambda message: print(f"[synthetic-submissions] {message}", flush=True)

    with connect_db() as conn:
        result = generate_synthetic_submissions(
            conn,
            args.unit_directory,
            assignment_codes=args.assignment_codes,
            grade_bands=args.grade_band,
            count_per_band=args.count,
            provider=args.provider,
            model=args.model,
            temperature=args.temperature,
            progress_callback=progress,
        )

    print(
        f"Generated {result.generated_count} extra synthetic submission(s) "
        f"for {result.course_code} at {result.output_root} using "
        f"{result.provider}:{result.model}."
    )
    print("Review the new submissions, then ingest when ready:")
    print(f"python ingest_unit.py \"{result.output_root}\"")


if __name__ == "__main__":
    main()
