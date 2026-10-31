# app/governance/rights.py - Data subject requests (export, erase) and retention-based anonymisation
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from app import sqlstore
from app import config, db
from app.contracts import Assessment, CrmEvent, Draft, Lead, Message, Run, Task, now_iso
from app.governance import store

ERASED = "[erased]"
DEFAULT_RETENTION_DAYS = 365


def leads_for(email: str) -> list[Lead]:
    e = email.strip().lower()
    return [l for l in db.list_("leads", Lead, limit=100000) if l.normalizedEmail == e]


def export_bundle(email: str) -> dict[str, Any]:
    """Everything held about one person, as one JSON document."""
    out: dict[str, Any] = {"subject": email.strip().lower(), "exportedAt": now_iso(), "leads": []}
    for lead in leads_for(email):
        out["leads"].append({
            "lead": lead.model_dump(mode="json"),
            "messages": [m.model_dump(mode="json") for m in db.list_("messages", Message, lead_id=lead.id)],
            "drafts": [d.model_dump(mode="json") for d in db.list_("drafts", Draft, lead_id=lead.id)],
            "assessments": [a.model_dump(mode="json") for a in db.list_("assessments", Assessment, lead_id=lead.id)],
            "tasks": [t.model_dump(mode="json") for t in db.list_("tasks", Task, lead_id=lead.id)],
        })
    return out


def anonymise(lead: Lead, why: str) -> None:
    """Replace personal data on the lead and everything hanging off it; keep business facts and the opt-out (as a hash)."""
    old_email = lead.normalizedEmail
    digest = store.subject_hash(old_email)
    erased_email = f"erased-{digest[:10]}@erased.invalid"
    names = [n for n in (lead.input.fullName, old_email, lead.input.phone) if n]

    def scrub(text: str) -> str:
        for n in names:
            text = text.replace(n, ERASED)
        return text

    lead.input = lead.input.model_copy(update={"fullName": "Erased contact", "email": erased_email, "phone": None,
                                               "inquiry": "", "role": None})
    lead.normalizedEmail = erased_email
    lead.sources = []
    lead.flags = list(dict.fromkeys(lead.flags + ["anonymised"]))
    lead.history = [{"at": h.get("at"), "type": h.get("type"), "detail": scrub(str(h.get("detail", "")))} for h in lead.history]
    lead.history.append({"at": now_iso(), "type": "anonymised", "detail": why})
    lead.updatedAt = now_iso()
    db.put("leads", lead)
    for m in db.list_("messages", Message, lead_id=lead.id):
        db.put("messages", m.model_copy(update={"body": ERASED, "intendedTo": erased_email, "deliveredTo": None}))
    for d in db.list_("drafts", Draft, lead_id=lead.id):
        db.put("drafts", d.model_copy(update={"to": erased_email, "body": ERASED}))
    for a in db.list_("assessments", Assessment, lead_id=lead.id):
        for f in a.bant.values():
            for ev in f.evidence:
                ev.excerpt = ERASED if ev.excerpt else ev.excerpt
        a.droppedQuotes = [{**q, "excerpt": ERASED} for q in a.droppedQuotes]
        a.injectionExcerpt = ERASED if a.injectionExcerpt else None
        db.put("assessments", a)
    for t in db.list_("tasks", Task, lead_id=lead.id):
        if t.card:
            t.card.contact = {"name": "Erased contact"}
            t.card.summary = scrub(t.card.summary)
        t.title = scrub(t.title)
        db.put("tasks", t)
    for r in db.list_("runs", Run, lead_id=lead.id):
        r.steps = [{**s, "detail": scrub(str(s.get("detail", "")))} for s in r.steps]
        db.put("runs", r)
    for e in db.list_("crm_events", CrmEvent, lead_id=lead.id):
        db.put("crm_events", e.model_copy(update={"payload": {"erased": True}}))
    _scrub_mock_crm(lead.id)
    # The opt-out must keep working after erasure: swap the plaintext entry for its hash
    rows = [r for r in db.list_("suppressions", _Row, limit=100000) if str(getattr(r, "email", "")).lower() == old_email]
    for r in rows:
        db.delete("suppressions", r.id)
    if rows or lead.suppressed:
        from app.pipeline import Suppression
        first = rows[0] if rows else None  # keep the original opt-out time so "nothing sent afterwards" stays measured from it
        db.put("suppressions", Suppression(id=f"sha256:{digest}", leadId=lead.id, email=f"sha256:{digest}",
                                           reason=f"{getattr(first, 'reason', 'opt-out')} (erased; kept as hash)",
                                           at=getattr(first, "at", None) or now_iso()))
    from app.crm import outbox
    outbox.emit("lead.erased", lead.id, {"reason": why})


def _scrub_mock_crm(lead_id: str) -> None:
    conn = sqlstore.connect(config.DB_PATH, timeout=10)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS crm_received (id TEXT PRIMARY KEY, lead_id TEXT, envelope TEXT NOT NULL, entry TEXT NOT NULL)")
        conn.execute("UPDATE crm_received SET envelope = ?, entry = ? WHERE lead_id = ?",
                     (json.dumps({"erased": True}), json.dumps({"leadId": lead_id, "erased": True}), lead_id))
        conn.commit()
    finally:
        conn.close()


class _Row(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str


def erase(email: str, why: str) -> int:
    leads = leads_for(email)
    for lead in leads:
        anonymise(lead, why)
    return len(leads)


# ---------------------------------------------------------------- retention

def retention_days() -> int:
    return int(store.setting("retention_days", DEFAULT_RETENTION_DAYS))


def retention_state() -> dict[str, Any]:
    last = store.setting("retention_last_run")
    nxt = (datetime.fromisoformat(last) + timedelta(days=1)).isoformat() if last else None
    return {"days": retention_days(), "lastRun": last, "nextRun": nxt}


def run_retention_if_due(force: bool = False) -> int:
    """Daily: anonymise leads with no activity for longer than the retention period. Runs lazily when the report is read."""
    state = retention_state()
    now = datetime.now(timezone.utc)
    if not force and state["nextRun"] and datetime.fromisoformat(state["nextRun"]) > now:
        return 0
    cutoff = (now - timedelta(days=state["days"])).isoformat()
    stale = [l for l in db.list_("leads", Lead, limit=100000) if "anonymised" not in l.flags and (l.updatedAt or "") < cutoff]
    for lead in stale:
        anonymise(lead, f"retention: no activity for {state['days']} days")
    store.set_setting("retention_last_run", now.isoformat())
    if stale or force:
        store.audit("system", "system", "retention.run", "retention", None, after={"anonymised": len(stale), "days": state["days"]})
    return len(stale)
