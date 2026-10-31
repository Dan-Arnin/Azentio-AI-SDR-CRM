# app/integrations/callmebot.py - Free WhatsApp gateway for custom text to the controlled test number (works without a paid Twilio account)
from __future__ import annotations

import re

import httpx

from app import config

API = "https://api.callmebot.com/whatsapp.php"
SUCCESS_RE = re.compile(r"message (queued|sent)|will be sent", re.I)


class CallMeBotError(RuntimeError):
    """CallMeBot is not configured or rejected the message."""


# True when an API key and the test recipient are configured
def configured() -> bool:
    return bool(config.env("CALLMEBOT_API_KEY") and config.env("TWILIO_TEST_TO"))


# Send custom WhatsApp text to the configured test number; returns (reference, delivered-to)
async def send(body: str) -> tuple[str, str]:
    key, to = config.env("CALLMEBOT_API_KEY"), config.env("TWILIO_TEST_TO").removeprefix("whatsapp:")
    if not (key and to):
        raise CallMeBotError("callmebot_not_configured")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(API, params={"phone": to, "text": body, "apikey": key})
    text = resp.text or ""
    if resp.status_code >= 300 or not SUCCESS_RE.search(text):
        raise CallMeBotError("callmebot_rejected" if "apikey" not in text.lower() else "callmebot_invalid_key")
    return f"callmebot-{resp.headers.get('date', '')}", "whatsapp:" + to
