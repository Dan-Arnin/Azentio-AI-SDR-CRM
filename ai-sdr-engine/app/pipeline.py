# app/pipeline.py - Orchestrator: the only layer that moves Lead.stage, creates Tasks and emits CRM events
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from pydantic import BaseModel

from app import config, db, events, jobs, workflow_requests
from app.contracts import (
    BANT_KEYS, Assessment, Campaign, CampaignInput, Draft, DraftKind, Enrollment, HandoffCard,
    Lead, LeadInput, Message, ReplyInput, Run, Stage, Task, TaskStatus, TaskType, now_iso,
)
from app.crm import outbox

PRODUCT_LABELS = {"lending_platform": "Lending Platform", "islamic_banking": "Islamic Banking", "aml_compliance": "AMLOCK Financial Crime & Compliance", "unknown": "an Azentio product"}
INBOUND_DRAFT_KIND: dict[str, Optional[DraftKind]] = {
    "website": "qualifying_email",
    "webinar": "event_followup",
    "event": "event_followup",
    "ad": "ad_followup",
    "marketing_csv": "qualifying_email",
    "target_account": None,
}
PEOPLE = {"ae.sara": "Sara Malik", "ae.khalid": "Khalid Al Mansoori", "ae.priya": "Priya Raman", "sdr.omar": "Nadia Karim"}
GCC = frozenset({"AE", "SA", "BH", "QA", "KW", "OM"})
# AE territory/product rules, first match wins: (countries or None = any, products or None = any, owner, reason)
AE_TERRITORY_RULES: tuple[tuple[Optional[frozenset], Optional[frozenset], str, str], ...] = (
    (GCC, frozenset({"islamic_banking"}), "ae.khalid", "covers Islamic banking in the GCC"),
    (frozenset({"IN"}), None, "ae.priya", "covers India"),
    (GCC, None, "ae.sara", "covers lending and AML/compliance in the GCC"),
    (None, None, "ae.sara", "is the default AE for accounts outside a mapped territory"),
)
COUNTRY_ALIASES = {"UAE": "AE", "UNITED ARAB EMIRATES": "AE", "SAUDI ARABIA": "SA", "KSA": "SA", "BAHRAIN": "BH", "QATAR": "QA",
                   "KUWAIT": "KW", "OMAN": "OM", "INDIA": "IN"}
TIMELINE_EXCERPT_CHARS = 160
REVIEW_STAGES = ("REP_REVIEW", "HUMAN_REVIEW")
ACTIVE_CONVERSATION_STAGES = ("ENGAGING", "QUALIFYING", "REP_REVIEW", "HUMAN_REVIEW", "OPPORTUNITY")
PENDING_DRAFT_STATUSES = ("DRAFT", "APPROVED")
OPEN_TASK_STATUSES = ("open", "acknowledged")

_background: set[asyncio.Task] = set()
_slots = asyncio.Semaphore(4)


class NotFound(LookupError):
    """Raised when a referenced record does not exist (mapped to HTTP 404)."""


class Suppression(BaseModel):
    """Suppression-list entry keyed by normalized email."""
    id: str
    leadId: str
    email: str
    reason: str
    at: str


# ---------------------------------------------------------------- helpers: records


# Load a lead or raise NotFound
def get_lead(lead_id: str) -> Lead:
    lead = db.get("leads", lead_id, Lead)
    if lead is None:
        raise NotFound(f"lead {lead_id}")
    return lead


# Load a draft or raise NotFound
def get_draft(draft_id: str) -> Draft:
    draft = db.get("drafts", draft_id, Draft)
    if draft is None:
        raise NotFound(f"draft {draft_id}")
    return draft


# Load a run or raise NotFound
def get_run(run_id: str) -> Run:
    run = db.get("runs", run_id, Run)
    if run is None:
        raise NotFound(f"run {run_id}")
    if jobs._runner is not None and run.status in ("running", "queued"):
        job = jobs._runner.get(run_id)
        if job and job["status"] == "failed":
            run.status, run.errorCode, run.finishedAt = "failed", job.get("errorCode") or "review_needs_attention", now_iso()
            db.put("runs", run)
    return run


# Latest persisted assessment for a lead
def latest_assessment(lead: Lead) -> Optional[Assessment]:
    if lead.latestAssessmentId:
        return db.get("assessments", lead.latestAssessmentId, Assessment)
    return None


# Append a history entry, bump version on material change, and persist the lead
def save_lead(lead: Lead, event_type: str, detail: str, material: bool = True) -> Lead:
    lead.history.append({"at": now_iso(), "type": event_type, "detail": detail})
    if material:
        lead.version += 1
    lead.updatedAt = now_iso()
    db.put("leads", lead)
    return lead


# Compact lead snapshot carried on every CRM payload and live event
def snapshot(lead: Lead) -> dict[str, Any]:
    asm = latest_assessment(lead)
    return {
        "leadId": lead.id,
        "version": lead.version,
        "fullName": lead.input.fullName,
        "email": lead.normalizedEmail,
        "companyName": lead.input.companyName,
        "country": lead.input.country,
        "productId": lead.input.productId,
        "source": lead.input.source,
        "stage": lead.stage,
        "ownerId": lead.ownerId,
        "accountOwnerId": lead.accountOwnerId,
        "fit": lead.icp.fit if lead.icp else None,
        "icpScore": lead.icp.score if lead.icp else None,
        "qualificationScore": asm.qualificationScore if asm else None,
        "route": asm.route if asm else None,
        "suppressed": lead.suppressed,
        "flags": lead.flags,
        "updatedAt": lead.updatedAt,
    }


# Emit a CRM event whose payload always includes the lead snapshot
def emit(type_: str, lead: Lead, **extra: Any) -> None:
    outbox.emit(type_, lead.id, {"lead": snapshot(lead), "leadName": lead.input.fullName, **extra})


# Move a lead to a new stage (history + CRM lead.stage_changed); no-op if unchanged
def set_stage(lead: Lead, stage: Stage, reason: str) -> Lead:
    if lead.stage == stage:
        return lead
    previous = lead.stage
    lead.stage = stage
    save_lead(lead, "stage_changed", f"{previous} -> {stage}: {reason}")
    emit("lead.stage_changed", lead, fromStage=previous, toStage=stage, reason=reason)
    return lead


# Keep a strong reference to a background coroutine until it finishes
def spawn(coro: Any) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


# ---------------------------------------------------------------- helpers: runs


# Create and persist a running Run
def start_run(kind: str, lead_id: Optional[str] = None) -> Run:
    run = Run(kind=kind, leadId=lead_id, status="running")
    db.put("runs", run)
    events.publish("run.started", lead_id, {"runId": run.id, "kind": kind, "summary": f"{kind} run started"})
    return run


# Append a step to a run, persist it and publish it live
def add_step(run: Run, name: str, status: str, detail: str, started: Optional[float] = None) -> None:
    duration = int((time.perf_counter() - started) * 1000) if started else 0
    step = {"name": name, "status": status, "detail": detail, "at": now_iso(), "durationMs": duration}
    run.steps.append(step)
    db.put("runs", run)
    events.publish("run.step", run.leadId, {"runId": run.id, "kind": run.kind, **step, "summary": f"{name}: {detail}"})


