# app/integrations/phantombuster.py - PhantomBuster client: list phantoms, read exports, launch outreach to the allow-listed test profile only
from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx

from app import config

BASE = "https://api.phantombuster.com/api/v2"


class PhantomBusterError(RuntimeError):
    """PhantomBuster is not configured or returned an error."""


# Authenticated GET against the PhantomBuster v2 API
async def _get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    key = config.env("PHANTOMBUSTER_API_KEY")
    if not key:
        raise PhantomBusterError("phantombuster_not_configured")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(BASE + path, params=params, headers={"X-Phantombuster-Key-1": key})
    if resp.status_code >= 400:
        raise PhantomBusterError(f"phantombuster_http_{resp.status_code}")
    return resp.json()


# All phantoms in the account with their last-run summary
async def list_phantoms() -> list[dict[str, Any]]:
    agents = await _get("/agents/fetch-all")
    out = []
    for a in agents if isinstance(agents, list) else []:
        out.append({"id": str(a.get("id")), "name": a.get("name"), "script": a.get("script"),
                    "launchType": a.get("launchType"), "lastEndedAt": a.get("lastEndedAt") or a.get("updatedAt"),
                    "lastEndMessage": a.get("lastEndMessage")})
    return out


# Rows from the most recent finished run of a phantom (empty list if it has never run)
async def latest_rows(agent_id: str) -> list[dict[str, Any]]:
    containers = await _get("/containers/fetch-all", {"agentId": agent_id})
    items = containers.get("containers", []) if isinstance(containers, dict) else containers or []
    finished = [c for c in items if c.get("status") in ("finished", "success", None)]
    if not finished:
        return []
    latest = sorted(finished, key=lambda c: c.get("endedAt") or c.get("createdAt") or 0)[-1]
    result = await _get("/containers/fetch-result-object", {"id": latest.get("id")})
    raw = result.get("resultObject") if isinstance(result, dict) else None
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    return [r for r in (data if isinstance(data, list) else [data]) if isinstance(r, dict)]


# Authenticated POST against the PhantomBuster v2 API; a busy phantom maps to phantombuster_busy
async def _post(path: str, body: dict[str, Any]) -> Any:
    key = config.env("PHANTOMBUSTER_API_KEY")
    if not key:
        raise PhantomBusterError("phantombuster_not_configured")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(BASE + path, json=body, headers={"X-Phantombuster-Key-1": key})
    if resp.status_code >= 400:
        slug = (resp.json().get("details") or {}).get("detailedErrorSlug") if "json" in resp.headers.get("content-type", "") else None
        raise PhantomBusterError("phantombuster_busy" if slug == "maxParallelismReached" else f"phantombuster_http_{resp.status_code}")
    return resp.json()


# Saved argument of a phantom as a dict (contains the LinkedIn session; never returned to callers outside this module)
async def _saved_argument(agent_id: str) -> dict[str, Any]:
    agent = await _get("/agents/fetch", {"id": agent_id})
    raw = agent.get("argument") if isinstance(agent, dict) else None
    try:
        return json.loads(raw) if isinstance(raw, str) and raw else dict(raw or {})
    except json.JSONDecodeError:
        return {}


# Only the allow-listed test profile may ever be targeted
def _allowed(profile_url: str) -> str:
    allowed = config.env("LINKEDIN_TEST_PROFILE_URL").rstrip("/")
    if not allowed or profile_url.rstrip("/") != allowed:
        raise PhantomBusterError("linkedin_target_not_allowlisted")
    return allowed


# Outreach phantom id: explicit setting, else the first "LinkedIn Outreach" phantom in the account
async def outreach_agent_id() -> str:
    explicit = config.env("PHANTOMBUSTER_OUTREACH_AGENT_ID")
    if explicit:
        return explicit
    for p in await list_phantoms():
        if "linkedin outreach" in (p.get("script") or "").lower():
            return p["id"]
    raise PhantomBusterError("phantombuster_outreach_phantom_missing")


# Find the send worker belonging to this workflow, without selecting another account's worker
async def _worker_agent_id(agent_id: str, script: str) -> str:
    agents = await _get("/agents/fetch-all")
    for agent in agents if isinstance(agents, list) else []:
        if str(agent.get("masterAgentId")) == agent_id and (agent.get("script") or "").lower() == script.lower():
            return str(agent["id"])
    raise PhantomBusterError("phantombuster_worker_missing")


# Connection state of the test profile ("connected" | "pending" | "none"), including worker results the workflow hasn't merged yet
async def connection_state(profile_url: str) -> str:
    target = _allowed(profile_url).lower()
    agent_id = await outreach_agent_id()
    worker_id = await _worker_agent_id(agent_id, "LinkedIn Auto Connect.js")
    state = "none"
    for source in (agent_id, worker_id):
        for r in await latest_rows(source):
            if (r.get("profileUrl") or r.get("linkedinProfileUrl") or "").rstrip("/").lower() != target:
                continue
            if r.get("connectionDegree") == "1st" or r.get("acceptanceDate") or "connected" in (r.get("connectionStatus") or "").lower():
                return "connected"
            if r.get("invitationSent") == "YES" or "invitation" in (r.get("connectionStatus") or "").lower():
                state = "pending"
    return state


