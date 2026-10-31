# app/api/performance.py - Campaign performance: funnel, inbound vs outbound, agent decisions vs human interventions
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from fastapi import APIRouter, Query

from app import db

router = APIRouter(prefix="/v1", tags=["performance"])

PEOPLE = {"ae.sara": "Sara Malik", "ae.khalid": "Khalid Al Mansoori", "ae.priya": "Priya Raman", "sdr.omar": "Nadia Karim",
          "sales.ops": "Sales Ops"}
AGENT_WHO = "AI SDR"
ROUTES = ("rep_review", "human_review", "nurture", "suppressed", "closed", "existing_owner")
ROUTE_WORDS = {"rep_review": "Sales for review", "human_review": "SDR review", "nurture": "nurture",
               "suppressed": "the opt-out list", "closed": "closed (not a fit)", "existing_owner": "the existing account owner"}
INTENT_WORDS = {"interested": "interested", "not_now": "not now", "wrong_person": "wrong person", "referral": "referral",
                "unsubscribe": "asked to unsubscribe", "out_of_office": "out of office", "not_interested": "not interested",
                "other": "unclear"}
KIND_WORDS = {"qualifying_email": "a qualifying email", "event_followup": "an event follow-up", "ad_followup": "an ad follow-up",
              "gap_fill": "a follow-up question", "outbound_touch_1": "campaign email 1", "outbound_touch_2": "campaign email 2",
              "outbound_touch_3": "campaign email 3", "nurture_reminder": "a nurture reminder"}
FUNNEL_KEYS = ("drafted", "approved", "sent", "replied", "positive", "qualified", "opportunities")
LOG_MAX = 60


