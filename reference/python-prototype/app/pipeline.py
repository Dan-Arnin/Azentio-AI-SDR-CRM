"""Lead state machine shared by inbound and outbound motions.

captured -> enriched -> icp_checked -> (email queued | pending_approval) -> awaiting_reply -> replied
         -> analysed -> qualified | awaiting_reply (gap follow-up) | sdr_followup | suppressed | nurture

Every transition writes an audit event. Decisions are deterministic functions of the analysis, so the
same reply always routes the same way and the rule that fired is shown to the user.
"""
import csv
import io
import json
import re

from . import analyzer, db, emails, hygiene
from .config import ae_for, cfg, sdr
from .enrichment import crm_check, enrich
from .icp import infer_product, score

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
SOURCES = {"website_form", "marketing_campaign", "ad_form", "webinar", "event", "outbound"}
TRUTHY = ("yes", "true", "1", "y")
RANK = {"missing": 0, "inferred": 1, "confirmed": 2}


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _set_state(lead_id: str, state: str, **extra) -> None:
    db.update("leads", lead_id, {"state": state, "updated_at": db.now(), **extra})
    db.log_event(lead_id, "state", {"state": state, **{k: v for k, v in extra.items() if k in ("outcome",)}})


def lead(lead_id: str) -> dict:
    row = db.get("leads", lead_id)
    if not row:
        raise KeyError(lead_id)
    return row


# ---------- capture ----------

def validate_capture(p: dict) -> list[str]:
    errors = []
    if p.get("source") not in SOURCES:
        errors.append(f"source must be one of {sorted(SOURCES)}")
    if not p.get("email") or not EMAIL_RE.match(str(p["email"]).strip()):
        errors.append(f"invalid or missing email: {p.get('email')!r}")
    if p.get("product") and p["product"] not in cfg()["products"]:
        errors.append(f"unknown product {p['product']!r}")
    return errors


def capture(p: dict) -> dict:
    p = {k: (v.strip() if isinstance(v, str) else v) for k, v in p.items()}
    errors = validate_capture(p)
    if errors:
        raise ValidationError(errors)
    p["email"] = p["email"].lower()
    product = p.get("product") or infer_product(" ".join(str(p.get(k, "")) for k in ("message", "webinar", "event_name", "asset", "campaign")))
    lid = db.new_id("lead")
    db.insert("leads", {"id": lid, "email": p["email"], "source": p["source"], "product": product, "state": "captured",
                        "capture": p, "created_at": db.now(), "updated_at": db.now(), "followups": 0})
    db.log_event(lid, "captured", {"source": p["source"], "product": product, "product_inferred": not p.get("product")})
    return lead(lid)


def parse_rows(text: str) -> list[dict]:
    """CSV or JSON (a list of objects) -> list of row dicts."""
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        data = json.loads(stripped)
        if isinstance(data, dict):
            data = data.get("leads", [data])
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ValidationError(["JSON must be a list of lead objects"])
        return data
    return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(io.StringIO(text))]


def _flag(v) -> bool | None:
    if isinstance(v, bool) or v is None:
        return v
    s = str(v).strip().lower()
    return None if s == "" else s in TRUTHY


def ingest_rows(rows: list[dict], defaults: dict) -> dict:
    """Row-level results: every row is either created or rejected with a reason; nothing is silently dropped."""
    created, rejected = [], []
    for i, row in enumerate(rows, start=1):
        payload = {**{k: v for k, v in defaults.items() if v}, **{k: v for k, v in row.items() if v not in ("", None)}}
        for flag in ("consent", "attended"):
            if flag in payload:
                payload[flag] = _flag(payload[flag])
        try:
            l = capture(payload)
            process(l["id"])
            created.append({"row": i, "email": l["email"], "lead_id": l["id"], "source": l["source"]})
        except ValidationError as e:
            rejected.append({"row": i, "email": row.get("email"), "errors": e.errors})
    return {"created": created, "rejected": rejected}


def ingest_webinar_csv(text: str, webinar: str, product: str) -> dict:
    return ingest_rows(parse_rows(text), {"source": "webinar", "webinar": webinar, "product": product})


# ---------- enrich, ICP, route ----------