# Time a pipeline step; the body may set info["detail"]/info["status"]; failures are recorded then re-raised
@contextmanager
def step(run: Run, name: str) -> Iterator[dict[str, Any]]:
    from app.llm import calllog
    started = time.perf_counter()
    info: dict[str, Any] = {"detail": "", "status": "ok"}
    token = calllog.bind(run.leadId, name)
    try:
        yield info
    except Exception as exc:
        add_step(run, name, "failed", f"{type(exc).__name__}: operation_failed", started)
        raise
    finally:
        calllog.unbind(token)
    add_step(run, name, info["status"], info["detail"], started)


# Mark a run finished, persist and publish
def finish_run(run: Run, status: str = "succeeded", error_code: Optional[str] = None) -> Run:
    run.status = status
    run.errorCode = error_code
    run.finishedAt = now_iso()
    db.put("runs", run)
    events.publish("run.finished", run.leadId, {
        "runId": run.id, "kind": run.kind, "status": status, "errorCode": error_code,
        "summary": f"{run.kind} run {status}" + (f" ({error_code})" if error_code else ""),
    })
    return run


# Flag a run as fixture-mode if any artifact it produced came from fixtures
def note_mode(run: Run, mode: str) -> None:
    if mode == "fixture":
        run.mode = "fixture"


# ---------------------------------------------------------------- helpers: tasks, cards, suppression


# Create a task, or refresh the open task of the same type for this lead
def upsert_task(lead: Lead, type_: TaskType, owner: str, title: str, card: Optional[HandoffCard] = None) -> Task:
    for existing in db.list_("tasks", Task, lead_id=lead.id):
        if existing.type == type_ and existing.status in OPEN_TASK_STATUSES:
            existing.ownerId, existing.title, existing.card = owner, title, card or existing.card
            db.put("tasks", existing)
            events.publish("task.updated", lead.id, {"taskId": existing.id, "type": type_, "summary": title})
            return existing
    task = Task(leadId=lead.id, type=type_, ownerId=owner, title=title, card=card)
    db.put("tasks", task)
    events.publish("task.created", lead.id, {"taskId": task.id, "type": type_, "ownerId": owner, "summary": title})
    return task


# Deterministic 1-3 sentence summary for the handoff card
def card_summary(lead: Lead, asm: Assessment, title: Optional[str]) -> str:
    who = f"{lead.input.fullName}" + (f" ({title})" if title else "")
    product = PRODUCT_LABELS.get(lead.input.productId, lead.input.productId)
    intent = asm.replyIntent or "no reply yet"
    conf = f" ({asm.intentConfidence:.0%} confidence)" if asm.intentConfidence is not None else ""
    confirmed = [k.capitalize() for k in BANT_KEYS if asm.bant.get(k) and asm.bant[k].status == "confirmed"]
    unresolved = [k.capitalize() for k in asm.missingFields] or ["none"]
    strength = f", {asm.intentStrength} intent" if asm.replyIntent else ""
    first = f"{who} at {lead.input.companyName} is evaluating {product}; reply intent is {intent}{conf}{strength}."
    second = f"BANT confirmed: {', '.join(confirmed) or 'none'}; unresolved: {', '.join(unresolved)}."
    third = f"Recommended next step: {asm.nextAction}." if asm.nextAction else ""
    return " ".join(s for s in (first, second, third) if s)


# Display name for an owner id
def person_name(owner_id: Optional[str]) -> str:
    return PEOPLE.get(owner_id or "", owner_id or "unassigned")


# Engagement history for the card: counts, first touch, last reply, campaign and the last 6 messages (in recorded order)
def engagement_summary(lead: Lead) -> dict[str, Any]:
    msgs = db.list_("messages", Message, lead_id=lead.id, limit=100000)
    sent = [m for m in msgs if m.direction == "outbound"]
    received = [m for m in msgs if m.direction == "inbound"]
    campaign_name = None
    for enr in db.find("enrollments", Enrollment, leadId=lead.id):
        if campaign := db.get("campaigns", enr.campaignId, Campaign):
            campaign_name = campaign.name
    timeline = []
    for m in msgs[-6:]:
        text = " ".join((m.body.split("\n\nKind regards,")[0] if m.direction == "outbound" else m.body).split())
        excerpt = text if len(text) <= TIMELINE_EXCERPT_CHARS else text[:TIMELINE_EXCERPT_CHARS - 1].rstrip() + "…"
        timeline.append({"at": m.at, "direction": m.direction, "subject": m.subject, "excerpt": excerpt})
    return {
        "emailsSent": len(sent), "repliesReceived": len(received),
        "firstTouchAt": sent[0].at if sent else None, "lastReplyAt": received[-1].at if received else None,
        "campaignName": campaign_name, "timeline": timeline,
    }


# Build the AE/SDR handoff card from lead, enrichment and assessment
def build_card(lead: Lead, asm: Assessment, owner: str, routing_reason: str = "") -> HandoffCard:
    enr = lead.enrichment
    person = enr.person if enr else {}
    company = enr.company if enr else {}
    title = lead.input.role or person.get("title")
    contact = {
        "name": lead.input.fullName, "email": lead.normalizedEmail, "title": title,
        "phone": lead.input.phone, "country": lead.input.country,
        "linkedin": person.get("linkedin_url") or person.get("linkedin"),
    }
    account = {
        "name": company.get("name") or lead.input.companyName,
        "domain": company.get("domain") or lead.input.companyDomain,
        "institutionType": company.get("institution_type") or company.get("institutionType") or lead.input.institutionType,
        "employeeCount": company.get("employee_count") or company.get("employeeCount") or lead.input.employeeCount,
        "country": company.get("country") or lead.input.country,
        "accountKey": lead.accountKey,
    }
    models = {"generation": config.GENERATION_MODEL, "judge": config.JUDGE_MODEL}
    if asm.decision:
        models.update({"decision": asm.decision.model, "decisionProvider": asm.decision.provider})
    return HandoffCard(
        leadId=lead.id, contact=contact, account=account, productId=lead.input.productId,
        source=lead.input.source, owner=owner, icp=asm.icp or lead.icp, bant=asm.bant,
        qualificationScore=asm.qualificationScore, replyIntent=asm.replyIntent, threadId=lead.threadId,
        unresolved=asm.missingFields, recommendedNextAction=asm.nextAction,
        summary=card_summary(lead, asm, title), policyVersion=asm.policyVersion, models=models,
        engagement=engagement_summary(lead), routingReason=routing_reason,
    )


# Suppress a lead: flag, stop sequences, stale pending drafts, record suppression, notify CRM
def suppress(lead: Lead, reason: str, run: Run) -> Lead:
    from app.engagement import sequences

    with step(run, "suppress") as info:
        lead.suppressed = True
        lead.suppressionReason = reason
        stopped = sequences.stop_on_reply(lead.id, "suppression")
        staled = stale_pending_drafts(lead.id)
        db.put("suppressions", Suppression(id=lead.normalizedEmail, leadId=lead.id, email=lead.normalizedEmail,
                                           reason=reason, at=now_iso()))
        save_lead(lead, "suppressed", reason)
        set_stage(lead, "SUPPRESSED", reason)
        emit("lead.suppressed", lead, reason=reason, stoppedEnrollments=[e.id for e in stopped], staleDrafts=staled)
        info["detail"] = f"{reason}; {len(stopped)} enrollment(s) stopped, {staled} draft(s) made stale"
    return lead


