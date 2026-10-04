"""QR + NFC attendance system (FastAPI + SQLite).

Run:  uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
import csv
import hashlib
import hmac
import io
import os
import socket
import sqlite3
import time
from pathlib import Path

import qrcode
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).parent
DB_PATH = BASE / "attendance.db"
SECRET = os.environ.get("SECRET_KEY", "change-me-before-real-use").encode()
WINDOW = 15  # seconds a QR token stays fresh
PORT = int(os.environ.get("PORT", 8000))


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# Phones must reach this URL. Override with BASE_URL=https://xxxx.ngrok.app
BASE_URL = os.environ.get("BASE_URL", f"http://{lan_ip()}:{PORT}").rstrip("/")


# ---------- database ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS students(
                id INTEGER PRIMARY KEY, roll_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS courses(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS enrollments(
                student_id INTEGER NOT NULL, course_id INTEGER NOT NULL,
                PRIMARY KEY(student_id, course_id));
            CREATE TABLE IF NOT EXISTS sessions(
                id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL,
                start_time REAL NOT NULL, end_time REAL, is_active INTEGER NOT NULL DEFAULT 1,
                closes_at REAL);
            CREATE TABLE IF NOT EXISTS attendance(
                id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, student_id INTEGER NOT NULL,
                timestamp REAL NOT NULL, device_id TEXT, method TEXT,
                UNIQUE(session_id, student_id));
            """
        )
        # Migration: databases created before the timer feature lack this column.
        cols = [r["name"] for r in c.execute("PRAGMA table_info(sessions)")]
        if "closes_at" not in cols:
            c.execute("ALTER TABLE sessions ADD COLUMN closes_at REAL")
        if c.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 0:
            c.execute("INSERT INTO courses(id, name) VALUES (1, 'Demo Course - Data Science 101')")
            names = ["Asha", "Ravi", "Meera", "Karan", "Sneha", "Arjun", "Divya", "Imran", "Pooja", "Vikram"]
            for i, n in enumerate(names, start=1):
                c.execute("INSERT INTO students(id, roll_no, name) VALUES (?,?,?)", (i, f"CS{100 + i}", n))
                c.execute("INSERT INTO enrollments VALUES (?,1)", (i,))


# ---------- tokens ----------
def _sig(session_id: int, label: str) -> str:
    return hmac.new(SECRET, f"{session_id}:{label}".encode(), hashlib.sha256).hexdigest()[:12]


