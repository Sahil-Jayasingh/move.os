
# 🌟 Live Link for DEMO 🌟
# https://starlit-bienenstitch-d68d55.netlify.app

# SWEATNET — Proof-of-Workout Protocol

> "In 2050, AI doesn't recommend healthy behavior — it decides whether you've earned the right to relax."

A dystopian government verification portal. A real webcam feeds a real pose-estimation
model (MediaPipe, running in-browser). Squats, jumping jacks, and high knees are counted
live with a debounced state machine, liveness checks, and biomechanical quality scoring —
then wrapped in the aesthetic of an authoritarian wellness bureaucracy.

## What's actually real here

- **Live pose tracking** — MediaPipe `PoseLandmarker` (33 landmarks) runs client-side against
  your webcam feed. No fake skeleton, no canned animation.
- **A real rep-counting engine** — per-exercise angle/position metrics, a debounced
  (3-frame) state machine, a 500ms cooldown, and liveness/visibility gating, all computed
  from your actual movement.
- **A real backend** — FastAPI service, now backed by **SQLite** (`backend/sweatnet.db`),
  so sessions, telemetry, violation events, and certificates survive a restart instead of
  living in memory. Matches the API surface in the PRD, plus a `/leaderboard` and
  `/admin/sessions` view.
- **Real-time fan-out** — a `/ws/live` WebSocket broadcasts every telemetry update and
  violation event the instant it lands, so an ops dashboard or big-screen demo monitor
  never has to poll.
- **Two live clients against the same backend** — the browser dashboard (`frontend/`) and
  a standalone desktop CCTV kiosk (`desktop_client/`, OpenCV + MediaPipe) both push into
  the same session/telemetry API, so you can run the kiosk on a laptop and watch the
  numbers land in the browser dashboard's session in parallel.
- **Graceful offline mode** — if the backend isn't running, the frontend keeps working
  entirely client-side and shows `GOVERNMENT LINK: OFFLINE — LOCAL VERIFICATION ONLY`
  in the corner (which, thematically, is a feature, not a bug).

## Run it

### 1. Backend (optional but recommended for the full demo)

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Runs at `http://localhost:8000`. Check `http://localhost:8000/health`.

### 2. Frontend

Camera access requires a proper origin (not `file://`) in most browsers, so serve it:

```bash
cd frontend
python3 -m http.server 5500
```

Then open **http://localhost:5500** in Chrome or Edge (best MediaPipe/WebGPU support).
Grant camera access when prompted.

If you change the backend port/host, update `API_BASE` at the top of the `<script type="module">`
block in `frontend/index.html`. Set it to `""` to force fully offline/local mode.

### 3. Desktop CCTV client (optional — alternative to the browser)

A standalone OpenCV window instead of the browser dashboard, using the same backend:

```bash
cd desktop_client
pip install -r requirements.txt
python main.py                              # talks to http://localhost:8000
python main.py --api-base http://host:8000  # point at a different backend
python main.py --camera 1                   # pick a different webcam
```

Press `q` in the OpenCV window to quit. Every rep, violation, and telemetry tick is pushed
to the same backend the browser dashboard reads from — check `/admin/sessions` or
`/leaderboard` on the backend to see it land.

## Demo flow (~2–3 minutes)

1. **Access Restricted** → Begin Verification
2. **Citizen Identification** — ID, region, and status generated (server-backed if online)
3. **Camera Initialization** — permission prompt, model load, connection log
4. **Dashboard** — live CCTV-style feed with skeleton overlay, exercise assignment,
   rep counter, compliance index, threat level, violation log, AI observation feed,
   propaganda ticker, and a live "citizens verified" counter
5. Complete the assigned reps → **Verification** animation → **Certificate**
6. **Final screen** — the ethics question

Trigger a violation on purpose for the demo: step out of frame ("Citizen Out Of View"),
or freeze mid-pose for ~2 seconds ("Suspicious Motion — Static Pose Detected").

## Project structure

```
sweatnet/
├── backend/
│   ├── main.py            FastAPI: sessions, telemetry, events, certificates,
│   │                      leaderboard, admin view, /ws/live realtime feed,
│   │                      SQLite persistence (sweatnet.db, created on first run)
│   └── requirements.txt
├── desktop_client/
│   ├── main.py             Standalone OpenCV + MediaPipe CCTV kiosk client
│   └── requirements.txt
├── frontend/
│   └── index.html         Single-file app: UI + pose engine + state machine (no build step)
└── README.md
```

## New backend endpoints (v13.0)

| Endpoint | What it's for |
|---|---|
| `GET /leaderboard?limit=10` | Top citizens by movement credits — for a live ranking screen |
| `GET /admin/sessions?limit=50` | Every session recorded, most recent first — ops/debug view |
| `WS /ws/live` | Realtime broadcast of every telemetry tick and event, across all sessions |

Persistence means the demo survives a backend restart — kill `uvicorn` mid-session and
everything is still in `sweatnet.db` when it comes back up.

## Notes for the pitch

- Every number on screen is derived from your real movement — depth %, symmetry %,
  cadence, compliance index, and movement credits are all computed from live landmark data,
  not scripted.
- The violation log and incident IDs are generated as they actually happen (insufficient
  depth, tracking loss, static-pose liveness flags) — that's a genuine (if simplified)
  anti-spoofing signal, not set dressing.
- Everything about the *interface* — the seals, the legal footer, the propaganda banner,
  the appeals system with its 247-day processing time — is the satire. The verification
  engine underneath it is not a toy.

## PPT link
https://www.canva.com/design/DAHPA22lAH0/_b1rcxeeqCTWwZqH732B-A/edit
