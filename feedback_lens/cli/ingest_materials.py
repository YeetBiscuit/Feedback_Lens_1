import argparse

from feedback_lens.db.connection import connect_db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest a unit material file into SQLite and ChromaDB.",
    )
    parser.add_argument("file_path")
    parser.add_argument("unit_id", type=int)
    parser.add_argument("material_type")
    parser.add_argument("title")
    parser.add_argument("week_number", nargs="?", type=int)
    parser.add_argument("--assignment-id", type=int)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    from feedback_lens.file_management.ingestion import ingest_material

    with connect_db() as conn:
        material_id = ingest_material(
            conn,
            args.file_path,
            args.unit_id,
            args.material_type,
            args.title,
            week_number=args.week_number,
            assignment_id=args.assignment_id,
        )

    print(f"Ingested unit material: material_id={material_id}.")


if __name__ == "__main__":
    main()
