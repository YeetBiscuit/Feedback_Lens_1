import argparse
from pathlib import Path

from feedback_lens.curriculum.pipeline import generate_unit
from feedback_lens.db.connection import connect_db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a complete unit curriculum package.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--description")
    source.add_argument("--description-file")
    parser.add_argument("--year", type=int)
    parser.add_argument("--semester")
    parser.add_argument("--provider", default="qwen")
    parser.add_argument("--model")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print the final summary, not live generation progress.",
    )
    return parser


def _load_description(args: argparse.Namespace) -> str:
    if args.description is not None:
        return args.description
    return Path(args.description_file).read_text(encoding="utf-8").strip()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    description = _load_description(args)
    progress = None
    if not args.quiet:
        progress = lambda message: print(f"[unit-gen] {message}", flush=True)

    with connect_db() as conn:
        result = generate_unit(
            conn,
            description,
            year=args.year,
            semester=args.semester,
            provider=args.provider,
            model=args.model,
            temperature=args.temperature,
            progress_callback=progress,
        )

    print(
        f"Generated curriculum_run={result.curriculum_run_id} for "
        f"{result.course_code} at {result.output_root} using "
        f"{result.provider}:{result.model}."
    )
    print("Review the generated files, then ingest when ready:")
    print(f"python ingest_unit.py \"{result.output_root}\"")


if __name__ == "__main__":
    main()
