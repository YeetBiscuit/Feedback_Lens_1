from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

from feedback_lens.feedback.llm.providers import (
    DEFAULT_FEEDBACK_MODEL,
    DEFAULT_FEEDBACK_PROVIDER,
    list_feedback_models,
    validate_feedback_model,
)
from feedback_lens.paths import PROJECT_ROOT
from feedback_lens.web.allocation_service import list_unit_staff
from feedback_lens.web.common import record_audit_event, student_import_is_ready
from feedback_lens.web.config import get_web_settings
from feedback_lens.web.errors import ApiError
from feedback_lens.web.security import can_administer_unit, is_chief_admin
from feedback_lens.web.storage import StoredUpload, remove_stored_upload


def _file_name(value: str | None) -> str:
    return Path(str(value or "").replace("\\", "/")).name


def _normalized_path(value: str | None) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").casefold()


def _scoping_materials_with_display_names(
    notes: list[sqlite3.Row],
    upload_jobs: list[sqlite3.Row],
) -> list[dict]:
    uploaded_names = []
    for job in upload_jobs:
        try:
            payload = json.loads(job["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        original_name = _file_name(payload.get("original_file_name"))
        job_path = _normalized_path(job["source_file_path"])
        if original_name:
            uploaded_names.append(
                (
                    job_path,
                    str(job["source_content_hash"] or "").casefold(),
                    original_name,
                )
            )

    materials = []
    for row in notes:
        material = dict(row)
        material_path = _normalized_path(material["source_file_path"])
        material_hash = str(
            material.get("source_content_hash") or ""
        ).casefold()
        original_name = next(
            (
                name
                for job_path, job_hash, name in uploaded_names
                if (
                    material_path
                    and job_path
                    and job_path.endswith(material_path)
                )
                or (
                    material_hash
                    and job_hash
                    and material_hash == job_hash
                )
            ),
            None,
        )
        material["original_file_name"] = original_name
        material["display_file_name"] = (
            original_name
            or _file_name(material["source_file_path"])
            or material["title"]
            or "Untitled material"
        )
        if material["material_type"] == "deleted_scoping_note":
            material["lifecycle_status"] = "deleted"
            material["download_url"] = None
        elif material["is_active"]:
            material["lifecycle_status"] = "active"
            material["download_url"] = (
                f"/api/admin/scoping-notes/{material['material_id']}/download"
            )
        else:
            material["lifecycle_status"] = "deactivated"
            material["download_url"] = (
                f"/api/admin/scoping-notes/{material['material_id']}/download"
            )
        materials.append(material)
    return materials


def _documents_with_display_names(
    rows: list[sqlite3.Row],
    upload_jobs: list[sqlite3.Row],
    document_kind: str,
) -> list[dict]:
    uploaded_names = []
    for job in upload_jobs:
        try:
            payload = json.loads(job["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        name = _file_name(payload.get("original_file_name"))
        if name:
            uploaded_names.append(
                (
                    _normalized_path(job["source_file_path"]),
                    str(job["source_content_hash"] or "").casefold(),
                    name,
                )
            )
    id_key = "spec_id" if document_kind == "specification" else "rubric_id"
    documents = []
    for row in rows:
        document = dict(row)
        source_path = _normalized_path(document["source_file_path"])
        source_hash = str(
            document.get("source_content_hash") or ""
        ).casefold()
        original_name = next(
            (
                name
                for job_path, job_hash, name in uploaded_names
                if (
                    source_path
                    and job_path
                    and job_path.endswith(source_path)
                )
                or (
                    source_hash
                    and job_hash
                    and source_hash == job_hash
                )
            ),
            None,
        )
        document["display_file_name"] = (
            original_name
            or _file_name(document["source_file_path"])
            or f"{document_kind.title()} version {document['version']}"
        )
        document["download_url"] = (
            f"/api/admin/{document_kind}s/{document[id_key]}/download"
        )
        documents.append(document)
    return documents


def _downloadable_upload_path(value: str | None) -> Path:
    if not value:
        raise ApiError(
            "download_unavailable",
            "The original uploaded file is no longer available.",
            410,
        )
    stored = Path(value)
    candidate = stored if stored.is_absolute() else PROJECT_ROOT / stored
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ApiError(
            "download_unavailable",
            "The original uploaded file is no longer available.",
            410,
        ) from exc
    upload_root = get_web_settings().upload_root.resolve()
    if (
        not resolved.is_file()
        or (resolved != upload_root and upload_root not in resolved.parents)
    ):
        raise ApiError(
            "download_unavailable",
            "The original uploaded file is no longer available.",
            410,
        )
    return resolved


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
            "Only a Chief Admin can manage Units.",
            403,
        )
    return int(row["organization_id"])


def list_admin_units(
    conn: sqlite3.Connection,
    user_id: int,
) -> list[dict]:
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
              EXISTS (
                  SELECT 1
                  FROM organization_role_assignments AS org_role
                  WHERE org_role.organization_id = course.organization_id
                    AND org_role.user_id = ?
                    AND org_role.role = 'chief_admin'
                    AND org_role.active = 1
              )
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
        (user_id, user_id),
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
    course_code = str(data.get("course_code") or "").strip().upper()
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

    existing_course = conn.execute(
        """
        SELECT course_id, course_name
        FROM courses
        WHERE organization_id = ?
          AND course_code = ?
        """,
        (organization_id, course_code),
    ).fetchone()
    if (
        existing_course is not None
        and str(existing_course["course_name"]).strip().casefold()
        != course_name.casefold()
    ):
        raise ApiError(
            "unit_code_conflict",
            (
                f"Unit code {course_code} already belongs to "
                f"{existing_course['course_name']}."
            ),
            409,
        )

    try:
        conn.execute("BEGIN")
        if existing_course is None:
            course_id = int(
                conn.execute(
                    """
                    INSERT INTO courses
                        (organization_id, course_code, course_name,
                         faculty, academic_level)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        organization_id,
                        course_code,
                        course_name,
                        (
                            str(data.get("faculty")).strip()
                            if data.get("faculty")
                            else None
                        ),
                        (
                            str(data.get("academic_level")).strip()
                            if data.get("academic_level")
                            else None
                        ),
                    ),
                ).lastrowid
            )
        else:
            course_id = int(existing_course["course_id"])
            course_name = str(existing_course["course_name"])
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


def update_unit(
    conn: sqlite3.Connection,
    actor_user_id: int,
    unit_offering_id: int,
    data: dict,
) -> dict:
    organization_id = _organization_for_chief(conn, actor_user_id)
    row = conn.execute(
        """
        SELECT
            offering.course_id,
            offering.legacy_unit_id,
            course.course_code,
            course.course_name
        FROM unit_offerings AS offering
        JOIN courses AS course ON course.course_id = offering.course_id
        WHERE offering.unit_offering_id = ?
          AND course.organization_id = ?
        """,
        (unit_offering_id, organization_id),
    ).fetchone()
    if row is None:
        raise ApiError("unit_not_found", "Unit not found.", 404)

    course_code = str(data.get("course_code") or "").strip().upper()
    course_name = str(data.get("course_name") or "").strip()
    if not course_code or not course_name:
        raise ApiError(
            "invalid_unit",
            "Unit code and name are required.",
            422,
        )
    old_code = str(row["course_code"])
    old_name = str(row["course_name"])
    code_changed = course_code.casefold() != old_code.casefold()
    if code_changed:
        material = conn.execute(
            """
            SELECT material.material_id
            FROM unit_offerings AS offering
            JOIN unit_materials AS material
              ON material.unit_id = offering.legacy_unit_id
            WHERE offering.course_id = ?
              AND material.assignment_id IS NULL
              AND material.material_type != 'deleted_scoping_note'
              AND NOT EXISTS (
                  SELECT 1
                  FROM audit_events AS audit
                  WHERE audit.event_type = 'scoping_note.restored'
                    AND audit.entity_type = 'unit_material'
                    AND audit.entity_id = CAST(
                        material.material_id AS TEXT
                    )
              )
            LIMIT 1
            """,
            (row["course_id"],),
        ).fetchone()
        if material is not None:
            raise ApiError(
                "unit_code_locked",
                (
                    "The Unit code cannot be changed after scoping "
                    "materials have been processed."
                ),
                409,
            )
        conflict = conn.execute(
            """
            SELECT course_id, course_name
            FROM courses
            WHERE organization_id = ?
              AND course_code = ?
              AND course_id != ?
            """,
            (organization_id, course_code, row["course_id"]),
        ).fetchone()
        if conflict is not None:
            raise ApiError(
                "unit_code_conflict",
                (
                    f"Unit code {course_code} already belongs to "
                    f"{conflict['course_name']}."
                ),
                409,
            )

    try:
        conn.execute("BEGIN")
        conn.execute(
            """
            UPDATE courses
            SET course_code = ?,
                course_name = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE course_id = ?
            """,
            (course_code, course_name, row["course_id"]),
        )
        conn.execute(
            """
            UPDATE units
            SET unit_code = ?,
                unit_name = ?
            WHERE unit_id IN (
                SELECT legacy_unit_id
                FROM unit_offerings
                WHERE course_id = ?
                  AND legacy_unit_id IS NOT NULL
            )
            """,
            (course_code, course_name, row["course_id"]),
        )
        offerings = conn.execute(
            """
            SELECT unit_offering_id, academic_year, teaching_period
            FROM unit_offerings
            WHERE course_id = ?
            """,
            (row["course_id"],),
        ).fetchall()
        for offering in offerings:
            conn.execute(
                """
                UPDATE unit_offerings
                SET offering_name = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE unit_offering_id = ?
                """,
                (
                    (
                        f"{course_code} {offering['academic_year']} "
                        f"{offering['teaching_period']}"
                    ),
                    offering["unit_offering_id"],
                ),
            )
        record_audit_event(
            conn,
            "unit.updated",
            "unit_offering",
            unit_offering_id,
            actor_user_id=actor_user_id,
            metadata={
                "old": {"course_code": old_code, "course_name": old_name},
                "new": {
                    "course_code": course_code,
                    "course_name": course_name,
                },
            },
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ApiError(
            "unit_code_conflict",
            f"Unit code {course_code} already exists.",
            409,
        ) from exc
    except Exception:
        conn.rollback()
        raise
    return get_unit_detail(conn, actor_user_id, unit_offering_id)


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
            material.material_type,
            material.source_file_path,
            material.source_content_hash,
            material.created_at,
            material.is_active,
            material.deactivated_at,
            (
                SELECT audit.created_at
                FROM audit_events AS audit
                WHERE audit.event_type = 'scoping_note.deleted'
                  AND audit.entity_type = 'unit_material'
                  AND audit.entity_id = CAST(material.material_id AS TEXT)
                ORDER BY audit.audit_event_id DESC
                LIMIT 1
            ) AS deleted_at
        FROM unit_materials AS material
        WHERE material.unit_id = ?
          AND material.assignment_id IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM audit_events AS audit
              WHERE audit.event_type = 'scoping_note.restored'
                AND audit.entity_type = 'unit_material'
                AND audit.entity_id = CAST(material.material_id AS TEXT)
          )
        ORDER BY material.material_id DESC
        """,
        (unit["legacy_unit_id"],),
    ).fetchall()
    upload_jobs = conn.execute(
        """
        SELECT source_file_path, source_content_hash, payload_json
        FROM processing_jobs
        WHERE unit_offering_id = ?
          AND job_type = 'scoping_note_ingest'
        ORDER BY processing_job_id DESC
        """,
        (unit_offering_id,),
    ).fetchall()
    return {
        "unit": dict(unit),
        "assessments": [dict(row) for row in assessments],
        "unit_admins": [dict(row) for row in admins],
        "staff": list_unit_staff(conn, user_id, unit_offering_id),
        "roster_imports": [dict(row) for row in rosters],
        "scoping_notes": _scoping_materials_with_display_names(
            notes,
            upload_jobs,
        ),
        "is_chief_admin": is_chief_admin(conn, user_id),
        "unit_code_editable": not any(
            material["material_type"] != "deleted_scoping_note"
            for material in notes
        ),
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
            offering.teaching_period,
            assignment.due_date,
            assignment.weight
        FROM assessment_plans AS plan
        JOIN unit_offerings AS offering
          ON offering.unit_offering_id = plan.unit_offering_id
        JOIN courses AS course ON course.course_id = offering.course_id
        LEFT JOIN assignments AS assignment
          ON assignment.assignment_id = plan.legacy_assignment_id
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
    document_jobs = conn.execute(
        """
        SELECT
            job_type,
            source_file_path,
            source_content_hash,
            payload_json
        FROM processing_jobs
        WHERE assessment_plan_id = ?
          AND job_type IN (
              'assignment_spec_ingest',
              'rubric_ingest'
          )
        ORDER BY processing_job_id DESC
        """,
        (assessment_plan_id,),
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
    roster_ready = student_import_is_ready(
        conn,
        int(plan["unit_offering_id"]),
    )
    active_version = next(
        (dict(row) for row in versions if row["status"] == "active"),
        None,
    )
    model_summary = _assessment_feedback_model_summary(
        conn,
        int(assessment_plan_id),
        (
            int(plan["legacy_assignment_id"])
            if plan["legacy_assignment_id"] is not None
            else None
        ),
    )
    return {
        "assessment": dict(plan),
        "feedback_models": list_feedback_models(),
        "feedback_model_summary": model_summary,
        "feedback_model_history": _assessment_feedback_model_history(
            conn,
            int(assessment_plan_id),
        ),
        "specifications": _documents_with_display_names(
            specs,
            [
                row
                for row in document_jobs
                if row["job_type"] == "assignment_spec_ingest"
            ],
            "specification",
        ),
        "rubrics": _documents_with_display_names(
            rubrics,
            [
                row
                for row in document_jobs
                if row["job_type"] == "rubric_ingest"
            ],
            "rubric",
        ),
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


def _assessment_feedback_model_summary(
    conn: sqlite3.Connection,
    assessment_plan_id: int,
    legacy_assignment_id: int | None,
) -> dict:
    generated = 0
    if legacy_assignment_id is not None:
        generated = conn.execute(
            """
            SELECT COUNT(DISTINCT submission_id)
            FROM generation_runs
            WHERE assignment_id = ?
              AND status = 'completed'
            """,
            (legacy_assignment_id,),
        ).fetchone()[0]
    reviewed = conn.execute(
        """
        SELECT COUNT(*)
        FROM submission_workflow_states AS workflow
        JOIN submission_attempts AS attempt
          ON attempt.submission_attempt_id = workflow.submission_attempt_id
        JOIN assessment_activities AS activity
          ON activity.assessment_activity_id = attempt.assessment_activity_id
        JOIN assessment_plan_versions AS version
          ON version.assessment_plan_version_id =
             activity.assessment_plan_version_id
        WHERE version.assessment_plan_id = ?
          AND workflow.marking_status = 'marker_confirmed'
          AND attempt.validity_status = 'valid'
        """,
        (assessment_plan_id,),
    ).fetchone()[0]
    return {
        "generated_feedback_count": int(generated or 0),
        "reviewed_feedback_count": int(reviewed or 0),
    }


def _assessment_feedback_model_history(
    conn: sqlite3.Connection,
    assessment_plan_id: int,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            audit.created_at,
            audit.metadata_json,
            user.display_name,
            user.email
        FROM audit_events AS audit
        LEFT JOIN users AS user ON user.user_id = audit.actor_user_id
        WHERE audit.event_type = 'assessment.feedback_model_updated'
          AND audit.entity_type = 'assessment_plan'
          AND audit.entity_id = ?
        ORDER BY audit.audit_event_id DESC
        LIMIT 20
        """,
        (str(assessment_plan_id),),
    ).fetchall()
    history = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        history.append(
            {
                "created_at": row["created_at"],
                "changed_by": row["display_name"] or row["email"] or "Unknown",
                "old": metadata.get("old") or {},
                "new": metadata.get("new") or {},
                "generated_feedback_count": metadata.get(
                    "generated_feedback_count",
                    0,
                ),
                "reviewed_feedback_count": metadata.get(
                    "reviewed_feedback_count",
                    0,
                ),
            }
        )
    return history


def update_assessment_feedback_model(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
    data: dict,
) -> dict:
    row = conn.execute(
        """
        SELECT
            assessment_plan_id,
            unit_offering_id,
            legacy_assignment_id,
            COALESCE(default_llm_provider, ?) AS default_llm_provider,
            COALESCE(default_llm_model, ?) AS default_llm_model
        FROM assessment_plans
        WHERE assessment_plan_id = ?
        """,
        (
            DEFAULT_FEEDBACK_PROVIDER,
            DEFAULT_FEEDBACK_MODEL,
            assessment_plan_id,
        ),
    ).fetchone()
    if row is None:
        raise ApiError("assessment_not_found", "Assessment not found.", 404)
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(row["unit_offering_id"]),
    ):
        raise ApiError(
            "assessment_forbidden",
            "You are not authorised to manage this assessment.",
            403,
        )
    if row["legacy_assignment_id"] is None:
        raise ApiError(
            "assessment_not_editable",
            "This assessment is missing its linked assignment record.",
            409,
        )

    try:
        provider, model = validate_feedback_model(
            data.get("provider"),
            data.get("model"),
        )
    except ValueError as exc:
        raise ApiError("invalid_feedback_model", str(exc), 422) from exc

    old = {
        "provider": row["default_llm_provider"],
        "model": row["default_llm_model"],
    }
    new = {"provider": provider, "model": model}
    summary = _assessment_feedback_model_summary(
        conn,
        assessment_plan_id,
        int(row["legacy_assignment_id"]),
    )
    if old != new:
        conn.execute(
            """
            UPDATE assessment_plans
            SET default_llm_provider = ?,
                default_llm_model = ?,
                feedback_model_updated_by_user_id = ?,
                feedback_model_updated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE assessment_plan_id = ?
            """,
            (provider, model, actor_user_id, assessment_plan_id),
        )
        record_audit_event(
            conn,
            "assessment.feedback_model_updated",
            "assessment_plan",
            assessment_plan_id,
            actor_user_id=actor_user_id,
            metadata={
                "old": old,
                "new": new,
                **summary,
                "existing_feedback_unchanged": True,
            },
        )
        conn.commit()

    return {
        "provider": provider,
        "model": model,
        "changed": old != new,
        "existing_feedback_unchanged": True,
        **summary,
    }


def update_assessment(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
    data: dict,
) -> dict:
    row = conn.execute(
        """
        SELECT
            plan.unit_offering_id,
            plan.legacy_assignment_id,
            plan.assessment_code,
            plan.title,
            assignment.due_date,
            assignment.weight
        FROM assessment_plans AS plan
        LEFT JOIN assignments AS assignment
          ON assignment.assignment_id = plan.legacy_assignment_id
        WHERE plan.assessment_plan_id = ?
        """,
        (assessment_plan_id,),
    ).fetchone()
    if row is None:
        raise ApiError("assessment_not_found", "Assessment not found.", 404)
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(row["unit_offering_id"]),
    ):
        raise ApiError(
            "assessment_forbidden",
            "You are not authorised to manage this assessment.",
            403,
        )
    if row["legacy_assignment_id"] is None:
        raise ApiError(
            "assessment_not_editable",
            "This assessment is missing its linked assignment record.",
            409,
        )

    title = str(data.get("title") or "").strip()
    code = str(data.get("assessment_code") or "").strip() or None
    due_value = data.get("due_date")
    due_date = str(due_value).strip() if due_value else None
    if not title:
        raise ApiError(
            "invalid_assessment",
            "Assessment title is required.",
            422,
        )
    if due_date:
        try:
            date.fromisoformat(due_date)
        except ValueError as exc:
            raise ApiError(
                "invalid_assessment_due_date",
                "Due date must be a valid calendar date.",
                422,
            ) from exc
    weight_value = data.get("weight")
    if weight_value is None or weight_value == "":
        weight = None
    else:
        try:
            weight = float(weight_value)
        except (TypeError, ValueError) as exc:
            raise ApiError(
                "invalid_assessment_weight",
                "Weight must be a number between 0 and 100.",
                422,
            ) from exc
        if not math.isfinite(weight) or weight < 0 or weight > 100:
            raise ApiError(
                "invalid_assessment_weight",
                "Weight must be a number between 0 and 100.",
                422,
            )

    old_values = {
        "assessment_code": row["assessment_code"],
        "title": row["title"],
        "due_date": row["due_date"],
        "weight": row["weight"],
    }
    new_values = {
        "assessment_code": code,
        "title": title,
        "due_date": due_date,
        "weight": weight,
    }
    try:
        conn.execute("BEGIN")
        conn.execute(
            """
            UPDATE assessment_plans
            SET assessment_code = ?,
                title = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE assessment_plan_id = ?
            """,
            (code, title, assessment_plan_id),
        )
        conn.execute(
            """
            UPDATE assignments
            SET assignment_code = ?,
                assignment_name = ?,
                due_date = ?,
                weight = ?
            WHERE assignment_id = ?
            """,
            (
                code,
                title,
                due_date,
                weight,
                row["legacy_assignment_id"],
            ),
        )
        record_audit_event(
            conn,
            "assessment.metadata_updated",
            "assessment_plan",
            assessment_plan_id,
            actor_user_id=actor_user_id,
            metadata={"old": old_values, "new": new_values},
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ApiError(
            "assessment_conflict",
            "An assessment with this code already exists.",
            409,
        ) from exc
    except Exception:
        conn.rollback()
        raise
    return get_assessment_detail(
        conn,
        actor_user_id,
        assessment_plan_id,
    )


def get_scoping_note_download(
    conn: sqlite3.Connection,
    actor_user_id: int,
    material_id: int,
) -> tuple[Path, str]:
    row = conn.execute(
        """
        SELECT
            material.material_id,
            material.source_file_path,
            material.source_content_hash,
            material.title,
            material.material_type,
            material.is_active,
            offering.unit_offering_id
        FROM unit_materials AS material
        JOIN unit_offerings AS offering
          ON offering.legacy_unit_id = material.unit_id
        WHERE material.material_id = ?
          AND material.assignment_id IS NULL
        """,
        (material_id,),
    ).fetchone()
    if row is None:
        raise ApiError(
            "scoping_material_not_found",
            "Scoping material not found.",
            404,
        )
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(row["unit_offering_id"]),
    ):
        raise ApiError("unit_forbidden", "Not authorised.", 403)
    if row["material_type"] == "deleted_scoping_note":
        raise ApiError(
            "download_unavailable",
            "Deleted materials cannot be downloaded.",
            410,
        )
    path = _downloadable_upload_path(row["source_file_path"])
    jobs = conn.execute(
        """
        SELECT source_file_path, source_content_hash, payload_json
        FROM processing_jobs
        WHERE unit_offering_id = ?
          AND job_type = 'scoping_note_ingest'
        ORDER BY processing_job_id DESC
        """,
        (row["unit_offering_id"],),
    ).fetchall()
    display = _scoping_materials_with_display_names([row], jobs)[0]
    return path, str(display["display_file_name"])


def get_assessment_document_download(
    conn: sqlite3.Connection,
    actor_user_id: int,
    document_kind: str,
    document_id: int,
) -> tuple[Path, str]:
    definitions = {
        "specification": (
            "assignment_specs",
            "spec_id",
            "assignment_spec_ingest",
        ),
        "rubric": ("rubrics", "rubric_id", "rubric_ingest"),
    }
    if document_kind not in definitions:
        raise ValueError("Unsupported assessment document kind.")
    table, id_column, job_type = definitions[document_kind]
    row = conn.execute(
        f"""
        SELECT
            document.{id_column},
            document.version,
            document.source_file_path,
            document.source_content_hash,
            plan.assessment_plan_id,
            plan.unit_offering_id
        FROM {table} AS document
        JOIN assignments AS assignment
          ON assignment.assignment_id = document.assignment_id
        JOIN assessment_plans AS plan
          ON plan.legacy_assignment_id = assignment.assignment_id
        WHERE document.{id_column} = ?
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        raise ApiError(
            "assessment_document_not_found",
            "Assessment document not found.",
            404,
        )
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(row["unit_offering_id"]),
    ):
        raise ApiError("assessment_forbidden", "Not authorised.", 403)
    path = _downloadable_upload_path(row["source_file_path"])
    jobs = conn.execute(
        """
        SELECT source_file_path, source_content_hash, payload_json
        FROM processing_jobs
        WHERE assessment_plan_id = ?
          AND job_type = ?
        ORDER BY processing_job_id DESC
        """,
        (row["assessment_plan_id"], job_type),
    ).fetchall()
    display = _documents_with_display_names(
        [row],
        jobs,
        document_kind,
    )[0]
    return path, str(display["display_file_name"])


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
