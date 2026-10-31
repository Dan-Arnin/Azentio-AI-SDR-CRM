# app/api/workspace.py - Workspace-facing endpoints: agent activity trace per lead, owner assignment, team list
from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import db
from app.contracts import Assessment, Draft, Lead, Message, Run, Task, now_iso
from app.llm import calllog

router = APIRouter(prefix="/v1", tags=["workspace"])

TEAM = [
    {"id": "ae.sara", "name": "Sara Malik", "role": "Account Executive"},
    {"id": "ae.khalid", "name": "Khalid Al Mansoori", "role": "Account Executive"},
    {"id": "ae.priya", "name": "Priya Raman", "role": "Account Executive"},
    {"id": "sdr.omar", "name": "Nadia Karim", "role": "Sales Development"},
]
STEP_AGENT = {
    "identity": ("CRM check", "Checked the CRM for duplicates and existing account ownership"),
    "enrich": ("Research", "Researched the company and contact"),
    "icp": ("Fit scoring", "Scored fit against the product's target profile"),
    "consent": ("Compliance", "Checked consent and opt-out lists"),
    "initial_assessment": ("Qualification", "Set the starting view of buying signals from the enquiry"),
    "draft": ("Writing", "Drafted an email for human approval"),
    "send": ("Email", "Sent the approved email"),
    "reply_received": ("Inbox", "Received a reply"),
    "stop_sequences": ("Campaigns", "Paused the campaign because the contact replied"),
    "assess_reply": ("Qualification", "Read the reply and judged intent and buying signals"),
    "route": ("Routing", "Decided who should own the next step"),
    "cohort_search": ("Campaigns", "Found contacts matching the campaign audience"),
    "enroll": ("Campaigns", "Enrolled eligible contacts"),
    "suppress": ("Compliance", "Stopped all outreach to this contact"),
    "due_touches": ("Scheduler", "Checked which campaign emails are due"),
    "advance": ("Scheduler", "Moved the workspace date forward"),
}


FIELD_NAMES = {"institutionType": "institution type", "employeeCount": "employee count", "title": "job title", "seniority": "seniority",
               "country": "country", "industry": "industry", "companyName": "company name", "department": "department"}


# Human-readable list of missing fields
def _unknown(fields: list[str]) -> list[str]:
    return [f"Still unknown: {', '.join(FIELD_NAMES.get(f, f) for f in fields)}"] if fields else []


class OwnerInput(BaseModel):
    ownerId: str


# Index of approved knowledge claims: id -> {text, sourceUrl}
@lru_cache(maxsize=1)
def _claims() -> dict[str, dict[str, str]]:
    from app.engagement.drafting import knowledge

    kb, out = knowledge(), {}
    groups = [kb.get("company", {})] + list(kb.get("products", {}).values())
    for g in groups:
        for c in g.get("claims", []):
            out[c["id"]] = {"text": c["text"], "sourceUrl": c.get("sourceUrl") or g.get("sourceUrl", "")}
    for th in kb.get("themes", {}).values():
        for a in th.get("angles", []):
            out[a["id"]] = {"text": a["text"], "sourceUrl": a.get("sourceUrl") or th.get("sourceUrl", "")}
    return out


# Research event: what was looked up and which quoted sources back it
def _research_event(lead: Lead) -> Optional[dict[str, Any]]:
    enr = lead.enrichment
    if not enr:
        return None
    co = enr.company or {}
    if enr.provider == "apollo":
        facts = [f"{k}: {v}" for k, v in {**co, **enr.person}.items()
                 if k in ("name", "industry", "institutionType", "employeeCount", "hq", "country", "title", "seniority")]
        return {"at": enr.fetchedAt, "agent": "Research", "mode": "lookup", "step": "enrich",
                "title": "Live Apollo enrichment" if enr.found else "Apollo lookup skipped",
                "summary": "Company data returned by Apollo; not buyer-confirmed BANT." if enr.found else "No live Apollo match was used.",
                "details": facts + _unknown(enr.missing) + list(enr.errors),
                "sources": [{"title": "Apollo record", "quote": e.excerpt,
                             "url": "https://app.apollo.io/#/organizations/" + str(co['apolloId']) if co.get('apolloId') else None}
                            for e in enr.evidence]}
    if enr.provider == "public_research":
        sources = [{"title": s.get("title") or s.get("url"), "url": s.get("url"), "quote": s.get("excerpt"), "field": s.get("field")}
                   for s in co.get("sources") or []]
        labels = {"officialCompanyName": "Official name", "industry": "Industry", "hq": "Headquarters", "country": "Country",
                  "institutionType": "Institution type", "employeeCount": "Employees", "website": "Website", "description": "About"}
        facts = [f"{labels[k]}: {v}" for k, v in co.items() if k in labels and v not in (None, "", [])]
        summary = (f"Searched {co.get('domain') or 'the company website'} on the live web and kept only facts quoted from retrieved pages."
                   if enr.found else "Searched the company's website on the live web; nothing could be verified, so nothing was assumed.")
        return {"at": enr.fetchedAt, "agent": "Research", "mode": "ai", "step": "enrich", "title": "Live web research", "summary": summary,
                "details": facts[:8] + _unknown(enr.missing), "sources": sources}
    facts = [f"{k}: {v}" for k, v in {**co, **(enr.person or {})}.items()
             if k in ("name", "industry", "institutionType", "employeeCount", "hq", "country", "title", "seniority") and v not in (None, "")]
    conflicts = [f"Form says {c['field']} is “{c.get('form')}”, profile says “{c.get('apollo')}”; kept what the buyer told us" for c in enr.conflicts]
    return {"at": enr.fetchedAt, "agent": "Research", "mode": "rule", "step": "enrich", "title": "Company profile lookup (database, no AI)",
            "summary": "Matched the company and contact in the company profile database. Web research runs only for real company domains."
                       if enr.found else "No profile found for this company.",
            "details": facts + conflicts + _unknown(enr.missing), "sources": []}