def process(lead_id: str) -> dict:
    l = lead(lead_id)
    cap = l["capture"]
    enr = enrich(cap)
    person, company = enr["person"], enr["company"]
    db.update("leads", lead_id, {"person": person, "company": company,
                                 "enrichment": {k: enr[k] for k in ("status", "source", "notes", "missing", "uncertain")}})
    db.log_event(lead_id, "enriched", {"status": enr["status"], "source": enr["source"], "notes": enr["notes"]})
    _set_state(lead_id, "enriched")

    crm = crm_check(l["email"], company.get("domain") or "", company.get("name"))
    others = [x for x in crm["existing_leads"] if x["id"] != lead_id]
    crm["duplicate_leads"] = others
    db.update("leads", lead_id, {"crm": crm})
    db.log_event(lead_id, "crm_checked", {"contact": bool(crm["existing_contact"]), "open_opportunities": len(crm["open_opportunities"]),
                                          "duplicates": len(others)})

    if not l["product"]:
        return _route_sdr(lead_id, "Product of interest could not be determined from the lead; SDR to review.", kind="needs_review")

    context = " ".join(str(cap.get(k, "")) for k in ("message", "webinar", "event_name", "asset", "campaign"))
    icp = score(l["product"], person, company, context)
    db.update("leads", lead_id, {"icp": icp})
    db.log_event(lead_id, "icp_checked", {"score": icp["score"], "verdict": icp["verdict"]})
    _set_state(lead_id, "icp_checked")

    if crm["open_opportunities"]:
        opp = crm["open_opportunities"][0]
        return _alert_owner_existing(lead_id, opp)
    if others and any(o["state"] not in ("suppressed", "nurture", "closed") for o in others):
        return _route_sdr(lead_id, f"Duplicate of active lead {others[0]['id']}; no new outreach sent.", kind="duplicate")

    hyg = hygiene.check(l["email"], company.get("region"), cap.get("consent"))
    db.update("leads", lead_id, {"hygiene": hyg})
    if not hyg["ok"]:
        _set_state(lead_id, "blocked", outcome={"route": "blocked", "reason": " ".join(hyg["blocks"]), "at": db.now()})
        db.log_event(lead_id, "outreach_blocked", {"blocks": hyg["blocks"]})
        return lead(lead_id)

    if l["source"] == "webinar" and cap.get("attended") is False:
        return _nurture(lead_id, "Registered for the webinar but did not attend; added to low-touch nurture.")
    if icp["verdict"] == "low_fit":
        return _nurture(lead_id, icp["reason"])
    if icp["verdict"] == "insufficient_data":
        return _route_sdr(lead_id, icp["reason"], kind="needs_review")

    if l["source"] != "outbound":
        queue_email(lead_id, "qualifying", emails.qualifying_email(lead(lead_id)))
    return lead(lead_id)


def _nurture(lead_id: str, reason: str) -> dict:
    _set_state(lead_id, "nurture", outcome={"route": "nurture", "reason": reason, "at": db.now()})
    return lead(lead_id)


# ---------- sending with approval mode ----------

def _needs_approval(template_key: str) -> bool:
    """approval_mode "always": every draft is reviewed (Level 1 default). "first_n": only the first N sends of each template."""
    g = cfg()["guardrails"]
    if g.get("approval_mode", "always") == "always":
        return True
    row = db.get("template_sends", template_key, key_col="template_key")
    return (row["approved_sends"] if row else 0) < g["approval_first_n_sends"]


def queue_email(lead_id: str, kind: str, mail: dict, sequence_id: str | None = None, step: int | None = None) -> dict:
    template_key = f"seq:{sequence_id}" if sequence_id else mail["template_key"]
    violations = hygiene.copy_violations(mail["subject"] + " " + mail["body"])
    mid = db.new_id("msg")
    msg = {"id": mid, "lead_id": lead_id, "direction": "out", "kind": kind, "subject": mail["subject"], "body": mail["body"],
           "template_key": template_key, "sequence_id": sequence_id, "step": step, "created_at": db.now(),
           "guardrail": {"violations": violations, "drafted_by": mail.get("drafted_by", "template"),
                         "notes": mail.get("draft_notes", [])}}
    if violations:
        msg["status"] = "blocked_guardrail"
        db.insert("messages", msg)
        db.log_event(lead_id, "email_blocked", {"message_id": mid, "violations": violations})
        _route_sdr(lead_id, f"Drafted email failed copy guardrails ({', '.join(violations)}); human to write it.", kind="guardrail")
        return msg
    if _needs_approval(template_key):
        msg["status"] = "pending_approval"
        db.insert("messages", msg)
        _set_state(lead_id, "pending_approval")
        db.log_event(lead_id, "email_pending_approval", {"message_id": mid, "template_key": template_key})
        return msg
    msg["status"] = "queued"
    db.insert("messages", msg)
    return send(mid)


