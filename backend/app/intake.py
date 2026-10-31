"""Lead intake (workstream B): validate, deduplicate, link to accounts, persist, gate contact.
Agents never write here directly; they only see leads that intake has accepted."""
import csv
import io
from typing import Any

from psycopg import Connection
from pydantic import ValidationError

from . import pipeline
from .contracts import PERMISSION_TO_CONSENT, SOURCE_TO_DB, ImportRowResult, LeadCreated, LeadInput
from .db import Jsonb
from .repo import Conflict, audit, policy, region_of, set_stage

CONTEXT_FIELDS = ("eventName", "campaignId", "topic", "asset", "submittedAt", "role", "companyDomain", "existingOpportunityId", "sourceRecordId")


class InvalidInput(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split()
    return (parts[0], " ".join(parts[1:]) or "-") if parts else ("-", "-")


def create_lead(conn: Connection, inp: LeadInput, idempotency_key: str | None = None, auto_run: bool = False) -> LeadCreated:
    guard = policy(conn, "guardrail")
    source = SOURCE_TO_DB.get(inp.source, inp.source)
    email = inp.email.lower()
    payload = inp.model_dump()

    # 1. Idempotent replay: same key or same (source, sourceRecordId) returns the original lead.
    if idempotency_key:
        prev = conn.execute("SELECT lead_id FROM ingestion_events WHERE idempotency_key = %s", [idempotency_key]).fetchone()
        if prev and prev["lead_id"]:
            return _existing(conn, prev["lead_id"], "Same Idempotency-Key already processed.")
    prev = conn.execute("SELECT id FROM leads WHERE source = %s AND source_record_id = %s", [source, inp.sourceRecordId]).fetchone()
    if prev:
        return _existing(conn, prev["id"], "Same source record already imported.")

    product = conn.execute("SELECT id, slug FROM products WHERE slug = %s AND active", [inp.productId]).fetchone()
    if not product:
        raise InvalidInput([f"unknown productId {inp.productId!r}"])

    # 2. Account: match on a validated company domain; never invent one from a personal mailbox.
    domain = (inp.companyDomain or email.split("@")[-1]).lower()
    personal = domain in guard["personalEmailDomains"]
    account = None
    if not personal:
        account = conn.execute("SELECT * FROM accounts WHERE domain = %s", [domain]).fetchone()
        if not account:
            account = conn.execute(
                "INSERT INTO accounts (name, domain, institution_type, country, region, employee_count) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                [inp.companyName, domain, inp.institutionType, inp.country.upper(), region_of(conn, inp.country), inp.employeeCount]).fetchone()

    # 3. Contact: exact email match; a phone that belongs to someone else is an identity conflict, not a merge.
    contact = conn.execute("SELECT * FROM contacts WHERE lower(email) = %s", [email]).fetchone()
    if inp.phone:
        other = conn.execute("SELECT id FROM contacts WHERE phone = %s AND lower(email) <> %s", [inp.phone, email]).fetchone()
        if other:
            err = Conflict("identity_conflict", f"phone belongs to contact {other['id']} with a different email; quarantined for review")
            err.quarantine = {"source": source, "payload": payload, "phone_matches_contact": other["id"],
                              "idempotency_key": idempotency_key, "source_record_id": inp.sourceRecordId}
            raise err
    consent = PERMISSION_TO_CONSENT[inp.contactPermission]
    if not contact:
        first, last = _split_name(inp.fullName)
        contact = conn.execute(
            "INSERT INTO contacts (account_id, first_name, last_name, email, phone, job_title, consent_status) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            [account["id"] if account else None, first, last, email, inp.phone, inp.role, consent]).fetchone()
    elif consent != "unknown" and contact["consent_status"] != consent:
        conn.execute("UPDATE contacts SET consent_status = %s WHERE id = %s", [consent, contact["id"]])
        contact["consent_status"] = consent

    # 4. Duplicate lead: one live lead per contact and product. Attach the event to it instead of creating another.
    live = conn.execute("SELECT id FROM leads WHERE contact_id = %s AND product_id = %s AND duplicate_of_lead_id IS NULL",
                        [contact["id"], product["id"]]).fetchone()
    if live:
        conn.execute("INSERT INTO ingestion_events (source, raw_payload, status, lead_id, dedupe_result, idempotency_key, source_record_id) "
                     "VALUES (%s,%s,'duplicate',%s,%s,%s,%s)",
                     [source, Jsonb(payload), live["id"], Jsonb({"match_type": "exact_email", "matched_lead_id": live["id"]}),
                      idempotency_key, inp.sourceRecordId])
        audit(conn, "lead", live["id"], "duplicate_submission", details={"source": source})
        return _existing(conn, live["id"], "Existing lead for this contact and product; new submission attached as an event.", "duplicate")

    context = {k: payload[k] for k in CONTEXT_FIELDS if payload.get(k)}
    lead = conn.execute(
        "INSERT INTO leads (contact_id, account_id, product_id, source, enquiry_text, source_record_id, source_context) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        [contact["id"], account["id"] if account else None, product["id"], source, inp.inquiry, inp.sourceRecordId, Jsonb(context)]).fetchone()
    lead_id = lead["id"]
    conn.execute("INSERT INTO ingestion_events (source, raw_payload, status, lead_id, idempotency_key, source_record_id) VALUES (%s,%s,'processed',%s,%s,%s)",
                 [source, Jsonb(payload), lead_id, idempotency_key, inp.sourceRecordId])
    if account and conn.execute("SELECT count(*) AS n FROM leads WHERE account_id = %s", [account["id"]]).fetchone()["n"] > 1 or (account and account["is_customer"]):
        conn.execute("INSERT INTO duplicate_matches (lead_id, matched_account_id, match_type, confidence, resolution) VALUES (%s,%s,'domain_account',0.95,'linked_account')",
                     [lead_id, account["id"]])
    audit(conn, "lead", lead_id, "created", details={"source": source, "product": product["slug"]})

    # 5. Contact gates. Checked here so nothing downstream can draft to a blocked contact.
    reasons = _gate(conn, lead_id, email, domain, contact["consent_status"], inp.country, account, product["id"], inp.existingOpportunityId)
    run_id = None
    if not reasons and auto_run:
        run_id = pipeline.process(conn, lead_id)
    stage = conn.execute("SELECT stage FROM leads WHERE id = %s", [lead_id]).fetchone()["stage"]
    return LeadCreated(leadId=str(lead_id), disposition="blocked" if reasons else "created",
                       runId=str(run_id) if run_id else None, stage=stage, reasons=reasons)


def record_quarantine(conn: Connection, q: dict) -> None:
    """Called by the route after the failed request rolled back, so the conflicting submission is still kept."""
    conn.execute("INSERT INTO ingestion_events (source, raw_payload, status, dedupe_result, source_record_id) VALUES (%s,%s,'failed',%s,%s)",
                 [q["source"], Jsonb(q["payload"]), Jsonb({"identity_conflict": {"phone_matches_contact": q["phone_matches_contact"]}}),
                  q["source_record_id"]])


def _existing(conn: Connection, lead_id: int, reason: str, disposition: str = "existing") -> LeadCreated:
    stage = conn.execute("SELECT stage FROM leads WHERE id = %s", [lead_id]).fetchone()["stage"]
    return LeadCreated(leadId=str(lead_id), disposition=disposition, stage=stage, reasons=[reason])


def _gate(conn: Connection, lead_id: int, email: str, domain: str, consent: str, country: str, account: dict | None,
          product_id: int, existing_opp_ref: str | None) -> list[str]:
    sup = conn.execute("SELECT reason FROM suppressions WHERE lower(email) = %s OR domain = %s LIMIT 1", [email, domain]).fetchone()
    if sup:
        set_stage(conn, lead_id, "suppressed", sup["reason"], "system", route="suppress", outcome_reason=f"Suppressed: {sup['reason']}")
        return [f"Suppressed: {sup['reason']}"]
    if consent == "opted_out":
        set_stage(conn, lead_id, "blocked", "contact opted out", "system", route="suppress", outcome_reason="Contact opted out")
        return ["Contact opted out of marketing."]
    if country.upper() in policy(conn, "consent")["consentRequiredCountries"] and consent != "opted_in":
        set_stage(conn, lead_id, "blocked", "consent required", "system", outcome_reason=f"{country} requires recorded consent (GDPR)")
        return [f"{country} contact without recorded consent (GDPR); outreach blocked."]
    opp = None
    if existing_opp_ref:
        opp = conn.execute("SELECT * FROM opportunities WHERE external_ref = %s AND status = 'open'", [existing_opp_ref]).fetchone()
    if not opp and account:
        opp = conn.execute("SELECT * FROM opportunities WHERE account_id = %s AND status = 'open' ORDER BY id LIMIT 1", [account["id"]]).fetchone()
    if opp:
        why = f"Account has open opportunity '{opp['name']}'; owner decides how to engage. No automated outreach."
        set_stage(conn, lead_id, "existing_opportunity", why, "system", route="existing_opportunity", outcome_reason=why,
                  owner_id=opp["owner_id"], ready_for_followup=True)
        conn.execute("INSERT INTO next_actions (lead_id, action_type, description, assigned_to) VALUES (%s,'account_owner_review',%s,%s)",
                     [lead_id, why, opp["owner_id"]])
        return [why]
    return []


# ───────────── imports ─────────────

def parse_csv(text: str) -> list[dict[str, Any]]:
    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k}
        row = {k: v for k, v in row.items() if v not in ("", None)}
        if "employeeCount" in row:
            try:
                row["employeeCount"] = int(row["employeeCount"])
            except ValueError:
                pass  # left as-is so validation reports it
        rows.append(row)
    return rows


def run_import(conn: Connection, rows: list[dict[str, Any]], source: str | None, filename: str | None, auto_run: bool) -> dict:
    imp = conn.execute("INSERT INTO imports (source, filename, total_rows) VALUES (%s,%s,%s) RETURNING id", [source, filename, len(rows)]).fetchone()
    results: list[ImportRowResult] = []
    for i, row in enumerate(rows, start=1):
        if source and "source" not in row:
            row = {**row, "source": source}
        row.setdefault("sourceRecordId", f"import-{imp['id']}-row-{i}")
        try:
            with conn.transaction():  # savepoint: one bad row never undoes the good ones
                created = create_lead(conn, LeadInput.model_validate(row), auto_run=auto_run)
            status = "created" if created.disposition in ("created", "blocked") else "duplicate"
            results.append(ImportRowResult(row=i, status=status, leadId=created.leadId, errors=created.reasons if status == "duplicate" else []))
        except ValidationError as e:
            results.append(ImportRowResult(row=i, status="rejected", errors=[f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]))
        except (InvalidInput, Conflict) as e:
            results.append(ImportRowResult(row=i, status="rejected", errors=getattr(e, "errors", None) or [str(e)]))
        last = results[-1]
        conn.execute("INSERT INTO import_rows (import_id, row_number, raw, status, lead_id, errors) VALUES (%s,%s,%s,%s,%s,%s)",
                     [imp["id"], i, Jsonb(row), last.status, int(last.leadId) if last.leadId else None, Jsonb(last.errors)])
    accepted = sum(r.status != "rejected" for r in results)
    conn.execute("UPDATE imports SET status='completed', accepted_rows=%s, rejected_rows=%s WHERE id=%s", [accepted, len(rows) - accepted, imp["id"]])
    return get_import(conn, imp["id"])


def get_import(conn: Connection, import_id: int) -> dict:
    imp = conn.execute("SELECT * FROM imports WHERE id = %s", [import_id]).fetchone()
    if not imp:
        from .repo import NotFound
        raise NotFound(f"import {import_id}")
    rows = conn.execute("SELECT row_number, status, lead_id, errors FROM import_rows WHERE import_id = %s ORDER BY row_number", [import_id]).fetchall()
    return {"importId": str(imp["id"]), "status": imp["status"], "total": imp["total_rows"], "accepted": imp["accepted_rows"],
            "rejected": imp["rejected_rows"],
            "rows": [{"row": r["row_number"], "status": r["status"], "leadId": str(r["lead_id"]) if r["lead_id"] else None, "errors": r["errors"]} for r in rows]}
