from functools import wraps

from flask import Flask, render_template, request, redirect, session, jsonify
from werkzeug.security import check_password_hash

from feedback_lens.db.connection import connect_db
from feedback_lens.feedback.pipeline import (
    DEFAULT_FEEDBACK_PROVIDER,
    generate_feedback_for_submission,
    regenerate_feedback_for_criterion,
)
from feedback_lens.feedback.prompt import DEFAULT_FEEDBACK_MODIFIER_MODE
from feedback_lens.feedback.review import fetch_generation_review, parse_json_text_list

app = Flask(__name__)
app.secret_key = "dev_secret_key"
DEFAULT_FEEDBACK_GENERATION_MODE = "retrieval"
DEFAULT_FEEDBACK_GENERATION_STRATEGY = "planned"
DEFAULT_RETRIEVAL_PROMPT_TEMPLATE = "unit-grounded-v2"


def login_required(role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get("role") != role:
                return redirect("/login")
            return view(*args, **kwargs)

        return wrapped

    return decorator


def fetch_session_user(conn):
    user_id = session.get("user_id")
    email = session.get("email")
    if user_id is None and email is None:
        return None

    if user_id is not None:
        return conn.execute(
            """
            SELECT
                u.user_id,
                u.email,
                u.role,
                u.display_name,
                u.tutor_id,
                t.full_name AS tutor_full_name
            FROM users AS u
            LEFT JOIN tutors AS t ON t.tutor_id = u.tutor_id
            WHERE u.user_id = ?
            """,
            (user_id,),
        ).fetchone()

    return conn.execute(
        """
        SELECT
            u.user_id,
            u.email,
            u.role,
            u.display_name,
            u.tutor_id,
            t.full_name AS tutor_full_name
        FROM users AS u
        LEFT JOIN tutors AS t ON t.tutor_id = u.tutor_id
        WHERE lower(u.email) = lower(?)
        """,
        (email,),
    ).fetchone()


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


def _fetch_authorised_submission(conn, submission_id, tutor_id):
    return conn.execute(
        """
        SELECT
            ss.submission_id,
            ss.assignment_id,
            a.unit_id
        FROM student_submissions AS ss
        JOIN assignments AS a ON a.assignment_id = ss.assignment_id
        JOIN unit_tutors AS ut ON ut.unit_id = a.unit_id
        WHERE ss.submission_id = ?
          AND ut.tutor_id = ?
        """,
        (submission_id, tutor_id),
    ).fetchone()


def _fetch_authorised_generation(conn, generation_id, tutor_id):
    return conn.execute(
        """
        SELECT
            gr.generation_id,
            gr.submission_id,
            gr.assignment_id,
            a.unit_id
        FROM generation_runs AS gr
        JOIN assignments AS a ON a.assignment_id = gr.assignment_id
        JOIN unit_tutors AS ut ON ut.unit_id = a.unit_id
        WHERE gr.generation_id = ?
          AND ut.tutor_id = ?
        """,
        (generation_id, tutor_id),
    ).fetchone()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
@login_required('admin')
def admin():
    return render_template("admin.html")

@app.route('/leadLecture')
@login_required('lead_lecturer')
def lead_lecture():
    return render_template("lead_dashboard.html")


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
@login_required('educator')
def educator():
    return render_template("educator.html")

@app.route('/student')
@login_required('student')
def student():
    return render_template("student.html")

@app.route('/educator/feedback-review')
@login_required('educator')
def feedback_review():
    return render_template('feedback_review.html')

@app.route('/educator/general-feedback')
@login_required('educator')
def general_feedback():
    return render_template('general_feedback.html')



@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.route('/educator/unit/<int:unit_id>')
@login_required('educator')
def unit_dashboard(unit_id):
    return render_template('unit_dashboard.html', unit_id=unit_id)


@app.route('/educator/unit/<int:unit_id>/ai-performance')
@login_required('educator')
def unit_ai_performance(unit_id):
    return render_template('ai_performance.html', unit_id=unit_id)

@app.route('/educator/unit/<int:unit_id>/export')
@login_required('educator')
def unit_export(unit_id):
    return render_template('export.html', unit_id=unit_id)

@app.route('/api/educator/unit/<int:unit_id>/dashboard')
def unit_dashboard_data(unit_id):
    user, error = api_session_user(required_role='educator')
    if error:
        return error
    if user.get('tutor_id') is None:
        return jsonify({'error': 'Educator account is not linked to a tutor'}), 403

    with connect_db() as conn:
        unit = conn.execute(
            """
            SELECT u.unit_id, u.unit_code, u.unit_name, u.semester, u.year
            FROM units u
            JOIN unit_tutors ut ON ut.unit_id = u.unit_id
            WHERE u.unit_id = ? AND ut.tutor_id = ?
            """,
            (unit_id, user['tutor_id']),
        ).fetchone()
        if unit is None:
            return jsonify({'error': 'Unit not found or not authorised'}), 404

        
        counts = conn.execute(
            """
            SELECT
                COUNT(DISTINCT ss.submission_id) AS total_submissions,
                COUNT(DISTINCT CASE WHEN gr.status = 'completed' AND hr.review_id IS NULL THEN ss.submission_id END) AS ai_generated_count,
                COUNT(DISTINCT CASE WHEN hr.review_id IS NOT NULL THEN ss.submission_id END) AS reviewed_count,
                COUNT(DISTINCT CASE WHEN gr.generation_id IS NULL OR gr.status != 'completed' THEN ss.submission_id END) AS pending_count
            FROM student_submissions ss
            JOIN assignments a ON a.assignment_id = ss.assignment_id
            LEFT JOIN generation_runs gr
                ON gr.submission_id = ss.submission_id
                AND gr.generation_id = (
                    SELECT MAX(generation_id)
                    FROM generation_runs
                    WHERE submission_id = ss.submission_id
                )
            LEFT JOIN human_reviews hr ON hr.generation_id = gr.generation_id
            WHERE a.unit_id = ?
            """,
            (unit_id,),
        ).fetchone()


    return jsonify({
        'unit': dict(unit),
        'counts': dict(counts) if counts else {},
    })

@app.route('/educator/unit/<int:unit_id>/submissions')
@login_required('educator')
def submissions_list(unit_id):
    return render_template('submissions_list.html', unit_id=unit_id)


@app.route('/api/educator/unit/<int:unit_id>/submissions')
def unit_submissions_data(unit_id):
    user, error = api_session_user(required_role='educator')
    if error:
        return error
    if user.get('tutor_id') is None:
        return jsonify({'error': 'Educator account is not linked to a tutor'}), 403

    with connect_db() as conn:
        unit = conn.execute(
            """
            SELECT u.unit_id, u.unit_code, u.unit_name, u.semester, u.year
            FROM units u
            JOIN unit_tutors ut ON ut.unit_id = u.unit_id
            WHERE u.unit_id = ? AND ut.tutor_id = ?
            """,
            (unit_id, user['tutor_id']),
        ).fetchone()
        if unit is None:
            return jsonify({'error': 'Unit not found or not authorised'}), 404

        rows = conn.execute(
            """
            SELECT
                ss.submission_id,
                ss.student_identifier,
                ss.submitted_at,
                a.assignment_name,
                a.assignment_id,
                gr.generation_id,
                gr.status AS generation_status,
                of.overall_grade_band,
                of.final_mark,
                CASE
                    WHEN hr.review_id IS NOT NULL THEN 'reviewed'
                    WHEN gr.status = 'completed' THEN 'ai_generated'
                    ELSE 'pending'
                END AS review_status
            FROM student_submissions ss
            JOIN assignments a ON a.assignment_id = ss.assignment_id
            LEFT JOIN generation_runs gr
                ON gr.submission_id = ss.submission_id
                AND gr.generation_id = (
                    SELECT MAX(generation_id)
                    FROM generation_runs
                    WHERE submission_id = ss.submission_id
                    AND status = 'completed'
                )
            LEFT JOIN overall_feedback of ON of.generation_id = gr.generation_id
            LEFT JOIN human_reviews hr ON hr.generation_id = gr.generation_id
            WHERE a.unit_id = ?
            ORDER BY ss.submission_id
            """,
            (unit_id,),
        ).fetchall()

    return jsonify({
        'unit': dict(unit),
        'submissions': [dict(r) for r in rows],
    })

@app.route('/api/educator/dashboard')
def educator_dashboard_data():
    user, error = api_session_user(required_role='educator')
    if error:
        return error
    if user.get('tutor_id') is None:
        return jsonify({'error': 'Educator account is not linked to a tutor'}), 403

    with connect_db() as conn:
        units = conn.execute("""
            SELECT u.unit_id, u.unit_code, u.unit_name, u.semester, u.year,
                   COUNT(DISTINCT ss.submission_id) as student_count,
                   COUNT(DISTINCT CASE WHEN gr.status='completed' THEN ss.submission_id END) as completed_count,
                   COUNT(DISTINCT ss.submission_id)
                     - COUNT(DISTINCT CASE WHEN gr.status='completed' THEN ss.submission_id END) as pending_count
            FROM units u
            JOIN unit_tutors ut ON ut.unit_id = u.unit_id
            LEFT JOIN assignments a ON a.unit_id = u.unit_id
            LEFT JOIN student_submissions ss ON ss.assignment_id = a.assignment_id
            LEFT JOIN generation_runs gr ON gr.submission_id = ss.submission_id
            WHERE ut.tutor_id = ?
            GROUP BY u.unit_id
        """, (user['tutor_id'],)).fetchall()
    return jsonify({
        'user': {
            'name': user.get('display_name') or user.get('tutor_full_name') or user['email'],
            'role': user['role'],
        },
        'units': [dict(u) for u in units]
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

    return jsonify({'submissions': [dict(r) for r in rows]})


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
    user, error = api_session_user(required_role='educator')
    if error:
        return error

    with connect_db() as conn:
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
    return jsonify({
        'run': run,
        'overall_feedback': overall,
        'criterion_feedback': criteria,
    })

@app.route('/api/feedback/generate', methods=['POST'])
def generate_feedback():
    user, error = api_session_user(required_role='educator')
    if error:
        return error
    if user.get('tutor_id') is None:
        return jsonify({'error': 'Educator account is not linked to a tutor'}), 403

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
        submission = _fetch_authorised_submission(
            conn,
            submission_id,
            user['tutor_id'],
        )
        if submission is None:
            return jsonify({'error': 'Submission not found or not authorised'}), 404

        try:
            result = generate_feedback_for_submission(
                conn,
                submission_id=submission_id,
                provider=data.get('provider') or DEFAULT_FEEDBACK_PROVIDER,
                model=data.get('model'),
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
        except ValueError as err:
            return jsonify({'error': str(err)}), 400
        except RuntimeError as err:
            return jsonify({'error': str(err)}), 502

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
    user, error = api_session_user(required_role='educator')
    if error:
        return error
    if user.get('tutor_id') is None:
        return jsonify({'error': 'Educator account is not linked to a tutor'}), 403

    data = request.get_json(silent=True) or {}
    feedback_modifier_mode, feedback_length, feedback_tone = (
        _resolve_feedback_modifier_payload(data)
    )
    with connect_db() as conn:
        generation = _fetch_authorised_generation(
            conn,
            generation_id,
            user['tutor_id'],
        )
        if generation is None:
            return jsonify({'error': 'Generation not found or not authorised'}), 404

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
    user, error = api_session_user(required_role='educator')
    if error:
        return error
    if user.get('tutor_id') is None:
        return jsonify({'error': 'Educator account is not linked to a tutor'}), 403

    data = request.get_json(silent=True) or {}
    with connect_db() as conn:
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

        if review_status == 'reviewed':
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
        elif review_status in ('pending', 'ai_generated'):
            conn.execute(
                """
                DELETE FROM human_reviews
                WHERE generation_id=? AND tutor_id=? AND review_type='tutor_review'
                """,
                (generation_id, user['tutor_id']),
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
                SELECT user_id, email, password_hash, role
                FROM users
                WHERE lower(email)=lower(?)
                """,
                (email,),
            ).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session.clear()
            session['user_id'] = user['user_id']
            session['email'] = user['email']
            session['role'] = user['role']
            if user['role'] == 'admin':
                return redirect('/admin')
            elif user['role'] == 'lead_lecturer':
                return redirect('/leadLecture')
            elif user['role'] == 'educator':
                return redirect('/educator')
            elif user['role'] == 'student':
                return redirect('/student')
        return render_template("login.html", error="Invalid credentials")
    return render_template('login.html')

if __name__ == '__main__':
    app.run(debug=True, port=5001)
