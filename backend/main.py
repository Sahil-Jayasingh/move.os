"""
SWEATNET — Proof-of-Workout Protocol
World Health Authority · Ministry of Human Performance
Backend Verification & Records API  (v13.0 — persistent + realtime)

Run:
    pip install -r requirements.txt
    uvicorn main:app --reload --port 8000

What changed vs v12.4:
    - SQLite persistence (sweatnet.db) — sessions/events/telemetry survive restarts
    - WebSocket broadcast (/ws/live) — dashboards / admin monitors get every
      telemetry + event update in real time, no polling
    - /leaderboard, /admin/sessions — for a "citizens verified" ops view
    - Basic input validation + consistent error envelope
    - Accepts telemetry from BOTH the browser client and the desktop
      (OpenCV) client — see desktop_client/ for the CCTV-style variant
"""

import json
import os
import random
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="SWEATNET Government Verification API", version="13.0")

# --------------------------------------------------------------------------
# Real AI — Groq (OpenAI-compatible chat completions). Set GROQ_API_KEY as
# an env var (Render dashboard, or a local .env loaded by your shell) —
# never hardcode it here. If it's unset, /ai/* endpoints return 503 and the
# frontend falls back to its canned observation lines instead of failing.
# --------------------------------------------------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


async def call_groq(system: str, user: str, max_tokens: int = 40) -> Optional[str]:
    if not GROQ_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.85,
                },
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip().strip('"').strip()
        return text[:280] if text else None
    except Exception:
        return None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # hackathon-mode: any origin may petition the Ministry
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------
# Persistence — SQLite (file-based, zero external deps)
# --------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "sweatnet.db"


def init_db():
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                citizen_id TEXT NOT NULL,
                region TEXT,
                age INTEGER,
                status TEXT,
                credits INTEGER DEFAULT 0,
                compliance INTEGER DEFAULT 0,
                exercise TEXT,
                target_reps INTEGER DEFAULT 0,
                rep_count INTEGER DEFAULT 0,
                created_at TEXT,
                verified_at TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                incident_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                severity TEXT,
                description TEXT,
                timestamp TEXT
            );

            CREATE TABLE IF NOT EXISTS telemetry (
                session_id TEXT PRIMARY KEY,
                payload TEXT,
                timestamp TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
            """
        )
        try:
            db.execute("ALTER TABLE sessions ADD COLUMN notes TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists (fresh vs. upgraded sweatnet.db)
        db.commit()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def row_to_session(row: sqlite3.Row) -> dict:
    return dict(row)


# --------------------------------------------------------------------------
# Realtime — WebSocket fan-out for live dashboards / admin monitors
# --------------------------------------------------------------------------

class LiveBus:
    def __init__(self):
        self.connections: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.add(ws)

    def disconnect(self, ws: WebSocket):
        self.connections.discard(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


bus = LiveBus()

EXERCISES = {
    "squats": {"label": "SQUATS", "target_reps": 12},
    "jumping_jacks": {"label": "JUMPING JACKS", "target_reps": 20},
    "high_knees": {"label": "HIGH KNEES", "target_reps": 15},
}

REGIONS = ["SECTOR-7", "SECTOR-12", "SECTOR-19", "SECTOR-4", "SECTOR-22"]

_BOOT_TIME = time.time()
_BASE_GLOBAL_COUNT = 18_247_921


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_citizen_id() -> str:
    return f"IN-2050-{uuid.uuid4().hex[:6].upper()}"


def new_incident_id() -> str:
    return f"INC-2050-{random.randint(100000, 999999)}"


def new_certificate_id() -> str:
    return f"CERT-2050-{random.randint(10000, 99999)}"


def get_session_or_404(db, session_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="CITIZEN RECORD NOT FOUND")
    return row


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class TelemetryIn(BaseModel):
    session_id: str
    exercise: Optional[str] = None
    state: Optional[str] = None
    rep_count: int = 0
    target: int = 0
    tracking: Optional[str] = "GOOD"
    tracking_confidence: float = 0.0
    movement_quality: float = 0.0
    cadence: float = 0.0
    depth: float = 0.0
    symmetry: float = 0.0
    liveness: Optional[str] = "VERIFIED"
    compliance: int = 0
    credits: int = 0
    threat: Optional[str] = "LOW"
    observation: Optional[str] = ""
    session_complete: bool = False


class EventIn(BaseModel):
    session_id: str
    severity: str = Field(description="INFO | WARNING | VIOLATION | CRITICAL")
    description: str


class AIObserveIn(BaseModel):
    session_id: str
    kind: str = "ambient"  # ambient | success | low_depth | tracking
    exercise: Optional[str] = None
    rep_count: int = 0
    target: int = 0
    depth: float = 0.0
    symmetry: float = 0.0
    cadence: float = 0.0
    threat: Optional[str] = "LOW"
    compliance: int = 0
    credits: int = 0
    violations_count: int = 0
    note_hint: Optional[str] = None


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    init_db()


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "OPERATIONAL", "revision": "13.0", "time": now_iso(), "persistence": "sqlite"}


@app.post("/session/start")
def start_session():
    session_id = str(uuid.uuid4())
    session = {
        "session_id": session_id,
        "citizen_id": new_citizen_id(),
        "region": random.choice(REGIONS),
        "age": random.randint(19, 64),
        "status": "RESTRICTED",
        "credits": 0,
        "compliance": 0,
        "exercise": None,
        "target_reps": 0,
        "rep_count": 0,
        "created_at": now_iso(),
        "verified_at": None,
    }
    with get_db() as db:
        db.execute(
            """INSERT INTO sessions (session_id, citizen_id, region, age, status, credits,
                compliance, exercise, target_reps, rep_count, created_at, verified_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session["session_id"], session["citizen_id"], session["region"], session["age"],
                session["status"], session["credits"], session["compliance"], session["exercise"],
                session["target_reps"], session["rep_count"], session["created_at"], session["verified_at"],
            ),
        )
        db.commit()
    return session


