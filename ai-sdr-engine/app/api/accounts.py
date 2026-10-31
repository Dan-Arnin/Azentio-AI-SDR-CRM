# app/api/accounts.py - Company (account) pages and person profiles, with Firecrawl-powered public research
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import config, db
from app.contracts import Assessment, Campaign, Enrichment, Enrollment, Lead, LeadInput, Message, Task
from app.engine import dossier as dz
from app.engine.enrichment import COUNTRY_ALIASES, company_domain_for, is_personal_domain, lookup_company, normalize_country, normalize_domain
from app.engine.icp import load_policy, score_icp
from app.engine.identity import _crm, crm_account

router = APIRouter(prefix="/v1", tags=["accounts"])

PEOPLE = {"ae.sara": "Sara Malik", "ae.khalid": "Khalid Al Mansoori", "ae.priya": "Priya Raman", "sdr.omar": "Nadia Karim"}
PRODUCTS = ("lending_platform", "islamic_banking", "aml_compliance")
STAGE_WORDS = {"NEW": "New", "ENRICHED": "Researching", "ENGAGING": "In conversation", "QUALIFYING": "Reviewing reply",
               "REP_REVIEW": "Qualified, awaiting account executive", "OPPORTUNITY": "Opportunity", "HUMAN_REVIEW": "Needs attention",
               "NURTURE": "Nurture", "SUPPRESSED": "Opted out", "CLOSED": "Not a fit", "QUARANTINED": "Identity check"}
OPPORTUNITY_STAGES = ("OPPORTUNITY", "REP_REVIEW")
PROFILE_KEYS = ("industry", "country", "hq", "employeeCount", "revenueBand", "institutionType", "website")
TLD_COUNTRY = {".ae": "AE", ".sa": "SA", ".qa": "QA", ".om": "OM", ".kw": "KW", ".bh": "BH", ".in": "IN", ".my": "MY"}
COUNTRY_HINTS = (("KW", ("kuwait", "KWD")), ("OM", ("oman", "muscat", "OMR")), ("AE", ("UAE", "emirates", "dubai", "abu dhabi", "AED")),
                 ("SA", ("saudi", "riyadh", "SAR", "riyals")), ("QA", ("qatar", "doha", "QAR")), ("BH", ("bahrain", "manama", "BHD")),
                 ("IN", ("india", "INR")), ("MY", ("malaysia", "MYR")))
FACT_PROFILE = {"employees": "employeeCount", "headquarters": "hq", "head office": "hq", "industry": "industry",
                "headquarters country": "country", "country": "country", "institution type": "institutionType"}


class CompanyResearchInput(BaseModel):
    companyName: str
    domain: str


class AccountResearchInput(BaseModel):
    companyName: Optional[str] = None


# Person reference {id,name} for a team member id, or None
def _person(pid: Optional[str]) -> Optional[dict[str, str]]:
    return {"id": pid, "name": PEOPLE.get(pid, pid)} if pid else None


# Company domain a lead belongs to (account key, work email, or form domain)
def _lead_domain(lead: Lead) -> Optional[str]:
    d = lead.accountKey or company_domain_for(lead.input)
    d = normalize_domain(d)
    return None if not d or is_personal_domain(d) else d


# All leads grouped by company domain
def _leads_by_domain() -> dict[str, list[Lead]]:
    out: dict[str, list[Lead]] = {}
    for lead in db.list_("leads", Lead, limit=100000):
        d = _lead_domain(lead)
        if d:
            out.setdefault(d, []).append(lead)
    return out


# Latest assessment per lead id
def _latest_assessments() -> dict[str, Assessment]:
    out: dict[str, Assessment] = {}
    for a in db.list_("assessments", Assessment, limit=100000):
        if a.leadId not in out or a.createdAt >= out[a.leadId].createdAt:
            out[a.leadId] = a
    return out


# Channel of a message: LinkedIn note, text message or email
def _channel(m: Message) -> str:
    if (m.subject or "").startswith("LinkedIn connection request"):
        return "linkedin"
    if m.transport == "twilio" or m.origin == "twilio":
        return "text"
    return "email"


# Cached dossier for a domain as a dict (or None)
def _dossier(domain: str) -> Optional[dict[str, Any]]:
    rec = dz.cached_company(domain)
    return rec.data if rec else None


