# app/engine/qualify.py - BANT assessment, intent strength, objections/competitors, qualification scoring and deterministic route selection
from __future__ import annotations

import asyncio
import re
from calendar import monthrange
from datetime import date
from math import isfinite
from typing import Any, Optional

from app import config
from app.contracts import BANT_KEYS, Assessment, BantField, DecisionResult, Evidence, Lead, Message
from app.engine.extraction import Extraction, extract_bant, fixture_extract
from app.llm.decisions import UNSUBSCRIBE_RE, decide, intent_strength, refusal_result

GAP_ORDER = ("need", "timeline", "authority", "budget")
SCORE = {"confirmed": 25, "inferred": 10, "missing": 0, "conflict": 0}
DECISION_MAKER_SENIORITY = {"c_suite", "c-suite", "cxo", "owner", "founder", "partner", "vp", "svp", "evp", "head", "director", "executive", "president"}
DECISION_MAKER_TITLE = re.compile(
    r"\b(chief|ceo|cio|cto|cfo|coo|cro|cdo|head|director|vp|vice president|president|managing director|general manager|gm|founder|owner)\b",
    re.I,
)
NEED_RE = re.compile(r"\b(replac\w*|moderni[sz]\w*|digiti[sz]\w*|migrat\w*|implement\w*|evaluat\w*|looking (for|to)|launch\w*|upgrad\w*|transform\w*|roll[- ]?out|project|legacy|need\w*|platform)\b", re.I)
BUDGET_RE = re.compile(r"\b(budget\w*|fund\w*|approved|allocated|capex)\b", re.I)
TIMELINE_RE = re.compile(r"\b(q[1-4]\s*20\d\d|20\d\d|this year|next year|quarter|within \d+ (months|weeks)|by (the )?end of)\b", re.I)
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
QUARTER_RE = re.compile(r"\bq([1-4])\s*(?:of\s*)?(?:fy\s*)?'?((?:20)?\d\d)\b", re.I)
HALF_RE = re.compile(r"\bh([12])\s*(?:of\s*)?'?((?:20)?\d\d)\b", re.I)
MONTH_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*,?\s*(20\d\d)\b", re.I)
FY_RE = re.compile(r"\bfy\s*'?((?:20)?\d\d)\b", re.I)
PRICING_ONLY_RE = re.compile(r"\b(price|pricing|cost|per user|licen[cs]e fee|quote|discount)\b", re.I)

QUESTIONS = {
    "need": "What is prompting you to look at this now — is there a specific platform or process you are looking to replace or improve?",
    "timeline": "Is there a timeframe you are working towards for evaluating options or making a decision?",
    "authority": "Who else would typically be involved in evaluating and signing off on a project like this?",
    "budget": "Has an initiative like this been scoped or funded for the coming financial year, or is that still being shaped?",
}

NEXT_ACTION = {
    "rep_review": "AE: review BANT evidence and accept opportunity",
    "nurture": "Nurture: reminder in 90 days",
    "suppressed": "Suppressed: block further outreach",
    "closed": "Close: outside ICP — no outreach",
    "existing_owner": "Owner: route to existing opportunity owner — no SDR outreach",
}


# Consultative gap-fill question for the first unresolved BANT field (Need -> Timeline -> Authority -> Budget); never pricing
def next_question_for(missing: list[str]) -> str | None:
    for key in GAP_ORDER:
        if key in missing:
            return QUESTIONS[key]
    return None


# First sentence of text that matches a pattern (used as a form excerpt)
def _sentence_matching(text: str, pattern: re.Pattern) -> Optional[str]:
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if sent.strip() and pattern.search(sent):
            return sent.strip()
    return None


# Whether title/seniority from enrichment or form looks like a decision-maker; returns (bool, detail, origin)
def _seniority(lead: Lead) -> tuple[bool, Optional[str], str]:
    person = (lead.enrichment.person if lead.enrichment else {}) or {}
    seniority = str(person.get("seniority") or "").lower()
    title = person.get("title")
    enrichment_origin = ("mock_enrichment" if lead.enrichment and lead.enrichment.mock else
                         "apollo" if lead.enrichment and lead.enrichment.provider == "apollo" else "public_research")
    if seniority in DECISION_MAKER_SENIORITY or (title and DECISION_MAKER_TITLE.search(title)):
        return True, f"{title or seniority} (seniority: {seniority or 'n/a'})", "form" if lead.input.role else enrichment_origin
    role = lead.input.role
    if role and DECISION_MAKER_TITLE.search(role):
        return True, role, "form"
    return False, title or role, enrichment_origin if title and not lead.input.role else "form"


