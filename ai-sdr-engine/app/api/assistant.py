# app/api/assistant.py - Home tab: "my day" + pipeline KPIs, and an Agents SDK assistant that answers questions over workspace data
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from agents import function_tool
from fastapi import APIRouter
from pydantic import BaseModel

from app import config, db
from app.contracts import Assessment, Campaign, Draft, Enrollment, Lead, Message, Task
from app.llm.client import LLMUnavailable, build_agent, run_agent

router = APIRouter(prefix="/v1", tags=["home"])

PEOPLE = {"ae.sara": "Sara Malik", "ae.khalid": "Khalid Al Mansoori", "ae.priya": "Priya Raman", "sdr.omar": "Nadia Karim"}
STAGE_WORDS = {"NEW": "new", "ENRICHED": "researching", "ENGAGING": "in conversation", "QUALIFYING": "reviewing reply",
               "REP_REVIEW": "qualified, awaiting AE", "OPPORTUNITY": "opportunity", "HUMAN_REVIEW": "needs attention",
               "NURTURE": "nurture", "SUPPRESSED": "opted out", "CLOSED": "not a fit", "QUARANTINED": "identity check"}
OPEN = ("open", "acknowledged")


class AskInput(BaseModel):
    question: str
    userId: Optional[str] = None


# Parse an ISO timestamp into an aware datetime
def _ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# Demo-aware "now" for message/schedule timestamps
def _demo_now() -> datetime:
    from app.engagement import clock
    return clock.demo_now()


# Snapshot of everything the home tab and assistant read
def _snapshot() -> dict[str, list]:
    return {"leads": db.list_("leads", Lead, limit=5000), "tasks": db.list_("tasks", Task, limit=5000),
            "drafts": db.list_("drafts", Draft, limit=5000), "messages": db.list_("messages", Message, limit=5000),
            "assessments": db.list_("assessments", Assessment, limit=5000), "campaigns": db.list_("campaigns", Campaign, limit=500),
            "enrollments": db.list_("enrollments", Enrollment, limit=5000)}


# Latest assessment per lead
def _latest(assessments: list[Assessment]) -> dict[str, Assessment]:
    out: dict[str, Assessment] = {}
    for a in assessments:
        if a.leadId not in out or a.createdAt >= out[a.leadId].createdAt:
            out[a.leadId] = a
    return out


# KPI values for one window [start, end)
def _kpis_between(s: dict, start: datetime, end: datetime, dstart: datetime, dend: datetime) -> dict[str, Any]:
    leads = [l for l in s["leads"] if (t := _ts(l.createdAt)) and start <= t < end]
    ids = {l.id for l in leads}
    first_draft: dict[str, datetime] = {}
    for d in s["drafts"]:
        t = _ts(d.createdAt)
        if d.leadId in ids and t and (d.leadId not in first_draft or t < first_draft[d.leadId]):
            first_draft[d.leadId] = t
    speeds = [(first_draft[l.id] - _ts(l.createdAt)).total_seconds() for l in leads if l.id in first_draft]
    out_msgs = [m for m in s["messages"] if m.direction == "outbound" and (t := _ts(m.at)) and dstart <= t < dend]
    contacted = {m.leadId for m in s["messages"] if m.direction == "outbound"}
    replied = {m.leadId for m in s["messages"] if m.direction == "inbound" and (t := _ts(m.at)) and dstart <= t < dend}
    latest = _latest(s["assessments"])
    positive = {lid for lid in replied if latest.get(lid) and latest[lid].replyIntent == "interested"}
    return {
        "newLeads": len(leads),
        "speedToLeadSec": round(statistics.median(speeds), 1) if speeds else None,
        "emailsSent": len(out_msgs),
        "replyRate": round(100 * len(replied & contacted) / len(contacted)) if contacted else None,
        "positiveReplies": round(100 * len(positive) / len(contacted)) if contacted else None,
        "qualified": sum(1 for l in s["leads"] if l.stage in ("REP_REVIEW", "OPPORTUNITY") and (t := _ts(l.updatedAt)) and start <= t < end),
        "opportunities": sum(1 for t_ in s["tasks"] if t_.type == "ae_handoff" and t_.status == "accepted" and (t := _ts(t_.createdAt)) and start <= t < end),
        "optOuts": sum(1 for l in s["leads"] if l.suppressed and (t := _ts(l.updatedAt)) and start <= t < end),
    }


# Pipeline KPIs for the last N days with previous-period comparison
def pipeline_kpis(days: int, s: Optional[dict] = None) -> dict[str, Any]:
    s = s or _snapshot()
    now, dnow, span = datetime.now(timezone.utc), _demo_now(), timedelta(days=days)
    cur = _kpis_between(s, now - span, now + timedelta(minutes=1), dnow - span, dnow + timedelta(minutes=1))
    prev = _kpis_between(s, now - 2 * span, now - span, dnow - 2 * span, dnow - span)
    return {"days": days, "current": cur, "previous": prev}


