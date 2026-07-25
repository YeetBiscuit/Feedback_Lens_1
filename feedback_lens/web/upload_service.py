from __future__ import annotations

import json
import re
import sqlite3
import stat
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from feedback_lens.db.connection import get_next_version
from feedback_lens.file_management.document_io import (
    extract_document,
    hash_file,
)
from feedback_lens.file_management.importers import (
    import_assignment_spec,
    import_rubric,
)
from feedback_lens.file_management.ingestion import ingest_material
from feedback_lens.file_management.indexing.embedding import (
    build_collection_name,
    get_chroma_client,
)
from feedback_lens.web.admin_service import get_assessment_detail
from feedback_lens.web.common import record_audit_event
from feedback_lens.web.config import get_web_settings
from feedback_lens.web.errors import ApiError
from feedback_lens.web.jobs import enqueue_job, update_job_progress
from feedback_lens.web.security import can_administer_unit
from feedback_lens.web.storage import (
    StoredUpload,
    UploadValidationError,
    remove_stored_upload,
    safe_archive_target,
    validate_relative_archive_path,
)


def _job_payload(job: sqlite3.Row) -> dict:
    try:
        return json.loads(job["payload_json"] or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Processing job payload is invalid.") from exc


def _plan_row(
    conn: sqlite3.Connection,
    assessment_plan_id: int,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            plan.*,
            offering.legacy_unit_id
        FROM assessment_plans AS plan
        JOIN unit_offerings AS offering
          ON offering.unit_offering_id = plan.unit_offering_id
        WHERE plan.assessment_plan_id = ?
        """,
        (assessment_plan_id,),
    ).fetchone()
    if row is None:
        raise ApiError("assessment_not_found", "Assessment not found.", 404)
    return row


def enqueue_document_upload(
    conn: sqlite3.Connection,
    actor_user_id: int,
    job_type: str,
    upload: StoredUpload,
    *,
    unit_offering_id: int | None = None,
    assessment_plan_id: int | None = None,
    title: str | None = None,
) -> int:
    if unit_offering_id is not None:
        if not can_administer_unit(conn, actor_user_id, unit_offering_id):
            raise ApiError("unit_forbidden", "Not authorised.", 403)
    elif assessment_plan_id is not None:
        plan = _plan_row(conn, assessment_plan_id)
        if not can_administer_unit(
            conn,
            actor_user_id,
            int(plan["unit_offering_id"]),
        ):
            raise ApiError("assessment_forbidden", "Not authorised.", 403)
    else:
        raise ValueError("A Unit or assessment target is required.")
    job_id = enqueue_job(
        conn,
        job_type,
        unit_offering_id=unit_offering_id,
        assessment_plan_id=assessment_plan_id,
        created_by_user_id=actor_user_id,
        source_file_path=str(upload.storage_path),
        source_content_hash=upload.content_hash,
        payload={
            "original_file_name": upload.original_file_name,
            "title": title or Path(upload.original_file_name).stem,
        },
    )
    record_audit_event(
        conn,
        "upload.queued",
        "processing_job",
        job_id,
        actor_user_id=actor_user_id,
        metadata={"job_type": job_type},
    )
    conn.commit()
    return job_id


def handle_processing_job(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
) -> dict:
    job_type = str(job["job_type"])
    if job_type == "scoping_note_ingest":
        return _handle_scoping_note(conn, job)
    if job_type == "assignment_spec_ingest":
        return _handle_assignment_spec(conn, job)
    if job_type == "rubric_ingest":
        return _handle_rubric(conn, job)
    if job_type == "submission_batch_ingest":
        return _handle_submission_batch(conn, job)
    raise RuntimeError(f"Unsupported processing job type: {job_type}")


def _require_text_document(path: str | Path) -> dict:
    document = extract_document(path)
    if not document["cleaned_text"].strip():
        raise ValueError(
            "No usable text could be extracted from the document."
        )
    return document


def _handle_scoping_note(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
) -> dict:
    path = str(job["source_file_path"])
    _require_text_document(path)
    offering = conn.execute(
        """
        SELECT
            offering.legacy_unit_id,
            course.course_code,
            offering.academic_year,
            offering.teaching_period
        FROM unit_offerings AS offering
        JOIN courses AS course ON course.course_id = offering.course_id
        WHERE offering.unit_offering_id = ?
        """,
        (job["unit_offering_id"],),
    ).fetchone()
    if offering is None or offering["legacy_unit_id"] is None:
        raise ValueError("The Unit no longer exists.")
    payload = _job_payload(job)
    material_id = ingest_material(
        conn,
        path,
        int(offering["legacy_unit_id"]),
        "scoping_note",
        str(payload.get("title") or Path(path).stem),
    )
    record_audit_event(
        conn,
        "scoping_note.ingested",
        "unit_material",
        material_id,
        actor_user_id=job["created_by_user_id"],
    )
    conn.commit()
    return {"material_id": material_id}


def _handle_assignment_spec(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
) -> dict:
    path = str(job["source_file_path"])
    _require_text_document(path)
    plan = _plan_row(conn, int(job["assessment_plan_id"]))
    imported = import_assignment_spec(
        conn,
        int(plan["legacy_assignment_id"]),
        path,
    )
    _ensure_initial_active_version(
        conn,
        int(job["assessment_plan_id"]),
        job["created_by_user_id"],
    )
    record_audit_event(
        conn,
        "assessment.specification_ingested",
        "assignment_spec",
        imported["spec_id"],
        actor_user_id=job["created_by_user_id"],
    )
    conn.commit()
    return imported


def _handle_rubric(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
) -> dict:
    path = str(job["source_file_path"])
    _require_text_document(path)
    plan = _plan_row(conn, int(job["assessment_plan_id"]))
    imported = import_rubric(
        conn,
        int(plan["legacy_assignment_id"]),
        path,
    )
    _ensure_initial_active_version(
        conn,
        int(job["assessment_plan_id"]),
        job["created_by_user_id"],
    )
    record_audit_event(
        conn,
        "assessment.rubric_ingested",
        "rubric",
        imported["rubric_id"],
        actor_user_id=job["created_by_user_id"],
    )
    conn.commit()
    return imported


def _latest_document_ids(
    conn: sqlite3.Connection,
    legacy_assignment_id: int,
) -> tuple[int | None, int | None]:
    spec = conn.execute(
        """
        SELECT spec_id
        FROM assignment_specs
        WHERE assignment_id = ?
        ORDER BY version DESC, spec_id DESC
        LIMIT 1
        """,
        (legacy_assignment_id,),
    ).fetchone()
    rubric = conn.execute(
        """
        SELECT rubric_id
        FROM rubrics
        WHERE assignment_id = ?
        ORDER BY version DESC, rubric_id DESC
        LIMIT 1
        """,
        (legacy_assignment_id,),
    ).fetchone()
    return (
        int(spec["spec_id"]) if spec is not None else None,
        int(rubric["rubric_id"]) if rubric is not None else None,
    )


def _create_version_activities(
    conn: sqlite3.Connection,
    assessment_plan_version_id: int,
) -> None:
    conn.execute(
        """
        INSERT INTO assessment_activities
            (assessment_plan_version_id, purpose, maximum_attempts,
             auto_release_feedback, staff_review_required,
             admin_confirmation_required)
        VALUES (?, 'formative', 3, 1, 0, 0)
        """,
        (assessment_plan_version_id,),
    )
    conn.execute(
        """
        INSERT INTO assessment_activities
            (assessment_plan_version_id, purpose, maximum_attempts,
             auto_release_feedback, staff_review_required,
             admin_confirmation_required)
        VALUES (?, 'summative', NULL, 0, 1, 1)
        """,
        (assessment_plan_version_id,),
    )


def _ensure_initial_active_version(
    conn: sqlite3.Connection,
    assessment_plan_id: int,
    actor_user_id: int | None,
) -> int | None:
    existing = conn.execute(
        """
        SELECT assessment_plan_version_id
        FROM assessment_plan_versions
        WHERE assessment_plan_id = ?
          AND status = 'active'
        """,
        (assessment_plan_id,),
    ).fetchone()
    if existing is not None:
        return int(existing["assessment_plan_version_id"])
    plan = _plan_row(conn, assessment_plan_id)
    spec_id, rubric_id = _latest_document_ids(
        conn,
        int(plan["legacy_assignment_id"]),
    )
    if spec_id is None or rubric_id is None:
        return None
    version_id = int(
        conn.execute(
            """
            INSERT INTO assessment_plan_versions
                (assessment_plan_id, version, spec_id, rubric_id,
                 status, created_by_user_id, activated_at)
            VALUES (?, 1, ?, ?, 'active', ?, CURRENT_TIMESTAMP)
            """,
            (
                assessment_plan_id,
                spec_id,
                rubric_id,
                actor_user_id,
            ),
        ).lastrowid
    )
    _create_version_activities(conn, version_id)
    conn.execute(
        """
        UPDATE assessment_plans
        SET status = 'active'
        WHERE assessment_plan_id = ?
        """,
        (assessment_plan_id,),
    )
    record_audit_event(
        conn,
        "assessment.version_activated",
        "assessment_plan_version",
        version_id,
        actor_user_id=actor_user_id,
        metadata={"initial": True},
    )
    return version_id


def activate_latest_assessment_version(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
) -> dict:
    plan = _plan_row(conn, assessment_plan_id)
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(plan["unit_offering_id"]),
    ):
        raise ApiError("assessment_forbidden", "Not authorised.", 403)
    spec_id, rubric_id = _latest_document_ids(
        conn,
        int(plan["legacy_assignment_id"]),
    )
    if spec_id is None or rubric_id is None:
        raise ApiError(
            "assessment_prerequisites_missing",
            "A valid specification and rubric are required.",
            409,
        )
    active = conn.execute(
        """
        SELECT *
        FROM assessment_plan_versions
        WHERE assessment_plan_id = ?
          AND status = 'active'
        """,
        (assessment_plan_id,),
    ).fetchone()
    if (
        active is not None
        and int(active["spec_id"]) == spec_id
        and int(active["rubric_id"]) == rubric_id
    ):
        return get_assessment_detail(
            conn,
            actor_user_id,
            assessment_plan_id,
        )
    next_version = int(
        conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1
            FROM assessment_plan_versions
            WHERE assessment_plan_id = ?
            """,
            (assessment_plan_id,),
        ).fetchone()[0]
    )
    conn.execute("BEGIN")
    try:
        affected_attempts: list[int] = []
        if active is not None:
            affected_attempts = [
                int(row["submission_attempt_id"])
                for row in conn.execute(
                    """
                    SELECT attempt.submission_attempt_id
                    FROM submission_attempts AS attempt
                    JOIN assessment_activities AS activity
                      ON activity.assessment_activity_id =
                         attempt.assessment_activity_id
                    WHERE activity.assessment_plan_version_id = ?
                      AND attempt.validity_status = 'valid'
                    """,
                    (active["assessment_plan_version_id"],),
                )
            ]
            for attempt_id in affected_attempts:
                workflow = conn.execute(
                    """
                    SELECT *
                    FROM submission_workflow_states
                    WHERE submission_attempt_id = ?
                    """,
                    (attempt_id,),
                ).fetchone()
                if workflow is not None:
                    conn.execute(
                        """
                        INSERT INTO submission_workflow_events
                            (submission_attempt_id, actor_user_id,
                             event_type, from_state_json, comment)
                        VALUES (?, ?, 'assessment_version_invalidated',
                                ?, ?)
                        """,
                        (
                            attempt_id,
                            actor_user_id,
                            json.dumps(dict(workflow), default=str),
                            "Specification or rubric version changed.",
                        ),
                    )
                conn.execute(
                    """
                    UPDATE feedback_revisions
                    SET status = 'superseded'
                    WHERE submission_attempt_id = ?
                      AND status != 'superseded'
                    """,
                    (attempt_id,),
                )
                conn.execute(
                    """
                    UPDATE marker_assignments
                    SET active = 0, ended_at = CURRENT_TIMESTAMP
                    WHERE submission_attempt_id = ? AND active = 1
                    """,
                    (attempt_id,),
                )
                conn.execute(
                    """
                    DELETE FROM current_summative_attempts
                    WHERE submission_attempt_id = ?
                    """,
                    (attempt_id,),
                )
                conn.execute(
                    """
                    UPDATE submission_attempts
                    SET validity_status = 'void',
                        invalidated_by_user_id = ?,
                        invalidated_at = CURRENT_TIMESTAMP,
                        invalidation_reason = ?
                    WHERE submission_attempt_id = ?
                    """,
                    (
                        actor_user_id,
                        "Specification or rubric version changed.",
                        attempt_id,
                    ),
                )
            conn.execute(
                """
                UPDATE assessment_plan_versions
                SET status = 'superseded',
                    superseded_at = CURRENT_TIMESTAMP
                WHERE assessment_plan_version_id = ?
                """,
                (active["assessment_plan_version_id"],),
            )
        version_id = int(
            conn.execute(
                """
                INSERT INTO assessment_plan_versions
                    (assessment_plan_id, version, spec_id, rubric_id,
                     status, created_by_user_id, activated_at)
                VALUES (?, ?, ?, ?, 'active', ?, CURRENT_TIMESTAMP)
                """,
                (
                    assessment_plan_id,
                    next_version,
                    spec_id,
                    rubric_id,
                    actor_user_id,
                ),
            ).lastrowid
        )
        _create_version_activities(conn, version_id)
        conn.execute(
            """
            UPDATE assessment_plans
            SET status = 'active'
            WHERE assessment_plan_id = ?
            """,
            (assessment_plan_id,),
        )
        record_audit_event(
            conn,
            "assessment.version_activated",
            "assessment_plan_version",
            version_id,
            actor_user_id=actor_user_id,
            metadata={
                "affected_attempt_count": len(affected_attempts),
                "spec_id": spec_id,
                "rubric_id": rubric_id,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_assessment_detail(conn, actor_user_id, assessment_plan_id)


def deactivate_scoping_note(
    conn: sqlite3.Connection,
    actor_user_id: int,
    material_id: int,
    reason: str,
) -> None:
    row = conn.execute(
        """
        SELECT
            material.*,
            offering.unit_offering_id,
            unit.unit_code,
            unit.year,
            unit.semester
        FROM unit_materials AS material
        JOIN units AS unit ON unit.unit_id = material.unit_id
        JOIN unit_offerings AS offering
          ON offering.legacy_unit_id = unit.unit_id
        WHERE material.material_id = ?
          AND material.assignment_id IS NULL
          AND material.material_type = 'scoping_note'
        """,
        (material_id,),
    ).fetchone()
    if row is None:
        raise ApiError(
            "scoping_note_not_found",
            "Scoping note not found.",
            404,
        )
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(row["unit_offering_id"]),
    ):
        raise ApiError("unit_forbidden", "Not authorised.", 403)
    reason = reason.strip()
    if not reason:
        raise ApiError(
            "deactivation_reason_required",
            "Provide a reason for deactivation.",
            422,
        )
    vector_ids = [
        str(vector["vector_id"])
        for vector in conn.execute(
            """
            SELECT map.vector_id
            FROM chunk_embedding_map AS map
            JOIN material_chunks AS chunk
              ON chunk.chunk_id = map.chunk_id
            WHERE chunk.material_id = ?
            """,
            (material_id,),
        )
    ]
    conn.execute(
        """
        UPDATE unit_materials
        SET is_active = 0,
            deactivated_by_user_id = ?,
            deactivated_at = CURRENT_TIMESTAMP,
            deactivation_reason = ?
        WHERE material_id = ?
        """,
        (actor_user_id, reason, material_id),
    )
    record_audit_event(
        conn,
        "scoping_note.deactivated",
        "unit_material",
        material_id,
        actor_user_id=actor_user_id,
        metadata={"reason": reason},
    )
    conn.commit()
    if vector_ids:
        collection_name = build_collection_name(
            row["unit_code"],
            row["year"],
            row["semester"],
        )
        client = get_chroma_client()
        existing = [collection.name for collection in client.list_collections()]
        if collection_name in existing:
            client.get_collection(collection_name).delete(ids=vector_ids)


def create_submission_batch(
    conn: sqlite3.Connection,
    actor_user_id: int,
    assessment_plan_id: int,
    upload: StoredUpload,
) -> dict:
    plan = _plan_row(conn, assessment_plan_id)
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(plan["unit_offering_id"]),
    ):
        raise ApiError("assessment_forbidden", "Not authorised.", 403)
    roster_ready = conn.execute(
        """
        SELECT 1
        FROM roster_imports
        WHERE unit_offering_id = ?
          AND status IN ('imported', 'partially_imported')
        LIMIT 1
        """,
        (plan["unit_offering_id"],),
    ).fetchone()
    active = conn.execute(
        """
        SELECT
            version.assessment_plan_version_id,
            version.spec_id,
            version.rubric_id,
            activity.assessment_activity_id
        FROM assessment_plan_versions AS version
        JOIN assessment_activities AS activity
          ON activity.assessment_plan_version_id =
             version.assessment_plan_version_id
         AND activity.purpose = 'summative'
         AND activity.enabled = 1
        WHERE version.assessment_plan_id = ?
          AND version.status = 'active'
        """,
        (assessment_plan_id,),
    ).fetchone()
    if roster_ready is None or active is None:
        raise ApiError(
            "summative_prerequisites_missing",
            "Import a roster and upload a valid specification and rubric first.",
            409,
        )
    existing = conn.execute(
        """
        SELECT *
        FROM submission_batches
        WHERE assessment_activity_id = ?
          AND source_content_hash = ?
        """,
        (active["assessment_activity_id"], upload.content_hash),
    ).fetchone()
    if existing is not None:
        remove_stored_upload(upload.storage_path)
        return {
            "submission_batch_id": int(existing["submission_batch_id"]),
            "status": existing["status"],
            "duplicate": True,
        }
    batch_id = int(
        conn.execute(
            """
            INSERT INTO submission_batches
                (assessment_activity_id, uploaded_by_user_id,
                 source_system, source_file_name, source_file_path,
                 source_content_hash, status)
            VALUES (?, ?, 'moodle', ?, ?, ?, 'uploaded')
            """,
            (
                active["assessment_activity_id"],
                actor_user_id,
                upload.original_file_name,
                str(upload.storage_path),
                upload.content_hash,
            ),
        ).lastrowid
    )
    job_id = enqueue_job(
        conn,
        "submission_batch_ingest",
        assessment_plan_id=assessment_plan_id,
        submission_batch_id=batch_id,
        created_by_user_id=actor_user_id,
        source_file_path=str(upload.storage_path),
        source_content_hash=upload.content_hash,
    )
    record_audit_event(
        conn,
        "submission_batch.uploaded",
        "submission_batch",
        batch_id,
        actor_user_id=actor_user_id,
        metadata={"processing_job_id": job_id},
    )
    conn.commit()
    return {
        "submission_batch_id": batch_id,
        "processing_job_id": job_id,
        "status": "uploaded",
        "duplicate": False,
    }


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _safe_zip_groups(
    archive: zipfile.ZipFile,
) -> dict[str, list[zipfile.ZipInfo]]:
    settings = get_web_settings()
    infos = archive.infolist()
    if len(infos) > settings.zip_entry_limit:
        raise ValueError("The ZIP contains too many entries.")
    total_uncompressed = 0
    groups: dict[str, list[zipfile.ZipInfo]] = {}
    for info in infos:
        path = validate_relative_archive_path(info.filename)
        if _is_symlink(info):
            raise ValueError("The ZIP contains a symbolic link.")
        total_uncompressed += int(info.file_size)
        if total_uncompressed > settings.zip_uncompressed_limit_bytes:
            raise ValueError("The ZIP expands beyond the configured limit.")
        compressed = max(int(info.compress_size), 1)
        if (
            info.file_size / compressed
            > settings.zip_compression_ratio_limit
        ):
            raise ValueError("The ZIP has an unsafe compression ratio.")
        if info.is_dir():
            continue
        if "__MACOSX" in path.parts or path.name in {".DS_Store", "Thumbs.db"}:
            continue
        if len(path.parts) < 2:
            raise ValueError(
                "Use Moodle 'Download submissions in folders'; flat ZIPs "
                "are not supported."
            )
        groups.setdefault(path.parts[0], []).append(info)
    if not groups:
        raise ValueError("The ZIP contains no student submission folders.")
    return groups


