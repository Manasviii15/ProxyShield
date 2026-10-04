import time, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient
import main
c = TestClient(main.app)

assert c.get("/").status_code == 200
assert c.get("/checkin").status_code == 200
courses = c.get("/api/courses").json(); print("courses:", courses)
s = c.post("/api/sessions/start", json={"course_id": 1}).json(); sid = s["session_id"]; print("session:", s)
qr = c.get(f"/api/sessions/{sid}/qr.png"); assert qr.status_code == 200 and qr.content[:4] == b"\x89PNG"
tok = main.qr_token(sid)

def ci(roll, token=tok, dev="d1"):
    r = c.post("/api/checkin", json={"session_id": sid, "token": token, "roll_no": roll, "device_id": dev})
    print(roll, dev, token[:3], "->", r.status_code, r.json().get("message") or r.json().get("detail")); return r.status_code

assert ci("cs101") == 200            # lowercase accepted
assert ci("CS101") == 409            # duplicate
assert ci("CS102", dev="d1") == 403  # same device, other student
assert ci("CS999", dev="d2") == 404  # not enrolled
assert ci("CS102", token="qbadtoken", dev="d2") == 400
assert ci("CS103", token=main.nfc_token(sid), dev="d3") == 200  # NFC token
old = main.qr_token(sid, time.time() - 3 * main.WINDOW)
assert ci("CS104", token=old, dev="d4") == 400                  # expired
a = c.get(f"/api/sessions/{sid}/attendance").json(); print("attendance:", a["present"], "/", a["total"])
assert a["present"] == 2
csv = c.get(f"/api/sessions/{sid}/export.csv"); print(csv.text.splitlines()[:4])
assert c.post(f"/api/sessions/{sid}/end").json()["ok"]
assert ci("CS105", dev="d5") == 400  # ended
print("summary:", c.get("/api/courses/1/summary").json()["students"][:3])

# ---------- timer ----------
import sqlite3, tempfile, pathlib

def ci2(sid_, roll, dev):
    r = c.post("/api/checkin", json={"session_id": sid_, "token": main.qr_token(sid_),
                                     "roll_no": roll, "device_id": dev})
    print("timer", roll, "->", r.status_code, r.json().get("message") or r.json().get("detail")); return r

assert c.post("/api/sessions/start", json={"course_id": 1, "minutes": 0}).status_code == 422  # validated
sid2 = c.post("/api/sessions/start", json={"course_id": 1, "minutes": 5}).json()["session_id"]
st = c.get(f"/api/sessions/{sid2}/status").json(); print("status:", st)
assert st["open"] and 290 <= st["seconds_left"] <= 300 and st["qr_window"] == 30
assert ci2(sid2, "CS106", "t1").status_code == 200

with main.db() as con:                       # pretend the 5 minutes ran out
    con.execute("UPDATE sessions SET closes_at=? WHERE id=?", (time.time() - 1, sid2))
st = c.get(f"/api/sessions/{sid2}/status").json()
assert st["active"] and not st["open"] and st["seconds_left"] == 0
r = ci2(sid2, "CS107", "t2"); assert r.status_code == 400 and "closed" in r.json()["detail"]

assert c.post(f"/api/sessions/{sid2}/extend", json={"minutes": 5}).status_code == 200   # teacher adds time
st = c.get(f"/api/sessions/{sid2}/status").json(); assert st["open"] and 295 <= st["seconds_left"] <= 300
assert ci2(sid2, "CS107", "t2").status_code == 200

c.post(f"/api/sessions/{sid2}/end")
st = c.get(f"/api/sessions/{sid2}/status").json(); assert not st["active"] and not st["open"]
assert ci2(sid2, "CS108", "t3").status_code == 400

# ---------- migration: a database made before the timer existed ----------
orig = main.DB_PATH
main.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "old.db"
old = sqlite3.connect(main.DB_PATH)
old.executescript("""CREATE TABLE sessions(id INTEGER PRIMARY KEY, course_id INTEGER NOT NULL,
    start_time REAL NOT NULL, end_time REAL, is_active INTEGER NOT NULL DEFAULT 1);
    INSERT INTO sessions(course_id, start_time) VALUES (1, 0);""")
old.commit(); old.close()
main.init_db()
with main.db() as con:
    assert "closes_at" in [r["name"] for r in con.execute("PRAGMA table_info(sessions)")]
st = c.get("/api/sessions/1/status").json(); assert st["seconds_left"] is None and st["open"]
main.DB_PATH = orig
print("migration ok")
print("ALL PASSED")
