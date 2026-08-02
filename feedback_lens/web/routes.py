from __future__ import annotations

import json
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
)
from werkzeug.exceptions import RequestEntityTooLarge

from feedback_lens.db.connection import connect_db
from feedback_lens.web.account_service import (
    GENERIC_ACCOUNT_MESSAGE,
    complete_activation,
    complete_password_reset,
    create_or_rotate_unit_entry,
    current_unit_entry,
    request_activation,
    request_password_reset,
    verify_token,
)
from feedback_lens.web.admin_service import (
    assign_unit_admin,
    commit_roster_import,
    create_assessment,
    create_roster_import,
    create_unit,
    get_assessment_document_download,
    get_assessment_detail,
    get_roster_import,
    get_scoping_note_download,
    get_unit_detail,
    list_admin_units,
    list_unit_admin_candidates,
    preview_roster_import,
    update_assessment,
    update_unit,
)
from feedback_lens.web.errors import ApiError
from feedback_lens.web.config import get_web_settings
from feedback_lens.web.mail import mail_is_configured
from feedback_lens.web.security import (
    can_administer_unit,
    csrf_token,
    fetch_authenticated_user,
    is_chief_admin,
    require_csrf,
    require_json_user,
)
from feedback_lens.web.storage import (
    UploadValidationError,
    remove_stored_upload,
    store_upload,
)
from feedback_lens.web.upload_service import (
    activate_latest_assessment_version,
    create_submission_batch,
    deactivate_scoping_note,
    delete_scoping_note,
    enqueue_document_upload,
    enqueue_scoping_note_restore,
    get_submission_batch,
    resolve_submission_batch_item,
)


feature_blueprint = Blueprint("features", __name__)


@feature_blueprint.errorhandler(ApiError)
def _api_error(error: ApiError):
    if request.path.startswith("/api/"):
        return jsonify(error.to_dict()), error.status
    return (
        render_template(
            "account_message.html",
            title="Unable to continue",
            message=error.message,
            success=False,
        ),
        error.status,
    )


@feature_blueprint.errorhandler(UploadValidationError)
def _upload_error(error: UploadValidationError):
    return (
        jsonify(
            {
                "error": {
                    "code": "upload_invalid",
                    "message": str(error),
                }
            }
        ),
        422,
    )


@feature_blueprint.app_errorhandler(RequestEntityTooLarge)
def _request_too_large(error: RequestEntityTooLarge):
    if request.path.startswith("/api/admin/"):
        return (
            jsonify(
                {
                    "error": {
                        "code": "upload_too_large",
                        "message": "The uploaded file exceeds the size limit.",
                    }
                }
            ),
            422,
        )
    return error.get_response()


def _page_admin_user():
    with connect_db() as conn:
        user = fetch_authenticated_user(conn)
        if user is None:
            return None
        chief = is_chief_admin(conn, int(user["user_id"]))
        unit_admin = (
            conn.execute(
                """
                SELECT 1
                FROM unit_role_assignments
                WHERE user_id = ?
                  AND role = 'unit_admin'
                  AND active = 1
                LIMIT 1
                """,
                (user["user_id"],),
            ).fetchone()
            is not None
        )
        if not chief and not unit_admin:
            return None
        return dict(user)


def _json_user(conn):
    user = fetch_authenticated_user(conn)
    if user is None:
        raise ApiError(
            "authentication_required",
            "Please log in.",
            401,
        )
    return user


@feature_blueprint.get("/admin/units")
def admin_units_page():
    user = _page_admin_user()
    if user is None:
        return redirect("/login")
    return render_template(
        "admin_units.html",
        csrf_token=csrf_token(),
        user=user,
    )


@feature_blueprint.get("/admin/unit/<int:unit_offering_id>")
def admin_unit_page(unit_offering_id: int):
    user = _page_admin_user()
    if user is None:
        return redirect("/login")
    with connect_db() as conn:
        if not can_administer_unit(
            conn,
            int(user["user_id"]),
            unit_offering_id,
        ):
            return redirect("/admin/units")
    return render_template(
        "admin_unit_detail.html",
        csrf_token=csrf_token(),
        user=user,
        unit_offering_id=unit_offering_id,
    )


