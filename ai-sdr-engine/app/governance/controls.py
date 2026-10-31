# app/governance/controls.py - The controls register: one typed list read by the page, the evidence pack and Ask AI.
# Status is set here in code, never computed: Live only when the control's check passes in the demo.
# PROVISIONAL: built from the control ids the governance spec references; replace with the agreed main-tab list.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

Area = Literal["Security", "Compliance", "Trust", "Paid ads"]
Priority = Literal["Must", "Should", "Later"]
Status = Literal["Live", "Partial", "Not built"]
SOURCE = "provisional"


@dataclass(frozen=True)
class Control:
    id: str
    name: str
    area: Area
    priority: Priority
    status: Status
    evidence: Optional[Callable[[dict[str, Any]], tuple[Optional[str], Optional[str]]]] = None  # (text, drill kind); text None = no activity
    simulated: bool = False
    note: str = ""


def _t(r: dict[str, Any], tid: str) -> dict[str, Any]:
    return next(t for t in r["tiles"] if t["id"] == tid)


def _ratio_text(r: dict[str, Any], tid: str, noun: str) -> Optional[str]:
    n = _t(r, tid)["now"]
    return f"{n['num']} of {n['den']} {noun} ({n['pct']}%)" if n.get("den") else None


def _p(r: dict[str, Any], *path: str) -> Any:
    v: Any = r["panels"]
    for k in path:
        v = v[k]
    return v


