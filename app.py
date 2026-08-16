import os
import threading
from functools import wraps

from flask import Flask, render_template, request, redirect, session, jsonify
from werkzeug.security import check_password_hash

from feedback_lens.db.connection import connect_db
from feedback_lens.feedback.quality_gate import generate_feedback_with_quality_gate
from feedback_lens.feedback.pipeline import (
    regenerate_feedback_for_criterion,
)
from feedback_lens.feedback.prompt import DEFAULT_FEEDBACK_MODIFIER_MODE
from feedback_lens.feedback.review import fetch_generation_review, parse_json_text_list
from feedback_lens.web import feature_blueprint
from feedback_lens.web.config import get_secret_key, get_web_settings
from feedback_lens.web.jobs import run_worker_forever
from feedback_lens.web.security import can_access_admin_workspace, csrf_token
from feedback_lens.web.staff_portal_service import (
    assignment_feedback_model,
    fetch_authorised_generation,
    fetch_authorised_submission,
    generation_workflow,
    get_unit_dashboard_data,
    get_unit_submissions_data,
    list_staff_units,
    record_generated_feedback,
    update_feedback_workflow,
)
from feedback_lens.web.embedded_evaluation import (
    EVALUATION_QUESTIONS,
    MAX_COMMENT_LENGTH,
    OPTIONAL_COMMENT_PROMPT,
    RATING_ANCHORS,
    VOLUNTARY_NOTICES,
    delete_embedded_evaluation,
    fetch_embedded_evaluation,
    pseudonymous_rater_key,
    save_embedded_evaluation,
    validate_evaluation_payload,
)

app = Flask(__name__)
app.secret_key = get_secret_key()
app.config["MAX_CONTENT_LENGTH"] = get_web_settings().zip_limit_bytes
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("FEEDBACK_LENS_SECURE_COOKIES", "0")
    in {"1", "true", "True"}
)
app.register_blueprint(feature_blueprint)
DEFAULT_FEEDBACK_GENERATION_MODE = "retrieval"
DEFAULT_FEEDBACK_GENERATION_STRATEGY = "planned"
DEFAULT_RETRIEVAL_PROMPT_TEMPLATE = "unit-grounded-v2"


