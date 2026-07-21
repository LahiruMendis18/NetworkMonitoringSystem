from flask import Flask, render_template, request, redirect, session, jsonify
import sqlite3, hashlib, os, re, threading, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from functools import wraps

app = Flask(__name__)
app.secret_key = os.urandom(32)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")

# ── EMAIL CONFIG ──────────────────────────────────────────────────────────────
SMTP_HOST  = os.environ.get("SMTP_HOST",  "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER  = os.environ.get("SMTP_USER",  "")   # your Gmail address
SMTP_PASS  = os.environ.get("SMTP_PASS",  "")   # Gmail App Password
SMTP_FROM  = os.environ.get("SMTP_FROM",  SMTP_USER)

# ── SMS CONFIG (Twilio) ───────────────────────────────────────────────────────
TWILIO_SID   = os.environ.get("TWILIO_SID",   "")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM  = os.environ.get("TWILIO_FROM",  "")

# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                email    TEXT DEFAULT '',
                phone    TEXT DEFAULT '',
                created  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS devices (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                ip           TEXT NOT NULL,
                group_label  TEXT DEFAULT 'General',
                status       TEXT DEFAULT 'Unknown',
                latency_ms   REAL DEFAULT NULL,
                uptime_pct   REAL DEFAULT 100.0,
                last_checked TEXT DEFAULT 'Never',
                added        TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ping_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER,
                status    TEXT,
                latency   REAL,
                ts        TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER,
                message   TEXT,
                severity  TEXT DEFAULT 'warning',
                seen      INTEGER DEFAULT 0,
                ts        TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
            );
        """)
        # migrate old DBs that lack email/phone columns
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "email" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
        if "phone" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''")
        pw = hashlib.sha256("Admin@1234".encode()).hexdigest()
        conn.execute("INSERT OR IGNORE INTO users (username,password) VALUES (?,?)", ("admin", pw))
        conn.commit()

init_db()

# ── NOTIFICATIONS ─────────────────────────────────────────────────────────────

def _email_wrap(title, body_html, accent):
    return (
        '<div style="font-family:Inter,Arial,sans-serif;max-width:520px;margin:auto;'
        'background:#0f172a;border-radius:12px;overflow:hidden;">'
        '<div style="background:linear-gradient(135deg,#0066ff,' + accent + ');padding:28px 32px;">'
        '<h1 style="margin:0;color:#fff;font-size:22px;">&#9889; Monitoring You</h1></div>'
        '<div style="padding:28px 32px;color:#d8e5f5;">' + body_html + '</div>'
        '<div style="padding:14px 32px;background:#0c1220;color:#4a6585;font-size:12px;">'
        'Monitoring You — Network Monitoring &mdash; automated notification</div></div>'
    )

def send_email(to_addr, subject, html_body, plain_body=""):
    if not to_addr or not SMTP_USER or not SMTP_PASS:
        return
    def _send():
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = "Monitoring You <{}>".format(SMTP_FROM)
            msg["To"]      = to_addr
            if plain_body:
                msg.attach(MIMEText(plain_body, "plain"))
            msg.attach(MIMEText(html_body, "html"))
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
                s.ehlo(); s.starttls(); s.ehlo()
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_FROM, to_addr, msg.as_string())
            print("  [mail] sent '{}' to {}".format(subject, to_addr))
        except Exception as e:
            print("  [mail-err] {}".format(e))
    threading.Thread(target=_send, daemon=True).start()

def send_sms(to_phone, body):
    if not to_phone or not TWILIO_SID or not TWILIO_TOKEN or not TWILIO_FROM:
        return
    def _send():
        try:
            import urllib.request, urllib.parse, base64
            url  = "https://api.twilio.com/2010-04-01/Accounts/{}/Messages.json".format(TWILIO_SID)
            data = urllib.parse.urlencode({"From": TWILIO_FROM, "To": to_phone, "Body": body}).encode()
            cred = base64.b64encode("{}:{}".format(TWILIO_SID, TWILIO_TOKEN).encode()).decode()
            req  = urllib.request.Request(url, data=data,
                       headers={"Authorization": "Basic " + cred}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                print("  [sms] sent to {}, status={}".format(to_phone, r.status))
        except Exception as e:
            print("  [sms-err] {}".format(e))
    threading.Thread(target=_send, daemon=True).start()

def notify_user(user_row, subject, html, plain):
    if user_row["email"]:
        send_email(user_row["email"], subject, html, plain)
    if user_row["phone"]:
        send_sms(user_row["phone"], plain)

def notify_all_users(subject, html, plain):
    with get_db() as conn:
        users = conn.execute("SELECT email, phone FROM users").fetchall()
    for u in users:
        if u["email"]: send_email(u["email"], subject, html, plain)
        if u["phone"]: send_sms(u["phone"], plain)

# ── EMAIL TEMPLATES ───────────────────────────────────────────────────────────

def tpl_login_ok(username):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        '<h2 style="color:#00e5a0;margin-top:0;">Welcome to Monitoring You</h2>'
        '<p>Hi <strong>' + username + '</strong>, you signed in successfully.</p>'
        '<p style="color:#7a95b5;font-size:13px;">Time: ' + ts + '</p>'
        '<p style="color:#ff3e5e;font-size:12px;">Not you? Change your password immediately.</p>'
    )
    return _email_wrap("Welcome to Monitoring You", body, "#00e5a0")

def tpl_login_fail(username):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        '<h2 style="color:#ff3e5e;margin-top:0;">Sign In Failed</h2>'
        '<p>A failed sign-in attempt was made for <strong>' + username + '</strong>.</p>'
        '<p style="color:#7a95b5;font-size:13px;">Time: ' + ts + '</p>'
        '<p style="color:#ff3e5e;font-size:12px;">If this wasn\'t you, your account may be at risk.</p>'
    )
    return _email_wrap("Sign In Failed", body, "#ff3e5e")

def tpl_logout(username):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        '<h2 style="color:#00c9ff;margin-top:0;">Check Your Privacy</h2>'
        '<p>Hi <strong>' + username + '</strong>, you have signed out of Monitoring You.</p>'
        '<p style="color:#7a95b5;font-size:13px;">Time: ' + ts + '</p>'
        '<p style="color:#7a95b5;font-size:12px;">Remember to log out of shared or public devices and review your account activity regularly.</p>'
    )
    return _email_wrap("Check Your Privacy", body, "#00c9ff")

def tpl_status_report(online, offline, changes):
    accent = "#00e5a0" if offline == 0 else "#ff3e5e"
    heading = "All Systems Online" if offline == 0 else "{} Device(s) Offline".format(offline)
    rows_html = ""
    for c in changes:
        color = "#00e5a0" if c["new_status"] == "Online" else "#ff3e5e"
        rows_html += (
            '<tr>'
            '<td style="padding:8px 12px;border-bottom:1px solid #1e2d45;">' + c["name"] + '</td>'
            '<td style="padding:8px 12px;border-bottom:1px solid #1e2d45;color:' + color + ';">' + c["new_status"] + '</td>'
            '<td style="padding:8px 12px;border-bottom:1px solid #1e2d45;color:#7a95b5;">' + c["ip"] + '</td>'
            '</tr>'
        )
    if not rows_html:
        rows_html = '<tr><td colspan="3" style="padding:12px;color:#4a6585;text-align:center;">No changes</td></tr>'

    body = (
        '<h2 style="color:' + accent + ';margin-top:0;">' + heading + '</h2>'
        '<div style="display:flex;gap:16px;margin:20px 0;">'
        '<div style="flex:1;background:#0c1220;border-radius:8px;padding:16px;text-align:center;">'
        '<div style="font-size:32px;font-weight:700;color:#00e5a0;">' + str(online) + '</div>'
        '<div style="color:#4a6585;font-size:12px;margin-top:4px;">ONLINE</div></div>'
        '<div style="flex:1;background:#0c1220;border-radius:8px;padding:16px;text-align:center;">'
        '<div style="font-size:32px;font-weight:700;color:#ff3e5e;">' + str(offline) + '</div>'
        '<div style="color:#4a6585;font-size:12px;margin-top:4px;">OFFLINE</div></div>'
        '</div>'
        '<p style="color:#7a95b5;font-size:13px;margin-bottom:10px;">Status changes:</p>'
        '<table style="width:100%;border-collapse:collapse;background:#0c1220;border-radius:8px;overflow:hidden;">'
        '<thead><tr>'
        '<th style="padding:10px 12px;text-align:left;color:#4a6585;font-size:11px;">DEVICE</th>'
        '<th style="padding:10px 12px;text-align:left;color:#4a6585;font-size:11px;">STATUS</th>'
        '<th style="padding:10px 12px;text-align:left;color:#4a6585;font-size:11px;">IP</th>'
        '</tr></thead><tbody>' + rows_html + '</tbody></table>'
        '<p style="color:#4a6585;font-size:12px;margin-top:16px;">' + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + '</p>'
    )
    return _email_wrap("Device Status Report", body, accent)

def tpl_welcome_account(username):
    body = (
        '<h2 style="color:#00c9ff;margin-top:0;">Account Created</h2>'
        '<p>Hi <strong>' + username + '</strong>, your Monitoring You account is ready.</p>'
        '<p style="color:#7a95b5;">Sign in to start monitoring your network.</p>'
    )
    return _email_wrap("Account Created", body, "#00c9ff")

# ── HELPERS ───────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper

def validate_ip(ip):
    if not re.match(r"^(\d{1,3}\.){3}\d{1,3}$", ip):
        return False
    return all(0 <= int(p) <= 255 for p in ip.split("."))

def validate_email(email):
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email)) if email else True

def validate_phone(phone):
    return bool(re.match(r"^\+?[\d\s\-()\.]{{7,20}}$".format(), phone)) if phone else True

# ── AUTH ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user" in session:
        return redirect("/")
    error   = None
    success = request.args.get("registered")
    logged_out = request.args.get("logged_out")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        pw_hash  = hashlib.sha256(password.encode()).hexdigest()
        with get_db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE username=? AND password=?",
                (username, pw_hash)
            ).fetchone()
        if user:
            session["user"]    = user["username"]
            session["user_id"] = user["id"]
            session["welcome"] = True
            notify_user(
                user,
                "Welcome to Monitoring You",
                tpl_login_ok(user["username"]),
                "Welcome to Monitoring You\n\nHi {}, you signed in at {}.".format(
                    user["username"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            return redirect("/")
        else:
            error = "Sign In Failed"
            with get_db() as conn:
                victim = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if victim:
                notify_user(
                    victim,
                    "Sign In Failed — Suspicious Activity",
                    tpl_login_fail(username),
                    "Sign In Failed\n\nA failed sign-in attempt was made for {} at {}.".format(
                        username, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                )
    return render_template("login.html", error=error, success=success, logged_out=logged_out)

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if "user" in session:
        return redirect("/")
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        confirm  = request.form.get("confirm",  "").strip()
        email    = request.form.get("email",    "").strip()
        phone    = request.form.get("phone",    "").strip()

        if not username or len(username) < 3:
            error = "Username must be at least 3 characters."
        elif not re.match(r"^[a-zA-Z0-9_]+$", username):
            error = "Username may only contain letters, numbers and underscores."
        elif not password or len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif not email and not phone:
            error = "Please provide at least an email address or phone number."
        elif email and not validate_email(email):
            error = "Invalid email address format."
        else:
            pw_hash = hashlib.sha256(password.encode()).hexdigest()
            try:
                with get_db() as conn:
                    conn.execute(
                        "INSERT INTO users (username,password,email,phone) VALUES (?,?,?,?)",
                        (username, pw_hash, email, phone)
                    )
                    conn.commit()
                if email:
                    send_email(
                        email,
                        "Welcome to Monitoring You — Account Created",
                        tpl_welcome_account(username),
                        "Hi {}, your Monitoring You account has been created.".format(username)
                    )
                return redirect("/login?registered=1")
            except sqlite3.IntegrityError:
                error = "Username already taken. Please choose another."
    return render_template("signup.html", error=error)

@app.route("/logout")
def logout():
    username = session.get("user")
    if username:
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user:
            notify_user(
                user,
                "Check Your Privacy",
                tpl_logout(user["username"]),
                "Check Your Privacy\n\nHi {}, you signed out of Monitoring You at {}.".format(
                    user["username"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
    session.clear()
    return redirect("/login?logged_out=1")

# ── DASHBOARD ─────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    welcome = session.pop("welcome", False)
    return render_template("index.html", user=session["user"], show_welcome=welcome)

# ── PROFILE ───────────────────────────────────────────────────────────────────

@app.route("/profile")
@login_required
def profile_page():
    with get_db() as conn:
        user = conn.execute(
            "SELECT id, username, email, phone, created FROM users WHERE id=?",
            (session["user_id"],)
        ).fetchone()
    if not user:
        session.clear()
        return redirect("/login")
    return render_template("profile.html", user=user)

@app.route("/api/profile", methods=["GET"])
@login_required
def api_profile_get():
    with get_db() as conn:
        user = conn.execute(
            "SELECT id, username, email, phone, created FROM users WHERE id=?",
            (session["user_id"],)
        ).fetchone()
    if not user:
        return jsonify({"error": "User not found."}), 404
    return jsonify(dict(user))

@app.route("/api/profile", methods=["PATCH"])
@login_required
def api_profile_update():
    """Update email and/or phone for the logged-in user."""
    data  = request.get_json(force=True) or {}
    email = data.get("email", "").strip()
    phone = data.get("phone", "").strip()

    if not email and not phone:
        return jsonify({"error": "Please provide at least an email address or phone number."}), 400
    if email and not validate_email(email):
        return jsonify({"error": "Invalid email address format."}), 400
    if phone and not validate_phone(phone):
        return jsonify({"error": "Invalid phone number format."}), 400

    with get_db() as conn:
        conn.execute(
            "UPDATE users SET email=?, phone=? WHERE id=?",
            (email, phone, session["user_id"])
        )
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/profile/password", methods=["POST"])
@login_required
def api_profile_change_password():
    """Change password for the logged-in user — requires current password."""
    data         = request.get_json(force=True) or {}
    current_pw   = data.get("current_password", "")
    new_pw       = data.get("new_password", "").strip()
    confirm_pw   = data.get("confirm_password", "").strip()

    if not current_pw or not new_pw or not confirm_pw:
        return jsonify({"error": "All password fields are required."}), 400
    if len(new_pw) < 6:
        return jsonify({"error": "New password must be at least 6 characters."}), 400
    if new_pw != confirm_pw:
        return jsonify({"error": "New passwords do not match."}), 400

    current_hash = hashlib.sha256(current_pw.encode()).hexdigest()
    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE id=? AND password=?",
            (session["user_id"], current_hash)
        ).fetchone()
        if not user:
            return jsonify({"error": "Current password is incorrect."}), 403

        new_hash = hashlib.sha256(new_pw.encode()).hexdigest()
        conn.execute("UPDATE users SET password=? WHERE id=?", (new_hash, session["user_id"]))
        conn.commit()

    # notify the user their password changed
    notify_user(
        user,
        "Monitoring You — Password Changed",
        _email_wrap(
            "Password Changed",
            '<h2 style="color:#00c9ff;margin-top:0;">Password Changed</h2>'
            '<p>Hi <strong>' + user["username"] + '</strong>, your password was just changed.</p>'
            '<p style="color:#7a95b5;font-size:13px;">Time: ' + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + '</p>'
            '<p style="color:#ff3e5e;font-size:12px;">If you didn\'t make this change, reset your password immediately.</p>',
            "#00c9ff"
        ),
        "Your Monitoring You password was changed at {}.".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    return jsonify({"ok": True})

# ── DEVICE CRUD ───────────────────────────────────────────────────────────────

@app.route("/api/devices", methods=["GET"])
@login_required
def api_devices():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id,name,ip,group_label,status,latency_ms,uptime_pct,last_checked,added FROM devices ORDER BY added DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/devices", methods=["POST"])
@login_required
def api_add_device():
    data  = request.get_json(force=True) or {}
    name  = data.get("name", "").strip()
    ip    = data.get("ip",   "").strip()
    group = data.get("group","General").strip() or "General"
    if not name:
        return jsonify({"error": "Device name is required."}), 400
    if not ip or not validate_ip(ip):
        return jsonify({"error": "A valid IPv4 address is required."}), 400
    with get_db() as conn:
        if conn.execute("SELECT 1 FROM devices WHERE ip=?", (ip,)).fetchone():
            return jsonify({"error": "A device with IP {} already exists.".format(ip)}), 409
        conn.execute("INSERT INTO devices (name,ip,group_label) VALUES (?,?,?)", (name,ip,group))
        conn.commit()
    return jsonify({"ok": True}), 201

@app.route("/api/devices/<int:device_id>", methods=["DELETE"])
@login_required
def api_delete_device(device_id):
    with get_db() as conn:
        conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
        conn.commit()
    return jsonify({"ok": True})

@app.route("/api/devices/<int:device_id>", methods=["PATCH"])
@login_required
def api_update_device(device_id):
    data  = request.get_json(force=True) or {}
    name  = data.get("name","").strip()
    group = data.get("group","General").strip() or "General"
    if not name:
        return jsonify({"error": "Name is required."}), 400
    with get_db() as conn:
        conn.execute("UPDATE devices SET name=?,group_label=? WHERE id=?", (name,group,device_id))
        conn.commit()
    return jsonify({"ok": True})

# ── STATS ─────────────────────────────────────────────────────────────────────

@app.route("/api/stats")
@login_required
def api_stats():
    with get_db() as conn:
        total   = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        online  = conn.execute("SELECT COUNT(*) FROM devices WHERE status='Online'").fetchone()[0]
        offline = conn.execute("SELECT COUNT(*) FROM devices WHERE status='Offline'").fetchone()[0]
        avg_lat = conn.execute("SELECT AVG(latency_ms) FROM devices WHERE latency_ms IS NOT NULL").fetchone()[0]
        unseen  = conn.execute("SELECT COUNT(*) FROM alerts WHERE seen=0").fetchone()[0]
    return jsonify({"total":total,"online":online,"offline":offline,
                    "unknown":total-online-offline,
                    "avg_latency":round(avg_lat,1) if avg_lat else None,
                    "unseen_alerts":unseen})

@app.route("/api/devices/<int:device_id>/history")
@login_required
def api_history(device_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT status,latency,ts FROM ping_history WHERE device_id=? ORDER BY ts DESC LIMIT 50",
            (device_id,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])

# ── ALERTS ────────────────────────────────────────────────────────────────────

@app.route("/api/alerts")
@login_required
def api_alerts():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT a.id,a.message,a.severity,a.seen,a.ts,d.name AS device_name
            FROM alerts a JOIN devices d ON d.id=a.device_id
            ORDER BY a.ts DESC LIMIT 50
        """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alerts/read", methods=["POST"])
@login_required
def api_mark_alerts_read():
    with get_db() as conn:
        conn.execute("UPDATE alerts SET seen=1")
        conn.commit()
    return jsonify({"ok": True})

# ── MONITOR UPDATE ────────────────────────────────────────────────────────────

@app.route("/api/internal/update", methods=["POST"])
def api_internal_update():
    token = request.headers.get("X-Monitor-Token","")
    if token != os.environ.get("MONITOR_TOKEN","monitor-secret"):
        return jsonify({"error":"Forbidden"}), 403

    data      = request.get_json(force=True) or {}
    device_id = data.get("device_id")
    status    = data.get("status")
    latency   = data.get("latency")
    if not device_id or status not in ("Online","Offline"):
        return jsonify({"error":"Bad payload"}), 400

    now            = datetime.now().isoformat(sep=" ", timespec="seconds")
    status_changed = False
    changes        = []

    with get_db() as conn:
        prev = conn.execute("SELECT status,name,ip FROM devices WHERE id=?", (device_id,)).fetchone()
        prev_status = prev["status"] if prev else "Unknown"

        history      = conn.execute("SELECT status FROM ping_history WHERE device_id=? ORDER BY ts DESC LIMIT 100",(device_id,)).fetchall()
        total_checks = len(history) + 1
        online_count = sum(1 for h in history if h["status"]=="Online") + (1 if status=="Online" else 0)
        uptime       = round((online_count / total_checks) * 100, 1)

        conn.execute("UPDATE devices SET status=?,latency_ms=?,uptime_pct=?,last_checked=? WHERE id=?",
                     (status, latency, uptime, now, device_id))
        conn.execute("INSERT INTO ping_history (device_id,status,latency,ts) VALUES (?,?,?,?)",
                     (device_id, status, latency, now))

        if status == "Offline" and prev_status != "Offline":
            conn.execute("INSERT INTO alerts (device_id,message,severity) VALUES (?,?,'critical')",
                         (device_id, "Device went offline at {}".format(now)))
            status_changed = True
        elif status == "Online" and prev_status == "Offline":
            status_changed = True

        if status_changed and prev:
            changes.append({"name": prev["name"], "ip": prev["ip"], "new_status": status})

        total_online  = conn.execute("SELECT COUNT(*) FROM devices WHERE status='Online'").fetchone()[0]
        total_offline = conn.execute("SELECT COUNT(*) FROM devices WHERE status='Offline'").fetchone()[0]
        conn.commit()

    if status_changed and changes:
        c       = changes[0]
        subject = "Monitoring You Alert: {} is {}".format(c["name"], c["new_status"])
        html    = tpl_status_report(total_online, total_offline, changes)
        plain   = (
            "Monitoring You Device Status Report\n\n"
            "Online: {}  |  Offline: {}\n\n".format(total_online, total_offline) +
            "\n".join("- {} ({}): {}".format(ch["name"], ch["ip"], ch["new_status"]) for ch in changes) +
            "\n\nTime: {}".format(now)
        )
        threading.Thread(target=notify_all_users, args=(subject, html, plain), daemon=True).start()

    return jsonify({"ok": True})


# ── FORGOT PASSWORD ───────────────────────────────────────────────────────────

import random, string

def generate_temp_password(length=10):
    chars = string.ascii_letters + string.digits + "!@#$"
    return "".join(random.choices(chars, k=length))

def tpl_reset_password(username, temp_pw):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        '<h2 style="color:#00c9ff;margin-top:0;">Password Reset</h2>'
        '<p>Hi <strong>' + username + '</strong>, your temporary password is:</p>'
        '<div style="background:#0c1220;border:1px solid #1e2d45;border-radius:8px;'
        'padding:16px 20px;margin:16px 0;text-align:center;">'
        '<span style="font-family:JetBrains Mono,monospace;font-size:22px;'
        'font-weight:700;color:#00c9ff;letter-spacing:2px;">' + temp_pw + '</span></div>'
        '<p style="color:#7a95b5;font-size:13px;">Sign in with this password and change it immediately.</p>'
        '<p style="color:#4a6585;font-size:12px;">Requested at: ' + ts + '</p>'
    )
    return _email_wrap("Password Reset", body, "#00c9ff")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    error = None
    sent  = None
    if request.method == "POST":
        method   = request.form.get("method", "email")
        username = request.form.get("username", "").strip()
        contact  = request.form.get("contact",  "").strip()

        if not username or not contact:
            error = "Please fill in all fields."
        else:
            with get_db() as conn:
                user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

            if not user:
                # Don\'t reveal if user exists
                error = "No account found with those details."
            else:
                matched = False
                if method == "email" and user["email"] and user["email"].lower() == contact.lower():
                    matched = True
                elif method == "phone" and user["phone"] and user["phone"].replace(" ","") == contact.replace(" ",""):
                    matched = True

                if not matched:
                    error = "The {} you entered does not match our records for that username.".format(
                        "email address" if method == "email" else "phone number")
                else:
                    temp_pw = generate_temp_password()
                    pw_hash = hashlib.sha256(temp_pw.encode()).hexdigest()
                    with get_db() as conn:
                        conn.execute("UPDATE users SET password=? WHERE id=?", (pw_hash, user["id"]))
                        conn.commit()

                    if method == "email":
                        send_email(
                            user["email"],
                            "Monitoring You — Password Reset",
                            tpl_reset_password(username, temp_pw),
                            "Your Monitoring You temporary password is: {}\n\nSign in and change it immediately.".format(temp_pw)
                        )
                        sent = "A temporary password has been sent to {}. Check your inbox.".format(
                            user["email"][:3] + "***@" + user["email"].split("@")[-1])
                    else:
                        masked = user["phone"][:3] + "****" + user["phone"][-3:]
                        send_sms(user["phone"],
                            "Monitoring You: Your temporary password is: {}. Sign in and change it immediately.".format(temp_pw))
                        sent = "A temporary password has been sent via SMS to {}.".format(masked)

    return render_template("forgot_password.html", error=error, sent=sent)


