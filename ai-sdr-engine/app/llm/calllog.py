# app/llm/calllog.py - Audit log of every real model/API call (purpose, model, duration, tokens) tied to the lead and pipeline step
from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any, Optional

from pydantic import BaseModel, Field

from app import db
from app.contracts import new_id, now_iso

_ctx: ContextVar[dict[str, Any]] = ContextVar("ai_call_context", default={})


class AiCall(BaseModel):
    """One outbound AI request made by the system."""
    id: str = Field(default_factory=lambda: new_id("ai"))
    leadId: Optional[str] = None
    step: Optional[str] = None
    purpose: str
    provider: str
    model: str
    ok: bool = True
    durationMs: int = 0
    inputTokens: Optional[int] = None
    outputTokens: Optional[int] = None
    error: Optional[str] = None
    at: str = Field(default_factory=now_iso)


# Bind the current lead/step so calls made inside are attributed to it; returns a token for unbind
def bind(lead_id: Optional[str], step: Optional[str]):
    return _ctx.set({"leadId": lead_id, "step": step})


# Restore the previous attribution context
def unbind(token) -> None:
    _ctx.reset(token)


# Persist one AI call record (never raises: logging must not break the pipeline)
def record(purpose: str, provider: str, model: str, started: float, ok: bool = True,
           usage: Optional[dict[str, Any]] = None, error: Optional[str] = None) -> None:
    try:
        c = _ctx.get()
        usage = usage or {}
        db.put("ai_calls", AiCall(
            leadId=c.get("leadId"), step=c.get("step"), purpose=purpose, provider=provider, model=model, ok=ok,
            durationMs=int((time.perf_counter() - started) * 1000),
            inputTokens=usage.get("input_tokens"), outputTokens=usage.get("output_tokens"), error=(error or "")[:200] or None,
        ))
    except Exception:  # noqa: BLE001 - audit logging is best effort
        pass


# All recorded AI calls for a lead, oldest first
def for_lead(lead_id: str) -> list[AiCall]:
    return db.list_("ai_calls", AiCall, lead_id=lead_id)
