from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict

from feedback_lens.web.common import record_audit_event
from feedback_lens.web.errors import ApiError
from feedback_lens.web.security import can_administer_unit


STAFF_ACCOUNT_ROLES = ("admin", "lead_lecturer", "educator")
ELIGIBLE_ATTEMPT_STATUSES = ("ready", "imported", "processing", "completed")


def _unit_context(
    conn: sqlite3.Connection,
    actor_user_id: int,
    unit_offering_id: int,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            offering.unit_offering_id,
            offering.legacy_unit_id,
            course.organization_id,
            course.course_code,
            course.course_name
        FROM unit_offerings AS offering
        JOIN courses AS course ON course.course_id = offering.course_id
        WHERE offering.unit_offering_id = ?
        """,
        (unit_offering_id,),
    ).fetchone()
    if row is None:
        raise ApiError("unit_not_found", "Unit not found.", 404)
    if not can_administer_unit(conn, actor_user_id, unit_offering_id):
        raise ApiError(
            "unit_forbidden",
            "You are not authorised to manage this Unit.",
            403,
        )
    return row


def _assessment_context(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            plan.assessment_plan_id,
            plan.unit_offering_id,
            plan.assessment_code,
            plan.title,
            plan.legacy_assignment_id,
            offering.legacy_unit_id,
            course.organization_id,
            course.course_code
        FROM assessment_plans AS plan
        JOIN unit_offerings AS offering
          ON offering.unit_offering_id = plan.unit_offering_id
        JOIN courses AS course ON course.course_id = offering.course_id
        WHERE plan.assessment_plan_id = ?
          AND plan.status != 'archived'
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
    return row


def _user_belongs_to_organization(
    conn: sqlite3.Connection,
    user_id: int,
    organization_id: int,
) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM users AS user
            WHERE user.user_id = ?
              AND user.role IN ('admin', 'lead_lecturer', 'educator')
              AND EXISTS (
                  SELECT 1
                  FROM organization_memberships AS membership
                  WHERE membership.user_id = user.user_id
                    AND membership.organization_id = ?
                    AND membership.active = 1
              )
            LIMIT 1
            """,
            (user_id, organization_id),
        ).fetchone()
        is not None
    )


def list_staff_candidates(
    conn: sqlite3.Connection,
    actor_user_id: int,
    unit_offering_id: int,
    query: str,
    *,
    limit: int = 10,
) -> list[dict]:
    context = _unit_context(conn, actor_user_id, unit_offering_id)
    normalized = query.strip().lower()
    bounded_limit = max(1, min(int(limit), 20))
    rows = conn.execute(
        """
        SELECT user.user_id, user.email, user.display_name, user.role,
               user.account_status,
               EXISTS (
                   SELECT 1
                   FROM unit_role_assignments AS existing
                   WHERE existing.unit_offering_id = ?
                     AND existing.user_id = user.user_id
                     AND existing.role = 'staff'
                     AND existing.active = 1
               ) AS already_staff,
               (
                   EXISTS (
                       SELECT 1
                       FROM unit_role_assignments AS admin_role
                       WHERE admin_role.unit_offering_id = ?
                         AND admin_role.user_id = user.user_id
                         AND admin_role.role = 'unit_admin'
                         AND admin_role.active = 1
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM organization_role_assignments AS chief_role
                       WHERE chief_role.organization_id = ?
                         AND chief_role.user_id = user.user_id
                         AND chief_role.role = 'chief_admin'
                         AND chief_role.active = 1
                   )
               ) AS automatically_available
        FROM users AS user
        WHERE user.role IN ('admin', 'lead_lecturer', 'educator')
          AND user.account_status = 'active'
          AND lower(user.email) LIKE '%' || ? || '%'
          AND EXISTS (
              SELECT 1
              FROM organization_memberships AS membership
              WHERE membership.user_id = user.user_id
                AND membership.organization_id = ?
                AND membership.active = 1
          )
        ORDER BY lower(user.email), user.user_id
        LIMIT ?
        """,
        (
            unit_offering_id,
            unit_offering_id,
            context["organization_id"],
            normalized,
            context["organization_id"],
            bounded_limit,
        ),
    ).fetchall()
    return [dict(row) for row in rows]


