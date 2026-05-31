from flask import Flask, render_template, request, redirect, session, jsonify
import hashlib, sqlite3, json
from feedback_lens.db.connection import connect_db
from feedback_lens.feedback.review import fetch_generation_review, parse_json_text_list

app = Flask(__name__)
app.secret_key = "dev_secret_key"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
def admin():
    if session.get('role') != 'admin':
        return redirect('/login')
    return render_template("admin.html")

@app.route('/leadLecture')
def leadlecture():
    if session.get('role') != 'lead_lecturer':
        return redirect('/login')
    return render_template("leadLecturer.html")

# @app.route('/educator')
# def educator():
#     if session.get('role') != 'educator':
#         return redirect('/login')
#     return render_template("educator.html")

@app.route('/educator')
def educator():
    return render_template("educator.html")

@app.route('/student')
def student():
    if session.get('role') != 'student':
        return redirect('/login')
    return render_template("student.html")

@app.route('/educator/feedback-review')
def feedback_review():
    return render_template('feedback_review.html')

@app.route('/educator/general-feedback')
def general_feedback():
    return render_template('general_feedback.html')

@app.route('/api/educator/dashboard')
def educator_dashboard_data():
    email = session.get('email', 'educator@a.com')
    with connect_db() as conn:
        user = conn.execute(
            "SELECT id, email, role, name FROM users WHERE email=?", (email,)
        ).fetchone()
        if user is None:
            return jsonify({'error': 'User not found'}), 404
        units = conn.execute("""
            SELECT u.unit_id, u.unit_code, u.unit_name, u.semester, u.year,
                   COUNT(DISTINCT ss.submission_id) as student_count,
                   COUNT(DISTINCT CASE WHEN gr.status='completed' THEN gr.generation_id END) as completed_count,
                   COUNT(DISTINCT CASE WHEN gr.status='running' OR gr.status IS NULL THEN ss.submission_id END) as pending_count
            FROM units u
            JOIN unit_tutors ut ON ut.unit_id = u.unit_id
            LEFT JOIN assignments a ON a.unit_id = u.unit_id
            LEFT JOIN student_submissions ss ON ss.assignment_id = a.assignment_id
            LEFT JOIN generation_runs gr ON gr.submission_id = ss.submission_id
            WHERE ut.tutor_id = ?
            GROUP BY u.unit_id
        """, (user['id'],)).fetchall()
    return jsonify({
        'user': {'name': user['name'] or user['email'], 'role': user['role']},
        'units': [dict(u) for u in units]
    })

@app.route('/api/feedback/<int:generation_id>')
def get_feedback(generation_id):
    with connect_db() as conn:
        data = fetch_generation_review(conn, generation_id)
        submission = conn.execute(
            "SELECT cleaned_text, status FROM student_submissions WHERE submission_id=?",
            (data['run']['submission_id'],)
        ).fetchone()
    run = dict(data['run'])
    overall = dict(data['overall_feedback']) if data['overall_feedback'] else {}
    criteria = [dict(r) for r in data['criterion_feedback']]
    with connect_db() as conn2:
        for c in criteria:
            row = conn2.execute(
                "SELECT mark FROM criterion_feedback WHERE generation_id=? AND criterion_id=?",
                (generation_id, c['criterion_id'])
            ).fetchone()
            c['mark'] = row['mark'] if row and row['mark'] is not None else None    
    overall['key_strengths'] = parse_json_text_list(overall.get('key_strengths'))
    overall['priority_improvements'] = parse_json_text_list(overall.get('priority_improvements'))
    run['submission_text'] = submission['cleaned_text'] if submission else ''
    run['status'] = submission['status'] if submission else 'pending'
    return jsonify({
        'run': run,
        'overall_feedback': overall,
        'criterion_feedback': criteria,
    })

@app.route('/api/feedback/<int:generation_id>/save', methods=['POST'])
def save_feedback(generation_id):
    data = request.get_json()
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
        if status:
            conn.execute("""
                UPDATE student_submissions
                SET status=?
                WHERE submission_id=(
                    SELECT submission_id FROM generation_runs WHERE generation_id=?
                )
            """, (status, generation_id))
        conn.commit()
    return jsonify({'status': 'ok'})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = hash_password(request.form['password'])
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role FROM users WHERE email=? AND password=?",
            (email, password)
        )
        user = cursor.fetchone()
        if user:
            session['email'] = email
            session['role'] = user[0]
            if user[0] == 'admin':
                return redirect('/admin')
            elif user[0] == 'lead_lecturer':
                return redirect('/leadLecture')
            elif user[0] == 'educator':
                return redirect('/educator')
            elif user[0] == 'student':
                return redirect('/student')
        return render_template("login.html", error="Invalid credentials")
    return render_template('login.html')

def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()
def get_db(): return sqlite3.connect("feedback_system.db")

if __name__ == '__main__':
    app.run(debug=True, port=5001)