# Pre-reply BANT baseline from form inquiry + enrichment (never confirmed: no buyer conversation yet)
def _baseline_bant(lead: Lead) -> dict[str, BantField]:
    bant = {k: BantField() for k in BANT_KEYS}
    inquiry = lead.input.inquiry or ""
    for key, pattern, label in (("need", NEED_RE, "Form inquiry mentions a project"),
                                ("budget", BUDGET_RE, "Form inquiry mentions budget/funding"),
                                ("timeline", TIMELINE_RE, "Form inquiry mentions a timeframe")):
        excerpt = _sentence_matching(inquiry, pattern)
        if excerpt:
            bant[key] = BantField(status="inferred", note=f"{label}; not yet stated in conversation.",
                                  evidence=[Evidence(field=key, value=excerpt, excerpt=excerpt, origin="form", status="inferred")])
    is_dm, detail, origin = _seniority(lead)
    if is_dm:
        bant["authority"] = BantField(status="inferred", note="Title/seniority suggests decision-maker; title alone never confirms authority.",
                                      evidence=[Evidence(field="authority", value=detail, origin=origin, status="inferred")])
    return bant


# BANT score (25 confirmed / 10 inferred) and blended qualification score (40% ICP, 60% BANT)
def _scores(lead: Lead, bant: dict[str, BantField]) -> tuple[int, int]:
    bant_score = sum(SCORE[bant[k].status] for k in BANT_KEYS)
    icp_score = lead.icp.score if lead.icp else 0
    return bant_score, round(0.4 * icp_score + 0.6 * bant_score)


# Fields not yet confirmed, in canonical order
def _unresolved(bant: dict[str, BantField]) -> list[str]:
    return [k for k in BANT_KEYS if bant[k].status != "confirmed"]


# Pre-route checks shared by both assessments: suppression, existing opportunity, outside ICP
def _gate_route(lead: Lead) -> Optional[tuple[str, str]]:
    if lead.suppressed:
        return "suppressed", f"Lead is suppressed ({lead.suppressionReason or 'contact hold'}); no further outreach."
    if "existing_opportunity" in lead.flags:
        return "existing_owner", "Account has an existing opportunity; routed to its owner instead of SDR outreach."
    if lead.icp and lead.icp.status == "outside_icp":
        reasons = "; ".join(lead.icp.reasons[:3]) or "policy checks failed"
        return "closed", f"Outside ICP ({reasons})."
    return None


# Pre-reply assessment from ICP result + form inquiry (sync, no LLM)
def initial_assessment(lead: Lead) -> Assessment:
    bant = _baseline_bant(lead)
    bant_score, qual = _scores(lead, bant)
    missing = _unresolved(bant)
    reasons: list[str] = []
    next_question = None
    gate = _gate_route(lead)
    if gate:
        route, reason = gate
        reasons.append(reason)
        next_action = NEXT_ACTION[route]
    else:
        route = "human_review"
        if not lead.icp:
            reasons.append("No ICP result available; human review required.")
        elif lead.icp.status in ("needs_review", "needs_info"):
            reasons.append(f"ICP status {lead.icp.status}: {'; '.join(lead.icp.reasons[:3]) or 'needs a human check'}.")
        else:
            reasons.append(f"ICP match ({lead.icp.fit}, score {lead.icp.score}); BANT not yet confirmed by the buyer.")
        next_question = next_question_for(missing)
        next_action = "Send qualifying email (awaiting approval)"
    return Assessment(
        leadId=lead.id, leadVersion=lead.version,
        policyVersion=lead.icp.policyVersion if lead.icp else config.POLICY_VERSION,
        icp=lead.icp, bant=bant, bantScore=bant_score, qualificationScore=qual, missingFields=missing,
        route=route, routeReasons=reasons, nextQuestion=next_question, nextAction=next_action, mode="live",
    )


