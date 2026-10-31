"""Shared persistence helpers: audit log, stage history, agent runs, policies, lead context."""
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from psycopg import Connection

from .db import Jsonb


class NotFound(Exception):
    pass


class Conflict(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def now() -> datetime:
    return datetime.now(timezone.utc)


def audit(conn: Connection, entity: str, entity_id: int | None, action: str, actor_type: str = "system",
          actor: str | None = None, details: dict | None = None) -> None:
    conn.execute("INSERT INTO audit_log (entity_type, entity_id, action, actor_type, actor, details) VALUES (%s,%s,%s,%s,%s,%s)",
                 [entity, entity_id, action, actor_type, actor, Jsonb(details or {})])


def set_stage(conn: Connection, lead_id: int, stage: str, reason: str, by_type: str = "agent", by: str | None = None, **cols) -> None:
    row = conn.execute("SELECT stage FROM leads WHERE id = %s", [lead_id]).fetchone()
    sets = ", ".join(f"{k} = %s" for k in cols)
    conn.execute(f"UPDATE leads SET stage = %s, updated_at = now(), version = version + 1{', ' + sets if sets else ''} WHERE id = %s",
                 [stage, *cols.values(), lead_id])
    if row["stage"] != stage:
        conn.execute("INSERT INTO lead_stage_history (lead_id, from_stage, to_stage, changed_by_type, changed_by, reason) VALUES (%s,%s,%s,%s,%s,%s)",
                     [lead_id, row["stage"], stage, by_type, by, reason])


def policy(conn: Connection, kind: str) -> dict:
    row = conn.execute("SELECT content FROM policies WHERE kind = %s AND active", [kind]).fetchone()
    if not row:
        raise NotFound(f"no active {kind} policy")
    return row["content"]


@contextmanager
def agent_run(conn: Connection, agent_key: str, lead_id: int | None, trigger: str, input_: dict | None = None):
    """Records one agent invocation in agent_runs. Yields a dict; set ["output"] / ["summary"] inside the block.
    A failure is recorded as status=failed and re-raised, so the lead is never silently lost."""
    agent = conn.execute("SELECT id, current_version_id FROM agents WHERE key = %s", [agent_key]).fetchone()
    started = now()
    t0 = time.perf_counter()
    run = conn.execute("INSERT INTO agent_runs (agent_id, agent_version_id, lead_id, trigger, status, input, started_at) "
                       "VALUES (%s,%s,%s,%s,'running',%s,%s) RETURNING id",
                       [agent["id"], agent["current_version_id"], lead_id, trigger, Jsonb(input_ or {}), started]).fetchone()
    box = {"id": run["id"], "output": None, "summary": None, "status": "succeeded"}
    try:
        yield box
    except Exception as e:
        conn.execute("UPDATE agent_runs SET status='failed', error=%s, finished_at=%s, duration_ms=%s WHERE id=%s",
                     [f"{type(e).__name__}: {e}", now(), int((time.perf_counter() - t0) * 1000), run["id"]])
        raise
    conn.execute("UPDATE agent_runs SET status=%s, output=%s, summary=%s, finished_at=%s, duration_ms=%s WHERE id=%s",
                 [box["status"], Jsonb(box["output"]), box["summary"], now(), int((time.perf_counter() - t0) * 1000), run["id"]])


def lead_context(conn: Connection, lead_id: int) -> dict:
    l = conn.execute("""
        SELECT l.*, c.first_name, c.last_name, c.email, c.job_title, c.seniority, c.phone, c.consent_status,
               a.name AS a_name, a.domain, a.industry, a.institution_type, a.country, a.region, a.employee_count, a.owner_id AS a_owner,
               a.is_customer, p.slug, p.name AS p_name, p.keywords
        FROM leads l JOIN contacts c ON c.id = l.contact_id
        LEFT JOIN accounts a ON a.id = l.account_id LEFT JOIN products p ON p.id = l.product_id
        WHERE l.id = %s""", [lead_id]).fetchone()
    if not l:
        raise NotFound(f"lead {lead_id}")
    enr = conn.execute("SELECT data FROM enrichment_records WHERE lead_id = %s ORDER BY id DESC LIMIT 1", [lead_id]).fetchone()
    dept = ((enr or {}).get("data") or {}).get("department", {}).get("value") if enr else None
    return {
        "leadId": l["id"], "source": l["source"], "sourceContext": l["source_context"], "enquiry": l["enquiry_text"],
        "stage": l["stage"], "version": l["version"], "consent": l["consent_status"],
        "contact": {"firstName": l["first_name"], "lastName": l["last_name"], "email": l["email"], "jobTitle": l["job_title"],
                    "seniority": l["seniority"], "phone": l["phone"], "department": dept},
        "account": None if l["account_id"] is None else {
            "id": l["account_id"], "name": l["a_name"], "domain": l["domain"], "industry": l["industry"],
            "institutionType": l["institution_type"], "country": l["country"], "region": l["region"],
            "employeeCount": l["employee_count"], "ownerId": l["a_owner"], "isCustomer": l["is_customer"]},
        "product": None if l["product_id"] is None else {"id": l["product_id"], "slug": l["slug"], "name": l["p_name"], "keywords": l["keywords"]},
    }


def region_of(conn: Connection, country: str | None) -> str | None:
    if not country:
        return None
    for region, countries in policy(conn, "routing")["regions"].items():
        if country.upper() in countries:
            return region
    return None


def owner_for(conn: Connection, ctx: dict) -> int | None:
    """Existing account owner first; otherwise the regional AE; otherwise the international AE."""
    acct = ctx.get("account") or {}
    if acct.get("ownerId"):
        return acct["ownerId"]
    row = conn.execute("SELECT id FROM users WHERE role='sales_rep' AND region = %s ORDER BY id LIMIT 1", [acct.get("region")]).fetchone() \
        or conn.execute("SELECT id FROM users WHERE role='sales_rep' AND region = 'International' LIMIT 1").fetchone()
    return row["id"] if row else None


def user_id(conn: Connection, role: str) -> int | None:
    row = conn.execute("SELECT id FROM users WHERE role = %s ORDER BY id LIMIT 1", [role]).fetchone()
    return row["id"] if row else None
