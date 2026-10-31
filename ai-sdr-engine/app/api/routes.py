# app/api/routes.py - Public /v1 HTTP API: leads, imports, replies, drafts, campaigns, tasks, dashboard, live events
from __future__ import annotations

import csv
import io
import json
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, TypeVar

from fastapi.responses import HTMLResponse
from fastapi import APIRouter, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from app import db, events, pipeline
from app.contracts import CampaignInput, Draft, LeadInput, ReplyInput, Task, TaskStatus, new_id
from app.crm import mock_crm, outbox
from app.governance import auth as gov_auth, store as gov_store

router = APIRouter(prefix="/v1")


@router.get("/enrichment/status", tags=["enrichment"])
def enrichment_status():
    from app import config
    return {"provider": config.ENRICHMENT_PROVIDER, "configured": bool(config.APOLLO_API_KEY),
            "companyEnrichment": config.ENRICHMENT_PROVIDER == "apollo" and bool(config.APOLLO_API_KEY),
            "peopleEnrichment": config.APOLLO_PEOPLE_ENRICHMENT,
            "cacheHours": 6, "syntheticDomainsSkipped": True,
            "demoProfilesEnabled": config.DEMO_MODE}

T = TypeVar("T")

MAX_IMPORT_ROWS = 100
REQUIRED_CSV_HEADERS = (
    "sourceRecordId", "source", "fullName", "email", "companyName",
    "country", "productId", "inquiry", "contactPermission",
)


class RunRequest(BaseModel):
    kind: str


class DraftEdit(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None


class ApproveRequest(BaseModel):
    reviewer: str = "sdr.omar"
    contentHash: Optional[str] = None


class RejectRequest(BaseModel):
    reason: str = ""


class ClockRequest(BaseModel):
    advanceHours: float


class TaskUpdate(BaseModel):
    status: TaskStatus


# JSON error body shared by every non-2xx response from this router
def error(status: int, code: str, message: str = "") -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message or code}}, status_code=status)


# Run a sync domain call, mapping ValueError(code) -> 409 and NotFound -> 404
def guarded(fn: Callable[..., T], *args: Any) -> T | JSONResponse:
    try:
        return fn(*args)
    except pipeline.NotFound as exc:
        return error(404, "not_found", str(exc))
    except ValidationError:
        raise
    except ValueError as exc:
        return error(409, str(exc))


# Await an async domain call with the same error mapping as guarded()
async def aguarded(coro: Awaitable[T]) -> T | JSONResponse:
    try:
        return await coro
    except pipeline.NotFound as exc:
        return error(404, "not_found", str(exc))
    except ValidationError:
        raise
    except ValueError as exc:
        return error(409, str(exc))


# Load a draft for an action or return a 404 response
def draft_or_404(draft_id: str) -> Draft | JSONResponse:
    draft = db.get("drafts", draft_id, Draft)
    return draft if draft else error(404, "not_found", f"draft {draft_id}")


# ---------------------------------------------------------------- leads + imports


# Create (or merge) a lead; processing continues asynchronously
@router.post("/leads", status_code=201, tags=["leads"])
async def create_lead(inp: LeadInput, idempotency_key: Optional[str] = Header(default=None)) -> Any:
    result = await aguarded(pipeline.ingest_lead(inp, idempotency_key))
    if isinstance(result, JSONResponse):
        return result
    return JSONResponse(result, status_code=200 if result.get("idempotentReplay") else 201)


# Bulk import marketing leads from CSV with per-row validation
@router.post("/imports", status_code=202, tags=["leads"])
async def import_csv(file: UploadFile = File(...), source: Optional[str] = Form(default=None)) -> Any:
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    missing = [h for h in REQUIRED_CSV_HEADERS if h not in headers and not (h == "source" and source)]
    if missing:
        return error(400, "missing_headers", f"CSV is missing required headers: {', '.join(missing)}")
    rows = list(reader)
    if len(rows) > MAX_IMPORT_ROWS:
        return error(413, "too_many_rows", f"{len(rows)} rows; the limit is {MAX_IMPORT_ROWS}")
    results = []
    for idx, raw in enumerate(rows, start=1):
        results.append(await import_row(idx, raw, source))
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("imported", "existing", "rejected")}
    return JSONResponse({"importId": new_id("imp"), "counts": counts, "rows": results}, status_code=202)