# Resolve an explicit dated window (quarter, half, month or FY plus year) to "start/end" ISO dates; bare years stay ambiguous
def resolve_timeline(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = QUARTER_RE.search(text)
    if m:
        q, year = int(m.group(1)), _full_year(m.group(2))
        return _window(year, 3 * q - 2, year, 3 * q)
    m = HALF_RE.search(text)
    if m:
        h, year = int(m.group(1)), _full_year(m.group(2))
        return _window(year, 1 if h == 1 else 7, year, 6 if h == 1 else 12)
    m = MONTH_RE.search(text)
    if m:
        month, year = MONTHS[m.group(1).lower()[:3]], int(m.group(2))
        return _window(year, month, year, month)
    m = FY_RE.search(text)
    if m:
        year = _full_year(m.group(1))
        return _window(year, 1, year, 12)
    return None


# Expand a two-digit year (27 -> 2027)
def _full_year(raw: str) -> int:
    year = int(raw)
    return year + 2000 if year < 100 else year


# Format an inclusive month range as "YYYY-MM-DD/YYYY-MM-DD"
def _window(y1: int, m1: int, y2: int, m2: int) -> str:
    return f"{date(y1, m1, 1).isoformat()}/{date(y2, m2, monthrange(y2, m2)[1]).isoformat()}"


# Merge one BANT field from verified excerpt + decision support + prior/baseline (deterministic rules)
def _merge_field(key: str, ext: Extraction, decision: DecisionResult, reply: Message,
                 prior: Optional[BantField], base: BantField) -> BantField:
    span = getattr(ext, key)
    if not span.excerpt:
        if prior and prior.status in ("confirmed", "inferred", "conflict"):
            return prior
        return base
    support = "unclear" if decision.refusal else decision.bantSupport.get(key, "unclear")
    if support == "yes":
        status, note = "confirmed", "Buyer statement verified verbatim; judge: explicitly established."
    elif support == "unclear":
        status, note = "inferred", "Buyer statement is hedged or ambiguous; judge: unclear."
    else:
        status, note = "conflict", "Excerpt found but judge says it does not establish this field."
    if status == "confirmed" and key in decision.confidenceByQuestion:
        try:
            confidence = float(decision.confidenceByQuestion[key])
        except (TypeError, ValueError):
            confidence = float("nan")
        if not isfinite(confidence) or not config.EVIDENCE_CONFIDENCE_FLOOR <= confidence <= 1:
            status, note = "inferred", "Buyer excerpt found, but this field's judgment confidence requires human review."
    if key == "budget" and status == "confirmed" and (ext.pricingOrCommercialAsk and PRICING_ONLY_RE.search(span.excerpt)
                                                       and not BUDGET_RE.search(span.excerpt)):
        status, note = "inferred", "Pricing/commercial question is not a budget commitment."
    resolved = (ext.timelineResolved or resolve_timeline(span.excerpt)) if key == "timeline" else None
    if key == "timeline" and status == "confirmed" and not resolved:
        status, note = "inferred", "Timeline stated but period/year ambiguous; cannot resolve to a date window."
    value = span.value
    if resolved:
        value = f"{span.value} ({resolved})" if span.value else resolved
    ev = Evidence(field=key, value=value, excerpt=span.excerpt, origin="buyer_statement", status=status, messageId=reply.id)
    return BantField(status=status, evidence=[ev] + [e for e in base.evidence if e.origin != "buyer_statement"], note=note)


# Route by precedence: suppression -> existing owner -> outside ICP -> intent -> objections/intent strength -> BANT;
# returns (route, reasons, gap_eligible)
def _route(lead: Lead, reply: Message, decision: DecisionResult, bant: dict[str, BantField], strength: str,
           objections: list[dict[str, Any]]) -> tuple[str, list[str], bool]:
    if UNSUBSCRIBE_RE.search(reply.body):
        return "suppressed", ["Explicit removal request in reply (keyword rule); suppressed immediately."], False
    gate = _gate_route(lead)
    if gate:
        return gate[0], [gate[1]], False
    intent, conf = decision.intent, decision.intentConfidence
    tag = f"intent {intent} ({conf:.2f} confidence)"
    if decision.refusal:
        return "human_review", ["Reply could not be classified with confidence; escalated to a human."], False
    if intent == "unsubscribe":
        return "suppressed", [f"Reply asks for no further contact: {tag}."], False
    if conf < config.INTENT_CONFIDENCE_FLOOR:
        return "human_review", [f"Low confidence, escalate: {tag} below floor {config.INTENT_CONFIDENCE_FLOOR}."], intent == "interested"
    if intent == "not_interested":
        return "suppressed", [f"Buyer declined: {tag}."], False
    if intent == "not_now":
        return "nurture", [f"Buyer deferred: {tag}; move to nurture."], False
    if intent in ("wrong_person", "referral", "out_of_office", "other"):
        return "human_review", [f"Reply needs a human decision: {tag}."], False
    unresolved = _unresolved(bant)
    icp_ok = bool(lead.icp and lead.icp.status == "matches_icp")
    if objections:
        first = objections[0]
        return "human_review", [f"Unresolved {first['type']} objection (\"{first['excerpt']}\"); an SDR addresses it before any AE "
                                f"handoff, {tag}."], bool(unresolved)
    if icp_ok and not unresolved and strength == "buying":
        return "rep_review", [f"ICP match, all four BANT fields confirmed by verified buyer excerpts, buying intent, {tag}."], False
    why = [] if icp_ok else [f"ICP status is {lead.icp.status if lead.icp else 'unknown'}, not matches_icp"]
    if strength != "buying":
        why.append(f"intent strength is {strength}, not buying (needs SDR nurture/qualification)")
    if unresolved:
        why.append("BANT unresolved: " + ", ".join(f"{k} {bant[k].status}" for k in unresolved))
    return "human_review", [f"Interested ({conf:.2f}) but not ready for AE: {'; '.join(why)}."], True


# Human-readable next action for the chosen route and context
def _next_action(route: str, decision: DecisionResult, ext: Extraction, next_question_key: Optional[str],
                 strength: str = "none", objections: Optional[list[dict[str, Any]]] = None) -> str:
    if route != "human_review":
        return NEXT_ACTION[route]
    if objections and not decision.refusal and decision.intent not in ("referral", "out_of_office", "wrong_person"):
        first = objections[0]
        action = f"Address {first['type']} objection: {first['handling']}"
        if ext.pricingOrCommercialAsk:
            action += " Pricing question goes to a specialist owner - not auto-answered."
        return action
    if ext.pricingOrCommercialAsk:
        return "Specialist owner: answer pricing/commercial question — not auto-answered"
    if decision.refusal or (decision.intentConfidence < config.INTENT_CONFIDENCE_FLOOR and decision.intent != "interested"):
        return "SDR: review reply manually — model unsure, escalated"
    if decision.intent == "referral":
        who = ", ".join(p.name + (f" ({p.role})" if p.role else "") for p in ext.mentionedPeople) or "the referred contact"
        return f"SDR: verify referral to {who} and confirm consent before any outreach"
    if decision.intent == "out_of_office":
        return "SDR: pause outreach and resume after the contact returns"
    if decision.intent == "wrong_person":
        return "SDR: ask once for the right owner or close the thread"
    if next_question_key:
        return f"SDR: send one gap-fill question about {next_question_key} (awaiting approval)"
    if decision.intent == "interested":
        return "SDR: review BANT gaps — gap-fill already used this cycle"
    if strength == "exploring":
        return "SDR: nurture — share relevant insight and qualify; not ready for AE"
    return "SDR: review reply manually"


# Merge prior competitor mentions with new ones (dedupe by name, newest excerpt wins)
def _merge_competitors(prior: Optional[Assessment], ext: Extraction) -> list[dict[str, Any]]:
    merged = {c.name.lower(): c.model_dump() for c in ext.competitors}
    for old in (prior.competitors if prior else []):
        merged.setdefault(str(old.get("name", "")).lower(), old)
    return [c for k, c in merged.items() if k]


# Merge prior buying signals with new ones (dedupe overlapping excerpts, newest first)
def _merge_signals(prior: Optional[Assessment], ext: Extraction) -> list[dict[str, Any]]:
    out = [s.model_dump() for s in ext.buyingSignals]
    for old in (prior.buyingSignals if prior else []):
        excerpt = str(old.get("excerpt") or "")
        if excerpt and not any(excerpt in s["excerpt"] or s["excerpt"] in excerpt for s in out):
            out.append(old)
    return out


# Assess a buyer reply: extraction + decision in parallel, deterministic merge, scoring and routing
async def assess_reply(lead: Lead, reply: Message, thread: list[Message], prior: Optional[Assessment]) -> Assessment:
    from app.governance.guardrails import injection_excerpt, record_injection

    injected = None if UNSUBSCRIBE_RE.search(reply.body) else injection_excerpt(reply.body)
    if injected:
        # Never let instructions inside a reply move the score: keep the prior view and hand the reply to a person
        record_injection(lead.id, reply.id, injected)
        keep = prior.model_copy() if prior else None
        return Assessment(
            leadId=lead.id, leadVersion=lead.version,
            policyVersion=lead.icp.policyVersion if lead.icp else config.POLICY_VERSION, icp=lead.icp,
            replyIntent="other", intentConfidence=0.0, bant=keep.bant if keep else _baseline_bant(lead),
            bantScore=keep.bantScore if keep else 0, qualificationScore=keep.qualificationScore if keep else 0,
            missingFields=keep.missingFields if keep else list(BANT_KEYS), route="human_review",
            routeReasons=["Reply contains instructions aimed at the system; nothing in it was acted on and the score is unchanged."],
            nextAction="SDR: read the reply yourself and respond personally",
            decision=DecisionResult(provider="rule", model="injection-guard", intent="other", intentConfidence=0.0,
                                    rationale="Hidden-instruction pattern detected; no model call made."),
            mode="live", injectionExcerpt=injected[:200],
        )
    if UNSUBSCRIBE_RE.search(reply.body):
        ext = fixture_extract(reply)
        decision = DecisionResult(provider="rule", model="unsubscribe-keywords", intent="unsubscribe", intentConfidence=1.0,
                                  bantSupport={k: "no" for k in BANT_KEYS}, rationale="Explicit removal keywords; no model call needed.")
        mode = "live"
    else:
        ext_res, dec_res = await asyncio.gather(extract_bant(reply, thread, lead),
                                                decide(reply.body, {k: None for k in BANT_KEYS}), return_exceptions=True)
        ext = ext_res if isinstance(ext_res, Extraction) else fixture_extract(reply)
        prelim = dec_res if isinstance(dec_res, DecisionResult) else refusal_result(config.DECISION_PROVIDER, str(dec_res))
        decision = prelim
        if any(ext.spans().values()) and not prelim.refusal:
            try:
                decision = await decide(reply.body, ext.spans())
            except Exception as exc:
                decision = refusal_result(config.DECISION_PROVIDER, str(exc))
            decision.durationMs += prelim.durationMs
        mode = "live" if ext.mode == "live" and not decision.provider.startswith("fixture") else "fixture"
    base = _baseline_bant(lead)
    prior_bant = prior.bant if prior else {}
    bant = {k: _merge_field(k, ext, decision, reply, prior_bant.get(k), base[k]) for k in BANT_KEYS}
    bant_score, qual = _scores(lead, bant)
    unresolved = _unresolved(bant)
    objections = [o.model_dump() for o in ext.objections]
    competitors = _merge_competitors(prior, ext)
    signals = _merge_signals(prior, ext)
    strength = intent_strength(decision, len(signals), sum(1 for k in BANT_KEYS if bant[k].status == "confirmed"))
    route, reasons, gap_eligible = _route(lead, reply, decision, bant, strength, objections)
    if route in ("suppressed", "closed"):
        strength = "none"
    next_question, nq_key = None, None
    if route == "human_review" and gap_eligible and lead.gapFillsSent < 1 and not ext.pricingOrCommercialAsk:
        next_question = next_question_for(unresolved)
        nq_key = next((k for k in GAP_ORDER if k in unresolved), None)
    if ext.pricingOrCommercialAsk:
        reasons.append("Pricing/commercial question needs specialist owner — not auto-answered.")
    if ext.competitors:
        reasons.append("Competitor/incumbent mentioned: " + ", ".join(f"{c.name} (\"{c.excerpt}\")" for c in ext.competitors)
                       + " — position on approved strengths; never disparage.")
    if ext.mentionedPeople:
        reasons.append("Mentioned people: " + ", ".join(
            p.name + (f" ({p.role})" if p.role else "") + (f" <{p.email}>" if p.email else "") for p in ext.mentionedPeople) + ".")
    if ext.dropped:
        reasons.append(f"Dropped {len(ext.dropped)} unverifiable excerpt(s): " + ", ".join(d.field for d in ext.dropped) + ".")
    if ext.mode == "fixture" and ext.error:
        reasons.append("Automatic evidence extraction was unavailable; a human should verify BANT.")
    return Assessment(
        leadId=lead.id, leadVersion=lead.version,
        policyVersion=lead.icp.policyVersion if lead.icp else config.POLICY_VERSION,
        icp=lead.icp, replyIntent=decision.intent, intentConfidence=decision.intentConfidence,
        bant=bant, bantScore=bant_score, qualificationScore=qual, missingFields=unresolved,
        route=route, routeReasons=reasons, nextQuestion=next_question,
        nextAction=_next_action(route, decision, ext, nq_key, strength, objections), decision=decision,
        intentStrength=strength, objections=objections, competitors=competitors, buyingSignals=signals, mode=mode,
        droppedQuotes=[{"field": d.field, "excerpt": d.excerpt[:200], "reason": d.reason} for d in ext.dropped],
    )
