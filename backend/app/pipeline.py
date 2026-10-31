"""Workflow (workstream B owns the gates; C and D plug in through engine/ and engagement/).

process:  enrich -> ICP -> intro draft (pending human approval)
drafts:   approve (exact text + version) -> simulated send; edits invalidate approval
replies:  store once -> stop sequences -> assess -> merge BANT across thread -> deterministic route -> act
tasks:    handoffs (AE) + next_actions (SDR/review); accepting a handoff creates the opportunity
"""
import hashlib
import re
import uuid

from psycopg import Connection

from . import engagement
from .db import Jsonb
from .engine import get_engine
from .engine import policy as qual
from .repo import Conflict, NotFound, agent_run, audit, lead_context, now, owner_for, policy, set_stage, user_id

BLOCKED_STAGES = {"suppressed", "blocked", "existing_opportunity", "duplicate", "disqualified"}
# Reply text is untrusted data. Text that reads like instructions to the system never drives an automatic route.
INJECTION_RE = re.compile(r"\b(ignore (all |any )?(previous|prior|above) instructions|disregard (the |your )?(rules|instructions)|"
                          r"system prompt|you are now|mark (this|the|me as|my) (lead|contact|status)|set (the )?(lead )?(status|stage|score))\b", re.I)


def content_hash(subject: str | None, body: str) -> str:
    return hashlib.sha256(f"{subject or ''}\n{body}".encode()).hexdigest()


# ───────────── guardrails (server-side; drafts never bypass this) ─────────────

def guardrail_check(conn: Connection, text: str, lead_id: int, message_id: int | None = None) -> list[str]:
    g = policy(conn, "guardrail")
    low = text.lower()
    hits = []
    for term in g["bannedTerms"]:
        pattern = re.escape(term) if not term.isalpha() else rf"\b{re.escape(term)}\b"
        if re.search(pattern, low):
            hits.append(f"banned term '{term}'")
    if re.search(r"(?:usd|aed|sar|inr|\$|₹)\s?\d", low):
        hits.append("currency amount")
    conn.execute("INSERT INTO guardrail_events (lead_id, message_id, rule, result, details, at) VALUES (%s,%s,'copy_policy',%s,%s,%s)",
                 [lead_id, message_id, "block" if hits else "pass", "; ".join(hits) or None, now()])
    return hits


# ───────────── process: enrich -> ICP -> draft ─────────────

def process(conn: Connection, lead_id: int) -> int | None:
    ctx = lead_context(conn, lead_id)
    if ctx["stage"] in BLOCKED_STAGES:
        return None
    engine = get_engine()
    last_run = enrich(conn, lead_id, engine)
    ctx = lead_context(conn, lead_id)
    if not ctx["product"]:
        _human_review(conn, lead_id, "Product of interest unknown; SDR to review.")
        return last_run
    last_run, icp = score(conn, lead_id, engine)
    if icp["fit"] == "low":
        set_stage(conn, lead_id, "nurture", icp["summary"], route="nurture", outcome_reason=icp["summary"])
        conn.execute("INSERT INTO next_actions (lead_id, action_type, description) VALUES (%s,'nurture',%s)", [lead_id, icp["summary"]])
        return last_run
    if icp["fit"] == "insufficient_data":
        _human_review(conn, lead_id, icp["summary"])
        return last_run
    return create_draft(conn, lead_id, "initial")["runId"]


