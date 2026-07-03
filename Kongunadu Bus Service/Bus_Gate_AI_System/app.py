import os
import re
import sqlite3
import smtplib
import ssl
import difflib
from datetime import datetime
from email.message import EmailMessage
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, jsonify
from werkzeug.utils import secure_filename
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from database import create_database, get_db

app = Flask(__name__)
app.secret_key = "bus_gate_ai_secret_key"
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["REPORT_FOLDER"] = "reports"
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["REPORT_FOLDER"], exist_ok=True)

create_database()

BUS_NO_PATTERN = re.compile(r"[A-Z]{2}\s*\d{1,2}\s*[A-Z]{1,2}\s*\d{3,4}")

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "admin_id" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def api_login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "admin_id" not in session:
            return jsonify({
                "ok": False,
                "type": "session_expired",
                "message": "Login session expired. Please login again and upload image."
            }), 401
        return fn(*args, **kwargs)
    return wrapper

def rows(query, params=()):
    conn = get_db(); cur = conn.cursor(); cur.execute(query, params)
    data = [dict(r) for r in cur.fetchall()]
    conn.close(); return data

def one(query, params=()):
    conn = get_db(); cur = conn.cursor(); cur.execute(query, params)
    r = cur.fetchone(); conn.close(); return dict(r) if r else None

def execute(query, params=()):
    conn = get_db(); cur = conn.cursor(); cur.execute(query, params)
    conn.commit(); conn.close()

def late_minutes(schedule, actual):
    s = datetime.strptime(schedule, "%H:%M")
    a = datetime.strptime(actual, "%H:%M")
    return max(int((a-s).total_seconds()/60), 0)

def normalize_bus_number(text):
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())

def extract_bus_number(text):
    clean = (text or "").upper().replace("O", "0")
    clean = re.sub(r"[^A-Z0-9\s]", " ", clean)
    match = BUS_NO_PATTERN.search(clean)
    if match:
        return normalize_bus_number(match.group())
    compact = normalize_bus_number(clean)
    match = re.search(r"[A-Z]{2}\d{1,2}[A-Z]{1,2}\d{3,4}", compact)
    return match.group(0) if match else ""

def find_best_registered_bus_number(detected_number):
    """Return exact or close registered bus number. Helps OCR mistakes like O/0, I/1."""
    detected = normalize_bus_number(detected_number)
    if not detected:
        return ""
    bus_numbers = [normalize_bus_number(r["bus_number"]) for r in rows("SELECT bus_number FROM buses")]
    if detected in bus_numbers:
        return detected
    matches = difflib.get_close_matches(detected, bus_numbers, n=1, cutoff=0.78)
    return matches[0] if matches else detected

def candidate_numbers_from_text(text):
    """Extract multiple possible plate numbers from OCR text."""
    text = (text or "").upper()
    variants = [text]
    variants.append(text.replace("O", "0"))
    variants.append(text.replace("I", "1").replace("L", "1"))
    variants.append(text.replace("S", "5"))
    found = []
    for v in variants:
        v = re.sub(r"[^A-Z0-9\s]", " ", v)
        for m in BUS_NO_PATTERN.finditer(v):
            n = normalize_bus_number(m.group())
            if n and n not in found:
                found.append(n)
        compact = normalize_bus_number(v)
        for m in re.finditer(r"[A-Z]{2}\d{1,2}[A-Z]{1,2}\d{3,4}", compact):
            n = m.group(0)
            if n and n not in found:
                found.append(n)
    return found

def preprocess_image_variants(image_path):
    """Create OCR-friendly image versions for better detection accuracy."""
    variants = [image_path]
    try:
        import cv2
        img = cv2.imread(image_path)
        if img is None:
            return variants
        base, ext = os.path.splitext(image_path)
        # Resize up for small number plates
        scale = 2.2
        resized = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        blur = cv2.bilateralFilter(gray, 11, 17, 17)
        # Adaptive threshold
        th1 = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5)
        # Otsu threshold
        _, th2 = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # Sharpened grayscale
        sharp = cv2.GaussianBlur(gray, (0, 0), 3)
        sharp = cv2.addWeighted(gray, 1.5, sharp, -0.5, 0)
        for name, im in [("_gray.png", gray), ("_adaptive.png", th1), ("_otsu.png", th2), ("_sharp.png", sharp)]:
            out = base + name
            cv2.imwrite(out, im)
            variants.append(out)
    except Exception:
        pass
    return variants

