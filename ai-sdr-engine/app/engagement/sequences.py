# app/engagement/sequences.py - Outbound campaigns: eligibility, enrollment, 3-touch drafting, due touches, stop-on-reply
from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from typing import Optional

from app import db
from app.contracts import Campaign, CampaignInput, Draft, Enrollment, Lead
from app.engagement import clock
from app.engagement.drafting import draft_for_lead, touch_of

DRAFT_CONCURRENCY = 6
UNSENT_STATES = {"DRAFT", "APPROVED", "BLOCKED"}


# Emails of contacts that already have an ACTIVE enrollment in any campaign
def _active_contacts() -> tuple[set[str], set[str]]:
    lead_ids, emails = set(), set()
    for enr in db.find("enrollments", Enrollment, state="ACTIVE"):
        lead_ids.add(enr.leadId)
        lead = db.get("leads", enr.leadId, Lead)
        if lead:
            emails.add(lead.normalizedEmail)
    return lead_ids, emails


# Reason a lead cannot be enrolled, or None if eligible
def ineligibility_reason(lead: Lead, active_ids: set[str], active_emails: set[str]) -> Optional[str]:
    if lead.suppressed:
        return "suppressed"
    if lead.input.contactPermission == "denied":
        return "contact_permission_denied"
    if "existing_opportunity" in lead.flags:
        return "existing_opportunity"
    if lead.icp and lead.icp.status == "outside_icp":
        return "outside_icp"
    if lead.id in active_ids or lead.normalizedEmail in active_emails:
        return "already_in_active_sequence"
    return None


# Resume a persisted campaign, reusing every saved enrollment and touch before requesting new model work.
async def create_campaign(inp: CampaignInput, leads: list[Lead], *, campaign: Optional[Campaign] = None) -> Campaign:
    campaign = (db.get("campaigns", campaign.id, Campaign) or campaign) if campaign else Campaign(
        name=inp.name, theme=inp.theme, cohort=inp.cohort, touchDays=inp.touchDays, status="DRAFTING")
    persisted = db.find("enrollments", Enrollment, campaignId=campaign.id)
    campaign.enrollmentIds = list(dict.fromkeys(e.id for e in persisted))
    by_lead = {e.leadId: e for e in persisted}
    selected = {lead.id: lead for lead in leads}
    for enr in persisted:
        if enr.leadId not in selected:
            saved_lead = db.get("leads", enr.leadId, Lead)
            if saved_lead:
                selected[saved_lead.id] = saved_lead
    if campaign.status == "COMPLETED" and not any(e.state == "ACTIVE" for e in persisted):
        db.put("campaigns", campaign)
        return campaign
    days = campaign.touchDays[:3]
    start = clock.demo_now()
    for enr in persisted:
        saved = db.find("drafts", Draft, enrollmentId=enr.id)
        dated = next((d for d in saved if d.dueAt and touch_of(d.kind) and touch_of(d.kind) <= len(days)), None)
        if dated:
            start = clock.parse_iso(dated.dueAt) - timedelta(days=days[touch_of(dated.kind) - 1])
            break
        if enr.nextDueAt and 1 <= enr.nextTouch <= len(days):
            start = clock.parse_iso(enr.nextDueAt) - timedelta(days=days[enr.nextTouch - 1])
            break
    active_ids, active_emails = set(), set()
    for other in db.find("enrollments", Enrollment, state="ACTIVE"):
        if other.campaignId == campaign.id:
            continue
        active_ids.add(other.leadId)
        active_lead = db.get("leads", other.leadId, Lead)
        if active_lead:
            active_emails.add(active_lead.normalizedEmail)
    enrollments: list[tuple[Enrollment, Lead]] = []
    for supplied in selected.values():
        lead = db.get("leads", supplied.id, Lead) or supplied
        enr = by_lead.get(lead.id)
        if enr and enr.state != "ACTIVE":
            enrollments.append((enr, lead))
            continue
        reason = ineligibility_reason(lead, active_ids, active_emails)
        if reason:
            if not any(e.get("leadId") == lead.id and e.get("reason") == reason for e in campaign.exclusions):
                campaign.exclusions.append({"leadId": lead.id, "email": lead.input.email, "reason": reason})
            if enr:
                enr.state = "STOPPED_SUPPRESSION" if reason in ("suppressed", "contact_permission_denied") else "STOPPED_REPLY"
                enr.stopReason, enr.nextDueAt = reason, None
                db.put("enrollments", enr)
            db.put("campaigns", campaign)
            continue
        if enr is None:
            token = hashlib.sha256(f"{campaign.id}:{lead.id}".encode()).hexdigest()[:24]
            enr = Enrollment(id=f"enr_{token}", campaignId=campaign.id, leadId=lead.id,
                             state="ACTIVE", nextTouch=1,
                             nextDueAt=(start + timedelta(days=days[0] if days else 0)).isoformat())
            db.put("enrollments", enr)
            by_lead[lead.id] = enr
            campaign.enrollmentIds.append(enr.id)
            db.put("campaigns", campaign)
        active_ids.add(lead.id)
        active_emails.add(lead.normalizedEmail)
        enrollments.append((enr, lead))
    db.put("campaigns", campaign)
    sem = asyncio.Semaphore(DRAFT_CONCURRENCY)

    # Save each touch pointer against the latest enrollment without overwriting reply stops or send progress.
    def remember(enrollment_id: str, draft: Draft) -> Optional[Enrollment]:
        current = db.get("enrollments", enrollment_id, Enrollment)
        if current is None:
            return None
        if current.state != "ACTIVE" and draft.status in UNSENT_STATES:
            draft = draft.model_copy(update={"status": "STALE"})
            db.put("drafts", draft)
        ids = list(dict.fromkeys([*current.touchDraftIds, draft.id]))
        current.touchDraftIds = sorted(ids, key=lambda value: touch_of(
            saved.kind) if (saved := db.get("drafts", value, Draft)) and touch_of(saved.kind) else 99)
        db.put("enrollments", current)
        return current

    # Draft sequential touches per contact; different contacts remain concurrently bounded.
    async def finish_enrollment(enr: Enrollment, lead: Lead) -> None:
        async with sem:
            for idx, day in enumerate(days):
                kind = f"outbound_touch_{idx + 1}"
                found = [draft for draft in db.find("drafts", Draft, enrollmentId=enr.id) if draft.kind == kind]
                if found:
                    remember(enr.id, found[0])
                    continue
                current = db.get("enrollments", enr.id, Enrollment)
                if current is None or current.state != "ACTIVE":
                    break
                current_lead = db.get("leads", lead.id, Lead) or lead
                generated = await draft_for_lead(
                    current_lead, kind, theme=campaign.theme, touch=idx + 1,
                    enrollment_id=enr.id, due_at=(start + timedelta(days=day)).isoformat())
                # The helper persists its result; save again for compatible injected providers.
                db.put("drafts", generated)
                remember(enr.id, generated)
    tasks = [asyncio.create_task(finish_enrollment(enr, lead)) for enr, lead in enrollments]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    latest = db.get("campaigns", campaign.id, Campaign) or campaign
    current_enrollments = db.find("enrollments", Enrollment, campaignId=campaign.id)
    latest.enrollmentIds = list(dict.fromkeys(e.id for e in current_enrollments))
    latest.status = "ACTIVE" if any(e.state == "ACTIVE" for e in current_enrollments) else "COMPLETED"
    db.put("campaigns", latest)
    return latest


