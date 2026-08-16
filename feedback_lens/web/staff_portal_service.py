from __future__ import annotations

import sqlite3

from feedback_lens.feedback.llm.providers import (
    DEFAULT_FEEDBACK_MODEL,
    DEFAULT_FEEDBACK_PROVIDER,
)
from feedback_lens.web.security import can_administer_unit, has_unit_role


def unit_staff_context(
    conn: sqlite3.Connection,
    user_id: int,
    legacy_unit_id: int,
) -> dict | None:
    row = conn.execute(
        """
        SELECT
            offering.unit_offering_id,
            offering.legacy_unit_id,
            offering.academic_year,
            offering.teaching_period,
            course.organization_id,
            course.course_code AS unit_code,
            course.course_name AS unit_name
        FROM unit_offerings AS offering
        JOIN courses AS course ON course.course_id = offering.course_id
        WHERE offering.legacy_unit_id = ?
          AND offering.status != 'archived'
        """,
        (legacy_unit_id,),
    ).fetchone()
    if row is None:
        return None
    is_admin = can_administer_unit(
        conn,
        user_id,
        int(row["unit_offering_id"]),
    )
    is_staff = has_unit_role(
        conn,
        user_id,
        int(row["unit_offering_id"]),
        ("staff",),
    )
    if not (is_admin or is_staff):
        return None
    result = dict(row)
    result["is_admin"] = is_admin
    return result


def fetch_authorised_submission(
    conn: sqlite3.Connection,
    submission_id: int,
    user_id: int,
    tutor_id: int | None = None,
) -> sqlite3.Row | None:
    v2 = conn.execute(
        """
        SELECT
            submission.submission_id,
            submission.assignment_id,
            assignment.unit_id,
            attempt.submission_attempt_id,
            marker.marker_user_id
        FROM student_submissions AS submission
        JOIN assignments AS assignment
          ON assignment.assignment_id = submission.assignment_id
        JOIN submission_attempts AS attempt
          ON attempt.legacy_submission_id = submission.submission_id
        JOIN current_summative_attempts AS current
          ON current.submission_attempt_id = attempt.submission_attempt_id
        LEFT JOIN marker_assignments AS marker
          ON marker.submission_attempt_id = attempt.submission_attempt_id
         AND marker.active = 1
        WHERE submission.submission_id = ?
          AND attempt.validity_status = 'valid'
        """,
        (submission_id,),
    ).fetchone()
    if v2 is not None:
        return v2 if v2["marker_user_id"] == user_id else None
    if tutor_id is None:
        return None
    return conn.execute(
        """
        SELECT
            submission.submission_id,
            submission.assignment_id,
            assignment.unit_id
        FROM student_submissions AS submission
        JOIN assignments AS assignment
          ON assignment.assignment_id = submission.assignment_id
        JOIN unit_tutors AS unit_tutor
          ON unit_tutor.unit_id = assignment.unit_id
        WHERE submission.submission_id = ?
          AND unit_tutor.tutor_id = ?
        """,
        (submission_id, tutor_id),
    ).fetchone()


def assignment_feedback_model(
    conn: sqlite3.Connection,
    assignment_id: int,
) -> tuple[str, str]:
    row = conn.execute(
        """
        SELECT default_llm_provider, default_llm_model
        FROM assessment_plans
        WHERE legacy_assignment_id = ?
          AND status != 'archived'
        ORDER BY assessment_plan_id DESC
        LIMIT 1
        """,
        (assignment_id,),
    ).fetchone()
    if row is None:
        return DEFAULT_FEEDBACK_PROVIDER, DEFAULT_FEEDBACK_MODEL
    return (
        row["default_llm_provider"] or DEFAULT_FEEDBACK_PROVIDER,
        row["default_llm_model"] or DEFAULT_FEEDBACK_MODEL,
    )


