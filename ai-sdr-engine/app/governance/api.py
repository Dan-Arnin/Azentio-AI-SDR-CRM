# app/governance/api.py - /v1/auth and /v1/governance: role checks on the server, every action audit-logged
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import db
from app.contracts import Campaign, now_iso
from app.governance import auth, guardrails, report, rights, store

router = APIRouter(prefix="/v1", tags=["governance"])
Days = Query(14, ge=1, le=90)
VALUE_RE = re.compile(r"([a-z0-9._%+-]+)?@[a-z0-9-]+(\.[a-z0-9-]+)+")  # email or "@domain"


class LoginInput(BaseModel):
    userId: str = Field(max_length=60)
    password: str = Field(max_length=200)


class ReasonInput(BaseModel):
    reason: str = Field(default="", max_length=300)


class SuppressionInput(BaseModel):
    value: str = Field(min_length=3, max_length=200)  # an email address or a domain such as "@bank.example"
    reason: str = Field(default="", max_length=300)


class RequestInput(BaseModel):
    kind: Literal["export", "erase"]
    email: str = Field(min_length=3, max_length=200)


class RetentionInput(BaseModel):
    days: int = Field(ge=30, le=3650)


# ---------------------------------------------------------------- sign-in

@router.get("/auth/users")
def users() -> dict[str, Any]:
    return {"items": [auth.user_view(uid) for uid in auth.USERS]}


@router.post("/auth/login")
def login(body: LoginInput, request: Request, response: Response) -> dict[str, Any]:
    user, token = auth.login(body.userId, body.password)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_SECONDS, httponly=True, samesite="lax", secure=secure)
    return {"user": user}


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    user = auth.current_user(request)
    if user:
        store.audit(user["id"], user["role"], "auth.sign_out", "user", user["id"])
    response.delete_cookie(auth.COOKIE)
    return {"signedOut": True}


@router.get("/auth/me")
def me(request: Request) -> dict[str, Any]:
    return {"user": auth.current_user(request)}


# ---------------------------------------------------------------- report and records

@router.get("/governance/report")
def get_report(request: Request, days: int = Days, campaign: Optional[str] = None) -> dict[str, Any]:
    user = auth.require(request, "viewGovernance")
    return report.governance_report(user, days, campaign)


@router.get("/governance/me")
def get_my_summary(request: Request, days: int = Days) -> dict[str, Any]:
    return report.my_summary(auth.require(request), days)


@router.get("/governance/records/{kind}")
def get_records(kind: str, request: Request, days: int = Days, campaign: Optional[str] = None, q: str = "") -> dict[str, Any]:
    user = auth.require(request, "viewGovernance")
    if kind == "requests":
        auth.require(request, "handleDataRequests")
    try:
        return report.records(kind, user, days, campaign, q[:100])
    except ValueError:
        raise HTTPException(404, detail={"code": "unknown_kind"})


# ---------------------------------------------------------------- actions (each writes one audit event)

def _flag_action(flag_id: str, status: str, body: ReasonInput, request: Request) -> dict[str, Any]:
    user = auth.require(request, "manageGuardrails")
    if not body.reason.strip():
        raise HTTPException(422, detail={"code": "reason_required"})
    flag = store.get_flag(flag_id)
    if not flag:
        raise HTTPException(404, detail={"code": "not_found"})
    if flag["status"] not in ("open", "confirmed") or (status == "confirmed" and flag["status"] == "confirmed"):
        raise HTTPException(409, detail={"code": "invalid_state"})
    store.set_flag_status(flag_id, status, body.reason.strip(), user["id"])
    unblocked = guardrails.unblock_if_cleared(flag["draft_id"]) if status == "cleared" and flag["draft_id"] else False
    store.audit(user["id"], user["role"], f"flag.{status}", "flag", flag_id, flag["lead_id"], before={"status": flag["status"]},
                after={"status": status, "draftUnblocked": unblocked}, detail=body.reason.strip())
    return {"id": flag_id, "status": status, "draftUnblocked": unblocked}


@router.post("/governance/flags/{flag_id}/clear")
def clear_flag(flag_id: str, body: ReasonInput, request: Request) -> dict[str, Any]:
    return _flag_action(flag_id, "cleared", body, request)


