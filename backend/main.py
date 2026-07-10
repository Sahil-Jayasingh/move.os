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
import random
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="SWEATNET Government Verification API", version="13.0")

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
        return {
            "certificate_id": new_certificate_id(),
            "citizen_id": session["citizen_id"],
            "status": "COMPLIANT",
            "movement_credits": session["credits"],
            "verification_time": session["verified_at"],
            "issuing_authority": "WORLD HEALTH AUTHORITY — MINISTRY OF HUMAN PERFORMANCE",
            "revision": "13.0",
        }


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
