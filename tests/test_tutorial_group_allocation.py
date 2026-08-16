import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module

from feedback_lens.web.admin_service import get_assessment_detail
from feedback_lens.web.allocation_service import (
    auto_assign_submission_if_enabled,
    confirm_allocation,
    preview_allocation,
)
from feedback_lens.web.common import student_import_is_ready
from feedback_lens.web.errors import ApiError
from feedback_lens.web.storage import StoredUpload
from feedback_lens.web.upload_service import create_submission_batch
from feedback_lens.web.tutorial_group_service import (
    add_tutorial_group_staff,
    apply_tutorial_group_import,
    complete_staff_activation,
    create_tutorial_group,
    create_tutorial_group_import,
    get_tutorial_group_overview,
    import_tutorial_group_staff,
    invite_tutorial_group_staff,
    set_tutorial_staff_groups,
)
from tests.test_staff_allocation import _allocation_connection


def _create_group(conn, code="TUT-01"):
    overview = create_tutorial_group(
        conn,
        1,
        1,
        {"group_code": code, "group_name": code},
    )
    return next(
        int(group["tutorial_group_id"])
        for group in overview["groups"]
        if group["group_code"] == code
    )


def _assign_every_student_to_group(conn, group_id):
    conn.executemany(
        """
        INSERT INTO student_tutorial_memberships
            (unit_offering_id, student_id, tutorial_group_id, source)
        VALUES (1, ?, ?, 'manual')
        """,
        [
            (int(row["student_id"]), group_id)
            for row in conn.execute(
                """
                SELECT student_id FROM student_enrolments
                WHERE unit_offering_id = 1 AND status = 'active'
                ORDER BY student_id
                """
            )
        ],
    )
    conn.commit()


