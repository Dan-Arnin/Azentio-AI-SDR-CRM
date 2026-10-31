# app/governance/auth.py - Console sign-in with server-side roles (signed session cookie); used by the governance page
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Optional

from fastapi import HTTPException, Request

from app.governance import store

# Demo users (fictional). Sign-in uses one shared demo password from CONSOLE_PASSWORD.
USERS: dict[str, dict[str, str]] = {
    "mgr.hana": {"name": "Hana Al Suwaidi", "role": "sales_manager"},
    "admin.ravi": {"name": "Ravi Menon", "role": "portal_admin"},
    "sdr.omar": {"name": "Nadia Karim", "role": "sdr"},
}
ROLE_LABEL = {"sales_manager": "Sales Manager", "portal_admin": "Portal Admin", "sdr": "SDR"}
PERMISSIONS = {
    "viewGovernance": {"sales_manager", "portal_admin"},
    "manageGuardrails": {"sales_manager", "portal_admin"},
    "viewFullAudit": {"portal_admin"},
    "handleDataRequests": {"portal_admin"},
}
COOKIE = "sdr_session"
SESSION_SECONDS = 8 * 3600
MAX_FAILURES, LOCK_SECONDS = 5, 900
_SECRET = (os.environ.get("SESSION_SECRET") or secrets.token_hex(32)).encode()
_failures: dict[str, list[float]] = {}


def _password() -> str:
    return os.environ.get("CONSOLE_PASSWORD") or "demo"


def _sign(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _unsign(token: str) -> Optional[dict[str, Any]]:
    body, _, sig = (token or "").partition(".")
    if not body or not hmac.compare_digest(sig, hmac.new(_SECRET, body.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, UnicodeError):
        return None
    return payload if payload.get("exp", 0) > time.time() and payload.get("uid") in USERS else None


def user_view(uid: str) -> dict[str, Any]:
    u = USERS[uid]
    return {"id": uid, "name": u["name"], "role": u["role"], "roleLabel": ROLE_LABEL[u["role"]],
            "permissions": sorted(p for p, roles in PERMISSIONS.items() if u["role"] in roles)}


def current_user(request: Request) -> Optional[dict[str, Any]]:
    payload = _unsign(request.cookies.get(COOKIE, ""))
    return user_view(payload["uid"]) if payload else None


def require(request: Request, permission: Optional[str] = None) -> dict[str, Any]:
    """Server-side check behind every governance call: 401 when signed out, 403 without the permission."""
    user = current_user(request)
    if not user:
        raise HTTPException(401, detail={"code": "sign_in_required"})
    if permission and permission not in user["permissions"]:
        raise HTTPException(403, detail={"code": "forbidden"})
    return user


def actor(request: Request, fallback: str = "unknown") -> tuple[str, str]:
    """Who to record in the audit log: the signed-in user, else the name the client sent (marked as not signed in)."""
    user = current_user(request)
    return (user["id"], user["role"]) if user else (fallback, "not_signed_in")


def login(uid: str, password: str) -> tuple[dict[str, Any], str]:
    now = time.time()
    recent = [t for t in _failures.get(uid, []) if now - t < LOCK_SECONDS]
    _failures[uid] = recent
    if uid not in USERS:
        store.audit(uid[:60] or "unknown", "unknown", "auth.sign_in_failed", "user", uid[:60], detail="unknown user")
        raise HTTPException(401, detail={"code": "invalid_credentials"})
    if len(recent) >= MAX_FAILURES:
        store.audit(uid, USERS[uid]["role"], "auth.sign_in_locked", "user", uid, detail=f"{len(recent)} failures in 15 minutes")
        raise HTTPException(429, detail={"code": "too_many_attempts"})
    if not hmac.compare_digest(password.encode(), _password().encode()):
        recent.append(now)
        store.audit(uid, USERS[uid]["role"], "auth.sign_in_failed", "user", uid, detail="wrong password")
        raise HTTPException(401, detail={"code": "invalid_credentials"})
    _failures.pop(uid, None)
    store.audit(uid, USERS[uid]["role"], "auth.sign_in", "user", uid)
    return user_view(uid), _sign({"uid": uid, "exp": int(now) + SESSION_SECONDS})
