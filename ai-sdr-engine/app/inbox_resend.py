# app/inbox_resend.py - Read-only Resend receiving poller feeding exact-matched durable inbox events
from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from email.utils import getaddresses
from typing import Any, Callable

from app.engagement.resend_mcp import ResendMCPTransport, EmailTransportError, _get, _payload, _text_blocks
from app.inbox import InboxStore, NormalizedEmail, timestamp

# Source-backed text formats: https://github.com/resend/resend-mcp/blob/main/src/tools/emails.ts
LIST_LINE = re.compile(r'^- From: (.*?) \| To: (.*?) \| Subject: "(.*)" \| Message ID: (.*?) \| Received: (.*?)(?: \| Attachments: \d+)? \| ID: ([A-Za-z0-9_-]+)$')


# Unwrap only JSON collection envelopes returned by the provider.
def unwrap(payload: dict) -> Any:
    current: Any = payload
    for _ in range(5):
        if isinstance(current, dict) and "data" in current:
            current = current["data"]
        else:
            return current
    raise EmailTransportError("resend_inbox_response_invalid")


# Parse structured JSON or the official MCP list-received-emails text response.
def list_payload(result) -> dict:
    if _get(result, "is_error", "isError", False):
        raise EmailTransportError("resend_inbox_read_failed")
    try:
        payload = _payload(result)
        rows = unwrap(payload)
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
            return {"items": rows, "hasMore": bool(payload.get("has_more") or (payload.get("data") or {}).get("has_more") if isinstance(payload.get("data"), dict) else payload.get("has_more"))}
    except EmailTransportError:
        pass
    texts = _text_blocks(result)
    if texts == ["No received emails found."]:
        return {"items": [], "hasMore": False}
    text = "\n".join(texts)
    header, _, rest = text.partition("\n\n")
    found = re.fullmatch(r"Found (\d+) received email\(s\)( \(more available\))?:", header.strip())
    if not found:
        raise EmailTransportError("resend_inbox_response_invalid")
    rows = []
    for line in rest.splitlines():
        match = LIST_LINE.fullmatch(line)
        if not match:
            raise EmailTransportError("resend_inbox_response_invalid")
        sender, recipients, subject, message_id, received_at, id_ = match.groups()
        rows.append({"id": id_, "from": sender, "to": [v.strip() for v in recipients.split(",")], "subject": subject, "message_id": message_id, "created_at": received_at})
    if len(rows) != int(found.group(1)):
        raise EmailTransportError("resend_inbox_response_invalid")
    return {"items": rows, "hasMore": bool(found.group(2))}


# Parse full content without fetching raw downloads or unrelated attachments.
def received_payload(result) -> dict:
    if _get(result, "is_error", "isError", False):
        raise EmailTransportError("resend_inbox_read_failed")
    try:
        payload = unwrap(_payload(result))
        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
            return payload
    except EmailTransportError:
        pass
    texts = _text_blocks(result)
    if len(texts) != 1 or not texts[0].startswith("Received Email Details:\n"):
        raise EmailTransportError("resend_inbox_response_invalid")
    metadata, separator, body = texts[0].partition("\n--- Plain Text Content ---\n")
    if not separator:
        raise EmailTransportError("resend_inbox_response_invalid")
    values = {}
    for line in metadata.splitlines()[1:]:
        match = re.fullmatch(r"- ([^:]+): (.*)", line)
        if match:
            if match.group(1) in values:
                raise EmailTransportError("resend_inbox_response_invalid")
            values[match.group(1)] = match.group(2)
    if not all(key in values for key in ("ID", "From", "To", "Subject", "Message ID", "Received")):
        raise EmailTransportError("resend_inbox_response_invalid")
    # The official tool appends a rendered HTML section after its plain text.
    # Keep the complete source separately and exclude that alternate rendering
    # from buyer-authored evidence, including its quoted outbound history.
    plain, marker, html = body.rpartition("\n--- HTML Content ---\n")
    text = plain if marker and html.lstrip().startswith("<") else body
    return {"id": values["ID"], "from": values["From"], "to": [v[1] for v in getaddresses([values["To"]])], "subject": values["Subject"], "message_id": values["Message ID"], "created_at": values["Received"], "text": text.rstrip("\n"), "raw_text": body.rstrip("\n"), "headers": {}}


# Convert verified provider fields to the bounded, provider-neutral ingestion contract.
def normalize(email: dict) -> NormalizedEmail:
    headers = email.get("headers") or {}
    if isinstance(headers, list):
        parsed = {}
        for item in headers:
            if isinstance(item, dict):
                parsed[item.get("name", "")] = item.get("value", "")
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                parsed[str(item[0])] = item[1]
        headers = parsed
    if not isinstance(headers, dict):
        raise EmailTransportError("resend_inbox_response_invalid")
    headers = {str(k).lower(): str(v) for k, v in headers.items()}
    headers.setdefault("message-id", str(email.get("message_id") or ""))
    recipients = email.get("to") or headers.get("to") or []
    if isinstance(recipients, str):
        recipients = [item[1] for item in getaddresses([recipients])]
    body = email.get("text")
    if not isinstance(body, str):
        # HTML-only mail is preserved for human review instead of pretending it
        # contained buyer-confirmed plain text.
        body = ""
    if body.strip() == "(none)":
        body = ""
    raw_body = str(email.get("raw_text") or email.get("text") or "")
    in_reply_to = email.get("in_reply_to") or headers.get("in-reply-to")
    references = email.get("references") or headers.get("references", "")
    if isinstance(references, str):
        references = re.findall(r"<([^<>]+)>", references)
    return NormalizedEmail(provider_id=str(email.get("id") or ""), from_email=str(email.get("from") or headers.get("from") or ""),
                           to_addresses=[str(v) for v in recipients], subject=str(email.get("subject") or ""),
                           body_text=body, raw_body_text=raw_body, received_at=str(email.get("created_at") or ""), headers=headers,
                           in_reply_to=in_reply_to, references=references)


