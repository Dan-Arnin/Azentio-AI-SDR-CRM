# app/events.py - In-process async pub/sub for live pipeline events (SSE feed for the console)
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, AsyncIterator, Optional

from app.contracts import new_id, now_iso

_RECENT_LIMIT = 500
_QUEUE_LIMIT = 1000

_recent: deque[dict[str, Any]] = deque(maxlen=_RECENT_LIMIT)
_subscribers: set[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = set()


# Record an event and fan it out to every live subscriber (safe from any thread)
def publish(type_: str, lead_id: Optional[str] = None, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    event = {"id": new_id("evt"), "type": type_, "leadId": lead_id, "occurredAt": now_iso(), "payload": data or {}}
    _recent.append(event)
    for queue, loop in list(_subscribers):
        _deliver(queue, loop, event)
    return event


# Push an event into one subscriber queue on its own loop, dropping the oldest item when full
def _deliver(queue: asyncio.Queue, loop: asyncio.AbstractEventLoop, event: dict[str, Any]) -> None:
    # Enqueue on the subscriber's loop, evicting the oldest event when the queue is full
    def put() -> None:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(event)

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        put()
    elif not loop.is_closed():
        loop.call_soon_threadsafe(put)


# Yield live events as they arrive; yields None after `keepalive` idle seconds so SSE can send a comment
async def subscribe(keepalive: Optional[float] = 15.0) -> AsyncIterator[Optional[dict[str, Any]]]:
    entry = (asyncio.Queue(maxsize=_QUEUE_LIMIT), asyncio.get_running_loop())
    _subscribers.add(entry)
    try:
        while True:
            try:
                yield await asyncio.wait_for(entry[0].get(), timeout=keepalive)
            except asyncio.TimeoutError:
                yield None
    finally:
        _subscribers.discard(entry)


# Most recent events, oldest first, optionally filtered by lead
def recent(limit: int = 100, lead_id: Optional[str] = None) -> list[dict[str, Any]]:
    items = [e for e in _recent if lead_id is None or e["leadId"] == lead_id]
    return items[-limit:]


# Forget the recent-event buffer (demo reset); live subscribers stay connected
def clear() -> None:
    _recent.clear()