# Mark every unsent (DRAFT/APPROVED) draft for a lead STALE; returns how many changed
def stale_pending_drafts(lead_id: str) -> int:
    staled = 0
    for draft in db.list_("drafts", Draft, lead_id=lead_id):
        if draft.status in PENDING_DRAFT_STATUSES:
            db.put("drafts", draft.model_copy(update={"status": "STALE"}))
            staled += 1
    return staled


# Close open SDR-side tasks once a lead is handed to an AE
def close_sdr_tasks(lead_id: str) -> None:
    for task in db.list_("tasks", Task, lead_id=lead_id):
        if task.type in ("sdr_followup", "sdr_review") and task.status in OPEN_TASK_STATUSES:
            task.status = "done"
            db.put("tasks", task)
            events.publish("task.updated", lead_id, {"taskId": task.id, "type": task.type, "status": "done",
                                                     "summary": f"{task.type} closed: lead handed to AE"})


# ---------------------------------------------------------------- ingest


# Source-history entry recorded on a lead for every inbound touchpoint
def source_entry(inp: LeadInput, idem_key: Optional[str]) -> dict[str, Any]:
    return {
        "source": inp.source, "sourceRecordId": inp.sourceRecordId, "eventName": inp.eventName,
        "campaignId": inp.campaignId, "assetName": inp.assetName, "idempotencyKey": idem_key,
        "at": inp.submittedAt or now_iso(),
    }


# Find a lead that already absorbed this Idempotency-Key or source record
def find_replay(inp: LeadInput, idem_key: Optional[str]) -> Optional[Lead]:
    for lead in db.list_("leads", Lead, limit=100000):
        for src in lead.sources:
            if idem_key and src.get("idempotencyKey") == idem_key:
                return lead
            if src.get("source") == inp.source and src.get("sourceRecordId") == inp.sourceRecordId:
                return lead
    return None


# Create and persist a brand-new lead record
def create_lead(inp: LeadInput, idem_key: Optional[str], account_key: Optional[str], stage: Stage = "NEW") -> Lead:
    email = inp.email.strip().lower()
    lead = Lead(input=inp, normalizedEmail=email, accountKey=account_key or email.split("@")[-1],
                sources=[source_entry(inp, idem_key)], stage=stage)
    lead.history.append({"at": now_iso(), "type": "created", "detail": f"from {inp.source}:{inp.sourceRecordId}"})
    db.put("leads", lead)
    return lead


# Resolve identity and create/merge the lead; returns (lead, disposition) without any outreach
def resolve_identity(inp: LeadInput, idem_key: Optional[str], run: Run) -> tuple[Lead, str]:
    from app.engine.identity import check_identity, is_suppressed

    with step(run, "identity") as info:
        ident = check_identity(inp)
        info["detail"] = f"{ident.match}" + (f" ({'; '.join(ident.reasons)})" if ident.reasons else "")

    if ident.match == "existing_lead" and ident.leadId and (lead := db.get("leads", ident.leadId, Lead)):
        lead.sources.append(source_entry(inp, idem_key))
        save_lead(lead, "source_merged", f"{inp.source}:{inp.sourceRecordId} merged; no duplicate outreach")
        emit("lead.upserted", lead, disposition="existing", mergedSource=inp.source)
        return lead, "existing"

    if ident.match == "conflict":
        lead = create_lead(inp, idem_key, ident.accountKey, stage="QUARANTINED")
        lead.ownerId = config.DEFAULT_SDR_OWNER
        lead.flags.append("identity_conflict")
        save_lead(lead, "quarantined", "; ".join(ident.reasons) or "identity conflict")
        upsert_task(lead, "identity_review", config.DEFAULT_SDR_OWNER,
                    f"Resolve identity conflict for {inp.fullName} ({inp.companyName})")
        emit("lead.upserted", lead, disposition="quarantined", reasons=ident.reasons)
        return lead, "quarantined"

    lead = create_lead(inp, idem_key, ident.accountKey)
    lead.accountOwnerId = ident.accountOwner
    lead.ownerId = config.DEFAULT_SDR_OWNER
    save_lead(lead, "assigned", f"assigned to {lead.ownerId}" + (f"; account owner {lead.accountOwnerId}" if lead.accountOwnerId else ""), material=False)
    if suppressed_reason := is_suppressed(lead.normalizedEmail):
        lead.suppressed, lead.suppressionReason = True, f"suppression list: {suppressed_reason}"
        lead.stage = "SUPPRESSED"
        save_lead(lead, "blocked", lead.suppressionReason)
        emit("lead.upserted", lead, disposition="blocked")
        return lead, "blocked"

    if ident.existingOpportunity:
        opp = ident.existingOpportunity
        lead.flags.append("existing_opportunity")
        lead.ownerId = opp.get("ownerId")
        lead.accountOwnerId = opp.get("ownerId")
        lead.stage = "HUMAN_REVIEW"
        save_lead(lead, "existing_opportunity", f"open opportunity {opp.get('id')} owned by {opp.get('ownerId')}")
        upsert_task(lead, "owner_task", opp.get("ownerId") or config.DEFAULT_AE_OWNER,
                    f"New {inp.source} touch from {inp.fullName} on your open opportunity {opp.get('id')} "
                    f"({opp.get('accountName') or inp.companyName}) - no SDR outreach sent")
        emit("lead.upserted", lead, disposition="existing_opportunity", opportunity=opp)
        return lead, "existing_opportunity"

    emit("lead.upserted", lead, disposition="created")
    return lead, "blocked" if inp.contactPermission == "denied" else "created"


# Ingest a lead: idempotency + identity synchronously, then enrich/score/assess/draft in the background
async def ingest_lead(inp: LeadInput, idem_key: Optional[str] = None) -> dict[str, Any]:
    if replay := find_replay(inp, idem_key):
        return {"leadId": replay.id, "disposition": "existing", "runId": None, "idempotentReplay": True}
    run = start_run("ingest")
    try:
        lead, disposition = resolve_identity(inp, idem_key, run)
    except Exception as exc:
        finish_run(run, "failed", type(exc).__name__)
        raise
    run.leadId = lead.id
    db.put("runs", run)
    if disposition in ("existing", "quarantined"):
        finish_run(run)
    else:
        jobs.enqueue(run.id, lead.id, "ingest")
    return {"leadId": lead.id, "disposition": disposition, "runId": run.id}


# Background: enrich -> ICP -> consent -> initial assessment -> first draft (awaiting approval)
async def process_new_lead(lead_id: str, run_id: str) -> None:
    run = get_run(run_id)
    async with _slots:
        try:
            await _process(lead_id, run)
            finish_run(run)
        except Exception as exc:
            _fail_lead(lead_id, run, exc)


# Never lose a lead: park it in HUMAN_REVIEW with a processing_error flag and fail the run
def _fail_lead(lead_id: str, run: Run, exc: Exception) -> None:
    finish_run(run, "failed", type(exc).__name__)
    lead = db.get("leads", lead_id, Lead)
    if lead is None:
        return
    if "processing_error" not in lead.flags:
        lead.flags.append("processing_error")
    save_lead(lead, "processing_error", f"{type(exc).__name__}: operation_failed")
    set_stage(lead, "HUMAN_REVIEW", "processing error - needs manual review")