# Whether the test profile already has an invite from us (pending or accepted)
async def invited_before(profile_url: str) -> bool:
    return await connection_state(profile_url) != "none"


# Queue the approved text as the workflow's 0-day follow-up, so it sends as soon as a pending invite is accepted
async def queue_follow_up(profile_url: str, message: str) -> None:
    allowed = _allowed(profile_url)
    await _enable_outreach(await outreach_agent_id(), allowed, follow_up=message)


# Keep the parent active at every hour, on every day, so PhantomBuster permits its workers to launch
async def _enable_outreach(agent_id: str, allowed: str, invitation_message: Optional[str] = None, follow_up: Optional[str] = None) -> None:
    argument = await _saved_argument(agent_id)
    argument.update({"leadsSourceUrl": allowed, "customizeInvite": True, "firstFollowUp": False, "secondFollowUp": False,
                     "thirdFollowUp": False, "requestsTime": "Randomly throughout the day and week", "retrieveLeadsEveryRun": True})
    if invitation_message is not None:
        argument["invitationMessage"] = invitation_message
    if follow_up is not None:
        argument.update({"firstFollowUp": True, "firstFollowUpTime": "0 days", "followUpMessage": follow_up})
    agent = await _get("/agents/fetch", {"id": agent_id})
    times = dict(agent.get("repeatedLaunchTimes") or {}) if isinstance(agent, dict) else {}
    times.pop("simplePreset", None)
    times.update({"hour": list(range(24)), "minute": [58], "isSimplePresetEnabled": False,
                  "day": list(range(1, 32)), "dow": ["sun", "mon", "tue", "wed", "thu", "fri", "sat"],
                  "month": ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]})
    times.setdefault("timezone", "Asia/Calcutta")
    await _post("/agents/save", {"id": agent_id, "launchType": "repeatedly", "repeatedLaunchTimes": times, "argument": json.dumps(argument)})


# Launch exactly one approved message through its worker immediately, independently of sequence timing
async def _launch_worker(profile_url: str, message: str, script: str) -> str:
    allowed = _allowed(profile_url)
    if not message.strip():
        raise PhantomBusterError("linkedin_message_empty")
    agent_id = await outreach_agent_id()
    worker_id = await _worker_agent_id(agent_id, script)
    invite = script == "LinkedIn Auto Connect.js"
    await _enable_outreach(agent_id, allowed, message if invite else None)
    bonus: dict[str, Any] = {"spreadsheetUrl": allowed, "message": message, "csvName": "result"}
    if invite:
        bonus["numberOfAddsPerLaunch"] = 1
    else:
        bonus.update({"profilesPerLaunch": 1, "hasMessagesInCsvInput": False, "messageControl": "none",
                      "reprocessAll": True, "enableScraping": False, "emailChooser": "none"})
    launched = await _post("/agents/launch", {"id": worker_id, "manualLaunch": True, "maxInstanceCount": 1,
                                              "saveArgument": False, "bonusArgument": bonus})
    container = str(launched.get("containerId") or "")
    if not container:
        raise PhantomBusterError("phantombuster_launch_not_confirmed")
    return container


# Connection request carrying the approved note; returns the run id
async def launch_outreach(profile_url: str, message: str) -> str:
    return await _launch_worker(profile_url, message, "LinkedIn Auto Connect.js")


# Send the approved text now, without waiting for a sequence's follow-up day or reply-state conditions
async def launch_message(profile_url: str, message: str) -> str:
    return await _launch_worker(profile_url, message, "LinkedIn Message Sender.js")


# Outcome of a finished send run read from its log: "sent", "not_connected", "running" or "failed"
async def run_outcome(container_id: str) -> str:
    r = await run_status(container_id)
    if r["status"] not in ("finished", "error", "stopped"):
        return "running"
    log = r["logTail"]
    if re.search(r"Message sent to|Invitation sent to|invitation(s)? (has|have) been sent", log, re.I):
        return "sent"
    if re.search(r"Couldn't type in chat widget|not a 1st degree|not connected", log, re.I):
        return "not_connected"
    return "sent" if r["exitCode"] == 0 and "[error]" not in log else "failed"


# Status of a phantom run plus the tail of its log (for confirming what happened on LinkedIn)
async def run_status(container_id: str) -> dict[str, Any]:
    c = await _get("/containers/fetch", {"id": container_id})
    out = await _get("/containers/fetch-output", {"id": container_id})
    log = (out.get("output") if isinstance(out, dict) else "") or ""
    return {"containerId": container_id, "status": c.get("status") if isinstance(c, dict) else None,
            "exitCode": c.get("exitCode") if isinstance(c, dict) else None, "logTail": log[-1500:]}