# Company name for a domain from directory, CRM, cached research or leads
def _company_name(domain: str, leads: list[Lead]) -> str:
    directory = lookup_company(domain) or {}
    crm = crm_account(domain) or {}
    rec = dz.cached_company(domain)
    names = Counter(l.input.companyName for l in leads if l.input.companyName)
    return (directory.get("name") or crm.get("name") or (rec.name if rec and rec.name and rec.name != domain else None)
            or (names.most_common(1)[0][0] if names else domain))


# Company profile from the demo directory, lead enrichment (any provider) and verified research facts
def _profile(domain: str, leads: list[Lead], dossier: Optional[dict[str, Any]], demo: bool, name: str = "") -> dict[str, Any]:
    prof: dict[str, Any] = {k: None for k in PROFILE_KEYS}
    sources = [lookup_company(domain) or {}]
    sources += [(l.enrichment.company if l.enrichment else None) or {} for l in sorted(leads, key=lambda l: l.updatedAt, reverse=True)]
    for src in sources:
        for k in PROFILE_KEYS:
            v = src.get("headquartersCountry") if k == "hq" and not src.get("hq") else src.get(k)
            if prof[k] in (None, "") and v not in (None, "", [], {}):
                prof[k] = v
    for l in leads:
        if prof["country"] is None and l.input.country:
            prof["country"] = normalize_country(l.input.country)
        if prof["institutionType"] is None and l.input.institutionType:
            prof["institutionType"] = l.input.institutionType
        if prof["employeeCount"] is None and l.input.employeeCount:
            prof["employeeCount"] = l.input.employeeCount
    for f in (dossier or {}).get("facts") or []:
        key = FACT_PROFILE.get(str(f.get("label", "")).strip().lower())
        if key and prof[key] in (None, ""):
            prof[key] = _country_code(f.get("value")) if key == "country" else f.get("value")
    prof["institutionType"] = _institution(prof["institutionType"])
    if not demo and dossier:
        facts = " ".join(f"{f.get('label', '')} {f.get('value', '')}" for f in dossier.get("facts") or [])
        text = f" {name} {domain} {dossier.get('summary') or ''} {facts} "
        prof["country"] = prof["country"] or _country_hint(domain, text)
        prof["institutionType"] = prof["institutionType"] or (
            "islamic_bank" if re.search(r"islamic|shari'?a|finance house", f"{name} {dossier.get('summary') or ''}", re.I)
            else "bank" if re.search(r"\bbank", text, re.I) else None)
    if not demo:
        prof["website"] = prof["website"] or f"https://{domain}"
    return prof


