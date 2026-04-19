import argparse
import sqlite3

from feedback_lens.db.connection import connect_db
from feedback_lens.file_management.unit_auto_ingestion import ingest_unit_directory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Auto-ingest a unit-level document directory into Feedback Lens.",
    )
    parser.add_argument("unit_directory")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report what would be imported without writing to SQLite or Chroma.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Import every recognized file as a new version even when hashes match.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    connection = sqlite3.connect(":memory:") if args.dry_run else connect_db()
    connection.row_factory = sqlite3.Row
    with connection as conn:
        result = ingest_unit_directory(
            conn,
            args.unit_directory,
            dry_run=args.dry_run,
            force=args.force,
        )

    summary = result.summary()
    print(
        f"Unit ingest {'dry run' if result.dry_run else 'complete'} for "
        f"{summary['course_code']}: planned={summary['planned']}, "
        f"imported={summary['imported']}, skipped={summary['skipped']}, "
        f"failed={summary['failed']}."
    )
    if summary["submission_ids"]:
        joined_ids = ", ".join(str(item) for item in summary["submission_ids"])
        print(f"Imported submission_id values: {joined_ids}")

    for item in result.items:
        if item.status == "failed":
            print(f"FAILED {item.item_type}: {item.file_path} - {item.message}")


if __name__ == "__main__":
    main()