def qr_token(session_id: int, at: float | None = None) -> str:
    return "q" + _sig(session_id, str(int((at or time.time()) // WINDOW)))


def nfc_token(session_id: int) -> str:
    return "n" + _sig(session_id, "nfc")


def token_method(session_id: int, token: str) -> str | None:
    """Returns 'qr' / 'nfc' if the token is valid, else None."""
    if hmac.compare_digest(token, nfc_token(session_id)):
        return "nfc"
    for back in (0, 1):  # accept current and previous window
        if hmac.compare_digest(token, qr_token(session_id, time.time() - back * WINDOW)):
            return "qr"
    return None


# ---------- app ----------
app = FastAPI(title="Lecture Attendance")
init_db()


class StartReq(BaseModel):
    course_id: int
    minutes: int = Field(10, ge=1, le=240)  # how long check-in stays open


class ExtendReq(BaseModel):
    minutes: int = Field(5, ge=1, le=60)


class CheckInReq(BaseModel):
    session_id: int
    token: str
    roll_no: str
    device_id: str = ""


def get_session(c: sqlite3.Connection, session_id: int) -> sqlite3.Row:
    row = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")
    return row


@app.get("/api/courses")
def courses():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM courses")]


@app.post("/api/sessions/start")
def start_session(req: StartReq):
    with db() as c:
        if not c.execute("SELECT 1 FROM courses WHERE id=?", (req.course_id,)).fetchone():
            raise HTTPException(404, "Course not found")
        c.execute("UPDATE sessions SET is_active=0, end_time=? WHERE course_id=? AND is_active=1",
                  (time.time(), req.course_id))
        now = time.time()
        cur = c.execute(
            "INSERT INTO sessions(course_id, start_time, closes_at) VALUES (?,?,?)",
            (req.course_id, now, now + req.minutes * 60),
        )
        sid = cur.lastrowid
    return {"session_id": sid, "nfc_url": f"{BASE_URL}/checkin?s={sid}&t={nfc_token(sid)}"}


@app.get("/api/sessions/{sid}/qr.png")
def qr_png(sid: int):
    with db() as c:
        s = get_session(c, sid)
    if not s["is_active"]:
        raise HTTPException(400, "Session ended")
    url = f"{BASE_URL}/checkin?s={sid}&t={qr_token(sid)}"
    buf = io.BytesIO()
    qrcode.make(url, box_size=8, border=2).save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/checkin")
def checkin(req: CheckInReq):
    with db() as c:
        s = get_session(c, req.session_id)
        if not s["is_active"]:
            raise HTTPException(400, "This session has ended.")
        if s["closes_at"] is not None and time.time() >= s["closes_at"]:
            raise HTTPException(400, "Check-in window is closed.")
        method = token_method(req.session_id, req.token)
        if not method:
            raise HTTPException(400, "QR expired. Scan the screen again.")
        stu = c.execute(
            """SELECT st.* FROM students st JOIN enrollments e ON e.student_id=st.id
               WHERE st.roll_no=? AND e.course_id=?""",
            (req.roll_no.strip().upper(), s["course_id"]),
        ).fetchone()
        if not stu:
            raise HTTPException(404, "Roll number not enrolled in this course.")
        if c.execute("SELECT 1 FROM attendance WHERE session_id=? AND student_id=?", (s["id"], stu["id"])).fetchone():
            raise HTTPException(409, f"{stu['name']}, you are already marked present.")
        if req.device_id and c.execute(
            "SELECT 1 FROM attendance WHERE session_id=? AND device_id=?", (s["id"], req.device_id)
        ).fetchone():
            raise HTTPException(403, "This device already checked in another student.")
        c.execute(
            "INSERT INTO attendance(session_id, student_id, timestamp, device_id, method) VALUES (?,?,?,?,?)",
            (s["id"], stu["id"], time.time(), req.device_id or None, method),
        )
    return {"ok": True, "message": f"Marked present: {stu['name']} ({stu['roll_no']})"}


@app.get("/api/sessions/{sid}/status")
def session_status(sid: int):
    """Timer info. Clients count down from these server-side values, never from their own clocks."""
    with db() as c:
        s = get_session(c, sid)
    now = time.time()
    left = None if s["closes_at"] is None else max(0, int(s["closes_at"] - now))
    is_open = bool(s["is_active"]) and (s["closes_at"] is None or now < s["closes_at"])
    return {
        "active": bool(s["is_active"]),
        "open": is_open,
        "seconds_left": left,                       # None = no time limit (old sessions)
        "qr_seconds_left": WINDOW - int(now % WINDOW),
        "qr_window": WINDOW,
    }


@app.post("/api/sessions/{sid}/extend")
def extend_session(sid: int, req: ExtendReq):
    with db() as c:
        s = get_session(c, sid)
        if not s["is_active"]:
            raise HTTPException(400, "Session ended")
        now = time.time()
        base = max(now, s["closes_at"] or now)
        c.execute("UPDATE sessions SET closes_at=? WHERE id=?", (base + req.minutes * 60, sid))
    return {"ok": True}


@app.get("/api/sessions/{sid}/attendance")
def attendance(sid: int):
    with db() as c:
        s = get_session(c, sid)
        rows = c.execute(
            """SELECT st.roll_no, st.name, a.timestamp, a.method FROM attendance a
               JOIN students st ON st.id=a.student_id WHERE a.session_id=? ORDER BY a.timestamp""",
            (sid,),
        ).fetchall()
        total = c.execute("SELECT COUNT(*) FROM enrollments WHERE course_id=?", (s["course_id"],)).fetchone()[0]
    return {"active": bool(s["is_active"]), "present": len(rows), "total": total, "records": [dict(r) for r in rows]}


@app.post("/api/sessions/{sid}/end")
def end_session(sid: int):
    with db() as c:
        get_session(c, sid)
        c.execute("UPDATE sessions SET is_active=0, end_time=? WHERE id=?", (time.time(), sid))
    return {"ok": True}


@app.get("/api/sessions/{sid}/export.csv")
def export_csv(sid: int):
    with db() as c:
        s = get_session(c, sid)
        rows = c.execute(
            """SELECT st.roll_no, st.name, a.timestamp, a.method FROM students st
               JOIN enrollments e ON e.student_id=st.id AND e.course_id=?
               LEFT JOIN attendance a ON a.student_id=st.id AND a.session_id=?
               ORDER BY st.roll_no""",
            (s["course_id"], sid),
        ).fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["roll_no", "name", "status", "time", "method"])
    for r in rows:
        t = time.strftime("%H:%M:%S", time.localtime(r["timestamp"])) if r["timestamp"] else ""
        w.writerow([r["roll_no"], r["name"], "Present" if r["timestamp"] else "Absent", t, r["method"] or ""])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=attendance_session_{sid}.csv"})


@app.get("/api/courses/{cid}/summary")
def course_summary(cid: int):
    """Attendance % per student across all sessions of a course (analytics stretch goal)."""
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM sessions WHERE course_id=?", (cid,)).fetchone()[0]
        rows = c.execute(
            """SELECT st.roll_no, st.name,
                      (SELECT COUNT(*) FROM attendance a JOIN sessions s ON s.id=a.session_id
                        WHERE a.student_id=st.id AND s.course_id=?) AS attended
               FROM students st JOIN enrollments e ON e.student_id=st.id AND e.course_id=?
               ORDER BY st.roll_no""",
            (cid, cid),
        ).fetchall()
    return {"sessions": n, "students": [
        {**dict(r), "percent": round(100 * r["attended"] / n, 1) if n else 0.0} for r in rows]}


@app.get("/")
def teacher_page():
    return FileResponse(BASE / "static" / "teacher.html")


@app.get("/checkin")
def student_page():
    return FileResponse(BASE / "static" / "checkin.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
