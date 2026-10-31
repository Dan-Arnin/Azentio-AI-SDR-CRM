# app/db.py - Tiny SQLite document store (one table per kind, JSON body) shared by all modules
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from app import sqlstore
from app.config import DB_PATH

T = TypeVar("T", bound=BaseModel)

KINDS = (
    "leads", "assessments", "drafts", "messages", "campaigns",
    "enrollments", "tasks", "crm_events", "runs", "suppressions", "ai_calls", "dossiers", "styles",
)

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


# Open (once) the SQLite connection and create tables
def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlstore.connect(DB_PATH, check_same_thread=False)
            for k in KINDS:
                _conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {k} (id TEXT PRIMARY KEY, lead_id TEXT, "
                    f"body TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
                )
                _conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{k}_lead ON {k}(lead_id)")
            _conn.commit()
        return _conn


# Insert or replace a model in its kind table
def put(kind: str, obj: BaseModel) -> BaseModel:
    body = obj.model_dump(mode="json")
    with _lock:
        conn().execute(
            f"INSERT INTO {kind}(id, lead_id, body) VALUES(?,?,?) "
            f"ON CONFLICT(id) DO UPDATE SET lead_id=excluded.lead_id, body=excluded.body",
            (body["id"], body.get("leadId") or (body["id"] if kind == "leads" else None), json.dumps(body)),
        )
        conn().commit()
    return obj


# Fetch one record by id, parsed into model
def get(kind: str, id_: str, model: Type[T]) -> Optional[T]:
    with _lock:
        row = conn().execute(f"SELECT body FROM {kind} WHERE id=?", (id_,)).fetchone()
    return model.model_validate_json(row[0]) if row else None


# List records of a kind, optionally by lead, newest last
def list_(kind: str, model: Type[T], lead_id: Optional[str] = None, limit: int = 500) -> list[T]:
    q, args = f"SELECT body FROM {kind}", []
    if lead_id:
        q, args = q + " WHERE lead_id=?", [lead_id]
    q += " ORDER BY created_at, rowid LIMIT ?"
    args.append(limit)
    with _lock:
        rows = conn().execute(q, args).fetchall()
    return [model.model_validate_json(r[0]) for r in rows]


# Find records whose JSON field equals a value (small-data scan; fine for demo scale)
def find(kind: str, model: Type[T], **eq: Any) -> list[T]:
    out = []
    for item in list_(kind, model, limit=100000):
        d = item.model_dump()
        if all(_dig(d, k) == v for k, v in eq.items()):
            out.append(item)
    return out


# Resolve a double-underscore path (input__email) inside a dict
def _dig(d: Any, path: str) -> Any:
    for part in path.split("__"):
        if not isinstance(d, dict):
            return None
        d = d.get(part)
    return d


# Remove one record (used by erasure to replace a plaintext suppression entry with its hash)
def delete(kind: str, id_: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind}")
    with _lock:
        conn().execute(f"DELETE FROM {kind} WHERE id=?", (id_,))
        conn().commit()


# Drop every table (demo reset)
def reset() -> None:
    with _lock:
        for k in KINDS:
            conn().execute(f"DELETE FROM {k}")
        conn().commit()