def _roster_candidates(
    folder_name: str,
    roster: list[sqlite3.Row],
) -> list[sqlite3.Row]:
    candidates = []
    for student in roster:
        identifier = str(student["institution_student_identifier"]).strip()
        if not identifier:
            continue
        pattern = (
            r"(?<![A-Za-z0-9])"
            + re.escape(identifier)
            + r"(?![A-Za-z0-9])"
        )
        if re.search(pattern, folder_name, flags=re.IGNORECASE):
            candidates.append(student)
    return candidates


def _extract_zip_pdf(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    batch_id: int,
    item_number: int,
) -> Path:
    target_root = (
        get_web_settings().upload_root
        / "submissions"
        / str(batch_id)
        / str(item_number)
    )
    target_root.mkdir(parents=True, exist_ok=True)
    target = safe_archive_target(
        target_root,
        validate_relative_archive_path(f"accepted-{uuid.uuid4().hex}.pdf"),
    )
    prefix = b""
    with archive.open(info, "r") as source, target.open("xb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            if not prefix:
                prefix = chunk[:8]
            output.write(chunk)
    if not prefix.startswith(b"%PDF-"):
        target.unlink(missing_ok=True)
        raise ValueError("The file extension is PDF but its content is not.")
    return target


def _insert_batch_item(
    conn: sqlite3.Connection,
    batch_id: int,
    item_number: int,
    folder_name: str,
    infos: list[zipfile.ZipInfo],
    candidates: list[sqlite3.Row],
) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO submission_batch_items
                (submission_batch_id, item_number,
                 source_folder_name, source_relative_path,
                 detected_student_identifier, student_id,
                 candidate_student_ids_json, detected_files_json,
                 item_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                batch_id,
                item_number,
                folder_name,
                folder_name,
                (
                    candidates[0]["institution_student_identifier"]
                    if len(candidates) == 1
                    else None
                ),
                (
                    int(candidates[0]["student_id"])
                    if len(candidates) == 1
                    else None
                ),
                json.dumps(
                    [int(row["student_id"]) for row in candidates]
                ),
                json.dumps(
                    [
                        {
                            "name": info.filename,
                            "size": info.file_size,
                        }
                        for info in infos
                    ]
                ),
            ),
        ).lastrowid
    )