# Load every record of a kind as plain dicts (tolerates legacy rows that no longer match the models)
def _rows(kind: str) -> list[dict[str, Any]]:
    try:
        with db._lock:
            raw = db.conn().execute(f"SELECT body FROM {kind} ORDER BY created_at, rowid").fetchall()
    except Exception:  # noqa: BLE001 - missing table means no data
        return []
    out = []
    for (body,) in raw:
        try:
            d = json.loads(body)
        except (TypeError, ValueError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


# Parse an ISO timestamp into an aware datetime (None when absent or malformed)
def _ts(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# Window start: N days before the earlier of the real clock and the demo clock (demo sends may be dated ahead)
def _window_start(days: int) -> datetime:
    now = datetime.now(timezone.utc)
    try:
        from app.engagement import clock
        now = min(now, clock.demo_now())
    except Exception:  # noqa: BLE001 - demo clock is optional
        pass
    return now - timedelta(days=days)


# Integer percentage, or None when the denominator is zero
def _rate(num: int, den: int) -> Optional[int]:
    return round(100 * num / den) if den else None


# Display name for a person id
def _person(pid: Any) -> str:
    return PEOPLE.get(str(pid or ""), str(pid or "Sales team"))


# Lead's display name
def _name(lead: Optional[dict]) -> str:
    inp = (lead or {}).get("input") or {}
    return inp.get("fullName") or inp.get("email") or "a contact"


# Latest assessment per lead from a list of assessment dicts
def _latest(assessments: Iterable[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for a in assessments:
        lid = a.get("leadId")
        if lid and (lid not in out or str(a.get("createdAt") or "") >= str(out[lid].get("createdAt") or "")):
            out[lid] = a
    return out


# Whether an assessment signals a positive reply
def _is_positive(a: Optional[dict]) -> bool:
    return bool(a) and (a.get("replyIntent") == "interested" or a.get("intentStrength") == "buying")


# Lead ids classed as outbound: target-account source or any campaign draft
def _outbound_ids(leads: list[dict], drafts: list[dict]) -> set[str]:
    ids = {l.get("id") for l in leads if ((l.get("input") or {}).get("source")) == "target_account"}
    ids |= {d.get("leadId") for d in drafts if d.get("enrollmentId")}
    return {i for i in ids if i}


# Find when a task was accepted/returned from the lead history (falls back to task creation)
def _task_event_at(task: dict, lead: Optional[dict], status: str) -> Optional[str]:
    types = ("opportunity_accepted",) if status == "accepted" else ("task_returned",)
    for h in reversed((lead or {}).get("history") or []):
        if isinstance(h, dict) and h.get("type") in types and str(task.get("id") or "~") in str(h.get("detail") or ""):
            return h.get("at")
    return task.get("createdAt")


# Shared context: every collection plus window-filtered views
def _context(days: int) -> dict[str, Any]:
    start = _window_start(days)
    inw = lambda v: (t := _ts(v)) is not None and t >= start  # noqa: E731
    leads, drafts, messages = _rows("leads"), _rows("drafts"), _rows("messages")
    assessments, tasks = _rows("assessments"), _rows("tasks")
    by_lead = {l.get("id"): l for l in leads if l.get("id")}
    drafts_by_id = {d.get("id"): d for d in drafts if d.get("id")}
    replies = [m for m in messages if m.get("direction") == "inbound" and m.get("origin") != "form"]
    approved = [d for d in drafts if (inw(d.get("approvedAt")) if d.get("approvedAt")
                                      else d.get("status") in ("APPROVED", "SIMULATED_SENT") and inw(d.get("createdAt")))]
    opp_tasks = [t for t in tasks if t.get("type") == "ae_handoff" and t.get("status") == "accepted"
                 and inw(_task_event_at(t, by_lead.get(t.get("leadId")), "accepted"))]
    return {
        "start": start, "inw": inw, "leads": leads, "byLead": by_lead, "drafts": drafts, "draftsById": drafts_by_id,
        "messages": messages, "assessments": assessments, "tasks": tasks,
        "latest": _latest(assessments), "outboundIds": _outbound_ids(leads, drafts),
        "wLeads": [l for l in leads if inw(l.get("createdAt"))],
        "wActiveLeads": [l for l in leads if inw(l.get("createdAt")) or inw(l.get("updatedAt"))],
        "wDrafts": [d for d in drafts if inw(d.get("createdAt"))],
        "wApproved": approved,
        "wSent": [m for m in messages if m.get("direction") == "outbound" and inw(m.get("at"))],
        "wReplies": [m for m in replies if inw(m.get("at"))],
        "wOppTasks": opp_tasks,
    }


# Funnel counts restricted to a set of lead ids (None = all leads)
def _funnel(c: dict, ids: Optional[set[str]] = None) -> dict[str, Any]:
    keep = (lambda lid: True) if ids is None else (lambda lid: lid in ids)  # noqa: E731
    sent = [m for m in c["wSent"] if keep(m.get("leadId"))]
    contacted = {m.get("leadId") for m in c["messages"] if m.get("direction") == "outbound" and keep(m.get("leadId"))}
    replied = {m.get("leadId") for m in c["wReplies"] if keep(m.get("leadId"))}
    positive = {lid for lid in replied if _is_positive(c["latest"].get(lid))}
    return {
        "drafted": sum(1 for d in c["wDrafts"] if keep(d.get("leadId"))),
        "approved": sum(1 for d in c["wApproved"] if keep(d.get("leadId"))),
        "sent": len(sent),
        "replied": len(replied),
        "positive": len(positive),
        "qualified": sum(1 for l in c["wActiveLeads"] if keep(l.get("id")) and l.get("stage") in ("REP_REVIEW", "OPPORTUNITY")),
        "opportunities": len({t.get("leadId") for t in c["wOppTasks"] if keep(t.get("leadId"))}),
        "replyRate": _rate(len(replied & contacted), len(contacted)),
        "positiveRate": _rate(len(positive), len(replied)),
    }


# Inbound or outbound segment: lead count plus funnel and rates
def _segment(c: dict, outbound: bool) -> dict[str, Any]:
    all_ids = {l.get("id") for l in c["leads"] if l.get("id")}
    ids = c["outboundIds"] & all_ids if outbound else all_ids - c["outboundIds"]
    return {"leads": sum(1 for l in c["wLeads"] if l.get("id") in ids), **_funnel(c, ids)}


# Per-campaign performance via its enrollments
def _campaigns(c: dict) -> list[dict[str, Any]]:
    enrollments = {e.get("id"): e for e in _rows("enrollments") if e.get("id")}
    out = []
    for cmp in sorted(_rows("campaigns"), key=lambda x: str(x.get("createdAt") or ""), reverse=True):
        enr_ids = set(cmp.get("enrollmentIds") or []) | {e_id for e_id, e in enrollments.items() if e.get("campaignId") == cmp.get("id")}
        enrs = [enrollments[i] for i in enr_ids if i in enrollments]
        lead_ids = {e.get("leadId") for e in enrs if e.get("leadId")}
        cdrafts = {d.get("id") for d in c["drafts"] if d.get("enrollmentId") in enr_ids}
        cdrafts |= {i for e in enrs for i in (e.get("touchDraftIds") or [])}
        sent = [m for m in c["wSent"] if m.get("enrollmentId") in enr_ids or (m.get("draftId") and m.get("draftId") in cdrafts)]
        contacted = {m.get("leadId") for m in c["messages"] if m.get("direction") == "outbound"
                     and (m.get("enrollmentId") in enr_ids or (m.get("draftId") and m.get("draftId") in cdrafts))}
        replied = {m.get("leadId") for m in c["wReplies"] if m.get("leadId") in lead_ids}
        positive = {lid for lid in replied if _is_positive(c["latest"].get(lid))}
        leads = [c["byLead"][i] for i in lead_ids if i in c["byLead"]]
        out.append({
            "id": cmp.get("id"), "name": cmp.get("name") or "", "theme": cmp.get("theme") or "",
            "contacts": len(enrs),
            "drafted": sum(1 for d in c["wDrafts"] if d.get("id") in cdrafts),
            "approved": sum(1 for d in c["wApproved"] if d.get("id") in cdrafts),
            "sent": len(sent),
            "replied": len(replied),
            "positive": len(positive),
            "qualified": sum(1 for l in leads if l.get("stage") in ("REP_REVIEW", "OPPORTUNITY")),
            "optOuts": len({l.get("id") for l in leads if l.get("suppressed")}
                           | {e.get("leadId") for e in enrs if e.get("state") == "STOPPED_SUPPRESSION"}),
            "stoppedOnReply": sum(1 for e in enrs if e.get("state") == "STOPPED_REPLY"),
            "replyRate": _rate(len(replied & contacted), len(contacted)),
            "positiveRate": _rate(len(positive), len(replied)),
        })
    return out


# What the agent decided on its own: AI usage, drafts, guardrail blocks, reply reads and routing
def _agent_decisions(c: dict) -> dict[str, Any]:
    inw = c["inw"]
    calls = [a for a in _rows("ai_calls") if inw(a.get("at"))]
    tokens = sum(int(a.get("inputTokens") or 0) + int(a.get("outputTokens") or 0) for a in calls)
    w_asm = [a for a in c["assessments"] if inw(a.get("createdAt"))]
    routed = {r: 0 for r in ROUTES}
    for a in _latest(w_asm).values():
        r = a.get("route")
        if r:
            routed[r] = routed.get(r, 0) + 1
    return {
        "aiCalls": len(calls),
        "aiTokens": tokens,
        "draftsGenerated": sum(1 for d in c["wDrafts"] if (d.get("mode") or "live") == "live"),
        "guardrailBlocks": sum(1 for d in c["wDrafts"] if d.get("status") == "BLOCKED" or d.get("guardrailFlags")),
        "repliesClassified": sum(1 for a in w_asm if a.get("replyIntent")),
        "routed": routed,
    }


# Owner-change history entries within the window, as (lead, entry) pairs
def _owner_changes(c: dict) -> list[tuple[dict, dict]]:
    return [(l, h) for l in c["leads"] for h in (l.get("history") or [])
            if isinstance(h, dict) and h.get("type") == "owner_changed" and c["inw"](h.get("at"))]


# Task status changes (accepted/returned) within the window, as (task, at) pairs
def _task_changes(c: dict, status: str, types: tuple[str, ...] = ("ae_handoff",)) -> list[tuple[dict, str]]:
    out = []
    for t in c["tasks"]:
        if t.get("type") in types and t.get("status") == status:
            at = _task_event_at(t, c["byLead"].get(t.get("leadId")), status)
            if c["inw"](at):
                out.append((t, at))
    return out


# What people did: approvals, edits, rejections, sends, reassignments, handoff decisions, completed tasks
def _human_interventions(c: dict) -> dict[str, Any]:
    approved_ids = {d.get("id") for d in c["drafts"] if d.get("approvedBy")}
    return {
        "approvals": sum(1 for d in c["wApproved"] if d.get("approvedBy")),
        "edits": sum(1 for d in c["wDrafts"] if int(d.get("version") or 1) > 1),
        "rejections": sum(1 for d in c["wDrafts"] if d.get("status") == "REJECTED"),
        "manualSends": sum(1 for m in c["wSent"] if m.get("draftId") in approved_ids),
        "ownerChanges": len(_owner_changes(c)),
        "handoffsAccepted": len(_task_changes(c, "accepted")),
        "handoffsReturned": len(_task_changes(c, "returned")),
        "tasksCompleted": sum(1 for t in c["tasks"] if t.get("status") == "done" and c["inw"](t.get("createdAt"))),
    }


# One decision-log entry
def _event(at: Any, actor: str, who: str, lead_id: Any, lead: Optional[dict], text: str) -> dict[str, Any]:
    return {"at": at, "actor": actor, "who": who, "leadId": lead_id, "leadName": _name(lead), "text": text}


# Merged agent + human decisions in plain business language, newest first
def _decision_log(c: dict) -> list[dict[str, Any]]:
    inw, by_lead, log = c["inw"], c["byLead"], []
    for a in c["assessments"]:
        if not inw(a.get("createdAt")):
            continue
        lid, lead = a.get("leadId"), by_lead.get(a.get("leadId"))
        route = ROUTE_WORDS.get(a.get("route") or "", a.get("route") or "review")
        if a.get("replyIntent"):
            intent = INTENT_WORDS.get(a["replyIntent"], a["replyIntent"])
            text = f"Read {_name(lead)}'s reply: {intent}, routed to {route}"
        else:
            text = f"Reviewed {_name(lead)}'s profile and fit, routed to {route}"
        log.append(_event(a.get("createdAt"), "agent", AGENT_WHO, lid, lead, text))
    for d in c["drafts"]:
        lid, lead = d.get("leadId"), by_lead.get(d.get("leadId"))
        if inw(d.get("createdAt")):
            kind = KIND_WORDS.get(d.get("kind") or "", "an email")
            log.append(_event(d.get("createdAt"), "agent", AGENT_WHO, lid, lead, f"Drafted {kind} for {_name(lead)}"))
            if d.get("status") == "BLOCKED" or d.get("guardrailFlags"):
                log.append(_event(d.get("createdAt"), "agent", AGENT_WHO, lid, lead,
                                  f"Held the email to {_name(lead)} for review: it failed a safety check"))
            if d.get("status") == "REJECTED":
                log.append(_event(d.get("createdAt"), "human", "Sales team", lid, lead, f"Rejected the draft email to {_name(lead)}"))
        if d.get("approvedAt") and inw(d.get("approvedAt")):
            who = d.get("approvedBy") or "Sales team"
            log.append(_event(d.get("approvedAt"), "human", _person(who), lid, lead, f"Approved email to {_name(lead)}"))
    for lead, h in _owner_changes(c):
        new_owner = str(h.get("detail") or "").split("->")[-1].strip()
        log.append(_event(h.get("at"), "human", "Sales team", lead.get("id"), lead, f"Reassigned {_name(lead)} to {_person(new_owner)}"))
    for t, at in _task_changes(c, "accepted"):
        lead = by_lead.get(t.get("leadId"))
        log.append(_event(at, "human", _person(t.get("ownerId")), t.get("leadId"), lead,
                          f"Accepted the handoff for {_name(lead)} as an opportunity"))
    for t, at in _task_changes(c, "returned", ("ae_handoff", "sdr_followup", "sdr_review", "owner_task", "referral_review", "identity_review")):
        lead = by_lead.get(t.get("leadId"))
        log.append(_event(at, "human", _person(t.get("ownerId")), t.get("leadId"), lead,
                          f"Returned {_name(lead)} for more qualification"))
    log.sort(key=lambda e: _ts(e["at"]) or c["start"], reverse=True)
    return log[:LOG_MAX]


# Campaign performance dashboard: funnel, inbound vs outbound, campaigns, agent vs human decisions
@router.get("/performance")
def performance(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    c = _context(days)
    full = _funnel(c)
    return {
        "days": days,
        "funnel": {k: full[k] for k in FUNNEL_KEYS},
        "inbound": _segment(c, outbound=False),
        "outbound": _segment(c, outbound=True),
        "campaigns": _campaigns(c),
        "agentDecisions": _agent_decisions(c),
        "humanInterventions": _human_interventions(c),
        "decisionLog": _decision_log(c),
    }