def send(message_id: str) -> dict:
    msg = db.get("messages", message_id)
    if not hygiene.under_send_cap():
        db.update("messages", message_id, {"status": "held_send_cap"})
        db.log_event(msg["lead_id"], "send_cap_hit", {"message_id": message_id})
        return db.get("messages", message_id)
    sup = hygiene.is_suppressed(lead(msg["lead_id"])["email"])
    if sup:
        db.update("messages", message_id, {"status": "blocked_suppressed"})
        return db.get("messages", message_id)
    db.update("messages", message_id, {"status": "sent", "sent_at": db.now()})
    db.log_event(msg["lead_id"], "email_sent", {"message_id": message_id, "subject": msg["subject"], "simulated": True})
    _set_state(msg["lead_id"], "awaiting_reply")
    return db.get("messages", message_id)


def approve(message_id: str, approver: str, edited_body: str | None = None) -> dict:
    msg = db.get("messages", message_id)
    if not msg or msg["status"] != "pending_approval":
        raise ValueError("message is not pending approval")
    if edited_body is not None and edited_body != msg["body"]:
        violations = hygiene.copy_violations(msg["subject"] + " " + edited_body)
        if violations:
            raise ValueError(f"edited copy fails guardrails: {', '.join(violations)}")
        db.update("messages", message_id, {"body": edited_body})
    key = msg["template_key"]
    row = db.get("template_sends", key, key_col="template_key")
    if row:
        db.update("template_sends", key, {"approved_sends": row["approved_sends"] + 1}, key_col="template_key")
    else:
        db.insert("template_sends", {"template_key": key, "approved_sends": 1})
    db.log_event(msg["lead_id"], "email_approved", {"message_id": message_id, "approver": approver, "edited": edited_body is not None})
    return send(message_id)


def reject(message_id: str, approver: str, reason: str) -> dict:
    msg = db.get("messages", message_id)
    db.update("messages", message_id, {"status": "rejected"})
    db.log_event(msg["lead_id"], "email_rejected", {"message_id": message_id, "approver": approver, "reason": reason})
    _route_sdr(msg["lead_id"], f"Approver rejected the drafted email: {reason}", kind="rejected_draft")
    return db.get("messages", message_id)


# ---------- replies ----------

def receive_reply(lead_id: str, body: str) -> dict:
    l = lead(lead_id)
    if not body or not body.strip():
        raise ValidationError(["reply body is empty"])
    mid = db.new_id("msg")
    db.insert("messages", {"id": mid, "lead_id": lead_id, "direction": "in", "kind": "reply", "subject": "Re:",
                           "body": body.strip(), "status": "received", "created_at": db.now(), "sent_at": db.now()})
    # Stop on any reply: outbound sequences never continue once the contact has written back.
    for enr in db.query("SELECT * FROM enrollments WHERE lead_id = ? AND status = 'active'", [lead_id]):
        db.update("enrollments", enr["id"], {"status": "stopped_reply", "reason": "Contact replied"})
        db.log_event(lead_id, "sequence_stopped", {"sequence_id": enr["sequence_id"], "reason": "reply received"})
    for pending in db.query("SELECT id FROM messages WHERE lead_id = ? AND direction='out' AND status IN ('pending_approval','queued')", [lead_id]):
        db.update("messages", pending["id"], {"status": "cancelled_reply"})
    _set_state(lead_id, "replied")

    result = analyzer.analyse(body, l)
    merged = merge_bant(lead_id, result)
    decision = decide(lead(lead_id), result, merged)
    decision["next_action"] = _next_step(lead(lead_id), merged, decision)
    aid = db.new_id("an")
    db.insert("analyses", {"id": aid, "lead_id": lead_id, "message_id": mid, "result": {**result, "merged_bant": merged},
                           "decision": decision, "engine": result["engine"], "created_at": db.now()})
    db.log_event(lead_id, "reply_analysed", {"intent": result["intent"], "confidence": result["intent_confidence"],
                                             "engine": result["engine"], "route": decision["route"]})
    act(lead_id, result, merged, decision)
    return {"lead": lead(lead_id), "analysis": result, "merged_bant": merged, "decision": decision}