# Qualification event for one assessment (intent judgment + verified buyer quotes)
def _assessment_event(a: Assessment) -> dict[str, Any]:
    names = {"budget": "Budget", "authority": "Decision maker", "need": "Need", "timeline": "Timeline"}
    words = {"confirmed": "confirmed", "inferred": "likely", "missing": "unknown", "conflict": "unclear"}
    details, sources = [], []
    if a.replyIntent and a.decision:
        conf = a.decision.intentConfidence
        details.append(f"Reply intent: {a.replyIntent.replace('_', ' ')} ({round(conf * 100)}% confidence)")
        for k, v in a.decision.bantSupport.items():
            c = a.decision.confidenceByQuestion.get(k)
            details.append(f"Does the reply establish {names.get(k, k).lower()}? {v}" + (f" ({round(c * 100)}%)" if isinstance(c, (int, float)) else ""))
    for k in ("need", "timeline", "authority", "budget"):
        f = a.bant.get(k)
        if not f:
            continue
        ev = next((e for e in f.evidence if e.excerpt), None)
        details.append(f"{names[k]}: {words.get(f.status, f.status)}")
        if ev:
            sources.append({"title": names[k] + (" · from their reply" if ev.origin == "buyer_statement" else " · from their enquiry"), "quote": ev.excerpt, "url": None})
    title = "Read the reply and qualified" if a.replyIntent else "Starting buying signals from the form (rules; AI reads replies)"
    return {"at": a.createdAt, "agent": "Qualification", "mode": "ai" if a.replyIntent else "rule", "step": "assess_reply" if a.replyIntent else None, "title": title,
            "summary": f"Qualification score {a.qualificationScore}/100. " + " ".join(a.routeReasons[:1]),
            "details": details, "sources": sources}


# Writing event for one draft (which approved Azentio statements it relied on, guardrail result)
def _draft_event(d: Draft) -> dict[str, Any]:
    claims = _claims()
    sources = [{"title": "Approved Azentio statement", "quote": claims[c]["text"], "url": claims[c]["sourceUrl"]} for c in d.claimIds if c in claims]
    guard = "Guardrails passed: no pricing, commitments or unsupported claims." if not d.guardrailFlags else \
        "Guardrails flagged: " + ", ".join(f.replace("_", " ") for f in d.guardrailFlags)
    return {"at": d.createdAt, "agent": "Writing", "mode": "ai" if d.mode == "live" else "rule", "step": "draft",
            "title": f"Drafted “{d.subject}”" + ("" if d.mode == "live" else " (template; AI unavailable)"),
            "summary": guard + (f" Asks: “{d.discoveryQuestion}”" if d.discoveryQuestion else ""),
            "details": [f"Status: {d.status.replace('SIMULATED_SENT', 'sent').replace('_', ' ').lower()}"], "sources": sources}


