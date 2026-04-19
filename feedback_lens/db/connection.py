import sqlite3
from pathlib import Path

from feedback_lens.paths import DB_PATH


def connect_db(
    db_path: str | Path = DB_PATH,
    ensure_updates: bool = True,
) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if ensure_updates:
        ensure_schema_updates(conn)
    return conn


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    if not table_exists(conn, table_name):
        return False

    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return any(row["name"] == column_name for row in rows)


def ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> bool:
    if column_exists(conn, table_name, column_name):
        return False

    conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_sql}')
    return True


def ensure_schema_updates(conn: sqlite3.Connection) -> None:
    changed = False
    changed |= ensure_column(conn, "generation_runs", "llm_provider", "TEXT")
    changed |= ensure_column(conn, "generation_runs", "prompt_text", "TEXT")
    changed |= ensure_column(conn, "generation_runs", "raw_response_text", "TEXT")
    changed |= ensure_column(conn, "overall_feedback", "overall_grade_band", "TEXT")
    changed |= ensure_column(conn, "assignment_specs", "retrieval_cues_json", "TEXT")

    if changed:
        conn.commit()


def get_next_version(
    conn: sqlite3.Connection,
    table_name: str,
    foreign_key_column: str,
    foreign_key_value: object,
    partition_column: str | None = None,
    partition_value: object | None = None,
) -> int:
    params = [foreign_key_value]
    query = (
        f'SELECT COALESCE(MAX(version), 0) + 1 AS next_version '
        f'FROM "{table_name}" WHERE "{foreign_key_column}" = ?'
    )

    if partition_column is not None:
        query += f' AND "{partition_column}" = ?'
        params.append(partition_value)

    row = conn.execute(query, params).fetchone()
    return int(row["next_version"])


def fetch_latest_version_row(
    conn: sqlite3.Connection,
    table_name: str,
    foreign_key_column: str,
    foreign_key_value: object,
) -> sqlite3.Row | None:
    return conn.execute(
        f'''
        SELECT *
        FROM "{table_name}"
        WHERE "{foreign_key_column}" = ?
        ORDER BY version DESC, rowid DESC
        LIMIT 1
        ''',
        (foreign_key_value,),
    ).fetchone()
