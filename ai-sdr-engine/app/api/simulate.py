# app/api/simulate.py - Demo helper: simulate realistic prospect replies for an outbound campaign
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import db
from app.contracts import Campaign, Draft, Enrollment, Lead, Message

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["simulate"])

SIM_MODEL = "gpt-6-luna"
LLM_TIMEOUT_S = 45
SCENARIOS = (
    "ready_to_buy", "objection_incumbent_competitor", "objection_price", "curious_not_buying",
    "referral", "not_now", "unsubscribe",
)

SCENARIO_BRIEFS = {
    "ready_to_buy": (
        "You are ready to buy. In 2-3 sentences state ALL of: an explicit approved budget with an amount and currency; "
        "that you personally own the decision (or co-own it with a named role such as the CIO); the concrete business "
        "need this solves for your team; and a dated timeline (a specific month/quarter and year after today). "
        "Ask for a call or demo."
    ),
    "objection_incumbent_competitor": (
        "You already use an incumbent vendor for this. Name ONE plausible incumbent such as Temenos, Finastra, "
        "Oracle FLEXCUBE or Path Solutions (for AML/compliance themes, refer to that vendor's compliance module or "
        "your in-house system built on it), say the contract or relationship makes switching hard right now, "
        "but leave a small opening (e.g. what would make you look again)."
    ),
    "objection_price": (
        "You are interested but worried about cost: say budgets are tight / you suspect it is too expensive for an "
        "institution your size, and ask roughly what this kind of platform costs before going further."
    ),
    "curious_not_buying": (
        "You are curious but not buying: ask one or two general questions about how it works or what other banks "
        "do, and make clear there is no project, budget or timeline yet - you are just keeping up to date."
    ),
    "referral": (
        "You are not the right person. Refer them to a named colleague (realistic first and last name, plausible "
        "title and an email address at your company domain) who owns this area, and say they can mention you."
    ),
    "not_now": (
        "Not now: say the team is tied up with another priority (e.g. a core migration, year-end close or a "
        "regulatory audit) and ask them to reconnect in a specific later quarter."
    ),
    "unsubscribe": (
        "You want no more emails: politely but clearly ask to be removed from the mailing list and not contacted again."
    ),
}

FIXTURE_REPLIES = {
    "ready_to_buy": (
        "Thanks for reaching out - your timing is good. We have USD 450,000 approved in our FY2027 budget for this and "
        "I own the decision together with our CIO; our current processes in this area are too manual for the volumes "
        "we now handle. We want a vendor selected by 31 March 2027, so could we set up a demo next week?"
    ),
    "objection_incumbent_competitor": (
        "Appreciate the note. We run Temenos today and are locked into that contract until late 2027, so switching "
        "isn't realistic right now. If you have something that sits alongside it rather than replacing it, I'd be open "
        "to a short summary."
    ),
    "objection_price": (
        "This is interesting, but budgets at {company} are tight this year and platforms like this usually end up "
        "too expensive for a bank our size. Before we go any further, roughly what does it cost?"
    ),
    "curious_not_buying": (
        "Thanks - out of curiosity, how are other banks in the region approaching {theme}? We don't have a project "
        "or budget for this at the moment; I'm just trying to keep up with what's out there."
    ),
    "referral": (
        "Thanks, but I'm not the right person for this. Please speak to Samir Haddad, our Head of Operations "
        "(samir.haddad@{domain}) - he owns this area and you can mention my name."
    ),
    "not_now": (
        "Thanks for following up. We're in the middle of a core banking migration and the team has no capacity "
        "until it's done - please reconnect with me in Q3 2027."
    ),
    "unsubscribe": "Please remove me from your mailing list and do not contact me again.",
}

INSTRUCTIONS = (
    "You write a realistic email reply AS a bank/financial-institution executive (the prospect) who just received a "
    "cold outbound email from Azentio's sales team. Write in the first person as the prospect, plain text, 1-3 "
    "sentences, no subject line, no greeting line like 'Hi team,' is required, sign off with just your first name. "
    "Follow the scenario brief exactly; stay consistent with your title, company and the email you received. "
    "Do not mention that this is simulated."
)


class SimulateRequest(BaseModel):
    count: int = Field(default=3, ge=1, le=50)


class SimulatedReply(BaseModel):
    bodyText: str


# JSON error body matching the /v1 router convention
def _error(status: int, code: str, message: str = "") -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message or code}}, status_code=status)


# Outbound messages for a lead, oldest first
def _outbound(lead_id: str) -> list[Message]:
    return [m for m in db.list_("messages", Message, lead_id=lead_id, limit=100000) if m.direction == "outbound"]


# True when the lead already replied after our latest outbound email (or at all, if never emailed)
def _already_replied(lead_id: str, outbound: list[Message]) -> bool:
    inbound = [m for m in db.list_("messages", Message, lead_id=lead_id, limit=100000) if m.direction == "inbound"]
    if not inbound:
        return False
    return not outbound or max(m.at for m in inbound) >= outbound[-1].at


# Number of campaign contacts that have already replied (rotation offset so repeat calls vary scenarios)
def _replied_count(campaign: Campaign) -> int:
    lead_ids = {e.leadId for i in campaign.enrollmentIds if (e := db.get("enrollments", i, Enrollment))}
    return sum(1 for lid in lead_ids
               if any(m.direction == "inbound" for m in db.list_("messages", Message, lead_id=lid, limit=100000)))


