from flask import Flask, render_template, request, redirect, session, jsonify
import sqlite3

app = Flask(__name__)
app.secret_key = "secret123"

# ---------------- LOGIN ----------------
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        if request.form['username'] == 'admin' and request.form['password'] == 'admin':
            session['user'] = 'admin'
            return redirect('/')
    return render_template('login.html')


# ---------------- LOGOUT ----------------
@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/login')


# ---------------- DASHBOARD ----------------
@app.route('/')
def index():
    if 'user' not in session:
        return redirect('/login')

    conn = sqlite3.connect('database.db')
    c = conn.cursor()

    c.execute("SELECT * FROM devices")
    devices = c.fetchall()

    conn.close()

    return render_template("index.html", devices=devices)


# ---------------- ADD DEVICE ----------------
@app.route('/add', methods=['POST'])
def add_device():
    name = request.form['name']
    ip = request.form['ip']

    conn = sqlite3.connect('database.db')
    c = conn.cursor()

    c.execute("""
        INSERT INTO devices (name, ip, status, last_checked)
        VALUES (?, ?, 'Unknown', 'Never')
    """, (name, ip))

    conn.commit()
    conn.close()

    return redirect('/')


# ---------------- API ----------------
@app.route('/api/devices')
def api_devices():
    conn = sqlite3.connect('database.db')
    c = conn.cursor()

    c.execute("SELECT id, name, ip, status, last_checked FROM devices")
    rows = c.fetchall()

    conn.close()

    data = []
    for r in rows:
        data.append({
            "id": r[0],
            "name": r[1],
            "ip": r[2],
            "status": r[3],
            "last_checked": str(r[4])
        })

    return jsonify(data)


if __name__ == "__main__":
    app.run(debug=True)