# Policy institution code from researched wording ("Islamic bank" -> islamic_bank, "Commercial bank" -> bank)
def _institution(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if "islamic" in text or "sharia" in text:
        return "islamic_bank"
    if text in ("bank", "commercial bank", "conventional bank", "retail bank"):
        return "bank"
    return text.replace(" ", "_")


# Country from a company's domain or verified research text (country names, capitals, currency codes)
def _country_hint(domain: str, text: str) -> Optional[str]:
    tld = next((c for t, c in TLD_COUNTRY.items() if domain.endswith(t)), None)
    if tld:
        return tld
    return next((c for c, words in COUNTRY_HINTS if any(re.search(rf"\b{w}\b", text, re.I) for w in words)), None)


# ISO country code for a country name as written in a source ("the State of Kuwait" -> KW)
def _country_code(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    code = normalize_country(text)
    if code and len(code) == 2:
        return code
    return next((c for alias, c in sorted(COUNTRY_ALIASES.items(), key=lambda kv: -len(kv[0])) if re.search(rf"\b{alias}\b", text)), text or None)


# Integer employee count from a profile value ("1800", "over 15,000")
def _employees(value: Any) -> Optional[int]:
    m = re.search(r"\d[\d,]*", str(value or ""))
    return int(m.group().replace(",", "")) if m else None


COUNTRY_NAMES = {"AE": "the UAE", "SA": "Saudi Arabia", "BH": "Bahrain", "QA": "Qatar", "KW": "Kuwait", "OM": "Oman", "IN": "India",
                 "MY": "Malaysia", "ID": "Indonesia", "DE": "Germany", "US": "the United States", "GB": "the United Kingdom"}
INSTITUTION_NAMES = {"bank": "Commercial bank", "islamic_bank": "Islamic bank", "nbfc": "Finance company", "fintech": "Fintech", "insurer": "Insurer"}
PRODUCT_NAMES = {"lending_platform": "Azentio Lending", "islamic_banking": "iMAL Islamic Banking", "aml_compliance": "AMLOCK Compliance"}


# Plain-English fit reasons built from the scoring checks (no internal codes or rule text)
def plain_reasons(icp: Any, country: Optional[str], institution: Optional[str], employees: Any, title: Optional[str] = None) -> list[str]:
    product = PRODUCT_NAMES.get(icp.productId, "this product")
    place = COUNTRY_NAMES.get((country or "").upper(), country or "this market")
    inst = INSTITUTION_NAMES.get((institution or "").lower(), (institution or "").replace("_", " ").capitalize())
    out = []
    for c in icp.checks:
        full = c.passed and c.points >= c.weight
        if c.rule == "geography":
            out.append(f"{place[0].upper() + place[1:]} is a priority market" if c.passed else f"{place[0].upper() + place[1:]} is outside our focus markets" if c.passed is False else "Location not confirmed yet")
        elif c.rule == "institution":
            out.append(f"{inst} is a core segment for {product}" if full else f"{inst} is a partial fit for {product}" if c.passed else
                       f"{inst} isn't a usual segment for {product}" if c.passed is False else "Institution type not confirmed yet")
        elif c.rule == "size":
            out.append(f"{int(employees):,} employees" + ("" if full else ", smaller than our usual customer") if employees else "Organisation size not known yet")
        elif c.rule == "role" and title:
            out.append(f"{title} is a decision maker" if full else f"{title} is likely an influencer" if c.points else f"{title}: seniority unclear")
        elif c.rule == "product_relevance" and title is not None:
            out.append("Their enquiry describes a relevant need" if full else f"Interested in {product}, need not described yet" if c.points else "No clear product interest yet")
    return out


# Our own company (the seller) is never scored or listed as a prospect account
def is_seller_domain(domain: str) -> bool:
    own = {d.strip().lower() for d in config.env("SELLER_DOMAINS", "azentio.com").split(",") if d.strip()}
    return any(domain == d or domain.endswith("." + d) for d in own)


# Fit per Azentio product from a synthetic account-level lead (role and enquiry are person-level, so their reasons are omitted)
def _fit(domain: str, name: str, prof: dict[str, Any]) -> list[dict[str, Any]]:
    products = load_policy()["products"]
    enr = Enrichment(provider="account_profile", mock=True, found=True, company={k: v for k, v in prof.items() if v is not None})
    out = []
    for pid in PRODUCTS:
        inp = LeadInput(sourceRecordId=f"account:{domain}", source="target_account", fullName=name, email=f"account@{domain}",
                        companyName=name, country=str(prof.get("country") or ""), productId=pid,
                        institutionType=prof.get("institutionType"), employeeCount=_employees(prof.get("employeeCount")))
        r = score_icp(inp, enr)
        reasons = plain_reasons(r, prof.get("country"), prof.get("institutionType"), _employees(prof.get("employeeCount")))
        out.append({"productId": pid, "product": PRODUCT_NAMES.get(pid, products[pid]["name"]), "score": r.score, "fit": r.fit, "status": r.status, "reasons": reasons})
    return out


# Account owner from the leads' account owner, else the CRM record
def _account_owner(domain: str, leads: list[Lead]) -> Optional[dict[str, str]]:
    owners = Counter(l.accountOwnerId for l in leads if l.accountOwnerId)
    pid = owners.most_common(1)[0][0] if owners else (crm_account(domain) or {}).get("ownerId")
    return _person(pid)


# Build the full account page for a domain
def _account_page(domain: str, leads: list[Lead]) -> dict[str, Any]:
    demo = dz.is_demo_domain(domain)
    dossier = _dossier(domain)
    if demo and not dossier:
        dossier = dz.Dossier(status="skipped_demo", message=dz.DEMO_MESSAGE).model_dump()
    name = _company_name(domain, leads)
    prof = _profile(domain, leads, dossier, demo, name)
    latest = _latest_assessments()
    ids = {l.id for l in leads}
    messages = [m for m in db.list_("messages", Message, limit=100000) if m.leadId in ids]
    last_msg: dict[str, str] = {}
    for m in messages:
        last_msg[m.leadId] = max(last_msg.get(m.leadId, ""), m.at or "")
    contacts = [{"leadId": l.id, "name": l.input.fullName, "title": l.input.role or ((l.enrichment.person or {}).get("title") if l.enrichment else None),
                 "stage": l.stage, "stageLabel": STAGE_WORDS.get(l.stage, l.stage), "assignee": _person(l.ownerId),
                 "fit": l.icp.fit if l.icp else None, "qualificationScore": latest[l.id].qualificationScore if l.id in latest else None,
                 "lastActivityAt": max(last_msg.get(l.id, ""), l.updatedAt or "") or None}
                for l in sorted(leads, key=lambda l: l.updatedAt, reverse=True)]
    enrollments = [e for e in db.list_("enrollments", Enrollment, limit=100000) if e.leadId in ids]
    camp_counts = Counter(e.campaignId for e in enrollments)
    campaigns = []
    for cid, n in camp_counts.items():
        c = db.get("campaigns", cid, Campaign)
        campaigns.append({"id": cid, "name": c.name if c else cid, "contacts": n})
    opportunities = [{"leadId": l.id, "name": l.input.fullName, "ownerId": l.ownerId, "ownerName": PEOPLE.get(l.ownerId or "", l.ownerId),
                      "stage": l.stage, "stageLabel": STAGE_WORDS.get(l.stage, l.stage)} for l in leads if l.stage in OPPORTUNITY_STAGES]
    for opp in _crm()["opportunities"]:
        if opp["accountDomain"] == domain and opp.get("status", "open") == "open":
            product = load_policy()["products"].get(opp["productId"], {}).get("name", opp["productId"])
            opportunities.append({"leadId": None, "name": f"{opp['accountName']} - {product}", "ownerId": opp["ownerId"],
                                  "ownerName": PEOPLE.get(opp["ownerId"], opp["ownerId"]), "stage": opp["stage"], "stageLabel": opp["stage"]})
    own = is_seller_domain(domain)
    return {"domain": domain, "name": name, "isDemoCompany": demo, "isOwnCompany": own, "profile": prof, "fit": [] if own else _fit(domain, name, prof),
            "accountOwner": _account_owner(domain, leads), "dossier": dossier, "contacts": contacts, "campaigns": campaigns,
            "opportunities": opportunities, "activity": _activity(leads, messages)}


# Latest 15 account events: emails, replies, LinkedIn notes, texts, stage moves and handoffs
def _activity(leads: list[Lead], messages: list[Message]) -> list[dict[str, Any]]:
    names = {l.id: l.input.fullName for l in leads}
    events = []
    for m in messages:
        ch = _channel(m)
        kind = ch if ch != "email" else ("email_sent" if m.direction == "outbound" else "reply")
        verb = {"email_sent": "Email sent", "reply": "Reply received", "linkedin": "LinkedIn note", "text": "Text message"}[kind]
        if ch != "email" and m.direction == "inbound":
            verb += " received"
        events.append({"at": m.at, "leadId": m.leadId, "name": names.get(m.leadId), "kind": kind,
                       "text": f"{verb}: {m.subject}" if m.subject and ch == "email" else f"{verb}: {m.body[:120]}"})
    for l in leads:
        for h in l.history:
            if h.get("type") == "stage_changed":
                target = str(h.get("detail", "")).split("->")[-1].split(":")[0].strip()
                events.append({"at": h.get("at"), "leadId": l.id, "name": l.input.fullName, "kind": "stage",
                               "text": f"Moved to {STAGE_WORDS.get(target, target).lower()}"})
    ids = set(names)
    for t in db.list_("tasks", Task, limit=100000):
        if t.leadId in ids and t.type == "ae_handoff":
            events.append({"at": t.createdAt, "leadId": t.leadId, "name": names.get(t.leadId), "kind": "handoff",
                           "text": f"Handed to {PEOPLE.get(t.ownerId, t.ownerId)}"})
    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    return events[:15]


# List every known account: from leads, CRM accounts and researched companies
@router.get("/accounts")
def list_accounts() -> dict[str, Any]:
    by_domain = _leads_by_domain()
    domains = set(by_domain) | {a["domain"] for a in _crm()["accounts"]}
    records = {r.domain: r for r in dz.all_company_records() if r.domain}
    domains |= set(records)
    items = []
    for d in sorted(domains - {x for x in domains if is_seller_domain(x)}):
        leads = by_domain.get(d, [])
        rec = records.get(d)
        open_opps = sum(1 for l in leads if l.stage in OPPORTUNITY_STAGES)
        open_opps += sum(1 for o in _crm()["opportunities"] if o["accountDomain"] == d and o.get("status", "open") == "open")
        researched = rec.data.get("researchedAt") if rec and rec.data.get("status") != "skipped_demo" else None
        items.append({"domain": d, "name": _company_name(d, leads), "isDemoCompany": dz.is_demo_domain(d), "contacts": len(leads),
                      "openOpportunities": open_opps, "researchedAt": researched})
    items.sort(key=lambda i: (-i["contacts"], i["name"].lower()))
    return {"items": items}


# Account page for one company domain
@router.get("/accounts/{domain}")
def get_account(domain: str) -> dict[str, Any]:
    d = normalize_domain(domain)
    if not d:
        raise HTTPException(404, detail={"code": "not_found"})
    leads = _leads_by_domain().get(d, [])
    if not leads and not dz.cached_company(d) and not lookup_company(d) and not crm_account(d):
        raise HTTPException(404, detail={"code": "not_found", "message": "No contacts or research for this company yet."})
    return _account_page(d, leads)


# Re-run public research for an account (synchronous)
@router.post("/accounts/{domain}/research")
async def research_account(domain: str, body: Optional[AccountResearchInput] = None) -> dict[str, Any]:
    d = normalize_domain(domain)
    if not d or is_personal_domain(d) or "." not in d:
        raise HTTPException(422, detail={"code": "invalid_domain", "message": "Enter a company website domain such as bank.com."})
    name = (body.companyName if body else None) or _company_name(d, _leads_by_domain().get(d, []))
    return (await dz.research_company(d, name)).model_dump()


# Ad-hoc research of any real company by name and domain
@router.post("/research/company")
async def research_company(body: CompanyResearchInput) -> dict[str, Any]:
    d = normalize_domain(body.domain)
    if not d or is_personal_domain(d) or "." not in d:
        raise HTTPException(422, detail={"code": "invalid_domain", "message": "Enter a company website domain such as bank.com."})
    return {"domain": d, "dossier": (await dz.research_company(d, body.companyName.strip() or d)).model_dump()}


# One sentence on why we are talking to this person (their enquiry, event, ad asset or campaign)
def _reason_to_reach_out(lead: Lead) -> str:
    inp = lead.input
    company = inp.companyName or "their company"
    if inp.source == "website" and inp.inquiry.strip():
        text = " ".join(inp.inquiry.split())
        return f"Sent a website enquiry: “{text[:220]}{'…' if len(text) > 220 else ''}”"
    if inp.eventName:
        return f"Registered for or attended {inp.eventName}."
    if inp.assetName:
        return f"Requested “{inp.assetName}” from an Azentio ad."
    enr = next((e for e in db.list_("enrollments", Enrollment, limit=100000) if e.leadId == lead.id), None)
    camp = db.get("campaigns", enr.campaignId, Campaign) if enr else None
    if camp:
        return f"Selected for the {camp.name} campaign ({camp.theme}) as a target contact at {company}."
    if inp.inquiry.strip():
        return f"Told us: “{' '.join(inp.inquiry.split())[:220]}”"
    if inp.source == "marketing_csv":
        return f"Came from a marketing list as a contact at {company}."
    if inp.source == "target_account":
        return f"Target contact at {company}, a target account."
    return f"Contact at {company}."


# Buyer-signal block per BANT key: status plus first supporting quote
def _bant(a: Optional[Assessment]) -> dict[str, Any]:
    out = {}
    for k in ("budget", "authority", "need", "timeline"):
        f = a.bant.get(k) if a else None
        out[k] = {"status": f.status if f else "missing", "quote": next((e.excerpt for e in f.evidence if e.excerpt), None) if f else None}
    return out


# Engagement counts and timeline (form enquiry + every message)
def _engagement(lead: Lead) -> dict[str, Any]:
    msgs = db.list_("messages", Message, lead_id=lead.id, limit=5000)
    timeline = []
    if lead.input.inquiry.strip():
        timeline.append({"at": lead.input.submittedAt or lead.createdAt, "channel": "form", "direction": "inbound",
                         "subject": "Website enquiry" if lead.input.source == "website" else "Form submission", "excerpt": lead.input.inquiry[:300]})
    for m in msgs:
        timeline.append({"at": m.at, "channel": _channel(m), "direction": m.direction, "subject": m.subject, "excerpt": m.body[:300]})
    timeline.sort(key=lambda e: e.get("at") or "")
    out_email = [m for m in msgs if m.direction == "outbound" and _channel(m) == "email"]
    return {"emailsSent": len(out_email), "replies": sum(1 for m in msgs if m.direction == "inbound"),
            "linkedin": sum(1 for m in msgs if _channel(m) == "linkedin"), "texts": sum(1 for m in msgs if _channel(m) == "text"),
            "timeline": timeline}


# Recommended next step: latest assessment's next action, else an open task
def _next_best_action(lead: Lead, a: Optional[Assessment]) -> str:
    if a and a.nextAction:
        return a.nextAction
    task = next((t for t in db.list_("tasks", Task, lead_id=lead.id) if t.status in ("open", "acknowledged")), None)
    if task:
        return (task.card.recommendedNextAction if task.card and task.card.recommendedNextAction else task.title)
    return "No action needed right now."


# Load a lead or 404
def _lead_or_404(lead_id: str) -> Lead:
    lead = db.get("leads", lead_id, Lead)
    if not lead:
        raise HTTPException(404, detail={"code": "not_found"})
    return lead


# True when the lead belongs to a fictional demo company
def _is_demo_lead(lead: Lead) -> bool:
    d = _lead_domain(lead)
    return dz.is_demo_domain(d) or dz.is_demo_company_name(lead.input.companyName) or (
        bool(lead.enrichment) and lead.enrichment.provider == "mock_apollo" and lead.enrichment.found)


# Person profile page
@router.get("/people/{lead_id}")
def get_person(lead_id: str) -> dict[str, Any]:
    lead = _lead_or_404(lead_id)
    inp = lead.input
    person = (lead.enrichment.person if lead.enrichment else None) or {}
    a = _latest_assessments().get(lead.id)
    domain = _lead_domain(lead)
    owner = lead.accountOwnerId or ((crm_account(domain, inp.companyName) or {}).get("ownerId") if domain else None)
    rec = dz.cached_person(lead.id)
    research = rec.data if rec else (dz.PersonResearch(status="skipped_demo", message=dz.DEMO_MESSAGE).model_dump() if _is_demo_lead(lead) else None)
    return {
        "leadId": lead.id, "name": inp.fullName, "title": inp.role or person.get("title"), "seniority": person.get("seniority"),
        "department": person.get("department"), "email": inp.email, "phone": inp.phone or person.get("phone"),
        "country": normalize_country(inp.country), "source": inp.source, "productId": inp.productId,
        "stage": lead.stage, "stageLabel": STAGE_WORDS.get(lead.stage, lead.stage),
        "assignee": _person(lead.ownerId), "accountOwner": _person(owner),
        "company": {"domain": domain, "name": inp.companyName},
        "fit": {"score": lead.icp.score, "fit": lead.icp.fit, "reasons": plain_reasons(
            lead.icp, lead.input.country, lead.input.institutionType or ((lead.enrichment.company or {}).get("institutionType") if lead.enrichment else None),
            lead.input.employeeCount or ((lead.enrichment.company or {}).get("employeeCount") if lead.enrichment else None),
            lead.input.role or "")} if lead.icp else None,
        "reasonToReachOut": _reason_to_reach_out(lead),
        "intentStrength": getattr(a, "intentStrength", None) if a else None,
        "qualificationScore": a.qualificationScore if a else None,
        "bant": _bant(a),
        "insights": {"buyingSignals": list(getattr(a, "buyingSignals", None) or []), "objections": list(getattr(a, "objections", None) or []),
                     "competitors": list(getattr(a, "competitors", None) or [])},
        "engagement": _engagement(lead),
        "nextBestAction": _next_best_action(lead, a),
        "research": research,
    }


# Research public mentions of a person (synchronous; skipped for demo companies)
@router.post("/people/{lead_id}/research")
async def research_person(lead_id: str) -> dict[str, Any]:
    lead = _lead_or_404(lead_id)
    person = (lead.enrichment.person if lead.enrichment else None) or {}
    r = await dz.research_person(lead.id, lead.input.fullName, lead.input.companyName, lead.input.role or person.get("title"), _is_demo_lead(lead))
    return r.model_dump()