CONTROLS: list[Control] = [
    Control("S1", "Append-only audit log", "Security", "Must", "Live",
            lambda r: (f"{_t(r, 'audit_events')['now']['num']} entries in period · edits and deletes refused by the database", "audit")),
    Control("S5", "Model prompts carry field names only, no phone numbers", "Security", "Must", "Live",
            lambda r: ((f"{_p(r, 'guardrails', 'modelCalls')} model calls · {_p(r, 'guardrails', 'callsWithPhone')} with a phone field", "prompts")
                       if _p(r, "guardrails", "modelCalls") else (None, "prompts"))),
    Control("T1", "Wording check before approval (pricing, guarantees, regulator claims, links, opt-out line)", "Trust", "Must", "Live",
            lambda r: ((f"{sum(sum(v[s] for s in ('open', 'cleared', 'confirmed', 'superseded')) for v in _p(r, 'guardrails', 'wording').values())} flags · "
                        f"{_t(r, 'open_flags')['now']['num']} open", "flags") if _p(r, "guardrails", "wording") else (None, "flags"))),
    Control("T2", "Hidden-instruction guard on replies", "Trust", "Must", "Live",
            lambda r: ((f"{len(_p(r, 'guardrails', 'injections'))} attempts blocked · score unchanged", "flags")
                       if _p(r, "guardrails", "injections") else (None, "flags"))),
    Control("T3", "BANT quotes verified word for word", "Trust", "Must", "Live",
            lambda r: (_ratio_text(r, "evidence_bant", "quotes verified"), "quotes")),
    Control("T4", "A person approves every send", "Trust", "Must", "Live",
            lambda r: (_ratio_text(r, "approved_sends", "sends approved"), "sends")),
    Control("C1", "Global suppression list checked at every send", "Compliance", "Must", "Live",
            lambda r: (f"{_p(r, 'consent', 'suppressionSize')} entries · {_p(r, 'consent', 'sendsBlocked')} sends blocked in period", "suppressions")),
    Control("C2", "Daily send caps", "Compliance", "Must", "Live",
            lambda r: (f"{_p(r, 'sending', 'sentToday')} of {_p(r, 'sending', 'dailyCap')} sent today · {_p(r, 'sending', 'capBlocks')} held by the cap", "blocked")),
    Control("C3", "No outreach without consent (GDPR hold for EU contacts)", "Compliance", "Must", "Partial",
            lambda r: (f"{_p(r, 'consent', 'noConsentHeld')} leads held to no outreach", "suppressions"),
            note="Engine now treats an 'unknown' permission as allowed, so the EU hold only applies to explicit denials."),
    Control("C4", "Pause a campaign; paused campaigns cannot send", "Compliance", "Should", "Live",
            lambda r: (f"{sum(1 for c in _p(r, 'sending', 'campaigns') if c['status'] == 'paused')} paused of {len(_p(r, 'sending', 'campaigns'))} campaigns", "blocked")
            if _p(r, "sending", "campaigns") else (None, "blocked")),
    Control("C5", "Consent recorded per channel", "Compliance", "Should", "Partial",
            lambda r: (f"{sum(v['granted'] for v in _p(r, 'consent', 'byChannel').values())} granted · "
                       f"{sum(v['withdrawn'] for v in _p(r, 'consent', 'byChannel').values())} withdrawn", "optouts")
            if _p(r, "consent", "byChannel") else (None, "optouts"),
            note="Email and the website form record permission; SMS/WhatsApp and LinkedIn send only to controlled test recipients; ad lead forms are not built."),
    Control("C6", "Export and erase requests", "Compliance", "Must", "Live",
            lambda r: (f"{_p(r, 'dataRights', 'completed')} completed · {_p(r, 'dataRights', 'open')} open", "requests")
            if _p(r, "dataRights", "completed") or _p(r, "dataRights", "open") else (None, "requests")),
    Control("C7", "Retention: anonymise inactive leads", "Compliance", "Should", "Live",
            lambda r: (f"{_p(r, 'dataRights', 'retention', 'days')}-day period · {_p(r, 'dataRights', 'anonymisedLeads')} leads anonymised", "requests")),
    Control("A1", "Ad audience sync excludes opted-out people", "Paid ads", "Must", "Not built", simulated=True),
    Control("A2", "Ad spend held to budget", "Paid ads", "Should", "Not built", simulated=True),
    Control("A3", "Consent captured on ad lead forms", "Paid ads", "Must", "Not built", simulated=True),
    Control("A4", "Wording check on ad copy", "Paid ads", "Should", "Not built", simulated=True),
    Control("A5", "WhatsApp STOP honoured", "Paid ads", "Must", "Not built", simulated=True,
            note="WhatsApp sends exist (controlled test number only); STOP replies are not read yet."),
    Control("A6", "Audience list type recorded (company or contact)", "Paid ads", "Later", "Not built", simulated=True),
    Control("B1", "Approval bound to the exact text (edits invalidate it)", "Trust", "Must", "Live"),
    Control("B2", "Any reply stops the sequence before analysis", "Trust", "Must", "Live"),
    Control("B3", "No duplicate outreach to the same contact", "Compliance", "Must", "Live"),
    Control("B4", "Identity conflicts quarantined for review", "Security", "Should", "Live"),
    Control("B5", "No pricing in outbound email", "Trust", "Must", "Live"),
    Control("B6", "Only approved product claims", "Trust", "Must", "Live"),
    Control("B7", "Signed, idempotent CRM webhooks", "Security", "Must", "Live"),
    Control("B8", "Synthetic domains never sent to live enrichment", "Security", "Should", "Live", simulated=True),
    Control("B9", "Real email only to the controlled test inbox", "Compliance", "Must", "Live"),
    Control("B10", "Prospect replies treated as untrusted data in prompts", "Security", "Must", "Live"),
]


def register(report: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for c in CONTROLS:
        text, drill = (c.evidence(report) if c.evidence else (None, None))
        if c.status == "Not built":
            text = "Not built"
        elif text is None:
            text = "No activity in period" if c.evidence else "In place (no live counter)"
        rows.append({"id": c.id, "name": c.name, "area": c.area, "priority": c.priority, "status": c.status, "evidence": text,
                     "drill": drill, "simulated": c.simulated, "note": c.note})
    return {"source": SOURCE, "rows": rows}
