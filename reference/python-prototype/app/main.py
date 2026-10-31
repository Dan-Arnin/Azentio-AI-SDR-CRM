"""FastAPI app: JSON API + static dashboard."""
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, llm, outbound, pipeline
from .config import cfg, load

app = FastAPI(title="Azentio AI SDR (hackathon prototype)")
STATIC = Path(__file__).resolve().parent.parent / "static"


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except pipeline.ValidationError as e:
        raise HTTPException(422, {"errors": e.errors})
    except KeyError as e:
        raise HTTPException(404, f"not found: {e}")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    c = cfg()
    return {"engine": f"claude:{llm.MODEL}" if llm.enabled() else "heuristic (no Claude credentials)",
            "products": {k: v["name"] for k, v in c["products"].items()}, "themes": c["themes"], "cohorts": c["cohorts"],
            "qualification_rule": c["qualification_rule"], "guardrails": c["guardrails"], "icp_thresholds": c["icp_thresholds"],
            "samples": load("samples")}


@app.post("/api/reset")
def reset():
    db.reset()
    return {"ok": True}


# ---------- inbound ----------

@app.post("/api/leads/inbound")
def inbound(payload: dict = Body(...)):
    lead = _guard(pipeline.capture, payload)
    return _guard(pipeline.process, lead["id"])


@app.post("/api/leads/import")
async def bulk_import(file: UploadFile | None = File(None), text: str | None = Form(None),
                      source: str | None = Form(None), product: str | None = Form(None)):
    """CSV or JSON list of leads; each row may carry its own source. Rows are created or rejected with reasons."""
    raw = (await file.read()).decode("utf-8-sig") if file else (text or "")
    if not raw.strip():
        raise HTTPException(422, {"errors": ["empty import"]})
    try:
        rows = pipeline.parse_rows(raw)
    except pipeline.ValidationError as e:
        raise HTTPException(422, {"errors": e.errors})
    except ValueError as e:  # JSONDecodeError
        raise HTTPException(422, {"errors": [f"could not parse import: {e}"]})
    return _guard(pipeline.ingest_rows, rows, {"source": source, "product": product})


@app.post("/api/leads/webinar")
async def webinar(file: UploadFile | None = File(None), csv_text: str | None = Form(None),
                  webinar: str = Form(...), product: str = Form(...)):
    text = (await file.read()).decode("utf-8-sig") if file else (csv_text or "")
    if not text.strip():
        raise HTTPException(422, {"errors": ["empty attendee list"]})
    return _guard(pipeline.ingest_webinar_csv, text, webinar, product)


@app.get("/api/leads")
def leads():
    rows = db.query("SELECT * FROM leads ORDER BY created_at DESC")
    for r in rows:
        a = db.query("SELECT decision, result, engine FROM analyses WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1", [r["id"]])
        r["last_decision"] = a[0]["decision"] if a else None
        r["bant_status"] = ({k: v["status"] for k, v in a[0]["result"]["merged_bant"].items() if not k.startswith("_")} if a else None)
        r["next"] = pipeline.next_action(r, r["last_decision"])
    return rows


@app.get("/api/leads/{lead_id}")
def lead_detail(lead_id: str):
    l = _guard(pipeline.lead, lead_id)
    last = db.query("SELECT decision FROM analyses WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1", [lead_id])
    return {"lead": l, "next": pipeline.next_action(l, last[0]["decision"] if last else None),
            "messages": db.query("SELECT * FROM messages WHERE lead_id = ? ORDER BY created_at", [lead_id]),
            "analyses": db.query("SELECT * FROM analyses WHERE lead_id = ? ORDER BY created_at", [lead_id]),
            "alerts": db.query("SELECT * FROM alerts WHERE lead_id = ? ORDER BY created_at", [lead_id]),
            "events": db.query("SELECT * FROM events WHERE lead_id = ? ORDER BY id", [lead_id])}


@app.post("/api/leads/{lead_id}/reply")
def reply(lead_id: str, payload: dict = Body(...)):
    return _guard(pipeline.receive_reply, lead_id, payload.get("body", ""))


