# app/governance/report.py - governanceReport: every trust figure computed on the server from the same store as the workspace
from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app import db
from app.contracts import Assessment, Campaign, Draft, Enrollment, Lead, Message, Run
from app.engagement import clock
from app.engagement.transport import GLOBAL_DAILY_CAP, PER_CONTACT_DAILY_CAP
from app.governance import gates, guardrails, rights, store
from app.llm.calllog import AiCall

# Audit actions only a Portal Admin may read (users, roles, ICP, reset, sign-in, data rights, retention)
ADMIN_ONLY = ("auth.", "user.", "role.", "icp.", "demo.reset", "data_request.", "retention.")
AUTO_ROUTES = {"rep_review", "nurture", "suppressed", "closed", "existing_owner"}
NOT_BUILT = {"status": "not_built"}


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def window(days: int) -> tuple[str, str, str]:
    """[start, end) for the period and the start of the previous period; end covers the demo clock when it runs ahead."""
    end = max(datetime.now(timezone.utc), clock.demo_now()) + timedelta(minutes=1)
    start = end - timedelta(days=days)
    return _iso(start), _iso(end), _iso(start - timedelta(days=days))


def _in(ts: Optional[str], start: str, end: str) -> bool:
    return bool(ts) and start <= ts < end


def visible_audit(rows: list[dict[str, Any]], user: dict[str, Any]) -> list[dict[str, Any]]:
    if "viewFullAudit" in user["permissions"]:
        return rows
    if "viewGovernance" in user["permissions"]:
        return [r for r in rows if not r["action"].startswith(ADMIN_ONLY)]
    return [r for r in rows if r["actor"] == user["id"]]


class Data:
    """One snapshot of the store, scoped to a campaign when asked."""

    def __init__(self, campaign: Optional[str] = None, channel: Optional[str] = None):
        self.campaigns = db.list_("campaigns", Campaign, limit=100000)
        self.enrollments = db.list_("enrollments", Enrollment, limit=100000)
        leads = db.list_("leads", Lead, limit=100000)
        if campaign:
            ids = {e.leadId for e in self.enrollments if e.campaignId == campaign}
            leads = [l for l in leads if l.id in ids]
        self.leads = {l.id: l for l in leads}
        keep = lambda x: x.leadId in self.leads  # noqa: E731
        self.drafts = [d for d in db.list_("drafts", Draft, limit=100000) if keep(d)]
        self.draft_by_id = {d.id: d for d in self.drafts}
        self.messages = [m for m in db.list_("messages", Message, limit=100000) if keep(m)]
        if channel:  # email, sms, whatsapp or linkedin
            self.messages = [m for m in self.messages if m.direction == "inbound" or gates.channel_of(self.draft_by_id.get(m.draftId), m.subject) == channel]
            self.drafts = [d for d in self.drafts if gates.channel_of(d) == channel or (channel == "whatsapp" and d.kind == "sms_message")]
            self.draft_by_id = {d.id: d for d in self.drafts}
        self.assessments = sorted((a for a in db.list_("assessments", Assessment, limit=100000) if keep(a)), key=lambda a: a.createdAt)
        self.runs = [r for r in db.list_("runs", Run, limit=100000) if r.leadId in self.leads]
        self.suppressions = [s for s in db.list_("suppressions", rights._Row, limit=100000)]
        self.calls = db.list_("ai_calls", AiCall, limit=100000)
        guardrails.sync_wording_flags()
        self.flags = [f for f in store.flags() if not f["lead_id"] or f["lead_id"] in self.leads or not campaign]
        self.audit = store.audit_rows()
        self.requests = store.requests()


# ---------------------------------------------------------------- headline tiles

def _ratio(num: int, den: int) -> dict[str, Any]:
    return {"num": num, "den": den, "pct": round(100 * num / den, 1) if den else None}


def _approved_sends(d: Data, start: str, end: str) -> dict[str, Any]:
    sent = [m for m in d.messages if m.direction == "outbound" and _in(m.at, start, end)]
    ok = [m for m in sent if (dr := d.draft_by_id.get(m.draftId)) and dr.approvedBy]
    return _ratio(len(ok), len(sent))


