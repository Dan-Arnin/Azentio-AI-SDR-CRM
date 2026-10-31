# app/engagement/resend_mcp.py - Narrow server-controlled Resend MCP transport with sender gates and idempotent dispatch
"""Narrow, server-controlled Resend MCP transport. No model receives these tools.

Official remote endpoint/auth: https://resend.com/docs/mcp-server
Only discovery/list-domains are used by status. Sending requires an approved
application envelope, a verified configured sender, and provider idempotency.
Accepted by Resend is deliberately distinct from delivered to an inbox.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from email.headerregistry import Address
from typing import Any

MCP_URL = "https://mcp.resend.com/mcp"


class EmailTransportError(RuntimeError):
    # Initialize explicit transport configuration without exposing credential values.
    def __init__(self, code: str, ambiguous: bool = False) -> None:
        self.code = code
        self.ambiguous = ambiguous
        self.ambiguous_delivery = ambiguous
        super().__init__(code)


# Read a field from typed MCP objects or plain dictionaries.
def _get(value: Any, key: str, alias: str | None = None, default=None):
    if isinstance(value, dict):
        return value.get(key, value.get(alias, default) if alias else default)
    return getattr(value, key, getattr(value, alias, default) if alias else default)


# Validate a single mailbox and reject unsafe headers or reserved domains.
def _mailbox(value: str, *, display_name: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 400 or "\r" in value or "\n" in value:
        raise EmailTransportError("email_address_invalid")
    value = value.strip()
    if display_name and ("<" in value or ">" in value):
        match = re.fullmatch(r"[^<>]*<([^<>]+)>", value)
        if not match:
            raise EmailTransportError("email_address_invalid")
        value = match.group(1)
    try:
        address = Address(addr_spec=value)
        local, domain = address.username, address.domain
        if (not local or len(local) > 64 or len(value) > 254
                or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
                or not domain or "." not in domain):
            raise ValueError("invalid_mailbox")
        domain = domain.encode("idna").decode("ascii").lower()
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in domain.split(".")):
            raise ValueError("invalid_domain")
        if domain.rsplit(".", 1)[-1].isdigit():
            raise ValueError("invalid_domain")
        normalized = f"{local}@{domain}".lower()
    except (ValueError, IndexError, UnicodeError):
        raise EmailTransportError("email_address_invalid") from None
    domain = normalized.rsplit("@", 1)[1]
    if domain.split(".")[-1] in {"example", "test", "localhost", "local", "invalid", "internal"} or domain in {"example.com", "example.org", "example.net"}:
        raise EmailTransportError("email_address_invalid")
    return normalized


# Parse successful structured or JSON MCP payloads without exposing raw errors.
def _payload(result) -> dict:
    if _get(result, "is_error", "isError", False):
        raise EmailTransportError("resend_tool_error")
    structured = _get(result, "structured_content", "structuredContent")
    if isinstance(structured, dict):
        return _successful_payload(structured)
    for item in _get(result, "content", default=[]) or []:
        if _get(item, "type") == "text":
            try:
                decoded = json.loads(_get(item, "text", default=""))
                if isinstance(decoded, dict):
                    return _successful_payload(decoded)
            except (ValueError, TypeError):
                continue
    raise EmailTransportError("resend_response_invalid")


# Reject explicit provider error envelopes using only a safe local error code.
def _successful_payload(payload: dict) -> dict:
    current: Any = payload
    for _ in range(4):
        if not isinstance(current, dict):
            break
        if current.get("error"):
            raise EmailTransportError("resend_tool_error")
        current = current.get("data")
    return payload


# Read text-only MCP content blocks for narrow source-backed response parsing.
def _text_blocks(result) -> list[str]:
    return [_get(item, "text", default="") for item in _get(result, "content", default=[]) or []
            if _get(item, "type") == "text" and isinstance(_get(item, "text"), str)]


# Parse only documented list-domains response formats.
def _domains_payload(result) -> dict:
    """Parse only source-backed list-domains formats, never a send response.

    https://github.com/resend/resend-mcp/blob/main/src/tools/domains.ts
    """
    try:
        return _payload(result)
    except EmailTransportError as error:
        if error.code != "resend_response_invalid":
            raise
    blocks = _text_blocks(result)
    if blocks and blocks[0].strip() == "No domains found.":
        return {"data": []}
    if blocks:
        found = re.fullmatch(r"Found ([1-9]\d*) domains?:", blocks[0].strip())
        if found:
            records = []
            for block in blocks[1:]:
                if not block.startswith("Name: "):
                    continue
                fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
                if not {"Name", "ID", "Status", "Region"}.issubset(fields):
                    raise EmailTransportError("resend_response_invalid")
                records.append({"name": fields["Name"], "id": fields["ID"], "status": fields["Status"],
                                "region": fields["Region"], "capabilities": {"sending": fields.get("Sending", "unknown")}})
            if len(records) == int(found.group(1)):
                return {"data": records}
    raise EmailTransportError("resend_response_invalid")


# Parse only documented send-email confirmations or proven validation rejection.
def _send_payload(result) -> dict:
    """Official send-email returns a fixed success prefix plus JSON data.

    https://github.com/resend/resend-mcp/blob/main/src/tools/emails.ts
    """
    if _get(result, "is_error", "isError", False):
        for text in _text_blocks(result):
            lowered = text.lower()
            if ("you can only send testing emails to your own email address" in lowered
                    and "please verify a domain" in lowered):
                # This provider validation rejection proves non-acceptance. Do
                # not return the diagnostic, which can contain account emails.
                raise EmailTransportError("resend_test_recipient_restricted", ambiguous=False)
    try:
        return _payload(result)
    except EmailTransportError as error:
        if error.code != "resend_response_invalid":
            raise
    prefix = "Email sent successfully! "
    for text in _text_blocks(result):
        if text.startswith(prefix):
            try:
                data = json.loads(text[len(prefix):])
                if isinstance(data, dict):
                    return _successful_payload(data)
            except (ValueError, TypeError):
                break
    raise EmailTransportError("resend_response_invalid")


# Read domain records from a valid account-list response envelope.
def _domain_records(payload: dict) -> list[dict]:
    current: Any = payload
    for _ in range(4):
        if isinstance(current, list):
            return [record for record in current if isinstance(record, dict)]
        if isinstance(current, dict) and "data" in current:
            current = current["data"]
        else:
            break
    raise EmailTransportError("resend_response_invalid")


# Find explicitly supported provider idempotency arguments in the live tool schema.
def _idempotency_slot(schema: dict) -> tuple[str, ...] | None:
    properties = schema.get("properties", {})
    for key in ("idempotencyKey", "idempotency_key"):
        if key in properties:
            return (key,)
    options = properties.get("options", {}).get("properties", {})
    for key in ("idempotencyKey", "idempotency_key"):
        if key in options:
            return ("options", key)
    # A message's custom headers are not the provider HTTP Idempotency-Key.
    return None


class ResendMCPTransport:
    transport = "resend_mcp"

    # Initialize explicit transport configuration without exposing credential values.
    def __init__(self, api_key: str | None = None, from_address: str | None = None,
                 reply_to: str | None = None, allowed_recipients: str | list[str] | None = None,
                 test_mode: bool | None = None, *, session_factory=None, status_ttl_seconds: float = 60) -> None:
        self._key = api_key if api_key is not None else os.getenv("RESEND_API_KEY", "")
        self._from = from_address if from_address is not None else os.getenv("RESEND_FROM", "")
        self._reply_to = reply_to if reply_to is not None else os.getenv("RESEND_REPLY_TO", "")
        allowed = allowed_recipients if allowed_recipients is not None else os.getenv("RESEND_ALLOWED_RECIPIENTS", "")
        self._allowed_raw = allowed.split(",") if isinstance(allowed, str) else list(allowed)
        self._test_mode = test_mode if test_mode is not None else os.getenv("RESEND_TEST_MODE", "false").lower() == "true"
        self._session_factory = session_factory or self._connect
        self._ttl = min(max(float(status_ttl_seconds), 0), 60)
        self._cache: dict | None = None
        self._cached_at = 0.0
        self._send_schema: dict = {}
        self._conversation_id: str | None = None
        self._lock = asyncio.Lock()

    # Build generic non-identifying analytics arguments and preserve the server conversation ID.
    def _tool_arguments(self, context: str) -> dict:
        result = {"context": context, "llm_model": "unknown"}
        if self._conversation_id is not None:
            result["conversation_id"] = self._conversation_id
        return result

    # Retain an actual server-issued conversation identifier without inventing one.
    def _remember_conversation(self, result, payload: dict) -> None:
        # Find the server-issued conversation identifier in response metadata.
        def find(value):
            if isinstance(value, dict):
                if isinstance(value.get("conversation_id"), str) and value["conversation_id"]:
                    return value["conversation_id"]
                for nested in value.values():
                    found = find(nested)
                    if found:
                        return found
            elif isinstance(value, list):
                for nested in value:
                    found = find(nested)
                    if found:
                        return found
            return None
        # The server owns this identifier; never derive one from a lead/thread.
        supplied = find(payload) or find(_get(result, "meta", "_meta"))
        if supplied and self._conversation_id is None:
            self._conversation_id = supplied

    # Authenticate one narrow MCP session using the configured server-only key.
    @asynccontextmanager
    async def _connect(self):
        if not self._key:
            raise EmailTransportError("resend_key_missing")
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {self._key}"}, timeout=30,
                               follow_redirects=False) as client,
            streamable_http_client(MCP_URL, http_client=client) as streams,
        ):
            read, write, *_ = streams
            async with ClientSession(read, write, read_timeout_seconds=25) as session:
                await session.initialize()
                yield session

    # Validate configured sender, reply address and allowed recipients.
    def _configuration(self) -> tuple[str | None, set[str], str | None]:
        sender = None
        allowed: set[str] = set()
        error = None
        try:
            if self._from:
                sender = _mailbox(self._from, display_name=True)
            for value in self._allowed_raw:
                if str(value).strip():
                    allowed.add(_mailbox(str(value)))
            if self._reply_to:
                _mailbox(self._reply_to)
        except EmailTransportError:
            error = "resend_address_configuration_invalid"
        return sender, allowed, error

    # Serialize and cache read-only provider readiness checks.
    async def status(self, *, force: bool = False) -> dict:
        async with self._lock:
            return await self._status_unlocked(force=force)

    # Discover exact MCP schemas and verified account sending domains.
    async def _status_unlocked(self, *, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and self._cache is not None and now - self._cached_at < self._ttl:
            return dict(self._cache)
        sender, allowed, config_error = self._configuration()
        status = {"transport": self.transport, "keyConfigured": bool(self._key), "senderConfigured": bool(sender),
                  "domainVerified": False, "connected": False, "authenticated": False, "idempotencySupported": False,
                  "testMode": self._test_mode, "allowedRecipientCount": len(allowed), "canSend": False,
                  "allowedRecipients": sorted(allowed), "fromAddress": self._from if sender else None,
                  "replyTo": self._reply_to or None, "errorCode": config_error}
        if not self._key:
            status["errorCode"] = "resend_key_missing"
        elif config_error:
            pass
        else:
            try:
                async with asyncio.timeout(35):
                    async with self._session_factory() as session:
                        tools = {}
                        cursor = None
                        for _ in range(10):
                            if cursor:
                                from mcp.types import PaginatedRequestParams
                                listed = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
                            else:
                                listed = await session.list_tools()
                            for tool in _get(listed, "tools", default=[]) or []:
                                tools[_get(tool, "name")] = _get(tool, "input_schema", "inputSchema", {})
                            cursor = _get(listed, "next_cursor", "nextCursor")
                            if not cursor:
                                break
                        status["connected"] = True
                        self._send_schema = tools.get("send-email", {})
                        status["idempotencySupported"] = bool(_idempotency_slot(self._send_schema))
                        if "list-domains" not in tools:
                            status["errorCode"] = "resend_domains_tool_unavailable"
                        else:
                            args = self._tool_arguments("The application checks an account's verified sending domains to determine whether an approved sender can safely use outbound email.")
                            args["limit"] = 100
                            result = await session.call_tool("list-domains", args)
                            payload = _domains_payload(result)
                            self._remember_conversation(result, payload)
                            domains = _domain_records(payload)
                            status["authenticated"] = True
                            sender_domain = sender.rsplit("@", 1)[1] if sender else None
                            status["domainVerified"] = any(
                                str(record.get("name", "")).lower() == sender_domain
                                and record.get("status") == "verified"
                                and (record.get("capabilities") or {}).get("sending") not in {"disabled", "failed"}
                                for record in domains)
                            controlled_test = self._test_mode and sender == "onboarding@resend.dev" and bool(allowed)
                            status["canSend"] = bool(sender and status["idempotencySupported"] and
                                                     (status["domainVerified"] or controlled_test))
                            if "send-email" not in tools:
                                status["errorCode"] = "resend_send_tool_unavailable"
                            elif not status["idempotencySupported"]:
                                status["errorCode"] = "resend_idempotency_unsupported"
                            elif not sender:
                                status["errorCode"] = "resend_sender_missing"
                            elif not status["domainVerified"] and not controlled_test:
                                status["errorCode"] = "resend_sender_domain_unverified"
            except Exception:  # noqa: BLE001 - MCP failures must expose only a safe readiness code
                status["canSend"] = False
                status["errorCode"] = "resend_mcp_unavailable"
        self._cache, self._cached_at = dict(status), now
        return status

    # Serialize one controlled dispatch after the application has approved its envelope.
    async def send(self, to: str, subject: str, body: str, idempotency_key: str, thread_id: str) -> dict:
        async with self._lock:
            return await self._send_unlocked(to, subject, body, idempotency_key, thread_id)

    # Validate sender, recipient and idempotency before one provider dispatch.
    async def _send_unlocked(self, to: str, subject: str, body: str, idempotency_key: str, thread_id: str) -> dict:
        recipient = _mailbox(to)
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 998 or "\r" in subject or "\n" in subject:
            raise EmailTransportError("email_subject_invalid")
        if not isinstance(body, str) or not body.strip() or len(body) > 1000000:
            raise EmailTransportError("email_body_invalid")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,256}", idempotency_key):
            raise EmailTransportError("email_idempotency_key_invalid")
        if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,200}", thread_id):
            raise EmailTransportError("email_thread_id_invalid")
        _, allowed, config_error = self._configuration()
        if config_error:
            raise EmailTransportError(config_error)
        if allowed and recipient not in allowed:
            raise EmailTransportError("email_recipient_not_allowed")
        capability = await self._status_unlocked(force=True)
        if not capability["canSend"]:
            raise EmailTransportError(capability["errorCode"] or "resend_send_unavailable")
        schema = self._send_schema
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if not {"from", "to", "subject", "text"}.issubset(properties):
            raise EmailTransportError("resend_send_schema_unsupported")
        args: dict[str, Any] = {"from": self._from, "to": [recipient] if properties["to"].get("type") == "array" else recipient,
                                "subject": subject, "text": body}
        args.update(self._tool_arguments("The application dispatches a human approved email through the configured provider while preserving duplicate prevention and conversation tracking controls."))
        slot = _idempotency_slot(schema)
        if slot is None:
            raise EmailTransportError("resend_idempotency_unsupported")
        if len(slot) == 1:
            args[slot[0]] = idempotency_key
        else:
            args[slot[0]] = {slot[1]: idempotency_key}
        if self._reply_to:
            reply_field = "reply_to" if "reply_to" in properties else "replyTo" if "replyTo" in properties else None
            if not reply_field:
                raise EmailTransportError("resend_reply_to_unsupported")
            args[reply_field] = [_mailbox(self._reply_to)] if properties[reply_field].get("type") == "array" else _mailbox(self._reply_to)
        if "headers" in properties:
            args["headers"] = {"X-SDR-Thread": thread_id}
        if required - args.keys():
            raise EmailTransportError("resend_send_schema_unsupported")
        dispatched = False
        try:
            async with asyncio.timeout(35):
                async with self._session_factory() as session:
                    dispatched = True
                    result = await session.call_tool("send-email", args)
                    payload = _send_payload(result)
                    self._remember_conversation(result, payload)
                    if payload.get("error"):
                        raise EmailTransportError("email_provider_rejected", ambiguous=True)
                    data = payload.get("data", payload)
                    message_id = data.get("id") if isinstance(data, dict) else None
                    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 200 or "\n" in message_id or "\r" in message_id:
                        raise EmailTransportError("email_delivery_unknown", ambiguous=True)
                    return {"providerMessageId": message_id, "status": "accepted", "transport": self.transport}
        except EmailTransportError as exc:
            if dispatched and not exc.ambiguous and exc.code != "resend_test_recipient_restricted":
                raise EmailTransportError("email_delivery_unknown", ambiguous=True) from None
            raise
        except Exception:  # noqa: BLE001 - unknown post-dispatch outcomes must remain ambiguous
            raise EmailTransportError("email_delivery_unknown" if dispatched else "resend_mcp_unavailable",
                                      ambiguous=dispatched) from None