def list_unit_staff(
    conn: sqlite3.Connection,
    actor_user_id: int,
    unit_offering_id: int,
) -> list[dict]:
    _unit_context(conn, actor_user_id, unit_offering_id)
    rows = conn.execute(
        """
        SELECT
            user.user_id,
            user.email,
            user.display_name,
            user.account_status,
            role.assigned_at,
            (
                SELECT COUNT(*)
                FROM marker_assignments AS assignment
                JOIN current_summative_attempts AS current
                  ON current.submission_attempt_id =
                     assignment.submission_attempt_id
                JOIN submission_attempts AS attempt
                  ON attempt.submission_attempt_id =
                     current.submission_attempt_id
                JOIN assessment_activities AS activity
                  ON activity.assessment_activity_id =
                     attempt.assessment_activity_id
                JOIN assessment_plan_versions AS version
                  ON version.assessment_plan_version_id =
                     activity.assessment_plan_version_id
                JOIN assessment_plans AS plan
                  ON plan.assessment_plan_id = version.assessment_plan_id
                LEFT JOIN submission_workflow_states AS workflow
                  ON workflow.submission_attempt_id =
                     assignment.submission_attempt_id
                WHERE assignment.marker_user_id = user.user_id
                  AND assignment.active = 1
                  AND plan.unit_offering_id = ?
                  AND attempt.validity_status = 'valid'
                  AND COALESCE(workflow.marking_status, 'not_started')
                      != 'marker_confirmed'
            ) AS incomplete_assignment_count
        FROM unit_role_assignments AS role
        JOIN users AS user ON user.user_id = role.user_id
        WHERE role.unit_offering_id = ?
          AND role.role = 'staff'
          AND role.active = 1
        ORDER BY lower(user.email), user.user_id
        """,
        (unit_offering_id, unit_offering_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _sync_legacy_staff(
    conn: sqlite3.Connection,
    legacy_unit_id: int | None,
    user_id: int,
) -> None:
    if legacy_unit_id is None:
        return
    user = conn.execute(
        "SELECT tutor_id FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if user is None or user["tutor_id"] is None:
        return
    conn.execute(
        """
        INSERT INTO unit_tutors(unit_id, tutor_id, role)
        VALUES (?, ?, 'educator')
        ON CONFLICT(unit_id, tutor_id)
        DO UPDATE SET role = excluded.role
        """,
        (legacy_unit_id, user["tutor_id"]),
    )


def add_unit_staff(
    conn: sqlite3.Connection,
    actor_user_id: int,
    unit_offering_id: int,
    staff_user_id: int,
) -> dict:
    context = _unit_context(conn, actor_user_id, unit_offering_id)
    if not _user_belongs_to_organization(
        conn,
        staff_user_id,
        int(context["organization_id"]),
    ):
        raise ApiError(
            "staff_not_found",
            "Choose an eligible staff account in this organization.",
            422,
        )
    automatically_available = conn.execute(
        """
        SELECT 1
        WHERE EXISTS (
            SELECT 1
            FROM unit_role_assignments
            WHERE unit_offering_id = ?
              AND user_id = ?
              AND role = 'unit_admin'
              AND active = 1
        )
        OR EXISTS (
            SELECT 1
            FROM organization_role_assignments
            WHERE organization_id = ?
              AND user_id = ?
              AND role = 'chief_admin'
              AND active = 1
        )
        """,
        (
            unit_offering_id,
            staff_user_id,
            context["organization_id"],
            staff_user_id,
        ),
    ).fetchone()
    if automatically_available is not None:
        raise ApiError(
            "staff_role_not_required",
            "This Admin is already available for allocation and does not need a separate Staff role.",
            409,
        )
    conn.execute(
        """
        INSERT INTO unit_role_assignments
            (unit_offering_id, user_id, role, assigned_by_user_id)
        VALUES (?, ?, 'staff', ?)
        ON CONFLICT(unit_offering_id, user_id, role)
        DO UPDATE SET
            active = 1,
            ended_at = NULL,
            assigned_at = CURRENT_TIMESTAMP,
            assigned_by_user_id = excluded.assigned_by_user_id
        """,
        (unit_offering_id, staff_user_id, actor_user_id),
    )
    _sync_legacy_staff(
        conn,
        context["legacy_unit_id"],
        staff_user_id,
    )
    record_audit_event(
        conn,
        "unit.staff_added",
        "unit_offering",
        unit_offering_id,
        actor_user_id=actor_user_id,
        metadata={"staff_user_id": staff_user_id},
    )
    conn.commit()
    return {"staff": list_unit_staff(conn, actor_user_id, unit_offering_id)}


def _notify(
    conn: sqlite3.Connection,
    user_id: int,
    event_type: str,
    title: str,
    message: str,
    *,
    unit_offering_id: int | None = None,
    assessment_plan_id: int | None = None,
    action_url: str | None = None,
) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO user_notifications
                (user_id, event_type, title, message, unit_offering_id,
                 assessment_plan_id, action_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                event_type,
                title,
                message,
                unit_offering_id,
                assessment_plan_id,
                action_url,
            ),
        ).lastrowid
    )


def remove_unit_staff(
    conn: sqlite3.Connection,
    actor_user_id: int,
    unit_offering_id: int,
    staff_user_id: int,
) -> None:
    context = _unit_context(conn, actor_user_id, unit_offering_id)
    role = conn.execute(
        """
        SELECT unit_role_assignment_id
        FROM unit_role_assignments
        WHERE unit_offering_id = ?
          AND user_id = ?
          AND role = 'staff'
          AND active = 1
        """,
        (unit_offering_id, staff_user_id),
    ).fetchone()
    if role is None:
        raise ApiError("staff_not_found", "Unit Staff member not found.", 404)
    incomplete = conn.execute(
        """
        SELECT COUNT(*)
        FROM marker_assignments AS assignment
        JOIN current_summative_attempts AS current
          ON current.submission_attempt_id = assignment.submission_attempt_id
        JOIN submission_attempts AS attempt
          ON attempt.submission_attempt_id = current.submission_attempt_id
        JOIN assessment_activities AS activity
          ON activity.assessment_activity_id = attempt.assessment_activity_id
        JOIN assessment_plan_versions AS version
          ON version.assessment_plan_version_id =
             activity.assessment_plan_version_id
        JOIN assessment_plans AS plan
          ON plan.assessment_plan_id = version.assessment_plan_id
        LEFT JOIN submission_workflow_states AS workflow
          ON workflow.submission_attempt_id = assignment.submission_attempt_id
        WHERE assignment.marker_user_id = ?
          AND assignment.active = 1
          AND plan.unit_offering_id = ?
          AND attempt.validity_status = 'valid'
          AND COALESCE(workflow.marking_status, 'not_started')
              != 'marker_confirmed'
        """,
        (staff_user_id, unit_offering_id),
    ).fetchone()[0]
    if incomplete:
        raise ApiError(
            "staff_has_incomplete_assignments",
            "Reassign this Staff member's incomplete submissions before removal.",
            409,
            {"incomplete_assignment_count": int(incomplete)},
        )
    conn.execute(
        """
        UPDATE unit_role_assignments
        SET active = 0, ended_at = CURRENT_TIMESTAMP
        WHERE unit_role_assignment_id = ?
        """,
        (role["unit_role_assignment_id"],),
    )
    if context["legacy_unit_id"] is not None:
        user = conn.execute(
            "SELECT tutor_id FROM users WHERE user_id = ?",
            (staff_user_id,),
        ).fetchone()
        other_role = conn.execute(
            """
            SELECT 1
            FROM unit_role_assignments
            WHERE unit_offering_id = ?
              AND user_id = ?
              AND role IN ('unit_admin', 'staff')
              AND active = 1
            LIMIT 1
            """,
            (unit_offering_id, staff_user_id),
        ).fetchone()
        if user and user["tutor_id"] is not None and other_role is None:
            conn.execute(
                "DELETE FROM unit_tutors WHERE unit_id = ? AND tutor_id = ?",
                (context["legacy_unit_id"], user["tutor_id"]),
            )
    _notify(
        conn,
        staff_user_id,
        "unit_staff_removed",
        f"Removed from {context['course_code']}",
        "You no longer have Staff access to this Unit.",
        unit_offering_id=unit_offering_id,
    )
    record_audit_event(
        conn,
        "unit.staff_removed",
        "unit_offering",
        unit_offering_id,
        actor_user_id=actor_user_id,
        metadata={"staff_user_id": staff_user_id},
    )
    conn.commit()


def list_allocation_candidates(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
) -> list[dict]:
    context = _assessment_context(conn, actor_user_id, assessment_plan_id)
    rows = conn.execute(
        """
        SELECT DISTINCT user.user_id, user.email, user.display_name
        FROM users AS user
        WHERE user.role IN ('admin', 'lead_lecturer', 'educator')
          AND user.account_status = 'active'
          AND (
              EXISTS (
                  SELECT 1
                  FROM unit_role_assignments AS role
                  WHERE role.unit_offering_id = ?
                    AND role.user_id = user.user_id
                    AND role.role IN ('unit_admin', 'staff')
                    AND role.active = 1
              )
              OR EXISTS (
                  SELECT 1
                  FROM organization_role_assignments AS org_role
                  WHERE org_role.organization_id = ?
                    AND org_role.user_id = user.user_id
                    AND org_role.role = 'chief_admin'
                    AND org_role.active = 1
              )
          )
        ORDER BY lower(user.email), user.user_id
        """,
        (
            context["unit_offering_id"],
            context["organization_id"],
        ),
    ).fetchall()
    result = []
    for row in rows:
        roles = []
        if conn.execute(
            """
            SELECT 1 FROM organization_role_assignments
            WHERE organization_id = ? AND user_id = ?
              AND role = 'chief_admin' AND active = 1
            """,
            (context["organization_id"], row["user_id"]),
        ).fetchone():
            roles.append("Chief Admin")
        unit_roles = conn.execute(
            """
            SELECT role FROM unit_role_assignments
            WHERE unit_offering_id = ? AND user_id = ? AND active = 1
            ORDER BY CASE role WHEN 'unit_admin' THEN 0 ELSE 1 END
            """,
            (context["unit_offering_id"], row["user_id"]),
        ).fetchall()
        roles.extend(
            "Unit Admin" if item["role"] == "unit_admin" else "Staff"
            for item in unit_roles
        )
        item = dict(row)
        item["scope_roles"] = roles
        result.append(item)
    return result


def _eligible_submissions(
    conn: sqlite3.Connection,
    assessment_plan_id: int,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            attempt.submission_attempt_id,
            attempt.legacy_submission_id AS submission_id,
            attempt.submitted_at,
            student.student_id,
            student.institution_student_identifier AS student_identifier,
            student.full_name AS student_name,
            tutorial_group.tutorial_group_id,
            tutorial_group.group_code AS tutorial_group_code,
            assignment.marker_assignment_id,
            assignment.marker_user_id,
            marker.email AS marker_email,
            marker.display_name AS marker_name,
            COALESCE(workflow.allocation_status, 'unassigned')
                AS allocation_status,
            COALESCE(workflow.ai_generation_status, 'not_started')
                AS ai_generation_status,
            COALESCE(workflow.marking_status, 'not_started')
                AS marking_status,
            generation.generation_id,
            generation.status AS generation_status,
            overall.overall_grade_band,
            overall.final_mark
        FROM current_summative_attempts AS current
        JOIN submission_attempts AS attempt
          ON attempt.submission_attempt_id = current.submission_attempt_id
        JOIN assessment_activities AS activity
          ON activity.assessment_activity_id = attempt.assessment_activity_id
        JOIN assessment_plan_versions AS version
          ON version.assessment_plan_version_id =
             activity.assessment_plan_version_id
        JOIN assessment_plans AS plan
          ON plan.assessment_plan_id = version.assessment_plan_id
        JOIN submission_participants AS participant
          ON participant.submission_attempt_id = attempt.submission_attempt_id
         AND participant.participant_role = 'primary'
        JOIN students AS student ON student.student_id = participant.student_id
        LEFT JOIN student_tutorial_memberships AS membership
          ON membership.unit_offering_id = plan.unit_offering_id
         AND membership.student_id = student.student_id
         AND membership.active = 1
        LEFT JOIN tutorial_groups AS tutorial_group
          ON tutorial_group.tutorial_group_id = membership.tutorial_group_id
         AND tutorial_group.active = 1
        LEFT JOIN marker_assignments AS assignment
          ON assignment.submission_attempt_id = attempt.submission_attempt_id
         AND assignment.active = 1
        LEFT JOIN users AS marker ON marker.user_id = assignment.marker_user_id
        LEFT JOIN submission_workflow_states AS workflow
          ON workflow.submission_attempt_id = attempt.submission_attempt_id
        LEFT JOIN generation_runs AS generation
          ON generation.generation_id = (
              SELECT MAX(candidate.generation_id)
              FROM generation_runs AS candidate
              WHERE candidate.status = 'completed'
                AND (
                    candidate.submission_attempt_id =
                        attempt.submission_attempt_id
                    OR (
                        candidate.submission_attempt_id IS NULL
                        AND candidate.submission_id =
                            attempt.legacy_submission_id
                    )
                )
          )
        LEFT JOIN overall_feedback AS overall
          ON overall.generation_id = generation.generation_id
        WHERE plan.assessment_plan_id = ?
          AND attempt.purpose = 'summative'
          AND attempt.validity_status = 'valid'
          AND attempt.status IN ('ready', 'imported', 'processing', 'completed')
        ORDER BY lower(student.institution_student_identifier),
                 attempt.submission_attempt_id
        """,
        (assessment_plan_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["is_confirmed"] = item["marking_status"] == "marker_confirmed"
        result.append(item)
    return result


def _tutorial_group_scope(
    conn: sqlite3.Connection,
    assessment_context: sqlite3.Row | dict,
    submissions: list[dict],
) -> dict:
    unit_offering_id = int(assessment_context["unit_offering_id"])
    staff_rows = conn.execute(
        """
        SELECT group_row.tutorial_group_id, group_row.group_code,
               link.user_id, user.email, user.display_name,
               user.account_status,
               (
                   SELECT COUNT(*)
                   FROM student_tutorial_memberships AS member_count
                   WHERE member_count.tutorial_group_id =
                         group_row.tutorial_group_id
                     AND member_count.active = 1
               ) AS student_count,
               EXISTS (
                   SELECT 1
                   FROM unit_role_assignments AS role
                   WHERE role.unit_offering_id = group_row.unit_offering_id
                     AND role.user_id = user.user_id
                     AND role.role IN ('unit_admin', 'staff')
                     AND role.active = 1
               ) AS has_unit_role
        FROM tutorial_groups AS group_row
        LEFT JOIN tutorial_group_staff AS link
          ON link.tutorial_group_id = group_row.tutorial_group_id
         AND link.active = 1
        LEFT JOIN users AS user ON user.user_id = link.user_id
        WHERE group_row.unit_offering_id = ?
          AND group_row.active = 1
        ORDER BY lower(group_row.group_code), lower(user.email), user.user_id
        """,
        (unit_offering_id,),
    ).fetchall()
    groups_by_id: dict[int, dict] = {}
    for row in staff_rows:
        group_id = int(row["tutorial_group_id"])
        group = groups_by_id.setdefault(
            group_id,
            {
                "tutorial_group_id": group_id,
                "group_code": row["group_code"],
                "staff": [],
                "student_count": int(row["student_count"]),
                "submission_count": 0,
                "unassigned_count": 0,
            },
        )
        if row["user_id"] is not None:
            person = {
                "user_id": int(row["user_id"]),
                "email": row["email"],
                "display_name": row["display_name"],
                "account_status": row["account_status"],
                "has_unit_role": bool(row["has_unit_role"]),
            }
            group["staff"].append(person)
    missing_group_count = 0
    for submission in submissions:
        group_id = submission.get("tutorial_group_id")
        if group_id is None:
            if submission["marker_user_id"] is None and not submission["is_confirmed"]:
                missing_group_count += 1
            continue
        group = groups_by_id.get(int(group_id))
        if group is None:
            if submission["marker_user_id"] is None and not submission["is_confirmed"]:
                missing_group_count += 1
            continue
        group["submission_count"] += 1
        if submission["marker_user_id"] is None and not submission["is_confirmed"]:
            group["unassigned_count"] += 1
    groups = []
    incomplete_submission_count = missing_group_count
    for group in groups_by_id.values():
        active_staff = [
            person
            for person in group["staff"]
            if person["account_status"] == "active" and person["has_unit_role"]
        ]
        pending_staff = [
            person
            for person in group["staff"]
            if person["account_status"] != "active" or not person["has_unit_role"]
        ]
        group["active_staff_count"] = len(active_staff)
        group["pending_staff_count"] = len(pending_staff)
        group["ready"] = bool(active_staff) and not pending_staff
        if not group["ready"]:
            incomplete_submission_count += int(group["unassigned_count"])
        groups.append(group)
    groups.sort(key=lambda item: (str(item["group_code"]).lower(), item["tutorial_group_id"]))
    policy = conn.execute(
        """
        SELECT strategy, active, enabled_at
        FROM assessment_allocation_policies
        WHERE assessment_plan_id = ?
        """,
        (assessment_context["assessment_plan_id"],),
    ).fetchone()
    return {
        "groups": groups,
        "missing_group_count": missing_group_count,
        "incomplete_submission_count": incomplete_submission_count,
        "unassigned_count": sum(
            submission["marker_user_id"] is None and not submission["is_confirmed"]
            for submission in submissions
        ),
        "ready_group_count": sum(group["ready"] for group in groups),
        "incomplete_group_count": sum(not group["ready"] for group in groups),
        "policy_active": bool(policy and policy["active"]),
        "policy_enabled_at": policy["enabled_at"] if policy else None,
    }


def get_assessment_allocation(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
) -> dict:
    context = _assessment_context(conn, actor_user_id, assessment_plan_id)
    submissions = _eligible_submissions(conn, assessment_plan_id)
    return {
        "assessment": dict(context),
        "candidates": list_allocation_candidates(
            conn,
            actor_user_id,
            assessment_plan_id,
        ),
        "submissions": submissions,
        "tutorial_group_scope": _tutorial_group_scope(
            conn,
            context,
            submissions,
        ),
    }


def _integer_ids(values: object, label: str) -> list[int]:
    if not isinstance(values, list):
        raise ApiError("allocation_invalid", f"{label} must be a list.", 422)
    result = []
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise ApiError(
                "allocation_invalid",
                f"{label} contains an invalid identifier.",
                422,
            ) from error
        if parsed not in result:
            result.append(parsed)
    return result


def _assignment_totals(
    conn: sqlite3.Connection,
    assessment_plan_id: int,
    user_ids: list[int],
) -> dict[int, int]:
    if not user_ids:
        return {}
    placeholders = ", ".join("?" for _ in user_ids)
    rows = conn.execute(
        f"""
        SELECT assignment.marker_user_id, COUNT(*) AS assignment_count
        FROM marker_assignments AS assignment
        JOIN current_summative_attempts AS current
          ON current.submission_attempt_id = assignment.submission_attempt_id
        JOIN submission_attempts AS attempt
          ON attempt.submission_attempt_id = current.submission_attempt_id
        JOIN assessment_activities AS activity
          ON activity.assessment_activity_id = attempt.assessment_activity_id
        JOIN assessment_plan_versions AS version
          ON version.assessment_plan_version_id =
             activity.assessment_plan_version_id
        WHERE version.assessment_plan_id = ?
          AND assignment.active = 1
          AND assignment.marker_user_id IN ({placeholders})
          AND attempt.validity_status = 'valid'
        GROUP BY assignment.marker_user_id
        """,
        (assessment_plan_id, *user_ids),
    ).fetchall()
    totals = {user_id: 0 for user_id in user_ids}
    totals.update(
        {
            int(row["marker_user_id"]): int(row["assignment_count"])
            for row in rows
        }
    )
    return totals


def _build_tutorial_group_preview(
    conn: sqlite3.Connection,
    assessment_context: sqlite3.Row,
    candidates: list[dict],
    submissions: list[dict],
) -> dict:
    candidate_by_id = {int(row["user_id"]): row for row in candidates}
    scope = _tutorial_group_scope(conn, assessment_context, submissions)
    scope_groups = {
        int(group["tutorial_group_id"]): group for group in scope["groups"]
    }
    unassigned = [
        row
        for row in submissions
        if row["marker_user_id"] is None and not row["is_confirmed"]
    ]
    rows_by_group: dict[int, list[dict]] = defaultdict(list)
    missing_group_rows = []
    for row in unassigned:
        group_id = row.get("tutorial_group_id")
        if group_id is None or int(group_id) not in scope_groups:
            missing_group_rows.append(row)
        else:
            rows_by_group[int(group_id)].append(row)

    exceptions = []
    if not any(int(group["student_count"]) > 0 for group in scope_groups.values()):
        exceptions.append(
            {
                "reason": "missing_tutorial_memberships",
                "message": (
                    "Import student Tutorial Group membership before enabling "
                    "Tutorial allocation."
                ),
                "submission_count": len(unassigned),
            }
        )
    if missing_group_rows:
        exceptions.append(
            {
                "reason": "missing_tutorial_group",
                "message": "Students are not assigned to a Tutorial Group.",
                "submission_count": len(missing_group_rows),
                "students": [
                    {
                        "student_identifier": row["student_identifier"],
                        "student_name": row["student_name"],
                    }
                    for row in missing_group_rows[:20]
                ],
            }
        )

    incomplete_group_ids: set[int] = set()
    for group_id, group in scope_groups.items():
        if not rows_by_group.get(group_id) or group["ready"]:
            continue
        incomplete_group_ids.add(group_id)
        pending_staff = [
            person
            for person in group["staff"]
            if person["account_status"] != "active" or not person["has_unit_role"]
        ]
        exceptions.append(
            {
                "reason": (
                    "pending_tutorial_staff"
                    if pending_staff
                    else "missing_tutorial_staff"
                ),
                "message": (
                    "Activate or replace pending Staff before allocating this Group."
                    if pending_staff
                    else "Assign at least one active Staff member to this Group."
                ),
                "tutorial_group_id": group_id,
                "group_code": group["group_code"],
                "submission_count": len(rows_by_group.get(group_id, [])),
                "pending_staff": pending_staff,
            }
        )

    operations = []
    group_summary = []
    all_staff_ids: set[int] = set()
    for group_id in sorted(
        rows_by_group,
        key=lambda item: (
            str(scope_groups[item]["group_code"]).lower(),
            item,
        ),
    ):
        group = scope_groups[group_id]
        if group_id in incomplete_group_ids:
            continue
        active_staff_ids = [
            int(person["user_id"])
            for person in group["staff"]
            if person["account_status"] == "active"
            and person["has_unit_role"]
            and int(person["user_id"]) in candidate_by_id
        ]
        pending_staff = [
            person
            for person in group["staff"]
            if person["account_status"] != "active" or not person["has_unit_role"]
        ]
        if pending_staff or not active_staff_ids:
            exceptions.append(
                {
                    "reason": (
                        "pending_tutorial_staff"
                        if pending_staff
                        else "missing_tutorial_staff"
                    ),
                    "message": (
                        "Activate or replace pending Staff before allocating this Group."
                        if pending_staff
                        else "Assign at least one active Staff member to this Group."
                    ),
                    "tutorial_group_id": group_id,
                    "group_code": group["group_code"],
                    "submission_count": len(rows_by_group[group_id]),
                    "pending_staff": pending_staff,
                }
            )
            continue
        staff_order = sorted(
            active_staff_ids,
            key=lambda user_id: (
                str(candidate_by_id[user_id]["email"]).lower(),
                user_id,
            ),
        )
        all_staff_ids.update(staff_order)
        working_counts = {user_id: 0 for user_id in staff_order}
        for submission in submissions:
            if submission.get("tutorial_group_id") != group_id:
                continue
            marker_user_id = submission.get("marker_user_id")
            if marker_user_id is not None and int(marker_user_id) in working_counts:
                working_counts[int(marker_user_id)] += 1
        before_counts = dict(working_counts)
        assigned_by_staff = {user_id: 0 for user_id in staff_order}
        for row in sorted(
            rows_by_group[group_id],
            key=lambda item: (
                str(item["student_identifier"] or "").lower(),
                int(item["submission_attempt_id"]),
            ),
        ):
            new_user_id = min(
                staff_order,
                key=lambda user_id: (
                    working_counts[user_id],
                    str(candidate_by_id[user_id]["email"]).lower(),
                    user_id,
                ),
            )
            working_counts[new_user_id] += 1
            assigned_by_staff[new_user_id] += 1
            operations.append(
                {
                    "submission_attempt_id": int(row["submission_attempt_id"]),
                    "student_id": int(row["student_id"]),
                    "student_identifier": row["student_identifier"],
                    "student_name": row["student_name"],
                    "tutorial_group_id": group_id,
                    "tutorial_group_code": group["group_code"],
                    "old_staff_user_id": None,
                    "new_staff_user_id": new_user_id,
                    "change_type": "assigned",
                }
            )
        group_summary.append(
            {
                "tutorial_group_id": group_id,
                "group_code": group["group_code"],
                "submission_count": len(rows_by_group[group_id]),
                "staff": [
                    {
                        **candidate_by_id[user_id],
                        "current_count": before_counts[user_id],
                        "assigned_count": assigned_by_staff[user_id],
                        "final_count": working_counts[user_id],
                    }
                    for user_id in staff_order
                ],
            }
        )

    current_totals = _assignment_totals(
        conn,
        int(assessment_context["assessment_plan_id"]),
        sorted(all_staff_ids),
    )
    summaries = []
    for user_id in sorted(
        all_staff_ids,
        key=lambda item: (
            str(candidate_by_id[item]["email"]).lower(),
            item,
        ),
    ):
        assigned_items = [
            operation
            for operation in operations
            if operation["new_staff_user_id"] == user_id
        ]
        summaries.append(
            {
                **candidate_by_id[user_id],
                "current_count": current_totals.get(user_id, 0),
                "assigned_count": len(assigned_items),
                "reassigned_away_count": 0,
                "final_count": current_totals.get(user_id, 0) + len(assigned_items),
                "submissions": [
                    {
                        "student_name": operation["student_name"],
                        "student_identifier": operation["student_identifier"],
                        "tutorial_group_code": operation["tutorial_group_code"],
                        "change_type": operation["change_type"],
                    }
                    for operation in assigned_items
                ],
            }
        )
    canonical = {
        "assessment_plan_id": int(assessment_context["assessment_plan_id"]),
        "mode": "tutorial_groups",
        "operations": operations,
        "exceptions": exceptions,
    }
    preview_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "assessment": dict(assessment_context),
        "mode": "tutorial_groups",
        "operations": operations,
        "summary": summaries,
        "group_summary": group_summary,
        "exceptions": exceptions,
        "can_confirm": not exceptions,
        "change_count": len(operations),
        "preview_hash": preview_hash,
        "tutorial_group_scope": scope,
    }


def _build_preview(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
    payload: dict,
) -> dict:
    context = _assessment_context(conn, actor_user_id, assessment_plan_id)
    mode = str(payload.get("mode") or "manual").strip().lower()
    if mode == "automatic":
        mode = "tutorial_groups"
    if mode not in {"manual", "equal", "tutorial_groups"}:
        raise ApiError("allocation_invalid", "Choose a valid allocation mode.", 422)

    candidates = list_allocation_candidates(
        conn,
        actor_user_id,
        assessment_plan_id,
    )
    candidate_by_id = {int(row["user_id"]): row for row in candidates}
    submissions = _eligible_submissions(conn, assessment_plan_id)
    if mode == "tutorial_groups":
        return _build_tutorial_group_preview(
            conn,
            context,
            candidates,
            submissions,
        )
    submission_by_id = {
        int(row["submission_attempt_id"]): row for row in submissions
    }

    selected_submission_ids = _integer_ids(
        payload.get("submission_attempt_ids"),
        "submission_attempt_ids",
    )
    if mode == "manual":
        try:
            selected_staff_ids = [int(payload.get("staff_user_id"))]
        except (TypeError, ValueError) as error:
            raise ApiError(
                "allocation_invalid",
                "Choose one Staff member.",
                422,
            ) from error
    else:
        selected_staff_ids = _integer_ids(
            payload.get("staff_user_ids"),
            "staff_user_ids",
        )

    if not selected_submission_ids:
        raise ApiError(
            "allocation_empty",
            "Select at least one eligible submission.",
            422,
        )
    if not selected_staff_ids:
        raise ApiError(
            "allocation_empty",
            "Select at least one Staff member.",
            422,
        )
    unknown_staff = [
        user_id for user_id in selected_staff_ids if user_id not in candidate_by_id
    ]
    if unknown_staff:
        raise ApiError(
            "allocation_staff_invalid",
            "One or more selected Staff accounts are no longer eligible.",
            409,
        )
    unknown_submissions = [
        attempt_id
        for attempt_id in selected_submission_ids
        if attempt_id not in submission_by_id
    ]
    if unknown_submissions:
        raise ApiError(
            "allocation_submission_invalid",
            "One or more selected submissions are no longer eligible.",
            409,
        )
    confirmed = [
        submission_by_id[attempt_id]
        for attempt_id in selected_submission_ids
        if submission_by_id[attempt_id]["is_confirmed"]
    ]
    if confirmed:
        raise ApiError(
            "allocation_confirmed_locked",
            "Return confirmed feedback to an editable state before reassignment.",
            409,
            {
                "submission_attempt_ids": [
                    row["submission_attempt_id"] for row in confirmed
                ]
            },
        )

    selected_rows = sorted(
        (submission_by_id[item] for item in selected_submission_ids),
        key=lambda row: (
            str(row["student_identifier"] or "").lower(),
            int(row["submission_attempt_id"]),
        ),
    )
    all_summary_ids = set(selected_staff_ids)
    all_summary_ids.update(
        int(row["marker_user_id"])
        for row in selected_rows
        if row["marker_user_id"] is not None
    )
    current_totals = _assignment_totals(
        conn,
        assessment_plan_id,
        sorted(all_summary_ids),
    )
    working_totals = dict(current_totals)

    if mode == "equal":
        for row in selected_rows:
            old_user_id = row["marker_user_id"]
            if old_user_id is not None and int(old_user_id) in selected_staff_ids:
                working_totals[int(old_user_id)] -= 1

    staff_order = sorted(
        selected_staff_ids,
        key=lambda user_id: (
            candidate_by_id[user_id]["email"].lower(),
            user_id,
        ),
    )
    operations = []
    for row in selected_rows:
        old_user_id = (
            int(row["marker_user_id"])
            if row["marker_user_id"] is not None
            else None
        )
        if mode == "manual":
            new_user_id = selected_staff_ids[0]
        else:
            new_user_id = min(
                staff_order,
                key=lambda user_id: (
                    working_totals.get(user_id, 0),
                    candidate_by_id[user_id]["email"].lower(),
                    user_id,
                ),
            )
            working_totals[new_user_id] = working_totals.get(new_user_id, 0) + 1

        if mode == "manual" and old_user_id != new_user_id:
            working_totals[new_user_id] = working_totals.get(new_user_id, 0) + 1
            if old_user_id is not None:
                working_totals[old_user_id] = working_totals.get(old_user_id, 0) - 1
        operations.append(
            {
                "submission_attempt_id": int(row["submission_attempt_id"]),
                "student_id": int(row["student_id"]),
                "student_identifier": row["student_identifier"],
                "student_name": row["student_name"],
                "tutorial_group_id": row.get("tutorial_group_id"),
                "tutorial_group_code": row.get("tutorial_group_code"),
                "old_staff_user_id": old_user_id,
                "new_staff_user_id": new_user_id,
                "change_type": (
                    "unchanged"
                    if old_user_id == new_user_id
                    else "assigned"
                    if old_user_id is None
                    else "reassigned"
                ),
            }
        )

    summary_ids = set(all_summary_ids)
    summary_ids.update(op["new_staff_user_id"] for op in operations)
    user_rows = conn.execute(
        f"""
        SELECT user_id, email, display_name
        FROM users
        WHERE user_id IN ({', '.join('?' for _ in summary_ids)})
        """,
        tuple(sorted(summary_ids)),
    ).fetchall()
    users = {int(row["user_id"]): dict(row) for row in user_rows}
    summaries = []
    for user_id in sorted(
        summary_ids,
        key=lambda item: (users[item]["email"].lower(), item),
    ):
        assigned_items = [
            op
            for op in operations
            if op["new_staff_user_id"] == user_id
            and op["change_type"] != "unchanged"
        ]
        removed_items = [
            op
            for op in operations
            if op["old_staff_user_id"] == user_id
            and op["new_staff_user_id"] != user_id
        ]
        summaries.append(
            {
                **users[user_id],
                "current_count": current_totals.get(user_id, 0),
                "assigned_count": len(assigned_items),
                "reassigned_away_count": len(removed_items),
                "final_count": (
                    current_totals.get(user_id, 0)
                    + len(assigned_items)
                    - len(removed_items)
                ),
                "submissions": [
                    {
                        "student_name": op["student_name"],
                        "student_identifier": op["student_identifier"],
                        "change_type": op["change_type"],
                    }
                    for op in operations
                    if op["new_staff_user_id"] == user_id
                ],
            }
        )

    canonical = {
        "assessment_plan_id": assessment_plan_id,
        "mode": mode,
        "operations": operations,
    }
    preview_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return {
        "assessment": dict(context),
        "mode": mode,
        "operations": operations,
        "summary": summaries,
        "change_count": sum(
            op["change_type"] != "unchanged" for op in operations
        ),
        "preview_hash": preview_hash,
    }


def preview_allocation(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
    payload: dict,
) -> dict:
    return _build_preview(
        conn,
        actor_user_id,
        assessment_plan_id,
        payload,
    )


def confirm_allocation(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
    payload: dict,
) -> dict:
    expected_hash = str(payload.get("preview_hash") or "")
    preview = _build_preview(
        conn,
        actor_user_id,
        assessment_plan_id,
        payload,
    )
    if not expected_hash or expected_hash != preview["preview_hash"]:
        raise ApiError(
            "allocation_preview_stale",
            "The allocation changed. Review the refreshed preview before confirming.",
            409,
            {"preview": preview},
        )
    if preview.get("exceptions"):
        raise ApiError(
            "tutorial_group_allocation_incomplete",
            "Resolve every Tutorial Group allocation issue before confirming.",
            409,
            {
                "preview": preview,
                "exception_count": len(preview["exceptions"]),
            },
        )

    changed = [
        operation
        for operation in preview["operations"]
        if operation["change_type"] != "unchanged"
    ]
    new_by_user: dict[int, list[dict]] = defaultdict(list)
    old_by_user: dict[int, list[dict]] = defaultdict(list)
    for operation in changed:
        attempt_id = operation["submission_attempt_id"]
        old_user_id = operation["old_staff_user_id"]
        new_user_id = operation["new_staff_user_id"]
        if old_user_id is not None:
            conn.execute(
                """
                UPDATE marker_assignments
                SET active = 0, ended_at = CURRENT_TIMESTAMP
                WHERE submission_attempt_id = ? AND active = 1
                """,
                (attempt_id,),
            )
            old_by_user[old_user_id].append(operation)
        assignment_id = int(
            conn.execute(
                """
                INSERT INTO marker_assignments
                    (submission_attempt_id, marker_user_id,
                     assigned_by_user_id, assignment_reason,
                     allocation_source, tutorial_group_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    new_user_id,
                    actor_user_id,
                    f"{preview['mode']} allocation",
                    preview["mode"],
                    operation.get("tutorial_group_id"),
                ),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO submission_workflow_states
                (submission_attempt_id)
            VALUES (?)
            """,
            (attempt_id,),
        )
        conn.execute(
            """
            UPDATE submission_workflow_states
            SET allocation_status = 'assigned',
                state_version = state_version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE submission_attempt_id = ?
            """,
            (attempt_id,),
        )
        conn.execute(
            """
            INSERT INTO submission_workflow_events
                (submission_attempt_id, actor_user_id, event_type,
                 from_state_json, to_state_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                actor_user_id,
                "staff_reassigned" if old_user_id is not None else "staff_assigned",
                json.dumps({"staff_user_id": old_user_id}),
                json.dumps({"staff_user_id": new_user_id}),
            ),
        )
        record_audit_event(
            conn,
            (
                "submission.staff_reassigned"
                if old_user_id is not None
                else "submission.staff_assigned"
            ),
            "marker_assignment",
            assignment_id,
            actor_user_id=actor_user_id,
            metadata={
                "assessment_plan_id": assessment_plan_id,
                "submission_attempt_id": attempt_id,
                "old_staff_user_id": old_user_id,
                "new_staff_user_id": new_user_id,
                "mode": preview["mode"],
            },
        )
        new_by_user[new_user_id].append(operation)

    if preview["mode"] == "tutorial_groups":
        conn.execute(
            """
            INSERT INTO assessment_allocation_policies
                (assessment_plan_id, strategy, active, enabled_by_user_id)
            VALUES (?, 'tutorial_groups', 1, ?)
            ON CONFLICT(assessment_plan_id)
            DO UPDATE SET
                strategy = 'tutorial_groups',
                active = 1,
                enabled_by_user_id = excluded.enabled_by_user_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (assessment_plan_id, actor_user_id),
        )

    assessment = preview["assessment"]
    action_url = (
        f"/educator/unit/{assessment['legacy_unit_id']}/submissions"
        f"?assessment_plan_id={assessment_plan_id}"
        if assessment.get("legacy_unit_id") is not None
        else "/educator"
    )
    for user_id, operations in new_by_user.items():
        _notify(
            conn,
            user_id,
            "submissions_assigned",
            f"New work in {assessment['assessment_code'] or assessment['title']}",
            (
                f"You have {len(operations)} newly assigned submission"
                f"{'s' if len(operations) != 1 else ''} in "
                f"{assessment['assessment_code'] or assessment['title']}."
            ),
            unit_offering_id=assessment["unit_offering_id"],
            assessment_plan_id=assessment_plan_id,
            action_url=action_url,
        )
    for user_id, operations in old_by_user.items():
        _notify(
            conn,
            user_id,
            "submissions_reassigned_away",
            f"Work reassigned in {assessment['assessment_code'] or assessment['title']}",
            (
                f"{len(operations)} submission"
                f"{'s were' if len(operations) != 1 else ' was'} reassigned "
                "to another Staff member."
            ),
            unit_offering_id=assessment["unit_offering_id"],
            assessment_plan_id=assessment_plan_id,
            action_url=action_url,
        )
    record_audit_event(
        conn,
        "assessment.allocation_confirmed",
        "assessment_plan",
        assessment_plan_id,
        actor_user_id=actor_user_id,
        metadata={
            "mode": preview["mode"],
            "change_count": len(changed),
            "preview_hash": preview["preview_hash"],
        },
    )
    conn.commit()
    return {
        "status": "confirmed",
        "change_count": len(changed),
        "allocation": get_assessment_allocation(
            conn,
            actor_user_id,
            assessment_plan_id,
        ),
    }


def auto_assign_submission_if_enabled(
    conn: sqlite3.Connection,
    submission_attempt_id: int,
) -> int | None:
    """Assign a new current summative attempt within its Tutorial Group.

    This helper deliberately leaves the attempt unassigned when membership or
    Staff configuration is incomplete. Existing assignments are never moved.
    The caller owns the surrounding transaction.
    """

    target = conn.execute(
        """
        SELECT attempt.submission_attempt_id, student.student_id,
               student.institution_student_identifier,
               student.full_name,
               plan.assessment_plan_id, plan.unit_offering_id,
               plan.assessment_code, plan.title,
               offering.legacy_unit_id,
               policy.enabled_by_user_id,
               membership.tutorial_group_id,
               tutorial_group.group_code
        FROM submission_attempts AS attempt
        JOIN assessment_activities AS activity
          ON activity.assessment_activity_id = attempt.assessment_activity_id
        JOIN assessment_plan_versions AS version
          ON version.assessment_plan_version_id =
             activity.assessment_plan_version_id
        JOIN assessment_plans AS plan
          ON plan.assessment_plan_id = version.assessment_plan_id
        JOIN unit_offerings AS offering
          ON offering.unit_offering_id = plan.unit_offering_id
        JOIN assessment_allocation_policies AS policy
          ON policy.assessment_plan_id = plan.assessment_plan_id
         AND policy.strategy = 'tutorial_groups'
         AND policy.active = 1
        JOIN submission_participants AS participant
          ON participant.submission_attempt_id = attempt.submission_attempt_id
         AND participant.participant_role = 'primary'
        JOIN students AS student ON student.student_id = participant.student_id
        LEFT JOIN student_tutorial_memberships AS membership
          ON membership.unit_offering_id = plan.unit_offering_id
         AND membership.student_id = student.student_id
         AND membership.active = 1
        LEFT JOIN tutorial_groups AS tutorial_group
          ON tutorial_group.tutorial_group_id = membership.tutorial_group_id
         AND tutorial_group.active = 1
        WHERE attempt.submission_attempt_id = ?
          AND attempt.purpose = 'summative'
          AND attempt.validity_status = 'valid'
          AND NOT EXISTS (
              SELECT 1 FROM marker_assignments AS existing
              WHERE existing.submission_attempt_id = attempt.submission_attempt_id
                AND existing.active = 1
          )
        """,
        (submission_attempt_id,),
    ).fetchone()
    if target is None or target["tutorial_group_id"] is None:
        return None
    group_id = int(target["tutorial_group_id"])
    staff_rows = conn.execute(
        """
        SELECT user.user_id, user.email, user.account_status,
               EXISTS (
                   SELECT 1 FROM unit_role_assignments AS role
                   WHERE role.unit_offering_id = ?
                     AND role.user_id = user.user_id
                     AND role.role IN ('unit_admin', 'staff')
                     AND role.active = 1
               ) AS has_unit_role
        FROM tutorial_group_staff AS link
        JOIN users AS user ON user.user_id = link.user_id
        WHERE link.tutorial_group_id = ? AND link.active = 1
        ORDER BY lower(user.email), user.user_id
        """,
        (target["unit_offering_id"], group_id),
    ).fetchall()
    if not staff_rows or any(
        row["account_status"] != "active" or not row["has_unit_role"]
        for row in staff_rows
    ):
        return None
    staff_ids = [int(row["user_id"]) for row in staff_rows]
    counts = {user_id: 0 for user_id in staff_ids}
    for submission in _eligible_submissions(
        conn,
        int(target["assessment_plan_id"]),
    ):
        if submission.get("tutorial_group_id") != group_id:
            continue
        marker_user_id = submission.get("marker_user_id")
        if marker_user_id is not None and int(marker_user_id) in counts:
            counts[int(marker_user_id)] += 1
    staff_by_id = {int(row["user_id"]): row for row in staff_rows}
    marker_user_id = min(
        staff_ids,
        key=lambda user_id: (
            counts[user_id],
            str(staff_by_id[user_id]["email"]).lower(),
            user_id,
        ),
    )
    actor_user_id = int(target["enabled_by_user_id"])
    assignment_id = int(
        conn.execute(
            """
            INSERT INTO marker_assignments
                (submission_attempt_id, marker_user_id,
                 assigned_by_user_id, assignment_reason,
                 allocation_source, tutorial_group_id)
            VALUES (?, ?, ?, 'tutorial_groups automatic allocation',
                    'tutorial_groups', ?)
            """,
            (
                submission_attempt_id,
                marker_user_id,
                actor_user_id,
                group_id,
            ),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO submission_workflow_states(submission_attempt_id)
        VALUES (?)
        """,
        (submission_attempt_id,),
    )
    conn.execute(
        """
        UPDATE submission_workflow_states
        SET allocation_status = 'assigned',
            state_version = state_version + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE submission_attempt_id = ?
        """,
        (submission_attempt_id,),
    )
    conn.execute(
        """
        INSERT INTO submission_workflow_events
            (submission_attempt_id, actor_user_id, event_type,
             from_state_json, to_state_json)
        VALUES (?, ?, 'staff_assigned', ?, ?)
        """,
        (
            submission_attempt_id,
            actor_user_id,
            json.dumps({"staff_user_id": None}),
            json.dumps(
                {
                    "staff_user_id": marker_user_id,
                    "tutorial_group_id": group_id,
                }
            ),
        ),
    )
    record_audit_event(
        conn,
        "submission.staff_assigned",
        "marker_assignment",
        assignment_id,
        actor_user_id=actor_user_id,
        metadata={
            "assessment_plan_id": target["assessment_plan_id"],
            "submission_attempt_id": submission_attempt_id,
            "new_staff_user_id": marker_user_id,
            "mode": "tutorial_groups",
            "tutorial_group_id": group_id,
            "automatic_new_submission": True,
        },
    )
    action_url = (
        f"/educator/unit/{target['legacy_unit_id']}/submissions"
        f"?assessment_plan_id={target['assessment_plan_id']}"
        if target["legacy_unit_id"] is not None
        else "/educator"
    )
    _notify(
        conn,
        marker_user_id,
        "submissions_assigned",
        f"New work in {target['assessment_code'] or target['title']}",
        (
            "A new submission was assigned from Tutorial Group "
            f"{target['group_code']}."
        ),
        unit_offering_id=int(target["unit_offering_id"]),
        assessment_plan_id=int(target["assessment_plan_id"]),
        action_url=action_url,
    )
    return marker_user_id


def reopen_submission(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
    submission_attempt_id: int,
) -> None:
    _assessment_context(conn, actor_user_id, assessment_plan_id)
    eligible = {
        int(row["submission_attempt_id"]): row
        for row in _eligible_submissions(conn, assessment_plan_id)
    }
    row = eligible.get(submission_attempt_id)
    if row is None:
        raise ApiError(
            "allocation_submission_invalid",
            "This submission is no longer current and eligible.",
            409,
        )
    if row["marking_status"] != "marker_confirmed":
        raise ApiError(
            "submission_not_confirmed",
            "Only confirmed feedback needs to be returned for editing.",
            409,
        )
    conn.execute(
        """
        UPDATE submission_workflow_states
        SET marking_status = 'in_progress',
            marker_confirmed_by_user_id = NULL,
            marker_confirmed_at = NULL,
            state_version = state_version + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE submission_attempt_id = ?
        """,
        (submission_attempt_id,),
    )
    conn.execute(
        """
        INSERT INTO submission_workflow_events
            (submission_attempt_id, actor_user_id, event_type, comment)
        VALUES (?, ?, 'feedback_returned',
                'Confirmed feedback returned to an editable state.')
        """,
        (submission_attempt_id, actor_user_id),
    )
    if row["marker_user_id"] is not None:
        _notify(
            conn,
            int(row["marker_user_id"]),
            "feedback_returned",
            "Feedback returned for editing",
            f"Feedback for student {row['student_identifier']} is editable again.",
            assessment_plan_id=assessment_plan_id,
        )
    record_audit_event(
        conn,
        "submission.feedback_reopened",
        "submission_attempt",
        submission_attempt_id,
        actor_user_id=actor_user_id,
        metadata={"assessment_plan_id": assessment_plan_id},
    )
    conn.commit()


def list_notifications(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    unread_only: bool = True,
    limit: int = 20,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT notification_id, event_type, title, message,
               unit_offering_id, assessment_plan_id, action_url,
               read_at, created_at
        FROM user_notifications
        WHERE user_id = ?
          AND (? = 0 OR read_at IS NULL)
        ORDER BY created_at DESC, notification_id DESC
        LIMIT ?
        """,
        (user_id, 1 if unread_only else 0, max(1, min(limit, 50))),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_notification_read(
    conn: sqlite3.Connection,
    user_id: int,
    notification_id: int,
) -> dict:
    row = conn.execute(
        """
        SELECT notification_id, action_url
        FROM user_notifications
        WHERE notification_id = ? AND user_id = ?
        """,
        (notification_id, user_id),
    ).fetchone()
    if row is None:
        raise ApiError(
            "notification_not_found",
            "Notification not found.",
            404,
        )
    conn.execute(
        """
        UPDATE user_notifications
        SET read_at = COALESCE(read_at, CURRENT_TIMESTAMP)
        WHERE notification_id = ?
        """,
        (notification_id,),
    )
    conn.commit()
    return {"action_url": row["action_url"] or "/educator"}
