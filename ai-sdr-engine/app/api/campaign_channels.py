# app/api/campaign_channels.py - Campaign channels beyond email: enable LinkedIn / WhatsApp / text steps, draft them per contact, report per-channel progress
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import db
from app.contracts import Campaign, Channel, Draft, Enrollment, Lead

router = APIRouter(prefix="/v1", tags=["campaigns"])

OPEN = ("DRAFT", "APPROVED", "BLOCKED")
SENT = ("SIMULATED_SENT", "SENT")
_tasks: set[asyncio.Task] = set()


class ChannelInput(BaseModel):
    channel: Channel


# Channel a draft belongs to, from its kind and recipient label
def draft_channel(d: Draft) -> str:
    if d.kind == "linkedin_note":
        return "linkedin"
    if d.kind == "sms_message":
        return "whatsapp" if (d.to or "").startswith("WhatsApp") else "sms"
    return "email"


# Per-channel counts for a campaign: drafts waiting for review, approved and ready, and sent
def channel_stats(c: Campaign, enrollments: list[Enrollment]) -> dict[str, dict[str, int]]:
    enr_ids = {e.id for e in enrollments}
    lead_ids = {e.leadId for e in enrollments}
    stats = {ch: {"review": 0, "approved": 0, "sent": 0} for ch in ("email", "linkedin", "whatsapp", "sms")}
    for lead_id in lead_ids:
        for d in db.list_("drafts", Draft, lead_id=lead_id):
            if d.enrollmentId not in enr_ids:
                continue
            s = stats[draft_channel(d)]
            if d.status in ("DRAFT", "BLOCKED"):
                s["review"] += 1
            elif d.status == "APPROVED":
                s["approved"] += 1
            elif d.status in SENT:
                s["sent"] += 1
    return stats


# Channels a campaign uses: the ones chosen at creation plus any that already have drafts
def campaign_channels(c: Campaign, stats: dict[str, dict[str, int]]) -> list[str]:
    used = [ch for ch, s in stats.items() if any(s.values())]
    return list(dict.fromkeys(["email", *c.channels, *used]))


# Active, reachable contacts in a campaign that don't already have an open draft on this channel
def _targets(c: Campaign, channel: str) -> list[tuple[Enrollment, Lead]]:
    out = []
    for eid in c.enrollmentIds:
        enr = db.get("enrollments", eid, Enrollment)
        lead = db.get("leads", enr.leadId, Lead) if enr and enr.state == "ACTIVE" else None
        if not lead or lead.suppressed or lead.input.contactPermission == "denied":
            continue
        if any(draft_channel(d) == channel and d.status in OPEN for d in db.list_("drafts", Draft, lead_id=lead.id)):
            continue
        out.append((enr, lead))
    return out


# Write one approval-ready draft per contact on a channel (LinkedIn note or short WhatsApp/text message)
async def draft_channel_step(campaign_id: str, channel: str) -> int:
    from app.api.messaging import compose_sms
    from app.engagement.linkedin import draft_note
    c = db.get("campaigns", campaign_id, Campaign)
    if not c or channel == "email":
        return 0
    made = 0
    for enr, lead in _targets(c, channel):
        try:
            if channel == "linkedin":
                await draft_note(lead, c.theme, enr.id)
            else:
                await compose_sms(lead, enrollment_id=enr.id, channel=channel)
            made += 1
        except Exception:
            continue
    return made


# Run channel drafting in the background so the request returns immediately
def schedule_channel_drafts(campaign_id: str, channels: list[str]) -> None:
    async def run() -> None:
        for ch in channels:
            if ch != "email":
                await draft_channel_step(campaign_id, ch)
    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# Add a LinkedIn, WhatsApp or text step to a campaign; drafts appear in Approvals as they're written
@router.post("/campaigns/{campaign_id}/channels", status_code=202)
async def add_channel(campaign_id: str, body: ChannelInput) -> dict[str, Any]:
    c = db.get("campaigns", campaign_id, Campaign)
    if not c:
        raise HTTPException(404, detail={"code": "not_found"})
    if body.channel not in c.channels:
        db.put("campaigns", c.model_copy(update={"channels": [*c.channels, body.channel]}))
    schedule_channel_drafts(campaign_id, [body.channel])
    label = {"linkedin": "LinkedIn notes", "whatsapp": "WhatsApp messages", "sms": "text messages"}.get(body.channel, "messages")
    return {"status": "drafting", "text": f"Writing {label} for each active contact. They'll appear in Approvals for you to review."}