def detect_bus_number_from_image(image_path):
    """Automatic OCR with preprocessing and registered-bus fuzzy matching."""
    if not image_path or not os.path.exists(image_path):
        return "", "No image found"

    ocr_logs = []
    variants = preprocess_image_variants(image_path)

    # EasyOCR. First run may download OCR model.
    try:
        import easyocr
        reader = easyocr.Reader(['en'], gpu=False, verbose=False)
        for img in variants:
            try:
                results = reader.readtext(
                    img,
                    detail=0,
                    paragraph=False,
                    allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
                )
                text = " ".join(results)
                if text:
                    ocr_logs.append(text)
                candidates = candidate_numbers_from_text(text)
                if candidates:
                    return find_best_registered_bus_number(candidates[0]), " | ".join(ocr_logs)
            except Exception as inner:
                ocr_logs.append(f"EasyOCR variant failed: {inner}")
    except Exception as e:
        ocr_logs.append(f"EasyOCR failed: {e}")

    # Optional fallback: needs Tesseract installed on Windows.
    try:
        import pytesseract
        from PIL import Image
        for img in variants:
            try:
                tess_text = pytesseract.image_to_string(
                    Image.open(img),
                    config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                )
                if tess_text:
                    ocr_logs.append(tess_text)
                candidates = candidate_numbers_from_text(tess_text)
                if candidates:
                    return find_best_registered_bus_number(candidates[0]), " | ".join(ocr_logs)
            except Exception as inner:
                ocr_logs.append(f"Tesseract variant failed: {inner}")
    except Exception as e:
        ocr_logs.append(f"Tesseract failed: {e}")

    return "", " | ".join(ocr_logs) if ocr_logs else "No OCR text detected"