# Pick (enrollment, lead, last outbound) for contacts already emailed, else ACTIVE enrollments
def _eligible(campaign: Campaign) -> list[tuple[Enrollment, Lead, Optional[Message]]]:
    emailed: list[tuple[Enrollment, Lead, Optional[Message]]] = []
    active: list[tuple[Enrollment, Lead, Optional[Message]]] = []
    for enr_id in campaign.enrollmentIds:
        enr = db.get("enrollments", enr_id, Enrollment)
        lead = db.get("leads", enr.leadId, Lead) if enr else None
        if not enr or not lead or lead.suppressed or enr.state not in ("ACTIVE", "COMPLETED"):
            continue
        sent = _outbound(lead.id)
        if _already_replied(lead.id, sent):
            continue
        campaign_sent = [m for m in sent if m.enrollmentId == enr.id] or sent
        if campaign_sent:
            emailed.append((enr, lead, campaign_sent[-1]))
        elif enr.state == "ACTIVE":
            active.append((enr, lead, None))
    return emailed or active


# Subject/body of the email the prospect is replying to (sent message, else the first touch draft)
def _last_email(enr: Enrollment, last: Optional[Message]) -> tuple[str, str]:
    if last:
        return last.subject, last.body
    for draft_id in enr.touchDraftIds:
        draft = db.get("drafts", draft_id, Draft)
        if draft:
            return draft.subject, draft.body
    return "", ""


# Prospect facts used for both the model prompt and fixture text
def _prospect(lead: Lead) -> dict[str, str]:
    inp = lead.input
    company = (lead.enrichment.company if lead.enrichment else None) or {}
    domain = inp.email.rsplit("@", 1)[-1] if "@" in inp.email else "example.com"
    return {
        "name": inp.fullName, "title": inp.role or "", "company": inp.companyName or company.get("name") or "",
        "country": inp.country or "", "domain": domain,
    }


# Fixture reply text for a scenario, personalised with company/theme/domain
def _fixture_reply(scenario: str, prospect: dict[str, str], theme: str) -> str:
    first = prospect["name"].split()[0] if prospect["name"] else ""
    body = FIXTURE_REPLIES[scenario].format(company=prospect["company"] or "our bank", theme=theme,
                                            domain=prospect["domain"])
    return f"{body}\n\n{first}".strip()


# Generate one reply with gpt-6-luna; fall back to the fixture text on any failure
async def _generate(scenario: str, prospect: dict[str, str], theme: str, subject: str, body: str) -> str:
    from app.llm.client import structured

    today = datetime.now(timezone.utc).strftime("%d %B %Y")
    prompt = (
        f"Today is {today}.\n"
        f"You are {prospect['name']}, {prospect['title']} at {prospect['company']} ({prospect['country']}); "
        f"your email domain is {prospect['domain']}.\n"
        f"Campaign theme of the email you received: {theme}\n"
        f"Email subject: {subject or '(none)'}\n"
        f"Email body:\n{body or '(not available)'}\n\n"
        f"Scenario: {scenario}\nScenario brief: {SCENARIO_BRIEFS[scenario]}\n"
        "Write only the reply text."
    )
    try:
        out, _meta = await asyncio.wait_for(
            structured("reply_simulator", INSTRUCTIONS, prompt, SimulatedReply, model=SIM_MODEL), LLM_TIMEOUT_S)
        text = (out.bodyText or "").strip()
        if text:
            return text
    except Exception as exc:
        log.warning("reply simulator fell back to fixture for %s: %s", scenario, exc)
    return _fixture_reply(scenario, prospect, theme)


# Simulate varied prospect replies for emailed campaign contacts and ingest them through the normal reply path
@router.post("/campaigns/{campaign_id}/simulate-replies")
async def simulate_replies(campaign_id: str, body: Optional[SimulateRequest] = None) -> Any:
    from app import pipeline
    from app.contracts import ReplyInput

    campaign = db.get("campaigns", campaign_id, Campaign)
    if campaign is None:
        return _error(404, "not_found", f"campaign {campaign_id}")
    count = min((body or SimulateRequest()).count, len(SCENARIOS))
    picked = _eligible(campaign)[:count]
    if not picked:
        return _error(409, "no_contacts_to_reply", "No enrolled contact in this campaign is waiting on a reply.")

    offset = _replied_count(campaign)
    plans = []
    for idx, (enr, lead, last) in enumerate(picked):
        subject, text = _last_email(enr, last)
        plans.append((lead, SCENARIOS[(offset + idx) % len(SCENARIOS)], _prospect(lead), subject, text))
    bodies = await asyncio.gather(*(
        _generate(scenario, prospect, campaign.theme, subject, text)
        for _lead, scenario, prospect, subject, text in plans
    ))

    replies: list[dict[str, Any]] = []
    for (lead, scenario, _prospect_facts, _subject, _text), reply_text in zip(plans, bodies):
        try:
            result = await pipeline.ingest_reply(lead.id, ReplyInput(bodyText=reply_text, origin="scripted"))
        except (LookupError, ValueError) as exc:
            log.warning("simulated reply for %s not ingested: %s", lead.id, exc)
            continue
        replies.append({"leadId": lead.id, "name": lead.input.fullName, "scenario": scenario,
                        "bodyText": reply_text, "runId": result.get("runId")})
    return {"replies": replies}