# ── PASSWORD RESET ────────────────────────────────────────────────────────────

import secrets as _secrets

@app.route("/api/reset-request", methods=["POST"])
def api_reset_request():
    data     = request.get_json(force=True) or {}
    username = data.get("username", "").strip()
    email    = data.get("email",    "").strip().lower()

    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username=? AND LOWER(email)=?",
            (username, email)
        ).fetchone()

    # Always return 200 — never reveal if user/email combo exists (security)
    if user:
        token = _secrets.token_urlsafe(32)
        now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Store token in DB (expires in 1 hour — checked on reset page)
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reset_tokens (
                    token    TEXT PRIMARY KEY,
                    user_id  INTEGER,
                    expires  TEXT
                )
            """)
            # expire = 1 hour from now
            from datetime import timedelta
            expires = (datetime.now() + timedelta(hours=1)).isoformat(sep=" ", timespec="seconds")
            conn.execute("DELETE FROM reset_tokens WHERE user_id=?", (user["id"],))
            conn.execute("INSERT INTO reset_tokens (token,user_id,expires) VALUES (?,?,?)",
                         (token, user["id"], expires))
            conn.commit()

        reset_url = "http://localhost:5000/reset-password?token=" + token
        html = (
            '<div style="font-family:Inter,Arial,sans-serif;max-width:520px;margin:auto;'
            'background:#0f172a;border-radius:12px;overflow:hidden;">'
            '<div style="background:linear-gradient(135deg,#0066ff,#00c9ff);padding:28px 32px;">'
            '<h1 style="margin:0;color:#fff;font-size:22px;">&#9889; Monitoring You</h1></div>'
            '<div style="padding:28px 32px;color:#d8e5f5;">'
            '<h2 style="color:#00c9ff;margin-top:0;">Password Reset</h2>'
            '<p>Hi <strong>' + user["username"] + '</strong>,</p>'
            '<p>Click the button below to reset your password. This link expires in 1 hour.</p>'
            '<div style="text-align:center;margin:28px 0;">'
            '<a href="' + reset_url + '" style="background:linear-gradient(135deg,#0066ff,#00c9ff);'
            'color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:600;'
            'font-size:15px;display:inline-block;">Reset My Password</a></div>'
            '<p style="color:#4a6585;font-size:12px;">Or copy this link:<br>'
            '<span style="color:#7a95b5;">' + reset_url + '</span></p>'
            '<p style="color:#ff3e5e;font-size:12px;margin-top:16px;">If you did not request this, ignore this email.</p>'
            '</div>'
            '<div style="padding:14px 32px;background:#0c1220;color:#4a6585;font-size:12px;">'
            'Monitoring You — Network Monitoring &mdash; automated notification</div></div>'
        )
        plain = (
            "Monitoring You Password Reset\n\n"
            "Hi " + user["username"] + ",\n\n"
            "Reset your password here:\n" + reset_url + "\n\n"
            "This link expires in 1 hour. If you did not request this, ignore this email."
        )
        send_email(user["email"], "Monitoring You — Password Reset Request", html, plain)

    return jsonify({"ok": True})


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    token = request.args.get("token", "")
    error = None
    if request.method == "POST":
        token    = request.form.get("token", "")
        password = request.form.get("password", "").strip()
        confirm  = request.form.get("confirm",  "").strip()
        if not password or len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            with get_db() as conn:
                try:
                    conn.execute("CREATE TABLE IF NOT EXISTS reset_tokens (token TEXT PRIMARY KEY, user_id INTEGER, expires TEXT)")
                except Exception:
                    pass
                row = conn.execute("SELECT * FROM reset_tokens WHERE token=?", (token,)).fetchone()
            if not row:
                error = "Invalid or expired reset link. Please request a new one."
            else:
                from datetime import datetime as _dt
                if _dt.now() > _dt.fromisoformat(row["expires"]):
                    error = "This reset link has expired. Please request a new one."
                else:
                    pw_hash = hashlib.sha256(password.encode()).hexdigest()
                    with get_db() as conn:
                        conn.execute("UPDATE users SET password=? WHERE id=?", (pw_hash, row["user_id"]))
                        conn.execute("DELETE FROM reset_tokens WHERE token=?", (token,))
                        conn.commit()
                    return redirect("/login?reset=1")
    return render_template("reset_password.html", token=token, error=error)

# ── PING-TEST ─────────────────────────────────────────────────────────────────

@app.route("/api/ping-test", methods=["POST"])
def api_ping_test():
    token    = request.headers.get("X-Monitor-Token","")
    expected = os.environ.get("MONITOR_TOKEN","monitor-secret")
    if token != expected:
        return jsonify({"error":"Forbidden"}), 403
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    return jsonify({"ok":True,"devices_in_db":count,"db_path":DB_PATH})

# ── GROUPS ────────────────────────────────────────────────────────────────────

@app.route("/api/groups")
@login_required
def api_groups():
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT group_label FROM devices ORDER BY group_label").fetchall()
    return jsonify([r["group_label"] for r in rows])


# ── FORGOT PASSWORD ───────────────────────────────────────────────────────────
import random, string, time as _time

# In-memory OTP store: { username: {otp, expires, token} }
_otp_store = {}

def _generate_otp():
    return ''.join(random.choices(string.digits, k=6))

def _generate_token():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=48))

def _mask_contact(value):
    """Partially hide email/phone for display."""
    if not value:
        return ""
    if "@" in value:
        local, domain = value.split("@", 1)
        return local[:2] + "***@" + domain
    # phone
    return value[:3] + "****" + value[-3:]

@app.route("/forgot-password")
def forgot_password_page():
    return render_template("forgot_password.html")

@app.route("/api/forgot-password/request", methods=["POST"])
def api_forgot_request():
    data     = request.get_json(force=True) or {}
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"error": "Username is required."}), 400

    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()

    if not user:
        return jsonify({"error": "No account found with that username."}), 404

    if not user["email"] and not user["phone"]:
        return jsonify({"error": "No contact method registered for this account. Contact an admin."}), 400

    otp     = _generate_otp()
    expires = _time.time() + 600  # 10 minutes
    _otp_store[username] = {"otp": otp, "expires": expires, "token": None}

    # build contact hint for the UI
    if user["email"]:
        hint = _mask_contact(user["email"])
    else:
        hint = _mask_contact(user["phone"])

    # send OTP
    subject  = "Monitoring You Password Reset Code: " + otp
    html_body = (
        '<div style="font-family:Inter,Arial,sans-serif;max-width:520px;margin:auto;'
        'background:#0f172a;border-radius:12px;overflow:hidden;">'
        '<div style="background:linear-gradient(135deg,#0066ff,#00c9ff);padding:28px 32px;">'
        '<h1 style="margin:0;color:#fff;font-size:22px;">&#9889; Monitoring You</h1></div>'
        '<div style="padding:28px 32px;color:#d8e5f5;">'
        '<h2 style="color:#00c9ff;margin-top:0;">Password Reset Code</h2>'
        '<p>Hi <strong>' + username + '</strong>, here is your verification code:</p>'
        '<div style="text-align:center;margin:28px 0;">'
        '<span style="font-family:JetBrains Mono,monospace;font-size:40px;font-weight:700;'
        'letter-spacing:12px;color:#00c9ff;background:#0c1220;padding:16px 24px;border-radius:10px;">' + otp + '</span></div>'
        '<p style="color:#7a95b5;font-size:13px;">This code expires in 10 minutes.</p>'
        '<p style="color:#ff3e5e;font-size:12px;">If you didn\'t request this, ignore this email.</p>'
        '</div><div style="padding:14px 32px;background:#0c1220;color:#4a6585;font-size:12px;">'
        'Monitoring You — Network Monitoring</div></div>'
    )
    plain_body = "Monitoring You Password Reset\n\nYour verification code is: " + otp + "\n\nExpires in 10 minutes."

    if user["email"]:
        send_email(user["email"], subject, html_body, plain_body)
    if user["phone"]:
        send_sms(user["phone"], "Monitoring You reset code: " + otp + " (expires 10 min)")

    print("  [reset] OTP {} sent for user {}".format(otp, username))
    return jsonify({"ok": True, "contact_hint": hint})

@app.route("/api/forgot-password/verify", methods=["POST"])
def api_forgot_verify():
    data     = request.get_json(force=True) or {}
    username = data.get("username", "").strip()
    otp      = data.get("otp", "").strip()

    entry = _otp_store.get(username)
    if not entry:
        return jsonify({"error": "No reset request found. Please start over."}), 400
    if _time.time() > entry["expires"]:
        _otp_store.pop(username, None)
        return jsonify({"error": "Code expired. Please request a new one."}), 400
    if entry["otp"] != otp:
        return jsonify({"error": "Incorrect code. Please try again."}), 400

    token = _generate_token()
    _otp_store[username]["token"] = token
    return jsonify({"ok": True, "token": token})

@app.route("/api/forgot-password/reset", methods=["POST"])
def api_forgot_reset():
    data     = request.get_json(force=True) or {}
    token    = data.get("token", "").strip()
    password = data.get("password", "").strip()

    if not password or len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    # find username by token
    username = None
    for u, entry in _otp_store.items():
        if entry.get("token") == token and _time.time() < entry["expires"]:
            username = u
            break

    if not username:
        return jsonify({"error": "Reset session expired or invalid. Please start over."}), 400

    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    with get_db() as conn:
        conn.execute("UPDATE users SET password=? WHERE username=?", (pw_hash, username))
        conn.commit()

    _otp_store.pop(username, None)
    print("  [reset] Password updated for user {}".format(username))
    return jsonify({"ok": True})

if __name__ == "__main__":
    print("=" * 50)
    print("  Monitoring You starting")
    print("  Email : {}".format("enabled  ("+SMTP_USER+")" if SMTP_USER else "disabled — set SMTP_USER + SMTP_PASS"))
    print("  SMS   : {}".format("enabled" if TWILIO_SID else "disabled — set TWILIO_SID + TWILIO_TOKEN"))
    print("=" * 50)
    app.run(debug=True, host="0.0.0.0", port=5000)
