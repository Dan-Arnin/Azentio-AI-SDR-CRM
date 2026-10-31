# app/governance/guardrails.py - Reply injection detection and guardrail-flag records (wording flags come from drafts)
from __future__ import annotations

import re
from typing import Any, Optional

from app import db
from app.contracts import Draft
from app.governance import store

# Text in a buyer reply that tries to instruct the system rather than talk to a person
INJECTION_RE = re.compile(
    r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(instructions?|rules?|prompts?|guardrails?|polic(y|ies)|system)\b"
    r"|\b(mark|set|flag|score|classify|route)\b[^.\n]{0,30}\b(me|this lead|this|the lead)\b[^.\n]{0,30}"
    r"\b(qualified|sales[- ]ready|hot|high fit|approved|budget (is )?(approved|confirmed)|100)\b"
    r"|\b(system prompt|developer message|you are now|act as (an?|the) (admin|system|assistant))\b"
    r"|<\s*/?\s*(system|instructions?|untrusted_[a-z_]+)\s*>",
    re.IGNORECASE,
)

WORDING = {
    "pricing": "Pricing", "sla_commitment": "Commitment or guarantee", "overclaim": "Overclaim",
    "regulator_claim": "Regulator claim", "outside_link": "Outside link", "missing_opt_out": "Missing opt-out line",
    "numeric_claim": "Unverified figure", "unsupported_claim": "Claim not in approved list", "meeting_promise": "Promised meeting time",
}


def injection_excerpt(text: str) -> Optional[str]:
    m = INJECTION_RE.search(text or "")
    if not m:
        return None
    start, end = max(0, m.start() - 40), min(len(text), m.end() + 40)
    return text[start:end].strip()


def record_injection(lead_id: str, message_id: str, excerpt: str) -> None:
    store.upsert_flag(f"inj:{message_id}", "injection", "hidden_instruction", excerpt, lead_id, message_id=message_id)
    store.audit("system", "system", "guardrail.injection_blocked", "message", message_id, lead_id,
                after={"excerpt": excerpt[:200]}, detail="Reply contained instructions to the system; score left unchanged, held for a person.")


def _matches(reason: str, text: str) -> list[str]:
    from app.engagement import drafting as d

    pattern = {"pricing": d.PRICING_RE, "sla_commitment": d.SLA_RE, "overclaim": d.OVERCLAIM_RE, "regulator_claim": d.REGULATOR_RE,
               "numeric_claim": d.NUMERIC_RE, "meeting_promise": d.MEETING_RE, "outside_link": d.LINK_RE}.get(reason)
    return list(dict.fromkeys(m.group(0) for m in pattern.finditer(text)))[:5] if pattern else []


def highlights(flag: dict[str, Any], draft: Optional[Draft]) -> list[str]:
    """Phrases to highlight when a reviewer opens the flagged draft or reply."""
    if flag["kind"] == "injection":
        m = INJECTION_RE.search(flag.get("excerpt") or "")
        return [m.group(0)] if m else []
    return _matches(flag["reason"], f"{draft.subject}\n{draft.body}") if draft else []


def sync_wording_flags() -> None:
    """Mirror each draft's current wording flags as flag records; flags from an older version become superseded."""
    existing = {f["id"]: f for f in store.flags()}
    for draft in db.list_("drafts", Draft, limit=100000):
        for reason in draft.guardrailFlags:
            if reason not in WORDING:
                continue
            fid = f"drf:{draft.id}:v{draft.version}:{reason}"
            if fid not in existing:
                hits = _matches(reason, f"{draft.subject}\n{draft.body}")
                store.upsert_flag(fid, "wording", reason, ", ".join(hits) or WORDING[reason], draft.leadId, draft_id=draft.id,
                                  created_at=draft.createdAt)
        for fid, f in existing.items():
            if f["draft_id"] == draft.id and f["status"] == "open" and not fid.startswith(f"drf:{draft.id}:v{draft.version}:"):
                store.set_flag_status(fid, "superseded", f"draft edited to version {draft.version}", "system")


def unblock_if_cleared(draft_id: str) -> bool:
    """After a reviewer clears a false positive, a draft blocked only by cleared flags can go back for approval."""
    from app.engagement.drafting import BLOCKING_FLAGS

    draft = db.get("drafts", draft_id, Draft)
    if not draft or draft.status != "BLOCKED" or "delivery_unknown" in draft.guardrailFlags:
        return False
    cleared = {f["reason"] for f in store.flags() if f["draft_id"] == draft_id and f["status"] == "cleared"
               and f["id"].startswith(f"drf:{draft_id}:v{draft.version}:")}
    if (BLOCKING_FLAGS & set(draft.guardrailFlags)) - cleared:
        return False
    db.put("drafts", draft.model_copy(update={"status": "DRAFT"}))
    return True