@app.get("/session/{session_id}")
def get_session(session_id: str):
    with get_db() as db:
        return row_to_session(get_session_or_404(db, session_id))


@app.post("/exercise/random")
def assign_exercise(session_id: str):
    with get_db() as db:
        get_session_or_404(db, session_id)
        exercise_type = random.choice(list(EXERCISES.keys()))
        config = EXERCISES[exercise_type]
        db.execute(
            "UPDATE sessions SET exercise=?, target_reps=?, rep_count=0 WHERE session_id=?",
            (exercise_type, config["target_reps"], session_id),
        )
        db.commit()
    return {"exercise": exercise_type, "label": config["label"], "target_reps": config["target_reps"]}


@app.post("/telemetry")
async def ingest_telemetry(payload: TelemetryIn):
    with get_db() as db:
        session = get_session_or_404(db, payload.session_id)
        record = payload.dict()
        record["timestamp"] = now_iso()

        db.execute(
            "INSERT INTO telemetry (session_id, payload, timestamp) VALUES (?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET payload=excluded.payload, timestamp=excluded.timestamp",
            (payload.session_id, json.dumps(record), record["timestamp"]),
        )

        new_status = session["status"]
        verified_at = session["verified_at"]
        if payload.session_complete and session["status"] != "COMPLIANT":
            new_status = "COMPLIANT"
            verified_at = now_iso()

        db.execute(
            "UPDATE sessions SET rep_count=?, credits=?, compliance=?, status=?, verified_at=? WHERE session_id=?",
            (payload.rep_count, payload.credits, payload.compliance, new_status, verified_at, payload.session_id),
        )
        db.commit()

    await bus.broadcast({"type": "telemetry", "data": record})
    return {"ok": True}


