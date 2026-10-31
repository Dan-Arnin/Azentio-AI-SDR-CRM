import os
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from psycopg import Connection

from .. import crm
from ..db import connect, get_conn

router = APIRouter(prefix="/v1/crm", tags=["crm"])


@router.post("/events")
async def receive_event(request: Request):
    """Signed webhook from ai-sdr-engine (set its CRM_WEBHOOK_URL to this path and share CRM_WEBHOOK_SECRET).

    The engine delivers events concurrently, so the database work runs off the event loop, and it commits
    before we answer: a 200 means the event is durable and the engine may mark it delivered.
    """
    body = await request.body()
    if len(body) > crm.MAX_BODY:
        return JSONResponse(status_code=413, content={"code": "body_too_large"})
    crm.verify(body, request.headers.get("X-SDR-Signature", ""))
    try:
        return await run_in_threadpool(_receive, body)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"code": str(e)})


def _receive(body: bytes) -> dict:
    with connect() as conn:
        return crm.receive(conn, body)


@router.post("/resync")
def resync():
    """Pull the engine's outbox and ingest anything missed (idempotent). Run it after the services wake up."""
    base = os.environ.get("ENGINE_URL", "").rstrip("/")
    if not base:
        return JSONResponse(status_code=503, content={"code": "engine_url_not_configured"})
    try:
        r = httpx.get(f"{base}/v1/crm/outbox", params={"limit": 100000}, timeout=90)  # a sleeping engine takes ~60 s to wake
        r.raise_for_status()
        items = r.json()["items"]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        return JSONResponse(status_code=502, content={"code": "engine_unreachable", "message": type(e).__name__})
    with connect() as conn:
        return {"engineEvents": len(items), **crm.resync(conn, items)}


@router.get("/leads")
def leads(motion: Literal["inbound", "outbound"] | None = None, conn: Connection = Depends(get_conn)):
    return crm.list_leads(conn, motion)


@router.get("/leads/{engine_lead_id}")
def lead(engine_lead_id: str, conn: Connection = Depends(get_conn)):
    return crm.get_lead(conn, engine_lead_id)


@router.get("/opportunities")
def opportunities(conn: Connection = Depends(get_conn)):
    return crm.list_opportunities(conn)


@router.get("/metrics")
def metrics(conn: Connection = Depends(get_conn)):
    return crm.metrics(conn)


@router.get("/routing")
def routing(country: str | None = None, email: str | None = None, conn: Connection = Depends(get_conn)):
    """Which AE should own this lead: existing account owner, then regional AE, then International."""
    return crm.route_ae(conn, country, email)