# Enrich + score a lead in place (shared by inbound and campaign paths)
async def enrich_and_score(lead: Lead, run: Run) -> Lead:
    from app.engine.enrichment import enrich_for_lead
    from app.engine.icp import score_icp

    with step(run, "enrich") as info:
        lead.enrichment = await enrich_for_lead(lead.input)
        if lead.enrichment.provider == "apollo":
            info["detail"] = ("Enriched from the live Apollo API" if lead.enrichment.found
                              else "Apollo skipped this synthetic or invalid company domain; no live facts added")
        elif lead.enrichment.mock:
            info["detail"] = "Matched the company in the company profile database"
        else:
            sources = len((lead.enrichment.company or {}).get("sources") or [])
            info["detail"] = (f"Researched the company's own website with OpenAI web search: {sources} quoted source(s)"
                              if lead.enrichment.found else "Web research found no verifiable public facts")
        if lead.enrichment.errors:
            info["detail"] += "; lookup notices: " + ", ".join(lead.enrichment.errors)
        enr = lead.enrichment
    save_lead(lead, "enriched", info["detail"])
    if lead.stage == "NEW":
        set_stage(lead, "ENRICHED", "enrichment complete")
    emit("lead.enriched", lead, enrichment={
        "provider": enr.provider, "mock": enr.mock, "found": enr.found, "company": enr.company,
        "person": enr.person, "missing": enr.missing, "conflicts": enr.conflicts,
    })

    with step(run, "icp") as info:
        lead.icp = score_icp(lead.input, lead.enrichment)
        info["detail"] = f"score {lead.icp.score} / fit {lead.icp.fit} / {lead.icp.status}"
    save_lead(lead, "icp_scored", info["detail"])
    return lead


# Body of process_new_lead (raises on failure)
async def _process(lead_id: str, run: Run) -> None:
    from app.engine.identity import consent_check
    from app.engine.qualify import initial_assessment

    lead = await enrich_and_score(get_lead(lead_id), run)
    if lead.suppressed or "existing_opportunity" in lead.flags:
        add_step(run, "outreach", "skipped", "existing opportunity owner handles contact" if not lead.suppressed
                 else "lead is suppressed")
        return

    with step(run, "consent") as info:
        consent = consent_check(lead.input, lead.enrichment)
        info["detail"] = f"{'allowed' if consent.allowed else 'blocked'}: {consent.reason}" + (
            f" [{consent.regime}]" if consent.regime else "")
        if not consent.allowed:
            info["status"] = "blocked"
    if not consent.allowed:
        if lead.input.contactPermission == "denied" or "suppression" in consent.reason.lower():
            suppress(lead, f"consent: {consent.reason}", run)
        else:
            lead.flags.append("consent_hold")
            save_lead(lead, "consent_hold", consent.reason)
            upsert_task(lead, "sdr_review", config.DEFAULT_SDR_OWNER,
                        f"Consent hold for {lead.input.fullName}: {consent.reason}")
            set_stage(lead, "HUMAN_REVIEW", f"consent hold: {consent.reason}")
        return

    with step(run, "initial_assessment") as info:
        asm = initial_assessment(lead)
        db.put("assessments", asm)
        note_mode(run, asm.mode)
        info["detail"] = f"route {asm.route}; qualification {asm.qualificationScore}; missing {asm.missingFields}"
    lead.latestAssessmentId = asm.id
    save_lead(lead, "assessed", info["detail"])
    emit("lead.assessed", lead, assessment=assessment_view(asm))

    if (lead.icp and lead.icp.status == "outside_icp") or asm.route == "closed":
        reason = "; ".join(asm.routeReasons or (lead.icp.reasons if lead.icp else [])) or "outside ICP"
        set_stage(lead, "CLOSED", reason)
        return
    if asm.route == "suppressed":
        suppress(lead, "; ".join(asm.routeReasons) or "policy suppression", run)
        return
    if lead.icp and lead.icp.status == "needs_review":
        upsert_task(lead, "sdr_review", config.DEFAULT_SDR_OWNER,
                    f"Review {lead.input.fullName} before outreach: {'; '.join(lead.icp.reasons)}")
        set_stage(lead, "HUMAN_REVIEW", "ICP needs review before outreach")
        return

    kind = INBOUND_DRAFT_KIND.get(lead.input.source)
    if kind is None:
        add_step(run, "draft", "skipped", "target accounts are drafted by campaign sequences")
        return
    await draft_and_announce(lead_id, kind, asm, run)


# Draft an email for a lead, move it to ENGAGING and announce the pending approval
async def draft_and_announce(lead_id: str, kind: DraftKind, asm: Optional[Assessment], run: Run) -> Draft:
    from app.engagement.drafting import draft_for_lead

    with step(run, "draft") as info:
        saved_id = next((item.get("draftId") for item in reversed(run.steps)
                         if item.get("name") == "draft_reference" and item.get("draftKind") == kind), None)
        draft = db.get("drafts", saved_id, Draft) if saved_id else None
        if draft is None:
            draft = await draft_for_lead(get_lead(lead_id), kind, asm)
            run.steps.append({"name": "draft_reference", "status": "ok", "draftId": draft.id,
                              "draftKind": kind, "at": now_iso()})
            db.put("runs", run)
        note_mode(run, draft.mode)
        info["detail"] = f"{kind} '{draft.subject}' awaiting approval" + (
            f"; guardrail flags {draft.guardrailFlags}" if draft.guardrailFlags else "")
    lead = get_lead(lead_id)
    save_lead(lead, "draft_created", f"{kind} {draft.id}", material=False)
    if lead.stage in ("NEW", "ENRICHED", "QUALIFYING"):
        set_stage(lead, "ENGAGING", f"{kind} drafted")
    emit("draft.pending_approval", lead, draft=draft_view(draft))
    return draft


# Compact assessment view for CRM payloads
def assessment_view(asm: Assessment) -> dict[str, Any]:
    return {
        "id": asm.id, "route": asm.route, "routeReasons": asm.routeReasons, "replyIntent": asm.replyIntent,
        "intentConfidence": asm.intentConfidence, "qualificationScore": asm.qualificationScore,
        "bantScore": asm.bantScore, "bant": {k: v.status for k, v in asm.bant.items()},
        "missingFields": asm.missingFields, "nextQuestion": asm.nextQuestion, "nextAction": asm.nextAction,
        "intentStrength": asm.intentStrength, "objections": asm.objections, "competitors": asm.competitors,
        "buyingSignals": asm.buyingSignals, "policyVersion": asm.policyVersion, "mode": asm.mode,
    }


# Compact draft view for CRM payloads
def draft_view(draft: Draft) -> dict[str, Any]:
    return {
        "id": draft.id, "kind": draft.kind, "version": draft.version, "subject": draft.subject,
        "status": draft.status, "dueAt": draft.dueAt, "enrollmentId": draft.enrollmentId,
        "guardrailFlags": draft.guardrailFlags, "contentHash": draft.contentHash,
    }


# ---------------------------------------------------------------- replies


# Ingest an inbound reply: dedupe, store, stop sequences FIRST, then assess+route in the background
def stop_for_received_reply(lead_id: str) -> None:
    from app.engagement import sequences
    lead = get_lead(lead_id)
    stopped = sequences.stop_on_reply(lead_id, "reply")
    stale_pending_drafts(lead_id)
    if stopped:
        emit("sequence.stopped", lead, reason="reply", enrollmentIds=[item.id for item in stopped])


