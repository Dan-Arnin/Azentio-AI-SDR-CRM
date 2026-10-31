# app/api/styles.py - Writing styles ("AI Studio"): uploaded playbooks/scripts that steer how the AI writes outreach
from __future__ import annotations

import re
from typing import Any, Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app import db
from app.contracts import Assessment, Draft, Lead, Message, now_iso
from app.engagement import personalization as pz
from app.llm import calllog

router = APIRouter(prefix="/v1", tags=["styles"])

MAX_UPLOAD_BYTES = 200 * 1024
UPLOAD_EXTENSIONS = (".txt", ".md", ".markdown")
HEADING_RE = re.compile(r"^\s*(#{1,6}\s+(?P<md>.+?)\s*#*\s*|(?P<plain>[A-Za-z][\w &/'-]{1,40}):\s*)$")
BULLET_RE = re.compile(r"^\s*[-*•]\s+(.*\S)\s*$")
SEPARATOR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
RULE_WORDS = re.compile(r"rule|instruction|guideline|tone|voice|style|do'?s|don'?ts", re.IGNORECASE)
EXAMPLE_WORDS = re.compile(r"example|script|sample|template", re.IGNORECASE)


class StyleInput(BaseModel):
    name: str
    description: str = ""
    instructions: str = ""
    examples: list[str] = []
    channels: list[pz.Channel] = list(pz.CHANNELS)
    isDefault: bool = False


class StylePatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    examples: Optional[list[str]] = None
    channels: Optional[list[pz.Channel]] = None


class AssignInput(BaseModel):
    target: Literal["lead", "campaign"]
    targetId: str
    styleId: Optional[str] = None


class PreviewInput(BaseModel):
    leadId: str
    channel: pz.Channel = "email"
    styleId: Optional[str] = None
    kind: Optional[str] = None


# Load a style or 404
def _style(style_id: str) -> pz.StyleRecord:
    style = pz.get_style(style_id)
    if not style:
        raise HTTPException(404, detail={"code": "style_not_found"})
    return style


# Load a lead or 404
def _lead(lead_id: str) -> Lead:
    lead = db.get("leads", lead_id, Lead)
    if not lead:
        raise HTTPException(404, detail={"code": "lead_not_found"})
    return lead


# Make one style the workspace default and clear the flag on every other style
def _set_default(style_id: str) -> None:
    for s in pz.list_styles():
        if s.isDefault != (s.id == style_id):
            db.put("styles", s.model_copy(update={"isDefault": s.id == style_id, "updatedAt": now_iso()}))


# Validate and normalise style text fields (non-empty name, something to follow, trimmed examples)
def _clean(name: str, instructions: str, examples: list[str], channels: list[str]) -> tuple[str, str, list[str], list[str]]:
    name = " ".join(name.split())[:80]
    examples = [e.strip()[:4000] for e in examples if e.strip()][:20]
    if not name:
        raise HTTPException(422, detail={"code": "name_required"})
    if not instructions.strip() and not examples:
        raise HTTPException(422, detail={"code": "style_empty", "message": "Add instructions or at least one example."})
    if not channels:
        raise HTTPException(422, detail={"code": "channels_required"})
    return name, instructions.strip()[:pz.MAX_INSTRUCTIONS * 4], examples, list(dict.fromkeys(channels))