# Touch drafts with an ACTIVE enrollment that are due at the demo time, filtered by status
def _due_with_status(statuses: set[str]) -> list[Draft]:
    now = clock.demo_now()
    out: list[Draft] = []
    enr_cache: dict[str, Optional[Enrollment]] = {}
    for d in db.list_("drafts", Draft, limit=100000):
        if d.status not in statuses or not d.enrollmentId or not d.dueAt:
            continue
        if clock.parse_iso(d.dueAt) > now:
            continue
        if d.enrollmentId not in enr_cache:
            enr_cache[d.enrollmentId] = db.get("enrollments", d.enrollmentId, Enrollment)
        enr = enr_cache[d.enrollmentId]
        if enr and enr.state == "ACTIVE":
            out.append(d)
    return sorted(out, key=lambda d: (d.dueAt or "", touch_of(d.kind) or 0))


# APPROVED touch drafts that are due now and whose enrollment is still ACTIVE
def due_touches() -> list[Draft]:
    return _due_with_status({"APPROVED"})


# Touch drafts that are due now but still awaiting approval (or blocked by guardrails)
def pending_unapproved_due() -> list[Draft]:
    return _due_with_status({"DRAFT", "BLOCKED"})


# Stop every ACTIVE enrollment for a lead (reply or suppression) and mark unsent touches STALE
def stop_on_reply(lead_id: str, reason: str = "reply") -> list[Enrollment]:
    state = "STOPPED_SUPPRESSION" if reason == "suppression" else "STOPPED_REPLY"
    stopped: list[Enrollment] = []
    for enr in db.find("enrollments", Enrollment, leadId=lead_id, state="ACTIVE"):
        enr.state, enr.stopReason, enr.nextDueAt = state, reason, None
        db.put("enrollments", enr)
        for d in db.find("drafts", Draft, enrollmentId=enr.id):
            if d.status in UNSENT_STATES:
                db.put("drafts", d.model_copy(update={"status": "STALE"}))
        stopped.append(enr)
    return stopped


# Advance an enrollment after a touch was sent (idempotent) and complete it after the last touch
def mark_touch_sent(draft: Draft) -> Optional[Enrollment]:
    if not draft.enrollmentId:
        return None
    enr = db.get("enrollments", draft.enrollmentId, Enrollment)
    if not enr or enr.state != "ACTIVE":
        return enr
    touch = touch_of(draft.kind) or enr.nextTouch
    enr.nextTouch = max(enr.nextTouch, touch + 1)
    nxt = [d for d in (db.get("drafts", i, Draft) for i in enr.touchDraftIds)
           if d and (touch_of(d.kind) or 0) == enr.nextTouch]
    enr.nextDueAt = nxt[0].dueAt if nxt else None
    db.put("enrollments", enr)
    return complete_if_done(enr.id)


# Mark an enrollment COMPLETED once its final touch is sent; complete the campaign when no enrollment is ACTIVE
def complete_if_done(enrollment_id: str) -> Optional[Enrollment]:
    enr = db.get("enrollments", enrollment_id, Enrollment)
    if not enr or enr.state != "ACTIVE":
        return enr
    total = len(enr.touchDraftIds) or 3
    if enr.nextTouch <= total:
        return enr
    enr.state, enr.nextDueAt = "COMPLETED", None
    db.put("enrollments", enr)
    campaign = db.get("campaigns", enr.campaignId, Campaign)
    if campaign and not any(
        (e := db.get("enrollments", i, Enrollment)) and e.state == "ACTIVE" for i in campaign.enrollmentIds
    ):
        campaign.status = "COMPLETED"
        db.put("campaigns", campaign)
    return enr
