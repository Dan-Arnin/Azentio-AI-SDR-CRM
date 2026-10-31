# app/engine/icp.py - Deterministic, explainable ICP scoring against the versioned demo policy
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Optional

from app.config import FIXTURES
from app.contracts import EvidenceOrigin, Enrichment, FitBand, IcpCheck, IcpResult, LeadInput
from app.engine.enrichment import normalize_country

REQUIRED_RULES = ("geography", "institution")


# Load the ICP policy fixture once
@lru_cache(maxsize=1)
def load_policy() -> dict[str, Any]:
    return json.loads((FIXTURES / "icp_policy.json").read_text())


# Map a free-text institution type to a canonical policy key
def normalize_institution(value: Optional[str]) -> Optional[str]:
    if not value or not str(value).strip():
        return None
    v = str(value).strip().lower().replace("_", " ")
    for key, aliases in load_policy()["institutionAliases"].items():
        if v == key.replace("_", " ") or v in aliases:
            return key
    for key, aliases in load_policy()["institutionAliases"].items():
        if any(re.search(rf"\b{re.escape(a)}\b", v) for a in aliases if " " in a):
            return key
    return "other"


# Infer seniority from a job title using policy keyword rules
def seniority_from_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    t = title.lower()
    for rule in load_policy()["roles"]["titleSeniority"]:
        if any(re.search(rf"\b{re.escape(k)}\b", t) for k in rule["keywords"]):
            return rule["seniority"]
    return None


# Pick the form value if present, else the enrichment value, with its origin
def _pick(form_v: Any, enr_v: Any) -> tuple[Any, Optional[EvidenceOrigin]]:
    if form_v is not None and str(form_v).strip() != "":
        return form_v, "form"
    if enr_v is not None and str(enr_v).strip() != "":
        return enr_v, "mock_enrichment"
    return None, None


# Find a recorded form-vs-Apollo conflict for a field
def _conflict(enr: Enrichment, field: str) -> Optional[dict[str, Any]]:
    return next((c for c in enr.conflicts if c.get("field") == field), None)


# Suffix describing a conflict for check details
def _conflict_note(enr: Enrichment, field: str) -> str:
    c = _conflict(enr, field)
    return f" (Public source says '{c.get('public', c.get('apollo'))}' - conflict, SDR to verify)" if c else ""


# Geography rule: country must be in the ICP geography list
def _check_geography(inp: LeadInput, enr: Enrichment, weight: int, geos: list[str]) -> IcpCheck:
    country, origin = _pick(normalize_country(inp.country), enr.company.get("country"))
    if country is None:
        return IcpCheck(rule="geography", passed=None, weight=weight, points=0,
                        detail="Country unknown from form and enrichment.", origin=None)
    note = _conflict_note(enr, "country")
    if country in geos:
        return IcpCheck(rule="geography", passed=True, weight=weight, points=weight,
                        detail=f"Country {country} is in ICP geography ({', '.join(geos)}){note}.", origin=origin)
    return IcpCheck(rule="geography", passed=False, weight=weight, points=0,
                    detail=f"Country {country} is outside ICP geography ({', '.join(geos)}){note}.", origin=origin)


# Institution rule: institution type allowed (full) or partial for the product
def _check_institution(inp: LeadInput, enr: Enrichment, weight: int, allowed: list[str],
                       partial: list[str], product_name: str) -> IcpCheck:
    raw, origin = _pick(inp.institutionType, enr.company.get("institutionType"))
    inst = normalize_institution(raw)
    if inst is None:
        return IcpCheck(rule="institution", passed=None, weight=weight, points=0,
                        detail="Institution type unknown from form and enrichment.", origin=None)
    note = _conflict_note(enr, "institutionType")
    if inst in allowed:
        return IcpCheck(rule="institution", passed=True, weight=weight, points=weight,
                        detail=f"Institution type '{inst}' is a target for {product_name}{note}.", origin=origin)
    if inst in partial:
        return IcpCheck(rule="institution", passed=True, weight=weight, points=weight // 2,
                        detail=f"Institution type '{inst}' is a partial fit for {product_name} "
                               f"(e.g. Islamic window){note}.", origin=origin)
    return IcpCheck(rule="institution", passed=False, weight=weight, points=0,
                    detail=f"Institution type '{inst}' is not a target for {product_name} "
                           f"(targets: {', '.join(allowed + partial)}){note}.", origin=origin)


# Role rule: decision makers full points, influencers half, unknown zero
def _check_role(inp: LeadInput, enr: Enrichment, weight: int, roles: dict[str, Any]) -> IcpCheck:
    title, origin = _pick(inp.role, enr.person.get("title"))
    seniority = seniority_from_title(title)
    if seniority is None and _conflict(enr, "title") is None:
        seniority = enr.person.get("seniority")
    note = _conflict_note(enr, "title")
    label = f"'{title}'" if title else "no title"
    if seniority in roles["decisionMaker"]:
        return IcpCheck(rule="role", passed=True, weight=weight, points=weight,
                        detail=f"Role {label} reads as decision maker ({seniority}){note}.", origin=origin)
    if seniority in roles["influencer"]:
        return IcpCheck(rule="role", passed=True, weight=weight, points=weight // 2,
                        detail=f"Role {label} reads as influencer ({seniority}){note}.", origin=origin)
    if seniority is None:
        return IcpCheck(rule="role", passed=None, weight=weight, points=0,
                        detail=f"Unclear role ({label}) -> SDR review{note}.", origin=origin)
    return IcpCheck(rule="role", passed=False, weight=weight, points=0,
                    detail=f"Role {label} ({seniority}) is not a buying role; find the decision maker{note}.",
                    origin=origin)


