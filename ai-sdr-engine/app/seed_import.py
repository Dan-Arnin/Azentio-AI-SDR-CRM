# app/seed_import.py - Load the demo workspace snapshot (seed-data/) into an empty engine database, once
#
# Set ENGINE_SEED_FILE (e.g. seed-data/sdr-demo.sqlite3). At startup, if the engine database has no leads and this
# snapshot was never imported, every table in the snapshot is copied in (rows are only added, never overwritten).
# Works for SQLite and PostgreSQL alike because it writes through app.sqlstore.
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app import config, sqlstore

log = logging.getLogger("uvicorn.error")
MARKER = "seed_imports"


def seed_path() -> Optional[Path]:
    value = os.environ.get("ENGINE_SEED_FILE", "").strip()
    if not value:
        return None
    path = Path(value)
    path = path if path.is_absolute() else config.ROOT / path
    return path if path.is_file() else None


def import_if_empty() -> dict[str, Any]:
    src_path = seed_path()
    if not src_path:
        return {"imported": False, "reason": "no seed file configured"}
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlstore.connect(config.DB_PATH, timeout=60)
    try:
        dst.execute(f"CREATE TABLE IF NOT EXISTS {MARKER} (name TEXT PRIMARY KEY, imported_at TEXT NOT NULL, detail TEXT)")
        tables = [(n, sql) for n, sql in src.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for _, sql in tables:  # make sure every table exists before checking or writing
            dst.execute(re.sub(r"^\s*CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?", "CREATE TABLE IF NOT EXISTS ", sql, flags=re.I))
        dst.execute("BEGIN IMMEDIATE")
        if dst.execute(f"SELECT 1 FROM {MARKER} WHERE name = ?", (src_path.name,)).fetchone():
            dst.rollback()
            return {"imported": False, "reason": "already imported"}
        if dst.execute("SELECT 1 FROM leads LIMIT 1").fetchone():
            dst.rollback()
            log.info("seed import skipped: the workspace already has leads")
            return {"imported": False, "reason": "workspace already has leads"}
        counts = {name: _copy_table(src, dst, name) for name, _ in tables}
        dst.execute(f"INSERT INTO {MARKER} (name, imported_at, detail) VALUES (?, ?, ?)",
                    (src_path.name, datetime.now(timezone.utc).isoformat(), json.dumps(counts)))
        dst.commit()
    except Exception:
        dst.rollback()
        raise
    finally:
        dst.close()
        src.close()
    _restore_clock(src_path.parent / "clock.json")
    log.info("seed import: %s rows from %s", sum(counts.values()), src_path.name)
    return {"imported": True, "rows": counts}


def _columns(conn, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _copy_table(src: sqlite3.Connection, dst, table: str) -> int:
    target = set(_columns(dst, table))
    cols = [c for c in _columns(src, table) if c in target and c != "rowid"]
    if not cols:
        return 0
    insert = f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    n = 0
    for row in src.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid"):
        dst.execute(insert, tuple(row))
        n += 1
    return n


def _restore_clock(path: Path) -> None:
    """The demo clock lives in DATA_DIR (a file, not the database), so it resets when the host's disk does."""
    if path.is_file() and not (config.DATA_DIR / "clock.json").exists():
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, config.DATA_DIR / "clock.json")