def fetch_authorised_generation(
    conn: sqlite3.Connection,
    generation_id: int,
    user_id: int,
    tutor_id: int | None = None,
    *,
    allow_admin_view: bool = False,
) -> sqlite3.Row | None:
    v2 = conn.execute(
        """
        SELECT
            generation.generation_id,
            generation.submission_id,
            generation.assignment_id,
            attempt.submission_attempt_id,
            offering.legacy_unit_id AS unit_id,
            marker.marker_user_id,
            offering.unit_offering_id
        FROM generation_runs AS generation
        JOIN submission_attempts AS attempt
          ON attempt.submission_attempt_id = COALESCE(
              generation.submission_attempt_id,
              (
                  SELECT mapped.submission_attempt_id
                  FROM submission_attempts AS mapped
                  WHERE mapped.legacy_submission_id =
                        generation.submission_id
              )
          )
        JOIN current_summative_attempts AS current
          ON current.submission_attempt_id = attempt.submission_attempt_id
        JOIN assessment_activities AS activity
          ON activity.assessment_activity_id = attempt.assessment_activity_id
        JOIN assessment_plan_versions AS version
          ON version.assessment_plan_version_id =
             activity.assessment_plan_version_id
        JOIN assessment_plans AS plan
          ON plan.assessment_plan_id = version.assessment_plan_id
        JOIN unit_offerings AS offering
          ON offering.unit_offering_id = plan.unit_offering_id
        LEFT JOIN marker_assignments AS marker
          ON marker.submission_attempt_id = attempt.submission_attempt_id
         AND marker.active = 1
        WHERE generation.generation_id = ?
          AND attempt.validity_status = 'valid'
        """,
        (generation_id,),
    ).fetchone()
    if v2 is not None:
        if v2["marker_user_id"] == user_id:
            return v2
        if allow_admin_view and can_administer_unit(
            conn,
            user_id,
            int(v2["unit_offering_id"]),
        ):
            return v2
        return None
    if tutor_id is None:
        return None
    return conn.execute(
        """
        SELECT
            generation.generation_id,
            generation.submission_id,
            generation.assignment_id,
            assignment.unit_id
        FROM generation_runs AS generation
        JOIN assignments AS assignment
          ON assignment.assignment_id = generation.assignment_id
        JOIN unit_tutors AS unit_tutor
          ON unit_tutor.unit_id = assignment.unit_id
        WHERE generation.generation_id = ?
          AND unit_tutor.tutor_id = ?
        """,
        (generation_id, tutor_id),
    ).fetchone()


