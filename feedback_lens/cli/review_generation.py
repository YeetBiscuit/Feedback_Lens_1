import argparse
import sqlite3
import textwrap
from pathlib import Path

from feedback_lens.feedback.review import (
    fetch_generation_review,
    list_generation_runs,
    parse_json_text_list,
)
from feedback_lens.paths import DB_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review feedback generation runs without writing raw SQL.",
    )
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser(
        "list",
        help="List recent generation runs.",
    )
    list_parser.add_argument("--limit", type=int, default=10)

    show_parser = subparsers.add_parser(
        "show",
        help="Show one generation run in detail.",
    )
    show_parser.add_argument("generation_id", nargs="?", type=int)
    show_parser.add_argument("--show-prompt", action="store_true")
    show_parser.add_argument("--show-response", action="store_true")
    show_parser.add_argument("--full-chunks", action="store_true")
    show_parser.add_argument("--chunk-chars", type=int, default=240)

    parser.set_defaults(command="show")
    return parser


def _print_wrapped(label: str, value: str | None, indent: str = "  ") -> None:
    if not value:
        print(f"{label}: (none)")
        return

    print(f"{label}:")
    wrapped = textwrap.fill(
        _terminal_safe(value),
        width=100,
        initial_indent=indent,
        subsequent_indent=indent,
        replace_whitespace=False,
    )
    print(wrapped)


def _format_chunk_text(text: str, limit: int, full_chunks: bool) -> str:
    cleaned = " ".join(text.split())
    if full_chunks or len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit].rstrip()}..."


def _terminal_safe(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def _row_value(row: sqlite3.Row, key: str) -> str | None:
    if key not in row.keys():
        return None
    return row[key]


def _connect_review_db() -> sqlite3.Connection:
    db_path = Path(DB_PATH).resolve()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def handle_list(limit: int) -> None:
    with _connect_review_db() as conn:
        rows = list_generation_runs(conn, limit=limit)

    if not rows:
        print("No generation runs found.")
        return

    for row in rows:
        print(
            f"[{row['generation_id']}] {row['unit_code']} | {row['assignment_name']} | "
            f"{row['student_identifier']} | status={row['status']} | "
            f"grade={row['overall_grade_band'] or 'None'} | "
            f"provider={row['llm_provider'] or 'unknown'}:{row['llm_model']} | "
            f"started={row['started_at']}"
        )


def handle_show(
    generation_id: int | None,
    show_prompt: bool,
    show_response: bool,
    full_chunks: bool,
    chunk_chars: int,
) -> None:
    with _connect_review_db() as conn:
        if generation_id is None:
            latest = list_generation_runs(conn, limit=1)
            if not latest:
                print("No generation runs found.")
                return
            generation_id = latest[0]["generation_id"]

        review = fetch_generation_review(conn, generation_id)

    run = review["run"]
    overall = review["overall_feedback"]
    criteria = review["criterion_feedback"]
    retrievals = review["retrieval_records"]

    print(f"Generation Run {run['generation_id']}")
    print(
        f"Unit: {run['unit_code']} - {run['unit_name']} "
        f"({run['semester'] or 'No semester'}, {run['year'] or 'No year'})"
    )
    print(
        f"Assignment: {run['assignment_name']} | "
        f"Student: {run['student_identifier']} | Status: {run['status']}"
    )
    print(
        f"Provider: {run['llm_provider'] or 'unknown'}:{run['llm_model']} | "
        f"Pipeline: {run['pipeline_version']} | Prompt template: {run['prompt_template_version']}"
    )
    print(
        f"Retrieval: strategy={run['retrieval_strategy'] or 'None'}, top_k={run['top_k']}, "
        f"temperature={run['temperature']}"
    )
    print(f"Started: {run['started_at']} | Completed: {run['completed_at'] or 'still running'}")
    print(f"Submission file: {run['original_file_path'] or '(none)'}")
    if run["error_message"]:
        print(f"Error: {run['error_message']}")

    print("\nOverall Feedback")
    if overall is None:
        print("(none)")
    else:
        print(f"Grade band: {overall['overall_grade_band'] or 'None'}")
        _print_wrapped("Overall comment", overall["overall_comment"])

        strengths = parse_json_text_list(overall["key_strengths"])
        print("Key strengths:")
        if strengths:
            for item in strengths:
                print(f"- {item}")
        else:
            print("(none)")

        improvements = parse_json_text_list(overall["priority_improvements"])
        print("Priority improvements:")
        if improvements:
            for item in improvements:
                print(f"- {item}")
        else:
            print("(none)")

    print("\nCriterion Feedback")
    if not criteria:
        print("(none)")
    else:
        for row in criteria:
            print(
                f"[{row['criterion_order']}] {row['criterion_name']} "
                f"(suggested={row['suggested_level'] or 'None'})"
            )
            _print_wrapped("Strengths", row["strengths"])
            _print_wrapped("Areas for improvement", row["areas_for_improvement"])
            _print_wrapped("Improvement suggestion", row["improvement_suggestion"])
            _print_wrapped("Evidence summary", row["evidence_summary"])
            print()

    print("Retrieved Chunks")
    if not retrievals:
        print("(none)")
    else:
        for row in retrievals:
            material_bits = [row["material_title"], row["material_type"]]
            if row["week_number"] is not None:
                material_bits.append(f"week {row['week_number']}")
            material_label = " | ".join(bit for bit in material_bits if bit)
            page_label = ""
            if row["page_number_start"] is not None:
                page_label = f" | pages {row['page_number_start']}-{row['page_number_end'] or row['page_number_start']}"

            print(
                f"- rank={row['rank_position']} chunk_id={row['chunk_id']} "
                f"score={row['similarity_score'] if row['similarity_score'] is not None else 'None'} "
                f"used_in_prompt={row['used_in_prompt']} | {material_label}{page_label}"
            )
            _print_wrapped("Query", row["query_text"])
            _print_wrapped(
                "Chunk text",
                _format_chunk_text(row["chunk_text"], chunk_chars, full_chunks),
            )
            print()

    if show_prompt:
        print("Prompt Text")
        prompt_text = _row_value(run, "prompt_text")
        if prompt_text is None:
            print("(not available in this database schema)")
        else:
            print(_terminal_safe(prompt_text) if prompt_text else "(none)")
        print()

    if show_response:
        print("Raw Response Text")
        raw_response_text = _row_value(run, "raw_response_text")
        if raw_response_text is None:
            print("(not available in this database schema)")
        else:
            print(_terminal_safe(raw_response_text) if raw_response_text else "(none)")
        print()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list":
        handle_list(args.limit)
        return

    handle_show(
        generation_id=args.generation_id,
        show_prompt=args.show_prompt,
        show_response=args.show_response,
        full_chunks=args.full_chunks,
        chunk_chars=args.chunk_chars,
    )


if __name__ == "__main__":
    main()
