# app/engagement/clock.py - Demo clock: real UTC time plus a persisted offset so sequences can be fast-forwarded
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import config

_lock = threading.RLock()
_offset_hours: float = 0.0


# Path of the persisted clock offset file
def _clock_path() -> Path:
    return config.DATA_DIR / "clock.json"


# Load the persisted offset (shared across processes) into the module-level cache
def _load() -> float:
    global _offset_hours
    try:
        _offset_hours = float(json.loads(_clock_path().read_text()).get("offsetHours", 0.0))
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        _offset_hours = 0.0
    return _offset_hours


# Persist the current offset to disk
def _save(offset: float) -> None:
    global _offset_hours
    _offset_hours = offset
    path = _clock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"offsetHours": offset, "savedAt": datetime.now(timezone.utc).isoformat()}))


# Current demo time (UTC, timezone-aware) = wall clock + offset
def demo_now() -> datetime:
    with _lock:
        return datetime.now(timezone.utc) + timedelta(hours=_load())


# Current demo time as ISO 8601 string
def demo_now_iso() -> str:
    return demo_now().isoformat()


# Current offset in hours
def offset_hours() -> float:
    with _lock:
        return _load()


# Move the demo clock forward by N hours and return the new demo time
def advance(hours: float) -> datetime:
    with _lock:
        _save(_load() + float(hours))
        return demo_now()


# Reset the demo clock back to wall-clock time
def reset() -> datetime:
    with _lock:
        _save(0.0)
        return demo_now()


# Calendar date of a timestamp in the demo timezone (used for daily send caps)
def demo_date(ts: datetime | str) -> str:
    dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(config.DEMO_TIMEZONE)).date().isoformat()


# Parse an ISO timestamp into an aware UTC datetime
def parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