@router.post("/governance/flags/{flag_id}/confirm")
def confirm_flag(flag_id: str, body: ReasonInput, request: Request) -> dict[str, Any]:
    return _flag_action(flag_id, "confirmed", body, request)


@router.post("/governance/suppressions", status_code=201)
def add_suppression(body: SuppressionInput, request: Request) -> dict[str, Any]:
    user = auth.require(request, "manageGuardrails")
    from app.pipeline import Suppression

    value = body.value.strip().lower()
    if "@" not in value:
        value = "@" + value  # a bare domain blocks the whole domain
    if not VALUE_RE.fullmatch(value):
        raise HTTPException(422, detail={"code": "invalid_value"})
    row = Suppression(id=value, leadId="", email=value, reason=f"manual: {body.reason.strip() or 'added by ' + user['name']}", at=now_iso())
    db.put("suppressions", row)
    store.audit(user["id"], user["role"], "suppression.added", "suppression", value, after={"reason": row.reason})
    return {"id": value, "reason": row.reason}


@router.delete("/governance/suppressions/{entry_id:path}")
def remove_suppression(entry_id: str, request: Request) -> dict[str, Any]:
    user = auth.require(request, "manageGuardrails")
    row = next((r for r in db.list_("suppressions", rights._Row, limit=100000) if r.id == entry_id), None)
    if not row:
        raise HTTPException(404, detail={"code": "not_found"})
    reason = str(getattr(row, "reason", ""))
    if not reason.startswith("manual"):
        # an opt-out or erasure must keep working; only manual entries can be removed
        raise HTTPException(409, detail={"code": "opt_out_protected"})
    db.delete("suppressions", entry_id)
    store.audit(user["id"], user["role"], "suppression.removed", "suppression", entry_id, before={"reason": reason})
    return {"removed": entry_id}


@router.post("/governance/campaigns/{campaign_id}/{action}")
def pause_campaign(campaign_id: str, action: Literal["pause", "resume"], request: Request) -> dict[str, Any]:
    user = auth.require(request, "manageGuardrails")
    if not db.get("campaigns", campaign_id, Campaign):
        raise HTTPException(404, detail={"code": "not_found"})
    paused = store.paused_campaigns()
    before = campaign_id in paused
    (paused.add if action == "pause" else paused.discard)(campaign_id)
    store.set_setting("paused_campaigns", sorted(paused))
    store.audit(user["id"], user["role"], f"campaign.{'paused' if action == 'pause' else 'resumed'}", "campaign", campaign_id,
                before={"paused": before}, after={"paused": action == "pause"})
    return {"id": campaign_id, "paused": action == "pause"}


@router.post("/governance/data-requests", status_code=201)
def start_request(body: RequestInput, request: Request) -> dict[str, Any]:
    user = auth.require(request, "handleDataRequests")
    row = store.add_request(body.kind, body.email, user["id"])
    store.audit(user["id"], user["role"], "data_request.started", "data_request", row["id"], after={"kind": body.kind, "subject": row["subject_hash"][:12]})
    return {**row, "matchingLeads": len(rights.leads_for(body.email))}


@router.post("/governance/data-requests/{req_id}/complete")
def complete_request(req_id: str, body: RequestInput, request: Request) -> Any:
    """Run the request. The admin re-enters the email; only its hash is stored, so it must match."""
    user = auth.require(request, "handleDataRequests")
    row = next((r for r in store.requests() if r["id"] == req_id), None)
    if not row:
        raise HTTPException(404, detail={"code": "not_found"})
    if row["completed_at"]:
        raise HTTPException(409, detail={"code": "already_completed"})
    if store.subject_hash(body.email) != row["subject_hash"] or body.kind != row["kind"]:
        raise HTTPException(422, detail={"code": "subject_mismatch"})
    if row["kind"] == "export":
        bundle = rights.export_bundle(body.email)
        outcome = f"exported {len(bundle['leads'])} lead record(s)"
    else:
        outcome = f"anonymised {rights.erase(body.email, f'erase request {req_id}')} lead record(s)"
        bundle = None
    store.complete_request(req_id, user["id"], outcome)
    store.audit(user["id"], user["role"], "data_request.completed", "data_request", req_id, after={"kind": row["kind"], "outcome": outcome})
    if bundle is not None:
        return JSONResponse(bundle, headers={"Content-Disposition": f'attachment; filename="export-{req_id}.json"'})
    return {"id": req_id, "outcome": outcome}