# Split an uploaded playbook into instructions and examples (rule/instruction headings vs example/script/sample headings)
def parse_style_text(text: str) -> tuple[str, list[str]]:
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.replace("\r\n", "\n").split("\n"):
        m = HEADING_RE.match(line)
        if m and (m.group("md") or RULE_WORDS.search(m.group("plain") or "") or EXAMPLE_WORDS.search(m.group("plain") or "")):
            sections.append(((m.group("md") or m.group("plain")).strip(), []))
        else:
            sections[-1][1].append(line)
    if len(sections) == 1:
        return text.strip(), []
    rules: list[str] = []
    examples: list[str] = []
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            continue
        if EXAMPLE_WORDS.search(heading):
            if any(SEPARATOR_RE.match(l) for l in lines):
                chunks, cur = [], []
                for l in lines:
                    if SEPARATOR_RE.match(l):
                        chunks.append("\n".join(cur)); cur = []
                    else:
                        cur.append(l)
                chunks.append("\n".join(cur))
            else:
                chunks = re.split(r"\n\s*\n", body)
            examples += [c.strip() for c in chunks if c.strip()]
        elif RULE_WORDS.search(heading):
            rules += [BULLET_RE.match(l).group(1) if BULLET_RE.match(l) else l.strip() for l in lines if l.strip()]
        else:
            rules += [l.strip() for l in lines if l.strip()]
    return "\n".join(f"- {r}" for r in rules), examples


# List writing styles (seeds "Azentio standard" and "Short & direct" on first use)
@router.get("/styles")
def list_styles() -> dict[str, Any]:
    items = pz.list_styles()
    default = next((s.id for s in items if s.isDefault), None)
    return {"items": [s.model_dump() for s in items], "defaultId": default}


# Create a writing style from JSON
@router.post("/styles")
def create_style(body: StyleInput) -> pz.StyleRecord:
    pz.list_styles()
    name, instructions, examples, channels = _clean(body.name, body.instructions, body.examples, body.channels)
    style = pz.StyleRecord(name=name, description=body.description.strip()[:300], instructions=instructions,
                           examples=examples, channels=channels)
    db.put("styles", style)
    if body.isDefault:
        _set_default(style.id)
    return _style(style.id)


# Create a writing style from an uploaded .txt/.md playbook (rules -> instructions, examples/scripts -> examples)
@router.post("/styles/upload")
async def upload_style(file: UploadFile = File(...), name: str = Form(...), description: str = Form(default=""),
                       channels: str = Form(default="email,linkedin,text")) -> pz.StyleRecord:
    if not (file.filename or "").lower().endswith(UPLOAD_EXTENSIONS):
        raise HTTPException(422, detail={"code": "unsupported_file", "message": "Upload a .txt or .md file."})
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, detail={"code": "file_too_large", "message": "Max 200 KB."})
    instructions, examples = parse_style_text(raw.decode("utf-8", errors="replace"))
    chans = [c.strip().lower() for c in channels.split(",") if c.strip().lower() in pz.CHANNELS]
    return create_style(StyleInput(name=name, description=description or f"Uploaded from {file.filename}",
                                   instructions=instructions, examples=examples, channels=chans))


# Assign a style to a lead or campaign (styleId null clears the override)
@router.post("/styles/assign")
def assign_style(body: AssignInput) -> dict[str, Any]:
    rec_id = f"assign:{body.targetId}"
    if not body.styleId:
        with db._lock:
            db.conn().execute("DELETE FROM styles WHERE id=?", (rec_id,))
            db.conn().commit()
        return {"target": body.target, "targetId": body.targetId, "styleId": None}
    _style(body.styleId)
    rec = pz.StyleAssignment(id=rec_id, target=body.target, targetId=body.targetId, styleId=body.styleId)
    db.put("styles", rec)
    return rec.model_dump()


# Current style override for a lead or campaign id (null when it inherits)
@router.get("/styles/assign/{target_id}")
def get_assignment(target_id: str) -> dict[str, Any]:
    sid = pz.assigned_style_id(target_id)
    style = pz.get_style(sid)
    return {"targetId": target_id, "styleId": style.id if style else None, "styleName": style.name if style else None}


# Which style each of a lead's drafts was written in ({draftId: {styleId, styleName, channel, researchUsed}})
@router.get("/styles/usage")
def style_usage(leadId: str) -> dict[str, Any]:
    return {"leadId": leadId, "drafts": pz.usage_for_lead(leadId)}