# ---------- approvals ----------

@app.get("/api/approvals")
def approvals():
    rows = db.query("SELECT m.*, l.email AS lead_email FROM messages m JOIN leads l ON l.id = m.lead_id WHERE m.status = 'pending_approval' ORDER BY m.created_at")
    return rows


@app.post("/api/messages/{message_id}/approve")
def approve(message_id: str, payload: dict = Body(default={})):
    return _guard(pipeline.approve, message_id, payload.get("approver", "SDR reviewer"), payload.get("body"))


@app.post("/api/messages/{message_id}/reject")
def reject(message_id: str, payload: dict = Body(default={})):
    return _guard(pipeline.reject, message_id, payload.get("approver", "SDR reviewer"), payload.get("reason", "No reason given"))


# ---------- outbound ----------

@app.post("/api/cohorts/preview")
def cohort_preview(payload: dict = Body(...)):
    return outbound.preview(_guard(outbound.cohort_definition, payload.get("cohort_key"), payload.get("custom")))


@app.post("/api/sequences")
def create_sequence(payload: dict = Body(...)):
    return _guard(outbound.create, payload.get("name") or "Untitled sequence", payload.get("cohort_key"),
                  payload.get("theme"), payload.get("product"), payload.get("custom"))


@app.get("/api/sequences")
def sequences():
    rows = db.query("SELECT * FROM sequences ORDER BY created_at DESC")
    for s in rows:
        s["enrollments"] = db.query("SELECT e.*, l.state FROM enrollments e JOIN leads l ON l.id = e.lead_id WHERE e.sequence_id = ?", [s["id"]])
    return rows


@app.put("/api/sequences/{sequence_id}/steps")
def edit_steps(sequence_id: str, payload: dict = Body(...)):
    return _guard(outbound.update_steps, sequence_id, payload["steps"])


@app.post("/api/sequences/{sequence_id}/approve")
def approve_sequence(sequence_id: str, payload: dict = Body(default={})):
    return _guard(outbound.approve, sequence_id, payload.get("approver", "Sales manager"))


@app.post("/api/sequences/{sequence_id}/advance")
def advance(sequence_id: str):
    return _guard(outbound.advance, sequence_id)


# ---------- alerts, CRM, audit ----------

@app.get("/api/alerts")
def alerts():
    return db.query("SELECT * FROM alerts ORDER BY created_at DESC")


@app.post("/api/alerts/{alert_id}/ack")
def ack(alert_id: str):
    db.update("alerts", alert_id, {"status": "acknowledged"})
    return db.get("alerts", alert_id)


@app.get("/api/crm")
def crm():
    return {"opportunities": db.query("SELECT * FROM crm_opportunities ORDER BY created_at DESC"),
            "suppressions": db.query("SELECT * FROM suppressions ORDER BY created_at DESC"),
            "mock_crm": load("mock_crm")}


@app.get("/api/stats")
def stats():
    by_state = db.query("SELECT state, COUNT(*) AS n FROM leads GROUP BY state")
    by_source = db.query("SELECT source, COUNT(*) AS n FROM leads GROUP BY source")
    return {"by_state": {r["state"]: r["n"] for r in by_state}, "by_source": {r["source"]: r["n"] for r in by_source},
            "pending_approvals": len(approvals()),
            "open_alerts": db.query("SELECT COUNT(*) AS n FROM alerts WHERE status IN ('new','open')")[0]["n"],
            "emails_sent": db.query("SELECT COUNT(*) AS n FROM messages WHERE direction='out' AND status='sent'")[0]["n"],
            "ready_for_human": sum(r["n"] for r in by_state if r["state"] in pipeline.READY_FOR_HUMAN),
            "by_icp": {r["v"]: r["n"] for r in db.query("SELECT json_extract(icp,'$.verdict') AS v, COUNT(*) AS n FROM leads WHERE icp IS NOT NULL GROUP BY v")}}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