def merge_bant(lead_id: str, result: dict) -> dict:
    """Combine signals across all replies in the thread, keeping the strongest evidence per signal."""
    merged = {k: dict(v) for k, v in result["bant"].items()}
    for prev in db.query("SELECT result FROM analyses WHERE lead_id = ?", [lead_id]):
        for k, v in prev["result"]["bant"].items():
            if RANK[v["status"]] > RANK[merged[k]["status"]]:
                merged[k] = {**v, "note": (v.get("note") or "") + " (from an earlier reply)"}
    tl = merged["timeline"]
    merged["_timeline_within_12_months"] = analyzer.within_12_months(tl["evidence"]) if tl["status"] != "missing" else "unknown"
    return merged


def bant_score(merged: dict) -> int:
    return sum({"confirmed": 25, "inferred": 12, "missing": 0}[merged[k]["status"]] for k in analyzer.SIGNALS)


def decide(l: dict, result: dict, merged: dict) -> dict:
    g = cfg()["guardrails"]
    intent, conf = result["intent"], result["intent_confidence"]
    missing = [k for k in ["need", "timeline", "authority", "budget"] if merged[k]["status"] != "confirmed"]
    trace = [f"Intent '{intent}' at confidence {conf:.2f} (minimum {g['min_confidence']})."]
    score_ = bant_score(merged)

    def out(route, reason, **extra):
        return {"route": route, "reason": reason, "trace": trace, "bant_score": score_, "missing": missing, **extra}

    if intent == "unsubscribe":
        return out("suppress", "Contact asked to stop receiving emails.")
    if intent == "not_interested":
        return out("suppress", "Contact is not interested.")
    if intent == "out_of_office":
        return out("wait", "Out-of-office auto reply; keep the thread open and do not count it as a response.")
    if intent == "not_now":
        return out("nurture", "Not a priority now; move to nurture and revisit later.")
    if intent in ("referral", "wrong_person"):
        return out("sdr", "Contact referred us to someone else; SDR decides whether to reach out to the referral.", referral=result.get("referral"))
    if conf < g["min_confidence"] or intent == "unclear":
        trace.append("Below confidence threshold: escalate to a human by default.")
        return out("sdr", "Reply could not be classified with confidence; human SDR to review.")

    need_ok = merged["need"]["status"] == "confirmed"
    tl_ok = merged["timeline"]["status"] == "confirmed" and merged["_timeline_within_12_months"] != "no"
    ba_ok = merged["budget"]["status"] == "confirmed" or merged["authority"]["status"] == "confirmed"
    trace += [f"Need confirmed: {need_ok}.",
              f"Timeline confirmed and within 12 months: {tl_ok} (window: {merged['_timeline_within_12_months']}).",
              f"Budget or Authority confirmed: {ba_ok}."]
    if need_ok and tl_ok and ba_ok:
        return out("qualified", "Meets the BANT qualification rule.", asks_for_pricing=result.get("asks_for_pricing"))
    gaps = [k for k in missing if merged[k]["status"] != "confirmed"]
    if l["followups"] < g["max_gap_followups"]:
        trace.append(f"Gaps {gaps}; {l['followups']} of {g['max_gap_followups']} gap follow-ups used, so send one follow-up.")
        return out("gap_followup", f"Partially qualified; asking about {', '.join(gaps)}.", gaps=gaps, asks_for_pricing=result.get("asks_for_pricing"))
    trace.append("Gap follow-up already used; hand to a human SDR.")
    return out("sdr", f"Partially qualified after follow-up; still missing {', '.join(gaps)}.")