@router.put("/governance/retention")
def set_retention(body: RetentionInput, request: Request) -> dict[str, Any]:
    user = auth.require(request, "handleDataRequests")
    before = rights.retention_days()
    store.set_setting("retention_days", body.days)
    store.audit(user["id"], user["role"], "retention.changed", "retention", None, before={"days": before}, after={"days": body.days})
    return rights.retention_state()


@router.post("/governance/retention/run")
def run_retention(request: Request) -> dict[str, Any]:
    auth.require(request, "handleDataRequests")
    return {"anonymised": rights.run_retention_if_due(force=True), **rights.retention_state()}


# ---------------------------------------------------------------- evidence pack

def _csv(rows: list[dict[str, Any]], cols: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in r.items()})
    return buf.getvalue()


def _summary_html(r: dict[str, Any], user: dict[str, Any], audit_rows: int) -> str:
    from html import escape

    def tile(t: dict[str, Any]) -> str:
        n = t["now"]
        v = (f"{n['pct']}% ({n['num']} of {n['den']})" if n.get("den") else "No activity in period") if t["kind"] == "ratio" else str(n["num"])
        return f"<tr><td>{escape(t['label'])}</td><td>{escape(v)}</td></tr>"

    counts = {s: sum(1 for c in r["register"]["rows"] if c["status"] == s) for s in ("Live", "Partial", "Not built")}
    p = r["period"]
    return f"""<!doctype html><meta charset="utf-8"><title>Evidence pack summary</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:720px;margin:32px auto;color:#16303d}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #dde5e7;padding:6px 8px;text-align:left}}h1{{font-size:22px}}.m{{color:#5b6d75}}</style>
<h1>Azentio AI SDR: trust and governance evidence pack</h1>
<p class="m">Period: last {p['days']} days ({escape(p['start'][:10])} to {escape(p['end'][:10])}){' · campaign ' + escape(p['campaign']) if p['campaign'] else ''}.
Generated {escape(r['generatedAt'][:19])} UTC by {escape(user['name'])} ({escape(user['roleLabel'])}).</p>
<h2>Headline figures</h2><table>{''.join(tile(t) for t in r['tiles'])}</table>
<h2>Controls</h2><p>{counts['Live']} live · {counts['Partial']} partial · {counts['Not built']} not built (register: {escape(r['register']['source'])}). See controls.csv.</p>
<h2>Files</h2><ul><li>controls.csv: the controls register with live evidence</li><li>audit.csv: {audit_rows} audit entries for the period</li>
<li>panels.json: every panel figure</li></ul>
<p class="m">All prospect data in this workspace is synthetic. Sends are simulated or limited to a controlled test inbox.</p>"""


@router.get("/governance/export")
def export_pack(request: Request, days: int = Days, campaign: Optional[str] = None) -> Response:
    user = auth.require(request, "viewGovernance")
    r = report.governance_report(user, days, campaign)
    audit = report.records("audit", user, days, campaign)["rows"]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = f"evidence-pack_{stamp}_{days}d"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{base}/controls.csv", _csv(r["register"]["rows"], ["id", "name", "area", "priority", "status", "evidence", "simulated", "note"]))
        z.writestr(f"{base}/audit.csv", _csv(audit, ["at", "actor", "role", "action", "record_type", "record_id", "lead_id", "before", "after", "detail"]))
        z.writestr(f"{base}/panels.json", json.dumps({"period": r["period"], "tiles": r["tiles"], "panels": r["panels"]}, indent=1, default=str))
        z.writestr(f"{base}/summary.html", _summary_html(r, user, len(audit)))
    store.audit(user["id"], user["role"], "evidence_pack.exported", "export", base, after={"days": days, "campaign": campaign, "auditRows": len(audit)})
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{base}.zip"'})
