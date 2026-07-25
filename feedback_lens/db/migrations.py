from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path


CURRENT_SCHEMA_VERSION = 2
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "setup" / "migrations"


class DatabaseSchemaError(RuntimeError):
    """Raised when a database is not at the schema version required by the app."""


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        is not None
    )


def column_exists(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
) -> bool:
    if not table_exists(conn, table_name):
        return False
    return any(
        row["name"] == column_name
        for row in conn.execute(f'PRAGMA table_info("{table_name}")')
    )


def _ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    if not column_exists(conn, table_name, column_name):
        conn.execute(
            f'ALTER TABLE "{table_name}" '
            f'ADD COLUMN "{column_name}" {column_sql}'
        )


def _ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def get_schema_version(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "schema_migrations"):
        return 0
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
    ).fetchone()
    return int(row["version"] if row is not None else 0)


def require_current_schema(conn: sqlite3.Connection) -> None:
    version = get_schema_version(conn)
    if version == CURRENT_SCHEMA_VERSION:
        return
    if version > CURRENT_SCHEMA_VERSION:
        raise DatabaseSchemaError(
            "This database was created by a newer Feedback Lens version "
            f"(database={version}, application={CURRENT_SCHEMA_VERSION})."
        )
    raise DatabaseSchemaError(
        "Feedback Lens database migration is required "
        f"(database={version}, required={CURRENT_SCHEMA_VERSION}). "
        "Run `python build.py` before starting the application."
    )


def _normalise_duplicate_versions(
    conn: sqlite3.Connection,
    table_name: str,
    primary_key: str,
    partition_columns: tuple[str, ...],
) -> None:
    partition_sql = ", ".join(f'"{column}"' for column in partition_columns)
    rows = conn.execute(
        f"""
        SELECT "{primary_key}" AS row_id, {partition_sql}, version
        FROM "{table_name}"
        ORDER BY {partition_sql}, version, "{primary_key}"
        """
    ).fetchall()

    used_versions: dict[tuple[object, ...], set[int]] = defaultdict(set)
    next_versions: dict[tuple[object, ...], int] = defaultdict(lambda: 1)
    for row in rows:
        partition = tuple(row[column] for column in partition_columns)
        version = int(row["version"])
        next_versions[partition] = max(next_versions[partition], version + 1)
        if version not in used_versions[partition]:
            used_versions[partition].add(version)
            continue

        replacement = next_versions[partition]
        while replacement in used_versions[partition]:
            replacement += 1
        conn.execute(
            f"""
            UPDATE "{table_name}"
            SET version = ?
            WHERE "{primary_key}" = ?
            """,
            (replacement, row["row_id"]),
        )
        used_versions[partition].add(replacement)
        next_versions[partition] = replacement + 1