# Validate one CSV row into a LeadInput and ingest it
async def import_row(idx: int, raw: dict[str, Any], default_source: Optional[str]) -> dict[str, Any]:
    data = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k}
    data = {k: v for k, v in data.items() if v not in ("", None)}
    if default_source and not data.get("source"):
        data["source"] = default_source
    errors: list[str] = []
    if data.get("email") and "@" not in data["email"]:
        errors.append("email: not a valid address")
    try:
        inp = LeadInput.model_validate(data)
    except ValidationError as exc:
        errors.extend(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    if errors:
        return {"row": idx, "status": "rejected", "errors": errors}
    result = await pipeline.ingest_lead(inp, None)
    status = "existing" if result["disposition"] == "existing" else "imported"
    return {"row": idx, "status": status, "leadId": result["leadId"], "disposition": result["disposition"],
            "runId": result["runId"]}


# List lead summaries with optional filters
@router.get("/leads", tags=["leads"])
def list_leads(stage: Optional[str] = None, source: Optional[str] = None, fit: Optional[str] = None,
               productId: Optional[str] = None) -> dict[str, Any]:
    return {"items": pipeline.list_leads(stage, source, fit, productId)}


# Full lead detail with assessments, drafts, messages, tasks, enrollments and runs
@router.get("/leads/{lead_id}", tags=["leads"])
def get_lead(lead_id: str) -> Any:
    return guarded(pipeline.lead_detail, lead_id)


# Ingest an inbound reply (stops sequences first, then assesses and routes asynchronously)
@router.post("/leads/{lead_id}/replies", status_code=202, tags=["leads"])
async def post_reply(lead_id: str, reply: ReplyInput) -> Any:
    return await aguarded(pipeline.ingest_reply(lead_id, reply))


# Re-run one pipeline stage for a lead
@router.post("/leads/{lead_id}/runs", status_code=202, tags=["leads"])
async def post_run(lead_id: str, req: RunRequest) -> Any:
    return await aguarded(pipeline.rerun(lead_id, req.kind))


# One pipeline run with its step log
@router.get("/runs/{run_id}", tags=["runs"])
def get_run(run_id: str) -> Any:
    return guarded(pipeline.get_run, run_id)


# ---------------------------------------------------------------- drafts


# Approval queue (filter by status / lead)
@router.get("/drafts", tags=["drafts"])
def list_drafts(status: Optional[str] = None, leadId: Optional[str] = None) -> dict[str, Any]:
    drafts = db.list_("drafts", Draft, lead_id=leadId, limit=100000)
    items = [d for d in drafts if not status or d.status == status]
    return {"items": items}


# Edit a draft (creates a new version and invalidates prior approval)
@router.patch("/drafts/{draft_id}", tags=["drafts"])
async def edit_draft(draft_id: str, req: DraftEdit, request: Request) -> Any:
    from app.engagement.approvals import edit_draft as do_edit

    draft = draft_or_404(draft_id)
    if isinstance(draft, JSONResponse):
        return draft
    result = guarded(do_edit, draft, req.subject, req.body)
    if not isinstance(result, JSONResponse):
        gov_store.audit(*gov_auth.actor(request, "reviewer"), "draft.edited", "draft", draft.id, draft.leadId,
                        before={"version": draft.version, "subject": draft.subject, "flags": draft.guardrailFlags},
                        after={"version": result.version, "subject": result.subject, "flags": result.guardrailFlags})
        events.publish("draft.edited", result.leadId, {"draftId": result.id, "version": result.version,
                                                       "summary": f"draft edited -> v{result.version}"})
    return result


# Approve a draft for sending (contentHash must match the reviewed version)
@router.post("/drafts/{draft_id}/approve", tags=["drafts"])
async def approve(draft_id: str, req: ApproveRequest, request: Request) -> Any:
    from app.engagement.approvals import approve_draft

    draft = draft_or_404(draft_id)
    if isinstance(draft, JSONResponse):
        return draft
    who, role = gov_auth.actor(request, req.reviewer)
    result = guarded(approve_draft, draft, who if role != "not_signed_in" else req.reviewer, req.contentHash)
    if not isinstance(result, JSONResponse):
        gov_store.audit(who, role, "draft.approved", "draft", draft.id, draft.leadId, before={"status": draft.status},
                        after={"status": result.status, "version": result.version, "contentHash": result.contentHash})
        events.publish("draft.approved", result.leadId, {"draftId": result.id, "reviewer": req.reviewer,
                                                         "summary": f"{result.kind} approved by {req.reviewer}"})
    return result


# Reject a draft
@router.post("/drafts/{draft_id}/reject", tags=["drafts"])
async def reject(draft_id: str, req: RejectRequest, request: Request) -> Any:
    from app.engagement.approvals import reject_draft

    draft = draft_or_404(draft_id)
    if isinstance(draft, JSONResponse):
        return draft
    result = guarded(reject_draft, draft, req.reason)
    if not isinstance(result, JSONResponse):
        gov_store.audit(*gov_auth.actor(request, "reviewer"), "draft.rejected", "draft", draft.id, draft.leadId,
                        before={"status": draft.status}, after={"status": result.status}, detail=req.reason)
        events.publish("draft.rejected", result.leadId, {"draftId": result.id, "reason": req.reason,
                                                         "summary": f"{result.kind} rejected: {req.reason}"})
    return result


# Branded HTML rendering of a draft exactly as the recipient would see it
@router.get("/drafts/{draft_id}/preview", tags=["drafts"], response_class=HTMLResponse)
def preview_draft(draft_id: str) -> HTMLResponse:
    from app import db
    from app.contracts import Draft
    from app.engagement import email_html

    draft = db.get("drafts", draft_id, Draft)
    if not draft:
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(email_html.render(draft.subject, draft.body))


# Send an approved draft through the configured email transport
@router.post("/drafts/{draft_id}/send", tags=["drafts"])
async def send(draft_id: str, request: Request) -> Any:
    return await aguarded(pipeline.send_draft(draft_id, actor=gov_auth.actor(request, "reviewer")))


# ---------------------------------------------------------------- campaigns + clock


# Create an outbound campaign: cohort -> enrollments -> touch drafts awaiting approval
@router.post("/campaigns", status_code=201, tags=["campaigns"])
async def create_campaign(inp: CampaignInput, request: Request) -> Any:
    result = await aguarded(pipeline.create_campaign(inp))
    if isinstance(result, dict) and result.get("campaign"):
        c = result["campaign"]
        gov_store.audit(*gov_auth.actor(request, "workspace user"), "campaign.launched", "campaign", c.id if hasattr(c, "id") else c.get("id"),
                        after={"name": inp.name, "cohort": inp.cohort.model_dump(), "touchDays": inp.touchDays,
                               "enrolled": len(result.get("enrollments") or [])})
    return result


# All campaigns with enrollment counts by state, newest first
@router.get("/campaigns", tags=["campaigns"])
def list_campaigns() -> dict[str, Any]:
    from app import db
    from app.contracts import Campaign, Enrollment

    items = []
    for c in reversed(db.list_("campaigns", Campaign)):
        enrollments = [e for e in (db.get("enrollments", eid, Enrollment) for eid in c.enrollmentIds) if e]
        states = [e.state for e in enrollments]
        items.append({**c.model_dump(mode="json"), "enrolled": len(states),
                      "states": {s: states.count(s) for s in set(states)},
                      "leadIds": [e.leadId for e in enrollments]})
    return {"items": items}


# Campaign detail with enrollments and drafts
@router.get("/campaigns/{campaign_id}", tags=["campaigns"])
def get_campaign(campaign_id: str) -> Any:
    return guarded(pipeline.campaign_detail, campaign_id)


# Current demo clock
@router.get("/demo/clock", tags=["demo"])
def get_clock() -> dict[str, Any]:
    from app.engagement import clock

    return {"now": clock.demo_now_iso(), "offsetHours": clock.offset_hours()}


# Advance the demo clock and send approved due touches
@router.post("/demo/clock", tags=["demo"])
async def advance_clock(req: ClockRequest) -> Any:
    return await aguarded(pipeline.advance_clock(req.advanceHours))


# Wipe every store: database, clock offset, mock CRM and the recent-event buffer
@router.post("/demo/reset", tags=["demo"])
def reset(request: Request) -> dict[str, Any]:
    from app.engagement import clock

    gov_store.audit(*gov_auth.actor(request, "workspace user"), "demo.reset", "workspace", None,
                    detail="Workspace data cleared; the audit log is kept.")
    db.reset()
    clock.reset()
    mock_crm.clear()
    events.clear()
    events.publish("demo.reset", None, {"summary": "demo data reset"})
    return {"reset": True}


# ---------------------------------------------------------------- tasks, alerts, dashboard, CRM


# AE alerts and SDR tasks (filter by status / type / owner / lead)
@router.get("/tasks", tags=["tasks"])
def list_tasks(status: Optional[str] = None, type: Optional[str] = None, ownerId: Optional[str] = None,
               leadId: Optional[str] = None) -> dict[str, Any]:
    tasks = db.list_("tasks", Task, lead_id=leadId, limit=100000)
    items = [t for t in tasks if (not status or t.status == status) and (not type or t.type == type)
             and (not ownerId or t.ownerId == ownerId)]
    return {"items": list(reversed(items))}


# Update a task; accepting an ae_handoff moves the lead to OPPORTUNITY
@router.patch("/tasks/{task_id}", tags=["tasks"])
def update_task(task_id: str, req: TaskUpdate) -> Any:
    return guarded(pipeline.update_task, task_id, req.status)


# Open AE handoff alerts with HandoffCards
@router.get("/alerts", tags=["tasks"])
def list_alerts() -> dict[str, Any]:
    return {"items": list(reversed(pipeline.alerts()))}


# Funnel dashboard
@router.get("/dashboard", tags=["dashboard"])
def get_dashboard() -> dict[str, Any]:
    return pipeline.dashboard()


# CRM webhook outbox with delivery status
@router.get("/crm/outbox", tags=["crm"])
def crm_outbox(limit: int = 200) -> dict[str, Any]:
    return {"items": outbox.outbox(limit)}


# ---------------------------------------------------------------- live events


# Server-sent events feed of every pipeline step and CRM push
@router.get("/events/stream", tags=["events"])
async def stream() -> StreamingResponse:
    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Encode the live event stream as SSE frames with keepalive comments
async def sse() -> AsyncIterator[str]:
    yield ": connected\n\n"
    async for event in events.subscribe(keepalive=15.0):
        yield ": ping\n\n" if event is None else f"data: {json.dumps(event, default=str)}\n\n"


# Recent live events (oldest first)
@router.get("/events/recent", tags=["events"])
def recent_events(limit: int = 100, leadId: Optional[str] = None) -> dict[str, Any]:
    return {"items": events.recent(limit, leadId)}
