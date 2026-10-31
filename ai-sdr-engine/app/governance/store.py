# app/governance/store.py - Governance records in their own tables (demo reset never wipes them); audit log is append-only
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from app import sqlstore
from app import config
from app.contracts import new_id, now_iso

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS gov_audit (
  id TEXT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
  record_type TEXT, record_id TEXT, lead_id TEXT, before TEXT, after TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS gov_audit_at ON gov_audit(at);
CREATE TRIGGER IF NOT EXISTS gov_audit_no_update BEFORE UPDATE ON gov_audit BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS gov_audit_no_delete BEFORE DELETE ON gov_audit BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TABLE IF NOT EXISTS gov_flags (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, reason TEXT NOT NULL, excerpt TEXT, lead_id TEXT, draft_id TEXT, message_id TEXT,
  status TEXT NOT NULL DEFAULT 'open', note TEXT, cleared_by TEXT, cleared_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gov_requests (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject_hash TEXT NOT NULL, requested_at TEXT NOT NULL, requested_by TEXT NOT NULL,
  completed_at TEXT, handled_by TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS gov_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


# PostgreSQL version of the append-only guard (the SQLite triggers above do the same locally)
PG_GUARD = [
    """CREATE OR REPLACE FUNCTION gov_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN RAISE EXCEPTION 'audit log is append-only'; END $$""",
    "CREATE OR REPLACE TRIGGER gov_audit_no_change BEFORE UPDATE OR DELETE ON gov_audit FOR EACH ROW EXECUTE FUNCTION gov_audit_append_only()",
    "CREATE OR REPLACE TRIGGER gov_audit_no_truncate BEFORE TRUNCATE ON gov_audit FOR EACH STATEMENT EXECUTE FUNCTION gov_audit_append_only()",
]
_pg_guarded = False


def _ensure_schema(conn) -> None:
    global _pg_guarded
    if not sqlstore.is_postgres():
        conn.executescript(SCHEMA)
        return
    conn.executescript("\n".join(line for line in SCHEMA.splitlines() if "CREATE TRIGGER" not in line))
    if not _pg_guarded:
        for statement in PG_GUARD:
            conn.execute(statement)
        _pg_guarded = True


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlstore.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def subject_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


# ---------------------------------------------------------------- audit (insert and read only; no update/delete exists)

def audit(actor: str, role: str, action: str, record_type: Optional[str] = None, record_id: Optional[str] = None,
          lead_id: Optional[str] = None, before: Any = None, after: Any = None, detail: str = "") -> dict[str, Any]:
    row = {"id": new_id("aud"), "at": now_iso(), "actor": actor, "role": role, "action": action, "record_type": record_type,
           "record_id": record_id, "lead_id": lead_id, "before": json.dumps(before, default=str) if before is not None else None,
           "after": json.dumps(after, default=str) if after is not None else None, "detail": detail[:500]}
    with _lock, _conn() as c:
        c.execute("INSERT INTO gov_audit VALUES (:id,:at,:actor,:role,:action,:record_type,:record_id,:lead_id,:before,:after,:detail)", row)
    _mirror(row)
    return row


def _mirror(row: dict[str, Any]) -> None:
    """Copy each audit event to the CRM outbox, so it survives this service's temporary disk (Render free tier)."""
    try:
        from app.crm import outbox
        outbox.emit("audit.recorded", None, {k: v for k, v in row.items() if k not in ("before", "after")})
    except Exception:  # noqa: BLE001 - the local audit row is already committed
        pass


def audit_rows(since: Optional[str] = None, until: Optional[str] = None, limit: int = 100000) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM gov_audit WHERE 1=1", []
    if since:
        q, args = q + " AND at >= ?", args + [since]
    if until:
        q, args = q + " AND at < ?", args + [until]
    with _lock, _conn() as c:
        rows = c.execute(q + " ORDER BY at DESC, rowid DESC LIMIT ?", args + [limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["before"] = json.loads(d["before"]) if d["before"] else None
        d["after"] = json.loads(d["after"]) if d["after"] else None
        out.append(d)
    return out


# ---------------------------------------------------------------- guardrail flags

def upsert_flag(flag_id: str, kind: str, reason: str, excerpt: str, lead_id: Optional[str], draft_id: Optional[str] = None,
                message_id: Optional[str] = None, created_at: Optional[str] = None) -> None:
    with _lock, _conn() as c:
        c.execute("""INSERT INTO gov_flags (id,kind,reason,excerpt,lead_id,draft_id,message_id,created_at) VALUES (?,?,?,?,?,?,?,?)
                     ON CONFLICT(id) DO NOTHING""",
                  (flag_id, kind, reason, (excerpt or "")[:200], lead_id, draft_id, message_id, created_at or now_iso()))


def flags() -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM gov_flags ORDER BY created_at DESC").fetchall()]


def get_flag(flag_id: str) -> Optional[dict[str, Any]]:
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM gov_flags WHERE id = ?", (flag_id,)).fetchone()
        return dict(r) if r else None


def set_flag_status(flag_id: str, status: str, note: str, by: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE gov_flags SET status = ?, note = ?, cleared_by = ?, cleared_at = ? WHERE id = ?",
                  (status, note[:300], by, now_iso(), flag_id))


# ---------------------------------------------------------------- data requests

def add_request(kind: str, email: str, by: str) -> dict[str, Any]:
    row = {"id": new_id("dsr"), "kind": kind, "subject_hash": subject_hash(email), "requested_at": now_iso(), "requested_by": by}
    with _lock, _conn() as c:
        c.execute("INSERT INTO gov_requests (id,kind,subject_hash,requested_at,requested_by) VALUES (:id,:kind,:subject_hash,:requested_at,:requested_by)", row)
    return row


def complete_request(req_id: str, by: str, outcome: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE gov_requests SET completed_at = ?, handled_by = ?, outcome = ? WHERE id = ? AND completed_at IS NULL",
                  (now_iso(), by, outcome[:300], req_id))


def requests() -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM gov_requests ORDER BY requested_at DESC").fetchall()]


# ---------------------------------------------------------------- settings (retention period, paused campaigns, last retention run)

def setting(key: str, default: Any = None) -> Any:
    with _lock, _conn() as c:
        r = c.execute("SELECT value FROM gov_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value: Any) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT INTO gov_settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


def paused_campaigns() -> set[str]:
    return set(setting("paused_campaigns", []))