def _handle_submission_batch(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
) -> dict:
    batch_id = int(job["submission_batch_id"])
    batch = conn.execute(
        """
        SELECT batch.*, version.assessment_plan_id
        FROM submission_batches AS batch
        JOIN assessment_activities AS activity
          ON activity.assessment_activity_id =
             batch.assessment_activity_id
        JOIN assessment_plan_versions AS version
          ON version.assessment_plan_version_id =
             activity.assessment_plan_version_id
        WHERE batch.submission_batch_id = ?
        """,
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise ValueError("Submission batch no longer exists.")
    plan = _plan_row(conn, int(batch["assessment_plan_id"]))
    roster = conn.execute(
        """
        SELECT student.*
        FROM student_enrolments AS enrolment
        JOIN students AS student
          ON student.student_id = enrolment.student_id
        WHERE enrolment.unit_offering_id = ?
          AND enrolment.status = 'active'
        """,
        (plan["unit_offering_id"],),
    ).fetchall()
    conn.execute(
        """
        UPDATE submission_batches
        SET status = 'validating', error_message = NULL
        WHERE submission_batch_id = ?
        """,
        (batch_id,),
    )
    conn.execute(
        "DELETE FROM submission_batch_items WHERE submission_batch_id = ?",
        (batch_id,),
    )
    conn.commit()

    imported = 0
    duplicates = 0
    unresolved = 0
    total = 0
    try:
        with zipfile.ZipFile(str(batch["source_file_path"])) as archive:
            if archive.testzip() is not None:
                raise ValueError("The ZIP is damaged.")
            groups = _safe_zip_groups(archive)
            total = len(groups)
            for index, (folder, infos) in enumerate(
                sorted(groups.items()),
                start=1,
            ):
                candidates = _roster_candidates(folder, roster)
                item_id = _insert_batch_item(
                    conn,
                    batch_id,
                    index,
                    folder,
                    infos,
                    candidates,
                )
                if (
                    len(infos) != 1
                    or Path(infos[0].filename).suffix.lower() != ".pdf"
                ):
                    _set_item_error(
                        conn,
                        item_id,
                        "format_error",
                        "The folder must contain exactly one PDF.",
                    )
                    unresolved += 1
                else:
                    try:
                        extracted_path = _extract_zip_pdf(
                            archive,
                            infos[0],
                            batch_id,
                            index,
                        )
                        document = _require_text_document(extracted_path)
                        content_hash = hash_file(extracted_path)
                        conn.execute(
                            """
                            UPDATE submission_batch_items
                            SET accepted_file_name = ?,
                                accepted_file_path = ?,
                                accepted_content_hash = ?,
                                accepted_mime_type = 'application/pdf',
                                accepted_size_bytes = ?,
                                item_status = 'ready'
                            WHERE submission_batch_item_id = ?
                            """,
                            (
                                Path(infos[0].filename).name,
                                str(extracted_path),
                                content_hash,
                                extracted_path.stat().st_size,
                                item_id,
                            ),
                        )
                        if len(candidates) == 0:
                            _set_item_error(
                                conn,
                                item_id,
                                "unmatched",
                                (
                                    "No exact roster student ID was found "
                                    "in the folder name."
                                ),
                            )
                            unresolved += 1
                        elif len(candidates) > 1:
                            _set_item_error(
                                conn,
                                item_id,
                                "ambiguous",
                                (
                                    "More than one roster student ID "
                                    "matched the folder name."
                                ),
                            )
                            unresolved += 1
                        else:
                            result = _import_ready_batch_item(
                                conn,
                                item_id,
                                int(candidates[0]["student_id"]),
                                int(batch["uploaded_by_user_id"]),
                                document,
                            )
                            if result["status"] == "imported":
                                imported += 1
                            elif result["status"] == "duplicate":
                                duplicates += 1
                    except Exception as exc:
                        _set_item_error(
                            conn,
                            item_id,
                            "format_error",
                            str(exc),
                        )
                        unresolved += 1
                conn.commit()
                update_job_progress(
                    conn,
                    int(job["processing_job_id"]),
                    index,
                    total,
                )
    except Exception as exc:
        conn.execute(
            """
            UPDATE submission_batches
            SET status = 'failed',
                error_message = ?,
                completed_at = CURRENT_TIMESTAMP
            WHERE submission_batch_id = ?
            """,
            (str(exc)[:2000], batch_id),
        )
        conn.commit()
        raise

    status = "imported" if unresolved == 0 else "partially_imported"
    conn.execute(
        """
        UPDATE submission_batches
        SET status = ?,
            detected_submission_count = ?,
            imported_submission_count = ?,
            completed_at = CURRENT_TIMESTAMP,
            error_message = NULL
        WHERE submission_batch_id = ?
        """,
        (status, total, imported, batch_id),
    )
    record_audit_event(
        conn,
        "submission_batch.processed",
        "submission_batch",
        batch_id,
        actor_user_id=batch["uploaded_by_user_id"],
        metadata={
            "imported": imported,
            "duplicates": duplicates,
            "unresolved": unresolved,
        },
    )
    conn.commit()
    return {
        "submission_batch_id": batch_id,
        "status": status,
        "imported": imported,
        "duplicates": duplicates,
        "unresolved": unresolved,
    }


def _set_item_error(
    conn: sqlite3.Connection,
    item_id: int,
    status: str,
    message: str,
) -> None:
    conn.execute(
        """
        UPDATE submission_batch_items
        SET item_status = ?, error_message = ?
        WHERE submission_batch_item_id = ?
        """,
        (status, message[:2000], item_id),
    )


def _import_ready_batch_item(
    conn: sqlite3.Connection,
    item_id: int,
    student_id: int,
    actor_user_id: int,
    document: dict | None = None,
) -> dict:
    item = conn.execute(
        """
        SELECT
            item.*,
            batch.assessment_activity_id,
            batch.submission_batch_id,
            version.assessment_plan_id,
            plan.legacy_assignment_id
        FROM submission_batch_items AS item
        JOIN submission_batches AS batch
          ON batch.submission_batch_id = item.submission_batch_id
        JOIN assessment_activities AS activity
          ON activity.assessment_activity_id =
             batch.assessment_activity_id
        JOIN assessment_plan_versions AS version
          ON version.assessment_plan_version_id =
             activity.assessment_plan_version_id
        JOIN assessment_plans AS plan
          ON plan.assessment_plan_id = version.assessment_plan_id
        WHERE item.submission_batch_item_id = ?
        """,
        (item_id,),
    ).fetchone()
    if item is None or not item["accepted_file_path"]:
        raise ApiError(
            "batch_item_not_ready",
            "Upload one valid text PDF before importing this item.",
            409,
        )
    student = conn.execute(
        "SELECT * FROM students WHERE student_id = ?",
        (student_id,),
    ).fetchone()
    if student is None:
        raise ApiError("student_not_found", "Student not found.", 404)
    current = conn.execute(
        """
        SELECT
            current.submission_attempt_id,
            file.content_hash
        FROM current_summative_attempts AS current
        LEFT JOIN submission_files AS file
          ON file.submission_attempt_id =
             current.submission_attempt_id
        WHERE current.assessment_activity_id = ?
          AND current.student_id = ?
        LIMIT 1
        """,
        (item["assessment_activity_id"], student_id),
    ).fetchone()
    if (
        current is not None
        and current["content_hash"] == item["accepted_content_hash"]
    ):
        conn.execute(
            """
            UPDATE submission_batch_items
            SET student_id = ?,
                item_status = 'duplicate',
                resolved_by_user_id = ?,
                resolved_at = CURRENT_TIMESTAMP,
                error_message = NULL
            WHERE submission_batch_item_id = ?
            """,
            (student_id, actor_user_id, item_id),
        )
        return {"status": "duplicate"}

    if document is None:
        document = _require_text_document(item["accepted_file_path"])
    legacy_version = get_next_version(
        conn,
        "student_submissions",
        "assignment_id",
        item["legacy_assignment_id"],
        partition_column="student_identifier",
        partition_value=student["institution_student_identifier"],
    )
    legacy_submission_id = int(
        conn.execute(
            """
            INSERT INTO student_submissions
                (assignment_id, student_identifier, original_file_path,
                 source_content_hash, raw_text, cleaned_text,
                 submitted_at, version)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            """,
            (
                item["legacy_assignment_id"],
                student["institution_student_identifier"],
                item["accepted_file_path"],
                item["accepted_content_hash"],
                document["raw_text"],
                document["cleaned_text"],
                legacy_version,
            ),
        ).lastrowid
    )
    attempt_number = int(
        conn.execute(
            """
            SELECT COALESCE(MAX(attempt.attempt_number), 0) + 1
            FROM submission_attempts AS attempt
            JOIN submission_participants AS participant
              ON participant.submission_attempt_id =
                 attempt.submission_attempt_id
            WHERE attempt.assessment_activity_id = ?
              AND participant.student_id = ?
              AND participant.participant_role = 'primary'
            """,
            (item["assessment_activity_id"], student_id),
        ).fetchone()[0]
    )
    attempt_id = int(
        conn.execute(
            """
            INSERT INTO submission_attempts
                (assessment_activity_id, submission_batch_id,
                 legacy_submission_id, purpose, attempt_number,
                 source_version, source_system, source_reference,
                 visibility, status, submitted_by_user_id,
                 submitted_at)
            VALUES (?, ?, ?, 'summative', ?, ?, 'moodle', ?,
                    'assigned_staff', 'ready', ?, CURRENT_TIMESTAMP)
            """,
            (
                item["assessment_activity_id"],
                item["submission_batch_id"],
                legacy_submission_id,
                attempt_number,
                legacy_version,
                item["source_relative_path"],
                actor_user_id,
            ),
        ).lastrowid
    )
    conn.execute(
        """
        INSERT INTO submission_participants
            (submission_attempt_id, student_id, participant_role)
        VALUES (?, ?, 'primary')
        """,
        (attempt_id, student_id),
    )
    conn.execute(
        """
        INSERT INTO submission_files
            (submission_attempt_id, original_file_name,
             relative_path, storage_path, content_hash,
             mime_type, size_bytes)
        VALUES (?, ?, ?, ?, ?, 'application/pdf', ?)
        """,
        (
            attempt_id,
            item["accepted_file_name"],
            item["source_relative_path"],
            item["accepted_file_path"],
            item["accepted_content_hash"],
            item["accepted_size_bytes"],
        ),
    )
    conn.execute(
        """
        INSERT INTO submission_workflow_states(submission_attempt_id)
        VALUES (?)
        """,
        (attempt_id,),
    )
    old_attempt_id = (
        int(current["submission_attempt_id"]) if current is not None else None
    )
    if old_attempt_id is None:
        conn.execute(
            """
            INSERT INTO current_summative_attempts
                (assessment_activity_id, student_id,
                 submission_attempt_id, set_by_user_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                item["assessment_activity_id"],
                student_id,
                attempt_id,
                actor_user_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE marker_assignments
            SET active = 0, ended_at = CURRENT_TIMESTAMP
            WHERE submission_attempt_id = ? AND active = 1
            """,
            (old_attempt_id,),
        )
        conn.execute(
            """
            UPDATE current_summative_attempts
            SET submission_attempt_id = ?,
                set_by_user_id = ?,
                set_at = CURRENT_TIMESTAMP
            WHERE assessment_activity_id = ?
              AND student_id = ?
            """,
            (
                attempt_id,
                actor_user_id,
                item["assessment_activity_id"],
                student_id,
            ),
        )
        conn.execute(
            """
            UPDATE submission_attempts
            SET validity_status = 'superseded',
                superseded_by_attempt_id = ?,
                invalidated_by_user_id = ?,
                invalidated_at = CURRENT_TIMESTAMP,
                invalidation_reason = ?
            WHERE submission_attempt_id = ?
            """,
            (
                attempt_id,
                actor_user_id,
                "A newer valid summative submission was uploaded.",
                old_attempt_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO submission_workflow_events
                (submission_attempt_id, actor_user_id,
                 event_type, comment)
            VALUES (?, ?, 'attempt_superseded', ?)
            """,
            (
                old_attempt_id,
                actor_user_id,
                f"Superseded by attempt {attempt_id}.",
            ),
        )
    conn.execute(
        """
        UPDATE submission_batch_items
        SET student_id = ?,
            submission_attempt_id = ?,
            item_status = 'imported',
            resolved_by_user_id = ?,
            resolved_at = CURRENT_TIMESTAMP,
            error_message = NULL
        WHERE submission_batch_item_id = ?
        """,
        (student_id, attempt_id, actor_user_id, item_id),
    )
    record_audit_event(
        conn,
        "submission_attempt.imported",
        "submission_attempt",
        attempt_id,
        actor_user_id=actor_user_id,
        metadata={
            "student_id": student_id,
            "superseded_attempt_id": old_attempt_id,
        },
    )
    return {"status": "imported", "submission_attempt_id": attempt_id}


def get_submission_batch(
    conn: sqlite3.Connection,
    actor_user_id: int,
    batch_id: int,
) -> dict:
    batch = conn.execute(
        """
        SELECT
            batch.*,
            version.assessment_plan_id,
            plan.unit_offering_id
        FROM submission_batches AS batch
        JOIN assessment_activities AS activity
          ON activity.assessment_activity_id =
             batch.assessment_activity_id
        JOIN assessment_plan_versions AS version
          ON version.assessment_plan_version_id =
             activity.assessment_plan_version_id
        JOIN assessment_plans AS plan
          ON plan.assessment_plan_id = version.assessment_plan_id
        WHERE batch.submission_batch_id = ?
        """,
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise ApiError("batch_not_found", "Submission batch not found.", 404)
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(batch["unit_offering_id"]),
    ):
        raise ApiError("assessment_forbidden", "Not authorised.", 403)
    items = conn.execute(
        """
        SELECT
            item.*,
            student.institution_student_identifier,
            student.full_name,
            student.institution_email
        FROM submission_batch_items AS item
        LEFT JOIN students AS student
          ON student.student_id = item.student_id
        WHERE item.submission_batch_id = ?
        ORDER BY item.item_number
        """,
        (batch_id,),
    ).fetchall()
    roster_students = conn.execute(
        """
        SELECT
            student.student_id,
            student.institution_student_identifier,
            student.full_name,
            student.institution_email
        FROM student_enrolments AS enrolment
        JOIN students AS student
          ON student.student_id = enrolment.student_id
        WHERE enrolment.unit_offering_id = ?
          AND enrolment.status = 'active'
        ORDER BY student.institution_student_identifier
        """,
        (batch["unit_offering_id"],),
    ).fetchall()
    return {
        "batch": dict(batch),
        "items": [dict(item) for item in items],
        "roster_students": [dict(student) for student in roster_students],
    }


def resolve_submission_batch_item(
    conn: sqlite3.Connection,
    actor_user_id: int,
    item_id: int,
    *,
    student_id: int | None = None,
    corrected_upload: StoredUpload | None = None,
    ignore: bool = False,
    note: str | None = None,
) -> dict:
    item = conn.execute(
        """
        SELECT
            item.*,
            batch.uploaded_by_user_id,
            plan.unit_offering_id
        FROM submission_batch_items AS item
        JOIN submission_batches AS batch
          ON batch.submission_batch_id = item.submission_batch_id
        JOIN assessment_activities AS activity
          ON activity.assessment_activity_id =
             batch.assessment_activity_id
        JOIN assessment_plan_versions AS version
          ON version.assessment_plan_version_id =
             activity.assessment_plan_version_id
        JOIN assessment_plans AS plan
          ON plan.assessment_plan_id = version.assessment_plan_id
        WHERE item.submission_batch_item_id = ?
        """,
        (item_id,),
    ).fetchone()
    if item is None:
        raise ApiError("batch_item_not_found", "Batch item not found.", 404)
    if not can_administer_unit(
        conn,
        actor_user_id,
        int(item["unit_offering_id"]),
    ):
        raise ApiError("assessment_forbidden", "Not authorised.", 403)
    if item["item_status"] in {"imported", "duplicate", "ignored"}:
        raise ApiError(
            "batch_item_resolved",
            "This batch item has already been resolved.",
            409,
        )
    if ignore:
        conn.execute(
            """
            UPDATE submission_batch_items
            SET item_status = 'ignored',
                resolved_by_user_id = ?,
                resolved_at = CURRENT_TIMESTAMP,
                resolution_note = ?,
                error_message = NULL
            WHERE submission_batch_item_id = ?
            """,
            (actor_user_id, note, item_id),
        )
        conn.commit()
        _refresh_batch_status(conn, int(item["submission_batch_id"]))
        return {"status": "ignored"}
    if student_id is None:
        student_id = (
            int(item["student_id"]) if item["student_id"] is not None else None
        )
    if student_id is None:
        raise ApiError(
            "student_required",
            "Choose a roster student.",
            422,
        )
    enrolled = conn.execute(
        """
        SELECT 1
        FROM student_enrolments
        WHERE unit_offering_id = ?
          AND student_id = ?
          AND status = 'active'
        """,
        (item["unit_offering_id"], student_id),
    ).fetchone()
    if enrolled is None:
        raise ApiError(
            "student_not_enrolled",
            "Choose an actively enrolled student.",
            422,
        )
    if corrected_upload is not None:
        document = _require_text_document(corrected_upload.storage_path)
        conn.execute(
            """
            UPDATE submission_batch_items
            SET accepted_file_name = ?,
                accepted_file_path = ?,
                accepted_content_hash = ?,
                accepted_mime_type = 'application/pdf',
                accepted_size_bytes = ?,
                item_status = 'ready'
            WHERE submission_batch_item_id = ?
            """,
            (
                corrected_upload.original_file_name,
                str(corrected_upload.storage_path),
                corrected_upload.content_hash,
                corrected_upload.size_bytes,
                item_id,
            ),
        )
    else:
        document = None
    result = _import_ready_batch_item(
        conn,
        item_id,
        student_id,
        actor_user_id,
        document,
    )
    conn.execute(
        """
        UPDATE submission_batch_items
        SET resolution_note = ?
        WHERE submission_batch_item_id = ?
        """,
        (note, item_id),
    )
    conn.commit()
    _refresh_batch_status(conn, int(item["submission_batch_id"]))
    return result


def _refresh_batch_status(
    conn: sqlite3.Connection,
    batch_id: int,
) -> None:
    counts = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN item_status = 'imported' THEN 1 ELSE 0 END)
                AS imported,
            SUM(
                CASE
                    WHEN item_status IN (
                        'unmatched', 'ambiguous', 'format_error',
                        'failed', 'pending', 'ready'
                    )
                    THEN 1 ELSE 0
                END
            ) AS unresolved
        FROM submission_batch_items
        WHERE submission_batch_id = ?
        """,
        (batch_id,),
    ).fetchone()
    status = "imported" if int(counts["unresolved"] or 0) == 0 else "partially_imported"
    conn.execute(
        """
        UPDATE submission_batches
        SET status = ?,
            detected_submission_count = ?,
            imported_submission_count = ?,
            completed_at = CURRENT_TIMESTAMP
        WHERE submission_batch_id = ?
        """,
        (
            status,
            int(counts["total"] or 0),
            int(counts["imported"] or 0),
            batch_id,
        ),
    )
    conn.commit()
