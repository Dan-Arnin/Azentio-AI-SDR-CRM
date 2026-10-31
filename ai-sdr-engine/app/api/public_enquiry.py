# app/api/public_enquiry.py - Public receipt and consent adapter for the canonical SDR pipeline
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Literal, Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from app import config, pipeline
from app.contracts import LeadInput, now_iso

router = APIRouter(prefix="/public", tags=["enquiries"])


class PublicEnquiry(LeadInput):
    privacyAccepted: Literal[True]


# Open a short-lived receipt connection in the same canonical database.
def receipts() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS public_enquiry_receipts (
        request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, lead_id TEXT NOT NULL,
        privacy_accepted INTEGER NOT NULL, contact_permission TEXT NOT NULL,
        acknowledged_at TEXT NOT NULL, response TEXT NOT NULL)""")
    return conn


# Bind public retries to their original details and enqueue only the native pipeline.
@router.post("/enquiries")
async def submit_enquiry(body: PublicEnquiry, idempotency_key: Optional[str] = Header(default=None)):
    key = idempotency_key or body.sourceRecordId
    if not key or len(key) > 200:
        return JSONResponse({"error": {"code": "invalid_reference", "message": "Please reload the form and try again."}}, status_code=422)
    normalized = body.model_dump(exclude={"submittedAt"}, mode="json")
    fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    conn = receipts()
    try:
        previous = conn.execute("SELECT * FROM public_enquiry_receipts WHERE request_key=?", (key,)).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                return JSONResponse({"error": {"code": "request_changed", "message": "The details changed. Please start a fresh enquiry."}}, status_code=409)
            return JSONResponse(json.loads(previous["response"]), status_code=200)
        inp = LeadInput.model_validate(body.model_dump(exclude={"privacyAccepted"}))
        response = await pipeline.ingest_lead(inp, key)
        response["reference"] = "AZ-" + response["leadId"].removeprefix("lead_").upper()
        conn.execute("INSERT INTO public_enquiry_receipts VALUES(?,?,?,?,?,?,?)", (
            key, fingerprint, response["leadId"], 1, inp.contactPermission, now_iso(), json.dumps(response)))
        conn.commit()
        return JSONResponse(response, status_code=201)
    finally:
        conn.close()