def send_pdf_email(to_email, pdf_path, date):
    settings = one("SELECT * FROM settings LIMIT 1") or {}
    smtp_host = (settings.get("smtp_host") or os.getenv("SMTP_HOST") or "smtp.gmail.com").strip()
    smtp_port = int(settings.get("smtp_port") or os.getenv("SMTP_PORT") or "587")
    smtp_email = (settings.get("smtp_email") or os.getenv("SMTP_EMAIL") or "").strip()
    smtp_password = (settings.get("smtp_password") or os.getenv("SMTP_PASSWORD") or "").strip().replace(" ", "")

    if not smtp_email or not smtp_password:
        raise RuntimeError("Sender Gmail ID and Gmail App Password are required in Admin Settings.")
    if not os.path.exists(pdf_path):
        raise RuntimeError("PDF report file was not created.")

    msg = EmailMessage()
    msg["Subject"] = f"Daily Bus Entry Report - {date}"
    msg["From"] = smtp_email
    msg["To"] = to_email
    msg.set_content(
        f"Dear Admin,\n\nPlease find attached the Daily Bus Entry Report for {date}.\n\nRegards,\nBus Gate AI System"
    )

    with open(pdf_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="pdf",
            filename=os.path.basename(pdf_path)
        )

    try:
        if smtp_port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as smtp:
                smtp.login(smtp_email, smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(smtp_email, smtp_password)
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError("Gmail login failed. Use a 16-character Gmail App Password, not your normal Gmail password.")
    except smtplib.SMTPRecipientsRefused:
        raise RuntimeError("Receiver email address was refused. Check the Email ID.")
    except Exception as e:
        raise RuntimeError(f"SMTP error: {e}")

@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("admin_id") and request.method == "GET":
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        admin = one("SELECT * FROM admins WHERE username=? AND password=?", (username, password))
        if admin:
            session["admin_id"] = admin["id"]
            session["admin_name"] = admin["name"]
            return redirect(url_for("dashboard"))
        flash("Invalid username or password", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    today = datetime.now().strftime("%Y-%m-%d")
    stats = {
        "total_buses": one("SELECT COUNT(*) c FROM buses")["c"],
        "today_entries": one("SELECT COUNT(*) c FROM entry_logs WHERE log_date=?", (today,))["c"],
        "late_today": one("SELECT COUNT(*) c FROM entry_logs WHERE log_date=? AND entry_status='LATE'", (today,))["c"],
        "unknown_today": one("SELECT COUNT(*) c FROM unknown_buses WHERE log_date=?", (today,))["c"],
        "avg_delay": one("SELECT COALESCE(ROUND(AVG(late_minutes),1),0) c FROM entry_logs WHERE log_date=?", (today,))["c"],
        "reentry_today": one("SELECT COUNT(*) c FROM entry_logs WHERE log_date=? AND COALESCE(duplicate_count,1) > 1", (today,))["c"]
    }
    latest = rows("SELECT * FROM entry_logs ORDER BY id DESC LIMIT 12")
    return render_template("dashboard.html", stats=stats, latest=latest)

@app.route("/buses")
@login_required
def buses():
    q = request.args.get("q", "")
    if q:
        data = rows("SELECT * FROM buses WHERE bus_number LIKE ? OR driver_name LIKE ? OR route_name LIKE ? ORDER BY id DESC", (f"%{q}%", f"%{q}%", f"%{q}%"))
    else:
        data = rows("SELECT * FROM buses ORDER BY id DESC")
    return render_template("buses.html", buses=data, q=q)

@app.route("/buses/add", methods=["POST"])
@login_required
def add_bus():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        execute('''INSERT INTO buses (bus_number,bus_name,driver_name,driver_mobile,route_name,entry_time,exit_time,created_at)
        VALUES (?,?,?,?,?,?,?,?)''', (
            normalize_bus_number(request.form["bus_number"]), request.form["bus_name"], request.form["driver_name"],
            request.form["driver_mobile"], request.form["route_name"], request.form["entry_time"], request.form.get("exit_time", ""), now))
        flash("Bus added successfully", "success")
    except sqlite3.IntegrityError:
        flash("Bus number already exists", "danger")
    return redirect(url_for("buses"))

@app.route("/buses/edit/<int:bus_id>", methods=["POST"])
@login_required
def edit_bus(bus_id):
    bus_number = normalize_bus_number(request.form.get("bus_number", ""))
    if not bus_number:
        flash("Bus number is required", "danger")
        return redirect(url_for("buses"))
    try:
        execute('''UPDATE buses SET
            bus_number=?, bus_name=?, driver_name=?, driver_mobile=?, route_name=?, entry_time=?
            WHERE id=?''', (
            bus_number, request.form.get("bus_name", ""), request.form.get("driver_name", ""),
            request.form.get("driver_mobile", ""), request.form.get("route_name", ""),
            request.form.get("entry_time", ""), bus_id
        ))
        flash("Bus details updated successfully", "success")
    except sqlite3.IntegrityError:
        flash("This bus number already exists", "danger")
    return redirect(url_for("buses"))

@app.route("/buses/delete/<int:bus_id>")
@login_required
def delete_bus(bus_id):
    execute("DELETE FROM buses WHERE id=?", (bus_id,))
    flash("Bus deleted", "success")
    return redirect(url_for("buses"))

def process_bus_image(file):
    if not file or not file.filename:
        return {"ok": False, "type": "error", "message": "Image file required"}, 400

    filename = datetime.now().strftime("%Y%m%d%H%M%S_") + secure_filename(file.filename)
    image_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(image_path)

    try:
        bus_number, ocr_text = detect_bus_number_from_image(image_path)
    except Exception as e:
        return {
            "ok": False,
            "type": "ocr_error",
            "message": f"OCR failed: {e}",
            "image_path": image_path
        }, 500

    now = datetime.now()
    actual = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    created = now.strftime("%Y-%m-%d %H:%M:%S")

    if not bus_number:
        execute("INSERT INTO unknown_buses (detected_number,image_path,detected_time,log_date) VALUES (?,?,?,?)", ("NOT_DETECTED", image_path, actual, today))
        return {
            "ok": False,
            "type": "not_detected",
            "message": "Bus number not detected. Upload a clear number plate image.",
            "ocr_text": ocr_text,
            "image_path": image_path
        }, 200

    bus = one("SELECT * FROM buses WHERE bus_number=?", (bus_number,))
    if not bus:
        execute("INSERT INTO unknown_buses (detected_number,image_path,detected_time,log_date) VALUES (?,?,?,?)", (bus_number, image_path, actual, today))
        return {
            "ok": False,
            "type": "unknown",
            "message": "Unknown bus detected",
            "bus_number": bus_number,
            "ocr_text": ocr_text,
            "image_path": image_path
        }, 200

    late = late_minutes(bus["entry_time"], actual)
    entry_status = "LATE" if late > 0 else "ON TIME"
    previous_entries = one("SELECT COUNT(*) c FROM entry_logs WHERE bus_id=? AND log_date=?", (bus["id"], today))["c"]
    duplicate_count = previous_entries + 1
    entry_type = "RE-ENTRY" if duplicate_count > 1 else "FIRST ENTRY"
    execute('''INSERT INTO entry_logs
    (bus_id,bus_number,bus_name,driver_name,route_name,scheduled_entry_time,actual_entry_time,entry_status,late_minutes,bus_status,image_path,log_date,created_at,entry_type,duplicate_count)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
    (bus["id"], bus["bus_number"], bus["bus_name"], bus["driver_name"], bus["route_name"], bus["entry_time"], actual, entry_status, late, "INSIDE", image_path, today, created, entry_type, duplicate_count))
    execute("UPDATE buses SET status='INSIDE' WHERE id=?", (bus["id"],))

    return {
        "ok": True,
        "type": "found",
        "message": "Entry added successfully",
        "bus": bus,
        "actual": actual,
        "status": entry_status,
        "entry_type": entry_type,
        "duplicate_count": duplicate_count,
        "late": late,
        "ocr_text": ocr_text,
        "image_path": image_path
    }, 200

@app.route("/detect", methods=["GET", "POST"])
@login_required
def detect():
    result = None
    if request.method == "POST":
        result, _status = process_bus_image(request.files.get("image"))
    return render_template("detect.html", result=result)

@app.route("/api/detect-entry", methods=["POST"])
@api_login_required
def api_detect_entry():
    try:
        result, status = process_bus_image(request.files.get("image"))
        return jsonify(result), status
    except Exception as e:
        # Always return JSON so upload page will not show generic server-response error
        return jsonify({
            "ok": False,
            "type": "server_error",
            "message": f"Server error: {e}"
        }), 500

@app.route("/exit/<int:bus_id>")
@login_required
def bus_exit(bus_id):
    now = datetime.now().strftime("%H:%M")
    execute("UPDATE buses SET status='OUTSIDE' WHERE id=?", (bus_id,))
    execute("UPDATE entry_logs SET actual_exit_time=?, bus_status='OUTSIDE' WHERE bus_id=? AND bus_status='INSIDE'", (now, bus_id))
    flash("Bus exit saved", "success")
    return redirect(url_for("buses"))

@app.route("/history")
@login_required
def history():
    date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    q = request.args.get("q", "")
    data = rows('''SELECT * FROM entry_logs WHERE log_date=? AND (bus_number LIKE ? OR route_name LIKE ? OR entry_status LIKE ?) ORDER BY id DESC''', (date, f"%{q}%", f"%{q}%", f"%{q}%"))
    return render_template("history.html", entries=data, date=date, q=q)

@app.route("/reports")
@login_required
def reports():
    date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    late = rows("SELECT * FROM entry_logs WHERE log_date=? AND entry_status='LATE' ORDER BY late_minutes DESC", (date,))
    unknown = rows("SELECT * FROM unknown_buses WHERE log_date=? ORDER BY id DESC", (date,))
    avg = one("SELECT COALESCE(ROUND(AVG(late_minutes),1),0) c FROM entry_logs WHERE log_date=?", (date,))["c"]
    return render_template("reports.html", date=date, late=late, unknown=unknown, avg=avg)

def build_pdf(date):
    data = rows("SELECT * FROM entry_logs WHERE log_date=? ORDER BY id DESC", (date,))
    path = os.path.join(app.config["REPORT_FOLDER"], f"Daily_Bus_Report_{date}.pdf")
    doc = SimpleDocTemplate(path, pagesize=A4)
    styles = getSampleStyleSheet()
    story = [Paragraph(f"Daily Bus Entry Report - {date}", styles["Title"]), Spacer(1, 12)]
    table_data = [["Bus No", "Bus Name", "Route", "Schedule", "Entry", "Status", "Late"]]
    for e in data:
        table_data.append([e["bus_number"], e["bus_name"] or "-", e["route_name"], e["scheduled_entry_time"], e["actual_entry_time"], e["entry_status"], f"{e['late_minutes']} min"])
    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.lightgrey), ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTSIZE", (0,0), (-1,-1), 8)
    ]))
    story.append(table)
    doc.build(story)
    return path

@app.route("/download-report")
@login_required
def download_report():
    date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    return send_file(build_pdf(date), as_attachment=True)

@app.route("/email-report", methods=["POST"])
@login_required
def email_report():
    date = request.form.get("date")
    email = request.form.get("email")
    path = build_pdf(date)
    try:
        send_pdf_email(email, path, date)
        flash(f"PDF report sent successfully to {email}", "success")
    except Exception as e:
        flash(f"Email send failed: {e}", "danger")
    return redirect(url_for("reports", date=date))

@app.route("/users")
@login_required
def users():
    return render_template("users.html", admins=rows("SELECT id,username,name,role,created_at FROM admins ORDER BY id DESC"))

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    admin = one("SELECT * FROM admins WHERE id=?", (session["admin_id"],))
    if request.method == "POST":
        form_type = request.form.get("form_type", "system")

        if form_type == "credentials":
            username = request.form.get("username", "").strip()
            admin_name = request.form.get("admin_name", "").strip() or "System Admin"
            current_password = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not username:
                flash("Login ID is required", "danger")
                return redirect(url_for("settings"))
            if not admin or current_password != admin["password"]:
                flash("Current password is incorrect", "danger")
                return redirect(url_for("settings"))
            if new_password and new_password != confirm_password:
                flash("New password and confirm password do not match", "danger")
                return redirect(url_for("settings"))

            password_to_save = new_password if new_password else admin["password"]
            try:
                execute("UPDATE admins SET username=?, password=?, name=? WHERE id=?", (username, password_to_save, admin_name, session["admin_id"]))
                session["admin_name"] = admin_name
                flash("Login ID and password updated successfully", "success")
            except sqlite3.IntegrityError:
                flash("This Login ID is already used. Please choose another one.", "danger")
            return redirect(url_for("settings"))

        execute("""UPDATE settings SET college_name=?, report_email=?, smtp_host=?, smtp_port=?, smtp_email=?, smtp_password=?, updated_at=? WHERE id=1""", (
            request.form["college_name"], request.form["report_email"], request.form.get("smtp_host", "smtp.gmail.com"),
            request.form.get("smtp_port", "587"), request.form.get("smtp_email", ""), request.form.get("smtp_password", ""),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        flash("Settings updated successfully", "success")
        return redirect(url_for("settings"))

    return render_template("settings.html", settings=one("SELECT * FROM settings LIMIT 1"), admin=admin)

if __name__ == "__main__":
    app.run(debug=False, use_reloader=False)
