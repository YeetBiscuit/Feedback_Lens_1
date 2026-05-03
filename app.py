from flask import Flask, render_template
import hashlib, sqlite3; 
from flask import request, redirect, session
import anthropic, json
from flask import jsonify
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

@app.route('/educator')
def educator():
    if session.get('role') != 'educator':
        return redirect('/login')
    return render_template("educator.html")

@app.route('/student')
def student():
    if session.get('role') != 'student':
        return redirect('/login')
    return render_template("student.html")

# @app.route('/educator/feedback-review')
# def feedback_review():
#     if session.get('role') != 'educator':
#         return redirect('/login')
#     return render_template('feedback_review.html')

@app.route('/educator/feedback-review')
def feedback_review():
    return render_template('feedback_review.html')

@app.route('/api/feedback/<int:generation_id>')
def get_feedback(generation_id):
    with connect_db() as conn:
        data = fetch_generation_review(conn, generation_id)
# @app.route('/api/feedback/<int:generation_id>')
# def get_feedback(generation_id):
#     with connect_db() as conn:
#         data = fetch_generation_review(conn, generation_id)
    run = dict(data['run'])
    overall = dict(data['overall_feedback']) if data['overall_feedback'] else {}
    criteria = [dict(r) for r in data['criterion_feedback']]
    overall['key_strengths'] = parse_json_text_list(overall.get('key_strengths'))
    overall['priority_improvements'] = parse_json_text_list(overall.get('priority_improvements'))
    from flask import jsonify
    return jsonify({
        'run': run,
        'overall_feedback': overall,
        'criterion_feedback': criteria,
    })

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