# Short lead descriptor used in lists and tool output
def _lead_brief(l: Lead, latest: dict[str, Assessment]) -> dict[str, Any]:
    a = latest.get(l.id)
    return {"leadId": l.id, "name": l.input.fullName, "company": l.input.companyName, "stage": STAGE_WORDS.get(l.stage, l.stage),
            "fit": l.icp.fit if l.icp else None, "fitScore": l.icp.score if l.icp else None,
            "qualificationScore": a.qualificationScore if a else None, "assignee": PEOPLE.get(l.ownerId or "", l.ownerId),
            "accountOwner": PEOPLE.get(l.accountOwnerId or "", l.accountOwnerId), "source": l.input.source,
            "product": l.input.productId, "lastIntent": a.replyIntent if a else None, "updatedAt": l.updatedAt}


# Everything one person should look at today
def my_day(user_id: str, s: Optional[dict] = None) -> dict[str, Any]:
    s = s or _snapshot()
    latest = _latest(s["assessments"])
    by_id = {l.id: l for l in s["leads"]}
    mine = {l.id for l in s["leads"] if l.ownerId == user_id}
    tasks = [t for t in s["tasks"] if t.ownerId == user_id and t.status in OPEN]
    dnow = _demo_now()
    end_of_day = dnow.replace(hour=23, minute=59, second=59)
    approvals = [d for d in s["drafts"] if d.status in ("DRAFT", "BLOCKED") and d.leadId in mine
                 and (not d.dueAt or (_ts(d.dueAt) or dnow) <= end_of_day)]
    due_today = [d for d in s["drafts"] if d.status == "APPROVED" and d.dueAt and (_ts(d.dueAt) or dnow) <= end_of_day and d.leadId in mine]
    recent_replies = [m for m in s["messages"] if m.direction == "inbound" and m.leadId in mine and (_ts(m.at) or dnow) >= dnow - timedelta(days=2)]
    contacted = {m.leadId for m in s["messages"] if m.direction == "outbound"}
    replied = {m.leadId for m in s["messages"] if m.direction == "inbound"}
    waiting = [by_id[i] for i in mine & contacted - replied if by_id[i].icp and by_id[i].icp.fit == "High" and not by_id[i].suppressed]
    item = lambda lid: _lead_brief(by_id[lid], latest) if lid in by_id else {"leadId": lid}
    return {
        "user": {"id": user_id, "name": PEOPLE.get(user_id, user_id)},
        "tasks": [{"taskId": t.id, "type": t.type, "title": t.title, "createdAt": t.createdAt,
                   "nextAction": (t.card.recommendedNextAction if t.card else ""), **item(t.leadId)} for t in tasks],
        "approvals": [{"draftId": d.id, "kind": d.kind, "subject": d.subject, "dueAt": d.dueAt, **item(d.leadId)} for d in approvals],
        "dueToday": [{"draftId": d.id, "kind": d.kind, "subject": d.subject, "dueAt": d.dueAt, **item(d.leadId)} for d in due_today],
        "replies": [{"messageId": m.id, "at": m.at, "excerpt": m.body[:180], **item(m.leadId)} for m in recent_replies],
        "awaitingReply": [_lead_brief(l, latest) for l in waiting],
        "myLeads": len(mine),
    }


# Home payload: my day + KPIs
@router.get("/home")
def home(userId: str = "ae.sara", days: int = 14) -> dict[str, Any]:
    s = _snapshot()
    return {"myDay": my_day(userId, s), "kpis": pipeline_kpis(max(1, min(days, 90)), s), "team": [{"id": k, "name": v} for k, v in PEOPLE.items()]}


# ---------- Assistant tools (read-only, JSON results) ----------
@function_tool
def search_leads(stage: Optional[str] = None, fit: Optional[str] = None, assignee: Optional[str] = None,
                 text: Optional[str] = None, replied: Optional[bool] = None, emailed: Optional[bool] = None, limit: int = 15) -> str:
    """Find leads. stage: new|in conversation|needs attention|qualified|opportunity|nurture|opted out|not a fit. fit: High|Medium|Low.
    assignee: person name or id. text: matches name, company or email. emailed: whether we have sent them an email.
    replied: whether they have replied. "Haven't replied yet" means emailed=true and replied=false."""
    s = _snapshot()
    latest = _latest(s["assessments"])
    replied_ids = {m.leadId for m in s["messages"] if m.direction == "inbound"}
    emailed_ids = {m.leadId for m in s["messages"] if m.direction == "outbound"}
    rows = []
    for l in sorted(s["leads"], key=lambda x: x.updatedAt, reverse=True):
        b = _lead_brief(l, latest)
        if stage and stage.lower() not in (b["stage"] or "").lower():
            continue
        if fit and (b["fit"] or "").lower() != fit.lower():
            continue
        if assignee and assignee.lower() not in f"{l.ownerId} {b['assignee']}".lower():
            continue
        if text and text.lower() not in f"{l.input.fullName} {l.input.companyName} {l.input.email}".lower():
            continue
        if replied is not None and (l.id in replied_ids) != replied:
            continue
        if emailed is not None and (l.id in emailed_ids) != emailed:
            continue
        rows.append({**b, "emailed": l.id in emailed_ids, "replied": l.id in replied_ids})
    return json.dumps({"count": len(rows), "leads": rows[:max(1, min(limit, 50))]}, default=str)


