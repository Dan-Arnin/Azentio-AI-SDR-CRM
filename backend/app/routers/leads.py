from fastapi import APIRouter, Depends, File, Form, Header, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from psycopg import Connection

from .. import intake, pipeline
from ..contracts import ImportInput, ImportResult, LeadCreated, LeadInput, LeadSummary
from ..db import get_conn
from ..repo import Conflict, NotFound, lead_context

router = APIRouter(prefix="/v1", tags=["leads"])


@router.post("/leads", response_model=LeadCreated)
def create_lead(inp: LeadInput, response: Response, autoRun: bool = False,
                idempotency_key: str | None = Header(None, alias="idempotency-key"), conn: Connection = Depends(get_conn)):
    try:
        out = intake.create_lead(conn, inp, idempotency_key, autoRun)
    except Conflict as e:
        if getattr(e, "quarantine", None):
            conn.rollback()
            intake.record_quarantine(conn, e.quarantine)
            conn.commit()
        raise
    response.status_code = 201 if out.disposition in ("created", "blocked") else 200
    return out


@router.post("/leads/import", response_model=ImportResult)
def import_json(inp: ImportInput, autoRun: bool = False, conn: Connection = Depends(get_conn)):
    return intake.run_import(conn, inp.leads, None, "json", autoRun)


@router.post("/imports", response_model=ImportResult)
async def import_csv(file: UploadFile = File(...), source: str | None = Form(None), autoRun: bool = False, conn: Connection = Depends(get_conn)):
    raw = await file.read()
    if len(raw) > 2_000_000:
        raise intake.InvalidInput(["file larger than 2 MB"])
    rows = intake.parse_csv(raw.decode("utf-8-sig"))
    if len(rows) > 100:
        raise intake.InvalidInput([f"{len(rows)} rows; limit is 100 per import"])
    # blocking DB work stays off the event loop, or a concurrent request waiting on our row locks would stall it
    return await run_in_threadpool(intake.run_import, conn, rows, source, file.filename, autoRun)


@router.get("/imports/{import_id}", response_model=ImportResult)
def get_import(import_id: int, conn: Connection = Depends(get_conn)):
    return intake.get_import(conn, import_id)


@router.get("/leads", response_model=list[LeadSummary])
def list_leads(source: str | None = None, stage: str | None = None, icpStatus: str | None = None, route: str | None = None,
               icpFit: str | None = None, bantStatus: str | None = None, readyForFollowup: bool | None = None,
               productId: str | None = None, q: str | None = None, cursor: int | None = None,
               limit: int = Query(50, ge=1, le=50), conn: Connection = Depends(get_conn)):
    where, args = [], []
    for col, val in [("l.source", source), ("l.stage", stage), ("l.route", route), ("l.icp_fit", icpFit or icpStatus),
                     ("l.bant_status", bantStatus), ("p.slug", productId)]:
        if val:
            where.append(f"{col} = %s")
            args.append(val)
    if readyForFollowup is not None:
        where.append("l.ready_for_followup = %s")
        args.append(readyForFollowup)
    if q:
        where.append("(c.email ILIKE %s OR a.name ILIKE %s OR (c.first_name || ' ' || c.last_name) ILIKE %s)")
        args += [f"%{q}%"] * 3
    if cursor:
        where.append("l.id < %s")
        args.append(cursor)
    rows = conn.execute(f"""
        SELECT l.*, c.first_name, c.last_name, c.email, a.name AS company, a.country, p.slug, u.name AS owner,
               (SELECT description FROM next_actions n WHERE n.lead_id = l.id AND n.status = 'open' ORDER BY n.id DESC LIMIT 1) AS next_action
        FROM leads l JOIN contacts c ON c.id = l.contact_id LEFT JOIN accounts a ON a.id = l.account_id
        LEFT JOIN products p ON p.id = l.product_id LEFT JOIN users u ON u.id = l.owner_id
        {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY l.id DESC LIMIT %s""", [*args, limit]).fetchall()
    return [LeadSummary(leadId=str(r["id"]), fullName=f"{r['first_name']} {r['last_name']}", email=r["email"], company=r["company"],
                        country=r["country"], source=r["source"], product=r["slug"], stage=r["stage"], route=r["route"],
                        icpScore=r["icp_score"], icpFit=r["icp_fit"], bantScore=r["bant_score"], bantStatus=r["bant_status"],
                        readyForFollowup=r["ready_for_followup"], owner=r["owner"], nextAction=r["next_action"],
                        createdAt=r["created_at"].isoformat()) for r in rows]


