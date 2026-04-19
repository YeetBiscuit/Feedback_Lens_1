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


def ensure_table(conn: sqlite3.Connection, table_name: str, table_sql: str) -> bool:
    if table_exists(conn, table_name):
        return False

    conn.execute(table_sql)
    return True


def ensure_schema_updates(conn: sqlite3.Connection) -> None:
    changed = False
    changed |= ensure_column(conn, "units", "level", "TEXT")
    changed |= ensure_column(conn, "units", "discipline", "TEXT")
    changed |= ensure_column(conn, "units", "credit_points", "REAL")
    changed |= ensure_column(conn, "units", "weeks", "INTEGER")
    changed |= ensure_column(conn, "units", "learning_outcomes_json", "TEXT")
    changed |= ensure_column(conn, "assignments", "assignment_code", "TEXT")
    changed |= ensure_column(conn, "assignments", "weight", "REAL")
    changed |= ensure_column(conn, "assignments", "due_week", "INTEGER")
    changed |= ensure_column(conn, "assignments", "word_count_or_equivalent", "TEXT")
    changed |= ensure_column(conn, "assignments", "linked_topics_json", "TEXT")
    changed |= ensure_column(
        conn,
        "assignments",
        "learning_outcomes_assessed_json",
        "TEXT",
    )
    changed |= ensure_column(conn, "generation_runs", "llm_provider", "TEXT")
    changed |= ensure_column(conn, "generation_runs", "prompt_text", "TEXT")
    changed |= ensure_column(conn, "generation_runs", "raw_response_text", "TEXT")
    changed |= ensure_column(conn, "overall_feedback", "overall_grade_band", "TEXT")
    changed |= ensure_column(conn, "assignment_specs", "retrieval_cues_json", "TEXT")
    changed |= ensure_column(conn, "assignment_specs", "source_content_hash", "TEXT")
    changed |= ensure_column(conn, "rubrics", "source_content_hash", "TEXT")
    changed |= ensure_column(conn, "unit_materials", "source_content_hash", "TEXT")
    changed |= ensure_column(conn, "student_submissions", "source_content_hash", "TEXT")
    changed |= ensure_table(
        conn,
        "curriculum_generation_runs",
        """
        CREATE TABLE curriculum_generation_runs (
            curriculum_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_description TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            temperature REAL,
            course_code TEXT,
            output_root TEXT,
            schema_json TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            error_message TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        )
        """,
    )
    changed |= ensure_table(
        conn,
        "curriculum_generation_steps",
        """
        CREATE TABLE curriculum_generation_steps (
            curriculum_step_id INTEGER PRIMARY KEY AUTOINCREMENT,
            curriculum_run_id INTEGER NOT NULL,
            stage_key TEXT NOT NULL,
            assignment_code TEXT,
            week_number INTEGER,
            grade_band TEXT,
            prompt_messages_json TEXT NOT NULL,
            raw_response TEXT,
            parsed_output_json TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            error_message TEXT,
            locked_at TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            CONSTRAINT fk_curriculum_generation_steps_run
                FOREIGN KEY (curriculum_run_id) REFERENCES curriculum_generation_runs(curriculum_run_id)
                ON DELETE CASCADE
        )
        """,
    )
    changed |= ensure_table(
        conn,
        "curriculum_artifacts",
        """
        CREATE TABLE curriculum_artifacts (
            curriculum_artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            curriculum_run_id INTEGER NOT NULL,
            curriculum_step_id INTEGER,
            artifact_type TEXT NOT NULL,
            title TEXT NOT NULL,
            file_path TEXT NOT NULL,
            content_hash TEXT,
            text_content TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_curriculum_artifacts_run
                FOREIGN KEY (curriculum_run_id) REFERENCES curriculum_generation_runs(curriculum_run_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_curriculum_artifacts_step
                FOREIGN KEY (curriculum_step_id) REFERENCES curriculum_generation_steps(curriculum_step_id)
                ON DELETE SET NULL
        )
        """,
    )
    changed |= ensure_table(
        conn,
        "unit_ingestion_runs",
        """
        CREATE TABLE unit_ingestion_runs (
            ingestion_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER,
            unit_directory TEXT NOT NULL,
            dry_run INTEGER NOT NULL DEFAULT 0,
            force INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'running',
            summary_json TEXT,
            error_message TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            CONSTRAINT fk_unit_ingestion_runs_unit
                FOREIGN KEY (unit_id) REFERENCES units(unit_id)
                ON DELETE SET NULL
        )
        """,
    )
    changed |= ensure_table(
        conn,
        "unit_ingestion_items",
        """
        CREATE TABLE unit_ingestion_items (
            ingestion_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingestion_run_id INTEGER NOT NULL,
            item_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            source_content_hash TEXT,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            assignment_id INTEGER,
            spec_id INTEGER,
            rubric_id INTEGER,
            material_id INTEGER,
            submission_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_unit_ingestion_items_run
                FOREIGN KEY (ingestion_run_id) REFERENCES unit_ingestion_runs(ingestion_run_id)
                ON DELETE CASCADE
        )
        """,
    )

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