def get_unit_dashboard_data(
    conn: sqlite3.Connection,
    user_id: int,
    legacy_unit_id: int,
) -> dict | None:
    unit = unit_staff_context(conn, user_id, legacy_unit_id)
    if unit is None:
        return None
    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS total_submissions,
            SUM(CASE
                WHEN workflow.marking_status = 'marker_confirmed'
                THEN 1 ELSE 0 END) AS reviewed_count,
            SUM(CASE
                WHEN workflow.marking_status != 'marker_confirmed'
                 AND workflow.ai_generation_status = 'generated'
                THEN 1 ELSE 0 END) AS ai_generated_count,
            SUM(CASE
                WHEN workflow.ai_generation_status != 'generated'
                THEN 1 ELSE 0 END) AS pending_count
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
        LEFT JOIN marker_assignments AS marker
          ON marker.submission_attempt_id = attempt.submission_attempt_id
         AND marker.active = 1
        LEFT JOIN submission_workflow_states AS workflow
          ON workflow.submission_attempt_id = attempt.submission_attempt_id
        WHERE plan.unit_offering_id = ?
          AND attempt.validity_status = 'valid'
          AND (? = 1 OR marker.marker_user_id = ?)
        """,
        (
            unit["unit_offering_id"],
            1 if unit["is_admin"] else 0,
            user_id,
        ),
    ).fetchone()
    return {"unit": unit, "counts": dict(counts) if counts else {}}


def get_unit_submissions_data(
    conn: sqlite3.Connection,
    user_id: int,
    legacy_unit_id: int,
) -> dict | None:
    unit = unit_staff_context(conn, user_id, legacy_unit_id)
    if unit is None:
        return None
    rows = conn.execute(
        """
        SELECT
            attempt.legacy_submission_id AS submission_id,
            attempt.submission_attempt_id,
            student.institution_student_identifier AS student_identifier,
            student.full_name AS student_name,
            attempt.submitted_at,
            plan.title AS assignment_name,
            plan.legacy_assignment_id AS assignment_id,
            plan.assessment_plan_id,
            generation.generation_id,
            generation.status AS generation_status,
            generation.llm_provider,
            generation.llm_model,
            plan.default_llm_provider,
            plan.default_llm_model,
            failure.generation_id AS failed_generation_id,
            failure.llm_provider AS failed_llm_provider,
            failure.llm_model AS failed_llm_model,
            failure.error_message AS generation_error_message,
            failure.provider_error_code,
            failure.provider_http_status,
            failure.provider_request_id,
            failure.completed_at AS generation_error_at,
            CASE
                WHEN failure.generation_id IS NOT NULL
                 AND failure.generation_id > COALESCE(generation.generation_id, 0)
                    THEN 1 ELSE 0
            END AS has_current_generation_error,
            overall.overall_grade_band,
            overall.final_mark,
            marker.marker_user_id,
            marker_user.email AS marker_email,
            CASE WHEN marker.marker_user_id = ? THEN 1 ELSE 0 END
                AS can_generate,
            CASE
                WHEN workflow.marking_status = 'marker_confirmed'
                    THEN 'reviewed'
                WHEN generation.status = 'completed' THEN 'ai_generated'
                ELSE 'pending'
            END AS review_status
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
        LEFT JOIN marker_assignments AS marker
          ON marker.submission_attempt_id = attempt.submission_attempt_id
         AND marker.active = 1
        LEFT JOIN users AS marker_user
          ON marker_user.user_id = marker.marker_user_id
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
        LEFT JOIN generation_runs AS failure
          ON failure.generation_id = (
              SELECT MAX(candidate.generation_id)
              FROM generation_runs AS candidate
              WHERE candidate.status = 'failed'
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
        WHERE plan.unit_offering_id = ?
          AND attempt.validity_status = 'valid'
          AND attempt.status IN ('ready', 'imported', 'processing', 'completed')
          AND (? = 1 OR marker.marker_user_id = ?)
        ORDER BY lower(student.institution_student_identifier),
                 attempt.submission_attempt_id
        """,
        (
            user_id,
            unit["unit_offering_id"],
            1 if unit["is_admin"] else 0,
            user_id,
        ),
    ).fetchall()
    return {"unit": unit, "submissions": [dict(row) for row in rows]}


def list_staff_units(
    conn: sqlite3.Connection,
    user_id: int,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            offering.legacy_unit_id AS unit_id,
            course.course_code AS unit_code,
            course.course_name AS unit_name,
            offering.teaching_period AS semester,
            offering.academic_year AS year,
            COUNT(DISTINCT CASE
                WHEN marker.marker_user_id = ?
                THEN attempt.submission_attempt_id END) AS student_count,
            COUNT(DISTINCT CASE
                WHEN marker.marker_user_id = ?
                 AND workflow.marking_status = 'marker_confirmed'
                THEN attempt.submission_attempt_id END) AS completed_count,
            COUNT(DISTINCT CASE
                WHEN marker.marker_user_id = ?
                 AND COALESCE(workflow.marking_status, 'not_started')
                     != 'marker_confirmed'
                THEN attempt.submission_attempt_id END) AS pending_count
        FROM unit_offerings AS offering
        JOIN courses AS course ON course.course_id = offering.course_id
        LEFT JOIN assessment_plans AS plan
          ON plan.unit_offering_id = offering.unit_offering_id
         AND plan.status != 'archived'
        LEFT JOIN assessment_plan_versions AS version
          ON version.assessment_plan_id = plan.assessment_plan_id
        LEFT JOIN assessment_activities AS activity
          ON activity.assessment_plan_version_id =
             version.assessment_plan_version_id
         AND activity.purpose = 'summative'
        LEFT JOIN submission_attempts AS attempt
          ON attempt.assessment_activity_id = activity.assessment_activity_id
         AND attempt.validity_status = 'valid'
         AND EXISTS (
             SELECT 1 FROM current_summative_attempts AS current
             WHERE current.submission_attempt_id = attempt.submission_attempt_id
         )
        LEFT JOIN marker_assignments AS marker
          ON marker.submission_attempt_id = attempt.submission_attempt_id
         AND marker.active = 1
        LEFT JOIN submission_workflow_states AS workflow
          ON workflow.submission_attempt_id = attempt.submission_attempt_id
        WHERE offering.status != 'archived'
          AND offering.legacy_unit_id IS NOT NULL
          AND (
              EXISTS (
                  SELECT 1 FROM unit_role_assignments AS role
                  WHERE role.unit_offering_id = offering.unit_offering_id
                    AND role.user_id = ?
                    AND role.role IN ('unit_admin', 'staff')
                    AND role.active = 1
              )
              OR EXISTS (
                  SELECT 1
                  FROM organization_role_assignments AS org_role
                  WHERE org_role.organization_id = course.organization_id
                    AND org_role.user_id = ?
                    AND org_role.role = 'chief_admin'
                    AND org_role.active = 1
              )
          )
        GROUP BY offering.unit_offering_id
        ORDER BY lower(course.course_code), offering.academic_year DESC
        """,
        (user_id, user_id, user_id, user_id, user_id),
    ).fetchall()
    return [dict(row) for row in rows]


