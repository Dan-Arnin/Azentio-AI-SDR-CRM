# app/engagement/transport.py - Email transport with send gates (approval, hash, consent, sequence, caps); Resend (test inbox) or simulated
from __future__ import annotations

import logging

from app import config, db
from app.contracts import Draft, Enrollment, Lead, Message
from app.engagement import clock, resend_email
from app.engagement.drafting import content_hash
from app.engagement.sequences import mark_touch_sent

log = logging.getLogger("uvicorn.error")

PER_CONTACT_DAILY_CAP = 2
GLOBAL_DAILY_CAP = 20


# Outbound messages sent on the current demo date (any transport)
def _sent_today() -> list[Message]:
    today = clock.demo_date(clock.demo_now())
    return [m for m in db.find("messages", Message, direction="outbound")
            if clock.demo_date(m.at) == today]


# Lead ids that share this lead's contact email (caps are per contact, not per lead)
def _contact_lead_ids(lead: Lead) -> set[str]:
    ids = {lead.id}
    ids.update(l.id for l in db.find("leads", Lead, normalizedEmail=lead.normalizedEmail))
    return ids


# Remaining sends today: (for this contact, overall)
def remaining_capacity(lead: Lead) -> tuple[int, int]:
    sent = _sent_today()
    contact_ids = _contact_lead_ids(lead)
    per_contact = sum(1 for m in sent if m.leadId in contact_ids)
    return PER_CONTACT_DAILY_CAP - per_contact, GLOBAL_DAILY_CAP - len(sent)


# Existing message for an already-sent draft (idempotent re-send)
def _existing_message(draft_id: str) -> Message | None:
    found = db.find("messages", Message, draftId=draft_id, direction="outbound")
    return found[0] if found else None


# Run every send gate; raise ValueError(code) on the first failure
def check_send_gates(draft: Draft, lead: Lead) -> None:
    from app.engine.identity import is_suppressed
    from app.governance import store

    # the suppression list is checked live, so entries added after intake (domain blocks, erasures) still stop the send
    if lead.suppressed or lead.input.contactPermission != "allowed" or is_suppressed(lead.normalizedEmail):
        raise ValueError("contact_blocked")
    if draft.enrollmentId:
        enr = db.get("enrollments", draft.enrollmentId, Enrollment)
        if not enr or enr.state != "ACTIVE":
            raise ValueError("sequence_stopped")
        if enr.campaignId in store.paused_campaigns():
            raise ValueError("campaign_paused")
    stored = db.get("drafts", draft.id, Draft)
    if (draft.contentHash != content_hash(draft.subject, draft.body)
            or (stored and stored.contentHash != draft.contentHash)):
        raise ValueError("stale_version")
    if draft.status != "APPROVED" or (stored and stored.status != "APPROVED"):
        raise ValueError("not_approved")
    per_contact, overall = remaining_capacity(lead)
    if per_contact <= 0 or overall <= 0:
        raise ValueError("send_cap")


# Deliver an approved draft: gate, send via Resend (or simulate), record the outbound Message, mark the draft sent
def simulate_send(draft: Draft, lead: Lead) -> Message:
    current = db.get("drafts", draft.id, Draft) or draft
    if current.status == "SIMULATED_SENT":
        existing = _existing_message(current.id)
        if existing:
            return existing
    check_send_gates(draft, lead)
    delivery = {"transport": "simulated", "origin": "simulated", "deliveryStatus": "simulated"}
    if config.EMAIL_TRANSPORT == "resend":
        try:
            provider_id, delivered_to = resend_email.send(draft, lead)
        except resend_email.DeliveryError as exc:
            if getattr(exc, "ambiguous", False):
                blocked = draft.model_copy(update={"status": "BLOCKED", "guardrailFlags": list(dict.fromkeys(draft.guardrailFlags + ["delivery_unknown"]))})
                db.put("drafts", blocked)
                log.warning("email delivery needs reconciliation for draft %s", draft.id)
                raise ValueError("delivery_unknown") from None
            log.warning("email delivery rejected for draft %s", draft.id)
            raise ValueError("delivery_failed") from exc
        delivery = {"transport": "resend", "origin": "resend", "deliveryStatus": "accepted",
                    "providerMessageId": provider_id, "deliveredTo": delivered_to}
    msg = Message(
        leadId=lead.id, threadId=lead.threadId, direction="outbound", subject=draft.subject, body=draft.body,
        at=clock.demo_now_iso(), draftId=draft.id, enrollmentId=draft.enrollmentId, intendedTo=lead.input.email,
        **delivery,
    )
    db.put("messages", msg)
    sent = draft.model_copy(update={"status": "SIMULATED_SENT"})
    db.put("drafts", sent)
    if sent.enrollmentId:
        mark_touch_sent(sent)
    return msg
