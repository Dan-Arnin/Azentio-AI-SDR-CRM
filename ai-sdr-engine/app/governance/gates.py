# app/governance/gates.py - One send gate for every channel (email, SMS/WhatsApp, LinkedIn) plus the send audit trail
from __future__ import annotations

from typing import Optional

from app import db
from app.contracts import Draft, Enrollment, Lead
from app.governance import store


def channel_of(draft: Optional[Draft], subject: str = "") -> str:
    kind = draft.kind if draft else ""
    if kind == "linkedin_note":
        return "linkedin"
    if kind == "sms_message":
        return "whatsapp" if subject.lower().startswith("whatsapp") else "sms"
    return "email"


def blocked_reason(draft: Draft, lead: Lead) -> Optional[str]:
    """Live checks shared by all channels: suppression list (email, @domain, erased hash), consent, paused campaign."""
    from app.engine.identity import is_suppressed

    if lead.suppressed or lead.input.contactPermission == "denied" or is_suppressed(lead.normalizedEmail):
        return "contact_blocked"
    if draft.enrollmentId:
        enr = db.get("enrollments", draft.enrollmentId, Enrollment)
        if enr and enr.campaignId in store.paused_campaigns():
            return "campaign_paused"
    return None


def record(actor: tuple[str, str], draft: Draft, lead_id: str, channel: str, *, message_id: Optional[str] = None,
           blocked: Optional[str] = None, launched: bool = False) -> None:
    action = "send.blocked" if blocked else "send.launched" if launched else "send.sent"
    store.audit(*actor, action, "message" if message_id else "draft", message_id or draft.id, lead_id,
                after={"draftId": draft.id, "kind": draft.kind, "channel": channel, "approvedBy": draft.approvedBy,
                       "enrollmentId": draft.enrollmentId}, detail=blocked or "")