def _quotes(d: Data, start: str, end: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    verified: dict[str, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []
    for a in d.assessments:
        if not a.replyIntent or not _in(a.createdAt, start, end):
            continue
        for key, f in a.bant.items():
            for ev in f.evidence:
                if ev.origin == "buyer_statement" and ev.excerpt and ev.id not in verified:
                    verified[ev.id] = {"leadId": a.leadId, "field": key, "excerpt": ev.excerpt, "status": f.status, "at": a.createdAt}
        dropped += [{"leadId": a.leadId, **q, "at": a.createdAt} for q in a.droppedQuotes]
    return list(verified.values()), dropped


def _opt_outs(d: Data, start: str, end: str) -> list[dict[str, Any]]:
    """Each opt-out with the sends recorded after it (audit time, the same clock as the opt-out)."""
    out = []
    by_email = {l.normalizedEmail: l for l in d.leads.values()}
    for s in d.suppressions:
        at, email = getattr(s, "at", None), str(getattr(s, "email", ""))
        lead = d.leads.get(getattr(s, "leadId", "")) or by_email.get(email)
        if not lead or not _in(at, start, end):
            continue
        after = [r for r in d.audit if r["lead_id"] == lead.id and r["at"] > at]
        out.append({"leadId": lead.id, "name": lead.input.fullName, "reason": getattr(s, "reason", ""), "at": at,
                    "sentAfter": sum(1 for r in after if r["action"] == "send.sent"),
                    "blockedAfter": sum(1 for r in after if r["action"] == "send.blocked")})
    return out


def tiles(d: Data, user: dict[str, Any], days: int) -> list[dict[str, Any]]:
    start, end, prev = window(days)
    open_flags = [f for f in d.flags if f["status"] == "open"]
    audit_now = visible_audit([r for r in d.audit if _in(r["at"], start, end)], user)
    audit_prev = visible_audit([r for r in d.audit if _in(r["at"], prev, start)], user)
    q_now, dq_now = _quotes(d, start, end)
    q_prev, dq_prev = _quotes(d, prev, start)
    oo_now, oo_prev = _opt_outs(d, start, end), _opt_outs(d, prev, start)
    honoured = lambda oo: _ratio(sum(1 for o in oo if o["sentAfter"] == 0), len(oo))  # noqa: E731
    return [
        {"id": "approved_sends", "label": "Human-approved sends", "kind": "ratio", "target": 100, "drill": "sends",
         "now": _approved_sends(d, start, end), "prev": _approved_sends(d, prev, start)},
        {"id": "evidence_bant", "label": "Evidence-backed BANT", "kind": "ratio", "drill": "quotes",
         "now": _ratio(len(q_now), len(q_now) + len(dq_now)), "prev": _ratio(len(q_prev), len(q_prev) + len(dq_prev))},
        {"id": "opt_outs", "label": "Opt-outs honoured", "kind": "ratio", "target": 100, "drill": "optouts",
         "now": honoured(oo_now), "prev": honoured(oo_prev)},
        {"id": "open_flags", "label": "Open guardrail flags", "kind": "count", "drill": "flags", "goodDown": True,
         "now": {"num": len(open_flags)}, "prev": None},
        {"id": "audit_events", "label": "Audit events", "kind": "count", "drill": "audit",
         "now": {"num": len(audit_now)}, "prev": {"num": len(audit_prev)}},
        {"id": "open_requests", "label": "Open data requests", "kind": "count", "drill": "requests", "goodDown": True,
         "now": {"num": sum(1 for r in d.requests if not r["completed_at"])}, "prev": None},
    ]


# ---------------------------------------------------------------- panels

def oversight(d: Data, start: str, end: str) -> dict[str, Any]:
    drafts = [x for x in d.drafts if _in(x.createdAt, start, end)]
    approved = [x for x in drafts if x.approvedBy]
    waits = []
    for x in approved:
        try:
            waits.append((datetime.fromisoformat(x.approvedAt) - datetime.fromisoformat(x.createdAt)).total_seconds() / 60)
        except (TypeError, ValueError):
            pass
    replies = [a for a in d.assessments if a.replyIntent and _in(a.createdAt, start, end)]
    engines = Counter(("AI writer · " + (x.model or "model")) if x.mode == "live" else "Template (rules)" for x in drafts)
    return {
        "approved": len(approved), "rejected": sum(1 for x in drafts if x.status == "REJECTED"),
        "editedBeforeApproval": sum(1 for x in approved if x.version > 1),
        "awaiting": sum(1 for x in drafts if x.status in ("DRAFT", "BLOCKED")),
        "medianMinutesToApproval": round(statistics.median(waits), 1) if waits else None,
        "repliesByAi": sum(1 for a in replies if a.route in AUTO_ROUTES), "repliesByPerson": sum(1 for a in replies if a.route not in AUTO_ROUTES),
        "draftsByEngine": dict(engines), "drafted": len(drafts),
    }


def evidence(d: Data, start: str, end: str) -> dict[str, Any]:
    verified, dropped = _quotes(d, start, end)
    downgraded: set[str] = set()
    last: dict[str, Assessment] = {}
    for a in d.assessments:
        prev = last.get(a.leadId)
        if prev and _in(a.createdAt, start, end) and any(
                prev.bant.get(k) and prev.bant[k].status == "confirmed" and a.bant.get(k) and a.bant[k].status != "confirmed" for k in a.bant):
            downgraded.add(a.leadId)
        last[a.leadId] = a
    mix: Counter = Counter()
    for a in last.values():
        mix.update(f.status for f in a.bant.values())
    return {"verified": len(verified), "dropped": len(dropped), "droppedQuotes": dropped[:20], "downgradedLeads": len(downgraded),
            "bantMix": {k: mix.get(k, 0) for k in ("confirmed", "inferred", "missing", "conflict")}}


def guardrail_panel(d: Data, start: str, end: str) -> dict[str, Any]:
    flags = [f for f in d.flags if _in(f["created_at"], start, end)]
    by_reason: dict[str, dict[str, int]] = {}
    for f in flags:
        if f["kind"] == "wording":
            row = by_reason.setdefault(f["reason"], {"open": 0, "cleared": 0, "confirmed": 0, "superseded": 0})
            row[f["status"]] = row.get(f["status"], 0) + 1
    injections = [{"id": f["id"], "leadId": f["lead_id"], "excerpt": f["excerpt"], "status": f["status"], "at": f["created_at"]}
                  for f in flags if f["kind"] == "injection"]
    calls = [c for c in d.calls if _in(c.at, start, end)]
    steps: dict[str, dict[str, Any]] = {}
    for c in calls:
        s = steps.setdefault(c.step or c.purpose, {"calls": 0, "fields": set()})
        s["calls"] += 1
        s["fields"].update(c.fields)
    phone = sum(1 for c in calls if any("phone" in f.lower() for f in c.fields))
    return {"wording": {k: {**v, "label": guardrails.WORDING.get(k, k)} for k, v in sorted(by_reason.items())},
            "injections": injections, "promptLog": {k: {"calls": v["calls"], "fields": sorted(v["fields"])} for k, v in steps.items()},
            "modelCalls": len(calls), "callsWithPhone": phone}


def _suppression_reason(reason: str, value: str) -> str:
    r = reason.lower()
    if value.startswith("@"):
        return "Blocked domain"
    if "erase" in r:
        return "Erased (hash)"
    if "bounce" in r:
        return "Bounce"
    if "manual" in r:
        return "Manual"
    if "stop" in r and "whatsapp" in r:
        return "WhatsApp STOP"
    return "Opt-out"


def consent(d: Data, start: str, end: str) -> dict[str, Any]:
    by_reason = Counter(_suppression_reason(str(getattr(s, "reason", "")), str(getattr(s, "email", ""))) for s in d.suppressions)
    blocked = [r for r in d.audit if r["action"] == "send.blocked" and r["detail"] == "contact_blocked" and _in(r["at"], start, end)]
    held = [l for l in d.leads.values() if l.input.contactPermission != "allowed" or any(
        s.get("name") == "consent" and str(s.get("detail", "")).startswith("blocked") for r in d.runs if r.leadId == l.id for s in r.steps)]
    channels: dict[str, dict[str, int]] = {}
    for l in d.leads.values():
        ch = "Lead form" if l.input.source == "website" else "Email (imported / campaign)"
        row = channels.setdefault(ch, {"granted": 0, "notGranted": 0, "withdrawn": 0})
        row["granted" if l.input.contactPermission == "allowed" else "notGranted"] += 1
        if l.suppressed:
            row["withdrawn"] += 1
    return {"suppressionSize": len(d.suppressions), "byReason": dict(by_reason), "sendsBlocked": len(blocked),
            "noConsentHeld": len(held), "byChannel": channels,
            "whatsapp": {"status": "test_only"}, "adsLeadForms": NOT_BUILT}


def sending(d: Data, days: int) -> dict[str, Any]:
    today = clock.demo_date(clock.demo_now())
    sent_today = [m for m in d.messages if m.direction == "outbound" and clock.demo_date(m.at) == today]
    paused = store.paused_campaigns()
    enr = {e.id: e for e in d.enrollments}
    rows = []
    for c in d.campaigns:
        mine = [enr[i] for i in c.enrollmentIds if i in enr]
        ids = {e.id for e in mine}
        rows.append({"id": c.id, "name": c.name, "status": "paused" if c.id in paused else c.status.lower(),
                     "enrolled": len(mine), "sentToday": sum(1 for m in sent_today if m.enrollmentId in ids),
                     "blockedPaused": sum(1 for r in d.audit if r["action"] == "send.blocked" and r["detail"] == "campaign_paused"
                                          and (r["after"] or {}).get("enrollmentId") in ids),
                     "prospectSource": "Simulated prospect directory"})
    start, end, _ = window(days)
    by_channel = Counter(gates.channel_of(d.draft_by_id.get(m.draftId), m.subject) for m in d.messages if m.direction == "outbound" and _in(m.at, start, end))
    return {"sentToday": len(sent_today), "dailyCap": GLOBAL_DAILY_CAP, "perContactCap": PER_CONTACT_DAILY_CAP,
            "sentByChannel": {c: by_channel.get(c, 0) for c in ("email", "sms", "whatsapp", "linkedin")},
            "capBlocks": sum(1 for r in d.audit if r["action"] == "send.blocked" and r["detail"] == "send_cap"),
            "campaigns": rows, "businessHours": NOT_BUILT, "ads": NOT_BUILT}


def data_rights(d: Data, start: str, end: str) -> dict[str, Any]:
    reqs = [r for r in d.requests if _in(r["requested_at"], start, end)]
    done = [r for r in reqs if r["completed_at"]]
    days_ = [(datetime.fromisoformat(r["completed_at"]) - datetime.fromisoformat(r["requested_at"])).total_seconds() / 86400 for r in done]
    return {"open": len(reqs) - len(done), "completed": len(done), "avgDaysToComplete": round(sum(days_) / len(days_), 2) if days_ else None,
            "byKind": dict(Counter(r["kind"] for r in reqs)), "anonymisedLeads": sum(1 for l in d.leads.values() if "anonymised" in l.flags),
            "retention": rights.retention_state()}


def governance_report(user: dict[str, Any], days: int = 14, campaign: Optional[str] = None, channel: Optional[str] = None) -> dict[str, Any]:
    rights.run_retention_if_due()
    d = Data(campaign, channel)
    start, end, _ = window(days)
    from app.governance.controls import register

    panels = {"oversight": oversight(d, start, end), "evidence": evidence(d, start, end), "guardrails": guardrail_panel(d, start, end),
              "consent": consent(d, start, end), "sending": sending(d, days), "dataRights": data_rights(d, start, end),
              "audit": visible_audit([r for r in d.audit if _in(r["at"], start, end)], user)[:20]}
    report = {"period": {"days": days, "start": start, "end": end, "campaign": campaign, "channel": channel}, "tiles": tiles(d, user, days), "panels": panels,
              "campaigns": [{"id": c.id, "name": c.name} for c in d.campaigns], "generatedAt": datetime.now(timezone.utc).isoformat()}
    report["register"] = register(report)
    return report


def my_summary(user: dict[str, Any], days: int = 14) -> dict[str, Any]:
    """An SDR's own view: flagged drafts and opt-outs on their leads, and their own audit entries."""
    d = Data()
    start, end, _ = window(days)
    mine = {lid for lid, l in d.leads.items() if l.ownerId == user["id"]}
    flagged = [{"draftId": f["draft_id"], "leadId": f["lead_id"], "lead": d.leads[f["lead_id"]].input.fullName, "reason": guardrails.WORDING.get(f["reason"], f["reason"]),
                "excerpt": f["excerpt"], "status": f["status"]}
               for f in d.flags if f["lead_id"] in mine and f["kind"] == "wording" and f["status"] == "open"]
    return {"period": {"days": days, "start": start, "end": end}, "flaggedDrafts": flagged,
            "optOuts": [o for o in _opt_outs(d, start, end) if o["leadId"] in mine],
            "myAudit": visible_audit([r for r in d.audit if _in(r["at"], start, end)], user)[:50]}


# ---------------------------------------------------------------- drill-down records

def records(kind: str, user: dict[str, Any], days: int = 14, campaign: Optional[str] = None, q: str = "",
            channel: Optional[str] = None) -> dict[str, Any]:
    d = Data(campaign, channel)
    start, end, _ = window(days)
    name = lambda lid: d.leads[lid].input.fullName if lid in d.leads else lid  # noqa: E731
    rows: list[dict[str, Any]]
    if kind == "sends":
        rows = [{"at": m.at, "leadId": m.leadId, "lead": name(m.leadId), "channel": gates.channel_of(d.draft_by_id.get(m.draftId), m.subject), "subject": m.subject, "approvedBy": (dr.approvedBy if (dr := d.draft_by_id.get(m.draftId)) else None),
                 "approvedAt": dr.approvedAt if dr else None, "transport": m.transport}
                for m in d.messages if m.direction == "outbound" and _in(m.at, start, end)]
    elif kind == "approvals":
        rows = [{"at": x.approvedAt or x.createdAt, "leadId": x.leadId, "lead": name(x.leadId), "draftId": x.id, "subject": x.subject, "status": x.status,
                 "version": x.version, "approvedBy": x.approvedBy, "engine": "AI" if x.mode == "live" else "Template"}
                for x in d.drafts if _in(x.createdAt, start, end)]
    elif kind == "quotes":
        verified, dropped = _quotes(d, start, end)
        rows = [{**v, "lead": name(v["leadId"]), "result": "verified"} for v in verified] + \
               [{**x, "lead": name(x["leadId"]), "result": "dropped"} for x in dropped]
    elif kind == "optouts":
        rows = _opt_outs(d, start, end)
    elif kind == "flags":
        rows = []
        for f in d.flags:
            if not _in(f["created_at"], start, end) and f["status"] != "open":
                continue
            draft = d.draft_by_id.get(f["draft_id"]) if f["draft_id"] else None
            msg = next((m for m in d.messages if m.id == f["message_id"]), None) if f["message_id"] else None
            rows.append({**f, "label": guardrails.WORDING.get(f["reason"], "Hidden instruction"), "lead": name(f["lead_id"]),
                         "subject": draft.subject if draft else (msg.subject if msg else None),
                         "text": draft.body if draft else (msg.body if msg else f["excerpt"]),
                         "draftStatus": draft.status if draft else None, "highlights": guardrails.highlights(f, draft)})
    elif kind == "audit":
        rows = visible_audit([r for r in d.audit if _in(r["at"], start, end)], user)
    elif kind == "requests":
        if "handleDataRequests" not in user["permissions"]:
            return {"kind": kind, "rows": [], "forbidden": True}
        rows = d.requests
    elif kind == "suppressions":
        rows = [{"value": str(getattr(s, "email", s.id)), "reason": str(getattr(s, "reason", "")), "category": _suppression_reason(str(getattr(s, "reason", "")), str(getattr(s, "email", ""))),
                 "leadId": getattr(s, "leadId", None), "at": getattr(s, "at", None), "id": s.id} for s in d.suppressions]
    elif kind == "blocked":
        rows = [r for r in d.audit if r["action"] == "send.blocked" and _in(r["at"], start, end)]
    elif kind == "prompts":
        rows = [{"at": c.at, "step": c.step or c.purpose, "provider": c.provider, "model": c.model, "fields": c.fields, "ok": c.ok}
                for c in d.calls if _in(c.at, start, end)]
    else:
        raise ValueError("unknown_kind")
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in str(r).lower()]
    return {"kind": kind, "period": {"days": days, "start": start, "end": end}, "rows": rows}