def _unassigned_attempt_count(conn):
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM current_summative_attempts AS current
            LEFT JOIN marker_assignments AS assignment
              ON assignment.submission_attempt_id = current.submission_attempt_id
             AND assignment.active = 1
            LEFT JOIN submission_workflow_states AS workflow
              ON workflow.submission_attempt_id = current.submission_attempt_id
            WHERE assignment.marker_user_id IS NULL
              AND COALESCE(workflow.marking_status, 'not_started') != 'marker_confirmed'
            """
        ).fetchone()[0]
    )


class TutorialGroupAllocationTests(unittest.TestCase):
    def test_tutorial_preview_requires_membership_configuration(self):
        with _allocation_connection() as conn:
            preview = preview_allocation(
                conn,
                1,
                1,
                {"mode": "tutorial_groups"},
            )
            self.assertFalse(preview["can_confirm"])
            self.assertIn(
                "missing_tutorial_memberships",
                {item["reason"] for item in preview["exceptions"]},
            )

    def test_csv_preview_apply_and_transfer_preserves_existing_assignment(self):
        with _allocation_connection() as conn:
            first_content = (
                "student_id,tutorial_group\n"
                "11111111,TUT-01\n"
                "12345678,TUT-01\n"
                "22222222,TUT-02\n"
                "33333333,TUT-02\n"
                "44444444,TUT-02\n"
            )
            headers = ["student_id", "tutorial_group"]
            first_rows = [
                {"student_id": line.split(",")[0], "tutorial_group": line.split(",")[1]}
                for line in first_content.strip().splitlines()[1:]
            ]
            with patch(
                "feedback_lens.web.tutorial_group_service._read_group_csv",
                return_value=(headers, first_rows),
            ):
                first = create_tutorial_group_import(
                    conn,
                    1,
                    1,
                    StoredUpload(
                        original_file_name="tutorials.csv",
                        storage_path=Path("tutorials.csv"),
                        content_hash=hashlib.sha256(first_content.encode()).hexdigest(),
                        size_bytes=len(first_content.encode()),
                        extension=".csv",
                    ),
                )
            self.assertEqual(first["tutorial_group_import"]["assigned_count"], 5)
            self.assertEqual(first["tutorial_group_import"]["invalid_row_count"], 0)
            apply_tutorial_group_import(
                conn,
                1,
                first["tutorial_group_import"]["tutorial_group_import_id"],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM student_tutorial_memberships WHERE active = 1"
                ).fetchone()[0],
                5,
            )

            student_id = int(
                conn.execute(
                    """
                    SELECT student_id FROM students
                    WHERE institution_student_identifier = '11111111'
                    """
                ).fetchone()[0]
            )
            attempt_id = int(
                conn.execute(
                    """
                    SELECT submission_attempt_id
                    FROM current_summative_attempts
                    WHERE student_id = ?
                    """,
                    (student_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO marker_assignments
                    (submission_attempt_id, marker_user_id,
                     assigned_by_user_id, assignment_reason)
                VALUES (?, 2, 1, 'manual allocation')
                """,
                (attempt_id,),
            )
            conn.commit()

            second_content = first_content.replace(
                "11111111,TUT-01",
                "11111111,TUT-02",
            )
            second_rows = [
                {"student_id": line.split(",")[0], "tutorial_group": line.split(",")[1]}
                for line in second_content.strip().splitlines()[1:]
            ]
            with patch(
                "feedback_lens.web.tutorial_group_service._read_group_csv",
                return_value=(headers, second_rows),
            ):
                second = create_tutorial_group_import(
                    conn,
                    1,
                    1,
                    StoredUpload(
                        original_file_name="tutorials-moved.csv",
                        storage_path=Path("tutorials-moved.csv"),
                        content_hash=hashlib.sha256(second_content.encode()).hexdigest(),
                        size_bytes=len(second_content.encode()),
                        extension=".csv",
                    ),
                )
            self.assertEqual(second["tutorial_group_import"]["moved_count"], 1)
            apply_tutorial_group_import(
                conn,
                1,
                second["tutorial_group_import"]["tutorial_group_import_id"],
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT marker_user_id FROM marker_assignments
                    WHERE submission_attempt_id = ? AND active = 1
                    """,
                    (attempt_id,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM student_tutorial_memberships
                    WHERE student_id = ?
                    """,
                    (student_id,),
                ).fetchone()[0],
                2,
            )

    def test_allocate_workbook_imports_only_selected_activity(self):
        def sheet_rows(code, identifiers):
            return [
                [],
                ["FIT1056_CL_S2_ON-CAMPUS"],
                [code, "Wed, 08:00, 120 minutes"],
                [None, "Location: Test room"],
                [None, "Staff: -"],
                [],
                ["student_code", "last_name", "preferred_name", "email_address"],
                *[
                    [int(identifier), "Test", identifier, f"{identifier}@example.test"]
                    for identifier in identifiers
                ],
            ]

        with _allocation_connection() as conn, patch(
            "feedback_lens.web.tutorial_group_service._read_excel_sheets",
            return_value=[
                (
                    "Applied-01_OnCampus(1)",
                    sheet_rows("Applied-01_OnCampus", ["11111111", "12345678"]),
                ),
                (
                    "Workshop-01_OnCampus(2)",
                    sheet_rows("Workshop-01_OnCampus", ["22222222"]),
                ),
            ],
        ):
            content_hash = hashlib.sha256(b"allocate-workbook").hexdigest()
            preview = create_tutorial_group_import(
                conn,
                1,
                1,
                StoredUpload(
                    original_file_name="allocate.xlsx",
                    storage_path=Path("allocate.xlsx"),
                    content_hash=content_hash,
                    size_bytes=1024,
                    extension=".xlsx",
                ),
                activity_type="Applied",
            )
            import_record = preview["tutorial_group_import"]
            self.assertEqual(import_record["total_row_count"], 2)
            self.assertEqual(import_record["assigned_count"], 2)
            self.assertEqual(import_record["invalid_row_count"], 0)
            self.assertTrue(
                all(
                    row["group_code"] == "Applied-01_OnCampus"
                    for row in preview["rows"]
                )
            )
            apply_tutorial_group_import(
                conn,
                1,
                int(import_record["tutorial_group_import_id"]),
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM tutorial_groups
                    WHERE unit_offering_id = 1
                      AND group_code = 'Applied-01_OnCampus'
                    """
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM tutorial_groups
                    WHERE unit_offering_id = 1
                      AND group_code LIKE 'Workshop-%'
                    """
                ).fetchone()[0],
                0,
            )

    def test_applied_workbook_registers_enrols_and_groups_new_students(self):
        def sheet_rows(code, students):
            return [
                [],
                ["FIT1056_CL_S2_ON-CAMPUS"],
                [code, "Wed, 08:00, 120 minutes"],
                [None, "Location: Test room"],
                [None, "Staff: -"],
                [],
                ["student_code", "last_name", "preferred_name", "email_address"],
                *students,
            ]

        with _allocation_connection() as conn:
            preserved_group_id = _create_group(conn, "Applied-OLD")
            preserved_student = conn.execute(
                """
                SELECT student_id FROM students
                WHERE institution_student_identifier = '11111111'
                """
            ).fetchone()
            conn.execute(
                """
                INSERT INTO student_tutorial_memberships
                    (unit_offering_id, student_id, tutorial_group_id, source)
                VALUES (1, ?, ?, 'manual')
                """,
                (preserved_student["student_id"], preserved_group_id),
            )
            conn.commit()

            with patch(
                "feedback_lens.web.tutorial_group_service._read_excel_sheets",
                return_value=[
                    (
                        "Applied-01_OnCampus(1)",
                        sheet_rows(
                            "Applied-01_OnCampus",
                            [
                                [90000001, "Alpha", "Alice", "alice.alpha@example.test"],
                                [90000002, "Beta", "Bob", "bob.beta@example.test"],
                            ],
                        ),
                    ),
                    (
                        "Workshop-01_OnCampus(2)",
                        sheet_rows(
                            "Workshop-01_OnCampus",
                            [
                                [90000003, "Gamma", "Gina", "gina.gamma@example.test"],
                            ],
                        ),
                    ),
                ],
            ):
                preview = create_tutorial_group_import(
                    conn,
                    1,
                    1,
                    StoredUpload(
                        original_file_name="allocate-full.xlsx",
                        storage_path=Path("allocate-full.xlsx"),
                        content_hash=hashlib.sha256(b"allocate-full").hexdigest(),
                        size_bytes=2048,
                        extension=".xlsx",
                    ),
                )

            registration = preview["student_registration"]
            self.assertTrue(registration["available"])
            self.assertEqual(registration["created_count"], 2)
            self.assertEqual(registration["enrolled_count"], 2)
            self.assertGreaterEqual(registration["not_in_applied_count"], 1)
            self.assertIn(
                "11111111",
                {
                    row["institution_student_identifier"]
                    for row in registration["not_in_applied_students"]
                },
            )
            self.assertFalse(student_import_is_ready(conn, 1))

            apply_tutorial_group_import(
                conn,
                1,
                int(preview["tutorial_group_import"]["tutorial_group_import_id"]),
            )

            imported = conn.execute(
                """
                SELECT student.institution_student_identifier,
                       student.full_name, student.institution_email,
                       enrolment.status, group_row.group_code
                FROM students AS student
                JOIN student_enrolments AS enrolment
                  ON enrolment.student_id = student.student_id
                 AND enrolment.unit_offering_id = 1
                JOIN student_tutorial_memberships AS membership
                  ON membership.student_id = student.student_id
                 AND membership.unit_offering_id = 1
                 AND membership.active = 1
                JOIN tutorial_groups AS group_row
                  ON group_row.tutorial_group_id = membership.tutorial_group_id
                WHERE student.institution_student_identifier LIKE '9000000%'
                ORDER BY student.institution_student_identifier
                """
            ).fetchall()
            self.assertEqual(
                [
                    (
                        row["institution_student_identifier"],
                        row["full_name"],
                        row["institution_email"],
                        row["status"],
                        row["group_code"],
                    )
                    for row in imported
                ],
                [
                    (
                        "90000001",
                        "Alice Alpha",
                        "alice.alpha@example.test",
                        "active",
                        "Applied-01_OnCampus",
                    ),
                    (
                        "90000002",
                        "Bob Beta",
                        "bob.beta@example.test",
                        "active",
                        "Applied-01_OnCampus",
                    ),
                ],
            )
            self.assertIsNone(
                conn.execute(
                    """
                    SELECT student_id FROM students
                    WHERE institution_student_identifier = '90000003'
                    """
                ).fetchone()
            )
            preserved = conn.execute(
                """
                SELECT enrolment.status, group_row.group_code
                FROM student_enrolments AS enrolment
                JOIN student_tutorial_memberships AS membership
                  ON membership.unit_offering_id = enrolment.unit_offering_id
                 AND membership.student_id = enrolment.student_id
                 AND membership.active = 1
                JOIN tutorial_groups AS group_row
                  ON group_row.tutorial_group_id = membership.tutorial_group_id
                WHERE enrolment.unit_offering_id = 1
                  AND enrolment.student_id = ?
                """,
                (preserved_student["student_id"],),
            ).fetchone()
            self.assertEqual((preserved["status"], preserved["group_code"]), ("active", "Applied-OLD"))
            self.assertTrue(student_import_is_ready(conn, 1))
            self.assertTrue(get_assessment_detail(conn, 1, 1)["summative_upload_ready"])
            batch = create_submission_batch(
                conn,
                1,
                1,
                StoredUpload(
                    original_file_name="submissions.zip",
                    storage_path=Path("submissions.zip"),
                    content_hash=hashlib.sha256(b"submissions").hexdigest(),
                    size_bytes=1024,
                    extension=".zip",
                ),
            )
            self.assertEqual(batch["status"], "uploaded")

    def test_applied_workbook_requires_student_identity_columns(self):
        rows = [
            [],
            ["IT305_CL_S2_ON-CAMPUS"],
            ["Applied_1", "Mon, 10:00, 120 minutes"],
            [None, "Location: Test room"],
            [None, "Staff: -"],
            [],
            ["student_code"],
            ["IT305TEST001"],
        ]
        with _allocation_connection() as conn, patch(
            "feedback_lens.web.tutorial_group_service._read_excel_sheets",
            return_value=[("Applied_1(1)", rows)],
        ), self.assertRaises(ApiError) as raised:
            create_tutorial_group_import(
                conn,
                1,
                1,
                StoredUpload(
                    original_file_name="missing-identity.xlsx",
                    storage_path=Path("missing-identity.xlsx"),
                    content_hash=hashlib.sha256(b"missing-identity").hexdigest(),
                    size_bytes=1024,
                    extension=".xlsx",
                ),
            )
        self.assertEqual(
            raised.exception.code,
            "tutorial_excel_student_identity_columns",
        )
        self.assertIn("last_name", raised.exception.message)

    def test_manual_allocate_workbook_fixture_registers_students_by_itself(self):
        workbook_path = Path(
            "test_data/it305_s2_manual_test_pack/"
            "IT305_AllocatePlus_Applied_test.xlsx"
        )
        workbook_bytes = workbook_path.read_bytes()
        with _allocation_connection() as conn:
            preview = create_tutorial_group_import(
                conn,
                1,
                1,
                StoredUpload(
                    original_file_name=workbook_path.name,
                    storage_path=workbook_path,
                    content_hash=hashlib.sha256(workbook_bytes).hexdigest(),
                    size_bytes=len(workbook_bytes),
                    extension=".xlsx",
                ),
            )
            self.assertEqual(preview["tutorial_group_import"]["total_row_count"], 8)
            self.assertEqual(preview["tutorial_group_import"]["invalid_row_count"], 0)
            self.assertEqual(preview["student_registration"]["created_count"], 8)
            self.assertEqual(preview["student_registration"]["enrolled_count"], 8)

            apply_tutorial_group_import(
                conn,
                1,
                int(preview["tutorial_group_import"]["tutorial_group_import_id"]),
            )
            imported_groups = {
                row["group_code"]: int(row["student_count"])
                for row in conn.execute(
                    """
                    SELECT group_row.group_code, COUNT(membership.student_id) AS student_count
                    FROM tutorial_groups AS group_row
                    LEFT JOIN student_tutorial_memberships AS membership
                      ON membership.tutorial_group_id = group_row.tutorial_group_id
                     AND membership.active = 1
                    WHERE group_row.unit_offering_id = 1
                    GROUP BY group_row.tutorial_group_id
                    """
                )
                if str(row["group_code"]).startswith(("Applied_", "Workshop_", "Lab_"))
            }
            self.assertEqual(imported_groups, {"Applied_1": 4, "Applied_2": 4})

    def test_staff_mapping_csv_and_tutor_centric_group_update(self):
        with _allocation_connection() as conn:
            first_group_id = _create_group(conn, "Applied-01_OnCampus")
            second_group_id = _create_group(conn, "Applied-02_OnCampus")
            conn.execute(
                """
                INSERT INTO users
                    (user_id, email, password_hash, role, display_name,
                     account_status)
                VALUES (4, 'bulk.educator@example.test', 'unused', 'educator',
                        'Bulk Educator', 'active')
                """
            )
            conn.commit()
            content = (
                "tutorial_group,staff_email\n"
                "Applied-01_OnCampus,marker@example.test\n"
                "Applied-02_OnCampus,second@example.test\n"
                "Applied-02_OnCampus,bulk.educator@example.test\n"
            )
            rows = [
                {
                    "tutorial_group": "Applied-01_OnCampus",
                    "staff_email": "marker@example.test",
                },
                {
                    "tutorial_group": "Applied-02_OnCampus",
                    "staff_email": "second@example.test",
                },
                {
                    "tutorial_group": "Applied-02_OnCampus",
                    "staff_email": "bulk.educator@example.test",
                },
            ]
            with patch(
                "feedback_lens.web.tutorial_group_service._read_group_csv",
                return_value=(["tutorial_group", "staff_email"], rows),
            ):
                imported = import_tutorial_group_staff(
                    conn,
                    1,
                    1,
                    StoredUpload(
                        original_file_name="staff-tutorial.csv",
                        storage_path=Path("staff-tutorial.csv"),
                        content_hash=hashlib.sha256(content.encode()).hexdigest(),
                        size_bytes=len(content.encode()),
                        extension=".csv",
                    ),
                )
            self.assertEqual(imported["staff_import"]["added_count"], 3)
            self.assertEqual(imported["staff_import"]["unit_staff_added_count"], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM tutorial_group_staff WHERE active = 1"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT active FROM unit_role_assignments
                    WHERE unit_offering_id = 1 AND user_id = 4
                      AND role = 'staff'
                    """
                ).fetchone()[0],
                1,
            )
            updated = set_tutorial_staff_groups(
                conn,
                1,
                1,
                2,
                [first_group_id, second_group_id],
            )
            staff_group_ids = {
                int(group["tutorial_group_id"])
                for group in updated["groups"]
                if any(int(person["user_id"]) == 2 for person in group["staff"])
            }
            self.assertEqual(
                staff_group_ids,
                {first_group_id, second_group_id},
            )

    def test_unstaffed_group_without_batch_submissions_does_not_block(self):
        with _allocation_connection() as conn:
            staffed_group_id = _create_group(conn, "Applied-01_OnCampus")
            empty_group_id = _create_group(conn, "Applied-02_OnCampus")
            _assign_every_student_to_group(conn, staffed_group_id)
            add_tutorial_group_staff(conn, 1, staffed_group_id, 2)
            student_id = int(
                conn.execute(
                    """
                    INSERT INTO students
                        (institution_student_identifier, full_name,
                         institution_email)
                    VALUES ('55550000', 'No Submission', 'none@example.test')
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO student_enrolments
                    (unit_offering_id, student_id, status, source)
                VALUES (1, ?, 'active', 'api')
                """,
                (student_id,),
            )
            conn.execute(
                """
                INSERT INTO student_tutorial_memberships
                    (unit_offering_id, student_id, tutorial_group_id, source)
                VALUES (1, ?, ?, 'api')
                """,
                (student_id, empty_group_id),
            )
            conn.commit()
            preview = preview_allocation(
                conn,
                1,
                1,
                {"mode": "tutorial_groups"},
            )
            self.assertTrue(preview["can_confirm"])
            self.assertEqual(preview["exceptions"], [])

    def test_group_preview_balances_only_group_staff_and_enables_new_attempts(self):
        with _allocation_connection() as conn:
            group_id = _create_group(conn)
            _assign_every_student_to_group(conn, group_id)
            add_tutorial_group_staff(conn, 1, group_id, 2)
            add_tutorial_group_staff(conn, 1, group_id, 3)

            preview = preview_allocation(
                conn,
                1,
                1,
                {"mode": "tutorial_groups"},
            )
            self.assertTrue(preview["can_confirm"])
            self.assertEqual(preview["exceptions"], [])
            expected_changes = _unassigned_attempt_count(conn)
            self.assertEqual(preview["change_count"], expected_changes)
            assigned_counts = sorted(
                person["assigned_count"] for person in preview["summary"]
            )
            self.assertLessEqual(assigned_counts[-1] - assigned_counts[0], 1)
            self.assertEqual(
                {operation["new_staff_user_id"] for operation in preview["operations"]},
                {2, 3},
            )

            payload = {
                "mode": "tutorial_groups",
                "preview_hash": preview["preview_hash"],
            }
            confirmed = confirm_allocation(conn, 1, 1, payload)
            self.assertEqual(confirmed["change_count"], expected_changes)
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM marker_assignments
                    WHERE active = 1 AND allocation_source = 'tutorial_groups'
                    """
                ).fetchone()[0],
                expected_changes,
            )

            new_student_id = int(
                conn.execute(
                    """
                    INSERT INTO students
                        (institution_student_identifier, full_name,
                         institution_email)
                    VALUES ('55555555', 'Late Student', 'late@example.test')
                    """
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO student_enrolments
                    (unit_offering_id, student_id, status, source)
                VALUES (1, ?, 'active', 'api')
                """,
                (new_student_id,),
            )
            conn.execute(
                """
                INSERT INTO student_tutorial_memberships
                    (unit_offering_id, student_id, tutorial_group_id, source)
                VALUES (1, ?, ?, 'api')
                """,
                (new_student_id, group_id),
            )
            activity_id = int(
                conn.execute(
                    "SELECT assessment_activity_id FROM submission_attempts LIMIT 1"
                ).fetchone()[0]
            )
            new_attempt_id = int(
                conn.execute(
                    """
                    INSERT INTO submission_attempts
                        (assessment_activity_id, purpose, attempt_number,
                         source_system, visibility, status,
                         submitted_by_user_id, submitted_at)
                    VALUES (?, 'summative', 1, 'api', 'assigned_staff',
                            'ready', 1, CURRENT_TIMESTAMP)
                    """,
                    (activity_id,),
                ).lastrowid
            )
            conn.execute(
                """
                INSERT INTO submission_participants
                    (submission_attempt_id, student_id, participant_role)
                VALUES (?, ?, 'primary')
                """,
                (new_attempt_id, new_student_id),
            )
            conn.execute(
                """
                INSERT INTO current_summative_attempts
                    (assessment_activity_id, student_id,
                     submission_attempt_id, set_by_user_id)
                VALUES (?, ?, ?, 1)
                """,
                (activity_id, new_student_id, new_attempt_id),
            )
            conn.execute(
                "INSERT INTO submission_workflow_states(submission_attempt_id) VALUES (?)",
                (new_attempt_id,),
            )
            assigned_user_id = auto_assign_submission_if_enabled(conn, new_attempt_id)
            self.assertIn(assigned_user_id, {2, 3})
            self.assertEqual(
                conn.execute(
                    """
                    SELECT allocation_source FROM marker_assignments
                    WHERE submission_attempt_id = ? AND active = 1
                    """,
                    (new_attempt_id,),
                ).fetchone()[0],
                "tutorial_groups",
            )

    def test_pending_invited_ta_blocks_until_activation(self):
        with _allocation_connection() as conn:
            group_id = _create_group(conn)
            _assign_every_student_to_group(conn, group_id)
            invitation = invite_tutorial_group_staff(
                conn,
                1,
                1,
                {
                    "email": "pending.ta@example.test",
                    "display_name": "Pending TA",
                    "tutorial_group_ids": [group_id],
                },
            )
            self.assertEqual(invitation["status"], "invited")
            self.assertIsNotNone(invitation["activation_url"])
            preview = preview_allocation(
                conn,
                1,
                1,
                {"mode": "tutorial_groups"},
            )
            self.assertFalse(preview["can_confirm"])
            self.assertIn(
                "pending_tutorial_staff",
                {item["reason"] for item in preview["exceptions"]},
            )

            token = invitation["activation_url"].rsplit("/", 1)[-1]
            complete_staff_activation(conn, token, "a-secure-password")
            overview = get_tutorial_group_overview(conn, 1, 1)
            group = next(
                item
                for item in overview["groups"]
                if item["tutorial_group_id"] == group_id
            )
            self.assertTrue(group["ready"])
            refreshed = preview_allocation(
                conn,
                1,
                1,
                {"mode": "tutorial_groups"},
            )
            self.assertTrue(refreshed["can_confirm"])

    def test_csv_import_scales_to_two_thousand_students(self):
        with _allocation_connection() as conn:
            student_rows = [
                (
                    f"9{index:07d}",
                    f"Large Class Student {index}",
                    f"large.student.{index}@example.test",
                )
                for index in range(2000)
            ]
            conn.executemany(
                """
                INSERT INTO students
                    (institution_student_identifier, full_name, institution_email)
                VALUES (?, ?, ?)
                """,
                student_rows,
            )
            conn.execute(
                """
                INSERT INTO student_enrolments
                    (unit_offering_id, student_id, status, source)
                SELECT 1, student_id, 'active', 'api'
                FROM students
                WHERE institution_student_identifier LIKE '9%'
                """
            )
            conn.commit()

            headers = ["student_id", "tutorial_group"]
            imported_rows = [
                {
                    "student_id": identifier,
                    "tutorial_group": f"TUT-{(index % 20) + 1:02d}",
                }
                for index, (identifier, _name, _email) in enumerate(student_rows)
            ]
            digest = hashlib.sha256(b"large-class-tutorial-import").hexdigest()
            with patch(
                "feedback_lens.web.tutorial_group_service._read_group_csv",
                return_value=(headers, imported_rows),
            ):
                preview = create_tutorial_group_import(
                    conn,
                    1,
                    1,
                    StoredUpload(
                        original_file_name="large-class.csv",
                        storage_path=Path("large-class.csv"),
                        content_hash=digest,
                        size_bytes=2000,
                        extension=".csv",
                    ),
                )
            import_record = preview["tutorial_group_import"]
            self.assertEqual(import_record["total_row_count"], 2000)
            self.assertEqual(import_record["assigned_count"], 2000)
            self.assertEqual(import_record["invalid_row_count"], 0)

            applied = apply_tutorial_group_import(
                conn,
                1,
                int(import_record["tutorial_group_import_id"]),
            )
            self.assertEqual(applied["recent_imports"][0]["status"], "applied")
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM student_tutorial_memberships AS membership
                    JOIN students AS student ON student.student_id = membership.student_id
                    WHERE membership.unit_offering_id = 1
                      AND membership.active = 1
                      AND student.institution_student_identifier LIKE '9%'
                    """
                ).fetchone()[0],
                2000,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM tutorial_groups WHERE unit_offering_id = 1"
                ).fetchone()[0],
                20,
            )