@app.get("/telemetry/{session_id}")
def get_telemetry(session_id: str):
    with get_db() as db:
        get_session_or_404(db, session_id)
        row = db.execute("SELECT payload FROM telemetry WHERE session_id=?", (session_id,)).fetchone()
        return json.loads(row["payload"]) if row else {}


@app.post("/events")
async def log_event(payload: EventIn):
    with get_db() as db:
        get_session_or_404(db, payload.session_id)
        event = {
            "incident_id": new_incident_id(),
            "session_id": payload.session_id,
            "severity": payload.severity,
            "description": payload.description,
            "timestamp": now_iso(),
        }
        db.execute(
            "INSERT INTO events (incident_id, session_id, severity, description, timestamp) VALUES (?,?,?,?,?)",
            (event["incident_id"], event["session_id"], event["severity"], event["description"], event["timestamp"]),
        )
        db.commit()

    await bus.broadcast({"type": "event", "data": event})
    return event


@app.get("/events/{session_id}")
def list_events(session_id: str):
    with get_db() as db:
        get_session_or_404(db, session_id)
        rows = db.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY timestamp DESC", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/verification/start")
def verification_start(session_id: str):
    with get_db() as db:
        get_session_or_404(db, session_id)
        db.execute("UPDATE sessions SET status='VERIFYING' WHERE session_id=?", (session_id,))
        db.commit()
        return row_to_session(get_session_or_404(db, session_id))


@app.post("/verification/reset")
def verification_reset(session_id: str):
    with get_db() as db:
        get_session_or_404(db, session_id)
        db.execute(
            """UPDATE sessions SET status='RESTRICTED', credits=0, compliance=0, exercise=NULL,
               target_reps=0, rep_count=0, verified_at=NULL WHERE session_id=?""",
            (session_id,),
        )
        db.execute("DELETE FROM events WHERE session_id=?", (session_id,))
        db.execute("DELETE FROM telemetry WHERE session_id=?", (session_id,))
        db.commit()
        return row_to_session(get_session_or_404(db, session_id))


@app.get("/compliance/{session_id}")
def compliance(session_id: str):
    with get_db() as db:
        session = get_session_or_404(db, session_id)
        return {
            "citizen_id": session["citizen_id"],
            "compliance": session["compliance"],
            "credits": session["credits"],
            "status": session["status"],
        }


@app.get("/certificate/{session_id}")
def certificate(session_id: str):
    with get_db() as db:
        session = get_session_or_404(db, session_id)
        if session["status"] != "COMPLIANT":
            raise HTTPException(status_code=403, detail="VERIFICATION INCOMPLETE — CERTIFICATE DENIED")
        session = dict(session)
        return {
            "certificate_id": new_certificate_id(),
            "citizen_id": session["citizen_id"],
            "status": "COMPLIANT",
            "movement_credits": session["credits"],
            "verification_time": session["verified_at"],
            "notes": session.get("notes"),
            "issuing_authority": "WORLD HEALTH AUTHORITY — MINISTRY OF HUMAN PERFORMANCE",
            "revision": "13.0",
        }


