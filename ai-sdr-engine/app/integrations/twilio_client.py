# app/integrations/twilio_client.py - Minimal Twilio REST client for SMS/WhatsApp, restricted to one controlled test recipient
from __future__ import annotations

import json
from typing import Any

import httpx

from app import config

API = "https://api.twilio.com/2010-04-01/Accounts/{sid}"


class TwilioError(RuntimeError):
    """Twilio is not configured or rejected the request."""


# Current configuration: credentials, sender per channel, and the single allowed test recipient
def settings() -> dict[str, Any]:
    return {"sid": config.env("TWILIO_SID"), "token": config.env("TWILIO_AUTH_TOKEN"),
            "smsFrom": config.env("TWILIO_FROM"), "whatsappFrom": config.env("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
            "testTo": config.env("TWILIO_TEST_TO"), "contentSid": config.env("TWILIO_CONTENT_SID")}


# Human-readable readiness per channel (never exposes secrets)
def status() -> dict[str, Any]:
    s = settings()
    creds = bool(s["sid"] and s["token"])
    return {"credentials": creds, "testRecipientSet": bool(s["testTo"]),
            "sms": creds and bool(s["smsFrom"]) and bool(s["testTo"]),
            "whatsapp": creds and bool(s["testTo"]),
            "missing": [k for k, ok in (("TWILIO_SID/TWILIO_AUTH_TOKEN", creds), ("TWILIO_TEST_TO", bool(s["testTo"])),
                                       ("TWILIO_FROM (SMS only)", bool(s["smsFrom"]))) if not ok]}


# Send one message to the controlled test recipient; returns (provider sid, delivered-to)
async def send(channel: str, body: str) -> tuple[str, str]:
    s = settings()
    if not (s["sid"] and s["token"]):
        raise TwilioError("twilio_not_configured")
    if not s["testTo"]:
        raise TwilioError("twilio_test_recipient_missing")
    if channel == "whatsapp":
        sender, to = s["whatsappFrom"], "whatsapp:" + s["testTo"].removeprefix("whatsapp:")
    else:
        if not s["smsFrom"]:
            raise TwilioError("twilio_sms_sender_missing")
        sender, to = s["smsFrom"], s["testTo"].removeprefix("whatsapp:")
    data = {"From": sender, "To": to, "Body": body}
    if channel == "whatsapp" and s["contentSid"]:
        data = {"From": sender, "To": to, "ContentSid": s["contentSid"], "ContentVariables": json.dumps({"1": body})}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(API.format(sid=s["sid"]) + "/Messages.json", auth=(s["sid"], s["token"]), data=data)
    if resp.status_code >= 300:
        code = (resp.json() or {}).get("code") if resp.headers.get("content-type", "").startswith("application/json") else None
        raise TwilioError(f"twilio_rejected_{code or resp.status_code}")
    return resp.json().get("sid", ""), to