class TutorialGroupRouteTests(unittest.TestCase):
    def setUp(self):
        self.previous_testing = app_module.app.config.get("TESTING")
        app_module.app.config["TESTING"] = True

    def tearDown(self):
        app_module.app.config["TESTING"] = self.previous_testing

    @staticmethod
    def _client():
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["email"] = "chief@example.test"
            session["role"] = "admin"
            session["session_version"] = 1
            session["_csrf_token"] = "tutorial-route-csrf"
        return client

    def test_unit_page_and_tutorial_group_routes(self):
        conn = _allocation_connection()
        client = self._client()
        with (
            patch("app.connect_db", return_value=conn),
            patch(
                "feedback_lens.web.routes.connect_db",
                return_value=conn,
            ),
            patch(
                "feedback_lens.web.security.connect_db",
                return_value=conn,
            ),
        ):
            page = client.get("/admin/unit/1")
            empty_overview = client.get(
                "/api/admin/unit-offerings/1/tutorial-groups"
            )
            created = client.post(
                "/api/admin/unit-offerings/1/tutorial-groups",
                json={"group_code": "TUT-ROUTE", "group_name": "Route Tutorial"},
                headers={"X-CSRF-Token": "tutorial-route-csrf"},
            )
            group_id = int(created.get_json()["groups"][0]["tutorial_group_id"])
            linked = client.post(
                f"/api/admin/tutorial-groups/{group_id}/staff",
                json={"staff_user_id": 2},
                headers={"X-CSRF-Token": "tutorial-route-csrf"},
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Tutorial Groups", page.data)
        self.assertIn(b"Upload Allocate+ Excel / existing-student CSV", page.data)
        self.assertIn(b"Bulk-add active Educators", page.data)
        self.assertNotIn(b"Invite new TA", page.data)
        self.assertNotIn(b"Optional roster fallback", page.data)
        self.assertNotIn(b"Map roster columns", page.data)
        self.assertEqual(empty_overview.status_code, 200)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(linked.status_code, 201)
        self.assertEqual(linked.get_json()["groups"][0]["active_staff_count"], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