# Size rule: at/above preferred minimum full points, below gets partial (never a rejection)
def _check_size(inp: LeadInput, enr: Enrichment, weight: int, size: dict[str, Any]) -> IcpCheck:
    count, origin = _pick(inp.employeeCount, enr.company.get("employeeCount"))
    min_size = size["preferredMin"]
    if count is None:
        return IcpCheck(rule="size", passed=None, weight=weight, points=0,
                        detail="Employee count unknown.", origin=None)
    note = _conflict_note(enr, "employeeCount")
    count = int(count)
    if count >= min_size:
        return IcpCheck(rule="size", passed=True, weight=weight, points=weight,
                        detail=f"{count} employees meets preferred size (>= {min_size}){note}.", origin=origin)
    return IcpCheck(rule="size", passed=False, weight=weight, points=int(weight * size["belowPreferredFraction"]),
                    detail=f"{count} employees is below preferred size ({min_size}); partial points, "
                           f"not a rejection{note}.", origin=origin)


# Product-relevance rule: inquiry/asset/event text mentions product keywords
def _check_relevance(inp: LeadInput, weight: int, keywords: list[str], product_name: str,
                     product_known: bool) -> IcpCheck:
    text = " ".join(x for x in (inp.inquiry, inp.assetName, inp.eventName) if x).lower()
    hits = [k for k in keywords if re.search(rf"\b{re.escape(k)}\b", text)]
    if hits:
        return IcpCheck(rule="product_relevance", passed=True, weight=weight, points=weight,
                        detail=f"Inquiry mentions {', '.join(sorted(set(hits))[:5])} (relevant to {product_name}).",
                        origin="form")
    if product_known:
        return IcpCheck(rule="product_relevance", passed=None, weight=weight, points=weight // 2,
                        detail=f"{product_name} selected, but the inquiry text does not describe a need yet.",
                        origin="form")
    return IcpCheck(rule="product_relevance", passed=None, weight=weight, points=0,
                    detail="No product selected and no product signal in the inquiry.", origin=None)


# Map a score to a fit band
def _fit(score: int, bands: dict[str, int]) -> FitBand:
    if score >= bands["High"]:
        return "High"
    if score >= bands["Medium"]:
        return "Medium"
    return "Low"


# Score a lead against the ICP policy for its product, with explainable checks
def score_icp(inp: LeadInput, enr: Enrichment) -> IcpResult:
    policy = load_policy()
    w = policy["weights"]
    products = policy["products"]
    product = products.get(inp.productId)
    product_known = product is not None
    if product_known:
        allowed, partial = product["institutions"]["allowed"], product["institutions"]["partial"]
        keywords, product_name = product["keywords"], product["name"]
    else:
        allowed = sorted({i for p in products.values() for i in p["institutions"]["allowed"]})
        partial = []
        keywords = sorted({k for p in products.values() for k in p["keywords"]})
        product_name = "any Azentio product"

    checks = [
        _check_geography(inp, enr, w["geography"], policy["geographies"]),
        _check_institution(inp, enr, w["institution"], allowed, partial, product_name),
        _check_role(inp, enr, w["role"], policy["roles"]),
        _check_size(inp, enr, w["size"], policy["size"]),
        _check_relevance(inp, w["productRelevance"], keywords, product_name, product_known),
    ]
    if not enr.mock:
        for check in checks:
            if check.origin == "mock_enrichment":
                check.origin = "public_research"
    score = max(0, min(100, sum(c.points for c in checks)))
    required = [c for c in checks if c.rule in REQUIRED_RULES]
    failed = [c for c in required if c.passed is False]
    absent = [c for c in required if c.passed is None]

    reasons: list[str] = []
    if enr.conflicts:
        status = "needs_review"
        reasons.append("Supplied company details conflict with a cited public source; review before outreach.")
    elif not product_known:
        status = "needs_review"
        reasons.append("Product interest is unknown - a human should confirm which product fits before scoring.")
    elif failed:
        status = "outside_icp"
        reasons.append("Outside ICP: " + " ".join(c.detail for c in failed))
    elif absent:
        status = "needs_info"
        reasons.append("Needs info: " + " ".join(c.detail for c in absent) + " Ask before qualifying.")
    else:
        status = "matches_icp"
        reasons.append(f"Matches the {product_name} ICP ({policy['version']}).")

    if failed:
        cap = policy["outsideIcpScoreCap"]
        if score > cap:
            reasons.append(f"Score capped at {cap} because a required ICP rule failed.")
            score = cap
    for c in checks:
        if c.rule not in REQUIRED_RULES or c.passed is True:
            reasons.append(f"{c.rule.replace('_', ' ').capitalize()}: {c.detail}")
    for c in enr.conflicts:
        reasons.append(f"Data conflict on {c['field']}: form says '{c['form']}', public source says "
                       f"'{c.get('public', c.get('apollo'))}'. Form value used; verify with the buyer.")

    return IcpResult(
        productId=inp.productId,
        policyVersion=policy["version"],
        score=score,
        fit=_fit(score, policy["fitBands"]),
        status=status,
        checks=checks,
        reasons=reasons,
    )
