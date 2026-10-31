"""SQLite persistence. JSON-heavy columns keep the schema small for a hackathon build."""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("SDR_DB", str(Path(__file__).resolve().parent.parent / "sdr.db"))
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY, email TEXT, source TEXT, product TEXT, state TEXT,
  capture TEXT, person TEXT, company TEXT, icp TEXT, crm TEXT, hygiene TEXT,
  outcome TEXT, owner TEXT, followups INTEGER DEFAULT 0, sequence_id TEXT, enrichment TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, lead_id TEXT, direction TEXT, kind TEXT, subject TEXT, body TEXT,
  status TEXT, template_key TEXT, sequence_id TEXT, step INTEGER, guardrail TEXT, created_at TEXT, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS analyses (
  id TEXT PRIMARY KEY, lead_id TEXT, message_id TEXT, result TEXT, decision TEXT, engine TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
  id TEXT PRIMARY KEY, lead_id TEXT, type TEXT, assignee TEXT, summary TEXT, status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS sequences (
  id TEXT PRIMARY KEY, name TEXT, cohort_key TEXT, cohort TEXT, theme TEXT, product TEXT,
  steps TEXT, status TEXT, engine TEXT, approved_by TEXT, sends INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS enrollments (
  id TEXT PRIMARY KEY, sequence_id TEXT, lead_id TEXT, email TEXT, next_step INTEGER, status TEXT, reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS suppressions (email TEXT PRIMARY KEY, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS crm_opportunities (
  id TEXT PRIMARY KEY, lead_id TEXT, account TEXT, name TEXT, product TEXT, stage TEXT, owner TEXT, bant TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS template_sends (template_key TEXT PRIMARY KEY, approved_sends INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, type TEXT, detail TEXT, created_at TEXT
);
"""

JSON_COLS = {"enrichment", "capture", "person", "company", "icp", "crm", "hygiene", "outcome", "owner", "guardrail",
             "result", "decision", "summary", "cohort", "steps", "bant", "detail"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_db = None


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = _conn()
        _db.executescript(SCHEMA)
        cols = {r[1] for r in _db.execute("PRAGMA table_info(leads)")}
        for col in ("enrichment",):  # databases created before these columns existed
            if col not in cols:
                _db.execute(f"ALTER TABLE leads ADD COLUMN {col} TEXT")
    return _db


def reset() -> None:
    with _lock:
        conn = db()
        for table in ["leads", "messages", "analyses", "alerts", "sequences", "enrollments",
                      "suppressions", "crm_opportunities", "template_sends", "events"]:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(SCHEMA)
        conn.commit()


def _encode(row: dict) -> dict:
    return {k: (json.dumps(v) if k in JSON_COLS and v is not None else v) for k, v in row.items()}


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for k in out:
        if k in JSON_COLS and isinstance(out[k], str):
            out[k] = json.loads(out[k])
    return out


def insert(table: str, row: dict) -> dict:
    with _lock:
        enc = _encode(row)
        cols = ", ".join(enc)
        marks = ", ".join("?" for _ in enc)
        db().execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(enc.values()))
        db().commit()
    return row


def update(table: str, key: str, values: dict, key_col: str = "id") -> None:
    with _lock:
        enc = _encode(values)
        sets = ", ".join(f"{k} = ?" for k in enc)
        db().execute(f"UPDATE {table} SET {sets} WHERE {key_col} = ?", [*enc.values(), key])
        db().commit()


def get(table: str, key: str, key_col: str = "id") -> dict | None:
    with _lock:
        return _decode(db().execute(f"SELECT * FROM {table} WHERE {key_col} = ?", [key]).fetchone())


def query(sql: str, params: list | tuple = ()) -> list[dict]:
    with _lock:
        return [_decode(r) for r in db().execute(sql, params).fetchall()]


def execute(sql: str, params: list | tuple = ()) -> None:
    with _lock:
        db().execute(sql, params)
        db().commit()


def log_event(lead_id: str | None, type_: str, detail: dict) -> None:
    insert("events", {"lead_id": lead_id, "type": type_, "detail": detail, "created_at": now()})
