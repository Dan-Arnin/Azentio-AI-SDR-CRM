"""Durable CRM behind ai-sdr-engine (Level 2).

The engine POSTs signed envelopes {id, type, leadId, occurredAt, payload} to /v1/crm/events
(header X-SDR-Signature: sha256=<hmac of the raw body with CRM_WEBHOOK_SECRET>). Every envelope is kept
once (idempotent on its id) and folded into crm_leads; an AE handoff becomes an opportunity routed to the
right Account Executive by this backend's routing policy.
"""
import hashlib
import hmac
import json
import os
from datetime import datetime

from psycopg import Connection

from .db import Jsonb
from .engine.fixture import PERSONAL_DOMAINS
from .repo import Conflict, NotFound, audit, region_of

MAX_BODY = 2 * 1024 * 1024
OUTBOUND_SOURCES = {"target_account", "outbound"}
POSITIVE_INTENTS = {"interested"}
AGENT_EVENTS = {"lead.enriched", "lead.assessed", "draft.pending_approval", "task.sdr_followup", "alert.ae_handoff", "sequence.stopped"}
HUMAN_EVENTS = {"message.sent", "lead.owner_changed"}  # the engine only sends after a human approves the draft


class Unauthorized(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def verify(body: bytes, signature: str) -> None:
    """Fail closed: without a shared secret nobody can write CRM records."""
    secret = os.environ.get("CRM_WEBHOOK_SECRET", "")
    if not secret:
        raise Unauthorized("webhook_not_configured")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        raise Unauthorized("invalid_signature")


def receive(conn: Connection, body: bytes) -> dict:
    try:
        env = json.loads(body)
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("invalid_json")
    if (not isinstance(env, dict) or not isinstance(env.get("id"), str) or not 1 <= len(env["id"]) <= 200
            or not isinstance(env.get("type"), str) or not isinstance(env.get("payload", {}), dict)):
        raise ValueError("invalid_envelope")
    digest = hashlib.sha256(body).hexdigest()
    lead_id = env.get("leadId") or ((env.get("payload") or {}).get("lead") or {}).get("leadId")
    row = conn.execute("""INSERT INTO crm_events (id, type, engine_lead_id, occurred_at, payload, body_sha256)
                          VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING RETURNING id""",
                       [env["id"], env["type"], lead_id, _ts(env.get("occurredAt")), Jsonb(env.get("payload") or {}), digest]).fetchone()
    if not row:
        prev = conn.execute("SELECT body_sha256 FROM crm_events WHERE id = %s", [env["id"]]).fetchone()
        if prev["body_sha256"] != digest:
            raise Conflict("event_id_conflict", f"event {env['id']} was already received with a different body")
        return {"received": True, "id": env["id"], "duplicate": True}
    if lead_id:
        _project(conn, lead_id, env)
    return {"received": True, "id": env["id"], "duplicate": False}


def resync(conn: Connection, outbox: list[dict]) -> dict:
    """Ingest events from the engine's /v1/crm/outbox that never reached us (e.g. sent while this service was asleep).

    The envelope is rebuilt exactly as the engine serialises it, so events we already hold count as duplicates."""
    counts = {"imported": 0, "duplicates": 0, "conflicts": 0, "invalid": 0}
    for evt in sorted(outbox, key=lambda e: e.get("createdAt") or ""):
        env = {"id": evt.get("id"), "type": evt.get("type"), "leadId": evt.get("leadId"), "occurredAt": evt.get("createdAt"),
               "payload": evt.get("payload")}
        try:
            with conn.transaction():
                out = receive(conn, json.dumps(env, separators=(",", ":")).encode())
            counts["duplicates" if out["duplicate"] else "imported"] += 1
        except Conflict:
            counts["conflicts"] += 1
        except ValueError:
            counts["invalid"] += 1
    return counts


def _ts(value) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid_occurredAt")


def _domain(email: str | None) -> str | None:
    domain = (email or "").rpartition("@")[2].lower()
    return domain if domain and domain not in PERSONAL_DOMAINS else None


def _account(conn: Connection, snap: dict, institution_type: str | None = None) -> int | None:
    """Accounts are matched by company domain and never created from a personal mailbox."""
    domain = _domain(snap.get("email"))
    if not domain:
        return None
    country = snap.get("country")
    row = conn.execute("""INSERT INTO accounts (name, domain, country, region, institution_type) VALUES (%s,%s,%s,%s,%s)
                          ON CONFLICT (domain) DO UPDATE SET institution_type = COALESCE(accounts.institution_type, EXCLUDED.institution_type)
                          RETURNING id""",
                       [snap.get("companyName") or domain, domain, country, region_of(conn, country), institution_type]).fetchone()
    return row["id"]


def route_ae(conn: Connection, country: str | None, email_or_domain: str | None) -> dict:
    """Existing account owner first, then the AE for the country's region, then the International AE."""
    domain = _domain(email_or_domain if "@" in (email_or_domain or "") else f"x@{email_or_domain or ''}")
    acct = conn.execute("SELECT a.owner_id, a.name FROM accounts a WHERE a.domain = %s AND a.owner_id IS NOT NULL", [domain]).fetchone() if domain else None
    if acct:
        ae, reason = acct["owner_id"], f"existing owner of account {acct['name']}"
    else:
        region = region_of(conn, country)
        row = conn.execute("SELECT id FROM users WHERE role = 'sales_rep' AND region = %s ORDER BY id LIMIT 1", [region]).fetchone() if region else None
        if row:
            ae, reason = row["id"], f"{region} region AE for country {country}"
        else:
            row = conn.execute("SELECT id FROM users WHERE role = 'sales_rep' AND region = 'International' ORDER BY id LIMIT 1").fetchone()
            ae, reason = (row["id"] if row else None), f"no regional AE for country {country or 'unknown'}; International AE"
    user = conn.execute("SELECT id, name, email, region FROM users WHERE id = %s", [ae]).fetchone() if ae else None
    if not user:
        raise NotFound("no sales_rep users configured")
    return {"aeId": str(user["id"]), "name": user["name"], "email": user["email"], "region": user["region"],
            "ownerKey": "ae." + user["email"].split(".")[0], "reason": reason}


def _project(conn: Connection, lead_id: str, env: dict) -> None:
    kind, payload, at = env["type"], env.get("payload") or {}, _ts(env.get("occurredAt"))
    snap = payload.get("lead") or {}
    draft = payload.get("draft") or {}
    outbound = (snap.get("source") in OUTBOUND_SOURCES or kind == "sequence.stopped" or bool(payload.get("enrollmentId"))
                or bool(draft.get("enrollmentId")) or str(payload.get("kind") or draft.get("kind") or "").startswith("outbound_"))
    conn.execute("INSERT INTO crm_leads (engine_lead_id) VALUES (%s) ON CONFLICT DO NOTHING", [lead_id])
    if snap:
        conn.execute("""UPDATE crm_leads SET engine_version = %(v)s, full_name = %(fullName)s, email = %(email)s,
                          company_name = %(companyName)s, country = %(country)s, region = %(region)s, product_key = %(productId)s,
                          source = %(source)s, stage = %(stage)s, fit = %(fit)s, icp_score = %(icpScore)s,
                          qualification_score = %(qualificationScore)s,
                          sdr_owner = CASE WHEN %(ownerId)s LIKE 'sdr.%%' THEN %(ownerId)s ELSE sdr_owner END
                        WHERE engine_lead_id = %(id)s AND engine_version <= %(v)s""",
                     {**{k: snap.get(k) for k in ("fullName", "email", "companyName", "country", "productId", "source", "stage",
                                                  "fit", "icpScore", "qualificationScore")},
                      "ownerId": snap.get("ownerId") or "", "v": snap.get("version") or 0, "id": lead_id,
                      "region": region_of(conn, snap.get("country"))})
    enrichment = payload.get("enrichment") or {}
    acct = _account(conn, snap, (enrichment.get("company") or {}).get("institutionType")) if snap else None
    sets = {"account_id": acct} if acct else {}
    if outbound:
        sets["motion"] = "outbound"
    if kind == "lead.enriched" and (title := (enrichment.get("person") or {}).get("title")):
        sets["title"] = title
    elif kind == "lead.assessed" and (asm := payload.get("assessment")):
        sets.update(bant=Jsonb(asm.get("bant") or {}), route=asm.get("route"))
        if asm.get("replyIntent"):
            sets["reply_intent"] = asm["replyIntent"]
    elif kind == "lead.suppressed":
        sets["suppressed"] = True
    elif kind == "alert.ae_handoff":
        sets.update(_handoff(conn, lead_id, snap, payload, acct))
    elif kind == "lead.erased":
        _erase(conn, lead_id, env["id"])
        sets.update(full_name="Erased contact", email=None, title=None, bant=None)
    bumps ={"draft.pending_approval": "emails_drafted", "message.sent": "emails_sent", "reply.received": "replies"}
    assignments = [f"{col} = %s" for col in sets] + ([f"{bumps[kind]} = {bumps[kind]} + 1"] if kind in bumps else [])
    assignments += ["last_event_type = CASE WHEN last_event_at IS NULL OR last_event_at <= %s THEN %s ELSE last_event_type END",
                    "last_event_at = GREATEST(last_event_at, %s)", "updated_at = now()"]
    conn.execute(f"UPDATE crm_leads SET {', '.join(assignments)} WHERE engine_lead_id = %s", [*sets.values(), at, kind, at, lead_id])


def _erase(conn: Connection, lead_id: str, erase_event_id: str) -> None:
    """A data-subject erasure in the engine: drop the personal data we hold for that lead (event bodies keep their hash for idempotency)."""
    conn.execute("UPDATE crm_events SET payload = %s WHERE engine_lead_id = %s AND id <> %s", [Jsonb({"erased": True}), lead_id, erase_event_id])
    conn.execute("UPDATE opportunities SET bant_snapshot = NULL WHERE external_ref = %s", [f"engine:{lead_id}"])
    audit(conn, "crm_lead", None, "erased_from_engine_request", "system", "ai-sdr-engine", {"engineLeadId": lead_id})


def _handoff(conn: Connection, lead_id: str, snap: dict, payload: dict, acct: int | None) -> dict:
    """Qualified lead from the engine: route to the right AE, open (or refresh) one opportunity, protect the account owner."""
    ae = route_ae(conn, snap.get("country"), snap.get("email"))
    card = payload.get("card") or {}
    if acct is None:  # no company domain (personal mailbox): keep the opportunity on a name-only account
        row = conn.execute("SELECT id FROM accounts WHERE domain IS NULL AND lower(name) = lower(%s) LIMIT 1", [snap.get("companyName")]).fetchone() \
            or conn.execute("INSERT INTO accounts (name, country, region) VALUES (%s,%s,%s) RETURNING id",
                            [snap.get("companyName") or "Unknown company", snap.get("country"), region_of(conn, snap.get("country"))]).fetchone()
        acct = row["id"]
    conn.execute("UPDATE accounts SET owner_id = %s WHERE id = %s AND owner_id IS NULL", [int(ae["aeId"]), acct])
    opp = conn.execute("""INSERT INTO opportunities (external_ref, account_id, owner_id, name, stage, status, bant_snapshot, product_key, routing_reason)
                          VALUES (%s,%s,%s,%s,'qualified','open',%s,%s,%s)
                          ON CONFLICT (external_ref) DO UPDATE SET bant_snapshot = EXCLUDED.bant_snapshot
                          RETURNING id, (xmax = 0) AS created""",
                       [f"engine:{lead_id}", acct, int(ae["aeId"]), f"{snap.get('companyName') or 'Unknown'} - {snap.get('productId') or 'product'}",
                        Jsonb(card), snap.get("productId"), ae["reason"]]).fetchone()
    if opp["created"]:
        audit(conn, "opportunity", opp["id"], "created_from_engine_handoff", "agent", "ai-sdr-engine",
              {"engineLeadId": lead_id, "engineOwner": payload.get("ownerId"), "routedTo": ae["name"], "reason": ae["reason"]})
    return {"ae_owner_id": int(ae["aeId"]), "routing_reason": ae["reason"]}


def list_leads(conn: Connection, motion: str | None = None) -> list[dict]:
    return conn.execute("""SELECT l.*, u.name AS ae_owner, a.name AS account_name FROM crm_leads l
                           LEFT JOIN users u ON u.id = l.ae_owner_id LEFT JOIN accounts a ON a.id = l.account_id
                           WHERE %(m)s::text IS NULL OR l.motion = %(m)s ORDER BY l.last_event_at DESC NULLS LAST""", {"m": motion}).fetchall()


def get_lead(conn: Connection, lead_id: str) -> dict:
    rows = [r for r in list_leads(conn) if r["engine_lead_id"] == lead_id]
    if not rows:
        raise NotFound(f"crm lead {lead_id}")
    timeline = conn.execute("SELECT id, type, occurred_at, payload FROM crm_events WHERE engine_lead_id = %s ORDER BY occurred_at, received_at",
                            [lead_id]).fetchall()
    opp = conn.execute("SELECT * FROM opportunities WHERE external_ref = %s", [f"engine:{lead_id}"]).fetchone()
    return {"lead": rows[0], "opportunity": opp, "timeline": timeline}


def list_opportunities(conn: Connection) -> list[dict]:
    return conn.execute("""SELECT o.*, a.name AS account_name, u.name AS owner FROM opportunities o
                           JOIN accounts a ON a.id = o.account_id LEFT JOIN users u ON u.id = o.owner_id
                           ORDER BY o.created_at DESC""").fetchall()


def metrics(conn: Connection) -> dict:
    """Campaign performance, split by inbound vs outbound motion."""
    rows = conn.execute("""
        SELECT l.motion, count(*) AS leads, sum(emails_drafted) AS drafted, sum(emails_sent) AS sent,
               count(*) FILTER (WHERE emails_sent > 0) AS contacted, count(*) FILTER (WHERE replies > 0) AS replied,
               count(*) FILTER (WHERE reply_intent = ANY(%s)) AS positive,
               count(*) FILTER (WHERE route = 'rep_review' OR o.id IS NOT NULL) AS qualified, count(o.id) AS opportunities
        FROM crm_leads l LEFT JOIN opportunities o ON o.external_ref = 'engine:' || l.engine_lead_id
        GROUP BY l.motion""", [list(POSITIVE_INTENTS)]).fetchall()
    empty = dict(leads=0, drafted=0, sent=0, contacted=0, replied=0, positive=0, qualified=0, opportunities=0)
    by_motion = {m: {**empty} for m in ("inbound", "outbound")}
    for r in rows:
        by_motion[r["motion"]] = {k: int(r[k] or 0) for k in empty}
    for m in by_motion.values():
        m["replyRate"] = round(m["replied"] / m["contacted"], 3) if m["contacted"] else None
    total = {k: sum(m[k] for m in by_motion.values()) for k in empty}
    total["replyRate"] = round(total["replied"] / total["contacted"], 3) if total["contacted"] else None
    counts = {r["type"]: r["n"] for r in conn.execute("SELECT type, count(*) AS n FROM crm_events GROUP BY type").fetchall()}
    by_ae = conn.execute("""SELECT u.name, u.region, count(*) AS opportunities FROM opportunities o JOIN users u ON u.id = o.owner_id
                            WHERE o.external_ref LIKE 'engine:%%' GROUP BY u.name, u.region ORDER BY 3 DESC""").fetchall()
    return {
        "inbound": by_motion["inbound"], "outbound": by_motion["outbound"], "total": total,
        "agentDecisions": {k: counts[k] for k in sorted(AGENT_EVENTS) if k in counts},
        "humanInterventions": {"approvedSends": counts.get("message.sent", 0), "ownerChanges": counts.get("lead.owner_changed", 0)},
        "escalatedToHuman": counts.get("task.sdr_followup", 0),
        "opportunitiesByAe": by_ae,
        "eventsReceived": sum(counts.values()),
        "notes": ["'sent' counts approved sends: the engine only sends after a human approves the draft.",
                  "A lead is outbound when it came from a target-account source or was enrolled in a campaign sequence."],
    }
