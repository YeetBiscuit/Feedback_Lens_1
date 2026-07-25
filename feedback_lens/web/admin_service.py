from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

from feedback_lens.web.common import record_audit_event
from feedback_lens.web.errors import ApiError
from feedback_lens.web.security import can_administer_unit, is_chief_admin
from feedback_lens.web.storage import StoredUpload, remove_stored_upload


def _organization_for_chief(
    conn: sqlite3.Connection,
    user_id: int,
) -> int:
    row = conn.execute(
        """
        SELECT organization_id
        FROM organization_role_assignments
        WHERE user_id = ?
          AND role = 'chief_admin'
          AND active = 1
        ORDER BY organization_id
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        raise ApiError(
            "chief_admin_required",
            "Only a Chief Admin can create a Unit.",
            403,
        )
    return int(row["organization_id"])


def list_admin_units(
    conn: sqlite3.Connection,
    user_id: int,
) -> list[dict]:
    chief = is_chief_admin(conn, user_id)
    rows = conn.execute(
        """
        SELECT
            offering.unit_offering_id,
            offering.status,
            offering.academic_year,
            offering.teaching_period,
            offering.legacy_unit_id,
            course.course_code,
            course.course_name,
            course.faculty,
            course.academic_level,
            (
                SELECT COUNT(*)
                FROM assessment_plans AS plan
                WHERE plan.unit_offering_id = offering.unit_offering_id
                  AND plan.status != 'archived'
            ) AS assessment_count,
            (
                SELECT COUNT(*)
                FROM student_enrolments AS enrolment
                WHERE enrolment.unit_offering_id =
                      offering.unit_offering_id
                  AND enrolment.status = 'active'
            ) AS active_student_count
        FROM unit_offerings AS offering
        JOIN courses AS course ON course.course_id = offering.course_id
        WHERE offering.status != 'archived'
          AND (
              ? = 1
              OR EXISTS (
                  SELECT 1
                  FROM unit_role_assignments AS role
                  WHERE role.unit_offering_id =
                        offering.unit_offering_id
                    AND role.user_id = ?
                    AND role.role = 'unit_admin'
                    AND role.active = 1
              )
          )
        ORDER BY course.course_code, offering.academic_year DESC
        """,
        (1 if chief else 0, user_id),
    ).fetchall()
    return [dict(row) for row in rows]


def list_unit_admin_candidates(
    conn: sqlite3.Connection,
    user_id: int,
) -> list[dict]:
    if not is_chief_admin(conn, user_id):
        raise ApiError(
            "chief_admin_required",
            "Only a Chief Admin can assign Unit Admins.",
            403,
        )
    rows = conn.execute(
        """
        SELECT user_id, email, display_name, role
        FROM users
        WHERE role IN ('admin', 'lead_lecturer', 'educator')
        ORDER BY lower(COALESCE(display_name, email))
        """
    ).fetchall()
    return [dict(row) for row in rows]


def create_unit(
    conn: sqlite3.Connection,
    actor_user_id: int,
    data: dict,
) -> dict:
    organization_id = _organization_for_chief(conn, actor_user_id)
    course_code = str(data.get("course_code") or "").strip()
    course_name = str(data.get("course_name") or "").strip()
    teaching_period = str(data.get("teaching_period") or "").strip()
    unit_admin_user_id = data.get("unit_admin_user_id")
    try:
        academic_year = int(data.get("academic_year"))
        unit_admin_user_id = int(unit_admin_user_id)
    except (TypeError, ValueError) as exc:
        raise ApiError(
            "invalid_unit",
            "Academic year and Unit Admin are required.",
            422,
        ) from exc
    if not course_code or not course_name or not teaching_period:
        raise ApiError(
            "invalid_unit",
            "Unit code, name, year, and teaching period are required.",
            422,
        )
    candidate = conn.execute(
        "SELECT role FROM users WHERE user_id = ?",
        (unit_admin_user_id,),
    ).fetchone()
    if (
        candidate is None
        or candidate["role"] not in {"admin", "lead_lecturer", "educator"}
    ):
        raise ApiError(
            "unit_admin_not_found",
            "Choose an eligible staff account as Unit Admin.",
            422,
        )

    try:
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO courses
                (organization_id, course_code, course_name,
                 faculty, academic_level)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(organization_id, course_code)
            DO UPDATE SET
                course_name = excluded.course_name,
                faculty = COALESCE(excluded.faculty, courses.faculty),
                academic_level = COALESCE(
                    excluded.academic_level,
                    courses.academic_level
                )
            """,
            (
                organization_id,
                course_code,
                course_name,
                (str(data.get("faculty")).strip()
                 if data.get("faculty") else None),
                (str(data.get("academic_level")).strip()
                 if data.get("academic_level") else None),
            ),
        )
        course_id = int(
            conn.execute(
                """
                SELECT course_id
                FROM courses
                WHERE organization_id = ?
                  AND course_code = ?
                """,
                (organization_id, course_code),
            ).fetchone()["course_id"]
        )
        legacy_unit_id = int(
            conn.execute(
                """
                INSERT INTO units
                    (unit_code, unit_name, semester, year,
                     level, discipline, credit_points)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    course_code,
                    course_name,
                    teaching_period,
                    academic_year,
                    data.get("academic_level"),
                    data.get("discipline"),
                    data.get("credit_points"),
                ),
            ).lastrowid
        )
        offering_id = int(
            conn.execute(
                """
                INSERT INTO unit_offerings
                    (course_id, legacy_unit_id, academic_year,
                     teaching_period, offering_name, status)
                VALUES (?, ?, ?, ?, ?, 'active')
                """,
                (
                    course_id,
                    legacy_unit_id,
                    academic_year,
                    teaching_period,
                    f"{course_code} {academic_year} {teaching_period}",
                ),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO unit_role_assignments
                (unit_offering_id, user_id, role, assigned_by_user_id)
            VALUES (?, ?, 'unit_admin', ?)
            ON CONFLICT(unit_offering_id, user_id, role)
            DO UPDATE SET
                active = 1,
                ended_at = NULL,
                assigned_by_user_id = excluded.assigned_by_user_id
            """,
            (offering_id, unit_admin_user_id, actor_user_id),
        )
        _sync_legacy_unit_admin(
            conn,
            legacy_unit_id,
            unit_admin_user_id,
        )
        record_audit_event(
            conn,
            "unit.created",
            "unit_offering",
            offering_id,
            actor_user_id=actor_user_id,
            metadata={"unit_admin_user_id": unit_admin_user_id},
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ApiError(
            "unit_conflict",
            "This Unit offering already exists.",
            409,
        ) from exc
    return get_unit_detail(conn, actor_user_id, offering_id)


def _sync_legacy_unit_admin(
    conn: sqlite3.Connection,
    legacy_unit_id: int,
    user_id: int,
) -> None:
    user = conn.execute(
        "SELECT tutor_id FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if user is not None and user["tutor_id"] is not None:
        conn.execute(
            """
            INSERT INTO unit_tutors(unit_id, tutor_id, role)
            VALUES (?, ?, 'unit_admin')
            ON CONFLICT(unit_id, tutor_id)
            DO UPDATE SET role = 'unit_admin'
            """,
            (legacy_unit_id, user["tutor_id"]),
        )


def assign_unit_admin(
    conn: sqlite3.Connection,
    actor_user_id: int,
    unit_offering_id: int,
    unit_admin_user_id: int,
) -> None:
    if not is_chief_admin(conn, actor_user_id):
        raise ApiError(
            "chief_admin_required",
            "Only a Chief Admin can assign Unit Admins.",
            403,
        )
    offering = conn.execute(
        """
        SELECT legacy_unit_id
        FROM unit_offerings
        WHERE unit_offering_id = ?
        """,
        (unit_offering_id,),
    ).fetchone()
    if offering is None:
        raise ApiError("unit_not_found", "Unit not found.", 404)
    candidate = conn.execute(
        "SELECT role FROM users WHERE user_id = ?",
        (unit_admin_user_id,),
    ).fetchone()
    if (
        candidate is None
        or candidate["role"] not in {"admin", "lead_lecturer", "educator"}
    ):
        raise ApiError(
            "unit_admin_not_found",
            "Choose an eligible staff account as Unit Admin.",
            422,
        )
    conn.execute(
        """
        INSERT INTO unit_role_assignments
            (unit_offering_id, user_id, role, assigned_by_user_id)
        VALUES (?, ?, 'unit_admin', ?)
        ON CONFLICT(unit_offering_id, user_id, role)
        DO UPDATE SET
            active = 1,
            ended_at = NULL,
            assigned_by_user_id = excluded.assigned_by_user_id
        """,
        (unit_offering_id, unit_admin_user_id, actor_user_id),
    )
    if offering["legacy_unit_id"] is not None:
        _sync_legacy_unit_admin(
            conn,
            int(offering["legacy_unit_id"]),
            unit_admin_user_id,
        )
    record_audit_event(
        conn,
        "unit.admin_assigned",
        "unit_offering",
        unit_offering_id,
        actor_user_id=actor_user_id,
        metadata={"unit_admin_user_id": unit_admin_user_id},
    )
    conn.commit()


def get_unit_detail(
    conn: sqlite3.Connection,
    user_id: int,
    unit_offering_id: int,
) -> dict:
    if not can_administer_unit(conn, user_id, unit_offering_id):
        raise ApiError(
            "unit_forbidden",
            "You are not authorised to manage this Unit.",
            403,
        )
    unit = conn.execute(
        """
        SELECT
            offering.unit_offering_id,
            offering.legacy_unit_id,
            offering.academic_year,
            offering.teaching_period,
            offering.status,
            course.course_code,
            course.course_name,
            course.faculty,
            course.academic_level
        FROM unit_offerings AS offering
        JOIN courses AS course ON course.course_id = offering.course_id
        WHERE offering.unit_offering_id = ?
        """,
        (unit_offering_id,),
    ).fetchone()
    if unit is None:
        raise ApiError("unit_not_found", "Unit not found.", 404)
    assessments = conn.execute(
        """
        SELECT
            plan.assessment_plan_id,
            plan.assessment_code,
            plan.title,
            plan.status,
            plan.legacy_assignment_id,
            (
                SELECT version
                FROM assessment_plan_versions AS version
                WHERE version.assessment_plan_id =
                      plan.assessment_plan_id
                  AND version.status = 'active'
            ) AS active_version,
            (
                SELECT COUNT(*)
                FROM submission_batches AS batch
                JOIN assessment_activities AS activity
                  ON activity.assessment_activity_id =
                     batch.assessment_activity_id
                JOIN assessment_plan_versions AS version
                  ON version.assessment_plan_version_id =
                     activity.assessment_plan_version_id
                WHERE version.assessment_plan_id =
                      plan.assessment_plan_id
            ) AS batch_count
        FROM assessment_plans AS plan
        WHERE plan.unit_offering_id = ?
          AND plan.status != 'archived'
        ORDER BY plan.created_at
        """,
        (unit_offering_id,),
    ).fetchall()
    admins = conn.execute(
        """
        SELECT user.user_id, user.email, user.display_name
        FROM unit_role_assignments AS role
        JOIN users AS user ON user.user_id = role.user_id
        WHERE role.unit_offering_id = ?
          AND role.role = 'unit_admin'
          AND role.active = 1
        ORDER BY lower(COALESCE(user.display_name, user.email))
        """,
        (unit_offering_id,),
    ).fetchall()
    rosters = conn.execute(
        """
        SELECT *
        FROM roster_imports
        WHERE unit_offering_id = ?
        ORDER BY roster_import_id DESC
        """,
        (unit_offering_id,),
    ).fetchall()
    notes = conn.execute(
        """
        SELECT
            material.material_id,
            material.title,
            material.source_file_path,
            material.created_at,
            material.is_active,
            material.deactivated_at
        FROM unit_materials AS material
        WHERE material.unit_id = ?
          AND material.assignment_id IS NULL
          AND material.material_type = 'scoping_note'
        ORDER BY material.material_id DESC
        """,
        (unit["legacy_unit_id"],),
    ).fetchall()
    return {
        "unit": dict(unit),
        "assessments": [dict(row) for row in assessments],
        "unit_admins": [dict(row) for row in admins],
        "roster_imports": [dict(row) for row in rosters],
        "scoping_notes": [dict(row) for row in notes],
        "is_chief_admin": is_chief_admin(conn, user_id),
    }


def create_assessment(
    conn: sqlite3.Connection,
    actor_user_id: int,
    unit_offering_id: int,
    data: dict,
) -> dict:
    if not can_administer_unit(conn, actor_user_id, unit_offering_id):
        raise ApiError(
            "unit_forbidden",
            "You are not authorised to manage this Unit.",
            403,
        )
    title = str(data.get("title") or "").strip()
    code = str(data.get("assessment_code") or "").strip() or None
    if not title:
        raise ApiError(
            "invalid_assessment",
            "Assessment title is required.",
            422,
        )
    offering = conn.execute(
        """
        SELECT legacy_unit_id
        FROM unit_offerings
        WHERE unit_offering_id = ?
        """,
        (unit_offering_id,),
    ).fetchone()
    if offering is None or offering["legacy_unit_id"] is None:
        raise ApiError("unit_not_found", "Unit not found.", 404)
    try:
        conn.execute("BEGIN")
        legacy_assignment_id = int(
            conn.execute(
                """
                INSERT INTO assignments
                    (unit_id, assignment_name, assignment_code,
                     assignment_type, description, due_date, weight)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    offering["legacy_unit_id"],
                    title,
                    code,
                    data.get("assignment_type") or "report",
                    data.get("description"),
                    data.get("due_date"),
                    data.get("weight"),
                ),
            ).lastrowid
        )
        plan_id = int(
            conn.execute(
                """
                INSERT INTO assessment_plans
                    (unit_offering_id, legacy_assignment_id,
                     assessment_code, title, description, status,
                     created_by_user_id)
                VALUES (?, ?, ?, ?, ?, 'draft', ?)
                """,
                (
                    unit_offering_id,
                    legacy_assignment_id,
                    code,
                    title,
                    data.get("description"),
                    actor_user_id,
                ),
            ).lastrowid
        )
        record_audit_event(
            conn,
            "assessment.created",
            "assessment_plan",
            plan_id,
            actor_user_id=actor_user_id,
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ApiError(
            "assessment_conflict",
            "An assessment with this code already exists.",
            409,
        ) from exc
    return get_assessment_detail(conn, actor_user_id, plan_id)


def get_assessment_detail(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
) -> dict:
    plan = conn.execute(
        """
        SELECT
            plan.*,
            offering.legacy_unit_id,
            course.course_code,
            course.course_name,
            offering.academic_year,
            offering.teaching_period
        FROM assessment_plans AS plan
        JOIN unit_offerings AS offering
          ON offering.unit_offering_id = plan.unit_offering_id
        JOIN courses AS course ON course.course_id = offering.course_id
        WHERE plan.assessment_plan_id = ?
        """,
        (assessment_plan_id,),
    ).fetchone()
    if plan is None:
        raise ApiError("assessment_not_found", "Assessment not found.", 404)
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(plan["unit_offering_id"]),
    ):
        raise ApiError(
            "assessment_forbidden",
            "You are not authorised to manage this assessment.",
            403,
        )
    specs = conn.execute(
        """
        SELECT spec_id, version, source_file_path, source_content_hash,
               created_at
        FROM assignment_specs
        WHERE assignment_id = ?
        ORDER BY version DESC
        """,
        (plan["legacy_assignment_id"],),
    ).fetchall()
    rubrics = conn.execute(
        """
        SELECT rubric_id, version, source_file_path, source_content_hash,
               created_at
        FROM rubrics
        WHERE assignment_id = ?
        ORDER BY version DESC
        """,
        (plan["legacy_assignment_id"],),
    ).fetchall()
    versions = conn.execute(
        """
        SELECT *
        FROM assessment_plan_versions
        WHERE assessment_plan_id = ?
        ORDER BY version DESC
        """,
        (assessment_plan_id,),
    ).fetchall()
    jobs = conn.execute(
        """
        SELECT processing_job_id, job_type, status, progress_current,
               progress_total, last_error, created_at, completed_at
        FROM processing_jobs
        WHERE assessment_plan_id = ?
        ORDER BY processing_job_id DESC
        LIMIT 20
        """,
        (assessment_plan_id,),
    ).fetchall()
    batches = conn.execute(
        """
        SELECT batch.*
        FROM submission_batches AS batch
        JOIN assessment_activities AS activity
          ON activity.assessment_activity_id =
             batch.assessment_activity_id
        JOIN assessment_plan_versions AS version
          ON version.assessment_plan_version_id =
             activity.assessment_plan_version_id
        WHERE version.assessment_plan_id = ?
        ORDER BY batch.submission_batch_id DESC
        """,
        (assessment_plan_id,),
    ).fetchall()
    roster_ready = (
        conn.execute(
            """
            SELECT 1
            FROM roster_imports
            WHERE unit_offering_id = ?
              AND status IN ('imported', 'partially_imported')
            LIMIT 1
            """,
            (plan["unit_offering_id"],),
        ).fetchone()
        is not None
    )
    active_version = next(
        (dict(row) for row in versions if row["status"] == "active"),
        None,
    )
    return {
        "assessment": dict(plan),
        "specifications": [dict(row) for row in specs],
        "rubrics": [dict(row) for row in rubrics],
        "versions": [dict(row) for row in versions],
        "active_version": active_version,
        "jobs": [dict(row) for row in jobs],
        "batches": [dict(row) for row in batches],
        "roster_ready": roster_ready,
        "summative_upload_ready": bool(
            roster_ready
            and active_version
            and active_version.get("spec_id")
            and active_version.get("rubric_id")
        ),
    }


def create_roster_import(
    conn: sqlite3.Connection,
    actor_user_id: int,
    unit_offering_id: int,
    upload: StoredUpload,
) -> dict:
    if not can_administer_unit(conn, actor_user_id, unit_offering_id):
        raise ApiError(
            "unit_forbidden",
            "You are not authorised to manage this Unit.",
            403,
        )
    existing = conn.execute(
        """
        SELECT *
        FROM roster_imports
        WHERE unit_offering_id = ?
          AND source_content_hash = ?
        """,
        (unit_offering_id, upload.content_hash),
    ).fetchone()
    if existing is not None:
        remove_stored_upload(upload.storage_path)
        return _roster_upload_response(existing)
    roster_import_id = int(
        conn.execute(
            """
            INSERT INTO roster_imports
                (unit_offering_id, uploaded_by_user_id,
                 source_file_name, source_file_path,
                 source_content_hash, status)
            VALUES (?, ?, ?, ?, ?, 'uploaded')
            """,
            (
                unit_offering_id,
                actor_user_id,
                upload.original_file_name,
                str(upload.storage_path),
                upload.content_hash,
            ),
        ).lastrowid
    )
    record_audit_event(
        conn,
        "roster.uploaded",
        "roster_import",
        roster_import_id,
        actor_user_id=actor_user_id,
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM roster_imports WHERE roster_import_id = ?",
        (roster_import_id,),
    ).fetchone()
    return _roster_upload_response(row)


def _read_roster(path: str | Path) -> tuple[list[str], list[dict]]:
    try:
        with Path(path).open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as handle:
            reader = csv.DictReader(handle)
            headers = [str(value) for value in (reader.fieldnames or [])]
            rows = [dict(row) for row in reader]
    except UnicodeDecodeError as exc:
        raise ApiError(
            "roster_encoding",
            "The roster CSV must use UTF-8 encoding.",
            422,
        ) from exc
    if not headers:
        raise ApiError(
            "roster_empty",
            "The roster CSV has no header row.",
            422,
        )
    return headers, rows


def _roster_upload_response(row: sqlite3.Row) -> dict:
    headers, rows = _read_roster(row["source_file_path"])
    return {
        "roster_import": dict(row),
        "headers": headers,
        "sample_rows": rows[:10],
    }


def preview_roster_import(
    conn: sqlite3.Connection,
    actor_user_id: int,
    roster_import_id: int,
    mapping: dict,
) -> dict:
    roster = conn.execute(
        "SELECT * FROM roster_imports WHERE roster_import_id = ?",
        (roster_import_id,),
    ).fetchone()
    if roster is None:
        raise ApiError("roster_not_found", "Roster import not found.", 404)
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(roster["unit_offering_id"]),
    ):
        raise ApiError("unit_forbidden", "Not authorised.", 403)
    headers, raw_rows = _read_roster(roster["source_file_path"])
    student_id_column = str(mapping.get("student_id") or "")
    email_column = str(mapping.get("email") or "")
    name_column = str(mapping.get("name") or "")
    first_name_column = str(mapping.get("first_name") or "")
    last_name_column = str(mapping.get("last_name") or "")
    if (
        not student_id_column
        or not email_column
        or (
            not name_column
            and (not first_name_column or not last_name_column)
        )
    ):
        raise ApiError(
            "roster_mapping_required",
            (
                "Map student ID, institutional email, and either full name "
                "or both first name and surname."
            ),
            422,
        )
    selected = [
        value
        for value in (
            student_id_column,
            email_column,
            name_column,
            first_name_column,
            last_name_column,
        )
        if value
    ]
    if len(set(selected)) != len(selected) or any(
        value not in headers for value in selected
    ):
        raise ApiError(
            "roster_mapping_invalid",
            "Each required field must map to a different CSV column.",
            422,
        )

    normalized_rows = []
    for number, raw in enumerate(raw_rows, start=2):
        identifier = str(raw.get(student_id_column) or "").strip()
        if name_column:
            name = str(raw.get(name_column) or "").strip()
        else:
            name = " ".join(
                value
                for value in (
                    str(raw.get(first_name_column) or "").strip(),
                    str(raw.get(last_name_column) or "").strip(),
                )
                if value
            )
        email = str(raw.get(email_column) or "").strip().casefold()
        normalized_rows.append(
            {
                "source_row_number": number,
                "raw": raw,
                "identifier": identifier,
                "name": name,
                "email": email,
            }
        )
    id_counts = Counter(
        row["identifier"].casefold()
        for row in normalized_rows
        if row["identifier"]
    )
    existing_students = {
        str(row["institution_student_identifier"]).casefold(): row
        for row in conn.execute("SELECT * FROM students")
    }
    email_owners = {
        str(row["institution_email"]).casefold(): int(row["student_id"])
        for row in conn.execute(
            """
            SELECT student_id, institution_email
            FROM students
            WHERE institution_email IS NOT NULL
              AND trim(institution_email) != ''
            """
        )
    }
    source_ids: set[str] = set()
    prepared: list[dict] = []
    for row in normalized_rows:
        identifier_key = row["identifier"].casefold()
        action = "pending"
        error = None
        student = existing_students.get(identifier_key)
        if not row["identifier"]:
            action, error = "invalid", "Student ID is required."
        elif id_counts[identifier_key] > 1:
            action, error = "invalid", "Student ID is duplicated in the CSV."
        elif not row["name"]:
            action, error = "invalid", "Student name is required."
        elif not row["email"] or "@" not in row["email"]:
            action, error = "invalid", "Institutional email is invalid."
        elif (
            row["email"] in email_owners
            and (
                student is None
                or email_owners[row["email"]] != int(student["student_id"])
            )
        ):
            action, error = (
                "invalid",
                "Institutional email belongs to another student.",
            )
        elif student is None:
            action = "create"
        elif (
            str(student["full_name"] or "") != row["name"]
            or str(student["institution_email"] or "").casefold()
            != row["email"]
        ):
            action = "update"
        else:
            action = "unchanged"
        if row["identifier"]:
            source_ids.add(identifier_key)
        prepared.append(
            {
                **row,
                "student_id": (
                    int(student["student_id"]) if student is not None else None
                ),
                "action": action,
                "error": error,
            }
        )

    enrolled = conn.execute(
        """
        SELECT student.student_id,
               student.institution_student_identifier,
               student.full_name,
               student.institution_email
        FROM student_enrolments AS enrolment
        JOIN students AS student
          ON student.student_id = enrolment.student_id
        WHERE enrolment.unit_offering_id = ?
          AND enrolment.status = 'active'
        """,
        (roster["unit_offering_id"],),
    ).fetchall()
    withdrawal_rows = [
        row
        for row in enrolled
        if str(row["institution_student_identifier"]).casefold()
        not in source_ids
    ]

    conn.execute("BEGIN")
    try:
        conn.execute(
            "DELETE FROM roster_import_rows WHERE roster_import_id = ?",
            (roster_import_id,),
        )
        for row in prepared:
            conn.execute(
                """
                INSERT INTO roster_import_rows
                    (roster_import_id, source_row_number, raw_data_json,
                     institution_student_identifier, full_name,
                     institution_email, student_id, action,
                     validation_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    roster_import_id,
                    row["source_row_number"],
                    json.dumps(row["raw"], ensure_ascii=False),
                    row["identifier"] or None,
                    row["name"] or None,
                    row["email"] or None,
                    row["student_id"],
                    row["action"],
                    row["error"],
                ),
            )
        for row in withdrawal_rows:
            conn.execute(
                """
                INSERT INTO roster_import_rows
                    (roster_import_id, student_id,
                     institution_student_identifier, full_name,
                     institution_email, action)
                VALUES (?, ?, ?, ?, ?, 'withdrawal_candidate')
                """,
                (
                    roster_import_id,
                    row["student_id"],
                    row["institution_student_identifier"],
                    row["full_name"],
                    row["institution_email"],
                ),
            )
        counts = Counter(row["action"] for row in prepared)
        conn.execute(
            """
            UPDATE roster_imports
            SET column_mapping_json = ?,
                status = 'previewed',
                total_row_count = ?,
                valid_row_count = ?,
                invalid_row_count = ?,
                new_student_count = ?,
                updated_student_count = ?,
                withdrawal_candidate_count = ?,
                previewed_at = CURRENT_TIMESTAMP,
                error_message = NULL
            WHERE roster_import_id = ?
            """,
            (
                json.dumps(mapping, ensure_ascii=False),
                len(prepared),
                len(prepared) - counts["invalid"],
                counts["invalid"],
                counts["create"],
                counts["update"],
                len(withdrawal_rows),
                roster_import_id,
            ),
        )
        record_audit_event(
            conn,
            "roster.previewed",
            "roster_import",
            roster_import_id,
            actor_user_id=actor_user_id,
            metadata={
                "invalid_rows": counts["invalid"],
                "withdrawal_candidates": len(withdrawal_rows),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_roster_import(conn, actor_user_id, roster_import_id)


def get_roster_import(
    conn: sqlite3.Connection,
    actor_user_id: int,
    roster_import_id: int,
) -> dict:
    roster = conn.execute(
        "SELECT * FROM roster_imports WHERE roster_import_id = ?",
        (roster_import_id,),
    ).fetchone()
    if roster is None:
        raise ApiError("roster_not_found", "Roster import not found.", 404)
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(roster["unit_offering_id"]),
    ):
        raise ApiError("unit_forbidden", "Not authorised.", 403)
    rows = conn.execute(
        """
        SELECT *
        FROM roster_import_rows
        WHERE roster_import_id = ?
        ORDER BY
            CASE WHEN source_row_number IS NULL THEN 1 ELSE 0 END,
            source_row_number,
            roster_import_row_id
        """,
        (roster_import_id,),
    ).fetchall()
    return {
        "roster_import": dict(roster),
        "rows": [dict(row) for row in rows],
    }


def commit_roster_import(
    conn: sqlite3.Connection,
    actor_user_id: int,
    roster_import_id: int,
    withdraw_missing: bool | None,
) -> dict:
    roster = conn.execute(
        "SELECT * FROM roster_imports WHERE roster_import_id = ?",
        (roster_import_id,),
    ).fetchone()
    if roster is None:
        raise ApiError("roster_not_found", "Roster import not found.", 404)
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(roster["unit_offering_id"]),
    ):
        raise ApiError("unit_forbidden", "Not authorised.", 403)
    if roster["status"] != "previewed":
        raise ApiError(
            "roster_not_previewed",
            "Preview and map the roster before importing.",
            409,
        )
    if int(roster["withdrawal_candidate_count"]) and withdraw_missing is None:
        raise ApiError(
            "withdrawal_confirmation_required",
            "Choose whether missing students should be marked withdrawn.",
            409,
            {
                "withdrawal_candidate_count": int(
                    roster["withdrawal_candidate_count"]
                )
            },
        )
    rows = conn.execute(
        """
        SELECT *
        FROM roster_import_rows
        WHERE roster_import_id = ?
        ORDER BY roster_import_row_id
        """,
        (roster_import_id,),
    ).fetchall()
    withdrawn_count = 0
    conn.execute("BEGIN")
    try:
        for row in rows:
            if row["action"] == "invalid":
                continue
            if row["action"] == "withdrawal_candidate":
                if withdraw_missing:
                    conn.execute(
                        """
                        UPDATE student_enrolments
                        SET status = 'withdrawn',
                            ended_at = CURRENT_TIMESTAMP
                        WHERE unit_offering_id = ?
                          AND student_id = ?
                        """,
                        (
                            roster["unit_offering_id"],
                            row["student_id"],
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE roster_import_rows
                        SET action = 'withdrawn',
                            applied_at = CURRENT_TIMESTAMP
                        WHERE roster_import_row_id = ?
                        """,
                        (row["roster_import_row_id"],),
                    )
                    withdrawn_count += 1
                else:
                    conn.execute(
                        """
                        UPDATE roster_import_rows
                        SET action = 'skipped',
                            applied_at = CURRENT_TIMESTAMP
                        WHERE roster_import_row_id = ?
                        """,
                        (row["roster_import_row_id"],),
                    )
                continue

            student = conn.execute(
                """
                SELECT *
                FROM students
                WHERE institution_student_identifier = ?
                """,
                (row["institution_student_identifier"],),
            ).fetchone()
            if student is None:
                student_id = int(
                    conn.execute(
                        """
                        INSERT INTO students
                            (institution_student_identifier,
                             full_name, institution_email)
                        VALUES (?, ?, ?)
                        """,
                        (
                            row["institution_student_identifier"],
                            row["full_name"],
                            row["institution_email"],
                        ),
                    ).lastrowid
                )
            else:
                student_id = int(student["student_id"])
                if student["user_id"] is not None:
                    conn.execute(
                        """
                        UPDATE users
                        SET email = ?, display_name = ?
                        WHERE user_id = ?
                        """,
                        (
                            row["institution_email"],
                            row["full_name"],
                            student["user_id"],
                        ),
                    )
                conn.execute(
                    """
                    UPDATE students
                    SET full_name = ?, institution_email = ?
                    WHERE student_id = ?
                    """,
                    (
                        row["full_name"],
                        row["institution_email"],
                        student_id,
                    ),
                )
            conn.execute(
                """
                INSERT INTO student_enrolments
                    (unit_offering_id, student_id, status,
                     source, source_reference)
                VALUES (?, ?, 'active', 'moodle', ?)
                ON CONFLICT(unit_offering_id, student_id)
                DO UPDATE SET
                    status = 'active',
                    source = 'moodle',
                    source_reference = excluded.source_reference,
                    ended_at = NULL
                """,
                (
                    roster["unit_offering_id"],
                    student_id,
                    str(roster_import_id),
                ),
            )
            conn.execute(
                """
                UPDATE roster_import_rows
                SET student_id = ?, applied_at = CURRENT_TIMESTAMP
                WHERE roster_import_row_id = ?
                """,
                (student_id, row["roster_import_row_id"]),
            )
        status = (
            "partially_imported"
            if int(roster["invalid_row_count"])
            else "imported"
        )
        conn.execute(
            """
            UPDATE roster_imports
            SET status = ?,
                withdrawn_student_count = ?,
                committed_at = CURRENT_TIMESTAMP
            WHERE roster_import_id = ?
            """,
            (status, withdrawn_count, roster_import_id),
        )
        record_audit_event(
            conn,
            "roster.committed",
            "roster_import",
            roster_import_id,
            actor_user_id=actor_user_id,
            metadata={
                "withdraw_missing": bool(withdraw_missing),
                "withdrawn_count": withdrawn_count,
            },
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ApiError(
            "roster_conflict",
            "The roster contains an identity or email conflict.",
            409,
        ) from exc
    except Exception:
        conn.rollback()
        raise
    return get_roster_import(conn, actor_user_id, roster_import_id)