# Store a fresh buyer message and queue exactly one durable assessment run.
async def ingest_reply(lead_id: str, reply: ReplyInput) -> dict[str, Any]:
    from app.engagement import sequences

    lead = get_lead(lead_id)
    if reply.messageId and (existing := db.get("messages", reply.messageId, Message)):
        if existing.leadId != lead_id or existing.direction != "inbound" or existing.body != reply.bodyText:
            raise ValueError("reply_identity_conflict")
        pending = [r for r in db.list_("runs", Run, lead_id=lead_id, limit=100000)
                   if any(step.get("messageId") == existing.id for step in r.steps)]
        if pending:
            run = pending[-1]
        else:
            # A restart may occur between persisting the message and its run.
            run = start_run("reply", lead.id)
            run.steps.append({"name": "input_reference", "status": "ok", "messageId": existing.id, "at": now_iso()})
            db.put("runs", run)
            stop_for_received_reply(lead.id)
            if not any(item.get("type") == "reply_received" and item.get("detail") == existing.id for item in lead.history):
                save_lead(lead, "reply_received", existing.id)
            if not lead.suppressed:
                set_stage(lead, "QUALIFYING", "reply received")
        if run.status != "succeeded" and not jobs._runner.get(run.id):
            jobs.enqueue(run.id, lead.id, "reply", {"messageId": existing.id})
        return {"runId": run.id, "messageId": reply.messageId,
                "stoppedEnrollments": [], "duplicate": True}

    msg = Message(leadId=lead.id, threadId=reply.threadId or lead.threadId, direction="inbound",
                  subject=_reply_subject(lead), body=reply.bodyText, origin=reply.origin,
                  at=reply.receivedAt or now_iso())
    if reply.messageId:
        msg.id = reply.messageId
    db.put("messages", msg)

    run = start_run("reply", lead.id)
    run.steps.append({"name": "input_reference", "status": "ok", "messageId": msg.id, "at": now_iso()})
    db.put("runs", run)
    with step(run, "stop_sequences") as info:
        stopped = sequences.stop_on_reply(lead.id, "reply")
        staled = stale_pending_drafts(lead.id)
        info["detail"] = (f"{len(stopped)} active enrollment(s) stopped before analysis; "
                          f"{staled} unsent draft(s) superseded")
    if stopped:
        emit("sequence.stopped", lead, reason="reply", enrollmentIds=[e.id for e in stopped])

    add_step(run, "reply_received", "ok", f"{len(reply.bodyText)} chars via {reply.origin}")
    save_lead(lead, "reply_received", msg.id)
    emit("reply.received", lead, messageId=msg.id, excerpt=reply.bodyText[:280], origin=reply.origin)
    if not lead.suppressed:
        set_stage(lead, "QUALIFYING", "reply received")
    jobs.enqueue(run.id, lead.id, "reply", {"messageId": msg.id})
    return {"runId": run.id, "messageId": msg.id, "stoppedEnrollments": [e.id for e in stopped]}


# Subject line for an inbound reply on the lead's thread
def _reply_subject(lead: Lead) -> str:
    sent = [m for m in db.list_("messages", Message, lead_id=lead.id) if m.direction == "outbound"]
    return f"Re: {sent[-1].subject}" if sent and sent[-1].subject else "Re: Azentio"


# Background: assess the reply and route the lead to AE, SDR, nurture, suppression or close
async def assess_and_route(lead_id: str, msg_id: str, run_id: str) -> None:
    run = get_run(run_id)
    async with _slots:
        try:
            await _assess_and_route(lead_id, msg_id, run)
            finish_run(run)
        except Exception as exc:
            _fail_lead(lead_id, run, exc)


# Body of assess_and_route (raises on failure)
async def _assess_and_route(lead_id: str, msg_id: str, run: Run) -> None:
    from app.engine.qualify import assess_reply

    lead = get_lead(lead_id)
    msg = db.get("messages", msg_id, Message)
    if msg is None:
        raise NotFound(f"message {msg_id}")
    received = [m for m in db.list_("messages", Message, lead_id=lead_id) if m.direction == "inbound"]
    if received and received[-1].id != msg_id:
        add_step(run, "superseded", "skipped", "A newer buyer reply is queued for this lead.")
        return
    thread = sorted(db.list_("messages", Message, lead_id=lead_id), key=lambda m: m.at)
    prior = latest_assessment(lead)

    with step(run, "assess_reply") as info:
        saved_id = next((item.get("assessmentId") for item in reversed(run.steps)
                         if item.get("name") == "assessment_reference" and item.get("messageId") == msg_id), None)
        asm = db.get("assessments", saved_id, Assessment) if saved_id else None
        if asm is None:
            asm = await assess_reply(lead, msg, thread, prior)
            db.put("assessments", asm)
            run.steps.append({"name": "assessment_reference", "status": "ok", "assessmentId": asm.id,
                              "messageId": msg_id, "at": now_iso()})
            db.put("runs", run)
        received = [m for m in db.list_("messages", Message, lead_id=lead_id) if m.direction == "inbound"]
        if received and received[-1].id != msg_id:
            add_step(run, "superseded", "skipped", "A newer buyer reply arrived during this review.")
            return
        note_mode(run, asm.mode)
        conf = f"{asm.intentConfidence:.2f}" if asm.intentConfidence is not None else "n/a"
        bant = ", ".join(f"{k}={v.status}" for k, v in asm.bant.items())
        info["detail"] = (f"intent {asm.replyIntent} ({conf}); {bant}; qualification {asm.qualificationScore}; "
                          f"route {asm.route}")
    lead = get_lead(lead_id)
    lead.latestAssessmentId = asm.id
    save_lead(lead, "assessed", info["detail"])
    emit("lead.assessed", lead, assessment=assessment_view(asm))

    with step(run, "route") as info:
        info["detail"] = await route(lead, asm, run)


# Pick the AE for a handoff: CRM account owner first, else the first matching territory/product rule; returns (owner, reason)
def ae_assignment(lead: Lead) -> tuple[str, str]:
    if lead.accountOwnerId:
        return lead.accountOwnerId, f"Account already owned by {person_name(lead.accountOwnerId)} in the CRM"
    country = (lead.input.country or "").strip().upper()
    country = COUNTRY_ALIASES.get(country, country)
    for countries, products, owner, why in AE_TERRITORY_RULES:
        if (countries is None or country in countries) and (products is None or lead.input.productId in products):
            return owner, f"{person_name(owner)} {why}"
    return config.DEFAULT_AE_OWNER, f"{person_name(config.DEFAULT_AE_OWNER)} is the default AE"


# The account executive who should receive a qualified handoff
def ae_owner(lead: Lead) -> str:
    return ae_assignment(lead)[0]