# Style a single draft was written in (used by draft cards); 404 when no style was recorded
@router.get("/styles/used/{draft_id}")
def style_used(draft_id: str) -> dict[str, Any]:
    from fastapi import HTTPException
    rec = db.get("styles", f"used:{draft_id}", pz.StyleUsage)
    if not rec or not rec.styleName:
        raise HTTPException(404, detail={"code": "not_found"})
    return {"styleId": rec.styleId, "styleName": rec.styleName, "researchUsed": rec.researchUsed}


# Default email draft kind for previewing a lead (latest email draft kind, else the pipeline's choice)
def _preview_kind(lead: Lead) -> str:
    from app.pipeline import INBOUND_DRAFT_KIND
    emails = [d for d in db.list_("drafts", Draft, lead_id=lead.id) if d.kind not in ("linkedin_note", "sms_message")]
    if emails:
        return emails[-1].kind
    asm = db.get("assessments", lead.latestAssessmentId, Assessment) if lead.latestAssessmentId else None
    if asm and asm.nextQuestion and db.list_("messages", Message, lead_id=lead.id):
        return "gap_fill"
    return INBOUND_DRAFT_KIND.get(lead.input.source) or "outbound_touch_1"


# Generate (without saving) a draft for a lead in a given style and channel, to compare styles side by side
@router.post("/styles/preview")
async def preview_style(body: PreviewInput) -> dict[str, Any]:
    from app.api.messaging import compose_sms
    from app.engagement.drafting import draft_for_lead
    from app.engagement.linkedin import draft_note
    lead = _lead(body.leadId)
    style = _style(body.styleId) if body.styleId else pz.resolve_style(lead, body.channel)
    if style and body.channel not in style.channels:
        raise HTTPException(422, detail={"code": "style_not_for_channel"})
    sid = style.id if style else None
    kind = None
    if body.channel == "linkedin":
        draft = await draft_note(lead, style_id=sid, persist=False)
    elif body.channel == "text":
        draft = await compose_sms(lead, style_id=sid, persist=False)
    else:
        kind = body.kind or _preview_kind(lead)
        asm = db.get("assessments", lead.latestAssessmentId, Assessment) if lead.latestAssessmentId else None
        token = calllog.bind(lead.id, "style_preview")
        try:
            draft = await draft_for_lead(lead, kind, asm, style_id=sid, persist=False)
        finally:
            calllog.unbind(token)
    return {"styleId": sid, "styleName": style.name if style else None, "channel": body.channel, "kind": kind or draft.kind,
            "researchUsed": pz.has_research(pz.context_for(lead)), "subject": draft.subject, "body": draft.body,
            "discoveryQuestion": draft.discoveryQuestion, "claimIds": draft.claimIds, "guardrailFlags": draft.guardrailFlags,
            "status": draft.status, "mode": draft.mode, "model": draft.model}


# Update a style's name, description, instructions, examples or channels
@router.patch("/styles/{style_id}")
def update_style(style_id: str, body: StylePatch) -> pz.StyleRecord:
    style = _style(style_id)
    merged = style.model_copy(update={k: v for k, v in body.model_dump().items() if v is not None})
    name, instructions, examples, channels = _clean(merged.name, merged.instructions, merged.examples, merged.channels)
    updated = merged.model_copy(update={"name": name, "instructions": instructions, "examples": examples,
                                        "channels": channels, "updatedAt": now_iso()})
    db.put("styles", updated)
    return updated


# Delete a custom style (built-ins stay; the house style becomes default if the default is deleted)
@router.delete("/styles/{style_id}")
def delete_style(style_id: str) -> dict[str, Any]:
    style = _style(style_id)
    if style.builtIn:
        raise HTTPException(409, detail={"code": "built_in_style"})
    with db._lock:
        db.conn().execute("DELETE FROM styles WHERE id=?", (style_id,))
        db.conn().commit()
    if style.isDefault:
        _set_default(pz.STANDARD_ID)
    return {"deleted": style_id}


# Make a style the workspace default (one default per workspace)
@router.post("/styles/{style_id}/default")
def make_default(style_id: str) -> pz.StyleRecord:
    _style(style_id)
    _set_default(style_id)
    return _style(style_id)
