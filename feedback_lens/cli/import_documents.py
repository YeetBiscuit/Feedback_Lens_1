import argparse

from feedback_lens.db.connection import connect_db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import assignment specs, rubrics, and student submissions into Feedback Lens.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    spec_parser = subparsers.add_parser(
        "assignment-spec",
        help="Import an assignment specification from a PDF or TXT file.",
    )
    spec_parser.add_argument("assignment_id", type=int)
    spec_parser.add_argument("file_path")

    rubric_parser = subparsers.add_parser(
        "rubric",
        help="Import a rubric PDF and extract rubric criteria from its tables.",
    )
    rubric_parser.add_argument("assignment_id", type=int)
    rubric_parser.add_argument("file_path")

    submission_parser = subparsers.add_parser(
        "submission",
        help="Import a student submission from a PDF or TXT file.",
    )
    submission_parser.add_argument("assignment_id", type=int)
    submission_parser.add_argument("student_identifier")
    submission_parser.add_argument("file_path")
    submission_parser.add_argument("--submitted-at")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    with connect_db() as conn:
        if args.command == "assignment-spec":
            from feedback_lens.file_management.importers import import_assignment_spec

            result = import_assignment_spec(conn, args.assignment_id, args.file_path)
            print(
                f"Imported assignment spec: spec_id={result['spec_id']} "
                f"(assignment_id={result['assignment_id']}, version={result['version']}, "
                f"retrieval_cues={result['cue_count']})."
            )
        elif args.command == "rubric":
            from feedback_lens.file_management.importers import import_rubric

            result = import_rubric(conn, args.assignment_id, args.file_path)
            print(
                f"Imported rubric: rubric_id={result['rubric_id']} "
                f"(assignment_id={result['assignment_id']}, version={result['version']}, "
                f"tables={result['table_count']}, criteria={result['criteria_count']})."
            )
        elif args.command == "submission":
            from feedback_lens.file_management.importers import import_student_submission

            result = import_student_submission(
                conn,
                args.assignment_id,
                args.student_identifier,
                args.file_path,
                submitted_at=args.submitted_at,
            )
            print(
                f"Imported submission: submission_id={result['submission_id']} "
                f"(assignment_id={result['assignment_id']}, student='{result['student_identifier']}', "
                f"version={result['version']})."
            )


if __name__ == "__main__":
    main()