def record_generated_feedback(
    conn: sqlite3.Connection,
    submission: sqlite3.Row,
    user_id: int,
    generation_id: int,
) -> None:
    if "submission_attempt_id" not in submission.keys():
        return
    attempt_id = int(submission["submission_attempt_id"])
    conn.execute(
        """
        UPDATE generation_runs
        SET submission_attempt_id = ?,
            started_by_user_id = ?,
            feedback_purpose = 'summative'
        WHERE generation_id = ?
        """,
        (attempt_id, user_id, generation_id),
    )
    conn.execute(
        """
        UPDATE submission_workflow_states
        SET ai_generation_status = 'generated',
            marking_status = CASE
                WHEN marking_status = 'not_started' THEN 'in_progress'
                ELSE marking_status
            END,
            current_generation_id = ?,
            state_version = state_version + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE submission_attempt_id = ?
        """,
        (generation_id, attempt_id),
    )
    conn.commit()


def generation_workflow(
    conn: sqlite3.Connection,
    generation: sqlite3.Row,
) -> sqlite3.Row | None:
    if "submission_attempt_id" not in generation.keys():
        return None
    return conn.execute(
        """
        SELECT marking_status, current_generation_id
        FROM submission_workflow_states
        WHERE submission_attempt_id = ?
        """,
        (generation["submission_attempt_id"],),
    ).fetchone()


def update_feedback_workflow(
    conn: sqlite3.Connection,
    generation: sqlite3.Row,
    user_id: int,
    review_status: str | None,
) -> None:
    if "submission_attempt_id" not in generation.keys() or not review_status:
        return
    marking_status = (
        "marker_confirmed" if review_status == "reviewed" else "in_progress"
    )
    conn.execute(
        """
        UPDATE submission_workflow_states
        SET marking_status = ?,
            marker_confirmed_by_user_id = CASE
                WHEN ? = 'marker_confirmed' THEN ? ELSE NULL END,
            marker_confirmed_at = CASE
                WHEN ? = 'marker_confirmed'
                THEN CURRENT_TIMESTAMP ELSE NULL END,
            state_version = state_version + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE submission_attempt_id = ?
        """,
        (
            marking_status,
            marking_status,
            user_id,
            marking_status,
            generation["submission_attempt_id"],
        ),
    )
    conn.execute(
        """
        INSERT INTO submission_workflow_events
            (submission_attempt_id, actor_user_id, event_type)
        VALUES (?, ?, ?)
        """,
        (
            generation["submission_attempt_id"],
            user_id,
            (
                "feedback_marker_confirmed"
                if review_status == "reviewed"
                else "feedback_editing"
            ),
        ),
    )
