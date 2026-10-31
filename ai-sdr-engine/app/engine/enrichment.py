# app/engine/enrichment.py - Deterministic mock-Apollo enrichment and target-account cohort search
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Optional

from app.config import FIXTURES
from app.contracts import CohortDefinition, Enrichment, Evidence, LeadInput

PERSONAL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.co.in", "ymail.com", "icloud.com", "me.com", "aol.com",
    "proton.me", "protonmail.com", "gmx.com", "gmx.de", "yandex.com", "mail.com",
    "rediffmail.com", "zoho.com",
})

GCC_COUNTRIES = ("AE", "SA", "BH", "QA", "KW", "OM")
EU_EEA_UK = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
    "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO", "GB",
})

COUNTRY_ALIASES = {
    "UAE": "AE", "UNITED ARAB EMIRATES": "AE", "KSA": "SA", "SAUDI ARABIA": "SA", "BAHRAIN": "BH",
    "QATAR": "QA", "KUWAIT": "KW", "OMAN": "OM", "INDIA": "IN", "MALAYSIA": "MY", "GERMANY": "DE",
    "FRANCE": "FR", "UK": "GB", "UNITED KINGDOM": "GB", "USA": "US", "UNITED STATES": "US",
}

ROLE_GROUPS = {
    "ceo": ("ceo", "chief executive", "chief executive officer"),
    "cto": ("cto", "chief technology", "chief technology officer"),
    "cfo": ("cfo", "chief financial", "chief financial officer"),
    "coo": ("coo", "chief operating", "chief operating officer"),
    "compliance": ("head of compliance", "chief compliance officer", "compliance director", "director of compliance",
                   "mlro", "money laundering reporting officer"),
    "credit risk": ("head of credit risk", "chief risk officer", "head of credit", "head of risk",
                    "credit risk director", "director of credit risk"),
    "transformation": ("head of transformation", "chief transformation officer", "head of digital transformation",
                       "transformation director", "director of transformation"),
}
ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    **{term: group for group in ROLE_GROUPS.values() for term in group},
    **ROLE_GROUPS,
    "risk": ROLE_GROUPS["credit risk"],
    "digital transformation": ROLE_GROUPS["transformation"],
}
INDUSTRY_HINTS = {
    "bank": "banking", "islamic_bank": "islamic banking", "nbfc": "nbfc non-banking finance",
    "fintech": "fintech", "insurer": "insurance",
}

COMPANY_FACTS = ("name", "industry", "country", "institutionType", "employeeCount", "revenueBand", "hq")
PERSON_FACTS = ("title", "seniority", "department")


# Load the mock Apollo fixture once
@lru_cache(maxsize=1)
def _apollo() -> dict[str, Any]:
    return json.loads((FIXTURES / "apollo_mock.json").read_text())


# Map a free-text country to ISO2 (accepts ISO2 or common names)
def normalize_country(country: Optional[str]) -> Optional[str]:
    if not country or not country.strip():
        return None
    c = country.strip().upper()
    return COUNTRY_ALIASES.get(c, c)


# Normalize an email address (trim + lowercase)
def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# Normalize a domain (lowercase, strip scheme/www/path)
def normalize_domain(domain: Optional[str]) -> Optional[str]:
    if not domain:
        return None
    d = domain.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d or None


# Domain part of an email, or None
def email_domain(email: str) -> Optional[str]:
    e = normalize_email(email)
    return e.rsplit("@", 1)[1] if "@" in e else None


# True when the domain is a consumer mailbox provider (never maps to a company)
def is_personal_domain(domain: Optional[str]) -> bool:
    return bool(domain) and domain in PERSONAL_DOMAINS


# Resolve the company domain for a lead: work-email domain first, then form companyDomain
def company_domain_for(inp: LeadInput) -> Optional[str]:
    dom = email_domain(inp.email)
    if dom and not is_personal_domain(dom):
        return dom
    alt = normalize_domain(inp.companyDomain)
    return alt if alt and not is_personal_domain(alt) else None


# Look up a mock-Apollo company record by domain
def lookup_company(domain: Optional[str]) -> Optional[dict[str, Any]]:
    return _apollo()["companies"].get(domain) if domain else None


# Look up a mock-Apollo person record by email
def lookup_person(email: str) -> Optional[dict[str, Any]]:
    return _apollo()["people"].get(normalize_email(email))


# Normalize a free-text value for equality comparison
def _norm(v: Any) -> str:
    return str(v).strip().lower().replace("_", " ").replace("-", " ") if v is not None else ""


