# app/engagement/approvals.py - Human review of drafts: edit (new version, stale approval), approve (hash-bound), reject
from __future__ import annotations

from typing import Optional

from app import db
from app.contracts import Draft, now_iso
from app.engagement import drafting
from app.engagement.drafting import check_guardrails, finalize_body, status_for_flags

LOCKED_STATES = {"SIMULATED_SENT", "STALE"}


# Apply a reviewer edit: same id, version+1, new hash, guardrails re-run, any prior approval cleared
def edit_draft(draft: Draft, subject: Optional[str], body: Optional[str]) -> Draft:
    if draft.status in LOCKED_STATES or "delivery_unknown" in draft.guardrailFlags:
        raise ValueError("not_editable")
    new_subject = (subject if subject is not None else draft.subject).strip()
    new_body = finalize_body(body) if body is not None else draft.body
    flags = check_guardrails(new_subject, new_body, draft.claimIds,
                             channel={"sms_message": "text", "linkedin_note": "linkedin"}.get(draft.kind, "email"))
    if draft.unresolvedClaims and "unsupported_claim" not in flags:
        flags.append("unsupported_claim")
    updated = draft.model_copy(update={
        "subject": new_subject,
        "body": new_body,
        "version": draft.version + 1,
        "contentHash": drafting.content_hash(new_subject, new_body),
        "guardrailFlags": flags,
        "status": status_for_flags(flags),
        "approvedBy": None,
        "approvedAt": None,
    })
    db.put("drafts", updated)
    return updated


# Approve the exact content the reviewer saw; stale hash or blocked guardrail raises ValueError
def approve_draft(draft: Draft, reviewer: str, content_hash: Optional[str] = None) -> Draft:
    current = drafting.content_hash(draft.subject, draft.body)
    if (content_hash and content_hash != draft.contentHash) or draft.contentHash != current:
        raise ValueError("stale_version")
    if draft.status == "BLOCKED":
        raise ValueError("blocked_guardrail")
    if draft.status == "APPROVED":
        return draft
    if draft.status != "DRAFT":
        raise ValueError("invalid_state")
    updated = draft.model_copy(update={"status": "APPROVED", "approvedBy": reviewer, "approvedAt": now_iso()})
    db.put("drafts", updated)
    return updated


# Reject a draft with a reviewer reason (kept in nextAction since Draft has no reason field)
def reject_draft(draft: Draft, reason: str) -> Draft:
    if draft.status == "SIMULATED_SENT":
        raise ValueError("invalid_state")
    updated = draft.model_copy(update={
        "status": "REJECTED", "approvedBy": None, "approvedAt": None,
        "nextAction": f"Rejected by reviewer: {reason}".strip(),
    })
    db.put("drafts", updated)
    return updated
