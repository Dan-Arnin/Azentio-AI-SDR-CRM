# app/api/linkedin.py - LinkedIn step endpoints: AI note drafts, manual-send logging, confirmed PhantomBuster sends and imports
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.contracts import Campaign, Draft, Lead, LeadInput

router = APIRouter(prefix="/v1", tags=["linkedin"])


class ImportInput(BaseModel):
    agentId: str
    productId: str = "unknown"


# Load a lead or 404
def _lead(lead_id: str) -> Lead:
    lead = db.get("leads", lead_id, Lead)
    if not lead:
        raise HTTPException(404, detail={"code": "not_found"})
    return lead


# Draft an AI LinkedIn connection note for one lead (awaits human approval)
@router.post("/leads/{lead_id}/linkedin-note")
async def lead_note(lead_id: str) -> Draft:
    from app.engagement.linkedin import draft_note
    return await draft_note(_lead(lead_id))


# Draft LinkedIn notes for every active contact in a campaign (one note each, skipped if one is already open)
@router.post("/campaigns/{campaign_id}/linkedin-notes")
async def campaign_notes(campaign_id: str) -> dict[str, Any]:
    from app.engagement.linkedin import campaign_targets, draft_note
    camp = db.get("campaigns", campaign_id, Campaign)
    if not camp:
        raise HTTPException(404, detail={"code": "not_found"})
    drafts = [await draft_note(lead, camp.theme, enr.id) for enr, lead in campaign_targets(campaign_id, camp.enrollmentIds)]
    return {"created": len(drafts), "drafts": [d.model_dump(mode="json") for d in drafts]}


# Record that an approved note was sent manually on LinkedIn
@router.post("/drafts/{draft_id}/linkedin-sent")
def linkedin_sent(draft_id: str, request: Request) -> Any:
    from app import pipeline
    from app.engagement.linkedin import mark_sent
    draft = db.get("drafts", draft_id, Draft)
    if not draft:
        raise HTTPException(404, detail={"code": "not_found"})
    lead = _lead(draft.leadId)
    from app.governance import auth as gov_auth, gates
    who = gov_auth.actor(request, "reviewer")
    try:
        msg = mark_sent(draft, lead)
    except ValueError as exc:
        if str(exc) in ("contact_blocked", "campaign_paused"):
            gates.record(who, draft, lead.id, "linkedin", blocked=str(exc))
        raise HTTPException(409, detail={"code": str(exc)}) from exc
    gates.record(who, draft, lead.id, "linkedin", message_id=msg.id)
    pipeline.emit("message.sent", lead, messageId=msg.id, draftId=draft.id, kind="linkedin_note", channel="linkedin", subject=msg.subject)
    return msg


# PhantomBuster connection status and phantoms
@router.get("/integrations/phantombuster")
async def phantombuster_status() -> dict[str, Any]:
    from app.integrations.phantombuster import PhantomBusterError, list_phantoms
    try:
        phantoms = await list_phantoms()
    except PhantomBusterError as exc:
        return {"connected": False, "error": str(exc), "phantoms": []}
    return {"connected": True, "phantoms": phantoms}


# Import the latest export of a phantom (e.g. Sales Navigator search) as leads; rows without an email are reported, not invented
@router.post("/integrations/phantombuster/import")
async def phantombuster_import(body: ImportInput) -> dict[str, Any]:
    from app import pipeline
    from app.integrations.phantombuster import PhantomBusterError, latest_rows
    try:
        rows = await latest_rows(body.agentId)
    except PhantomBusterError as exc:
        raise HTTPException(502, detail={"code": str(exc)}) from exc
    results: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        email = r.get("email") or r.get("mail") or r.get("professionalEmail")
        name = r.get("fullName") or " ".join(x for x in (r.get("firstName"), r.get("lastName")) if x) or ""
        company = r.get("companyName") or r.get("company") or ""
        if not email or not name or not company:
            results.append({"row": i + 1, "status": "needs_email" if not email else "incomplete", "name": name})
            continue
        inp = LeadInput(sourceRecordId=f"phantombuster:{body.agentId}:{r.get('profileUrl') or i}", source="linkedin", fullName=name,
                        email=email, companyName=company, country=(r.get("country") or r.get("location") or "")[:2].upper(),
                        productId=body.productId, role=r.get("title") or r.get("jobTitle"), contactPermission="allowed")
        out = await pipeline.ingest_lead(inp, str(uuid.uuid4()))
        results.append({"row": i + 1, "status": out.get("disposition"), "leadId": out.get("leadId"), "name": name})
    return {"rows": len(rows), "imported": sum(1 for x in results if x["status"] == "created"),
            "needsEmail": sum(1 for x in results if x["status"] == "needs_email"), "results": results}


QUEUED_TEXT = "Queued. It sends automatically as soon as they accept your LinkedIn connection request."
SENDING_TEXT = "Sending on LinkedIn. It appears in the timeline once LinkedIn confirms it."
RETRY_TEXT = "LinkedIn didn't confirm this send yet. It's still approved, so you can send it again shortly."
_confirmations: set[asyncio.Task] = set()


