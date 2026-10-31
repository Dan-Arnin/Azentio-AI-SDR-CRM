"""Contact hygiene: suppression, regional consent, one active sequence per contact, send caps, copy guardrails."""
import re
from datetime import datetime, timezone

from . import db
from .config import cfg


def is_suppressed(email: str) -> dict | None:
    return db.get("suppressions", email.lower(), key_col="email")


def suppress(email: str, reason: str) -> None:
    if not is_suppressed(email):
        db.insert("suppressions", {"email": email.lower(), "reason": reason, "created_at": db.now()})


def consent_ok(region: str | None, consent: bool | None) -> tuple[bool, str]:
    if region in cfg()["consent_required_regions"]:
        if consent:
            return True, f"{region} contact with recorded consent."
        return False, f"{region} contact without recorded marketing consent (GDPR). Outreach blocked."
    return True, "No regional consent requirement for this region." if region else "Region unknown; no consent rule applied."


def active_enrollment(email: str) -> dict | None:
    rows = db.query("SELECT * FROM enrollments WHERE email = ? AND status = 'active'", [email.lower()])
    return rows[0] if rows else None


def sends_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    rows = db.query("SELECT COUNT(*) AS n FROM messages WHERE direction='out' AND status='sent' AND substr(sent_at,1,10)=?", [today])
    return rows[0]["n"]


def under_send_cap() -> bool:
    return sends_today() < cfg()["guardrails"]["daily_send_cap"]


def check(email: str, region: str | None, consent: bool | None) -> dict:
    """Everything that must be true before any outreach to this contact."""
    blocks = []
    sup = is_suppressed(email)
    if sup:
        blocks.append(f"Suppressed: {sup['reason']}")
    ok, why = consent_ok(region, consent)
    if not ok:
        blocks.append(why)
    enr = active_enrollment(email)
    if enr:
        blocks.append(f"Already in active sequence {enr['sequence_id']} (one active sequence per contact).")
    return {"ok": not blocks, "blocks": blocks, "consent": why}


def copy_violations(text: str) -> list[str]:
    """No pricing, discounts, commitments or unapproved numeric claims in outbound copy."""
    t = text.lower()
    hits = []
    for term in cfg()["banned_terms"]:
        pattern = re.escape(term) if not term.isalpha() else rf"\b{re.escape(term)}\b"
        if re.search(pattern, t):
            hits.append(f"banned term '{term}'")
    if re.search(r"(?:usd|aed|sar|inr|\$|₹)\s?\d", t):
        hits.append("currency amount")
    return hits
