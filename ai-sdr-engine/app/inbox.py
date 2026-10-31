# app/inbox.py - Durable provider-neutral inbox matching, deduplication and reply ingestion
from __future__ import annotations

import hashlib
import inspect
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import sqlstore
from app.contracts import ReplyInput

HEADER_ALLOWLIST = {"message-id", "in-reply-to", "references", "x-sdr-thread", "auto-submitted", "precedence", "reply-to"}
TERMINAL = {"ingested", "quarantined"}


# Return a UTC timestamp suitable for durable lease comparisons.
def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


# Normalize a single mailbox address without retaining display names.
def address(value: str) -> str:
    parsed = parseaddr(str(value))[1].strip().casefold()
    return parsed if "@" in parsed and not any(c in parsed for c in "\r\n") else ""


# Normalize an RFC Message-ID while preserving its case-sensitive identifier.
def reference(value: str) -> str:
    return str(value).strip().strip("<>").strip()


# Mint an opaque correlation token, not a lead identifier or credential.
def new_correlation_token() -> str:
    return secrets.token_urlsafe(24)


# Retain only newly authored text, excluding quoted Gmail and forwarded history.
def strip_quoted_history(text: str) -> str:
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    fresh = []
    for index, line in enumerate(lines):
        unquoted = line.lstrip(" >\t")
        if re.match(r"(?i)^On .{1,800} wrote:\s*$", unquoted):
            break
        if re.match(r"(?i)^On .{1,800}$", unquoted):
            wrapped = " ".join(part.strip() for part in lines[index:index + 3])
            if re.match(r"(?i)^On .{1,1500} wrote:\s*$", wrapped):
                break
        if re.match(r"(?i)^\s*(?:-+\s*Original Message\s*-+|Begin forwarded message:)\s*$", line):
            break
        if re.match(r"(?i)^\s*From:\s*.+", line):
            following = lines[index:index + 9]
            names = {match.group(1).lower() for part in following if (match := re.match(r"(?i)^\s*(From|Sent|Date|To|Subject):", part))}
            if "from" in names and len(names & {"sent", "date", "to", "subject"}) >= 2:
                break
        if re.match(r"^\s*>", line):
            continue
        fresh.append(line)
    return "\n".join(fresh).strip()


class NormalizedEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=1, max_length=500)
    from_email: str = Field(max_length=500)
    to_addresses: list[str] = Field(default_factory=list, max_length=100)
    subject: str = Field(default="", max_length=1000)
    body_text: str = Field(default="", max_length=12000)
    raw_body_text: str | None = Field(default=None, max_length=12000)
    raw_body_truncated: bool = False
    quote_stripped: bool = False
    received_at: str = Field(default_factory=timestamp, max_length=100)
    in_reply_to: str | None = Field(default=None, max_length=2000)
    references: list[str] = Field(default_factory=list, max_length=100)
    thread_hint: str | None = Field(default=None, max_length=500)
    headers: dict[str, str] = Field(default_factory=dict)
    is_outbound: bool = False
    body_truncated: bool = False

    # Bound stored content and drop credential-bearing or irrelevant headers.
    @model_validator(mode="before")
    @classmethod
    def sanitize(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        data = dict(raw)
        original = str(data.get("body_text") or "")
        raw_body = str(data.get("raw_body_text") if data.get("raw_body_text") is not None else original)
        data["raw_body_text"] = raw_body[:12000]
        data["raw_body_truncated"] = bool(data.get("raw_body_truncated") or len(raw_body) > 12000)
        body = strip_quoted_history(original)
        data["quote_stripped"] = bool(data.get("quote_stripped") or body != original.strip())
        data["body_text"] = body
        if len(body) > 12000:
            data.update(body_text=body[:12000], body_truncated=True)
        headers = data.get("headers") or {}
        data["headers"] = {str(k).lower(): str(v)[:2000] for k, v in headers.items() if str(k).lower() in HEADER_ALLOWLIST}
        return data


# Await optional asynchronous callbacks while also accepting small synchronous hooks.
async def invoke(callback: Callable, *args):
    result = callback(*args)
    return await result if inspect.isawaitable(result) else result


class InboxStore:
    # Use the canonical engine database, with an explicit injectable path for isolated checks.
    def __init__(self, path: Path | str | None = None):
        if path is None:
            from app.config import DB_PATH
            path = DB_PATH
        self.path = Path(path)

    # Open a transaction without importing or mutating workflow stores.
    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlstore.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Create inbox-owned tables explicitly; no provider calls or workflow processing occur.
    def ensure_schema(self) -> None:
        with self.transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS inbox_events (
                    provider TEXT NOT NULL, provider_id TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
                    reason TEXT, lead_id TEXT, thread_id TEXT, match_basis TEXT, run_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_until TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider, provider_id)
                );
                CREATE INDEX IF NOT EXISTS inbox_events_status ON inbox_events(status, updated_at);
                CREATE TABLE IF NOT EXISTS inbox_aliases (
                    kind TEXT NOT NULL, value TEXT NOT NULL, lead_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL, expected_sender TEXT NOT NULL,
                    allowed_senders TEXT NOT NULL, provider TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(kind,value,lead_id,thread_id,expected_sender,provider)
                );
                CREATE INDEX IF NOT EXISTS inbox_aliases_value ON inbox_aliases(kind,value);
                CREATE TABLE IF NOT EXISTS inbox_checkpoints (
                    provider TEXT NOT NULL, account TEXT NOT NULL, cursor TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(provider,account)
                );
            """)

    # Register matching aliases only after an actual outbound has been provider-accepted.
    def register_outbound(self, *, lead_id: str, thread_id: str, expected_sender: str,
                          rfc_message_id: str | None = None, provider_message_id: str | None = None,
                          provider: str = "", correlation_token: str | None = None,
                          reply_address: str | None = None, allowed_reply_senders: list[str] | None = None,
                          actual_outbound: bool = True) -> None:
        if not actual_outbound:
            raise ValueError("Email previews cannot register inbox aliases")
        expected = address(expected_sender)
        if not lead_id or not thread_id or not expected:
            raise ValueError("A lead, thread and expected sender are required")
        aliases: list[tuple[str, str]] = []
        if rfc_message_id:
            aliases.extend([("reference", reference(rfc_message_id)), ("outbound_rfc", reference(rfc_message_id))])
        if provider_message_id:
            aliases.extend([("reference", reference(provider_message_id)), ("outbound_provider", provider_message_id)])
        if correlation_token:
            if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", correlation_token):
                raise ValueError("Correlation token must be opaque and at least 20 characters")
            aliases.append(("thread_token", correlation_token))
        if reply_address:
            if not address(reply_address):
                raise ValueError("Reply address must be a mailbox address")
            aliases.append(("reply_address", address(reply_address)))
        if not aliases:
            raise ValueError("At least one exact reply reference is required")
        allowed = json.dumps(sorted({address(v) for v in allowed_reply_senders or [] if address(v)}))
        with self.transaction() as conn:
            for kind, value in aliases:
                conn.execute("INSERT OR IGNORE INTO inbox_aliases VALUES(?,?,?,?,?,?,?,?)",
                             (kind, value, lead_id, thread_id, expected, allowed, provider, timestamp()))

    # Check whether a recipient has an exact registered reply alias before retrieving its body.
    def recognizes_recipient(self, recipients: list[str]) -> bool:
        normalized = [address(v) for v in recipients if address(v)]
        if not normalized:
            return False
        with self.transaction() as conn:
            return any(conn.execute("SELECT 1 FROM inbox_aliases WHERE kind='reply_address' AND value=? LIMIT 1", (v,)).fetchone() for v in normalized)

    # Store an incoming message before any pipeline call and detect conflicting provider identifiers.
    def receive(self, provider: str, email: NormalizedEmail) -> dict:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", provider):
            raise ValueError("Invalid inbox provider name")
        payload = email.model_dump(mode="json")
        serialized = json.dumps(payload, sort_keys=True)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        message_id = f"inbox_{provider}_{hashlib.sha256(email.provider_id.encode()).hexdigest()}"
        with self.transaction() as conn:
            old = conn.execute("SELECT * FROM inbox_events WHERE provider=? AND provider_id=?", (provider, email.provider_id)).fetchone()
            if old:
                if old["payload_hash"] != digest:
                    conn.execute("UPDATE inbox_events SET status='quarantined',reason='provider_id_conflict',updated_at=? WHERE provider=? AND provider_id=?", (timestamp(), provider, email.provider_id))
                return dict(conn.execute("SELECT * FROM inbox_events WHERE provider=? AND provider_id=?", (provider, email.provider_id)).fetchone())
            at = timestamp()
            conn.execute("INSERT INTO inbox_events(provider,provider_id,message_id,payload,payload_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,'received',?,?)", (provider, email.provider_id, message_id, serialized, digest, at, at))
            return dict(conn.execute("SELECT * FROM inbox_events WHERE provider=? AND provider_id=?", (provider, email.provider_id)).fetchone())

    # Claim an event with a short lease so an interrupted ingestion can safely be replayed.
    def claim(self, provider: str, provider_id: str) -> dict | None:
        at, owner = timestamp(), secrets.token_hex(12)
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM inbox_events WHERE provider=? AND provider_id=?", (provider, provider_id)).fetchone()
            if not row or row["status"] in TERMINAL or row["status"] == "queued":
                return None
            if row["status"] == "processing" and row["lease_until"] and row["lease_until"] > at:
                return None
            until = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
            conn.execute("UPDATE inbox_events SET status='processing',lease_owner=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE provider=? AND provider_id=?", (owner, until, at, provider, provider_id))
            return dict(conn.execute("SELECT * FROM inbox_events WHERE provider=? AND provider_id=?", (provider, provider_id)).fetchone())

    # Finish a claimed event only if this worker still owns its lease.
    def finish(self, event: dict, status: str, reason: str | None = None, **values) -> dict:
        allowed = {key: values.get(key, event.get(key)) for key in ("lead_id", "thread_id", "match_basis", "run_id")}
        with self.transaction() as conn:
            conn.execute("UPDATE inbox_events SET status=?,reason=?,lead_id=?,thread_id=?,match_basis=?,run_id=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE provider=? AND provider_id=? AND lease_owner=? AND status='processing'", (status, reason, allowed["lead_id"], allowed["thread_id"], allowed["match_basis"], allowed["run_id"], timestamp(), event["provider"], event["provider_id"], event["lease_owner"]))
            return dict(conn.execute("SELECT * FROM inbox_events WHERE provider=? AND provider_id=?", (event["provider"], event["provider_id"])).fetchone())

    # Resolve exact references or registered recipient tokens; subjects never identify a lead alone.
    def match(self, provider: str, email: NormalizedEmail) -> dict:
        if email.is_outbound:
            return {"status": "quarantined", "reason": "outbound_message"}
        headers = email.headers
        refs = [reference(v) for v in email.references]
        refs += re.findall(r"<([^<>]+)>", email.in_reply_to or headers.get("in-reply-to", ""))
        if email.in_reply_to and not re.search(r"[<>\s]", email.in_reply_to):
            refs.append(reference(email.in_reply_to))
        refs += re.findall(r"<([^<>]+)>", headers.get("references", ""))
        tokens = [headers.get("x-sdr-thread", "").strip()]
        tokens += re.findall(r"\[SDR:([A-Za-z0-9_-]{20,100})\]", email.subject)
        selectors = [("reference", v) for v in refs if v] + [("thread_token", v) for v in tokens if v]
        selectors += [("reply_address", address(v)) for v in email.to_addresses if address(v)]
        matches = []
        with self.transaction() as conn:
            own_rfc = reference(headers.get("message-id", ""))
            if own_rfc and conn.execute("SELECT 1 FROM inbox_aliases WHERE kind='outbound_rfc' AND value=?", (own_rfc,)).fetchone():
                return {"status": "quarantined", "reason": "outbound_message"}
            if conn.execute("SELECT 1 FROM inbox_aliases WHERE kind='outbound_provider' AND provider=? AND value=?", (provider, email.provider_id)).fetchone():
                return {"status": "quarantined", "reason": "outbound_message"}
            for kind, value in selectors:
                matches.extend(dict(row) | {"basis": kind} for row in conn.execute("SELECT * FROM inbox_aliases WHERE kind=? AND value=?", (kind, value)).fetchall())
        targets = {(m["lead_id"], m["thread_id"]) for m in matches}
        if not targets:
            return {"status": "unmatched", "reason": "no_exact_thread_match"}
        if len(targets) != 1:
            return {"status": "quarantined", "reason": "ambiguous_thread_match"}
        lead_id, thread_id = next(iter(targets))
        return {"status": "matched", "lead_id": lead_id, "thread_id": thread_id, "aliases": matches, "match_basis": ",".join(sorted({m["basis"] for m in matches}))}

    # Process one saved event with injected idempotent workflow and lead-lookup callbacks.
    async def ingest(self, provider: str, email: NormalizedEmail, *, ingest_reply: Callable | None = None,
                     lookup_lead: Callable | None = None, on_matched_reply: Callable | None = None) -> dict:
        saved = self.receive(provider, email)
        event = self.claim(provider, email.provider_id)
        if event is None:
            return self.public_event(saved) | {"duplicate": True}
        matched = self.match(provider, email)
        if matched["status"] != "matched":
            return self.public_event(self.finish(event, matched["status"], matched["reason"]))
        if lookup_lead is None or ingest_reply is None:
            from app import pipeline
            lookup_lead = lookup_lead or pipeline.get_lead
            ingest_reply = ingest_reply or pipeline.ingest_reply
        try:
            lead = await invoke(lookup_lead, matched["lead_id"])
            lead_data = lead.model_dump() if isinstance(lead, BaseModel) else lead
            expected = address(lead_data.get("normalizedEmail") or lead_data.get("email") or lead_data.get("input", {}).get("email", ""))
            sender = address(email.from_email)
            aliases = matched["aliases"]
            alias_expected = {m["expected_sender"] for m in aliases}
            permitted = {v for m in aliases for v in json.loads(m["allowed_senders"])}
            if not sender or alias_expected != {expected} or (sender != expected and sender not in permitted):
                return self.public_event(self.finish(event, "quarantined", "sender_mismatch", **{k: matched[k] for k in ("lead_id", "thread_id", "match_basis")}))
            if on_matched_reply:
                await invoke(on_matched_reply, matched["lead_id"])
            if email.body_truncated:
                return self.public_event(self.finish(event, "quarantined", "message_too_long_for_automatic_review", **{k: matched[k] for k in ("lead_id", "thread_id", "match_basis")}))
            reply = ReplyInput(messageId=event["message_id"], threadId=matched["thread_id"], bodyText=email.body_text, receivedAt=email.received_at, origin="test_inbox")
            result = await invoke(ingest_reply, matched["lead_id"], reply)
            run_id = result.get("runId") if isinstance(result, dict) else None
            if isinstance(result, dict) and result.get("duplicate") and not run_id:
                pending = self.finish(event, "received", "assessment_queue_not_confirmed", run_id=None,
                                      **{k: matched[k] for k in ("lead_id", "thread_id", "match_basis")})
                return self.public_event(pending)
            finished = self.finish(event, "queued" if run_id else "ingested", run_id=run_id, **{k: matched[k] for k in ("lead_id", "thread_id", "match_basis")})
            return self.public_event(finished) | {"controlledSender": sender != expected}
        except Exception:
            finished = self.finish(event, "received", "ingestion_retry_needed", **{k: matched[k] for k in ("lead_id", "thread_id", "match_basis")})
            return self.public_event(finished)

    # Recover persisted received events after a restart; the engine callback must dedupe message IDs.
    async def recover(self, *, limit: int = 25, **callbacks) -> list[dict]:
        with self.transaction() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM inbox_events WHERE status='received' OR (status='processing' AND lease_until<?) ORDER BY created_at LIMIT ?", (timestamp(), max(1, min(limit, 100)))).fetchall()]
        return [await self.ingest(row["provider"], NormalizedEmail.model_validate_json(row["payload"]), **callbacks) for row in rows]

    # Read the owning durable job without importing a process-local worker or exposing its payload.
    def job_status(self, run_id: str) -> dict | None:
        with self.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sdr_jobs'").fetchone()
            if not exists:
                return None
            row = conn.execute("SELECT status,error_code FROM sdr_jobs WHERE run_id=?", (run_id,)).fetchone()
            return {"status": row["status"], "errorCode": row["error_code"]} if row else None

    # Finalize only after durable retries finish; native per-attempt failure is not terminal.
    def reconcile(self, lookup_run: Callable, lookup_job: Callable | None = None) -> int:
        with self.transaction() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM inbox_events WHERE status='queued' AND run_id IS NOT NULL").fetchall()]
        count = 0
        for row in rows:
            try:
                job = (lookup_job or self.job_status)(row["run_id"])
                job = job.model_dump() if isinstance(job, BaseModel) else job
                durable_status = job.get("status") if job else None
                if job and durable_status not in ("succeeded", "failed", "cancelled"):
                    continue
                run = lookup_run(row["run_id"])
                result = run.model_dump() if isinstance(run, BaseModel) else run
            except Exception:
                continue
            if durable_status not in ("failed", "cancelled") and result.get("status") not in ("succeeded", "failed"):
                continue
            reason = ("qualification_cancelled" if durable_status == "cancelled" else
                      "qualification_failed" if durable_status == "failed" or result.get("status") == "failed" else None)
            with self.transaction() as conn:
                changed = conn.execute("UPDATE inbox_events SET status='ingested',reason=?,updated_at=? WHERE provider=? AND provider_id=? AND status='queued' AND run_id=?", (reason, timestamp(), row["provider"], row["provider_id"], row["run_id"])).rowcount
            count += changed
        return count

    # Persist a provider cursor only after its fetched messages have been durably received.
    def checkpoint(self, provider: str, account: str, cursor: str | None = None) -> str | None:
        with self.transaction() as conn:
            if cursor is not None:
                conn.execute("INSERT INTO inbox_checkpoints VALUES(?,?,?,?) ON CONFLICT(provider,account) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at", (provider, account, cursor, timestamp()))
            row = conn.execute("SELECT cursor FROM inbox_checkpoints WHERE provider=? AND account=?", (provider, account)).fetchone()
            return row["cursor"] if row else None

    # Return redacted operational metadata, never message bodies or authentication headers.
    def public_event(self, event: dict) -> dict:
        return {"source": "real_inbox", **{key: event.get(key) for key in ("provider", "provider_id", "message_id", "status", "reason", "lead_id", "thread_id", "match_basis", "run_id", "attempts", "created_at", "updated_at")}}

    # Summarize durable inbox state for a connection/status view.
    def status(self) -> dict:
        with self.transaction() as conn:
            counts = {row["status"]: row["n"] for row in conn.execute("SELECT status,COUNT(*) n FROM inbox_events GROUP BY status").fetchall()}
            aliases = conn.execute("SELECT COUNT(*) n FROM inbox_aliases WHERE kind='reply_address'").fetchone()["n"]
            latest = conn.execute("SELECT MAX(updated_at) at FROM inbox_events").fetchone()["at"]
        return {"source": "real_inbox", "counts": counts, "registeredReplyAddresses": aliases, "lastActivityAt": latest}