# Decide whether a form value and an Apollo value genuinely disagree
def _differs(field: str, form_v: Any, apollo_v: Any) -> bool:
    if field == "employeeCount":
        try:
            a, b = float(form_v), float(apollo_v)
        except (TypeError, ValueError):
            return _norm(form_v) != _norm(apollo_v)
        return abs(a - b) > 0.25 * max(a, b)
    if field == "companyName":
        a, b = _norm(form_v), _norm(apollo_v)
        return not (a in b or b in a)
    return _norm(form_v) != _norm(apollo_v)


# Present-and-non-empty test
def _has(v: Any) -> bool:
    return v is not None and str(v).strip() != ""


# Build enrichment for a lead from mock Apollo without overwriting form data
def enrich(inp: LeadInput) -> Enrichment:
    email_dom = email_domain(inp.email)
    person = lookup_person(inp.email)
    company = lookup_company(company_domain_for(inp))
    if company is None and person and not is_personal_domain(email_dom):
        company = lookup_company(person.get("companyDomain"))

    person_d = dict(person or {})
    company_d = dict(company or {})
    evidence: list[Evidence] = []
    conflicts: list[dict[str, Any]] = []

    pairs = [
        ("title", inp.role, person_d.get("title")),
        ("country", normalize_country(inp.country), company_d.get("country")),
        ("institutionType", inp.institutionType, company_d.get("institutionType")),
        ("employeeCount", inp.employeeCount, company_d.get("employeeCount")),
        ("companyName", inp.companyName, company_d.get("name")),
    ]
    conflicted: set[str] = set()
    for field, form_v, apollo_v in pairs:
        if _has(form_v) and _has(apollo_v) and _differs(field, form_v, apollo_v):
            conflicts.append({"field": field, "form": form_v, "apollo": apollo_v})
            conflicted.add(field)

    form_facts = {
        "title": inp.role, "country": normalize_country(inp.country),
        "institutionType": inp.institutionType, "employeeCount": inp.employeeCount,
        "companyName": inp.companyName, "companyDomain": normalize_domain(inp.companyDomain),
    }
    for field, val in form_facts.items():
        if _has(val):
            evidence.append(Evidence(
                field=field, value=str(val), origin="form",
                status="conflict" if field in conflicted else "inferred",
                excerpt=f"Form self-reported {field}: {val}",
            ))
    for field in COMPANY_FACTS:
        val = company_d.get(field)
        if _has(val):
            ev_field = "companyName" if field == "name" else field
            evidence.append(Evidence(
                field=ev_field, value=str(val), origin="mock_enrichment",
                status="conflict" if ev_field in conflicted else "inferred",
                excerpt=f"Apollo company record ({company_d.get('domain')}): {field}={val}",
            ))
    for field in PERSON_FACTS:
        val = person_d.get(field)
        if _has(val):
            evidence.append(Evidence(
                field=field, value=str(val), origin="mock_enrichment",
                status="conflict" if field in conflicted else "inferred",
                excerpt=f"Apollo person record ({person_d.get('email')}): {field}={val}",
            ))

    sources = {
        "title": (inp.role, person_d.get("title")),
        "seniority": (None, person_d.get("seniority")),
        "department": (None, person_d.get("department")),
        "country": (form_facts["country"], company_d.get("country")),
        "institutionType": (inp.institutionType, company_d.get("institutionType")),
        "employeeCount": (inp.employeeCount, company_d.get("employeeCount")),
        "companyDomain": (company_domain_for(inp), company_d.get("domain")),
    }
    missing = [f for f, (fv, av) in sources.items() if not _has(fv) and not _has(av)]
    uncertain = [f for f, (fv, av) in sources.items() if _has(fv) and not _has(av)]
    if is_personal_domain(email_dom):
        uncertain.append("email_domain_personal")

    return Enrichment(
        found=bool(person or company),
        company=company_d,
        person=person_d,
        missing=missing,
        uncertain=uncertain,
        conflicts=conflicts,
        evidence=evidence,
    )