# Apply an assessment's route to the lead; returns a human-readable outcome
async def route(lead: Lead, asm: Assessment, run: Run) -> str:
    reasons = "; ".join(asm.routeReasons) or asm.route
    if asm.route == "rep_review":
        owner, why = ae_assignment(lead)
        card = build_card(lead, asm, owner, why)
        task = upsert_task(lead, "ae_handoff", owner, f"Qualified: {lead.input.fullName} ({lead.input.companyName})", card)
        lead.ownerId = owner
        close_sdr_tasks(lead.id)
        stale_pending_drafts(lead.id)
        save_lead(lead, "ae_handoff", task.id)
        set_stage(lead, "REP_REVIEW", reasons)
        emit("alert.ae_handoff", lead, taskId=task.id, ownerId=owner, card=card.model_dump(mode="json"))
        return f"AE handoff to {owner} (task {task.id})"

    if asm.route == "human_review":
        return await _route_human_review(lead, asm, run, reasons)

    if asm.route == "nurture":
        save_lead(lead, "next_action", asm.nextAction or "nurture: re-engage later", material=False)
        set_stage(lead, "NURTURE", reasons)
        return f"nurture; next action: {asm.nextAction or 'revisit later'}"

    if asm.route == "suppressed":
        suppress(lead, f"{asm.replyIntent or 'policy'}: {reasons}", run)
        return "suppressed; sequences stopped and pending drafts made stale"

    if asm.route == "closed":
        set_stage(lead, "CLOSED", reasons)
        return f"closed: {reasons}"

    owner, why = ae_assignment(lead)
    card = build_card(lead, asm, owner, why)
    task = upsert_task(lead, "owner_task", owner, f"Reply on existing opportunity from {lead.input.fullName}", card)
    set_stage(lead, "HUMAN_REVIEW", reasons)
    return f"routed to existing owner {owner} (task {task.id})"


# Human-review routing: pick the task type, alert the SDR, and draft one gap-fill question if useful
async def _route_human_review(lead: Lead, asm: Assessment, run: Run, reasons: str) -> str:
    owner = config.DEFAULT_SDR_OWNER
    low_conf = asm.intentConfidence is not None and asm.intentConfidence < config.INTENT_CONFIDENCE_FLOOR
    if asm.replyIntent == "referral":
        task_type: TaskType = "referral_review"
    elif asm.replyIntent in ("out_of_office", "wrong_person") or low_conf:
        task_type = "sdr_review"
    else:
        task_type = "sdr_followup"
    why = (f"{person_name(owner)} (SDR) owns follow-up until the lead is qualified: "
           + (asm.nextAction or "needs a human decision"))
    card = build_card(lead, asm, owner, why)
    task = upsert_task(lead, task_type, owner, f"{task_type.replace('_', ' ').title()}: {lead.input.fullName}", card)
    if not (lead.ownerId or "").startswith("sdr."):
        lead.accountOwnerId = lead.accountOwnerId or lead.ownerId
        lead.ownerId = owner
        save_lead(lead, "assigned", f"SDR follow-up assigned to {owner}")
    set_stage(lead, "HUMAN_REVIEW", reasons)
    emit("task.sdr_followup", lead, taskId=task.id, taskType=task_type, ownerId=owner, card=card.model_dump(mode="json"))
    outcome = f"{task_type} for {owner} (task {task.id})"

    pending_gap = any(d.kind == "gap_fill" and d.status in PENDING_DRAFT_STATUSES
                      for d in db.list_("drafts", Draft, lead_id=lead.id))
    if task_type == "sdr_followup" and asm.nextQuestion and lead.gapFillsSent < 1 and not pending_gap:
        draft = await draft_and_announce(lead.id, "gap_fill", asm, run)
        outcome += f"; gap-fill draft {draft.id} awaiting approval"
    return outcome


# ---------------------------------------------------------------- sending, campaigns, clock


