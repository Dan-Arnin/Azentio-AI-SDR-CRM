# app/engine/identity.py - Lead identity resolution (dedupe, conflicts, existing opportunity) and consent checks
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app import db
from app.config import FIXTURES
from app.contracts import Enrichment, Lead, LeadInput
from app.engine.enrichment import (
    EU_EEA_UK, company_domain_for, normalize_country, normalize_email,
)

OPEN_OPPORTUNITY_STATUSES = frozenset({"open"})


class IdentityResult(BaseModel):
    match: Literal["new", "existing_lead", "conflict"]
    leadId: Optional[str] = None
    accountKey: Optional[str] = None
    existingOpportunity: Optional[dict[str, Any]] = None
    reasons: list[str] = Field(default_factory=list)
    normalizedEmail: str = ""
    accountOwner: Optional[str] = None


class ConsentResult(BaseModel):
    allowed: bool
    reason: str
    regime: Optional[str] = None


class _SuppressionRow(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str


# Load the CRM seed fixture once
@lru_cache(maxsize=1)
def _crm() -> dict[str, Any]:
    return json.loads((FIXTURES / "crm_seed.json").read_text())


# Reduce a phone number to digits for comparison
def normalize_phone(phone: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D", "", phone or "")
    return digits if len(digits) >= 7 else None


# Find the seeded CRM account for an account domain or company name
def crm_account(account_key: Optional[str], company_name: str = "") -> Optional[dict[str, Any]]:
    name = company_name.strip().lower()
    for acc in _crm()["accounts"]:
        if account_key and acc["domain"] == account_key:
            return acc
    for acc in _crm()["accounts"]:
        if name and acc["name"].lower() == name:
            return acc
    return None


# Find an open seeded opportunity for the same account and product
def existing_opportunity(account_key: Optional[str], product_id: str,
                         company_name: str = "") -> Optional[dict[str, Any]]:
    acc = crm_account(account_key, company_name)
    domain = account_key or (acc or {}).get("domain")
    if not domain:
        return None
    for opp in _crm()["opportunities"]:
        if (opp["accountDomain"] == domain and opp["productId"] == product_id
                and opp.get("status", "open") in OPEN_OPPORTUNITY_STATUSES):
            return {k: opp[k] for k in ("id", "ownerId", "stage", "productId", "accountName")}
    return None


# Resolve whether a lead is new, a duplicate of an existing lead, or an identity conflict
def check_identity(inp: LeadInput) -> IdentityResult:
    email = normalize_email(inp.email)
    account_key = company_domain_for(inp)
    acc = crm_account(account_key, inp.companyName)
    opp = existing_opportunity(account_key, inp.productId, inp.companyName)
    reasons: list[str] = []
    if account_key is None:
        reasons.append("No company domain (personal mailbox and no company domain given); account match by name only.")
    if acc:
        reasons.append(f"Account {acc['name']} already in CRM, owned by {acc['ownerId']}.")
    if opp:
        reasons.append(f"Open opportunity {opp['id']} ({opp['productId']}, stage {opp['stage']}) "
                       f"owned by {opp['ownerId']} - route to the existing owner.")
    base = dict(accountKey=account_key, existingOpportunity=opp, normalizedEmail=email,
                accountOwner=(acc or {}).get("ownerId"))

    same_email = db.find("leads", Lead, normalizedEmail=email) if email else []
    same_product = [l for l in same_email if l.input.productId == inp.productId]
    if same_product:
        lead = same_product[0]
        reasons.insert(0, f"Same email and product as existing lead {lead.id}; merge source instead of creating a duplicate.")
        return IdentityResult(match="existing_lead", leadId=lead.id, reasons=reasons, **base)
    if same_email:
        reasons.insert(0, f"Email already known on lead {same_email[0].id} for product "
                          f"{same_email[0].input.productId}; new product interest creates a separate lead.")

    phone = normalize_phone(inp.phone)
    if phone:
        for lead in db.list_("leads", Lead, limit=100000):
            if lead.normalizedEmail != email and normalize_phone(lead.input.phone) == phone:
                reasons.insert(0, f"Phone matches lead {lead.id} ({lead.normalizedEmail}) but the email differs; "
                                  f"quarantine for identity review.")
                return IdentityResult(match="conflict", leadId=lead.id, reasons=reasons, **base)

    reasons.insert(0, "No existing lead with this email and product.")
    return IdentityResult(match="new", leadId=None, reasons=reasons, **base)


# Return the suppression reason if the email is on the CRM seed or runtime suppression list
def is_suppressed(email: str) -> Optional[str]:
    e = normalize_email(email)
    for row in _crm()["suppressions"]:
        if normalize_email(row["email"]) == e:
            return row.get("reason", "suppressed")
    for row in db.list_("suppressions", _SuppressionRow, limit=100000):
        d = row.model_dump()
        if normalize_email(str(d.get("email") or d.get("normalizedEmail") or "")) == e:
            return str(d.get("reason") or "suppressed")
    return None


# Decide whether we may contact this lead (permission, suppression, GDPR)
def consent_check(inp: LeadInput, enr: Optional[Enrichment]) -> ConsentResult:
    if inp.contactPermission == "denied":
        return ConsentResult(allowed=False, reason="Contact permission denied on the source record.")
    suppressed = is_suppressed(inp.email)
    if suppressed:
        return ConsentResult(allowed=False, reason=f"Email is on the suppression list ({suppressed}).")
    country = normalize_country(inp.country) or ((enr.company.get("country") if enr else None))
    if country in EU_EEA_UK:
        if inp.contactPermission == "allowed":
            return ConsentResult(allowed=True, regime="GDPR",
                                 reason=f"{country} contact: explicit contact permission recorded (GDPR).")
        return ConsentResult(allowed=False, regime="GDPR",
                             reason=f"{country} contact requires explicit contact permission under GDPR; "
                                    f"permission is '{inp.contactPermission}'. Hold outreach.")
    return ConsentResult(allowed=True,
                         reason=f"B2B legitimate interest - demo policy (permission '{inp.contactPermission}', "
                                f"country {country or 'unknown'}).")