def _normalise_duplicate_criterion_orders(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT criterion_id, rubric_id, criterion_order
        FROM rubric_criteria
        ORDER BY rubric_id, criterion_order, criterion_id
        """
    ).fetchall()
    used: dict[int, set[int]] = defaultdict(set)
    next_order: dict[int, int] = defaultdict(lambda: 1)
    for row in rows:
        rubric_id = int(row["rubric_id"])
        order = int(row["criterion_order"])
        next_order[rubric_id] = max(next_order[rubric_id], order + 1)
        if order not in used[rubric_id]:
            used[rubric_id].add(order)
            continue
        replacement = next_order[rubric_id]
        while replacement in used[rubric_id]:
            replacement += 1
        conn.execute(
            """
            UPDATE rubric_criteria
            SET criterion_order = ?
            WHERE criterion_id = ?
            """,
            (replacement, row["criterion_id"]),
        )
        used[rubric_id].add(replacement)
        next_order[rubric_id] = replacement + 1


def _migration_001_legacy_stabilization(conn: sqlite3.Connection) -> None:
    required_core_tables = {
        "units",
        "tutors",
        "unit_tutors",
        "assignments",
        "assignment_specs",
        "rubrics",
        "rubric_criteria",
        "unit_materials",
        "student_submissions",
        "material_chunks",
        "chunk_embedding_map",
        "generation_runs",
        "retrieval_records",
        "criterion_feedback",
        "overall_feedback",
        "human_reviews",
    }
    missing = sorted(
        table for table in required_core_tables if not table_exists(conn, table)
    )
    if missing:
        raise DatabaseSchemaError(
            "The legacy Feedback Lens schema is incomplete; missing table(s): "
            + ", ".join(missing)
        )

    legacy_columns = {
        "units": {
            "level": "TEXT",
            "discipline": "TEXT",
            "credit_points": "REAL",
            "weeks": "INTEGER",
            "learning_outcomes_json": "TEXT",
            "faculty": "TEXT",
            "academic_level": "TEXT",
            "is_archived": "INTEGER NOT NULL DEFAULT 0",
        },
        "assignments": {
            "assignment_code": "TEXT",
            "weight": "REAL",
            "due_week": "INTEGER",
            "word_count_or_equivalent": "TEXT",
            "linked_topics_json": "TEXT",
            "learning_outcomes_assessed_json": "TEXT",
        },
        "generation_runs": {
            "llm_provider": "TEXT",
            "prompt_text": "TEXT",
            "raw_response_text": "TEXT",
            "per_cue_top_k": "INTEGER",
            "max_final_chunks": "INTEGER",
        },
        "overall_feedback": {
            "overall_grade_band": "TEXT",
            "final_mark": "REAL",
        },
        "criterion_feedback": {"mark": "REAL"},
        "assignment_specs": {
            "retrieval_cues_json": "TEXT",
            "source_content_hash": "TEXT",
        },
        "rubrics": {"source_content_hash": "TEXT"},
        "unit_materials": {"source_content_hash": "TEXT"},
        "student_submissions": {"source_content_hash": "TEXT"},
    }
    for table_name, columns in legacy_columns.items():
        for column_name, column_sql in columns.items():
            _ensure_column(
                conn,
                table_name,
                column_name,
                column_sql,
            )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL
                CHECK (
                    role IN (
                        'admin', 'lead_lecturer', 'educator', 'student'
                    )
                ),
            display_name TEXT,
            tutor_id INTEGER UNIQUE,
            student_identifier TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_users_tutor
                FOREIGN KEY (tutor_id) REFERENCES tutors(tutor_id)
                ON DELETE SET NULL
        )
        """
    )
    _ensure_column(conn, "users", "student_identifier", "TEXT")

    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_users_updated_at
        AFTER UPDATE ON users
        FOR EACH ROW
        WHEN NEW.updated_at = OLD.updated_at
        BEGIN
            UPDATE users
            SET updated_at = CURRENT_TIMESTAMP
            WHERE user_id = NEW.user_id;
        END
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS curriculum_generation_runs (
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
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS curriculum_generation_steps (
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
                FOREIGN KEY (curriculum_run_id)
                REFERENCES curriculum_generation_runs(curriculum_run_id)
                ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS curriculum_artifacts (
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
                FOREIGN KEY (curriculum_run_id)
                REFERENCES curriculum_generation_runs(curriculum_run_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_curriculum_artifacts_step
                FOREIGN KEY (curriculum_step_id)
                REFERENCES curriculum_generation_steps(curriculum_step_id)
                ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS unit_ingestion_runs (
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
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS unit_ingestion_items (
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
                FOREIGN KEY (ingestion_run_id)
                REFERENCES unit_ingestion_runs(ingestion_run_id)
                ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_planning_records (
            planning_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_id INTEGER NOT NULL,
            strategy TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_template_version TEXT NOT NULL,
            prompt_text TEXT,
            raw_response_text TEXT,
            planned_cues_json TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            error_message TEXT,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            CONSTRAINT fk_retrieval_planning_generation
                FOREIGN KEY (generation_id)
                REFERENCES generation_runs(generation_id)
                ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_feedback_approvals (
            approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_id INTEGER NOT NULL UNIQUE,
            approved_by_user_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status = 'approved'),
            comment TEXT,
            approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_lead_approvals_generation
                FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
                ON DELETE CASCADE,
            CONSTRAINT fk_lead_approvals_user
                FOREIGN KEY (approved_by_user_id) REFERENCES users(user_id)
                ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_lead_feedback_approvals_updated_at
        AFTER UPDATE ON lead_feedback_approvals
        FOR EACH ROW
        WHEN NEW.updated_at = OLD.updated_at
        BEGIN
            UPDATE lead_feedback_approvals
            SET updated_at = CURRENT_TIMESTAMP
            WHERE approval_id = NEW.approval_id;
        END
        """
    )

    indexes = (
        "CREATE INDEX IF NOT EXISTS idx_curriculum_generation_runs_status "
        "ON curriculum_generation_runs(status)",
        "CREATE INDEX IF NOT EXISTS idx_curriculum_generation_runs_course_code "
        "ON curriculum_generation_runs(course_code)",
        "CREATE INDEX IF NOT EXISTS idx_curriculum_generation_steps_run_id "
        "ON curriculum_generation_steps(curriculum_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_curriculum_generation_steps_stage_key "
        "ON curriculum_generation_steps(stage_key)",
        "CREATE INDEX IF NOT EXISTS idx_curriculum_artifacts_run_id "
        "ON curriculum_artifacts(curriculum_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_curriculum_artifacts_type "
        "ON curriculum_artifacts(artifact_type)",
        "CREATE INDEX IF NOT EXISTS idx_unit_ingestion_runs_unit_id "
        "ON unit_ingestion_runs(unit_id)",
        "CREATE INDEX IF NOT EXISTS idx_unit_ingestion_runs_status "
        "ON unit_ingestion_runs(status)",
        "CREATE INDEX IF NOT EXISTS idx_unit_ingestion_items_run_id "
        "ON unit_ingestion_items(ingestion_run_id)",
        "CREATE INDEX IF NOT EXISTS idx_unit_ingestion_items_status "
        "ON unit_ingestion_items(status)",
        "CREATE INDEX IF NOT EXISTS idx_retrieval_planning_generation_id "
        "ON retrieval_planning_records(generation_id)",
        "CREATE INDEX IF NOT EXISTS idx_retrieval_planning_status "
        "ON retrieval_planning_records(status)",
        "CREATE INDEX IF NOT EXISTS idx_users_student_identifier "
        "ON users(student_identifier)",
    )
    for sql in indexes:
        conn.execute(sql)

    _normalise_duplicate_versions(
        conn,
        "assignment_specs",
        "spec_id",
        ("assignment_id",),
    )
    _normalise_duplicate_versions(
        conn,
        "rubrics",
        "rubric_id",
        ("assignment_id",),
    )
    _normalise_duplicate_versions(
        conn,
        "student_submissions",
        "submission_id",
        ("assignment_id", "student_identifier"),
    )
    _normalise_duplicate_criterion_orders(conn)

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_assignment_specs_version
        ON assignment_specs(assignment_id, version)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_rubrics_version
        ON rubrics(assignment_id, version)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_student_submissions_version
        ON student_submissions(assignment_id, student_identifier, version)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_rubric_criteria_order
        ON rubric_criteria(rubric_id, criterion_order)
        """
    )


def _execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    statement_lines: list[str] = []
    for line in script.splitlines():
        statement_lines.append(line)
        statement = "\n".join(statement_lines).strip()
        if not statement or not sqlite3.complete_statement(statement):
            continue
        conn.execute(statement)
        statement_lines = []
    if "\n".join(statement_lines).strip():
        raise DatabaseSchemaError("Database migration contains incomplete SQL.")


def _latest_document_id(
    conn: sqlite3.Connection,
    table_name: str,
    primary_key: str,
    assignment_id: int,
    at_timestamp: str | None = None,
) -> int | None:
    timestamp_filter = ""
    params: list[object] = [assignment_id]
    if at_timestamp:
        timestamp_filter = "AND created_at <= ?"
        params.append(at_timestamp)
    row = conn.execute(
        f"""
        SELECT "{primary_key}" AS document_id
        FROM "{table_name}"
        WHERE assignment_id = ?
          {timestamp_filter}
        ORDER BY version DESC, "{primary_key}" DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None and at_timestamp:
        return _latest_document_id(
            conn,
            table_name,
            primary_key,
            assignment_id,
        )
    return int(row["document_id"]) if row is not None else None


def _backfill_academic_structure(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO organizations
            (organization_code, organization_name)
        VALUES ('default', 'Feedback Lens')
        """
    )
    organization_id = int(
        conn.execute(
            """
            SELECT organization_id
            FROM organizations
            WHERE organization_code = 'default'
            """
        ).fetchone()["organization_id"]
    )

    unit_rows = conn.execute("SELECT * FROM units ORDER BY unit_id").fetchall()
    for unit in unit_rows:
        conn.execute(
            """
            INSERT OR IGNORE INTO courses
                (organization_id, course_code, course_name, faculty,
                 academic_level, discipline, credit_points,
                 learning_outcomes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                organization_id,
                unit["unit_code"],
                unit["unit_name"],
                unit["faculty"],
                unit["academic_level"] or unit["level"],
                unit["discipline"],
                unit["credit_points"],
                unit["learning_outcomes_json"],
            ),
        )
        course = conn.execute(
            """
            SELECT course_id
            FROM courses
            WHERE organization_id = ? AND course_code = ?
            """,
            (organization_id, unit["unit_code"]),
        ).fetchone()
        status = "archived" if int(unit["is_archived"] or 0) else "active"
        conn.execute(
            """
            INSERT OR IGNORE INTO unit_offerings
                (course_id, legacy_unit_id, academic_year, teaching_period,
                 offering_name, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                course["course_id"],
                unit["unit_id"],
                unit["year"],
                unit["semester"],
                unit["unit_name"],
                status,
            ),
        )

    return organization_id


def _backfill_scoped_roles(
    conn: sqlite3.Connection,
    organization_id: int,
) -> None:
    for user in conn.execute("SELECT * FROM users ORDER BY user_id"):
        if user["role"] in {"admin", "lead_lecturer"}:
            conn.execute(
                """
                INSERT OR IGNORE INTO organization_role_assignments
                    (organization_id, user_id, role, assigned_by_user_id)
                VALUES (?, ?, 'chief_admin', NULL)
                """,
                (organization_id, user["user_id"]),
            )

    staff_rows = conn.execute(
        """
        SELECT DISTINCT
            u.user_id,
            ut.unit_id,
            lower(COALESCE(ut.role, 'staff')) AS legacy_role
        FROM unit_tutors AS ut
        JOIN users AS u ON u.tutor_id = ut.tutor_id
        """
    ).fetchall()
    for row in staff_rows:
        offering = conn.execute(
            """
            SELECT unit_offering_id
            FROM unit_offerings
            WHERE legacy_unit_id = ?
            """,
            (row["unit_id"],),
        ).fetchone()
        if offering is None:
            continue
        role = (
            "unit_admin"
            if "admin" in row["legacy_role"] or "lead" in row["legacy_role"]
            else "staff"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO unit_role_assignments
                (unit_offering_id, user_id, role, assigned_by_user_id)
            VALUES (?, ?, ?, NULL)
            """,
            (offering["unit_offering_id"], row["user_id"], role),
        )


def _backfill_students_and_enrolments(conn: sqlite3.Connection) -> None:
    identifiers = {
        row["student_identifier"]
        for row in conn.execute(
            """
            SELECT DISTINCT student_identifier
            FROM student_submissions
            WHERE student_identifier IS NOT NULL
              AND trim(student_identifier) != ''
            """
        )
    }
    identifiers.update(
        row["student_identifier"]
        for row in conn.execute(
            """
            SELECT DISTINCT student_identifier
            FROM users
            WHERE role = 'student'
              AND student_identifier IS NOT NULL
              AND trim(student_identifier) != ''
            """
        )
    )
    for identifier in sorted(identifiers, key=str.casefold):
        conn.execute(
            """
            INSERT OR IGNORE INTO students(institution_student_identifier)
            VALUES (?)
            """,
            (identifier,),
        )

    for user in conn.execute(
        """
        SELECT user_id, student_identifier
        FROM users
        WHERE role = 'student'
          AND student_identifier IS NOT NULL
          AND trim(student_identifier) != ''
        """
    ):
        conn.execute(
            """
            UPDATE students
            SET user_id = COALESCE(user_id, ?)
            WHERE institution_student_identifier = ?
            """,
            (user["user_id"], user["student_identifier"]),
        )

    enrolments = conn.execute(
        """
        SELECT DISTINCT
            ss.student_identifier,
            a.unit_id
        FROM student_submissions AS ss
        JOIN assignments AS a ON a.assignment_id = ss.assignment_id
        """
    ).fetchall()
    for row in enrolments:
        student = conn.execute(
            """
            SELECT student_id
            FROM students
            WHERE institution_student_identifier = ?
            """,
            (row["student_identifier"],),
        ).fetchone()
        offering = conn.execute(
            """
            SELECT unit_offering_id
            FROM unit_offerings
            WHERE legacy_unit_id = ?
            """,
            (row["unit_id"],),
        ).fetchone()
        if student is None or offering is None:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO student_enrolments
                (unit_offering_id, student_id, status, source)
            VALUES (?, ?, 'active', 'legacy_import')
            """,
            (offering["unit_offering_id"], student["student_id"]),
        )


def _backfill_assessment_plans(conn: sqlite3.Connection) -> None:
    assignments = conn.execute(
        "SELECT * FROM assignments ORDER BY assignment_id"
    ).fetchall()
    for assignment in assignments:
        offering = conn.execute(
            """
            SELECT unit_offering_id
            FROM unit_offerings
            WHERE legacy_unit_id = ?
            """,
            (assignment["unit_id"],),
        ).fetchone()
        if offering is None:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO assessment_plans
                (unit_offering_id, legacy_assignment_id, assessment_code,
                 title, description, status, supports_formative,
                 supports_summative)
            VALUES (?, ?, ?, ?, ?, 'active', 1, 1)
            """,
            (
                offering["unit_offering_id"],
                assignment["assignment_id"],
                assignment["assignment_code"],
                assignment["assignment_name"],
                assignment["description"],
            ),
        )
        plan = conn.execute(
            """
            SELECT assessment_plan_id
            FROM assessment_plans
            WHERE legacy_assignment_id = ?
            """,
            (assignment["assignment_id"],),
        ).fetchone()
        spec_id = _latest_document_id(
            conn,
            "assignment_specs",
            "spec_id",
            assignment["assignment_id"],
        )
        rubric_id = _latest_document_id(
            conn,
            "rubrics",
            "rubric_id",
            assignment["assignment_id"],
        )
        configuration = json.dumps(
            {
                "legacy_assignment_version": assignment["version"],
                "weight": assignment["weight"],
                "due_week": assignment["due_week"],
                "word_count_or_equivalent": assignment[
                    "word_count_or_equivalent"
                ],
            },
            ensure_ascii=False,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO assessment_plan_versions
                (assessment_plan_id, version, spec_id, rubric_id,
                 configuration_json, status, activated_at)
            VALUES (?, 1, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """,
            (
                plan["assessment_plan_id"],
                spec_id,
                rubric_id,
                configuration,
            ),
        )
        version = conn.execute(
            """
            SELECT assessment_plan_version_id
            FROM assessment_plan_versions
            WHERE assessment_plan_id = ? AND status = 'active'
            """,
            (plan["assessment_plan_id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT OR IGNORE INTO assessment_activities
                (assessment_plan_version_id, purpose, enabled,
                 maximum_attempts, auto_release_feedback,
                 staff_review_required, admin_confirmation_required,
                 disclaimer_text)
            VALUES (?, 'formative', 0, NULL, 1, 0, 0, ?)
            """,
            (
                version["assessment_plan_version_id"],
                "AI-generated formative feedback is informal and does not "
                "represent a final summative grade.",
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO assessment_activities
                (assessment_plan_version_id, purpose, enabled,
                 maximum_attempts, auto_release_feedback,
                 staff_review_required, admin_confirmation_required)
            VALUES (?, 'summative', 1, NULL, 0, 1, 1)
            """,
            (version["assessment_plan_version_id"],),
        )


def _backfill_index_builds(conn: sqlite3.Connection) -> None:
    groups = conn.execute(
        """
        SELECT DISTINCT
            um.unit_id,
            cem.vector_store_name,
            cem.embedding_model,
            cem.embedding_version
        FROM chunk_embedding_map AS cem
        JOIN material_chunks AS mc ON mc.chunk_id = cem.chunk_id
        JOIN unit_materials AS um ON um.material_id = mc.material_id
        """
    ).fetchall()
    for group in groups:
        offering = conn.execute(
            """
            SELECT unit_offering_id
            FROM unit_offerings
            WHERE legacy_unit_id = ?
            """,
            (group["unit_id"],),
        ).fetchone()
        if offering is None:
            continue
        build = conn.execute(
            """
            SELECT index_build_id
            FROM index_builds
            WHERE unit_offering_id = ?
              AND vector_store_name = ?
              AND embedding_model = ?
              AND embedding_version IS ?
            """,
            (
                offering["unit_offering_id"],
                group["vector_store_name"],
                group["embedding_model"],
                group["embedding_version"],
            ),
        ).fetchone()
        if build is None:
            cur = conn.execute(
                """
                INSERT INTO index_builds
                    (unit_offering_id, vector_store_name, embedding_model,
                     embedding_version, status, completed_at)
                VALUES (?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
                """,
                (
                    offering["unit_offering_id"],
                    group["vector_store_name"],
                    group["embedding_model"],
                    group["embedding_version"],
                ),
            )
            build_id = int(cur.lastrowid)
        else:
            build_id = int(build["index_build_id"])

        item_rows = conn.execute(
            """
            SELECT cem.chunk_id, cem.vector_id
            FROM chunk_embedding_map AS cem
            JOIN material_chunks AS mc ON mc.chunk_id = cem.chunk_id
            JOIN unit_materials AS um ON um.material_id = mc.material_id
            WHERE um.unit_id = ?
              AND cem.vector_store_name = ?
              AND cem.embedding_model = ?
              AND cem.embedding_version IS ?
            """,
            (
                group["unit_id"],
                group["vector_store_name"],
                group["embedding_model"],
                group["embedding_version"],
            ),
        ).fetchall()
        conn.executemany(
            """
            INSERT OR IGNORE INTO index_build_items
                (index_build_id, chunk_id, vector_id)
            VALUES (?, ?, ?)
            """,
            [
                (build_id, row["chunk_id"], row["vector_id"])
                for row in item_rows
            ],
        )


def _backfill_submission_attempts(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT ss.*
        FROM student_submissions AS ss
        ORDER BY
            ss.assignment_id,
            lower(ss.student_identifier),
            ss.version,
            ss.submission_id
        """
    ).fetchall()
    attempt_numbers: dict[tuple[int, str], int] = defaultdict(int)
    for submission in rows:
        plan_version = conn.execute(
            """
            SELECT
                ap.assessment_plan_id,
                apv.assessment_plan_version_id
            FROM assessment_plans AS ap
            JOIN assessment_plan_versions AS apv
              ON apv.assessment_plan_id = ap.assessment_plan_id
             AND apv.status = 'active'
            WHERE ap.legacy_assignment_id = ?
            """,
            (submission["assignment_id"],),
        ).fetchone()
        if plan_version is None:
            continue
        activity = conn.execute(
            """
            SELECT assessment_activity_id
            FROM assessment_activities
            WHERE assessment_plan_version_id = ?
              AND purpose = 'summative'
            """,
            (plan_version["assessment_plan_version_id"],),
        ).fetchone()
        student = conn.execute(
            """
            SELECT student_id
            FROM students
            WHERE institution_student_identifier = ?
            """,
            (submission["student_identifier"],),
        ).fetchone()
        if activity is None or student is None:
            continue

        key = (
            int(plan_version["assessment_plan_id"]),
            str(submission["student_identifier"]).casefold(),
        )
        attempt_numbers[key] += 1
        conn.execute(
            """
            INSERT OR IGNORE INTO submission_attempts
                (assessment_activity_id, legacy_submission_id, purpose,
                 attempt_number, source_version, source_system,
                 source_reference, visibility, status, submitted_at)
            VALUES (?, ?, 'summative', ?, ?, 'legacy_import', ?,
                    'assigned_staff', 'imported', ?)
            """,
            (
                activity["assessment_activity_id"],
                submission["submission_id"],
                attempt_numbers[key],
                submission["version"],
                submission["original_file_path"],
                submission["submitted_at"],
            ),
        )
        attempt = conn.execute(
            """
            SELECT submission_attempt_id
            FROM submission_attempts
            WHERE legacy_submission_id = ?
            """,
            (submission["submission_id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT OR IGNORE INTO submission_participants
                (submission_attempt_id, student_id, participant_role)
            VALUES (?, ?, 'primary')
            """,
            (attempt["submission_attempt_id"], student["student_id"]),
        )
        if submission["original_file_path"]:
            original_path = str(submission["original_file_path"])
            file_name = Path(original_path).name or original_path
            conn.execute(
                """
                INSERT OR IGNORE INTO submission_files
                    (submission_attempt_id, original_file_name,
                     relative_path, storage_path, content_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    attempt["submission_attempt_id"],
                    file_name,
                    file_name,
                    original_path,
                    submission["source_content_hash"],
                ),
            )


def _feedback_review_state(
    conn: sqlite3.Connection,
    generation_id: int,
) -> tuple[str, int | None, str | None]:
    approval = conn.execute(
        """
        SELECT approved_by_user_id, approved_at
        FROM lead_feedback_approvals
        WHERE generation_id = ?
        """,
        (generation_id,),
    ).fetchone()
    if approval is not None:
        return (
            "admin_confirmed",
            int(approval["approved_by_user_id"]),
            approval["approved_at"],
        )

    review = conn.execute(
        """
        SELECT hr.tutor_id, hr.reviewed_at, u.user_id
        FROM human_reviews AS hr
        LEFT JOIN users AS u ON u.tutor_id = hr.tutor_id
        WHERE hr.generation_id = ?
          AND hr.approved = 1
        ORDER BY hr.reviewed_at DESC, hr.review_id DESC
        LIMIT 1
        """,
        (generation_id,),
    ).fetchone()
    if review is not None:
        return (
            "marker_confirmed",
            int(review["user_id"]) if review["user_id"] is not None else None,
            review["reviewed_at"],
        )
    return ("draft", None, None)


def _backfill_feedback_revisions(conn: sqlite3.Connection) -> None:
    generation_rows = conn.execute(
        """
        SELECT
            gr.*,
            ofb.overall_comment,
            ofb.key_strengths,
            ofb.priority_improvements,
            ofb.overall_grade_band,
            ofb.final_mark
        FROM generation_runs AS gr
        JOIN overall_feedback AS ofb ON ofb.generation_id = gr.generation_id
        ORDER BY gr.submission_id, gr.generation_id
        """
    ).fetchall()
    revision_numbers: dict[int, int] = defaultdict(int)
    previous_revisions: dict[int, int] = {}
    current_revisions: dict[int, int] = {}
    current_generations: dict[int, int] = {}
    current_statuses: dict[int, tuple[str, int | None, str | None]] = {}

    for generation in generation_rows:
        attempt = conn.execute(
            """
            SELECT submission_attempt_id
            FROM submission_attempts
            WHERE legacy_submission_id = ?
            """,
            (generation["submission_id"],),
        ).fetchone()
        if attempt is None:
            continue
        attempt_id = int(attempt["submission_attempt_id"])
        revision_numbers[attempt_id] += 1
        status, actor_user_id, status_at = _feedback_review_state(
            conn,
            generation["generation_id"],
        )
        marks = [
            row["mark"]
            for row in conn.execute(
                """
                SELECT mark
                FROM criterion_feedback
                WHERE generation_id = ?
                  AND mark IS NOT NULL
                """,
                (generation["generation_id"],),
            )
        ]
        calculated_total = (
            float(sum(float(mark) for mark in marks))
            if marks
            else None
        )
        final_total = (
            generation["final_mark"]
            if generation["final_mark"] is not None
            else calculated_total
        )
        cur = conn.execute(
            """
            INSERT INTO feedback_revisions
                (submission_attempt_id, generation_id, revision_number,
                 source, status, based_on_revision_id, created_by_user_id,
                 calculated_total_mark, final_total_mark, created_at)
            VALUES (?, ?, ?, 'ai', ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                generation["generation_id"],
                revision_numbers[attempt_id],
                status,
                previous_revisions.get(attempt_id),
                actor_user_id,
                calculated_total,
                final_total,
                generation["completed_at"] or generation["started_at"],
            ),
        )
        revision_id = int(cur.lastrowid)
        previous_revisions[attempt_id] = revision_id
        current_revisions[attempt_id] = revision_id
        current_generations[attempt_id] = int(generation["generation_id"])
        current_statuses[attempt_id] = (status, actor_user_id, status_at)

        criterion_rows = conn.execute(
            """
            SELECT *
            FROM criterion_feedback
            WHERE generation_id = ?
            """,
            (generation["generation_id"],),
        ).fetchall()
        conn.executemany(
            """
            INSERT INTO criterion_feedback_revisions
                (feedback_revision_id, criterion_id, strengths,
                 areas_for_improvement, improvement_suggestion,
                 suggested_level, evidence_summary, mark, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    revision_id,
                    row["criterion_id"],
                    row["strengths"],
                    row["areas_for_improvement"],
                    row["improvement_suggestion"],
                    row["suggested_level"],
                    row["evidence_summary"],
                    row["mark"],
                    row["created_at"],
                )
                for row in criterion_rows
            ],
        )
        conn.execute(
            """
            INSERT INTO overall_feedback_revisions
                (feedback_revision_id, overall_comment, key_strengths,
                 priority_improvements, overall_grade_band, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                generation["overall_comment"],
                generation["key_strengths"],
                generation["priority_improvements"],
                generation["overall_grade_band"],
                generation["completed_at"] or generation["started_at"],
            ),
        )

    attempts = conn.execute(
        """
        SELECT submission_attempt_id, legacy_submission_id
        FROM submission_attempts
        """
    ).fetchall()
    for attempt in attempts:
        attempt_id = int(attempt["submission_attempt_id"])
        latest_run = conn.execute(
            """
            SELECT generation_id, status
            FROM generation_runs
            WHERE submission_id = ?
            ORDER BY generation_id DESC
            LIMIT 1
            """,
            (attempt["legacy_submission_id"],),
        ).fetchone()
        ai_status = "not_started"
        if latest_run is not None:
            if latest_run["status"] == "completed":
                ai_status = "generated"
            elif latest_run["status"] == "failed":
                ai_status = "failed"
            elif latest_run["status"] == "running":
                ai_status = "running"

        revision_id = current_revisions.get(attempt_id)
        marking_status = "not_started"
        marker_user_id = None
        marker_confirmed_at = None
        admin_user_id = None
        admin_confirmed_at = None
        if revision_id is not None:
            status, actor_user_id, status_at = current_statuses[attempt_id]
            marking_status = (
                status
                if status in {"marker_confirmed", "admin_confirmed"}
                else "not_started"
            )
            if status == "marker_confirmed":
                marker_user_id = actor_user_id
                marker_confirmed_at = status_at
            elif status == "admin_confirmed":
                admin_user_id = actor_user_id
                admin_confirmed_at = status_at

            revision = conn.execute(
                """
                SELECT calculated_total_mark, final_total_mark
                FROM feedback_revisions
                WHERE feedback_revision_id = ?
                """,
                (revision_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT OR REPLACE INTO assessment_results
                    (submission_attempt_id, current_feedback_revision_id,
                     calculated_total_mark, final_total_mark,
                     marker_confirmed_by_user_id, marker_confirmed_at,
                     admin_confirmed_by_user_id, admin_confirmed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    revision_id,
                    revision["calculated_total_mark"],
                    revision["final_total_mark"],
                    marker_user_id,
                    marker_confirmed_at,
                    admin_user_id,
                    admin_confirmed_at,
                ),
            )

        conn.execute(
            """
            INSERT OR REPLACE INTO submission_workflow_states
                (submission_attempt_id, allocation_status,
                 ai_generation_status, marking_status,
                 current_generation_id, current_feedback_revision_id,
                 marker_confirmed_by_user_id, marker_confirmed_at,
                 admin_confirmed_by_user_id, admin_confirmed_at)
            VALUES (?, 'unassigned', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                ai_status,
                marking_status,
                current_generations.get(attempt_id),
                revision_id,
                marker_user_id,
                marker_confirmed_at,
                admin_user_id,
                admin_confirmed_at,
            ),
        )


def _backfill_generation_snapshots(conn: sqlite3.Connection) -> None:
    for generation in conn.execute(
        "SELECT * FROM generation_runs ORDER BY generation_id"
    ):
        attempt = conn.execute(
            """
            SELECT
                sa.submission_attempt_id,
                apv.assessment_plan_version_id
            FROM submission_attempts AS sa
            JOIN assessment_activities AS aa
              ON aa.assessment_activity_id = sa.assessment_activity_id
            JOIN assessment_plan_versions AS apv
              ON apv.assessment_plan_version_id =
                 aa.assessment_plan_version_id
            WHERE sa.legacy_submission_id = ?
            """,
            (generation["submission_id"],),
        ).fetchone()
        spec_id = _latest_document_id(
            conn,
            "assignment_specs",
            "spec_id",
            generation["assignment_id"],
            generation["started_at"],
        )
        index_build = conn.execute(
            """
            SELECT ib.index_build_id
            FROM retrieval_records AS rr
            JOIN chunk_embedding_map AS cem ON cem.chunk_id = rr.chunk_id
            JOIN index_builds AS ib
              ON ib.vector_store_name = cem.vector_store_name
             AND ib.embedding_model = cem.embedding_model
             AND ib.embedding_version IS cem.embedding_version
            WHERE rr.generation_id = ?
            ORDER BY rr.retrieval_record_id
            LIMIT 1
            """,
            (generation["generation_id"],),
        ).fetchone()
        prompt_text = generation["prompt_text"] or ""
        snapshot_hash = (
            hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
            if prompt_text
            else None
        )
        configuration = json.dumps(
            {
                "pipeline_version": generation["pipeline_version"],
                "llm_provider": generation["llm_provider"],
                "llm_model": generation["llm_model"],
                "prompt_template_version": generation[
                    "prompt_template_version"
                ],
                "retrieval_strategy": generation["retrieval_strategy"],
                "temperature": generation["temperature"],
                "top_k": generation["top_k"],
                "per_cue_top_k": generation["per_cue_top_k"],
                "max_final_chunks": generation["max_final_chunks"],
            },
            ensure_ascii=False,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO generation_input_snapshots
                (generation_id, submission_attempt_id,
                 assessment_plan_version_id, spec_id, rubric_id,
                 index_build_id, code_version, input_snapshot_hash,
                 generation_configuration_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation["generation_id"],
                attempt["submission_attempt_id"] if attempt else None,
                attempt["assessment_plan_version_id"] if attempt else None,
                spec_id,
                generation["rubric_id"],
                index_build["index_build_id"] if index_build else None,
                generation["pipeline_version"],
                snapshot_hash,
                configuration,
            ),
        )
        conn.execute(
            """
            UPDATE generation_runs
            SET spec_id = COALESCE(spec_id, ?),
                assessment_plan_version_id =
                    COALESCE(assessment_plan_version_id, ?),
                submission_attempt_id =
                    COALESCE(submission_attempt_id, ?),
                feedback_purpose =
                    COALESCE(feedback_purpose, 'summative'),
                code_version = COALESCE(code_version, pipeline_version),
                input_snapshot_hash =
                    COALESCE(input_snapshot_hash, ?)
            WHERE generation_id = ?
            """,
            (
                spec_id,
                attempt["assessment_plan_version_id"] if attempt else None,
                attempt["submission_attempt_id"] if attempt else None,
                snapshot_hash,
                generation["generation_id"],
            ),
        )


def _backfill_retrieval_provenance(conn: sqlite3.Connection) -> None:
    generation_ids = [
        row["generation_id"]
        for row in conn.execute(
            """
            SELECT DISTINCT generation_id
            FROM retrieval_records
            ORDER BY generation_id
            """
        )
    ]
    for generation_id in generation_ids:
        query_groups = conn.execute(
            """
            SELECT
                query_text,
                criterion_id,
                MIN(retrieval_record_id) AS first_record
            FROM retrieval_records
            WHERE generation_id = ?
            GROUP BY query_text, criterion_id
            ORDER BY first_record
            """,
            (generation_id,),
        ).fetchall()
        for query_sequence, query_group in enumerate(query_groups, start=1):
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO retrieval_queries_v2
                    (generation_id, query_sequence, source,
                     criterion_id, query_text)
                VALUES (?, ?, 'legacy', ?, ?)
                """,
                (
                    generation_id,
                    query_sequence,
                    query_group["criterion_id"],
                    query_group["query_text"],
                ),
            )
            if cur.lastrowid:
                query_id = int(cur.lastrowid)
            else:
                query_id = int(
                    conn.execute(
                        """
                        SELECT retrieval_query_id
                        FROM retrieval_queries_v2
                        WHERE generation_id = ? AND query_sequence = ?
                        """,
                        (generation_id, query_sequence),
                    ).fetchone()["retrieval_query_id"]
                )
            hit_rows = conn.execute(
                """
                SELECT rr.*, mc.chunk_text
                FROM retrieval_records AS rr
                JOIN material_chunks AS mc ON mc.chunk_id = rr.chunk_id
                WHERE rr.generation_id = ?
                  AND rr.query_text = ?
                  AND rr.criterion_id IS ?
                ORDER BY rr.rank_position, rr.retrieval_record_id
                """,
                (
                    generation_id,
                    query_group["query_text"],
                    query_group["criterion_id"],
                ),
            ).fetchall()
            seen_chunks: set[int] = set()
            rank = 0
            for hit in hit_rows:
                chunk_id = int(hit["chunk_id"])
                if chunk_id in seen_chunks:
                    continue
                seen_chunks.add(chunk_id)
                rank += 1
                conn.execute(
                    """
                    INSERT OR IGNORE INTO retrieval_hits_v2
                        (retrieval_query_id, chunk_id, rank_position,
                         score, score_metric, used_in_prompt,
                         chunk_content_hash)
                    VALUES (?, ?, ?, ?, 'legacy_similarity', ?, ?)
                    """,
                    (
                        query_id,
                        chunk_id,
                        rank,
                        hit["similarity_score"],
                        hit["used_in_prompt"],
                        hashlib.sha256(
                            hit["chunk_text"].encode("utf-8")
                        ).hexdigest(),
                    ),
                )


def _migration_002_database_v2(conn: sqlite3.Connection) -> None:
    migration_path = MIGRATIONS_DIR / "002_database_v2.sql"
    if not migration_path.exists():
        raise DatabaseSchemaError(
            f"Database migration file is missing: {migration_path}"
        )
    _execute_sql_script(
        conn,
        migration_path.read_text(encoding="utf-8"),
    )

    v2_generation_columns = {
        "spec_id": (
            "INTEGER REFERENCES assignment_specs(spec_id) ON DELETE SET NULL"
        ),
        "assessment_plan_version_id": (
            "INTEGER REFERENCES "
            "assessment_plan_versions(assessment_plan_version_id) "
            "ON DELETE SET NULL"
        ),
        "submission_attempt_id": (
            "INTEGER REFERENCES "
            "submission_attempts(submission_attempt_id) ON DELETE SET NULL"
        ),
        "started_by_user_id": (
            "INTEGER REFERENCES users(user_id) ON DELETE SET NULL"
        ),
        "feedback_purpose": "TEXT",
        "feedback_modifier_mode": "TEXT",
        "feedback_length": "TEXT",
        "feedback_tone": "TEXT",
        "code_version": "TEXT",
        "input_snapshot_hash": "TEXT",
    }
    for column_name, column_sql in v2_generation_columns.items():
        _ensure_column(
            conn,
            "generation_runs",
            column_name,
            column_sql,
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_generation_runs_submission_attempt
        ON generation_runs(submission_attempt_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_generation_runs_plan_version
        ON generation_runs(assessment_plan_version_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_generation_runs_spec
        ON generation_runs(spec_id)
        """
    )

    organization_id = _backfill_academic_structure(conn)
    _backfill_scoped_roles(conn, organization_id)
    _backfill_students_and_enrolments(conn)
    _backfill_assessment_plans(conn)
    _backfill_index_builds(conn)
    _backfill_submission_attempts(conn)
    _backfill_feedback_revisions(conn)
    _backfill_generation_snapshots(conn)
    _backfill_retrieval_provenance(conn)


def _migration_checksum(version: int) -> str:
    if version == 1:
        content = b"001_legacy_stabilization_v1"
    elif version == 2:
        migration_path = MIGRATIONS_DIR / "002_database_v2.sql"
        content = (
            migration_path.read_bytes()
            + b"\n002_database_v2_backfill_v1"
        )
    else:
        raise ValueError(f"Unknown migration version: {version}")
    return hashlib.sha256(content).hexdigest()


MIGRATIONS = (
    (1, "legacy_stabilization", _migration_001_legacy_stabilization),
    (2, "database_v2", _migration_002_database_v2),
)


def migrate_database(conn: sqlite3.Connection) -> int:
    """Apply pending, immutable database migrations exactly once."""

    if conn.row_factory is None:
        conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    _ensure_schema_migrations_table(conn)
    applied_rows = conn.execute(
        """
        SELECT version, name, checksum
        FROM schema_migrations
        ORDER BY version
        """
    ).fetchall()
    applied = {int(row["version"]): row for row in applied_rows}

    for version, name, migration in MIGRATIONS:
        checksum = _migration_checksum(version)
        existing = applied.get(version)
        if existing is not None:
            if existing["name"] != name or existing["checksum"] != checksum:
                raise DatabaseSchemaError(
                    "Applied database migration does not match the "
                    f"application definition: version {version}."
                )
            continue

        conn.execute(f"SAVEPOINT feedback_lens_migration_{version}")
        try:
            migration(conn)
            conn.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum)
                VALUES (?, ?, ?)
                """,
                (version, name, checksum),
            )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                first = violations[0]
                raise DatabaseSchemaError(
                    "Database migration created a foreign-key violation: "
                    f"{tuple(first)}"
                )
            conn.execute(f"RELEASE feedback_lens_migration_{version}")
        except Exception:
            conn.execute(
                f"ROLLBACK TO feedback_lens_migration_{version}"
            )
            conn.execute(f"RELEASE feedback_lens_migration_{version}")
            raise

    require_current_schema(conn)
    return CURRENT_SCHEMA_VERSION
