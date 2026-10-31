# app/engagement/resend_email.py - Approved-email MCP adapter restricted to the configured controlled verification inbox
from __future__ import annotations

import asyncio

from app import config
from app.contracts import Draft, Lead
from app.engagement.resend_mcp import EmailTransportError, ResendMCPTransport, _mailbox
from app import reply_routes


class DeliveryError(RuntimeError):
    """Safe provider outcome; ambiguous outcomes require reconciliation before retry."""

    # Keep safe outcome attributes available to the native transport caller.
    def __init__(self, code: str, ambiguous: bool = False) -> None:
        self.code = code
        self.ambiguous = ambiguous
        self.ambiguous_delivery = ambiguous
        super().__init__(code)


# Send a human-approved draft only to the explicitly configured verification inbox.
def send(draft: Draft, lead: Lead) -> tuple[str, str]:
    if not config.RESEND_API_KEY:
        raise DeliveryError("resend_not_configured")
    try:
        sender = _mailbox(config.EMAIL_FROM, display_name=True)
        reply_route = reply_routes.route_for(lead.threadId)

        # Create one MCP client in this worker's event loop; no tools reach models.
        async def dispatch() -> dict:
            client = ResendMCPTransport(
                api_key=config.RESEND_API_KEY,
                from_address=config.EMAIL_FROM,
                reply_to=reply_route['address'] if reply_route else config.DEMO_INBOX,
                allowed_recipients=[config.DEMO_INBOX],
                test_mode=sender == "onboarding@resend.dev",
            )
            return await client.send(
                to=config.DEMO_INBOX,
                subject=draft.subject,
                body=draft.body,
                idempotency_key=f"{draft.id}-v{draft.version}",
                thread_id=reply_route['token'] if reply_route else lead.threadId,
            )

        accepted = asyncio.run(dispatch())
        reply_routes.register_sent(lead, accepted["providerMessageId"], reply_route)
        return accepted["providerMessageId"], config.DEMO_INBOX
    except EmailTransportError as error:
        raise DeliveryError(error.code, ambiguous=error.ambiguous) from None
    except Exception:  # noqa: BLE001 - sanitize unknown provider outcomes without exposing diagnostics
        # An unexpected outcome cannot safely be asserted to mean non-delivery.
        raise DeliveryError("email_delivery_unknown", ambiguous=True) from None
