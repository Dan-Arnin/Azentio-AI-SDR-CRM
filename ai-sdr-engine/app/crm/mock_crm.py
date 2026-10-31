# app/crm/mock_crm.py - Built-in stand-in CRM: receives SDR webhooks and folds them into per-lead snapshots
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config
from app.contracts import now_iso

router = APIRouter(prefix="/mock-crm", tags=["mock-crm"])

# Open an independent durable projection connection on the canonical application DB.
def _connection():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS crm_received (id TEXT PRIMARY KEY, lead_id TEXT, envelope TEXT NOT NULL, entry TEXT NOT NULL)")
    return conn


# HMAC-SHA256 signature header value for a raw webhook body
def sign(body: bytes) -> str:
    digest = hmac.new(config.CRM_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# Store one delivered envelope (from HTTP or the internal default sink)
def record(envelope: dict[str, Any], via: str = "internal", signature_valid: Optional[bool] = None) -> dict[str, Any]:
    if (not isinstance(envelope, dict) or not isinstance(envelope.get("id"), str)
            or not 1 <= len(envelope["id"]) <= 200 or not isinstance(envelope.get("payload", {}), dict)):
        raise ValueError("invalid_envelope")
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    entry = {**envelope, "receivedAt": now_iso(), "via": via, "signatureValid": signature_valid}
    conn = _connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("SELECT envelope,entry FROM crm_received WHERE id=?", (envelope["id"],)).fetchone()
        if previous:
            if previous["envelope"] != encoded:
                raise ValueError("event_id_conflict")
            result = json.loads(previous["entry"])
            result["duplicate"] = True
            conn.commit()
            return result
        conn.execute("INSERT INTO crm_received(id,lead_id,envelope,entry) VALUES(?,?,?,?)",
                     (envelope["id"], envelope.get("leadId"), encoded, json.dumps(entry)))
        conn.commit()
        return entry
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# Check whether an envelope already reached the durable workspace projection.
def has_event(event_id: str) -> bool:
    conn = _connection()
    try:
        return conn.execute("SELECT 1 FROM crm_received WHERE id=?", (event_id,)).fetchone() is not None
    finally:
        conn.close()


# All received envelopes, newest first, optionally for one lead.
def events(lead_id: Optional[str] = None) -> list[dict[str, Any]]:
    conn = _connection()
    try:
        if lead_id is None:
            rows = conn.execute("SELECT entry FROM crm_received ORDER BY rowid DESC").fetchall()
        else:
            rows = conn.execute("SELECT entry FROM crm_received WHERE lead_id=? ORDER BY rowid DESC", (lead_id,)).fetchall()
        return [json.loads(row["entry"]) for row in rows]
    finally:
        conn.close()


# Fold the event stream into the latest CRM-side snapshot per lead
def leads() -> list[dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    ordered = list(reversed(events()))
    for env in ordered:
        lead_id = env.get("leadId")
        if not lead_id:
            continue
        payload = env.get("payload") or {}
        snap = snapshots.setdefault(lead_id, {
            "leadId": lead_id, "firstSeenAt": env.get("occurredAt"), "eventCount": 0,
            "messagesSent": 0, "repliesReceived": 0, "pendingApprovals": 0,
        })
        snap.update(payload.get("lead") or {})
        snap["eventCount"] += 1
        snap["lastEventType"] = env.get("type")
        snap["lastEventAt"] = env.get("occurredAt")
        _apply(snap, env.get("type", ""), payload)
    return sorted(snapshots.values(), key=lambda s: s.get("lastEventAt") or "", reverse=True)


# Apply event-type specific fields to a lead snapshot
def _apply(snap: dict[str, Any], type_: str, payload: dict[str, Any]) -> None:
    if type_ == "lead.assessed" and payload.get("assessment"):
        snap["assessment"] = payload["assessment"]
    elif type_ == "draft.pending_approval":
        snap["pendingApprovals"] += 1
    elif type_ == "message.sent":
        snap["messagesSent"] += 1
        snap["pendingApprovals"] = max(0, snap["pendingApprovals"] - 1)
    elif type_ == "reply.received":
        snap["repliesReceived"] += 1
        snap["lastReply"] = payload.get("excerpt")
    elif type_ == "alert.ae_handoff":
        snap["aeAlert"] = {"taskId": payload.get("taskId"), "owner": payload.get("ownerId"),
                           "summary": (payload.get("card") or {}).get("summary")}
    elif type_ == "task.sdr_followup":
        snap["sdrTask"] = {"taskId": payload.get("taskId"), "type": payload.get("taskType"),
                           "owner": payload.get("ownerId"), "summary": (payload.get("card") or {}).get("summary")}
    elif type_ == "sequence.stopped":
        snap["sequenceStopped"] = payload.get("reason")


# Drop everything the mock CRM has received (demo reset)
def clear() -> None:
    conn = _connection()
    try:
        conn.execute("DELETE FROM crm_received")
        conn.commit()
    finally:
        conn.close()


# Receive a signed SDR webhook envelope
@router.post("/webhook")
async def receive_webhook(request: Request) -> JSONResponse:
    body = await request.body()
    if len(body) > 2 * 1024 * 1024:
        return JSONResponse({"error": {"code": "body_too_large"}}, status_code=413)
    header = request.headers.get("X-SDR-Signature", "")
    valid = hmac.compare_digest(header.encode(), sign(body).encode()) if config.CRM_WEBHOOK_SECRET else None
    if config.CRM_WEBHOOK_SECRET and not valid:
        return JSONResponse({"error": {"code": "invalid_signature"}}, status_code=401)
    try:
        envelope = json.loads(body)
        entry = record(envelope, via="http", signature_valid=valid)
    except (json.JSONDecodeError, UnicodeError):
        return JSONResponse({"error": {"code": "invalid_json", "message": "Body must be JSON"}}, status_code=400)
    except ValueError as exc:
        code = str(exc)
        return JSONResponse({"error": {"code": code}}, status_code=409 if code == "event_id_conflict" else 400)
    return JSONResponse({"received": True, "id": entry.get("id"), "duplicate": entry.get("duplicate", False)})



# List received CRM events, newest first
@router.get("/events")
def list_events(leadId: Optional[str] = None) -> dict[str, Any]:
    return {"items": events(leadId)}


# Latest CRM-side snapshot per lead
@router.get("/leads")
def list_leads() -> dict[str, Any]:
    return {"items": leads()}