# Send an approved draft through the simulated transport and record the outbound message
async def send_draft(draft_id: str) -> Message:
    from app.engagement import sequences
    from app.engagement.transport import simulate_send

    draft = get_draft(draft_id)
    lead = get_lead(draft.leadId)
    run = start_run("send", lead.id)
    try:
        with step(run, "send") as info:
            msg = await asyncio.to_thread(simulate_send, draft, lead)
            if db.get("messages", msg.id, Message) is None:
                db.put("messages", msg)
            info["detail"] = f"{draft.kind} '{draft.subject}' sent to {draft.to}"
        if draft.enrollmentId:
            sequences.mark_touch_sent(get_draft(draft_id))
    except Exception as exc:
        finish_run(run, "failed", str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        raise
    lead = get_lead(lead.id)
    if draft.kind == "gap_fill":
        lead.gapFillsSent += 1
    save_lead(lead, "message_sent", f"{draft.kind} {msg.id}")
    emit("message.sent", lead, messageId=msg.id, draftId=draft.id, kind=draft.kind, subject=draft.subject,
         enrollmentId=draft.enrollmentId, deliveryStatus=msg.deliveryStatus, deliveredTo=msg.deliveredTo,
         intendedTo=msg.intendedTo)
    if lead.stage in ("NEW", "ENRICHED"):
        set_stage(lead, "ENGAGING", f"{draft.kind} sent")
    finish_run(run)
    return msg


# Campaign exclusion reason for a lead, or None if it can be enrolled
def _exclusion(lead: Lead) -> Optional[str]:
    if lead.suppressed:
        return "suppressed"
    if "existing_opportunity" in lead.flags:
        return "existing_opportunity"
    if lead.stage in ("QUARANTINED", "REP_REVIEW", "OPPORTUNITY", "CLOSED"):
        return f"stage {lead.stage}"
    for other in db.find("leads", Lead, normalizedEmail=lead.normalizedEmail):
        if other.id != lead.id and other.stage in ACTIVE_CONVERSATION_STAGES:
            return f"active_conversation ({other.input.productId} lead {other.id} is {other.stage})"
    return None


# Outbound campaign: cohort search -> identity/enrich/ICP (no inbound drafting) -> sequence enrollment + drafts
async def create_campaign(inp: CampaignInput) -> dict[str, Any]:
    run = start_run("campaign")
    campaign = Campaign(name=inp.name, theme=inp.theme, cohort=inp.cohort, touchDays=inp.touchDays, status="DRAFTING",
                        channels=["email", *dict.fromkeys(c for c in inp.channels if c != "email")])
    db.put("campaigns", campaign)
    workflow_requests.save(run.id, {"campaignInput": inp.model_dump(mode="json"), "campaignId": campaign.id})
    jobs.enqueue(run.id, None, "campaign", {"requestId": run.id})
    # Preserve the native API response; the work itself survives a client disconnect.
    while True:
        saved = workflow_requests.get(run.id)
        if saved and saved["result"]:
            return saved["result"]
        current = get_run(run.id)
        if jobs._runner.get(run.id)["status"] == "failed":
            raise ValueError("campaign_needs_review")
        await asyncio.sleep(0.5)


# Execute one saved campaign request, reusing partial enrollment and draft state.
async def _create_campaign(inp: CampaignInput, run: Run, campaign: Optional[Campaign] = None) -> dict[str, Any]:
    from app.engagement import sequences
    from app.engine.enrichment import search_cohort

    try:
        with step(run, "cohort_search") as info:
            inputs = search_cohort(inp.cohort)
            info["detail"] = f"{len(inputs)} contact(s) match {inp.cohort.productId} cohort"
        leads: list[Lead] = []
        excluded: list[dict[str, Any]] = []
        for li in inputs:
            lead, disposition = resolve_identity(li, None, run)
            if disposition == "created":
                lead = await enrich_and_score(lead, run)
            own_enrollment = campaign and any(e.campaignId == campaign.id for e in db.find("enrollments", Enrollment, leadId=lead.id))
            reason = None if own_enrollment else _exclusion(lead)
            if reason:
                excluded.append({"leadId": lead.id, "email": lead.normalizedEmail, "reason": reason})
            else:
                leads.append(lead)
        with step(run, "enroll") as info:
            campaign = await sequences.create_campaign(inp, leads, campaign=campaign)
            known = {e.get("leadId") for e in campaign.exclusions}
            campaign.exclusions.extend(e for e in excluded if e["leadId"] not in known)
            db.put("campaigns", campaign)
            info["detail"] = f"{len(campaign.enrollmentIds)} enrolled, {len(campaign.exclusions)} excluded"
    except Exception as exc:
        finish_run(run, "failed", type(exc).__name__)
        raise
    detail = campaign_detail(campaign.id)
    for draft in detail["drafts"]:
        lead = get_lead(draft.leadId)
        if lead.stage in ("NEW", "ENRICHED"):
            set_stage(lead, "ENGAGING", f"enrolled in campaign {campaign.name}")
        emit("draft.pending_approval", lead, draft=draft_view(draft), campaignId=campaign.id)
    finish_run(run)
    return {**detail, "runId": run.id}


# Resume a model stage from durable record references; email sends are never replayed here.
async def execute_job(kind: str, lead_id: Optional[str], run_id: str, payload: dict[str, Any]) -> None:
    run = get_run(run_id)
    if run.status == "succeeded":
        if kind == "campaign":
            request = workflow_requests.get(payload["requestId"])
            if request and not request["result"]:
                workflow_requests.finish(run_id, {**campaign_detail(request["payload"]["campaignId"]), "runId": run_id})
        return
    run.status, run.errorCode, run.finishedAt = "running", None, None
    db.put("runs", run)
    if lead_id:
        lead = get_lead(lead_id)
        if "processing_error" in lead.flags:
            lead.flags.remove("processing_error")
            db.put("leads", lead)
    try:
        if kind == "ingest":
            async with _slots:
                await _process(lead_id, run)
        elif kind == "reply":
            async with _slots:
                await _assess_and_route(lead_id, payload["messageId"], run)
        elif kind in ("enrich", "assess", "draft"):
            await _rerun(lead_id, kind, run)
            if get_run(run_id).status == "failed":
                raise RuntimeError("stage_needs_retry")
        elif kind == "campaign":
            request = workflow_requests.get(payload["requestId"])
            if request is None:
                raise ValueError("campaign_request_missing")
            data = request["payload"]
            campaign = db.get("campaigns", data["campaignId"], Campaign)
            result = await _create_campaign(CampaignInput.model_validate(data["campaignInput"]), run, campaign)
            workflow_requests.finish(run_id, result)
        else:
            raise ValueError("job_kind_unsupported")
        finish_run(run)
    except asyncio.CancelledError:
        add_step(run, "interrupted", "pending", "Review will resume after restart.")
        raise
    except Exception as exc:
        if lead_id:
            _fail_lead(lead_id, run, exc)
        else:
            finish_run(run, "failed", type(exc).__name__)
        raise


# Recover pre-worker run records conservatively, without guessing campaign input or replaying sends.
def recover_unfinished_runs(runner) -> int:
    recovered = 0
    for run in db.list_("runs", Run, limit=100000):
        if run.status not in ("running", "queued") or runner.get(run.id):
            continue
        payload = {}
        if run.kind == "reply" and run.leadId:
            message_id = next((item.get("messageId") for item in run.steps if item.get("name") == "input_reference"), None)
            if not message_id:
                lead = get_lead(run.leadId)
                message_id = next((entry.get("detail") for entry in lead.history
                                   if entry.get("type") == "reply_received" and entry.get("at", "") >= run.startedAt), None)
            if message_id and db.get("messages", message_id, Message):
                payload = {"messageId": message_id}
            else:
                finish_run(run, "failed", "recovery_context_missing")
                continue
        elif run.kind == "campaign":
            if workflow_requests.get(run.id):
                payload = {"requestId": run.id}
            else:
                finish_run(run, "failed", "recovery_context_missing")
                continue
        elif run.kind not in ("ingest", "enrich", "assess", "draft") or not run.leadId:
            finish_run(run, "failed", "interrupted_action_needs_review")
            continue
        runner.submit(run.id, run.leadId, run.kind, payload)
        recovered += 1
    return recovered


# Campaign with its enrollments and touch drafts
def campaign_detail(campaign_id: str) -> dict[str, Any]:
    campaign = db.get("campaigns", campaign_id, Campaign)
    if campaign is None:
        raise NotFound(f"campaign {campaign_id}")
    enrollments = [e for eid in campaign.enrollmentIds if (e := db.get("enrollments", eid, Enrollment))]
    drafts = [d for e in enrollments for did in e.touchDraftIds if (d := db.get("drafts", did, Draft))]
    return {"campaign": campaign, "enrollments": enrollments, "drafts": drafts}


# Advance the demo clock and send every approved, due sequence touch
async def advance_clock(hours: float) -> dict[str, Any]:
    from app.engagement import clock, sequences

    run = start_run("clock")
    with step(run, "advance") as info:
        now = clock.advance(hours)
        info["detail"] = f"+{hours}h -> {now.isoformat()}"
    sent, skipped = [], []
    for draft in sequences.due_touches():
        try:
            msg = await send_draft(draft.id)
            sent.append({"draftId": draft.id, "leadId": draft.leadId, "kind": draft.kind, "messageId": msg.id})
        except ValueError as exc:
            skipped.append({"draftId": draft.id, "leadId": draft.leadId, "kind": draft.kind, "code": str(exc)})
    waiting = [{"draftId": d.id, "leadId": d.leadId, "kind": d.kind, "dueAt": d.dueAt}
               for d in sequences.pending_unapproved_due()]
    add_step(run, "due_touches", "ok", f"{len(sent)} sent, {len(skipped)} skipped, {len(waiting)} waiting approval")
    finish_run(run)
    return {"now": now.isoformat(), "sent": sent, "skipped": skipped, "waitingApproval": waiting, "runId": run.id}


# ---------------------------------------------------------------- manual runs


# Re-run one pipeline stage for a lead on demand (enrich | assess | draft)
async def rerun(lead_id: str, kind: str) -> dict[str, Any]:
    if kind not in ("enrich", "assess", "draft"):
        raise ValueError("invalid_run_kind")
    lead = get_lead(lead_id)
    run = start_run(kind, lead.id)
    jobs.enqueue(run.id, lead.id, kind)
    return {"runId": run.id}


# Background body for a manual re-run
async def _rerun(lead_id: str, kind: str, run: Run) -> None:
    from app.engine.qualify import initial_assessment

    async with _slots:
        try:
            if kind == "enrich":
                await enrich_and_score(get_lead(lead_id), run)
            elif kind == "assess":
                inbound = [m for m in db.list_("messages", Message, lead_id=lead_id) if m.direction == "inbound"]
                if inbound:
                    await _assess_and_route(lead_id, sorted(inbound, key=lambda m: m.at)[-1].id, run)
                else:
                    lead = get_lead(lead_id)
                    with step(run, "initial_assessment") as info:
                        asm = initial_assessment(lead)
                        db.put("assessments", asm)
                        info["detail"] = f"route {asm.route}; qualification {asm.qualificationScore}"
                    lead.latestAssessmentId = asm.id
                    save_lead(lead, "assessed", info["detail"])
                    emit("lead.assessed", lead, assessment=assessment_view(asm))
            else:
                lead = get_lead(lead_id)
                asm = latest_assessment(lead)
                kind_ = "gap_fill" if asm and asm.nextQuestion and db.list_("messages", Message, lead_id=lead_id) \
                    else INBOUND_DRAFT_KIND.get(lead.input.source) or "qualifying_email"
                await draft_and_announce(lead_id, kind_, asm, run)
            finish_run(run)
        except Exception as exc:
            _fail_lead(lead_id, run, exc)


# ---------------------------------------------------------------- tasks + read models


# Update a task's status; accepting an AE handoff creates the opportunity stage (human decision only)
def update_task(task_id: str, status: TaskStatus) -> Task:
    task = db.get("tasks", task_id, Task)
    if task is None:
        raise NotFound(f"task {task_id}")
    if task.status == status:
        return task
    lead = get_lead(task.leadId)
    if task.type == "ae_handoff" and status == "accepted":
        assessment = latest_assessment(lead)
        if (lead.suppressed or lead.input.contactPermission != "allowed" or not assessment
                or assessment.route != "rep_review" or not lead.icp or lead.icp.status != "matches_icp"
                or any(assessment.bant.get(key) is None or assessment.bant[key].status != "confirmed" for key in BANT_KEYS)):
            raise ValueError("handoff_needs_current_qualification")
    task.status = status
    db.put("tasks", task)
    events.publish("task.updated", task.leadId, {"taskId": task.id, "type": task.type, "status": status,
                                                 "summary": f"{task.type} {status} by {task.ownerId}"})
    lead = get_lead(task.leadId)
    if task.type == "ae_handoff" and status == "accepted":
        lead.ownerId = task.ownerId
        save_lead(lead, "opportunity_accepted", f"{task.ownerId} accepted handoff {task.id}")
        set_stage(lead, "OPPORTUNITY", f"AE {task.ownerId} accepted the handoff")
    elif status == "returned":
        if task.type == "ae_handoff":
            lead.ownerId = config.DEFAULT_SDR_OWNER
        save_lead(lead, "task_returned", f"{task.type} {task.id} returned by {task.ownerId}")
        set_stage(lead, "HUMAN_REVIEW", f"{task.type} returned for more qualification")
    return task


# Alias kept for the orchestration contract name
accept_task = update_task


# One-row lead summary for list views
def lead_summary(lead: Lead) -> dict[str, Any]:
    asm = latest_assessment(lead)
    return {
        "id": lead.id,
        "fullName": lead.input.fullName,
        "email": lead.normalizedEmail,
        "companyName": lead.input.companyName,
        "country": lead.input.country,
        "source": lead.input.source,
        "productId": lead.input.productId,
        "stage": lead.stage,
        "fit": lead.icp.fit if lead.icp else None,
        "icpScore": lead.icp.score if lead.icp else None,
        "icpStatus": lead.icp.status if lead.icp else None,
        "qualificationScore": asm.qualificationScore if asm else None,
        "replyIntent": asm.replyIntent if asm else None,
        "route": asm.route if asm else None,
        "bant": {k: (asm.bant[k].status if asm and k in asm.bant else "missing") for k in BANT_KEYS},
        "nextAction": asm.nextAction if asm else "",
        "suppressed": lead.suppressed,
        "flags": lead.flags,
        "ownerId": lead.ownerId,
        "updatedAt": lead.updatedAt,
    }


# List lead summaries with optional filters, most recently updated first
def list_leads(stage: Optional[str] = None, source: Optional[str] = None, fit: Optional[str] = None,
               product_id: Optional[str] = None) -> list[dict[str, Any]]:
    items = [lead_summary(lead) for lead in db.list_("leads", Lead, limit=100000)]
    items = [s for s in items if (not stage or s["stage"] == stage) and (not source or s["source"] == source)
             and (not fit or s["fit"] == fit) and (not product_id or s["productId"] == product_id)]
    return sorted(items, key=lambda s: s["updatedAt"], reverse=True)


# Full lead detail with every related record
def lead_detail(lead_id: str) -> dict[str, Any]:
    lead = get_lead(lead_id)
    runs = sorted(db.list_("runs", Run, lead_id=lead_id), key=lambda r: r.startedAt)
    return {
        "lead": lead,
        "summary": lead_summary(lead),
        "assessments": db.list_("assessments", Assessment, lead_id=lead_id),
        "drafts": db.list_("drafts", Draft, lead_id=lead_id),
        "messages": sorted(db.list_("messages", Message, lead_id=lead_id), key=lambda m: m.at),
        "tasks": db.list_("tasks", Task, lead_id=lead_id),
        "enrollments": db.list_("enrollments", Enrollment, lead_id=lead_id),
        "runs": runs,
    }


# Increment a nested counter
def _count(bucket: dict[str, int], key: Optional[str]) -> None:
    bucket[key or "unknown"] = bucket.get(key or "unknown", 0) + 1


# Pipeline dashboard: counts by source/stage/fit/BANT/route plus the ready-for-human queue
def dashboard() -> dict[str, Any]:
    leads = db.list_("leads", Lead, limit=100000)
    by_source: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    by_fit: dict[str, int] = {}
    by_route: dict[str, int] = {}
    bant: dict[str, dict[str, int]] = {k: {} for k in BANT_KEYS}
    ready = []
    for lead in leads:
        asm = latest_assessment(lead)
        _count(by_source, lead.input.source)
        _count(by_stage, lead.stage)
        _count(by_fit, lead.icp.fit if lead.icp else "unscored")
        _count(by_route, asm.route if asm else "unassessed")
        for k in BANT_KEYS:
            _count(bant[k], asm.bant[k].status if asm and k in asm.bant else "missing")
        if lead.stage in REVIEW_STAGES:
            ready.append({
                "leadId": lead.id, "fullName": lead.input.fullName, "companyName": lead.input.companyName,
                "stage": lead.stage, "fit": lead.icp.fit if lead.icp else None,
                "icpScore": lead.icp.score if lead.icp else None,
                "qualificationScore": asm.qualificationScore if asm else None,
                "replyIntent": asm.replyIntent if asm else None,
                "nextAction": asm.nextAction if asm else "", "flags": lead.flags, "updatedAt": lead.updatedAt,
            })
    tasks = db.list_("tasks", Task, limit=100000)
    drafts = db.list_("drafts", Draft, limit=100000)
    return {
        "totals": {"leads": len(leads), "readyForHuman": len(ready)},
        "bySource": by_source,
        "byStage": by_stage,
        "byFit": by_fit,
        "byRoute": by_route,
        "bant": bant,
        "readyForHuman": sorted(ready, key=lambda r: (r["stage"] != "REP_REVIEW", -(r["qualificationScore"] or 0))),
        "pendingApprovals": sum(1 for d in drafts if d.status == "DRAFT"),
        "approvedAwaitingSend": sum(1 for d in drafts if d.status == "APPROVED"),
        "openAlerts": sum(1 for t in tasks if t.type == "ae_handoff" and t.status in OPEN_TASK_STATUSES),
        "openTasks": sum(1 for t in tasks if t.status in OPEN_TASK_STATUSES),
    }


# Open AE handoff alerts with their cards
def alerts() -> list[Task]:
    return [t for t in db.list_("tasks", Task, limit=100000) if t.type == "ae_handoff" and t.status in OPEN_TASK_STATUSES]