def enrich(conn: Connection, lead_id: int, engine=None) -> int:
    engine = engine or get_engine()
    ctx = lead_context(conn, lead_id)
    with agent_run(conn, "enricher", lead_id, "inbound_lead", {"email": ctx["contact"]["email"]}) as run:
        res = engine.enrich(ctx)
        run["output"], run["summary"] = res, f"{res['status']} via {res['provider']}; missing: {', '.join(res['missing']) or 'none'}"
    conn.execute("INSERT INTO enrichment_records (lead_id, provider, status, data, missing_fields, uncertain_fields, agent_run_id) "
                 "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                 [lead_id, res["provider"], res["status"], Jsonb(res["data"]), Jsonb(res["missing"]), Jsonb(res["uncertain"]), run["id"]])
    # Enrichment fills gaps only; it never overwrites what the buyer supplied.
    p, c = res["person"], res["company"]
    conn.execute("UPDATE contacts SET job_title = COALESCE(job_title, %s), seniority = COALESCE(seniority, %s) "
                 "WHERE id = (SELECT contact_id FROM leads WHERE id = %s)", [p["jobTitle"], p["seniority"], lead_id])
    if ctx["account"]:
        conn.execute("UPDATE accounts SET industry = COALESCE(industry, %s), institution_type = COALESCE(institution_type, %s), "
                     "country = COALESCE(country, %s), employee_count = COALESCE(employee_count, %s) WHERE id = %s",
                     [c["industry"], c["institutionType"], c["country"], c["employeeCount"], ctx["account"]["id"]])
    set_stage(conn, lead_id, "enriched", f"enrichment {res['status']}")
    return run["id"]


def score(conn: Connection, lead_id: int, engine=None) -> tuple[int, dict]:
    engine = engine or get_engine()
    ctx = lead_context(conn, lead_id)
    profile = conn.execute("SELECT * FROM icp_profiles WHERE product_id = %s AND active ORDER BY version DESC LIMIT 1",
                           [ctx["product"]["id"]]).fetchone()
    criteria = conn.execute("SELECT dimension, weight, rule FROM icp_criteria WHERE icp_profile_id = %s ORDER BY id", [profile["id"]]).fetchall()
    with agent_run(conn, "icp_scorer", lead_id, "inbound_lead", {"profile": profile["id"], "version": profile["version"]}) as run:
        icp = engine.score_icp(ctx, profile, criteria)
        run["output"], run["summary"] = icp, icp["summary"]
    conn.execute("INSERT INTO icp_evaluations (lead_id, icp_profile_id, score, fit, missing_count, breakdown, summary, agent_run_id) "
                 "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                 [lead_id, profile["id"], icp["score"], icp["fit"], icp["missingCount"], Jsonb(icp["breakdown"]), icp["summary"], run["id"]])
    set_stage(conn, lead_id, "scored", icp["summary"], icp_score=icp["score"], icp_fit=icp["fit"])
    return run["id"], icp


def _human_review(conn: Connection, lead_id: int, reason: str, action_type: str = "human_review") -> None:
    set_stage(conn, lead_id, "human_review", reason, route="human_review", outcome_reason=reason, ready_for_followup=True)
    conn.execute("INSERT INTO next_actions (lead_id, action_type, description, assigned_to) VALUES (%s,%s,%s,%s)",
                 [lead_id, action_type, reason, user_id(conn, "sdr")])


# ───────────── drafts ─────────────

def _conversation(conn: Connection, lead_id: int, subject: str) -> dict:
    conv = conn.execute("SELECT * FROM conversations WHERE lead_id = %s ORDER BY id DESC LIMIT 1", [lead_id]).fetchone()
    if conv:
        return conv
    return conn.execute("INSERT INTO conversations (lead_id, subject, status, started_at, last_message_at, thread_id) "
                        "VALUES (%s,%s,'draft_pending',%s,%s,%s) RETURNING *",
                        [lead_id, subject, now(), now(), f"thr-{lead_id}-{uuid.uuid4().hex[:8]}"]).fetchone()


def create_draft(conn: Connection, lead_id: int, stage: str, gap: str | None = None, asks_pricing: bool = False,
                 sequence_step: dict | None = None) -> dict:
    ctx = lead_context(conn, lead_id)
    if ctx["stage"] in BLOCKED_STAGES:
        raise Conflict("contact_blocked", f"lead is {ctx['stage']}; no drafts")
    facts = conn.execute("SELECT id, statement FROM product_facts WHERE product_id = %s AND approved_at IS NOT NULL ORDER BY id",
                         [ctx["product"]["id"]]).fetchall() if ctx["product"] else []
    with agent_run(conn, "outreach_writer", lead_id, "inbound_lead" if stage == "initial" else "prospect_reply", {"stage": stage, "gap": gap}) as run:
        if sequence_step:
            d = {"kind": "sequence_step", "qualifyingQuestions": [], "productFactsUsed": [], **sequence_step}
        else:
            d = engagement.compose("follow_up" if stage == "follow_up" else "initial", ctx, facts, gap, asks_pricing)
        run["output"], run["summary"] = {"subject": d["subject"]}, f"{d['kind']} draft"
        run["status"] = "awaiting_human"
    conv = _conversation(conn, lead_id, d["subject"])
    msg = conn.execute(
        "INSERT INTO messages (conversation_id, lead_id, direction, sender_type, kind, subject, body, status, qualifying_questions, "
        "product_facts_used, generated_by_run_id, created_at, content_hash, sequence_step_id) "
        "VALUES (%s,%s,'outbound','agent',%s,%s,%s,'pending_approval',%s,%s,%s,%s,%s,%s) RETURNING id",
        [conv["id"], lead_id, d["kind"], d["subject"], d["body"], Jsonb(d["qualifyingQuestions"]), Jsonb(d["productFactsUsed"]),
         run["id"], now(), content_hash(d["subject"], d["body"]), (sequence_step or {}).get("stepId")]).fetchone()
    hits = guardrail_check(conn, d["subject"] + " " + d["body"], lead_id, msg["id"])
    if hits:
        conn.execute("UPDATE messages SET status = 'blocked' WHERE id = %s", [msg["id"]])
        conn.execute("INSERT INTO next_actions (lead_id, action_type, description, assigned_to) VALUES (%s,'rewrite_draft',%s,%s)",
                     [lead_id, f"Draft blocked by guardrails ({', '.join(hits)}); rewrite manually.", user_id(conn, "sdr")])
    else:
        conn.execute("INSERT INTO next_actions (lead_id, action_type, description, assigned_to) VALUES (%s,'review_draft',%s,%s)",
                     [lead_id, f"Review and approve: {d['subject']}", user_id(conn, "reviewer")])
    conn.execute("UPDATE leads SET first_draft_at = COALESCE(first_draft_at, now()) WHERE id = %s", [lead_id])
    audit(conn, "message", msg["id"], "drafted", "agent", "outreach_writer", {"blocked": hits})
    return {"draftId": msg["id"], "runId": run["id"], "status": "blocked" if hits else "pending_approval", "guardrail": hits}


def _reviewer(conn: Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM users WHERE lower(name) = lower(%s) OR lower(email) = lower(%s)", [name, name]).fetchone()
    return row["id"] if row else user_id(conn, "reviewer")


def approve(conn: Connection, draft_id: int, approved_by: str, body: str | None, expected_version: int | None) -> dict:
    m = conn.execute("SELECT * FROM messages WHERE id = %s AND direction = 'outbound' FOR UPDATE", [draft_id]).fetchone()
    if not m:
        raise NotFound(f"draft {draft_id}")
    if m["status"] != "pending_approval":
        raise Conflict("stale_version", f"draft is {m['status']}, not pending approval")
    if expected_version is not None and expected_version != m["version"]:
        raise Conflict("stale_version", f"draft is at version {m['version']}, you approved version {expected_version}")
    final, decision = m["body"], "approved"
    if body is not None and body != m["body"]:
        hits = guardrail_check(conn, (m["subject"] or "") + " " + body, m["lead_id"], draft_id)
        if hits:
            raise Conflict("guardrail_block", f"edited text fails guardrails: {', '.join(hits)}")
        final, decision = body, "edited_approved"
    h = content_hash(m["subject"], final)
    conn.execute("UPDATE messages SET body = %s, status = 'approved', content_hash = %s, version = version + %s WHERE id = %s",
                 [final, h, 1 if decision == "edited_approved" else 0, draft_id])
    version = m["version"] + (1 if decision == "edited_approved" else 0)
    conn.execute("INSERT INTO approvals (message_id, reviewer_id, decision, original_body, final_body, decided_at, content_hash, message_version) "
                 "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", [draft_id, _reviewer(conn, approved_by), decision, m["body"], final, now(), h, version])
    conn.execute("UPDATE next_actions SET status = 'done' WHERE lead_id = %s AND action_type = 'review_draft' AND status = 'open'", [m["lead_id"]])
    audit(conn, "message", draft_id, decision, "user", approved_by)
    return {"draftId": str(draft_id), "status": "approved", "version": version, "contentHash": h}


def reject(conn: Connection, draft_id: int, rejected_by: str, reason: str) -> dict:
    m = conn.execute("SELECT * FROM messages WHERE id = %s AND status = 'pending_approval'", [draft_id]).fetchone()
    if not m:
        raise Conflict("stale_version", "draft is not pending approval")
    conn.execute("UPDATE messages SET status = 'rejected' WHERE id = %s", [draft_id])
    conn.execute("INSERT INTO approvals (message_id, reviewer_id, decision, original_body, comment, decided_at) VALUES (%s,%s,'rejected',%s,%s,%s)",
                 [draft_id, _reviewer(conn, rejected_by), m["body"], reason, now()])
    conn.execute("UPDATE next_actions SET status = 'done' WHERE lead_id = %s AND action_type = 'review_draft' AND status = 'open'", [m["lead_id"]])
    conn.execute("INSERT INTO next_actions (lead_id, action_type, description, assigned_to) VALUES (%s,'rewrite_draft',%s,%s)",
                 [m["lead_id"], f"Rejected: {reason}", user_id(conn, "sdr")])
    audit(conn, "message", draft_id, "rejected", "user", rejected_by, {"reason": reason})
    return {"draftId": str(draft_id), "status": "rejected"}


def send(conn: Connection, draft_id: int) -> dict:
    """Simulated send. Re-checks every gate at dispatch time; repeated calls return the existing send."""
    m = conn.execute("SELECT * FROM messages WHERE id = %s FOR UPDATE", [draft_id]).fetchone()
    if not m:
        raise NotFound(f"draft {draft_id}")
    if m["status"] == "sent_simulated":
        return {"draftId": str(draft_id), "status": "sent_simulated", "sentAt": m["sent_at"].isoformat(), "idempotent": True}
    if m["status"] != "approved":
        raise Conflict("not_approved", f"draft is {m['status']}; approve it first")
    appr = conn.execute("SELECT content_hash FROM approvals WHERE message_id = %s AND decision <> 'rejected' ORDER BY id DESC LIMIT 1", [draft_id]).fetchone()
    if not appr or appr["content_hash"] != content_hash(m["subject"], m["body"]):
        conn.execute("UPDATE messages SET status = 'stale' WHERE id = %s", [draft_id])
        raise Conflict("stale_version", "text changed after approval; re-approval required")
    ctx = lead_context(conn, m["lead_id"])
    if ctx["stage"] in BLOCKED_STAGES:
        raise Conflict("contact_blocked", f"lead is {ctx['stage']}")
    email = ctx["contact"]["email"]
    if conn.execute("SELECT 1 FROM suppressions WHERE lower(email) = %s OR domain = %s", [email, email.split("@")[-1]]).fetchone():
        raise Conflict("contact_blocked", "contact is suppressed")
    g = policy(conn, "guardrail")
    sent_today = conn.execute("SELECT count(*) AS n FROM messages WHERE status = 'sent_simulated' AND sent_at::date = now()::date").fetchone()["n"]
    if sent_today >= g["dailySendCap"]:
        raise Conflict("send_cap", f"daily cap of {g['dailySendCap']} simulated sends reached")
    if m["sequence_step_id"] and conn.execute("SELECT 1 FROM messages WHERE lead_id = %s AND direction = 'inbound'", [m["lead_id"]]).fetchone():
        conn.execute("UPDATE messages SET status = 'cancelled' WHERE id = %s", [draft_id])
        raise Conflict("reply_received", "prospect already replied; sequence touch cancelled")
    sent_at = now()
    conn.execute("UPDATE messages SET status = 'sent_simulated', sent_at = %s WHERE id = %s", [sent_at, draft_id])
    conn.execute("UPDATE conversations SET status = 'awaiting_reply', last_message_at = %s WHERE id = %s", [sent_at, m["conversation_id"]])
    conn.execute("UPDATE leads SET first_response_at = COALESCE(first_response_at, %s), "
                 "response_time_seconds = COALESCE(response_time_seconds, EXTRACT(EPOCH FROM (%s - created_at))::int) WHERE id = %s",
                 [sent_at, sent_at, m["lead_id"]])
    if ctx["stage"] in ("new", "enriched", "scored"):
        set_stage(conn, m["lead_id"], "engaged", "intro sent (simulated)")
    audit(conn, "message", draft_id, "sent_simulated", details={"to": email})
    return {"draftId": str(draft_id), "status": "sent_simulated", "sentAt": sent_at.isoformat(), "simulated": True}


# ───────────── replies ─────────────

def ingest_reply(conn: Connection, lead_id: int, message_id: str, thread_id: str, body: str, origin: str) -> dict:
    """Stop first, analyse second. Inbound and outbound replies share this path."""
    dup = conn.execute("SELECT id FROM messages WHERE external_message_id = %s", [message_id]).fetchone()
    if dup:
        a = conn.execute("SELECT id, recommended_action FROM bant_assessments WHERE trigger_message_id = %s", [dup["id"]]).fetchone()
        return {"leadId": str(lead_id), "messageId": str(dup["id"]), "idempotent": True,
                "assessmentId": str(a["id"]) if a else None, "recommendedAction": a["recommended_action"] if a else None}
    ctx = lead_context(conn, lead_id)
    conv = conn.execute("SELECT * FROM conversations WHERE thread_id = %s", [thread_id]).fetchone() or _conversation(conn, lead_id, "Re:")
    msg = conn.execute("INSERT INTO messages (conversation_id, lead_id, direction, sender_type, kind, body, status, created_at, external_message_id) "
                       "VALUES (%s,%s,'inbound','prospect','reply',%s,'received',%s,%s) RETURNING id",
                       [conv["id"], lead_id, body, now(), message_id]).fetchone()
    stopped = conn.execute("UPDATE enrollments SET state = 'stopped_reply', stop_reason = 'prospect replied' WHERE lead_id = %s AND state = 'active' RETURNING id",
                           [lead_id]).fetchall()
    conn.execute("UPDATE messages SET status = 'cancelled' WHERE lead_id = %s AND direction = 'outbound' AND status IN ('pending_approval','approved')", [lead_id])
    conn.execute("UPDATE conversations SET status = 'replied', last_message_at = now() WHERE id = %s", [conv["id"]])
    audit(conn, "message", msg["id"], "reply_received", details={"origin": origin, "sequencesStopped": len(stopped)})

    keywords = [k for r in conn.execute("SELECT keywords FROM products WHERE active").fetchall() for k in r["keywords"]]
    bant_policy = policy(conn, "bant")
    with agent_run(conn, "qualifier", lead_id, "prospect_reply", {"messageId": msg["id"]}) as run:
        res = get_engine().assess_reply(body, ctx, keywords)
        prev = conn.execute("SELECT s.* FROM bant_signals s JOIN bant_assessments a ON a.id = s.assessment_id WHERE a.lead_id = %s", [lead_id]).fetchall()
        merged = qual.merge(prev, res["bant"])
        followups = conn.execute("SELECT count(*) AS n FROM messages WHERE lead_id = %s AND kind = 'followup' AND status = 'sent_simulated'", [lead_id]).fetchone()["n"]
        decision = qual.decide(res["intent"], res["intent_confidence"], merged, followups, bant_policy)
        if INJECTION_RE.search(body):
            conn.execute("INSERT INTO guardrail_events (lead_id, message_id, rule, result, details, at) VALUES (%s,%s,'reply_injection','block',%s,%s)",
                         [lead_id, msg["id"], "reply contains instruction-like text; routed to a human", now()])
            decision = {**decision, "route": "human_review", "action": "human_review", "salesReady": False,
                        "reason": "Reply contains instruction-like text; a human reviews it before anything else happens.",
                        "trace": decision["trace"] + ["Possible prompt injection in reply: automatic routing disabled."]}
        score_ = qual.bant_score(merged, bant_policy["points"])
        action_text = qual.next_action_text(decision, (ctx["account"] or {}).get("name"), (ctx["product"] or {}).get("name"),
                                            res.get("referral"), res.get("asks_for_pricing", False))
        run["output"] = {"analysis": res, "merged": merged, "decision": decision}
        run["summary"] = f"{res['intent']} -> {decision['route']}"
    a = conn.execute(
        "INSERT INTO bant_assessments (lead_id, trigger_message_id, budget_status, authority_status, need_status, timeline_status, score, "
        "recommended_action, action_detail, sales_ready, summary, agent_run_id, assessed_at, reply_intent, intent_confidence, referral, "
        "asks_for_pricing, rule_trace, policy_version, engine) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        [lead_id, msg["id"], *(merged[k]["status"] for k in qual.DIMENSIONS), score_, decision["action"], action_text,
         decision["salesReady"], res["summary"], run["id"], now(), res["intent"], res["intent_confidence"], Jsonb(res.get("referral")),
         res.get("asks_for_pricing", False), Jsonb(decision["trace"]), "bant-v1", res.get("engine")]).fetchone()
    title = (ctx["contact"].get("jobTitle") or "").lower()
    for k in qual.DIMENSIONS:
        s = merged[k]
        src = {"reply": "message", "message": "message", "enrichment": "enrichment"}.get(s.get("source"))
        quote = s.get("evidence") or None
        verified = None if not quote else (quote.lower() in (body.lower() if src == "message" else title)) or "earlier reply" in (s.get("note") or "")
        conn.execute("INSERT INTO bant_signals (assessment_id, dimension, status, value, evidence_quote, evidence_source, message_id, quote_verified) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", [a["id"], k, s["status"], s.get("note") or None, quote, src, msg["id"] if src == "message" else None, verified])
    _act(conn, lead_id, ctx, decision, merged, score_, action_text, a["id"], res)
    return {"leadId": str(lead_id), "messageId": str(msg["id"]), "assessmentId": str(a["id"]), "runId": str(run["id"]),
            "intent": res["intent"], "route": decision["route"], "recommendedAction": decision["action"], "nextAction": action_text,
            "bantScore": score_, "bant": merged, "trace": decision["trace"], "sequencesStopped": len(stopped)}


def _act(conn: Connection, lead_id: int, ctx: dict, decision: dict, merged: dict, score_: int, action_text: str, assessment_id: int, res: dict) -> None:
    route = decision["route"]
    status = "qualified" if route == "rep_review" else ("partial" if any(merged[k]["status"] != "missing" for k in qual.DIMENSIONS) else "unqualified")
    common = {"bant_score": score_, "bant_status": status, "route": route, "outcome_reason": decision["reason"]}
    if route == "rep_review":
        owner = owner_for(conn, ctx)
        card = {"lead": ctx["contact"], "account": ctx["account"], "product": ctx["product"], "bant": merged, "bantScore": score_,
                "trace": decision["trace"], "nextAction": action_text, "assessmentId": assessment_id}
        conn.execute("INSERT INTO handoffs (lead_id, from_agent_id, to_user_id, summary, status, created_at, bant_assessment_id, card) "
                     "VALUES (%s,(SELECT id FROM agents WHERE key='qualifier'),%s,%s,'pending',%s,%s,%s)",
                     [lead_id, owner, action_text, now(), assessment_id, Jsonb(card)])
        conn.execute("INSERT INTO next_actions (lead_id, action_type, description, assigned_to) VALUES (%s,'handoff_to_rep',%s,%s)", [lead_id, action_text, owner])
        set_stage(conn, lead_id, "qualified", decision["reason"], owner_id=owner, ready_for_followup=True, **common)
    elif route == "gap_followup":
        set_stage(conn, lead_id, "qualifying", decision["reason"], **common)
        create_draft(conn, lead_id, "follow_up", gap=decision.get("nextQuestion"), asks_pricing=res.get("asks_for_pricing", False))
    elif route == "human_review":
        kind = "referral_review" if res.get("intent") == "referral" else "human_review"
        conn.execute("INSERT INTO next_actions (lead_id, action_type, description, assigned_to) VALUES (%s,%s,%s,%s)",
                     [lead_id, kind, action_text, user_id(conn, "sdr")])
        set_stage(conn, lead_id, "human_review", decision["reason"], ready_for_followup=True, **common)
    elif route == "suppress":
        conn.execute("INSERT INTO suppressions (email, reason) VALUES (%s,%s)", [ctx["contact"]["email"], decision["reason"]])
        conn.execute("UPDATE contacts SET consent_status = 'opted_out' WHERE lower(email) = %s AND %s", [ctx["contact"]["email"], res["intent"] == "unsubscribe"])
        set_stage(conn, lead_id, "suppressed", decision["reason"], **common)
    elif route == "nurture":
        conn.execute("INSERT INTO next_actions (lead_id, action_type, description) VALUES (%s,'nurture',%s)", [lead_id, action_text])
        set_stage(conn, lead_id, "nurture", decision["reason"], **common)
    else:  # wait (out of office): thread stays open
        conn.execute("UPDATE leads SET route = %s, outcome_reason = %s, updated_at = now() WHERE id = %s", [route, decision["reason"], lead_id])


# ───────────── tasks ─────────────

def list_tasks(conn: Connection, status: str | None = None) -> list[dict]:
    rows = []
    for h in conn.execute("SELECT h.*, u.name AS assignee FROM handoffs h LEFT JOIN users u ON u.id = h.to_user_id ORDER BY h.id DESC").fetchall():
        if status and h["status"] != status:
            continue
        rows.append({"taskId": f"handoff-{h['id']}", "kind": "handoff", "leadId": str(h["lead_id"]), "type": "handoff_to_rep",
                     "description": h["summary"], "status": h["status"], "assignee": h["assignee"], "card": h["card"], "createdAt": h["created_at"].isoformat()})
    for n in conn.execute("SELECT n.*, u.name AS assignee FROM next_actions n LEFT JOIN users u ON u.id = n.assigned_to "
                          "WHERE n.action_type <> 'handoff_to_rep' ORDER BY n.id DESC").fetchall():
        if status and n["status"] != status:
            continue
        rows.append({"taskId": f"action-{n['id']}", "kind": "next_action", "leadId": str(n["lead_id"]), "type": n["action_type"],
                     "description": n["description"], "status": n["status"], "assignee": n["assignee"], "card": None, "createdAt": n["created_at"].isoformat()})
    return rows


def accept_task(conn: Connection, task_id: str, accepted_by: str) -> dict:
    kind, _, raw = task_id.partition("-")
    if not raw.isdigit():
        raise NotFound(f"task {task_id}")
    if kind == "action":
        n = conn.execute("UPDATE next_actions SET status = 'done' WHERE id = %s RETURNING lead_id", [int(raw)]).fetchone()
        if not n:
            raise NotFound(f"task {task_id}")
        audit(conn, "next_action", int(raw), "done", "user", accepted_by)
        return {"taskId": task_id, "status": "done"}
    h = conn.execute("SELECT * FROM handoffs WHERE id = %s FOR UPDATE", [int(raw)]).fetchone()
    if not h:
        raise NotFound(f"task {task_id}")
    if h["status"] != "pending":
        raise Conflict("stale_version", f"handoff already {h['status']}")
    ctx = lead_context(conn, h["lead_id"])
    if not ctx["account"]:
        raise Conflict("no_account", "lead has no company account; create one before opening an opportunity")
    opp = conn.execute("INSERT INTO opportunities (account_id, product_id, source_lead_id, owner_id, name, stage, bant_snapshot) "
                       "VALUES (%s,%s,%s,%s,%s,'qualified',%s) RETURNING id",
                       [ctx["account"]["id"], ctx["product"]["id"], h["lead_id"], h["to_user_id"],
                        f"{ctx['account']['name']} - {ctx['product']['name']}", Jsonb(h["card"]["bant"] if h["card"] else None)]).fetchone()
    conn.execute("UPDATE handoffs SET status = 'accepted', accepted_at = now(), opportunity_id = %s, decision_reason = %s WHERE id = %s",
                 [opp["id"], f"accepted by {accepted_by}", h["id"]])
    conn.execute("UPDATE next_actions SET status = 'done' WHERE lead_id = %s AND action_type = 'handoff_to_rep' AND status = 'open'", [h["lead_id"]])
    set_stage(conn, h["lead_id"], "handed_off", f"accepted by {accepted_by}", "user", accepted_by, ready_for_followup=False)
    audit(conn, "handoff", h["id"], "accepted", "user", accepted_by, {"opportunityId": opp["id"]})
    return {"taskId": task_id, "status": "accepted", "opportunityId": str(opp["id"])}