@function_tool
def lead_details(name_or_id: str) -> str:
    """Full context for one lead by name or id: profile, buying signals with buyer quotes, conversation, tasks and next step."""
    s = _snapshot()
    q = name_or_id.lower()
    lead = next((l for l in s["leads"] if l.id == name_or_id or q in l.input.fullName.lower() or q in l.input.companyName.lower()), None)
    if not lead:
        return json.dumps({"error": "not found"})
    a = _latest(s["assessments"]).get(lead.id)
    msgs = [{"direction": m.direction, "at": m.at, "subject": m.subject, "body": m.body[:400]} for m in s["messages"] if m.leadId == lead.id]
    return json.dumps({
        **_lead_brief(lead, _latest(s["assessments"])), "role": lead.input.role, "country": lead.input.country, "inquiry": lead.input.inquiry,
        "buyingSignals": {k: {"status": v.status, "quote": next((e.excerpt for e in v.evidence if e.excerpt), None)} for k, v in (a.bant.items() if a else [])},
        "nextAction": a.nextAction if a else None, "fitReasons": lead.icp.reasons if lead.icp else [],
        "conversation": msgs[-6:], "openTasks": [{"type": t.type, "owner": PEOPLE.get(t.ownerId, t.ownerId), "title": t.title} for t in s["tasks"] if t.leadId == lead.id and t.status in OPEN],
    }, default=str)


@function_tool
def tasks_for(person: str) -> str:
    """Open tasks, emails awaiting approval, replies and campaign emails due today for a person (name or id)."""
    pid = next((k for k, v in PEOPLE.items() if person.lower() in (k + " " + v).lower()), person)
    return json.dumps(my_day(pid), default=str)


@function_tool
def pipeline_stats(days: int = 7) -> str:
    """Pipeline KPIs for the last N days and the previous N days: new leads, speed to lead (seconds to first draft), emails sent,
    reply rate %, positive reply %, qualified, opportunities, opt-outs."""
    return json.dumps(pipeline_kpis(max(1, min(days, 90))), default=str)


@function_tool
def campaigns_overview() -> str:
    """Outbound campaigns with contacts enrolled, active, replied and completed."""
    s = _snapshot()
    enr = {e.id: e for e in s["enrollments"]}
    out = []
    for c in s["campaigns"]:
        states = [enr[i].state for i in c.enrollmentIds if i in enr]
        out.append({"name": c.name, "theme": c.theme, "contacts": len(states), "active": states.count("ACTIVE"),
                    "replied": states.count("STOPPED_REPLY"), "completed": states.count("COMPLETED"), "excluded": len(c.exclusions)})
    return json.dumps(out)


ASSISTANT_INSTRUCTIONS = """You are the Azentio Sales Workspace assistant for SDRs and account executives.
Answer questions about leads, tasks, approvals, replies, campaigns and pipeline performance using ONLY the tools. Never invent leads, numbers, meetings or deal values.
The workspace has no meeting calendar and no deal values: if asked about meetings or revenue, say so briefly and offer the closest available view (qualified leads, opportunities, tasks).
Style: concise, plain business English, no jargon (say "buying signals" not BANT, "fit" not ICP). Start with the direct answer. Use short "- " bullet lines for lists, at most 8 items, each with the person's full name and company exactly as returned by tools, plus the one detail that matters. End with one suggested next step when useful.
Never mention tools, APIs, models or JSON."""


# Ask the workspace assistant a question (Agents SDK agent with read-only data tools)
@router.post("/assistant/ask")
async def ask(body: AskInput) -> dict[str, Any]:
    who = PEOPLE.get(body.userId or "", "a sales team member")
    agent = build_agent("workspace_assistant", ASSISTANT_INSTRUCTIONS + f"\nThe user is {who} (id {body.userId}). 'My'/'me' refers to them.",
                        model=config.GENERATION_MODEL, tools=[search_leads, lead_details, tasks_for, pipeline_stats, campaigns_overview], effort="low")
    try:
        answer, meta = await run_agent(agent, body.question.strip()[:1000], max_turns=8)
    except LLMUnavailable:
        return {"answer": "I couldn't reach the assistant just now. Please try again in a moment.", "leads": []}
    text = str(answer or "").strip()
    names = {l.input.fullName: l.id for l in db.list_("leads", Lead, limit=5000)}
    mentioned = [{"leadId": i, "name": n} for n, i in names.items() if n in text]
    return {"answer": text, "leads": mentioned, "durationMs": meta.get("durationMs")}
