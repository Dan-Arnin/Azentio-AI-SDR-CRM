# app/enquiry_notifications.py - Durable internal notifications for accepted website enquiries
from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
import hashlib
import json
import os
import sqlite3
import time
from urllib.parse import urlsplit
from uuid import uuid4

from app import sqlstore
from app import config
from app.contracts import LeadInput
from app.engagement.resend_mcp import EmailTransportError, ResendMCPTransport, _mailbox


# Keep the notification in the same transaction as its public enquiry receipt.
def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS enquiry_notifications (
        request_key TEXT PRIMARY KEY, lead_id TEXT NOT NULL, envelope TEXT NOT NULL,
        status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, available_at REAL NOT NULL,
        lease_until REAL, owner TEXT, provider_id TEXT, error_code TEXT)''')


# Construct one immutable notification; retries use precisely the same provider envelope.
def enqueue(conn: sqlite3.Connection, key: str, lead_id: str, inp: LeadInput, reference: str) -> None:
    ensure_schema(conn)
    base = os.getenv('SDR_PUBLIC_BASE_URL', 'http://localhost:8077').rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
        base = 'http://localhost:8077'
    product = {'lending_platform': 'Lending Platform', 'islamic_banking': 'Islamic Banking',
               'aml_compliance': 'AMLOCK Financial Crime & Compliance', 'unknown': 'Not specified'}
    values = [('Name', inp.fullName), ('Email', inp.email), ('Company', inp.companyName),
              ('Website', inp.companyDomain), ('Role', inp.role), ('Phone', inp.phone),
              ('Country / region', inp.country), ('Product', product.get(inp.productId, inp.productId)),
              ('Source', inp.source), ('Contact permission', inp.contactPermission)]
    body = 'A new enquiry has been submitted.\n\n' + '\n'.join(f'{label}: {value or "Not provided"}' for label, value in values)
    body += f'\n\nEnquiry\n{inp.inquiry or "No message provided."}\n\nReference: {reference}\nView lead: {base}/workspace#/leads/{lead_id}\n'
    company = ' '.join(inp.companyName.split())[:100] or 'Website visitor'
    envelope = {'to': config.DEMO_INBOX, 'sender': config.EMAIL_FROM,
                'subject': f'New Azentio enquiry: {company}', 'body': body}
    now = time.time()
    conn.execute('''INSERT OR IGNORE INTO enquiry_notifications
        (request_key,lead_id,envelope,status,created_at,available_at) VALUES(?,?,?,'queued',?,?)''',
        (key, lead_id, json.dumps(envelope), now, now))


class NotificationWorker:
    # Use a separate queue so internal notifications never wait for model processing.
    def __init__(self, path=None, *, send=None, interval: float = 1):
        self.path = path or config.DB_PATH
        self.send = send or self._send
        self.interval = interval
        self.owner = uuid4().hex
        self.task = None
        with self._tx() as conn:
            ensure_schema(conn)

    # Serialize claims across server processes and always close SQLite connections.
    @contextmanager
    def _tx(self):
        conn = sqlstore.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Persist the send lease before making any external call.
    def _claim(self):
        now = time.time()
        with self._tx() as conn:
            # Provider duplicate protection has a bounded retention window.
            conn.execute('''UPDATE enquiry_notifications SET status='needs_review',error_code='notification_retry_expired'
                WHERE status IN ('queued','retrying','sending') AND created_at<?''', (now - 23 * 3600,))
            conn.execute('''UPDATE enquiry_notifications SET status='needs_review',error_code='notification_attempts_exhausted'
                WHERE status='sending' AND lease_until<? AND attempts>=3''', (now,))
            row = conn.execute('''SELECT * FROM enquiry_notifications WHERE attempts<3 AND
                ((status IN ('queued','retrying') AND available_at<=?) OR (status='sending' AND lease_until<?))
                ORDER BY created_at LIMIT 1''', (now, now)).fetchone()
            if not row:
                return None
            conn.execute('''UPDATE enquiry_notifications SET status='sending',attempts=attempts+1,
                lease_until=?,owner=? WHERE request_key=?''', (now + 180, self.owner, row['request_key']))
            return dict(row)

    # Send only to the configured internal inbox, using a stable opaque idempotency key.
    async def _send(self, envelope: dict, idempotency_key: str) -> str:
        sender = _mailbox(envelope['sender'], display_name=True)
        client = ResendMCPTransport(api_key=config.RESEND_API_KEY, from_address=envelope['sender'],
                                    allowed_recipients=[config.DEMO_INBOX],
                                    test_mode=sender == 'onboarding@resend.dev')
        result = await client.send(to=envelope['to'], subject=envelope['subject'], body=envelope['body'],
                                   idempotency_key=idempotency_key, thread_id=idempotency_key)
        return result['providerMessageId']

    # Deliver one queued notification; transient failures retry without failing the form.
    async def once(self) -> bool:
        row = self._claim()
        if row is None:
            return False
        provider_id, error = None, None
        key = 'enquiry-' + hashlib.sha256(row['request_key'].encode()).hexdigest()
        try:
            provider_id = await self.send(json.loads(row['envelope']), key)
        except asyncio.CancelledError:
            # Keep the lease: a restart can safely retry the identical provider key.
            raise
        except EmailTransportError as exc:
            error = exc.code
        except Exception:
            error = 'notification_delivery_unavailable'
        with self._tx() as conn:
            status = 'accepted' if provider_id else 'failed' if row['attempts'] + 1 >= 3 else 'retrying'
            conn.execute('''UPDATE enquiry_notifications SET status=?,provider_id=?,error_code=?,
                lease_until=NULL,owner=NULL,available_at=? WHERE request_key=? AND owner=?''',
                (status, provider_id, error, time.time() + 10 * (row['attempts'] + 1), row['request_key'], self.owner))
        return True

    # Continue across individual delivery failures without leaking provider diagnostics.
    async def _loop(self):
        while True:
            try:
                worked = await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                worked = False
            await asyncio.sleep(0 if worked else self.interval)

    # Start at application startup; queued receipts survive process restarts.
    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    # Stop cleanly while preserving any unfinished delivery lease.
    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
