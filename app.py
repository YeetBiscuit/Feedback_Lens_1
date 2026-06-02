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
            "SELECT cleaned_text FROM student_submissions WHERE submission_id=?",
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
    run['submission_text'] = submission['cleaned_text'] if submission else ''
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
                SET strengths=?, areas_for_improvement=?, improvement_suggestion=?, mark=?
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
        status = data.get('status')
        review_status = data.get('review_status') or status
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