class ResendInboxClient:
    # Reuse the existing server-only authenticated MCP session without giving tools to a model.
    def __init__(self, transport: ResendMCPTransport | None = None):
        self.transport = transport or ResendMCPTransport()
        self.lock = asyncio.Lock()

    # Execute only the two explicitly supported read-only receiving tools.
    async def read(self, name: str, arguments: dict) -> dict:
        if name not in ("list-received-emails", "get-received-email"):
            raise ValueError("Only read-only receiving tools are available")
        async with self.lock:
            async with asyncio.timeout(35):
                async with self.transport._session_factory() as session:
                    schemas = {}
                    cursor = None
                    for _ in range(10):
                        if cursor:
                            from mcp.types import PaginatedRequestParams
                            listing = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
                        else:
                            listing = await session.list_tools()
                        for tool in _get(listing, "tools", default=[]) or []:
                            schemas[_get(tool, "name")] = _get(tool, "input_schema", "inputSchema", {})
                        cursor = _get(listing, "next_cursor", "nextCursor")
                        if not cursor:
                            break
                    if name not in schemas:
                        raise EmailTransportError("resend_inbox_tool_unavailable")
                    args = {**self.transport._tool_arguments("The application checks received messages for registered conversation addresses and routes only verified replies into the existing qualification workflow."), **arguments}
                    if set(schemas[name].get("required", [])) - args.keys():
                        raise EmailTransportError("resend_inbox_schema_unsupported")
                    result = await session.call_tool(name, args)
                    try:
                        self.transport._remember_conversation(result, _payload(result))
                    except EmailTransportError:
                        pass
                    payload = list_payload(result) if name == "list-received-emails" else received_payload(result)
                    self.transport._remember_conversation(result, payload)
                    return payload

    # List at most one bounded page of received metadata.
    async def list_received(self, *, limit: int = 25, after: str | None = None) -> dict:
        args = {"limit": min(max(limit, 1), 25)}
        if after:
            args["after"] = after
        return await self.read("list-received-emails", args)

    # Retrieve content only after its recipient matches a registered reply address.
    async def get_received(self, id_: str) -> dict:
        return await self.read("get-received-email", {"id": id_})


class ResendInboxPoller:
    # Keep provider fetching separate from the durable matching and workflow callbacks.
    def __init__(self, store: InboxStore, *, client: ResendInboxClient | None = None,
                 account: str = "default", interval_seconds: float = 15,
                 lookup_run: Callable | None = None, **callbacks):
        self.store = store
        self.client = client or ResendInboxClient()
        self.account = account
        self.interval = max(15, float(interval_seconds))
        self.callbacks = callbacks
        self.lookup_run = lookup_run
        self.task = None
        self.last_checked = None
        self.connected = False
        self.error_code = None

    # Fetch one page and advance its cursor only after matching messages are durably saved.
    async def once(self) -> dict:
        await self.store.recover(limit=25, **self.callbacks)
        if self.lookup_run:
            self.store.reconcile(self.lookup_run)
        raw = self.store.checkpoint("resend", self.account)
        checkpoint = json.loads(raw) if raw else {}
        page = await self.client.list_received(limit=25, after=checkpoint.get("after"))
        rows = page["items"]
        head = checkpoint.get("pendingHead") or (rows[0].get("id") if rows else checkpoint.get("head"))
        old_head = checkpoint.get("stopAt") or checkpoint.get("head")
        seen_old = False
        processed = 0
        for metadata in rows:
            if metadata.get("id") == old_head:
                seen_old = True
                break
            recipients = metadata.get("to") or []
            if isinstance(recipients, str):
                recipients = [recipients]
            if not self.store.recognizes_recipient(recipients):
                continue
            email = normalize(await self.client.get_received(str(metadata["id"])))
            if email.provider_id != str(metadata["id"]):
                raise EmailTransportError("resend_inbox_identifier_mismatch")
            self.store.receive("resend", email)
            await self.store.ingest("resend", email, **self.callbacks)
            processed += 1
        # On first setup, do not crawl unrelated historical pages. Later bursts
        # continue with bounded pages until the previous checkpoint is reached.
        more = bool(page.get("hasMore") and not seen_old and old_head and rows)
        next_checkpoint = {"head": checkpoint.get("head"), "pendingHead": head, "stopAt": old_head, "after": rows[-1]["id"]} if more else {"head": head}
        self.store.checkpoint("resend", self.account, json.dumps(next_checkpoint))
        self.last_checked, self.connected, self.error_code = timestamp(), True, None
        return {"checked": len(rows), "matched": processed, "morePending": more}

    # Poll quietly, exposing safe status codes rather than provider exception bodies.
    async def loop(self):
        while True:
            try:
                await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connected, self.error_code = False, "reply_receiving_unavailable"
                self.last_checked = timestamp()
            await asyncio.sleep(self.interval)

    # Start a single receiving task after the application explicitly sets up inbox storage.
    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.loop())

    # Stop polling without losing persisted events or checkpoint progress.
    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    # Return user-facing connection state without keys, provider payloads or message bodies.
    def status(self) -> dict:
        return {"connected": self.connected, "label": "Replies connected" if self.connected else "Replies need setup",
                "lastCheckedAt": self.last_checked, "errorCode": self.error_code}