@router.get("/leads/{lead_id}")
def get_lead(lead_id: int, conn: Connection = Depends(get_conn)):
    ctx = lead_context(conn, lead_id)
    q = lambda sql: conn.execute(sql, [lead_id]).fetchall()  # noqa: E731
    lead = conn.execute("SELECT l.*, u.name AS owner FROM leads l LEFT JOIN users u ON u.id = l.owner_id WHERE l.id = %s", [lead_id]).fetchone()
    assessments = q("SELECT * FROM bant_assessments WHERE lead_id = %s ORDER BY id")
    for a in assessments:
        a["signals"] = conn.execute("SELECT dimension, status, value, evidence_quote, evidence_source, quote_verified FROM bant_signals "
                                    "WHERE assessment_id = %s ORDER BY id", [a["id"]]).fetchall()
    return {
        "lead": lead, "context": ctx,
        "enrichment": q("SELECT * FROM enrichment_records WHERE lead_id = %s ORDER BY id DESC LIMIT 1"),
        "icp": q("SELECT * FROM icp_evaluations WHERE lead_id = %s ORDER BY id DESC LIMIT 1"),
        "messages": q("SELECT m.*, c.thread_id FROM messages m JOIN conversations c ON c.id = m.conversation_id WHERE m.lead_id = %s ORDER BY m.id"),
        "approvals": q("SELECT a.* FROM approvals a JOIN messages m ON m.id = a.message_id WHERE m.lead_id = %s ORDER BY a.id"),
        "assessments": assessments,
        "nextActions": q("SELECT * FROM next_actions WHERE lead_id = %s ORDER BY id"),
        "handoffs": q("SELECT * FROM handoffs WHERE lead_id = %s ORDER BY id"),
        "duplicates": q("SELECT * FROM duplicate_matches WHERE lead_id = %s"),
        "ingestionEvents": q("SELECT id, source, status, dedupe_result, received_at FROM ingestion_events WHERE lead_id = %s ORDER BY id"),
        "stageHistory": q("SELECT * FROM lead_stage_history WHERE lead_id = %s ORDER BY id"),
        "runs": q("SELECT r.id, ag.key AS agent, r.status, r.summary, r.duration_ms, r.started_at FROM agent_runs r "
                  "JOIN agents ag ON ag.id = r.agent_id WHERE r.lead_id = %s ORDER BY r.id"),
        "guardrails": q("SELECT rule, result, details, message_id, at FROM guardrail_events WHERE lead_id = %s ORDER BY id"),
    }


@router.post("/leads/{lead_id}/enrich")
def enrich(lead_id: int, conn: Connection = Depends(get_conn)):
    return {"leadId": str(lead_id), "runId": str(pipeline.enrich(conn, lead_id))}


@router.post("/leads/{lead_id}/assess")
def assess(lead_id: int, conn: Connection = Depends(get_conn)):
    """Re-run ICP scoring (e.g. after a config change). Reply assessment happens on POST /v1/replies."""
    if not lead_context(conn, lead_id)["product"]:
        raise NotFound("lead has no product")
    run_id, icp = pipeline.score(conn, lead_id)
    return {"leadId": str(lead_id), "runId": str(run_id), "icp": icp}


@router.post("/leads/{lead_id}/process")
def process(lead_id: int, conn: Connection = Depends(get_conn)):
    run_id = pipeline.process(conn, lead_id)
    stage = conn.execute("SELECT stage FROM leads WHERE id = %s", [lead_id]).fetchone()["stage"]
    return {"leadId": str(lead_id), "runId": str(run_id) if run_id else None, "stage": stage}