def act(lead_id: str, result: dict, merged: dict, decision: dict) -> None:
    l = lead(lead_id)
    route = decision["route"]
    if route == "suppress":
        hygiene.suppress(l["email"], decision["reason"])
        _set_state(lead_id, "suppressed", outcome={"route": "suppressed", "reason": decision["reason"], "at": db.now()})
        db.insert("alerts", {"id": db.new_id("al"), "lead_id": lead_id, "type": "suppressed", "assignee": "system",
                             "summary": {"reason": decision["reason"], "email": l["email"]}, "status": "logged", "created_at": db.now()})
    elif route == "wait":
        _set_state(lead_id, "awaiting_reply")
    elif route == "nurture":
        _nurture(lead_id, decision["reason"])
    elif route == "gap_followup":
        db.update("leads", lead_id, {"followups": l["followups"] + 1})
        queue_email(lead_id, "gap_followup", emails.gap_followup(l, decision["gaps"], bool(decision.get("asks_for_pricing"))))
    elif route == "qualified":
        _qualified(lead_id, merged, decision)
    else:
        _route_sdr(lead_id, decision["reason"], kind="partial", decision=decision, merged=merged)


# ---------- handoffs ----------

def handoff_summary(l: dict, merged: dict | None, decision: dict | None) -> dict:
    person, company = l.get("person") or {}, l.get("company") or {}
    thread = db.query("SELECT direction, subject, body, status, created_at FROM messages WHERE lead_id = ? AND status IN ('sent','received') ORDER BY created_at", [l["id"]])
    return {
        "lead": {"name": f"{person.get('first_name', '')} {person.get('last_name', '')}".strip(), "title": person.get("title"), "email": l["email"]},
        "account": {"name": company.get("name"), "country": company.get("country"), "region": company.get("region"),
                    "institution_type": company.get("institution_type"), "employees": company.get("employees")},
        "product": l.get("product"), "source": l.get("source"),
        "existing_account": (l.get("crm") or {}).get("existing_account"),
        "icp": {"score": (l.get("icp") or {}).get("score"), "verdict": (l.get("icp") or {}).get("verdict")},
        "bant": {k: v for k, v in (merged or {}).items() if not k.startswith("_")},
        "bant_score": decision.get("bant_score") if decision else None,
        "decision": decision,
        "thread": thread,
        "recommended_next_step": _next_step(l, merged, decision),
    }


def _next_step(l: dict, merged: dict | None, decision: dict | None) -> str:
    company = (l.get("company") or {}).get("name") or "the account"
    pname = cfg()["products"].get(l.get("product") or "", {}).get("name", "the product")
    if not decision:
        return "Review the lead."
    route = decision["route"]
    if route == "qualified":
        gaps = [k for k in ("budget", "authority") if merged[k]["status"] != "confirmed"]
        step = f"Book a discovery call with {company} within 2 business days and prepare an overview of {pname}."
        if gaps:
            step += f" Confirm {' and '.join(gaps)} on the call."
        if decision.get("asks_for_pricing"):
            step += " Prospect asked about pricing; commercials are for the AE to handle (the agent never quotes)."
        return step
    if route == "gap_followup":
        return f"Review and approve the follow-up asking about {', '.join(decision.get('gaps', []))}, then wait for the reply."
    if route == "suppress":
        return "No further outreach. Contact is on the suppression list."
    if route == "nurture":
        return "Move to the low-touch nurture track and revisit next quarter."
    if route == "wait":
        return "Out of office: wait for the contact to return; no follow-up until then."
    if route == "sdr" and decision.get("referral", {}).get("name"):
        r = decision["referral"]
        return f"Decide whether to contact the referral {r['name']} ({r.get('title') or 'title unknown'}, {r.get('email') or 'no email'}) referencing the original contact."
    if decision.get("missing"):
        return "Call or email personally to fill the gaps: " + ", ".join(decision["missing"]) + "."
    return "Review the reply personally and decide the next step."


READY_FOR_HUMAN = {"qualified", "sdr_followup", "existing_opportunity"}