# Full, time-ordered agent activity for one lead
@router.get("/leads/{lead_id}/agent-trace")
def agent_trace(lead_id: str) -> dict[str, Any]:
    lead = db.get("leads", lead_id, Lead)
    if not lead:
        raise HTTPException(404, detail={"code": "not_found"})
    events: list[dict[str, Any]] = []
    for run in db.list_("runs", Run, lead_id=lead_id):
        for s in run.steps:
            name = s.get("name", "")
            if name in ("enrich", "icp", "assess_reply", "initial_assessment", "draft") or name not in STEP_AGENT:
                continue
            agent, title = STEP_AGENT[name]
            events.append({"at": s.get("at") or run.startedAt, "agent": agent, "title": title, "summary": "", "mode": "system" if name == "send" else "rule",
                           "details": [], "sources": [], "status": s.get("status"), "durationMs": s.get("durationMs")})
    research = _research_event(lead)
    if research:
        events.append(research)
    if lead.icp:
        events.append({"at": (lead.enrichment.fetchedAt if lead.enrichment else lead.createdAt), "agent": "Fit scoring", "mode": "rule",
                       "title": f"Fit: {lead.icp.fit} ({lead.icp.score}/100)", "summary": "",
                       "details": [f"{'✓' if c.passed else '✗' if c.passed is False else '–'} {c.rule.replace('_', ' ')}: {c.points}/{c.weight}" for c in lead.icp.checks],
                       "sources": []})
    events += [_assessment_event(a) for a in db.list_("assessments", Assessment, lead_id=lead_id)]
    events += [_draft_event(d) for d in db.list_("drafts", Draft, lead_id=lead_id)]
    for m in db.list_("messages", Message, lead_id=lead_id):
        events.append({"at": m.at, "agent": "Email" if m.direction == "outbound" else "Inbox",
                       "title": ("Email sent: " if m.direction == "outbound" else "Reply received: ") + (m.subject or ""),
                       "summary": ("Delivered to the controlled inbox" if m.deliveryStatus == "accepted" else "") if m.direction == "outbound" else m.body[:160],
                       "details": [], "sources": []})
    for t in db.list_("tasks", Task, lead_id=lead_id):
        who = next((p["name"] for p in TEAM if p["id"] == t.ownerId), t.ownerId)
        events.append({"at": t.createdAt, "agent": "Routing", "title": f"Handed to {who}", "summary": t.title, "details": [f"Status: {t.status}"], "sources": []})
    events.sort(key=lambda e: e.get("at") or "")
    calls = calllog.for_lead(lead_id)
    _attach_calls(events, calls)
    summary = {"aiCalls": len(calls), "failedCalls": sum(1 for c in calls if not c.ok),
               "tokens": sum((c.inputTokens or 0) + (c.outputTokens or 0) for c in calls),
               "modelSeconds": round(sum(c.durationMs for c in calls) / 1000, 1),
               "models": sorted({c.model for c in calls}), "providers": sorted({c.provider for c in calls})}
    return {"leadId": lead_id, "summary": summary, "events": events}


# Attach each logged AI call to the first AI event of the same pipeline step at or after the call
def _attach_calls(events: list[dict[str, Any]], calls: list) -> None:
    for e in events:
        e.setdefault("mode", "rule")
        e["aiCalls"] = []
    for c in calls:
        targets = [e for e in events if e.get("step") == c.step and e.get("mode") == "ai"]
        if not targets:
            continue
        later = [e for e in targets if (e.get("at") or "") >= c.at[:19]]
        (later[0] if later else targets[-1])["aiCalls"].append(
            {"purpose": c.purpose.replace("_", " "), "provider": c.provider, "model": c.model, "ok": c.ok, "durationMs": c.durationMs,
             "tokens": (c.inputTokens or 0) + (c.outputTokens or 0), "at": c.at})


# Team members who can own a lead
@router.get("/team")
def team() -> dict[str, Any]:
    return {"items": TEAM}


# Assign or change the owner of a lead (and its open AE handoff)
@router.post("/leads/{lead_id}/owner")
def set_owner(lead_id: str, body: OwnerInput) -> dict[str, Any]:
    from app import pipeline

    lead = db.get("leads", lead_id, Lead)
    if not lead:
        raise HTTPException(404, detail={"code": "not_found"})
    if body.ownerId not in {p["id"] for p in TEAM}:
        raise HTTPException(422, detail={"code": "unknown_owner"})
    previous = lead.ownerId
    lead.ownerId = body.ownerId
    if body.ownerId.startswith("ae.") and not lead.accountOwnerId:
        lead.accountOwnerId = body.ownerId
    lead.history.append({"at": now_iso(), "type": "owner_changed", "detail": f"{previous} -> {body.ownerId}"})
    lead.updatedAt = now_iso()
    db.put("leads", lead)
    if body.ownerId.startswith("ae."):
        for t in db.list_("tasks", Task, lead_id=lead_id):
            if t.type == "ae_handoff" and t.status in ("open", "acknowledged"):
                t.ownerId = body.ownerId
                db.put("tasks", t)
    pipeline.emit("lead.owner_changed", lead, fromOwner=previous, toOwner=body.ownerId)
    return {"leadId": lead_id, "ownerId": body.ownerId}
