# QR + NFC Lecture Attendance

One FastAPI backend, two entry points: a rotating QR code on the teacher's screen, and an optional NFC tag.

## Run it in VS Code

1. Open this folder in VS Code (File > Open Folder).
2. Open the terminal (Ctrl+`) and run:

```bash
python -m venv .venv
# Windows:        .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

3. Open http://localhost:8000 on the laptop (teacher dashboard).
4. Phones must be on the **same wifi** as the laptop. The QR points to your laptop's LAN IP, which is detected automatically.
   - If scanning fails, allow Python through the firewall, or set the URL by hand:
     `BASE_URL=http://192.168.x.x:8000` (PowerShell: `$env:BASE_URL="http://192.168.x.x:8000"`)
   - Different networks or college wifi that isolates devices? Use a tunnel: `ngrok http 8000`, then set `BASE_URL` to the ngrok URL and restart.

## Demo flow

1. Pick the course, click **Start session**. A QR appears and changes every 30 s (the page refreshes it every 10 s).
2. On a phone, scan it, enter a roll number (demo data: `CS101` to `CS110`), tap **Check in**.
3. Watch the live list update. Try scanning twice (blocked), using an old screenshot after 60 s (blocked), and a wrong roll number (blocked).
4. **End session**, then download the CSV (present and absent).
5. Click **Course summary** for attendance % per student (red under 75%).

## Timer

- The teacher sets how many minutes check-in stays open (default 10) before pressing **Start session**.
- The dashboard shows a big countdown, a "QR changes in Ns" counter, and a **+5 min** button.
- Students see "Check-in closes in MM:SS" on their phone. It turns red in the last minute, and the button locks at zero.
- The server enforces the deadline itself, and phones only count down from the server's remaining seconds, so changing a phone's clock does nothing.

## How the anti-cheating works

- QR tokens are HMAC-signed and valid for the current 30 s window plus the previous one, so a forwarded photo expires fast.
- One check-in per student per session (DB unique constraint).
- One phone can't mark two different students (device id stored in the phone's browser; a determined cheater can clear it, so treat it as a speed bump).
- Only students enrolled in the course are accepted.

## NFC (bonus)

Click **Write NFC tag** on the dashboard (Chrome on Android only) and hold a blank NFC tag to the phone. The tag stores a session link. Tapping it opens the same check-in page. Tag links don't rotate, so they are weaker than the QR. Mention that trade-off in your pitch, and an idea for later is to pair NFC with a teacher-announced code.

## Customize

- Real students: edit the seed data in `init_db()` in `main.py`, or delete `attendance.db` and change it.
- Set `SECRET_KEY` in the environment for anything beyond a demo.

## Files

- `main.py` - API, database, token logic
- `static/teacher.html` - dashboard
- `static/checkin.html` - student page