def next_action(l: dict, decision: dict | None) -> dict:
    """What should happen next for this lead, and who owns it. Shown on the dashboard and lead view."""
    state, out = l["state"], l.get("outcome") or {}
    owner = (l.get("owner") or {}).get("name")
    if state in ("qualified",) and decision:
        action = decision.get("next_action") or _next_step(l, decision.get("merged"), decision)
    elif state == "sdr_followup":
        action = (decision or {}).get("next_action") or f"Human SDR to review: {out.get('reason', '')}"
    elif state == "existing_opportunity":
        action = out.get("reason", "") + " Account owner decides how to engage."
    elif state == "pending_approval":
        action, owner = "Review and approve the drafted email before it is sent.", "SDR reviewer"
    elif state == "awaiting_reply":
        action, owner = "Waiting for the prospect's reply; the agent will analyse it on arrival.", "AI SDR"
    elif state == "blocked":
        action = f"Do not contact. {out.get('reason', '')}"
    elif state == "suppressed":
        action = "No further outreach."
    elif state == "nurture":
        action = f"Low-touch nurture. {out.get('reason', '')}"
    else:
        action, owner = "Processing.", "AI SDR"
    return {"action": action.strip(), "owner": owner or "—", "ready_for_human": state in READY_FOR_HUMAN}


def owner_for(l: dict) -> dict:
    """An existing CRM account keeps its owner; otherwise the regional AE."""
    acct = (l.get("crm") or {}).get("existing_account")
    if acct:
        known = [o for o in cfg()["owners"]["ae_by_region"].values() if o["name"] == acct["owner"]]
        return known[0] if known else {"name": acct["owner"], "role": "Account owner"}
    return ae_for((l.get("company") or {}).get("region"))


def _qualified(lead_id: str, merged: dict, decision: dict) -> None:
    l = lead(lead_id)
    ae = owner_for(l)
    company = (l.get("company") or {}).get("name")
    opp_id = db.new_id("opp")
    db.insert("crm_opportunities", {"id": opp_id, "lead_id": lead_id, "account": company,
                                    "name": f"{company} - {cfg()['products'][l['product']]['name']}", "product": l["product"],
                                    "stage": "Qualified (BANT)", "owner": ae["name"],
                                    "bant": {k: v["status"] for k, v in merged.items() if not k.startswith("_")}, "created_at": db.now()})
    summary = handoff_summary(l, merged, decision)
    summary["crm_opportunity_id"] = opp_id
    db.insert("alerts", {"id": db.new_id("al"), "lead_id": lead_id, "type": "ae_alert", "assignee": f"{ae['name']} ({ae['role']})",
                         "summary": summary, "status": "new", "created_at": db.now()})
    _set_state(lead_id, "qualified", owner=ae, outcome={"route": "qualified", "reason": decision["reason"], "at": db.now()})
    db.log_event(lead_id, "crm_opportunity_created", {"opportunity_id": opp_id, "owner": ae["name"]})


def _route_sdr(lead_id: str, reason: str, kind: str, decision: dict | None = None, merged: dict | None = None) -> dict:
    l = lead(lead_id)
    s = sdr()
    summary = handoff_summary(l, merged, decision or {"route": "sdr", "reason": reason, "missing": [], "trace": []})
    summary["task"] = reason
    db.insert("alerts", {"id": db.new_id("al"), "lead_id": lead_id, "type": "sdr_task", "assignee": f"{s['name']} ({s['role']})",
                         "summary": summary, "status": "open", "created_at": db.now()})
    _set_state(lead_id, "sdr_followup", owner=s, outcome={"route": "sdr_followup", "kind": kind, "reason": reason, "at": db.now()})
    return lead(lead_id)


def _alert_owner_existing(lead_id: str, opp: dict) -> dict:
    l = lead(lead_id)
    reason = f"Account has an open opportunity '{opp['name']}' ({opp['stage']}) owned by {opp['owner']}; no automated outreach."
    summary = handoff_summary(l, None, {"route": "existing_opportunity", "reason": reason, "missing": [], "trace": []})
    summary["recommended_next_step"] = f"{opp['owner']} to decide how to engage this contact as part of the open opportunity."
    db.insert("alerts", {"id": db.new_id("al"), "lead_id": lead_id, "type": "ae_alert", "assignee": opp["owner"],
                         "summary": summary, "status": "new", "created_at": db.now()})
    _set_state(lead_id, "existing_opportunity", outcome={"route": "existing_opportunity", "reason": reason, "at": db.now()})
    return lead(lead_id)