@app.post("/ai/observe")
async def ai_observe(payload: AIObserveIn):
    """Real, model-generated observation line for the AI Observation Feed —
    not picked from a static list. Also persisted as a session note (INFO
    event) so every session's AI commentary survives in the DB and shows up
    in /events/{session_id} and /admin/sessions."""
    if not GROQ_API_KEY:
        raise HTTPException(status_code=503, detail="AI OBSERVER OFFLINE — GROQ_API_KEY NOT CONFIGURED")

    system = (
        "You are the AI surveillance observer for SWEATNET, a dystopian 2050 government "
        "fitness-verification system. You watch a citizen exercise via CCTV and log exactly "
        "ONE short, cold, bureaucratic observation line about their movement, based only on "
        "the metrics given. Rules: one sentence, under 14 words, no quotation marks, no "
        "markdown, no emoji, terminal-log tone, e.g. 'Cadence within tolerance. Compliance "
        "trending upward.'"
    )
    user = (
        f"kind={payload.kind} exercise={payload.exercise} reps={payload.rep_count}/{payload.target} "
        f"depth={payload.depth:.2f} symmetry={payload.symmetry:.2f} cadence={payload.cadence:.1f} "
        f"threat={payload.threat} compliance={payload.compliance} credits={payload.credits} "
        f"violations={payload.violations_count} hint={payload.note_hint or ''}"
    )
    text = await call_groq(system, user, max_tokens=30)
    if not text:
        raise HTTPException(status_code=502, detail="AI OBSERVER — NO RESPONSE")

    with get_db() as db:
        get_session_or_404(db, payload.session_id)
        event = {
            "incident_id": new_incident_id(),
            "session_id": payload.session_id,
            "severity": "INFO",
            "description": text,
            "timestamp": now_iso(),
        }
        db.execute(
            "INSERT INTO events (incident_id, session_id, severity, description, timestamp) VALUES (?,?,?,?,?)",
            (event["incident_id"], event["session_id"], event["severity"], event["description"], event["timestamp"]),
        )
        db.commit()

    await bus.broadcast({"type": "ai_observation", "data": event})
    return {"text": text}


@app.post("/ai/session-summary")
async def ai_session_summary(session_id: str):
    """Model-generated closing note for a completed session's certificate —
    stored on the session row so it survives a restart."""
    with get_db() as db:
        session = get_session_or_404(db, session_id)
        session = dict(session)

    if not GROQ_API_KEY:
        raise HTTPException(status_code=503, detail="AI OBSERVER OFFLINE — GROQ_API_KEY NOT CONFIGURED")

    system = (
        "You are the AI compliance officer for SWEATNET, a dystopian 2050 government "
        "fitness-verification system, writing the closing remark on a citizen's compliance "
        "certificate. Write 1-2 short sentences, cold bureaucratic tone, referencing their "
        "actual performance numbers. No markdown, no quotation marks."
    )
    user = (
        f"exercise={session.get('exercise')} reps={session.get('rep_count')}/{session.get('target_reps')} "
        f"credits={session.get('credits')} compliance={session.get('compliance')} status={session.get('status')}"
    )
    text = await call_groq(system, user, max_tokens=70)
    if not text:
        raise HTTPException(status_code=502, detail="AI OBSERVER — NO RESPONSE")

    with get_db() as db:
        db.execute("UPDATE sessions SET notes=? WHERE session_id=?", (text, session_id))
        db.commit()

    return {"notes": text}


@app.get("/stats/global")
def global_stats():
    elapsed = time.time() - _BOOT_TIME
    drift = int(elapsed * random.uniform(1.5, 3.5))
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
        compliant = db.execute("SELECT COUNT(*) c FROM sessions WHERE status='COMPLIANT'").fetchone()["c"]
    return {
        "citizens_verified_today": _BASE_GLOBAL_COUNT + drift,
        "sessions_total": total,
        "sessions_compliant": compliant,
    }


@app.get("/leaderboard")
def leaderboard(limit: int = 10):
    with get_db() as db:
        rows = db.execute(
            "SELECT citizen_id, region, credits, compliance, status FROM sessions "
            "ORDER BY credits DESC, compliance DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/admin/sessions")
def admin_sessions(limit: int = 50):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    """Real-time feed of every telemetry update and event across all sessions —
    point an ops dashboard or a big-screen demo monitor at this."""
    await bus.connect(websocket)
    try:
        while True:
            # We don't expect inbound messages, but keep the socket alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        bus.disconnect(websocket)
    except Exception:
        bus.disconnect(websocket)


if __name__ == "__main__":
    # Render/Railway/most PaaS set $PORT — bind to that instead of a fixed port.
    import os
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
