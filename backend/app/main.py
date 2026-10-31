"""Azentio AI SDR backend. Run: uvicorn app.main:app --reload (from backend/)."""
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .crm import Unauthorized
from .intake import InvalidInput
from .repo import Conflict, NotFound
from .routers import crm, leads, system, workflow

app = FastAPI(title="Azentio AI SDR Backend", version="0.2.0",
              description="Synthetic SDR demo. All sends simulated. All model modes explicit.")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("CORS_ORIGIN", "http://localhost:3000").split(","),
                   allow_methods=["*"], allow_headers=["*"])

for r in (system.router, leads.router, workflow.router, crm.router):
    app.include_router(r)


@app.exception_handler(Unauthorized)
def _unauthorized(_: Request, e: Unauthorized):
    return JSONResponse(status_code=503 if e.code == "webhook_not_configured" else 401, content={"code": e.code})


@app.exception_handler(NotFound)
def _not_found(_: Request, e: NotFound):
    return JSONResponse(status_code=404, content={"code": "not_found", "message": str(e)})


@app.exception_handler(Conflict)
def _conflict(_: Request, e: Conflict):
    return JSONResponse(status_code=409, content={"code": e.code, "message": str(e)})


@app.exception_handler(InvalidInput)
def _invalid(_: Request, e: InvalidInput):
    return JSONResponse(status_code=422, content={"code": "validation_failed", "errors": e.errors})
