from functools import wraps

from flask import Flask, render_template, request, redirect, session, jsonify
from werkzeug.security import check_password_hash

from feedback_lens.db.connection import connect_db
from feedback_lens.feedback.review import fetch_generation_review, parse_json_text_list

app = Flask(__name__)
app.secret_key = "dev_secret_key"


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

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
@login_required('admin')
def admin():
    return render_template("admin.html")

@app.route('/leadLecture')
@login_required('lead_lecturer')
def leadlecture():
    return render_template("leadLecturer.html")

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
