# app/crm/outbox.py - Durable signed CRM outbox with leased retries and idempotent workspace projection
from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
import json
import sqlite3
import time
from typing import Any, Optional
from uuid import uuid4

import httpx

from app import sqlstore
from app import config, db, events
from app.contracts import CrmEvent, now_iso
from app.crm import mock_crm

_RETRY_DELAYS = (0.5, 1.5)
_TIMEOUT = 5.0
_MAX_ATTEMPTS = 3
_LEASE_SECONDS = 30
_loop: Optional[asyncio.AbstractEventLoop] = None
_inflight: set[Any] = set()
_recovery_task: Optional[asyncio.Task] = None


# Reserve delivery state through an independent cross-process SQLite transaction.
@contextmanager
def _tx():
    db.conn()
    conn = sqlstore.connect(config.DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('''CREATE TABLE IF NOT EXISTS crm_delivery_leases
            (event_id TEXT PRIMARY KEY,owner TEXT,lease_until REAL,next_attempt_at REAL NOT NULL DEFAULT 0,retry_blocked INTEGER NOT NULL DEFAULT 0)''')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(crm_delivery_leases)')}
        if 'retry_blocked' not in columns:
            conn.execute('ALTER TABLE crm_delivery_leases ADD COLUMN retry_blocked INTEGER NOT NULL DEFAULT 0')
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# Remember the event loop so synchronous workflow emits can wake async delivery.
def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


# Stable signed envelope reused byte-for-byte when an event is retried.
def envelope(evt: CrmEvent) -> dict[str, Any]:
    return {'id': evt.id, 'type': evt.type, 'leadId': evt.leadId, 'occurredAt': evt.createdAt, 'payload': evt.payload}


# Persist before projection or scheduling so a process interruption cannot lose the event.
def emit(type_: str, lead_id: Optional[str], payload: dict[str, Any]) -> CrmEvent:
    evt = CrmEvent(type=type_, leadId=lead_id, payload=payload)
    db.put('crm_events', evt)
    if not config.CRM_WEBHOOK_URL:
        mock_crm.record(envelope(evt), via='internal')
        evt.status = 'no_target'
        db.put('crm_events', evt)
    else:
        _schedule(evt)
    events.publish(type_, lead_id, {**payload, 'crmEventId': evt.id, 'delivery': evt.status})
    return evt


# Schedule best-effort immediate delivery; periodic recovery owns missed scheduling.
def _schedule(evt: CrmEvent) -> None:
    try:
        fut: Any = asyncio.get_running_loop().create_task(deliver(evt))
    except RuntimeError:
        if _loop is None or _loop.is_closed():
            return
        fut = asyncio.run_coroutine_threadsafe(deliver(evt), _loop)
    _inflight.add(fut)
    fut.add_done_callback(_inflight.discard)


# Acquire an event lease only when the durable event is eligible for another bounded attempt.
def _claim(event_id: str, owner: str) -> Optional[CrmEvent]:
    current = time.time()
    with _tx() as conn:
        row = conn.execute('SELECT body FROM crm_events WHERE id=?', (event_id,)).fetchone()
        if row is None:
            return None
        evt = CrmEvent.model_validate_json(row['body'])
        if evt.status == 'delivered':
            return None
        lease = conn.execute('SELECT * FROM crm_delivery_leases WHERE event_id=?', (event_id,)).fetchone()
        if lease and (lease['retry_blocked'] or (lease['lease_until'] or 0) > current or lease['next_attempt_at'] > current):
            return None
        if evt.attempts >= _MAX_ATTEMPTS and config.CRM_WEBHOOK_URL:
            if evt.status == 'pending':
                evt.status, evt.lastError = 'failed', 'delivery_attempt_limit'
                conn.execute('UPDATE crm_events SET body=? WHERE id=?', (evt.model_dump_json(), evt.id))
            return None
        conn.execute('''INSERT INTO crm_delivery_leases(event_id,owner,lease_until,next_attempt_at)
            VALUES(?,?,?,0) ON CONFLICT(event_id) DO UPDATE SET owner=excluded.owner,
            lease_until=excluded.lease_until''', (event_id, owner, current + _LEASE_SECONDS))
        return evt


# Persist an attempt only while this delivery invocation still holds its lease.
def _save(evt: CrmEvent, owner: str, *, release: bool = False, retry_at: float = 0, retry_blocked: bool = False) -> bool:
    with _tx() as conn:
        row = conn.execute('SELECT owner FROM crm_delivery_leases WHERE event_id=?', (evt.id,)).fetchone()
        if row is None or row['owner'] != owner:
            return False
        conn.execute('UPDATE crm_events SET body=? WHERE id=?', (evt.model_dump_json(), evt.id))
        conn.execute('''UPDATE crm_delivery_leases SET owner=?,lease_until=?,next_attempt_at=?,retry_blocked=?
            WHERE event_id=? AND owner=?''',
            (None if release else owner, None if release else time.time() + _LEASE_SECONDS,
             retry_at, int(retry_blocked), evt.id, owner))
        return True


# POST a signed event using its stable event ID and safe, bounded retry metadata.
async def deliver(evt: CrmEvent) -> CrmEvent:
    owner = uuid4().hex
    current = _claim(evt.id, owner)
    if current is None:
        return db.get('crm_events', evt.id, CrmEvent) or evt
    evt = current
    try:
        if not config.CRM_WEBHOOK_URL:
            mock_crm.record(envelope(evt), via='internal')
            evt.status, evt.lastError = 'no_target', None
            _save(evt, owner, release=True)
            return evt
        body = json.dumps(envelope(evt), separators=(',', ':')).encode()
        headers = {'Content-Type': 'application/json', 'X-SDR-Signature': mock_crm.sign(body),
                   'X-SDR-Event': evt.type, 'X-SDR-Event-Id': evt.id, 'Idempotency-Key': evt.id}
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            while evt.attempts < _MAX_ATTEMPTS:
                evt.attempts += 1
                evt.status = 'pending'
                if not _save(evt, owner):
                    return db.get('crm_events', evt.id, CrmEvent) or evt
                retryable = True
                try:
                    response = await client.post(config.CRM_WEBHOOK_URL, content=body, headers=headers)
                    if 200 <= response.status_code < 300:
                        evt.status, evt.lastError = 'delivered', None
                        _save(evt, owner, release=True)
                        break
                    evt.lastError = f'http_{response.status_code}'
                    retryable = response.status_code in (408, 409, 425, 429) or response.status_code >= 500
                except httpx.TimeoutException:
                    evt.lastError = 'delivery_timeout'
                except httpx.HTTPError:
                    evt.lastError = 'connection_failed'
                except Exception:
                    evt.lastError = 'delivery_error'
                if not retryable or evt.attempts >= _MAX_ATTEMPTS:
                    evt.status = 'failed'
                    _save(evt, owner, release=True, retry_blocked=not retryable)
                    break
                _save(evt, owner)
                await asyncio.sleep(_RETRY_DELAYS[min(evt.attempts - 1, len(_RETRY_DELAYS) - 1)])
    except asyncio.CancelledError:
        evt.status, evt.lastError = 'pending', 'delivery_interrupted'
        _save(evt, owner, release=True)
        raise
    except Exception:
        evt.status, evt.lastError = 'failed', 'delivery_error'
        _save(evt, owner, release=True, retry_at=time.time() + 2)
    events.publish('crm.delivery', evt.leadId, {'crmEventId': evt.id, 'eventType': evt.type,
        'status': evt.status, 'attempts': evt.attempts, 'lastError': evt.lastError, 'at': now_iso()})
    return evt


# Skip blocked or currently owned attempts without consuming the recovery batch limit.
def _ready_for_recovery(event_id: str) -> bool:
    with _tx() as conn:
        lease = conn.execute('SELECT * FROM crm_delivery_leases WHERE event_id=?', (event_id,)).fetchone()
        return lease is None or (not lease['retry_blocked'] and (lease['lease_until'] or 0) <= time.time()
                                 and lease['next_attempt_at'] <= time.time())


# Recover persisted pending or bounded retryable events, including missed internal projection writes.
async def recover_once(limit: int = 100) -> int:
    count = 0
    for evt in db.list_('crm_events', CrmEvent, limit=100000):
        external = bool(config.CRM_WEBHOOK_URL)
        eligible = ((evt.status == 'pending' or (evt.status == 'failed' and evt.attempts < _MAX_ATTEMPTS)) if external else
                    (evt.status != 'delivered' and not mock_crm.has_event(evt.id)))
        if eligible and _ready_for_recovery(evt.id):
            await deliver(evt)
            count += 1
            if count >= max(1, min(limit, 1000)):
                break
    return count


# Poll durable records so process death or a temporarily absent loop does not drop CRM events.
async def _recover_loop(interval_seconds: float):
    while True:
        try:
            await recover_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(max(0.05, min(interval_seconds, 60)))


# Start recovery without blocking application readiness on webhook availability.
async def start(interval_seconds: float = 2) -> None:
    global _recovery_task
    bind_loop(asyncio.get_running_loop())
    if _recovery_task is None or _recovery_task.done():
        _recovery_task = asyncio.create_task(_recover_loop(interval_seconds))


# Gracefully release in-flight leases; stable event IDs make subsequent retries idempotent.
async def stop() -> None:
    global _recovery_task
    tasks = [task for task in [_recovery_task, *_inflight] if task is not None]
    for task in tasks:
        task.cancel()
    for task in tasks:
        if isinstance(task, asyncio.Future):
            with suppress(asyncio.CancelledError, Exception):
                await task
    _recovery_task = None
    _inflight.clear()


# Outbox records, newest first, preserving the existing HTTP contract.
def outbox(limit: int = 200) -> list[CrmEvent]:
    return list(reversed(db.list_('crm_events', CrmEvent, limit=100000)))[:limit]