# Set the short status sentence shown on the approved draft card
def _note_status(draft_id: str, text: str, status: Optional[str] = None) -> None:
    draft = db.get("drafts", draft_id, Draft)
    if draft:
        db.put("drafts", draft.model_copy(update={"nextAction": text, **({"status": status} if status else {})}))


# Record a LinkedIn send only once its run log confirms delivery; queue it for acceptance when the profile isn't connected yet
async def _confirm(draft_id: str, container: str, target: str, direct: bool) -> None:
    from app import pipeline
    from app.contracts import Message
    from app.integrations.phantombuster import PhantomBusterError, queue_follow_up, run_outcome
    outcome = "running"
    for _ in range(36):
        await asyncio.sleep(10)
        try:
            outcome = await run_outcome(container)
        except PhantomBusterError:
            continue
        if outcome != "running":
            break
    draft = db.get("drafts", draft_id, Draft)
    lead = db.get("leads", draft.leadId, Lead) if draft else None
    if not draft or not lead:
        return
    if outcome == "sent":
        msg = Message(leadId=lead.id, threadId=lead.threadId, direction="outbound", subject="LinkedIn message" if direct else "LinkedIn connection request",
                      body=draft.body, draftId=draft.id, enrollmentId=draft.enrollmentId, origin="phantombuster", transport="phantombuster",
                      deliveryStatus="accepted", providerMessageId=container, deliveredTo=target, intendedTo=f"LinkedIn: {lead.input.fullName}")
        db.put("messages", msg)
        from app.governance import gates
        gates.record(("system", "system"), draft, lead.id, "linkedin", message_id=msg.id)
        _note_status(draft.id, "Sent on LinkedIn", "SIMULATED_SENT")
        pipeline.emit("message.sent", lead, messageId=msg.id, draftId=draft.id, kind="linkedin_note", channel="linkedin", subject=msg.subject)
    elif outcome == "not_connected":
        try:
            await queue_follow_up(target, draft.body)
            _note_status(draft.id, QUEUED_TEXT)
        except PhantomBusterError:
            _note_status(draft.id, RETRY_TEXT)
    else:
        _note_status(draft.id, RETRY_TEXT)


# Send an approved note on LinkedIn (allow-listed test profile only): invite if never contacted, else direct message (queued for acceptance if LinkedIn refuses)
@router.post("/drafts/{draft_id}/linkedin-launch", status_code=202)
async def linkedin_launch(draft_id: str, request: Request) -> dict[str, Any]:
    from app import config
    from app.engagement.drafting import content_hash
    from app.integrations.phantombuster import PhantomBusterError, connection_state, launch_message, launch_outreach
    draft = db.get("drafts", draft_id, Draft)
    if not draft or draft.kind != "linkedin_note":
        raise HTTPException(404, detail={"code": "not_found"})
    lead = _lead(draft.leadId)
    if draft.status != "APPROVED" or draft.contentHash != content_hash(draft.subject, draft.body):
        raise HTTPException(409, detail={"code": "not_approved"})
    from app.governance import auth as gov_auth, gates
    who = gov_auth.actor(request, "reviewer")
    if blocked := gates.blocked_reason(draft, lead):
        gates.record(who, draft, lead.id, "linkedin", blocked=blocked)
        raise HTTPException(409, detail={"code": blocked})
    target = config.env("LINKEDIN_TEST_PROFILE_URL")
    try:
        state = await connection_state(target)
        container = await (launch_outreach if state == "none" else launch_message)(target, draft.body)
    except PhantomBusterError:
        _note_status(draft.id, RETRY_TEXT)
        return {"status": "queued", "text": RETRY_TEXT}
    _note_status(draft.id, SENDING_TEXT)
    gates.record(who, draft, lead.id, "linkedin", launched=True)
    task = asyncio.create_task(_confirm(draft.id, container, target, state != "none"))
    _confirmations.add(task)
    task.add_done_callback(_confirmations.discard)
    return {"status": "sending", "text": SENDING_TEXT}


# Check a LinkedIn send run (status + log tail)
@router.get("/integrations/phantombuster/runs/{container_id}")
async def linkedin_run(container_id: str) -> dict[str, Any]:
    from app.integrations.phantombuster import PhantomBusterError, run_status
    try:
        return await run_status(container_id)
    except PhantomBusterError as exc:
        raise HTTPException(502, detail={"code": str(exc)}) from exc


# Whether automated LinkedIn sends to the test profile are available (no secrets)
@router.get("/integrations/linkedin")
def linkedin_status() -> dict[str, Any]:
    from app import config
    return {"testProfileSet": bool(config.env("LINKEDIN_TEST_PROFILE_URL")), "phantomConfigured": bool(config.env("PHANTOMBUSTER_API_KEY"))}
