# app/reply_routes.py - Durable opaque reply addresses for provider-accepted email conversations
from __future__ import annotations

import os
import re
import secrets
import sqlite3
from contextlib import closing

from app import config
from app.inbox import InboxStore


# Validate an explicitly configured account-managed receiving domain.
def receiving_domain() -> str | None:
    domain = os.getenv('RESEND_RECEIVING_DOMAIN', '').strip().lower()
    return domain if re.fullmatch(r'[a-z0-9-]+\.resend\.app', domain) else None


# Reuse one persistent random alias per thread, rather than exposing a lead identifier.
def route_for(thread_id: str) -> dict | None:
    domain = receiving_domain()
    if not domain:
        return None
    with closing(sqlite3.connect(config.DB_PATH, timeout=15)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute('''CREATE TABLE IF NOT EXISTS reply_routes (
            thread_id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, domain TEXT NOT NULL)''')
        row = conn.execute('SELECT * FROM reply_routes WHERE thread_id=?', (thread_id,)).fetchone()
        if row is None or row['domain'] != domain:
            token = secrets.token_urlsafe(24)
            conn.execute('INSERT INTO reply_routes VALUES(?,?,?) ON CONFLICT(thread_id) DO UPDATE SET token=excluded.token,domain=excluded.domain',
                         (thread_id, token, domain))
        else:
            token = row['token']
    return {'token': token, 'address': token + '@' + domain}


# Admit reply aliases only after an actual outbound has been accepted by the provider.
def register_sent(lead, provider_id: str, route: dict | None) -> None:
    if route is None:
        return
    store = InboxStore(config.DB_PATH)
    store.ensure_schema()
    store.register_outbound(lead_id=lead.id, thread_id=lead.threadId, expected_sender=lead.normalizedEmail,
                           provider_message_id=provider_id, provider='resend', correlation_token=route['token'],
                           reply_address=route['address'], allowed_reply_senders=[config.DEMO_INBOX], actual_outbound=True)
