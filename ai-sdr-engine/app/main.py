# app/main.py - FastAPI application: wires the /v1 API, mock CRM, live console and health check
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app import config, db, jobs, pipeline, reply_routes, seed_import
from app.inbox import InboxStore
from app.inbox_resend import ResendInboxPoller
from app.enquiry_notifications import NotificationWorker
from app.api.inbox import router as inbox_router
from app.api.routes import router as api_router
from app.api.workspace import router as workspace_router
from app.api.assistant import router as assistant_router
from app.api.performance import router as performance_router
from app.api.simulate import router as simulate_router
from app.api.linkedin import router as linkedin_router
from app.api.messaging import router as messaging_router
from app.api.accounts import router as accounts_router
from app.api.styles import router as styles_router
from app.api.campaign_channels import router as campaign_channels_router
from app.api.public_enquiry import router as public_enquiry_router
from app.crm import outbox
from app.crm.mock_crm import router as mock_crm_router
from app.governance.api import router as governance_router

STATIC_DIR = Path(__file__).resolve().parent / "static"

DESCRIPTION = """
Azentio AI SDR decision + engagement engine.

Leads arrive from the website, events, ads, marketing CSVs or outbound cohorts. The engine resolves identity,
enriches company and contact data, scores ICP fit, drafts outreach that **always waits for human approval**, reads replies,
extracts BANT with verified buyer excerpts, and routes each lead to an AE handoff, SDR follow-up, nurture,
suppression or close. Every change is pushed to the CRM as a signed webhook and streamed live over SSE.
"""


# Bind the server loop for cross-thread CRM delivery and open the database on startup
@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    outbox.bind_loop(asyncio.get_running_loop())
    try:  # demo snapshot into an empty workspace (ENGINE_SEED_FILE); never blocks startup
        seed_import.import_if_empty()
    except Exception:  # noqa: BLE001
        logging.getLogger("uvicorn.error").exception("seed import failed; starting without demo data")
    db.conn()
    runner = jobs.configure(pipeline.execute_job)
    application.state.job_runner = runner
    pipeline.recover_unfinished_runs(runner)
    store = InboxStore(config.DB_PATH)
    store.ensure_schema()
    application.state.inbox_store = store
    poller = None
    if reply_routes.receiving_domain() and config.RESEND_API_KEY:
        poller = ResendInboxPoller(store, account=reply_routes.receiving_domain(), lookup_run=pipeline.get_run,
                                  ingest_reply=pipeline.ingest_reply, lookup_lead=pipeline.get_lead,
                                  on_matched_reply=pipeline.stop_for_received_reply)
        poller.start()
    application.state.inbox_poller = poller
    runner.start()
    await outbox.start()
    notifications = NotificationWorker()
    application.state.notification_worker = notifications
    notifications.start()
    try:
        yield
    finally:
        await notifications.stop()
        if poller:
            await poller.stop()
        await runner.stop()
        await outbox.stop()


app = FastAPI(title="Azentio AI SDR Engine", description=DESCRIPTION, version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(api_router)
app.include_router(workspace_router)
app.include_router(assistant_router)
app.include_router(performance_router)
app.include_router(simulate_router)
app.include_router(linkedin_router)
app.include_router(messaging_router)
app.include_router(accounts_router)
app.include_router(styles_router)
app.include_router(campaign_channels_router)
app.include_router(public_enquiry_router)
app.include_router(inbox_router)
app.include_router(mock_crm_router)
app.include_router(governance_router)
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


# Serve a static HTML page if it exists, else fall back to the API docs
def page(name: str) -> Response:
    path = STATIC_DIR / name
    # no-cache: browsers revalidate (cheap 304) so a deploy is visible on the next load, not a cached old page
    return FileResponse(path, headers={"Cache-Control": "no-cache"}) if path.exists() else RedirectResponse("/docs")


# Live demo console
@app.get("/", include_in_schema=False)
def console_root() -> Response:
    return page("console.html")


# Sales workspace (alias of the console)
@app.get("/workspace", include_in_schema=False)
def workspace() -> Response:
    return page("console.html")


# Live demo console (explicit path)
@app.get("/console", include_in_schema=False)
def console() -> Response:
    return page("console.html")


# Trust & governance page (spec address); the page lives in the workspace shell
@app.get("/console/trust", include_in_schema=False)
def console_trust() -> Response:
    return RedirectResponse("/workspace#/trust")


# Public Azentio-style contact form that posts leads into the engine
@app.get("/contact", include_in_schema=False)
def contact() -> Response:
    return page("contact.html")


# Public trust center: security, compliance, AI safeguards and subprocessors
@app.get("/trust", include_in_schema=False)
def trust() -> Response:
    return page("trust.html")


# Liveness probe plus the demo clock (no internal configuration exposed)
@app.get("/health", tags=["health"])
def health() -> dict[str, Any]:
    from app.engagement import clock

    return {"status": "ok", "demoNow": clock.demo_now_iso()}
