"""Outbound sequences (workstream D owns the copy; B owns enrollment gates and the demo clock).
Every touch is drafted as pending_approval; a reply on the lead stops the enrollment (pipeline.ingest_reply)."""
from datetime import timedelta

from psycopg import Connection

from . import engagement, pipeline
from .db import Jsonb
from .repo import Conflict, NotFound, audit, lead_context, now, policy

THEME_STEPS = [
    ("{theme} at {company}", "Hi {first_name},\n\nAs {title} at {company}, you may be looking at {theme_lower}.\n\n{fact}\n\n"
                             "Would a short conversation to compare notes be useful?"),
    ("Re: {theme} at {company}", "Hi {first_name},\n\nFollowing up on my note. If {theme_lower} is on your agenda for the coming year, "
                                 "I'd be glad to share how peers are approaching it."),
    ("Should I close the loop?", "Hi {first_name},\n\nI don't want to crowd your inbox. If this isn't a priority, just say so and I'll close it out. "
                                 "If someone else at {company} owns this, a pointer would be appreciated."),
]


def _clock(conn: Connection):
    return now() + timedelta(hours=conn.execute("SELECT offset_hrs FROM demo_clock WHERE id = 1").fetchone()["offset_hrs"])


def create(conn: Connection, lead_ids: list[str], theme: str, cadence_hours: list[int]) -> dict:
    if len(cadence_hours) != len(THEME_STEPS):
        raise Conflict("validation_failed", f"cadenceHours must have {len(THEME_STEPS)} entries")
    seq = conn.execute("INSERT INTO sequences (name, theme, cohort, status, approved_at) VALUES (%s,%s,%s,'active',now()) RETURNING id",
                       [theme[:120], theme, Jsonb({"leadIds": lead_ids, "cadenceHours": cadence_hours})]).fetchone()
    for i, ((subj, body), hrs) in enumerate(zip(THEME_STEPS, cadence_hours), start=1):
        conn.execute("INSERT INTO sequence_steps (sequence_id, step_number, day_offset, subject, body) VALUES (%s,%s,%s,%s,%s)",
                     [seq["id"], i, hrs // 24, subj, body])
    enrolled, excluded = [], []
    start = _clock(conn)
    for raw in lead_ids:
        try:
            ctx = lead_context(conn, int(raw))
        except (ValueError, NotFound):
            excluded.append({"leadId": raw, "reason": "lead not found"})
            continue
        reason = _exclusion(conn, ctx)
        if reason:
            excluded.append({"leadId": raw, "reason": reason})
            continue
        contact_id = conn.execute("SELECT contact_id FROM leads WHERE id = %s", [ctx["leadId"]]).fetchone()["contact_id"]
        try:
            with conn.transaction():
                conn.execute("INSERT INTO enrollments (sequence_id, contact_id, lead_id, next_step, next_due_at) VALUES (%s,%s,%s,1,%s)",
                             [seq["id"], contact_id, ctx["leadId"], start + timedelta(hours=cadence_hours[0])])
            enrolled.append(raw)
        except Exception:  # partial unique index: one active sequence per contact
            excluded.append({"leadId": raw, "reason": "contact already in an active sequence"})
    conn.execute("UPDATE sequences SET cohort = cohort || %s WHERE id = %s", [Jsonb({"enrolled": enrolled, "excluded": excluded}), seq["id"]])
    audit(conn, "sequence", seq["id"], "created", details={"enrolled": len(enrolled), "excluded": len(excluded)})
    return get(conn, seq["id"])


def _exclusion(conn: Connection, ctx: dict) -> str | None:
    if ctx["stage"] in pipeline.BLOCKED_STAGES:
        return f"lead is {ctx['stage']}"
    email = ctx["contact"]["email"]
    if conn.execute("SELECT 1 FROM suppressions WHERE lower(email) = %s", [email]).fetchone():
        return "suppressed"
    if ctx["consent"] == "opted_out":
        return "opted out"
    country = (ctx["account"] or {}).get("country") or ""
    if country in policy(conn, "consent")["consentRequiredCountries"] and ctx["consent"] != "opted_in":
        return f"{country} contact without recorded consent"
    if ctx["account"] and conn.execute("SELECT 1 FROM opportunities WHERE account_id = %s AND status = 'open'", [ctx["account"]["id"]]).fetchone():
        return "account has an open opportunity"
    return None


def tick(conn: Connection, sequence_id: int, advance_hours: int) -> dict:
    """Advance the demo clock, then draft every due touch (each still needs approval and a send)."""
    if not conn.execute("SELECT 1 FROM sequences WHERE id = %s", [sequence_id]).fetchone():
        raise NotFound(f"sequence {sequence_id}")
    conn.execute("UPDATE demo_clock SET offset_hrs = offset_hrs + %s, updated_at = now() WHERE id = 1", [advance_hours])
    clock = _clock(conn)
    steps = conn.execute("SELECT * FROM sequence_steps WHERE sequence_id = %s ORDER BY step_number", [sequence_id]).fetchall()
    cadence = conn.execute("SELECT cohort FROM sequences WHERE id = %s", [sequence_id]).fetchone()["cohort"]["cadenceHours"]
    drafted, held, completed = [], [], []
    for e in conn.execute("SELECT * FROM enrollments WHERE sequence_id = %s AND state = 'active' AND next_due_at <= %s", [sequence_id, clock]).fetchall():
        if conn.execute("SELECT 1 FROM messages WHERE lead_id = %s AND kind = 'sequence_step' AND status IN ('pending_approval','approved')", [e["lead_id"]]).fetchone():
            held.append(str(e["lead_id"]))  # previous touch not yet approved/sent
            continue
        if e["next_step"] > len(steps):
            conn.execute("UPDATE enrollments SET state = 'completed', stop_reason = 'all touches sent' WHERE id = %s", [e["id"]])
            completed.append(str(e["lead_id"]))
            continue
        step = steps[e["next_step"] - 1]
        ctx = lead_context(conn, e["lead_id"])
        fact = conn.execute("SELECT statement FROM product_facts WHERE product_id = %s ORDER BY id LIMIT 1", [(ctx["product"] or {}).get("id")]).fetchone()
        theme = conn.execute("SELECT theme FROM sequences WHERE id = %s", [sequence_id]).fetchone()["theme"]
        vals = {"first_name": ctx["contact"]["firstName"], "title": ctx["contact"]["jobTitle"] or "a leader",
                "company": (ctx["account"] or {}).get("name") or "your organisation", "theme": theme, "theme_lower": theme.lower(),
                "fact": fact["statement"] if fact else ""}
        d = pipeline.create_draft(conn, e["lead_id"], "outbound", sequence_step={
            "subject": step["subject"].format(**vals), "body": step["body"].format(**vals) + engagement.FOOTER, "stepId": step["id"]})
        nxt = e["next_step"] + 1
        due = e["next_due_at"] - timedelta(hours=cadence[e["next_step"] - 1]) + timedelta(hours=cadence[nxt - 1]) if nxt <= len(steps) else clock
        conn.execute("UPDATE enrollments SET next_step = %s, next_due_at = %s WHERE id = %s", [nxt, due, e["id"]])
        drafted.append({"leadId": str(e["lead_id"]), "draftId": str(d["draftId"]), "step": step["step_number"]})
    return {"sequenceId": str(sequence_id), "clock": clock.isoformat(), "drafted": drafted, "held": held, "completed": completed}


def get(conn: Connection, sequence_id: int) -> dict:
    s = conn.execute("SELECT * FROM sequences WHERE id = %s", [sequence_id]).fetchone()
    if not s:
        raise NotFound(f"sequence {sequence_id}")
    steps = conn.execute("SELECT step_number, day_offset, subject, body FROM sequence_steps WHERE sequence_id = %s ORDER BY step_number", [sequence_id]).fetchall()
    enr = conn.execute("SELECT lead_id, state, next_step, next_due_at, stop_reason FROM enrollments WHERE sequence_id = %s ORDER BY id", [sequence_id]).fetchall()
    return {"sequenceId": str(s["id"]), "theme": s["theme"], "status": s["status"], "steps": steps,
            "excluded": s["cohort"].get("excluded", []),
            "enrollments": [{**e, "lead_id": str(e["lead_id"]), "next_due_at": e["next_due_at"].isoformat() if e["next_due_at"] else None} for e in enr]}


def list_all(conn: Connection) -> list[dict]:
    return [get(conn, r["id"]) for r in conn.execute("SELECT id FROM sequences ORDER BY id DESC").fetchall()]