async def enrich_for_lead(inp: LeadInput) -> Enrichment:
    """Demo companies/people -> demo profile (never sent to live providers); real domains -> Apollo, then OpenAI web search."""
    from app import config
    profile = enrich(inp)
    if profile.found:
        return profile
    if config.ENRICHMENT_PROVIDER == "apollo" and config.APOLLO_API_KEY:
        from app.engine.apollo import enrich_apollo
        apollo = await enrich_apollo(inp, api_key=config.APOLLO_API_KEY,
            cache_path=config.DATA_DIR / "apollo-cache.sqlite3", people=config.APOLLO_PEOPLE_ENRICHMENT)
        if apollo.found or any(e in apollo.errors for e in ("synthetic_domain", "company_domain_unavailable")):
            return apollo if apollo.found else profile
        from app.engine.public_research import enrich_public
        public = await enrich_public(inp)
        public.errors = list(dict.fromkeys(apollo.errors + public.errors))
        return public
    from app.engine.public_research import enrich_public
    return await enrich_public(inp)


# Expand a cohort role keyword into the keyword itself plus its equivalent titles
def _role_terms(role: str) -> tuple[str, ...]:
    r = role.strip().lower()
    return tuple(dict.fromkeys((r, *ROLE_ALIASES.get(r, ()))))


# Title contains a role term (short acronyms like CEO/MLRO must be whole words)
def _title_has(title: str, term: str) -> bool:
    if len(term) <= 4:
        return re.search(rf"\b{re.escape(term)}\b", title) is not None
    return term in title


# Searchable industry text for a company: its industry plus a label derived from institution type
def _industry_text(industry: Any, institution: Any) -> str:
    parts = [str(industry or "").strip(), INDUSTRY_HINTS.get(str(institution or "").strip().lower(), "")]
    return " ".join(p for p in parts if p).lower()


# Case-insensitive substring match on industry text; "banking" does not match "non-banking"
def _industry_has(industry: str, term: str) -> bool:
    return bool(industry) and re.search(rf"(?<!non-)(?<!non ){re.escape(term)}", industry) is not None


# Expand cohort country tokens (supports "GCC")
def _country_set(countries: list[str]) -> set[str]:
    out: set[str] = set()
    for c in countries:
        cu = c.strip().upper()
        out.update(GCC_COUNTRIES if cu == "GCC" else (cu,))
    return out


# Find target contacts: mock-directory people first, then existing workspace leads (de-duplicated by email)
def search_cohort(cohort: CohortDefinition) -> list[LeadInput]:
    from app import db
    from app.contracts import Lead

    terms = [t for r in cohort.roles if r.strip() for t in _role_terms(r)]
    countries = _country_set(cohort.countries)
    inst_types = {i.strip().lower() for i in cohort.institutionTypes if i.strip()}
    industries = [i.strip().lower() for i in cohort.industries if i.strip()]
    limit = max(0, min(cohort.limit, 100))
    out: list[LeadInput] = []
    seen: set[str] = set()

    # Apply the role / market / institution / industry filters to one candidate
    def matches(title: str, country: Optional[str], institution: str, industry: str) -> bool:
        if terms and not any(_title_has(title.lower(), t) for t in terms):
            return False
        if countries and normalize_country(country) not in countries:
            return False
        if industries and not any(_industry_has(industry, i) for i in industries):
            return False
        return not inst_types or institution.lower() in inst_types

    for email, person in _apollo()["people"].items():
        if len(out) >= limit:
            return out
        company = lookup_company(person.get("companyDomain")) or {}
        institution = str(company.get("institutionType") or "")
        industry = _industry_text(company.get("industry"), institution)
        if email in seen or not matches(person.get("title") or "", company.get("country"), institution, industry):
            continue
        seen.add(email)
        out.append(LeadInput(
            sourceRecordId=f"directory:{email}", source="target_account", fullName=person["fullName"], email=email,
            companyName=company.get("name") or "", companyDomain=company.get("domain"), country=company.get("country") or "",
            productId=cohort.productId, role=person.get("title"), phone=person.get("phone"),
            institutionType=company.get("institutionType"), employeeCount=company.get("employeeCount"),
            contactPermission=person.get("contactPermission") or "allowed",
        ))
    for lead in db.list_("leads", Lead, limit=100000):
        if len(out) >= limit:
            break
        inp = lead.input
        if lead.normalizedEmail in seen:
            continue
        company = (lead.enrichment.company if lead.enrichment else None) or {}
        institution = str(inp.institutionType or company.get("institutionType") or "")
        industry = _industry_text(company.get("industry"), institution)
        if not matches(inp.role or "", inp.country, institution, industry):
            continue
        seen.add(lead.normalizedEmail)
        out.append(inp.model_copy(update={"source": "target_account", "sourceRecordId": "workspace:" + lead.id, "productId": cohort.productId}))
    return out
