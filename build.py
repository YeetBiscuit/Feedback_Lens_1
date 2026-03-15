import sqlite3
from pathlib import Path


def initialise_database(
    db_path: str | Path = "feedback_system.db",
    schema_path: str | Path = "schema.sql",
) -> None:
    db_path = Path(db_path)
    schema_path = Path(schema_path)

    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    sql_script = schema_path.read_text(encoding="utf-8")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(sql_script)
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    initialise_database()
    print("Database initialised.")


if __name__ == "__main__":
    main()