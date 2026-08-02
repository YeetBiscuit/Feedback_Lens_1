import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash

from feedback_lens.db.migrations import (
    require_current_schema,
    table_exists,
)
from feedback_lens.paths import DB_PATH


def connect_db(
    db_path: str | Path = DB_PATH,
    ensure_updates: bool = True,
) -> sqlite3.Connection:
    """
    Open a Feedback Lens database connection.

    ``ensure_updates`` is retained for backwards compatibility with existing
    callers. It now validates the formal schema version and never mutates the
    schema. Database creation and upgrades happen explicitly through
    ``feedback_lens.setup.build.initialise_database``.
    """

    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    if ensure_updates:
        try:
            require_current_schema(conn)
        except Exception:
            conn.close()
            raise
    return conn


def ensure_schema_updates(conn: sqlite3.Connection) -> None:
    """
    Backwards-compatible schema guard.

    This function intentionally performs no CREATE TABLE or ALTER TABLE work.
    Call ``initialise_database`` to apply versioned migrations.
    """

    require_current_schema(conn)


def is_default_db_path(db_path: str | Path) -> bool:
    if str(db_path) == ":memory:" or str(db_path).startswith("file:"):
        return False
    return Path(db_path).resolve() == DB_PATH.resolve()


def seed_local_demo_accounts(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "users"):
        return
    if not all(
        table_exists(conn, table)
        for table in ("tutors", "unit_tutors", "units")
    ):
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO tutors
            (institution_identifier, full_name, email)
        VALUES (?, ?, ?)
        """,
        ("DEV-TUTOR-001", "Demo Educator", "educator@test.com"),
    )
    tutor = conn.execute(
        """
        SELECT tutor_id
        FROM tutors
        WHERE institution_identifier = ?
        """,
        ("DEV-TUTOR-001",),
    ).fetchone()
    tutor_id = tutor["tutor_id"] if tutor else None

    conn.execute(
        """
        INSERT OR IGNORE INTO users
            (email, password_hash, role, display_name, tutor_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "admin@test.com",
            generate_password_hash("123456"),
            "admin",
            "Demo Admin",
            None,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO users
            (email, password_hash, role, display_name, tutor_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "educator@test.com",
            generate_password_hash("123456"),
            "educator",
            "Demo Educator",
            tutor_id,
        ),
    )
    if tutor_id is not None:
        conn.execute(
            """
            UPDATE users
            SET tutor_id = COALESCE(tutor_id, ?),
                display_name = COALESCE(display_name, ?)
            WHERE lower(email) = lower(?)
              AND role = 'educator'
            """,
            (tutor_id, "Demo Educator", "educator@test.com"),
        )

        unit_rows = conn.execute("SELECT unit_id FROM units").fetchall()
        conn.executemany(
            """
            INSERT OR IGNORE INTO unit_tutors (unit_id, tutor_id, role)
            VALUES (?, ?, ?)
            """,
            [
                (row["unit_id"], tutor_id, "educator")
                for row in unit_rows
            ],
        )

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
        f"""
        SELECT *
        FROM "{table_name}"
        WHERE "{foreign_key_column}" = ?
        ORDER BY version DESC, rowid DESC
        LIMIT 1
        """,
        (foreign_key_value,),
    ).fetchone()
