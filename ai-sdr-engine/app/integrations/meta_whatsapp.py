# app/integrations/meta_whatsapp.py - Meta WhatsApp Cloud API sender (free test number) for custom text to the controlled test recipient
from __future__ import annotations

import httpx

from app import config

GRAPH = "https://graph.facebook.com/v21.0/{phone_id}/messages"


class MetaWhatsAppError(RuntimeError):
    """Meta WhatsApp Cloud API is not configured or rejected the message."""


# True when a Cloud API token, sender phone-number ID and the test recipient are configured
def configured() -> bool:
    return bool(config.env("WHATSAPP_CLOUD_TOKEN") and config.env("WHATSAPP_PHONE_NUMBER_ID") and config.env("TWILIO_TEST_TO"))


# Send free-form WhatsApp text (needs the recipient to have messaged the test number in the last 24h); returns (message id, to)
async def send(body: str) -> tuple[str, str]:
    token, phone_id = config.env("WHATSAPP_CLOUD_TOKEN"), config.env("WHATSAPP_PHONE_NUMBER_ID")
    to = config.env("TWILIO_TEST_TO").removeprefix("whatsapp:").lstrip("+")
    if not (token and phone_id and to):
        raise MetaWhatsAppError("whatsapp_cloud_not_configured")
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body, "preview_url": False}}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(GRAPH.format(phone_id=phone_id), json=payload, headers={"Authorization": f"Bearer {token}"})
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code >= 300:
        code = (data.get("error") or {}).get("code")
        raise MetaWhatsAppError(f"whatsapp_cloud_rejected_{code or resp.status_code}")
    return (data.get("messages") or [{}])[0].get("id", ""), "whatsapp:+" + to