def login_required(role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            with connect_db() as conn:
                user = fetch_session_user(conn)
            if user is None or user["role"] != role:
                session.clear()
                return redirect("/login")
            return view(*args, **kwargs)

        return wrapped

    return decorator


def staff_portal_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        with connect_db() as conn:
            user = fetch_session_user(conn)
        if user is None or user["role"] == "student":
            session.clear()
            return redirect("/login")
        return view(*args, **kwargs)

    return wrapped


def fetch_session_user(conn):
    user_id = session.get("user_id")
    email = session.get("email")
    if user_id is None and email is None:
        return None

    if user_id is not None:
        user = conn.execute(
            """
            SELECT
                u.user_id,
                u.email,
                u.role,
                u.display_name,
                u.tutor_id,
                u.session_version,
                t.full_name AS tutor_full_name
            FROM users AS u
            LEFT JOIN tutors AS t ON t.tutor_id = u.tutor_id
            WHERE u.user_id = ?
            """,
            (user_id,),
        ).fetchone()
    else:
        user = conn.execute(
            """
            SELECT
                u.user_id,
                u.email,
                u.role,
                u.display_name,
                u.tutor_id,
                u.session_version,
                t.full_name AS tutor_full_name
            FROM users AS u
            LEFT JOIN tutors AS t ON t.tutor_id = u.tutor_id
            WHERE lower(u.email) = lower(?)
            """,
            (email,),
        ).fetchone()
    if not _session_version_matches(user):
        session.clear()
        return None
    return user


def _session_version_matches(user):
    if user is None:
        return False
    stored_version = session.get("session_version")
    if stored_version is None:
        # Existing route tests create a minimal session directly. Production
        # sessions must always carry the version written by the login route.
        return bool(app.config.get("TESTING"))
    return int(user["session_version"]) == int(stored_version)


def api_session_user(required_role=None):
    with connect_db() as conn:
        user = fetch_session_user(conn)
        if user is None:
            return None, (jsonify({"error": "Authentication required"}), 401)
        if required_role is not None and user["role"] != required_role:
            return None, (jsonify({"error": "Forbidden"}), 403)
        return dict(user), None


def _coerce_optional_int(value, label):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{label} must be an integer.") from err


def _coerce_optional_float(value, label):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{label} must be a number.") from err


def _resolve_feedback_modifier_payload(data):
    feedback_length = data.get('feedback_length')
    feedback_tone = data.get('feedback_tone')
    length_supplied = feedback_length is not None and feedback_length != ''
    tone_supplied = feedback_tone is not None and feedback_tone != ''
    feedback_modifier_mode = (
        data.get('feedback_modifier_mode')
        or data.get('feedback_customisation_mode')
    )
    if feedback_modifier_mode is None:
        feedback_modifier_mode = (
            'custom'
            if length_supplied or tone_supplied
            else DEFAULT_FEEDBACK_MODIFIER_MODE
        )

    return (
        feedback_modifier_mode,
        feedback_length if length_supplied else None,
        feedback_tone if tone_supplied else None,
    )


def _staff_api_user():
    user, error = api_session_user()
    if error:
        return None, error
    if user["role"] == "student":
        return None, (jsonify({"error": "Forbidden"}), 403)
    return user, None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
def admin():
    return redirect("/admin/units")

@app.route('/leadLecture')
def lead_lecture():
    return redirect("/admin/units")


@app.route('/api/lead/dashboard')
def lead_dashboard_data():
    user, error = api_session_user(required_role='lead_lecturer')
    if error:
        return error

    with connect_db() as conn:
        
        units = conn.execute(
            """
            SELECT
                u.unit_id,
                u.unit_code,
                u.unit_name,
                u.semester,
                u.year,
                COUNT(DISTINCT ut.tutor_id) AS educator_count,
                COUNT(DISTINCT ss.submission_id) AS submission_count,
                COUNT(DISTINCT CASE
                    WHEN gr.status = 'completed' AND hr.review_id IS NULL
                    THEN ss.submission_id END) AS pending_count,
                COUNT(DISTINCT CASE
                    WHEN hr.review_id IS NOT NULL
                    THEN ss.submission_id END) AS finalised_count
            FROM units u
            LEFT JOIN unit_tutors ut ON ut.unit_id = u.unit_id
            LEFT JOIN assignments a ON a.unit_id = u.unit_id
            LEFT JOIN student_submissions ss ON ss.assignment_id = a.assignment_id
            LEFT JOIN generation_runs gr
                ON gr.submission_id = ss.submission_id
                AND gr.generation_id = (
                    SELECT MAX(generation_id)
                    FROM generation_runs
                    WHERE submission_id = ss.submission_id
                )
            LEFT JOIN human_reviews hr ON hr.generation_id = gr.generation_id
            GROUP BY u.unit_id
            ORDER BY u.unit_code
            """,
        ).fetchall()

        totals = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM units) AS total_units,
                (SELECT COUNT(*) FROM student_submissions) AS total_submissions,
                (SELECT COUNT(DISTINCT ss.submission_id)
                    FROM student_submissions ss
                    LEFT JOIN generation_runs gr
                        ON gr.submission_id = ss.submission_id
                        AND gr.generation_id = (
                            SELECT MAX(generation_id) FROM generation_runs
                            WHERE submission_id = ss.submission_id
                        )
                    LEFT JOIN human_reviews hr ON hr.generation_id = gr.generation_id
                    WHERE gr.status = 'completed' AND hr.review_id IS NULL
                ) AS pending_approvals,
                (SELECT COUNT(DISTINCT ss.submission_id)
                    FROM student_submissions ss
                    LEFT JOIN generation_runs gr
                        ON gr.submission_id = ss.submission_id
                    LEFT JOIN human_reviews hr ON hr.generation_id = gr.generation_id
                    WHERE hr.review_id IS NOT NULL
                ) AS finalised_submissions
            """,
        ).fetchone()

    return jsonify({
        'user': dict(user),
        'totals': dict(totals) if totals else {},
        'units': [dict(u) for u in units],
    })



@app.route('/educator')
@staff_portal_required
def educator():
    return render_template("educator.html", csrf_token=csrf_token())

@app.route('/student')
@login_required('student')
def student_home():
    return render_template('student_home.html')


@app.route('/student/units')
@login_required('student')
def student_units():
    return render_template('student_units.html')


@app.route('/student/unit/<int:unit_id>')
@login_required('student')
def student_unit_detail(unit_id):
    return render_template('student_unit_detail.html', unit_id=unit_id)


@app.route('/student/assessments')
@login_required('student')
def student_assessments():
    return render_template('student_assessments.html')


@app.route('/student/assessment/<int:assignment_id>')
@login_required('student')
def student_assessment_detail(assignment_id):
    return render_template('student_assessment_detail.html', assignment_id=assignment_id)


@app.route('/student/feedback')
@login_required('student')
def student_feedback():
    return render_template('student_feedback.html')


@app.route('/student/feedback/<int:generation_id>')
@login_required('student')
def student_feedback_detail(generation_id):
    return render_template('student_feedback_detail.html', generation_id=generation_id)


def _student_identifier_or_error():
    user, error = api_session_user(required_role='student')
    if error:
        return None, None, error
    
    with connect_db() as conn:
        row = conn.execute(
            "SELECT student_identifier FROM users WHERE user_id = ?",
            (user['user_id'],)
        ).fetchone()
    sid = row['student_identifier'] if row else None
    if not sid:
        return None, None, (jsonify({'error': 'Student account is not linked to a student record'}), 403)
    user['student_identifier'] = sid
    return user, sid, None


def _can_evaluate_feedback(conn, user, generation_id):
    if user["role"] == "student":
        student = conn.execute(
            """
            SELECT student_identifier
            FROM users
            WHERE user_id = ?
            """,
            (user["user_id"],),
        ).fetchone()
        if student is None or not student["student_identifier"]:
            return False
        return (
            conn.execute(
                """
                SELECT 1
                FROM generation_runs AS gr
                JOIN student_submissions AS ss
                  ON ss.submission_id = gr.submission_id
                WHERE gr.generation_id = ?
                  AND ss.student_identifier = ?
                  AND EXISTS (
                      SELECT 1
                      FROM human_reviews AS hr
                      WHERE hr.generation_id = gr.generation_id
                        AND hr.approved = 1
                  )
                """,
                (generation_id, student["student_identifier"]),
            ).fetchone()
            is not None
        )

    if user["role"] == "educator" and user.get("tutor_id") is not None:
        return (
            conn.execute(
                """
                SELECT 1
                FROM generation_runs AS gr
                JOIN assignments AS a
                  ON a.assignment_id = gr.assignment_id
                JOIN unit_tutors AS ut
                  ON ut.unit_id = a.unit_id
                WHERE gr.generation_id = ?
                  AND ut.tutor_id = ?
                """,
                (generation_id, user["tutor_id"]),
            ).fetchone()
            is not None
        )

    return False


@app.route(
    "/api/feedback/<int:generation_id>/embedded-evaluation",
    methods=["GET", "POST", "DELETE"],
)
def embedded_feedback_evaluation(generation_id):
    user, error = api_session_user()
    if error:
        return error
    participant_role = user.get("role")
    if participant_role not in EVALUATION_QUESTIONS:
        return jsonify({"error": "Forbidden"}), 403
    rater_key_hash = pseudonymous_rater_key(
        user["user_id"],
        app.secret_key,
    )

    with connect_db() as conn:
        if not _can_evaluate_feedback(conn, user, generation_id):
            return jsonify({"error": "Feedback not found"}), 404

        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            try:
                rating, comment = validate_evaluation_payload(data)
            except ValueError as err:
                return jsonify({"error": str(err)}), 400
            evaluation = save_embedded_evaluation(
                conn,
                generation_id,
                participant_role,
                rater_key_hash,
                rating_usefulness=rating,
                comment=comment,
            )
            conn.commit()
        elif request.method == "DELETE":
            deleted = delete_embedded_evaluation(
                conn,
                generation_id,
                participant_role,
                rater_key_hash,
            )
            conn.commit()
            return jsonify({"status": "ok", "deleted": deleted})
        else:
            evaluation = fetch_embedded_evaluation(
                conn,
                generation_id,
                participant_role,
                rater_key_hash,
            )

    return jsonify(
        {
            "status": "ok",
            "participant_role": participant_role,
            "question": EVALUATION_QUESTIONS[participant_role],
            "rating_anchors": RATING_ANCHORS,
            "optional_comment_prompt": OPTIONAL_COMMENT_PROMPT,
            "voluntary_notice": VOLUNTARY_NOTICES[participant_role],
            "estimated_time_seconds": 60,
            "max_comment_length": MAX_COMMENT_LENGTH,
            "evaluation": evaluation,
        }
    )


@app.route('/api/student/home')
def student_home_data():
    user, sid, error = _student_identifier_or_error()
    if error:
        return error

    with connect_db() as conn:
        counts = conn.execute("""
            SELECT
                (SELECT COUNT(*) FROM student_submissions WHERE student_identifier = ?) AS total_submissions,
                (SELECT COUNT(DISTINCT of.generation_id)
                    FROM overall_feedback of
                    JOIN generation_runs gr ON gr.generation_id = of.generation_id
                    JOIN student_submissions ss ON ss.submission_id = gr.submission_id
                    WHERE ss.student_identifier = ?
                ) AS total_feedback
        """, (sid, sid)).fetchone()

    return jsonify({
        'user': dict(user),
        'counts': dict(counts) if counts else {}
    })


@app.route('/api/student/units')
def student_units_data():
    user, sid, error = _student_identifier_or_error()
    if error:
        return error

    with connect_db() as conn:
        # check if is_archived column exists (it may not exist yet, will be added by backend)- please fix it
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(units)").fetchall()]
        has_archived = 'is_archived' in cols
        archived_select = 'u.is_archived' if has_archived else '0 AS is_archived'

        units = conn.execute(f"""
            SELECT DISTINCT
                u.unit_id, u.unit_code, u.unit_name, u.semester, u.year,
                {archived_select},
                (SELECT COUNT(*) FROM assignments a2 WHERE a2.unit_id = u.unit_id) AS total_assignments,
                (SELECT COUNT(*) FROM student_submissions ss2
                    JOIN assignments a3 ON a3.assignment_id = ss2.assignment_id
                    WHERE a3.unit_id = u.unit_id AND ss2.student_identifier = ?
                ) AS submitted_count
            FROM units u
            JOIN assignments a ON a.unit_id = u.unit_id
            JOIN student_submissions ss ON ss.assignment_id = a.assignment_id
            WHERE ss.student_identifier = ?
            ORDER BY u.unit_code
        """, (sid, sid)).fetchall()

    return jsonify({'units': [dict(u) for u in units]})


@app.route('/api/student/unit/<int:unit_id>')
def student_unit_data(unit_id):
    user, sid, error = _student_identifier_or_error()
    if error:
        return error

    with connect_db() as conn:
        unit = conn.execute("""
            SELECT unit_id, unit_code, unit_name FROM units WHERE unit_id = ?
        """, (unit_id,)).fetchone()
        if unit is None:
            return jsonify({'error': 'Unit not found'}), 404

        assignments = conn.execute("""
            SELECT
                a.assignment_id, a.assignment_name, a.assignment_type, a.due_date,
                ss.submission_id, ss.submitted_at,
                gr.generation_id,
                CASE
                    WHEN hr.review_id IS NOT NULL THEN 'marked'
                    WHEN ss.submission_id IS NOT NULL THEN 'pending'
                    ELSE 'not_submitted'
                END AS status
            FROM assignments a
            LEFT JOIN student_submissions ss
                ON ss.assignment_id = a.assignment_id AND ss.student_identifier = ?
            LEFT JOIN generation_runs gr
                ON gr.submission_id = ss.submission_id
                AND gr.generation_id = (
                    SELECT MAX(generation_id) FROM generation_runs WHERE submission_id = ss.submission_id
                )
            LEFT JOIN human_reviews hr ON hr.generation_id = gr.generation_id
            WHERE a.unit_id = ?
            ORDER BY a.assignment_id
        """, (sid, unit_id)).fetchall()

    return jsonify({
        'unit': dict(unit),
        'assignments': [dict(a) for a in assignments]
    })


@app.route('/api/student/assessments')
def student_assessments_data():
    user, sid, error = _student_identifier_or_error()
    if error:
        return error

    with connect_db() as conn:
        rows = conn.execute("""
            SELECT
                a.assignment_id, a.assignment_name, a.assignment_type, a.due_date,
                u.unit_id, u.unit_code, u.unit_name,
                ss.submission_id, ss.submitted_at,
                gr.generation_id,
                CASE
                    WHEN hr.review_id IS NOT NULL THEN 'marked'
                    WHEN ss.submission_id IS NOT NULL THEN 'pending'
                    ELSE 'not_submitted'
                END AS status
            FROM assignments a
            JOIN units u ON u.unit_id = a.unit_id
            LEFT JOIN student_submissions ss
                ON ss.assignment_id = a.assignment_id AND ss.student_identifier = ?
            LEFT JOIN generation_runs gr
                ON gr.submission_id = ss.submission_id
                AND gr.generation_id = (
                    SELECT MAX(generation_id) FROM generation_runs WHERE submission_id = ss.submission_id
                )
            LEFT JOIN human_reviews hr ON hr.generation_id = gr.generation_id
            WHERE a.unit_id IN (
                SELECT DISTINCT a2.unit_id
                FROM assignments a2
                JOIN student_submissions ss2 ON ss2.assignment_id = a2.assignment_id
                WHERE ss2.student_identifier = ?
            )
            ORDER BY a.due_date, a.assignment_id
        """, (sid, sid)).fetchall()

    return jsonify({'assignments': [dict(r) for r in rows]})


@app.route('/api/student/download/spec/<int:spec_id>')
def student_download_spec(spec_id):
    user, sid, error = _student_identifier_or_error()
    if error:
        return error

    with connect_db() as conn:
        row = conn.execute("""
            SELECT s.source_file_path, s.assignment_id
            FROM assignment_specs s
            JOIN assignments a ON a.assignment_id = s.assignment_id
            JOIN student_submissions ss ON ss.assignment_id = a.assignment_id
            WHERE s.spec_id = ? AND ss.student_identifier = ?
            LIMIT 1
        """, (spec_id, sid)).fetchone()

    if not row or not row['source_file_path']:
        return jsonify({'error': 'File not found'}), 404

    return _send_file_safe(row['source_file_path'])


@app.route('/api/student/download/material/<int:material_id>')
def student_download_material(material_id):
    user, sid, error = _student_identifier_or_error()
    if error:
        return error

    with connect_db() as conn:
        row = conn.execute("""
            SELECT m.source_file_path
            FROM unit_materials m
            JOIN assignments a ON a.assignment_id = m.assignment_id
            JOIN student_submissions ss ON ss.assignment_id = a.assignment_id
            WHERE m.material_id = ? AND ss.student_identifier = ?
            LIMIT 1
        """, (material_id, sid)).fetchone()

    if not row or not row['source_file_path']:
        return jsonify({'error': 'File not found'}), 404

    return _send_file_safe(row['source_file_path'])


@app.route('/api/student/download/submission/<int:submission_id>')
def student_download_submission(submission_id):
    user, sid, error = _student_identifier_or_error()
    if error:
        return error

    with connect_db() as conn:
        row = conn.execute("""
            SELECT original_file_path
            FROM student_submissions
            WHERE submission_id = ? AND student_identifier = ?
        """, (submission_id, sid)).fetchone()

    if not row or not row['original_file_path']:
        return jsonify({'error': 'File not found'}), 404

    return _send_file_safe(row['original_file_path'])


def _send_file_safe(file_path):
    import os
    from flask import send_file, abort
    # resolve to absolute path and check it exists
    if not os.path.exists(file_path):
        # try relative to project root
        abs_path = os.path.join(os.path.dirname(__file__), file_path)
        if not os.path.exists(abs_path):
            return jsonify({'error': 'File missing on disk'}), 404
        file_path = abs_path
    return send_file(file_path, as_attachment=True)

@app.route('/api/student/assessment/<int:assignment_id>')
def student_assessment_data(assignment_id):
    user, sid, error = _student_identifier_or_error()
    if error:
        return error

    with connect_db() as conn:
        a = conn.execute("""
            SELECT
                a.assignment_id, a.assignment_name, a.assignment_type, a.due_date,
                u.unit_id, u.unit_code, u.unit_name
            FROM assignments a
            JOIN units u ON u.unit_id = a.unit_id
            WHERE a.assignment_id = ?
        """, (assignment_id,)).fetchone()
        if a is None:
            return jsonify({'error': 'Assessment not found'}), 404

        sub = conn.execute("""
            SELECT submission_id, submitted_at, original_file_path
            FROM student_submissions
            WHERE assignment_id = ? AND student_identifier = ?
            ORDER BY submission_id DESC
            LIMIT 1
        """, (assignment_id, sid)).fetchone()

        gen = None
        review_status = 'not_submitted'
        if sub:
            gen = conn.execute("""
                SELECT generation_id, status
                FROM generation_runs
                WHERE submission_id = ?
                ORDER BY generation_id DESC
                LIMIT 1
            """, (sub['submission_id'],)).fetchone()
            if gen:
                hr = conn.execute("""
                    SELECT review_id FROM human_reviews WHERE generation_id = ?
                """, (gen['generation_id'],)).fetchone()
                review_status = 'marked' if hr else 'pending'
            else:
                review_status = 'pending'

        spec = conn.execute("""
            SELECT spec_id, source_file_path FROM assignment_specs WHERE assignment_id = ?
        """, (assignment_id,)).fetchone()
        materials = conn.execute("""
            SELECT material_id, title, material_type, source_file_path
            FROM unit_materials WHERE assignment_id = ?
        """, (assignment_id,)).fetchall()

    return jsonify({
        'assignment': dict(a),
        'submission': dict(sub) if sub else None,
        'generation_id': gen['generation_id'] if gen else None,
        'status': review_status,
        'spec': dict(spec) if spec else None,
        'materials': [dict(m) for m in materials]
    })


@app.route('/api/student/feedback-list')
def student_feedback_list():
    user, sid, error = _student_identifier_or_error()
    if error:
        return error

    with connect_db() as conn:
        units = conn.execute("""
            SELECT
                u.unit_id, u.unit_code, u.unit_name,
                COUNT(DISTINCT gr.generation_id) AS feedback_count
            FROM units u
            JOIN assignments a ON a.unit_id = u.unit_id
            JOIN student_submissions ss ON ss.assignment_id = a.assignment_id
            JOIN generation_runs gr ON gr.submission_id = ss.submission_id
            JOIN overall_feedback of ON of.generation_id = gr.generation_id
            WHERE ss.student_identifier = ?
            GROUP BY u.unit_id
            HAVING feedback_count > 0
            ORDER BY u.unit_code
        """, (sid,)).fetchall()

    return jsonify({'units': [dict(u) for u in units]})


@app.route('/api/student/feedback-list/<int:unit_id>')
def student_feedback_by_unit(unit_id):
    user, sid, error = _student_identifier_or_error()
    if error:
        return error

    with connect_db() as conn:
        items = conn.execute("""
            SELECT
                gr.generation_id,
                a.assignment_name,
                ss.submitted_at,
                of.overall_grade_band,
                of.final_mark,
                CASE
                    WHEN hr.review_id IS NOT NULL THEN 'marked'
                    ELSE 'pending'
                END AS status
            FROM generation_runs gr
            JOIN student_submissions ss ON ss.submission_id = gr.submission_id
            JOIN assignments a ON a.assignment_id = ss.assignment_id
            LEFT JOIN overall_feedback of ON of.generation_id = gr.generation_id
            LEFT JOIN human_reviews hr ON hr.generation_id = gr.generation_id
            WHERE a.unit_id = ? AND ss.student_identifier = ?
            ORDER BY gr.generation_id DESC
        """, (unit_id, sid)).fetchall()

    return jsonify({'items': [dict(i) for i in items]})


@app.route('/api/student/feedback/<int:generation_id>')
def student_feedback_detail_data(generation_id):
    user, sid, error = _student_identifier_or_error()
    if error:
        return error

    with connect_db() as conn:
        #confirm this feedback belongs to this student
        check = conn.execute("""
            SELECT gr.generation_id
            FROM generation_runs gr
            JOIN student_submissions ss ON ss.submission_id = gr.submission_id
            WHERE gr.generation_id = ? AND ss.student_identifier = ?
        """, (generation_id, sid)).fetchone()
        if not check:
            return jsonify({'error': 'Feedback not found'}), 404

        try:
            data = fetch_generation_review(conn, generation_id)
        except ValueError as err:
            return jsonify({'error': str(err)}), 404

        submission = conn.execute("""
            SELECT
                ss.cleaned_text, ss.submitted_at,
                u.unit_id, u.unit_code, u.unit_name,
                a.assignment_name
            FROM student_submissions ss
            JOIN assignments a ON a.assignment_id = ss.assignment_id
            JOIN units u ON u.unit_id = a.unit_id
            WHERE ss.submission_id = ?
        """, (data['run']['submission_id'],)).fetchone()

        # look up the educator linked to this unit
        educator = None
        if submission:
            educator = conn.execute("""
                SELECT t.full_name
                FROM unit_tutors ut
                JOIN tutors t ON t.tutor_id = ut.tutor_id
                WHERE ut.unit_id = ?
                LIMIT 1
            """, (submission['unit_id'],)).fetchone()

    run = dict(data['run'])
    if submission:
        run['submission_text'] = submission['cleaned_text']
        run['submitted_at'] = submission['submitted_at']
        run['unit_code'] = submission['unit_code']
        run['unit_name'] = submission['unit_name']
        run['assignment_name'] = submission['assignment_name']
    if educator:
        run['educator_name'] = educator['full_name']

    overall = dict(data.get('overall_feedback') or {})
    if overall:
        overall['key_strengths'] = parse_json_text_list(overall.get('key_strengths'))
        overall['priority_improvements'] = parse_json_text_list(overall.get('priority_improvements'))

    criteria = [dict(c) for c in (data.get('criterion_feedback') or [])]

    return jsonify({
        'run': run,
        'overall_feedback': overall,
        'criterion_feedback': criteria
    })

@app.route('/educator/feedback-review')
@staff_portal_required
def feedback_review():
    return render_template('feedback_review.html')

@app.route('/educator/general-feedback')
@staff_portal_required
def general_feedback():
    return render_template('general_feedback.html')



@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.route('/educator/unit/<int:unit_id>')
@staff_portal_required
def unit_dashboard(unit_id):
    return render_template('unit_dashboard.html', unit_id=unit_id)


@app.route('/educator/unit/<int:unit_id>/ai-performance')
@staff_portal_required
def unit_ai_performance(unit_id):
    return render_template('ai_performance.html', unit_id=unit_id)

@app.route('/educator/unit/<int:unit_id>/export')
@staff_portal_required
def unit_export(unit_id):
    return render_template('export.html', unit_id=unit_id)

@app.route('/api/educator/unit/<int:unit_id>/dashboard')
def unit_dashboard_data(unit_id):
    user, error = _staff_api_user()
    if error:
        return error

    with connect_db() as conn:
        result = get_unit_dashboard_data(conn, user['user_id'], unit_id)
        if result is None:
            return jsonify({'error': 'Unit not found or not authorised'}), 404
    return jsonify(result)

@app.route('/educator/unit/<int:unit_id>/submissions')
@staff_portal_required
def submissions_list(unit_id):
    return render_template('submissions_list.html', unit_id=unit_id)


@app.route('/api/educator/unit/<int:unit_id>/submissions')
def unit_submissions_data(unit_id):
    user, error = _staff_api_user()
    if error:
        return error

    with connect_db() as conn:
        result = get_unit_submissions_data(conn, user['user_id'], unit_id)
        if result is None:
            return jsonify({'error': 'Unit not found or not authorised'}), 404
    return jsonify(result)

@app.route('/api/educator/dashboard')
def educator_dashboard_data():
    user, error = _staff_api_user()
    if error:
        return error

    with connect_db() as conn:
        units = list_staff_units(conn, user['user_id'])
        can_access_admin = can_access_admin_workspace(
            conn,
            user['user_id'],
        )
    return jsonify({
        'user': {
            'name': user.get('display_name') or user.get('tutor_full_name') or user['email'],
            'email': user['email'],
            'role': user['role'],
        },
        'units': units,
        'can_access_admin': can_access_admin,
    })

@app.route('/leadLecture/units')
@login_required('lead_lecturer')
def lead_units_page():
    return render_template('lead_units.html')


@app.route('/leadLecture/unit/<int:unit_id>')
@login_required('lead_lecturer')
def lead_unit_detail_page(unit_id):
    return render_template('lead_unit_detail.html', unit_id=unit_id)


@app.route('/leadLecture/reporting')
@login_required('lead_lecturer')
def lead_reporting_page():
    return render_template('lead_reporting.html')


@app.route('/leadLecture/feedback/<int:generation_id>')
@login_required('lead_lecturer')
def lead_feedback_detail_page(generation_id):
    return render_template('lead_feedback_detail.html', generation_id=generation_id)


@app.route('/api/lead/reporting')
def lead_reporting_data():
    user, error = api_session_user(required_role='lead_lecturer')
    if error:
        return error

    with connect_db() as conn:
        rows = conn.execute("""
            SELECT
                gr.generation_id,
                ss.submission_id,
                ss.student_identifier,
                ss.submitted_at,
                a.assignment_id,
                a.assignment_name,
                u.unit_id,
                u.unit_code,
                u.unit_name,
                t.full_name AS educator_name,
                t.tutor_id AS educator_id,
                of.overall_grade_band,
                of.final_mark,
                (SELECT MIN(dim_avg) FROM (
                    SELECT AVG(je.score) AS dim_avg
                    FROM judge_evaluations je
                    WHERE je.generation_id = gr.generation_id
                      AND je.accepted = 1
                      AND je.score IS NOT NULL
                    GROUP BY je.dimension
                )) AS judge_min_score,
                (SELECT MAX(je2.attempt_number) FROM judge_evaluations je2
                 WHERE je2.generation_id = gr.generation_id) AS judge_attempts,
                CASE
                    WHEN hr.review_id IS NOT NULL THEN 'reviewed'
                    WHEN gr.status = 'completed' THEN 'ai_generated'
                    ELSE 'pending'
                END AS status
            FROM generation_runs gr
            JOIN student_submissions ss ON ss.submission_id = gr.submission_id
            JOIN assignments a ON a.assignment_id = gr.assignment_id
            JOIN units u ON u.unit_id = a.unit_id
            LEFT JOIN unit_tutors ut ON ut.unit_id = u.unit_id
            LEFT JOIN tutors t ON t.tutor_id = ut.tutor_id
            LEFT JOIN overall_feedback of ON of.generation_id = gr.generation_id
            LEFT JOIN human_reviews hr ON hr.generation_id = gr.generation_id
            WHERE gr.generation_id IN (
                SELECT MAX(generation_id) FROM generation_runs GROUP BY submission_id
            )
            GROUP BY gr.generation_id
            ORDER BY gr.generation_id DESC
        """).fetchall()

    rows = [dict(r) for r in rows]
    for r in rows:
        min_score = r.get("judge_min_score")
        if min_score is None:
            r["quality_flag"] = None
        elif min_score >= 4:
            r["quality_flag"] = (
                "passed_after_revision" if (r.get("judge_attempts") or 1) > 1 else "passed"
            )
        else:
            r["quality_flag"] = "needs_review"

    return jsonify({'submissions': rows})


@app.route('/api/lead/feedback/<int:generation_id>')
def lead_feedback_detail(generation_id):
    user, error = api_session_user(required_role='lead_lecturer')
    if error:
        return error

    with connect_db() as conn:
        try:
            data = fetch_generation_review(conn, generation_id)
        except ValueError as err:
            return jsonify({'error': str(err)}), 404

        submission = conn.execute("""
            SELECT
                ss.cleaned_text,
                ss.submitted_at,
                a.unit_id,
                u.unit_code,
                u.unit_name
            FROM student_submissions ss
            JOIN assignments a ON a.assignment_id = ss.assignment_id
            JOIN units u ON u.unit_id = a.unit_id
            WHERE ss.submission_id = ?
        """, (data['run']['submission_id'],)).fetchone()

        review = conn.execute("""
            SELECT review_id, reviewed_at
            FROM human_reviews
            WHERE generation_id = ?
            ORDER BY reviewed_at DESC, review_id DESC
            LIMIT 1
        """, (generation_id,)).fetchone()

        educator = conn.execute("""
            SELECT t.full_name, t.email
            FROM unit_tutors ut
            JOIN tutors t ON t.tutor_id = ut.tutor_id
            WHERE ut.unit_id = ?
            LIMIT 1
        """, (submission['unit_id'] if submission else None,)).fetchone() if submission else None

    run = dict(data['run'])
    if submission:
        run['submission_text'] = submission['cleaned_text']
        run['submitted_at'] = submission['submitted_at']
        run['unit_id'] = submission['unit_id']
        run['unit_code'] = submission['unit_code']
        run['unit_name'] = submission['unit_name']
    if educator:
        run['educator_name'] = educator['full_name']
        run['educator_email'] = educator['email']
    if review:
        run['review_status'] = 'reviewed'
        run['reviewed_at'] = review['reviewed_at']
    elif run.get('status') == 'completed':
        run['review_status'] = 'ai_generated'
    else:
        run['review_status'] = 'pending'

    overall = dict(data.get('overall_feedback') or {})
    if overall:
        overall['key_strengths'] = parse_json_text_list(overall.get('key_strengths'))
        overall['priority_improvements'] = parse_json_text_list(overall.get('priority_improvements'))

    criteria = [dict(c) for c in (data.get('criterion_feedback') or [])]

    return jsonify({
        'run': run,
        'overall_feedback': overall,
        'criterion_feedback': criteria,
    })


@app.route('/api/lead/units')
def lead_units_list():
    user, error = api_session_user(required_role='lead_lecturer')
    if error:
        return error

    with connect_db() as conn:
        rows = conn.execute("""
            SELECT
                u.unit_id, u.unit_code, u.unit_name, u.semester, u.year,
                COUNT(DISTINCT ut.tutor_id) AS educator_count,
                COUNT(DISTINCT a.assignment_id) AS task_count
            FROM units u
            LEFT JOIN unit_tutors ut ON ut.unit_id = u.unit_id
            LEFT JOIN assignments a ON a.unit_id = u.unit_id
            GROUP BY u.unit_id
            ORDER BY u.unit_code
        """).fetchall()

    return jsonify({'units': [dict(r) for r in rows]})


@app.route('/api/lead/unit/<int:unit_id>')
def lead_unit_detail(unit_id):
    user, error = api_session_user(required_role='lead_lecturer')
    if error:
        return error

    with connect_db() as conn:
        unit = conn.execute("""
            SELECT unit_id, unit_code, unit_name, semester, year
            FROM units WHERE unit_id = ?
        """, (unit_id,)).fetchone()

        if unit is None:
            return jsonify({'error': 'Unit not found'}), 404

        assignments = conn.execute("""
            SELECT
                a.assignment_id,
                a.assignment_name,
                a.assignment_type,
                a.due_date,
                (SELECT COUNT(*) FROM assignment_specs s WHERE s.assignment_id = a.assignment_id)
                + (SELECT COUNT(*) FROM unit_materials m WHERE m.assignment_id = a.assignment_id)
                AS file_count,
                (SELECT COUNT(DISTINCT ss.submission_id) FROM student_submissions ss WHERE ss.assignment_id = a.assignment_id)
                AS submission_count
            FROM assignments a
            WHERE a.unit_id = ?
            ORDER BY a.assignment_id
        """, (unit_id,)).fetchall()

        educators = conn.execute("""
            SELECT t.tutor_id, t.full_name, t.email
            FROM unit_tutors ut
            JOIN tutors t ON t.tutor_id = ut.tutor_id
            WHERE ut.unit_id = ?
        """, (unit_id,)).fetchall()

        # scoping notes = unit-level materials (assignment_id IS NULL)
        scoping_notes = conn.execute("""
            SELECT
                material_id,
                title,
                material_type,
                source_file_path,
                created_at
            FROM unit_materials
            WHERE unit_id = ? AND assignment_id IS NULL
            ORDER BY material_id
        """, (unit_id,)).fetchall()

    return jsonify({
        'unit': dict(unit),
        'assignments': [dict(a) for a in assignments],
        'educators': [dict(e) for e in educators],
        'scoping_notes': [dict(n) for n in scoping_notes],
    })

@app.route('/api/feedback/<int:generation_id>')
def get_feedback(generation_id):
    user, error = _staff_api_user()
    if error:
        return error

    with connect_db() as conn:
        authorised = fetch_authorised_generation(
            conn,
            generation_id,
            user['user_id'],
            user.get('tutor_id'),
            allow_admin_view=True,
        )
        if authorised is None:
            return jsonify({'error': 'Generation not found or not authorised'}), 404
        try:
            data = fetch_generation_review(conn, generation_id)
        except ValueError as err:
            return jsonify({'error': str(err)}), 404
        submission = conn.execute(
            """
            SELECT ss.cleaned_text, ss.submitted_at, a.unit_id
            FROM student_submissions ss
            JOIN assignments a ON a.assignment_id = ss.assignment_id
            WHERE ss.submission_id = ?
            """,
            (data['run']['submission_id'],)
        ).fetchone()
        review = conn.execute(
            """
            SELECT review_id
            FROM human_reviews
            WHERE generation_id = ?
            ORDER BY reviewed_at DESC, review_id DESC
            LIMIT 1
            """,
            (generation_id,),
        ).fetchone()
        current_provider, current_model = assignment_feedback_model(
            conn,
            int(data['run']['assignment_id']),
        )
        previous_generation = conn.execute(
            """
            SELECT generation_id, llm_provider, llm_model, completed_at
            FROM generation_runs
            WHERE submission_id = ?
              AND generation_id < ?
              AND status = 'completed'
            ORDER BY generation_id DESC
            LIMIT 1
            """,
            (data['run']['submission_id'], generation_id),
        ).fetchone()
        workflow = generation_workflow(conn, authorised)
    run = dict(data['run'])
    overall = dict(data['overall_feedback']) if data['overall_feedback'] else {}
    criteria = [dict(r) for r in data['criterion_feedback']]
    overall['key_strengths'] = parse_json_text_list(overall.get('key_strengths'))
    overall['priority_improvements'] = parse_json_text_list(overall.get('priority_improvements'))
    if submission:
        run['submission_text'] = submission['cleaned_text']
        run['submitted_at'] = submission['submitted_at']
        run['unit_id'] = submission['unit_id']
    else:
        run['submission_text'] = ''
        run['submitted_at'] = None
        run['unit_id'] = None
    if review:
        run['review_status'] = 'reviewed'
    elif run.get('status') == 'completed':
        run['review_status'] = 'ai_generated'
    else:
        run['review_status'] = 'pending'
    run['assignment_default_provider'] = current_provider
    run['assignment_default_model'] = current_model
    run['uses_current_assignment_model'] = bool(
        run.get('llm_provider') == current_provider
        and run.get('llm_model') == current_model
    )
    run['is_current_generation'] = bool(
        workflow is None
        or workflow['current_generation_id'] is None
        or int(workflow['current_generation_id']) == generation_id
    )
    run['can_regenerate_all'] = bool(
        run['is_current_generation']
        and not (
            workflow is not None
            and workflow['marking_status'] == 'marker_confirmed'
        )
    )
    return jsonify({
        'run': run,
        'overall_feedback': overall,
        'criterion_feedback': criteria,
        'previous_generation': (
            dict(previous_generation) if previous_generation else None
        ),
    })

@app.route('/api/feedback/generate', methods=['POST'])
def generate_feedback():
    user, error = _staff_api_user()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    try:
        submission_id = int(data.get('submission_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'submission_id is required and must be an integer'}), 400

    try:
        per_cue_top_k = _coerce_optional_int(
            data.get('per_cue_top_k', data.get('top_k')),
            'per_cue_top_k',
        )
        max_final_chunks = _coerce_optional_int(
            data.get('max_final_chunks'),
            'max_final_chunks',
        )
        temperature = _coerce_optional_float(data.get('temperature'), 'temperature')
    except ValueError as err:
        return jsonify({'error': str(err)}), 400

    context_mode = data.get('context_mode') or data.get('mode') or DEFAULT_FEEDBACK_GENERATION_MODE
    retrieval_strategy = data.get('retrieval_strategy') or data.get('strategy')
    if retrieval_strategy is None and context_mode == DEFAULT_FEEDBACK_GENERATION_MODE:
        retrieval_strategy = DEFAULT_FEEDBACK_GENERATION_STRATEGY
    prompt_template_version = data.get('prompt_template_version') or data.get('prompt')
    if prompt_template_version is None and context_mode == DEFAULT_FEEDBACK_GENERATION_MODE:
        prompt_template_version = DEFAULT_RETRIEVAL_PROMPT_TEMPLATE
    feedback_modifier_mode, feedback_length, feedback_tone = (
        _resolve_feedback_modifier_payload(data)
    )

    with connect_db() as conn:
        submission = fetch_authorised_submission(
            conn,
            submission_id,
            user['user_id'],
            user.get('tutor_id'),
        )
        if submission is None:
            return jsonify({'error': 'Submission not found or not authorised'}), 404

        if "submission_attempt_id" in submission.keys():
            workflow = conn.execute(
                """
                SELECT marking_status
                FROM submission_workflow_states
                WHERE submission_attempt_id = ?
                """,
                (submission["submission_attempt_id"],),
            ).fetchone()
            if workflow is not None and workflow["marking_status"] == "marker_confirmed":
                return jsonify({
                    'error': (
                        'Reviewed feedback must be returned by a Unit Admin '
                        'before it can be regenerated.'
                    )
                }), 409

        provider, model = assignment_feedback_model(
            conn,
            int(submission["assignment_id"]),
        )
        generation_floor = conn.execute(
            """
            SELECT COALESCE(MAX(generation_id), 0)
            FROM generation_runs
            WHERE submission_id = ?
            """,
            (submission_id,),
        ).fetchone()[0]

        try:
            result, gate_report = generate_feedback_with_quality_gate(
                conn,
                submission_id=submission_id,
                provider=provider,
                model=model,
                per_cue_top_k=per_cue_top_k,
                max_final_chunks=max_final_chunks,
                temperature=temperature if temperature is not None else 0.2,
                prompt_template_version=prompt_template_version,
                context_mode=context_mode,
                retrieval_strategy=retrieval_strategy,
                feedback_modifier_mode=feedback_modifier_mode,
                feedback_length=feedback_length,
                feedback_tone=feedback_tone,
            )
            # Judge results are internal evaluation data and are not returned
            # to the Staff workspace.
            app.logger.info("quality gate: %s", gate_report)
        except Exception as err:
            failed = conn.execute(
                """
                SELECT
                    generation_id,
                    llm_provider,
                    llm_model,
                    error_message,
                    provider_error_code,
                    provider_http_status,
                    provider_request_id,
                    completed_at
                FROM generation_runs
                WHERE submission_id = ?
                  AND generation_id > ?
                  AND status = 'failed'
                ORDER BY generation_id DESC
                LIMIT 1
                """,
                (submission_id, generation_floor),
            ).fetchone()
            if failed is not None:
                return jsonify({
                    'error': failed['error_message'] or str(err),
                    'generation_error': {
                        'generation_id': failed['generation_id'],
                        'provider': failed['llm_provider'],
                        'model': failed['llm_model'],
                        'code': failed['provider_error_code'],
                        'http_status': failed['provider_http_status'],
                        'request_id': failed['provider_request_id'],
                        'message': failed['error_message'] or str(err),
                        'occurred_at': failed['completed_at'],
                    },
                }), 502
            if isinstance(err, ValueError):
                return jsonify({'error': str(err)}), 400
            return jsonify({'error': str(err)}), 502

        record_generated_feedback(
            conn,
            submission,
            user['user_id'],
            result.generation_id,
        )

    return jsonify({
        'status': 'ok',
        'generation_id': result.generation_id,
        'submission_id': submission_id,
        'overall_grade_band': result.overall_grade_band,
        'criterion_count': result.criterion_count,
        'retrieval_cue_count': result.retrieval_cue_count,
        'deduplicated_chunk_count': result.deduplicated_chunk_count,
        'provider': result.provider,
        'model': result.model,
        'context_mode': result.context_mode,
        'pipeline_version': result.pipeline_version,
        'prompt_template_version': result.prompt_template_version,
        'retrieval_strategy': result.retrieval_strategy,
        'per_cue_top_k': result.per_cue_top_k,
        'max_final_chunks': result.max_final_chunks,
        'feedback_modifier_mode': result.feedback_modifier_mode,
        'feedback_length': result.feedback_length,
        'feedback_tone': result.feedback_tone,
    })


@app.route(
    '/api/feedback/<int:generation_id>/criterion/<int:criterion_id>/regenerate',
    methods=['POST'],
)
def regenerate_criterion_feedback(generation_id, criterion_id):
    user, error = _staff_api_user()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    feedback_modifier_mode, feedback_length, feedback_tone = (
        _resolve_feedback_modifier_payload(data)
    )
    with connect_db() as conn:
        generation = fetch_authorised_generation(
            conn,
            generation_id,
            user['user_id'],
            user.get('tutor_id'),
        )
        if generation is None:
            return jsonify({'error': 'Generation not found or not authorised'}), 404
        workflow = generation_workflow(conn, generation)
        if (
            workflow is not None
            and workflow['current_generation_id'] is not None
            and int(workflow['current_generation_id']) != generation_id
        ):
            return jsonify({
                'error': 'Previous generated versions are read-only.'
            }), 409

        try:
            criterion_feedback = regenerate_feedback_for_criterion(
                conn,
                generation_id=generation_id,
                criterion_id=criterion_id,
                feedback_modifier_mode=feedback_modifier_mode,
                feedback_length=feedback_length,
                feedback_tone=feedback_tone,
            )
        except ValueError as err:
            return jsonify({'error': str(err)}), 400
        except RuntimeError as err:
            return jsonify({'error': str(err)}), 502

    return jsonify({
        'status': 'ok',
        'generation_id': generation_id,
        'criterion_id': criterion_id,
        'criterion_feedback': criterion_feedback,
        'feedback_modifier_mode': feedback_modifier_mode,
        'feedback_length': feedback_length,
        'feedback_tone': feedback_tone,
    })


@app.route('/api/feedback/<int:generation_id>/save', methods=['POST'])
def save_feedback(generation_id):
    user, error = _staff_api_user()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    with connect_db() as conn:
        generation = fetch_authorised_generation(
            conn,
            generation_id,
            user['user_id'],
            user.get('tutor_id'),
        )
        if generation is None:
            return jsonify({'error': 'Generation not found or not authorised'}), 404
        workflow = generation_workflow(conn, generation)
        if (
            workflow is not None
            and workflow['current_generation_id'] is not None
            and int(workflow['current_generation_id']) != generation_id
        ):
            return jsonify({
                'error': 'Previous generated versions are read-only.'
            }), 409
        for item in data.get('criteria', []):
            conn.execute("""
                UPDATE criterion_feedback
                SET strengths=COALESCE(?, strengths),
                    areas_for_improvement=COALESCE(?, areas_for_improvement),
                    improvement_suggestion=COALESCE(?, improvement_suggestion),
                    mark=COALESCE(?, mark)
                WHERE generation_id=? AND criterion_id=?
            """, (
                item.get('strengths'),
                item.get('weaknesses'),
                item.get('suggestions'),
                item.get('mark'),
                generation_id,
                item.get('criterion_id')
            ))
        overall_comment = data.get('overall_comment')
        final_mark = data.get('final_mark')
        if overall_comment is not None or final_mark is not None:
            conn.execute("""
                UPDATE overall_feedback
                SET overall_comment=COALESCE(?, overall_comment),
                    final_mark=COALESCE(?, final_mark)
                WHERE generation_id=?
            """, (overall_comment, final_mark, generation_id))

        review_status = data.get('review_status') or data.get('status')

        if (
            workflow is not None
            and workflow['marking_status'] == 'marker_confirmed'
            and review_status in ('pending', 'ai_generated')
        ):
            return jsonify({
                'error': 'Confirmed feedback must be returned by a Unit Admin before editing.'
            }), 409

        if review_status == 'reviewed' and user.get('tutor_id') is not None:
            review = conn.execute(
                """
                SELECT review_id
                FROM human_reviews
                WHERE generation_id=? AND tutor_id=? AND review_type='tutor_review'
                ORDER BY reviewed_at DESC, review_id DESC
                LIMIT 1
                """,
                (generation_id, user['tutor_id']),
            ).fetchone()
            if review:
                conn.execute(
                    """
                    UPDATE human_reviews
                    SET approved=1, comments=?, reviewed_at=CURRENT_TIMESTAMP
                    WHERE review_id=?
                    """,
                    (overall_comment, review['review_id']),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO human_reviews
                        (generation_id, tutor_id, review_type, approved, comments)
                    VALUES (?, ?, 'tutor_review', 1, ?)
                    """,
                    (generation_id, user['tutor_id'], overall_comment),
                )
        elif review_status in ('pending', 'ai_generated') and user.get('tutor_id') is not None:
            conn.execute(
                """
                DELETE FROM human_reviews
                WHERE generation_id=? AND tutor_id=? AND review_type='tutor_review'
                """,
                (generation_id, user['tutor_id']),
            )

        update_feedback_workflow(
            conn,
            generation,
            user['user_id'],
            review_status,
        )

        conn.commit()
    return jsonify({'status': 'ok', 'review_status': review_status})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip()
        password = request.form['password']
        with connect_db() as conn:
            user = conn.execute(
                """
                SELECT
                    user_id, email, password_hash, role,
                    session_version, account_status
                FROM users
                WHERE lower(email)=lower(?)
                """,
                (email,),
            ).fetchone()
            has_admin_scope = False
            if user is not None:
                has_admin_scope = conn.execute(
                    """
                    SELECT 1
                    FROM organization_role_assignments
                    WHERE user_id = ? AND active = 1
                    UNION ALL
                    SELECT 1
                    FROM unit_role_assignments
                    WHERE user_id = ?
                      AND role = 'unit_admin'
                      AND active = 1
                    LIMIT 1
                    """,
                    (user["user_id"], user["user_id"]),
                ).fetchone() is not None
        if (
            user
            and user['account_status'] == 'active'
            and check_password_hash(user['password_hash'], password)
        ):
            session.clear()
            session['user_id'] = user['user_id']
            session['email'] = user['email']
            session['role'] = user['role']
            session['session_version'] = user['session_version']
            if has_admin_scope:
                return redirect('/admin/units')
            elif user['role'] == 'educator':
                return redirect('/educator')
            elif user['role'] == 'student':
                return redirect('/student')
        return render_template("login.html", error="Invalid credentials")
    return render_template('login.html')

if __name__ == '__main__':
    if os.environ.get(
        "FEEDBACK_LENS_START_WORKER",
        "1",
    ) not in {"0", "false", "False"}:
        threading.Thread(
            target=run_worker_forever,
            name="feedback-lens-worker",
            daemon=True,
        ).start()
    app.run(debug=True, port=5001, use_reloader=False)