@feature_blueprint.get("/admin/assessment/<int:assessment_plan_id>")
def admin_assessment_page(assessment_plan_id: int):
    user = _page_admin_user()
    if user is None:
        return redirect("/login")
    with connect_db() as conn:
        get_assessment_detail(
            conn,
            int(user["user_id"]),
            assessment_plan_id,
        )
    return render_template(
        "admin_assessment_detail.html",
        csrf_token=csrf_token(),
        user=user,
        assessment_plan_id=assessment_plan_id,
    )


@feature_blueprint.get("/api/admin/units")
@require_json_user
def api_admin_units():
    with connect_db() as conn:
        user = _json_user(conn)
        units = list_admin_units(conn, int(user["user_id"]))
        return jsonify(
            {
                "units": units,
                "is_chief_admin": is_chief_admin(
                    conn,
                    int(user["user_id"]),
                ),
            }
        )


@feature_blueprint.patch(
    "/api/admin/unit-offerings/<int:unit_offering_id>"
)
@require_json_user
@require_csrf
def api_update_unit(unit_offering_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        result = update_unit(
            conn,
            int(user["user_id"]),
            unit_offering_id,
            request.get_json(silent=True) or {},
        )
    return jsonify(result)


@feature_blueprint.post("/api/admin/units")
@require_json_user
@require_csrf
def api_create_unit():
    with connect_db() as conn:
        user = _json_user(conn)
        result = create_unit(
            conn,
            int(user["user_id"]),
            request.get_json(silent=True) or {},
        )
    return jsonify(result), 201


@feature_blueprint.get("/api/admin/unit-admin-candidates")
@require_json_user
def api_unit_admin_candidates():
    with connect_db() as conn:
        user = _json_user(conn)
        candidates = list_unit_admin_candidates(
            conn,
            int(user["user_id"]),
        )
    return jsonify({"users": candidates})


@feature_blueprint.get(
    "/api/admin/unit-offerings/<int:unit_offering_id>"
)
@require_json_user
def api_unit_detail(unit_offering_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        result = get_unit_detail(
            conn,
            int(user["user_id"]),
            unit_offering_id,
        )
        result["activation"] = current_unit_entry(
            conn,
            int(user["user_id"]),
            unit_offering_id,
        )
    return jsonify(result)


@feature_blueprint.post(
    "/api/admin/unit-offerings/<int:unit_offering_id>/admins"
)
@require_json_user
@require_csrf
def api_assign_unit_admin(unit_offering_id: int):
    data = request.get_json(silent=True) or {}
    try:
        target_user_id = int(data.get("user_id"))
    except (TypeError, ValueError) as exc:
        raise ApiError(
            "unit_admin_required",
            "Choose a Unit Admin.",
            422,
        ) from exc
    with connect_db() as conn:
        user = _json_user(conn)
        assign_unit_admin(
            conn,
            int(user["user_id"]),
            unit_offering_id,
            target_user_id,
        )
    return jsonify({"status": "ok"})


@feature_blueprint.post(
    "/api/admin/unit-offerings/<int:unit_offering_id>/assessments"
)
@require_json_user
@require_csrf
def api_create_assessment(unit_offering_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        result = create_assessment(
            conn,
            int(user["user_id"]),
            unit_offering_id,
            request.get_json(silent=True) or {},
        )
    return jsonify(result), 201


@feature_blueprint.post(
    "/api/admin/unit-offerings/<int:unit_offering_id>/rosters"
)
@require_json_user
@require_csrf
def api_upload_roster(unit_offering_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        if not can_administer_unit(
            conn,
            int(user["user_id"]),
            unit_offering_id,
        ):
            raise ApiError("unit_forbidden", "Not authorised.", 403)
        upload = store_upload(
            request.files.get("file"),
            "rosters",
            {".csv"},
        )
        try:
            result = create_roster_import(
                conn,
                int(user["user_id"]),
                unit_offering_id,
                upload,
            )
        except Exception:
            remove_stored_upload(upload.storage_path)
            raise
    return jsonify(result), 201


@feature_blueprint.get(
    "/api/admin/rosters/<int:roster_import_id>"
)
@require_json_user
def api_roster_detail(roster_import_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        result = get_roster_import(
            conn,
            int(user["user_id"]),
            roster_import_id,
        )
    return jsonify(result)


@feature_blueprint.post(
    "/api/admin/rosters/<int:roster_import_id>/preview"
)
@require_json_user
@require_csrf
def api_preview_roster(roster_import_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        result = preview_roster_import(
            conn,
            int(user["user_id"]),
            roster_import_id,
            (request.get_json(silent=True) or {}).get("mapping") or {},
        )
    return jsonify(result)


@feature_blueprint.post(
    "/api/admin/rosters/<int:roster_import_id>/commit"
)
@require_json_user
@require_csrf
def api_commit_roster(roster_import_id: int):
    data = request.get_json(silent=True) or {}
    withdraw_missing = data.get("withdraw_missing")
    if withdraw_missing not in {True, False, None}:
        raise ApiError(
            "withdrawal_choice_invalid",
            "Choose whether missing students should be withdrawn.",
            422,
        )
    with connect_db() as conn:
        user = _json_user(conn)
        result = commit_roster_import(
            conn,
            int(user["user_id"]),
            roster_import_id,
            withdraw_missing,
        )
    return jsonify(result)


@feature_blueprint.post(
    "/api/admin/unit-offerings/<int:unit_offering_id>/scoping-notes"
)
@require_json_user
@require_csrf
def api_upload_scoping_note(unit_offering_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        if not can_administer_unit(
            conn,
            int(user["user_id"]),
            unit_offering_id,
        ):
            raise ApiError("unit_forbidden", "Not authorised.", 403)
        files = request.files.getlist("files")
        if not files:
            files = [request.files.get("file")]
        results = []
        job_ids = []
        for file_storage in files:
            file_name = (
                Path(file_storage.filename).name
                if file_storage is not None and file_storage.filename
                else "Unnamed file"
            )
            try:
                upload = store_upload(
                    file_storage,
                    "scoping-notes",
                    {".pdf", ".txt"},
                )
            except UploadValidationError as exc:
                results.append(
                    {
                        "file_name": file_name,
                        "status": "rejected",
                        "error": str(exc),
                    }
                )
                continue
            try:
                job_id = enqueue_document_upload(
                    conn,
                    int(user["user_id"]),
                    "scoping_note_ingest",
                    upload,
                    unit_offering_id=unit_offering_id,
                    title=(
                        request.form.get("title")
                        if len(files) == 1
                        else None
                    ),
                )
            except Exception:
                remove_stored_upload(upload.storage_path)
                current_app.logger.exception(
                    "Could not queue scoping material %s",
                    file_name,
                )
                results.append(
                    {
                        "file_name": file_name,
                        "status": "rejected",
                        "error": "The file could not be queued for processing.",
                    }
                )
                continue
            job_ids.append(job_id)
            results.append(
                {
                    "file_name": upload.original_file_name,
                    "status": "queued",
                    "processing_job_id": job_id,
                    "status_url": f"/api/jobs/{job_id}",
                }
            )
    if not job_ids:
        return (
            jsonify(
                {
                    "error": {
                        "code": "upload_invalid",
                        "message": "No scoping materials were accepted.",
                        "details": {"uploads": results},
                    }
                }
            ),
            422,
        )
    response = {
        "accepted_count": len(job_ids),
        "rejected_count": len(results) - len(job_ids),
        "processing_job_ids": job_ids,
        "uploads": results,
    }
    if len(job_ids) == 1:
        response["processing_job_id"] = job_ids[0]
        response["status_url"] = f"/api/jobs/{job_ids[0]}"
    return jsonify(response), 202


@feature_blueprint.post(
    "/api/admin/scoping-notes/<int:material_id>/deactivate"
)
@require_json_user
@require_csrf
def api_deactivate_scoping_note(material_id: int):
    data = request.get_json(silent=True) or {}
    with connect_db() as conn:
        user = _json_user(conn)
        deactivate_scoping_note(
            conn,
            int(user["user_id"]),
            material_id,
            str(data.get("reason") or ""),
        )
    return jsonify({"status": "deactivated"})


@feature_blueprint.post(
    "/api/admin/scoping-notes/<int:material_id>/restore"
)
@require_json_user
@require_csrf
def api_restore_scoping_note(material_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        job_id = enqueue_scoping_note_restore(
            conn,
            int(user["user_id"]),
            material_id,
        )
    return jsonify(
        {
            "status": "queued",
            "processing_job_id": job_id,
            "status_url": f"/api/jobs/{job_id}",
        }
    ), 202


@feature_blueprint.delete(
    "/api/admin/scoping-notes/<int:material_id>"
)
@require_json_user
@require_csrf
def api_delete_scoping_note(material_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        delete_scoping_note(
            conn,
            int(user["user_id"]),
            material_id,
        )
    return jsonify({"status": "deleted"})


@feature_blueprint.get(
    "/api/admin/scoping-notes/<int:material_id>/download"
)
@require_json_user
def api_download_scoping_note(material_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        path, download_name = get_scoping_note_download(
            conn,
            int(user["user_id"]),
            material_id,
        )
    return send_file(
        path,
        as_attachment=True,
        download_name=download_name,
    )


@feature_blueprint.get(
    "/api/admin/assessments/<int:assessment_plan_id>"
)
@require_json_user
def api_assessment_detail(assessment_plan_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        result = get_assessment_detail(
            conn,
            int(user["user_id"]),
            assessment_plan_id,
        )
    return jsonify(result)


@feature_blueprint.patch(
    "/api/admin/assessments/<int:assessment_plan_id>"
)
@require_json_user
@require_csrf
def api_update_assessment(assessment_plan_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        result = update_assessment(
            conn,
            int(user["user_id"]),
            assessment_plan_id,
            request.get_json(silent=True) or {},
        )
    return jsonify(result)


@feature_blueprint.get(
    "/api/admin/specifications/<int:spec_id>/download"
)
@require_json_user
def api_download_specification(spec_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        path, download_name = get_assessment_document_download(
            conn,
            int(user["user_id"]),
            "specification",
            spec_id,
        )
    return send_file(
        path,
        as_attachment=True,
        download_name=download_name,
    )


@feature_blueprint.get(
    "/api/admin/rubrics/<int:rubric_id>/download"
)
@require_json_user
def api_download_rubric(rubric_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        path, download_name = get_assessment_document_download(
            conn,
            int(user["user_id"]),
            "rubric",
            rubric_id,
        )
    return send_file(
        path,
        as_attachment=True,
        download_name=download_name,
    )


def _enqueue_assessment_document(
    assessment_plan_id: int,
    job_type: str,
    allowed_extensions: set[str],
):
    with connect_db() as conn:
        user = _json_user(conn)
        plan = conn.execute(
            """
            SELECT unit_offering_id
            FROM assessment_plans
            WHERE assessment_plan_id = ?
            """,
            (assessment_plan_id,),
        ).fetchone()
        if plan is None:
            raise ApiError(
                "assessment_not_found",
                "Assessment not found.",
                404,
            )
        if not can_administer_unit(
            conn,
            int(user["user_id"]),
            int(plan["unit_offering_id"]),
        ):
            raise ApiError("assessment_forbidden", "Not authorised.", 403)
        upload = store_upload(
            request.files.get("file"),
            job_type.replace("_ingest", "s"),
            allowed_extensions,
        )
        try:
            job_id = enqueue_document_upload(
                conn,
                int(user["user_id"]),
                job_type,
                upload,
                assessment_plan_id=assessment_plan_id,
            )
        except Exception:
            remove_stored_upload(upload.storage_path)
            raise
    return jsonify(
        {
            "processing_job_id": job_id,
            "status_url": f"/api/jobs/{job_id}",
        }
    ), 202


@feature_blueprint.post(
    "/api/admin/assessments/<int:assessment_plan_id>/specifications"
)
@require_json_user
@require_csrf
def api_upload_specification(assessment_plan_id: int):
    return _enqueue_assessment_document(
        assessment_plan_id,
        "assignment_spec_ingest",
        {".pdf", ".txt"},
    )


@feature_blueprint.post(
    "/api/admin/assessments/<int:assessment_plan_id>/rubrics"
)
@require_json_user
@require_csrf
def api_upload_rubric(assessment_plan_id: int):
    return _enqueue_assessment_document(
        assessment_plan_id,
        "rubric_ingest",
        {".pdf"},
    )


@feature_blueprint.post(
    "/api/admin/assessments/<int:assessment_plan_id>/versions/activate"
)
@require_json_user
@require_csrf
def api_activate_assessment_version(assessment_plan_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        result = activate_latest_assessment_version(
            conn,
            int(user["user_id"]),
            assessment_plan_id,
        )
    return jsonify(result)


@feature_blueprint.post(
    "/api/admin/assessments/<int:assessment_plan_id>/submission-batches"
)
@require_json_user
@require_csrf
def api_upload_submission_batch(assessment_plan_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        plan = conn.execute(
            """
            SELECT unit_offering_id
            FROM assessment_plans
            WHERE assessment_plan_id = ?
            """,
            (assessment_plan_id,),
        ).fetchone()
        if plan is None:
            raise ApiError(
                "assessment_not_found",
                "Assessment not found.",
                404,
            )
        if not can_administer_unit(
            conn,
            int(user["user_id"]),
            int(plan["unit_offering_id"]),
        ):
            raise ApiError("assessment_forbidden", "Not authorised.", 403)
        upload = store_upload(
            request.files.get("file"),
            "submission-batches",
            {".zip"},
            size_limit=get_web_settings().zip_limit_bytes,
        )
        try:
            result = create_submission_batch(
                conn,
                int(user["user_id"]),
                assessment_plan_id,
                upload,
            )
        except Exception:
            remove_stored_upload(upload.storage_path)
            raise
    return jsonify(result), 200 if result["duplicate"] else 202


@feature_blueprint.get(
    "/api/admin/submission-batches/<int:batch_id>"
)
@require_json_user
def api_submission_batch(batch_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        result = get_submission_batch(
            conn,
            int(user["user_id"]),
            batch_id,
        )
    return jsonify(result)


@feature_blueprint.post(
    "/api/admin/submission-batch-items/<int:item_id>/resolve"
)
@require_json_user
@require_csrf
def api_resolve_batch_item(item_id: int):
    raw_student_id = request.form.get("student_id")
    try:
        student_id = int(raw_student_id) if raw_student_id else None
    except (TypeError, ValueError) as exc:
        raise ApiError(
            "student_invalid",
            "Choose a roster student.",
            422,
        ) from exc
    ignore = request.form.get("ignore") in {"1", "true", "True"}
    with connect_db() as conn:
        user = _json_user(conn)
        target = conn.execute(
            """
            SELECT plan.unit_offering_id
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
        if target is None:
            raise ApiError(
                "batch_item_not_found",
                "Batch item not found.",
                404,
            )
        if not can_administer_unit(
            conn,
            int(user["user_id"]),
            int(target["unit_offering_id"]),
        ):
            raise ApiError("assessment_forbidden", "Not authorised.", 403)
        corrected_upload = None
        if request.files.get("file"):
            corrected_upload = store_upload(
                request.files.get("file"),
                "submission-corrections",
                {".pdf"},
            )
        try:
            result = resolve_submission_batch_item(
                conn,
                int(user["user_id"]),
                item_id,
                student_id=student_id,
                corrected_upload=corrected_upload,
                ignore=ignore,
                note=request.form.get("note"),
            )
        except Exception:
            if corrected_upload is not None:
                remove_stored_upload(corrected_upload.storage_path)
            raise
    return jsonify(result)


@feature_blueprint.get("/api/jobs/<int:processing_job_id>")
@require_json_user
def api_job_status(processing_job_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        job = conn.execute(
            """
            SELECT *
            FROM processing_jobs
            WHERE processing_job_id = ?
            """,
            (processing_job_id,),
        ).fetchone()
        if job is None:
            raise ApiError("job_not_found", "Processing job not found.", 404)
        offering_id = job["unit_offering_id"]
        if offering_id is None and job["assessment_plan_id"] is not None:
            plan = conn.execute(
                """
                SELECT unit_offering_id
                FROM assessment_plans
                WHERE assessment_plan_id = ?
                """,
                (job["assessment_plan_id"],),
            ).fetchone()
            offering_id = plan["unit_offering_id"] if plan else None
        if offering_id is None and job["submission_batch_id"] is not None:
            row = conn.execute(
                """
                SELECT plan.unit_offering_id
                FROM submission_batches AS batch
                JOIN assessment_activities AS activity
                  ON activity.assessment_activity_id =
                     batch.assessment_activity_id
                JOIN assessment_plan_versions AS version
                  ON version.assessment_plan_version_id =
                     activity.assessment_plan_version_id
                JOIN assessment_plans AS plan
                  ON plan.assessment_plan_id =
                     version.assessment_plan_id
                WHERE batch.submission_batch_id = ?
                """,
                (job["submission_batch_id"],),
            ).fetchone()
            offering_id = row["unit_offering_id"] if row else None
        if offering_id is None or not can_administer_unit(
            conn,
            int(user["user_id"]),
            int(offering_id),
        ):
            raise ApiError("job_forbidden", "Not authorised.", 403)
        result = dict(job)
        result["payload_json"] = None
    return jsonify({"job": result})


@feature_blueprint.get(
    "/api/admin/unit-offerings/<int:unit_offering_id>/activation-entry"
)
@require_json_user
def api_get_activation_entry(unit_offering_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        result = current_unit_entry(
            conn,
            int(user["user_id"]),
            unit_offering_id,
        )
    return jsonify(result)


@feature_blueprint.post(
    "/api/admin/unit-offerings/<int:unit_offering_id>/activation-entry"
)
@require_json_user
@require_csrf
def api_rotate_activation_entry(unit_offering_id: int):
    with connect_db() as conn:
        user = _json_user(conn)
        result = create_or_rotate_unit_entry(
            conn,
            int(user["user_id"]),
            unit_offering_id,
        )
    return jsonify(result), 201


@feature_blueprint.get("/activate/<path:entry_token>")
def activation_entry_page(entry_token: str):
    with connect_db() as conn:
        entry = verify_token(
            conn,
            entry_token,
            "unit_activation_entry",
        )
    return render_template(
        "activate.html",
        entry_token=entry_token,
        entry_valid=entry is not None,
    )


@feature_blueprint.post("/api/account/activation/request")
def api_activation_request():
    data = request.get_json(silent=True) or request.form
    with connect_db() as conn:
        message = request_activation(
            conn,
            str(data.get("entry_token") or ""),
            str(data.get("student_identifier") or ""),
            str(data.get("email") or ""),
            request.remote_addr or "unknown",
            resend=False,
        )
    return jsonify({"message": message}), 202


@feature_blueprint.post("/api/account/activation/resend")
def api_activation_resend():
    data = request.get_json(silent=True) or request.form
    with connect_db() as conn:
        message = request_activation(
            conn,
            str(data.get("entry_token") or ""),
            str(data.get("student_identifier") or ""),
            str(data.get("email") or ""),
            request.remote_addr or "unknown",
            resend=True,
        )
    return jsonify({"message": message}), 202


@feature_blueprint.route(
    "/account/activate/<path:token_value>",
    methods=["GET", "POST"],
)
def complete_activation_page(token_value: str):
    if request.method == "POST":
        password = request.form.get("password") or ""
        confirmation = request.form.get("password_confirmation") or ""
        if password != confirmation:
            return render_template(
                "set_password.html",
                title="Activate account",
                token_value=token_value,
                action_url=request.path,
                error="Passwords do not match.",
            ), 422
        with connect_db() as conn:
            complete_activation(conn, token_value, password)
        return render_template(
            "account_message.html",
            title="Account activated",
            message="Your account is ready. You can now log in.",
            success=True,
        )
    with connect_db() as conn:
        valid = (
            verify_token(conn, token_value, "student_activation")
            is not None
        )
    if not valid:
        raise ApiError(
            "token_invalid",
            "This activation link is invalid or has expired.",
            409,
        )
    return render_template(
        "set_password.html",
        title="Activate account",
        token_value=token_value,
        action_url=request.path,
        error=None,
    )


@feature_blueprint.route("/forgot-password", methods=["GET", "POST"])
def forgot_password_page():
    message = None
    if request.method == "POST":
        with connect_db() as conn:
            message = request_password_reset(
                conn,
                request.form.get("email") or "",
                request.remote_addr or "unknown",
            )
    return render_template(
        "forgot_password.html",
        message=message,
    )


@feature_blueprint.route(
    "/account/reset/<path:token_value>",
    methods=["GET", "POST"],
)
def password_reset_page(token_value: str):
    if request.method == "POST":
        password = request.form.get("password") or ""
        confirmation = request.form.get("password_confirmation") or ""
        if password != confirmation:
            return render_template(
                "set_password.html",
                title="Reset password",
                token_value=token_value,
                action_url=request.path,
                error="Passwords do not match.",
            ), 422
        with connect_db() as conn:
            complete_password_reset(conn, token_value, password)
        session.clear()
        return render_template(
            "account_message.html",
            title="Password updated",
            message="Your password has been changed. Log in again.",
            success=True,
        )
    with connect_db() as conn:
        valid = (
            verify_token(conn, token_value, "password_reset")
            is not None
        )
    if not valid:
        raise ApiError(
            "token_invalid",
            "This password reset link is invalid or has expired.",
            409,
        )
    return render_template(
        "set_password.html",
        title="Reset password",
        token_value=token_value,
        action_url=request.path,
        error=None,
    )
