# app/api/inbox.py - Read-only durable inbox status for the operator workspace
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/v1/inbox", tags=["inbox"])


# Report persisted inbox counts without returning message bodies or credentials.
@router.get("/status")
def status(request: Request):
    store = getattr(request.app.state, "inbox_store", None)
    if store is None:
        raise HTTPException(503, detail="Reply receiving is not set up yet")
    result = store.status()
    poller = getattr(request.app.state, "inbox_poller", None)
    return {**result, **(poller.status() if poller else {"connected": False, "label": "Replies need setup"})}
