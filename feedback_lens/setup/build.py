import shutil
import sqlite3
from pathlib import Path

from feedback_lens.db.connection import ensure_schema_updates
from feedback_lens.paths import CHROMA_DIR, DB_PATH, SCHEMA_PATH


def initialise_database(
    db_path: str | Path = DB_PATH,
    schema_path: str | Path = SCHEMA_PATH,
) -> None:
    db_path = Path(db_path)
    schema_path = Path(schema_path)

    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    sql_script = schema_path.read_text(encoding="utf-8")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        existing_table = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        ).fetchone()
        if existing_table is None:
            conn.executescript(sql_script)
        ensure_schema_updates(conn)
        conn.commit()
    finally:
        conn.close()


def _remove_sqlite_sidecars(db_path: Path) -> None:
    sidecar_paths = [
        db_path,
        db_path.with_name(f"{db_path.name}-journal"),
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    ]

    for path in sidecar_paths:
        if path.exists():
            path.unlink()


def reset_feedback_system(
    db_path: str | Path = DB_PATH,
    schema_path: str | Path = SCHEMA_PATH,
    chroma_dir: str | Path = CHROMA_DIR,
) -> None:
    db_path = Path(db_path)
    chroma_dir = Path(chroma_dir)

    if chroma_dir.exists():
        shutil.rmtree(chroma_dir)

    if db_path.parent != Path():
        db_path.parent.mkdir(parents=True, exist_ok=True)

    _remove_sqlite_sidecars(db_path)
    initialise_database(db_path=db_path, schema_path=schema_path)
    chroma_dir.mkdir(parents=True, exist_ok=True)


def main() -> None:
    initialise_database()
    print("Database initialised.")


if __name__ == "__main__":
    main()
