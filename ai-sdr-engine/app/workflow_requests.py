# app/workflow_requests.py - Durable campaign input and result references in the canonical database
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from app import config


# Open campaign request storage without adding fields to the shared workflow contracts.
@contextmanager
def connection():
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS workflow_requests (
        run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT)''')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# Save a bounded application input; credentials and email bodies are not accepted here.
def save(run_id: str, payload: dict):
    if set(payload) - {'campaignInput', 'campaignId'}:
        raise ValueError('workflow_request_invalid')
    with connection() as conn:
        conn.execute('INSERT OR IGNORE INTO workflow_requests(run_id,payload) VALUES(?,?)',
                     (run_id, json.dumps(payload)))


# Retrieve persisted input or the completed response after a process restart.
def get(run_id: str):
    with connection() as conn:
        row = conn.execute('SELECT * FROM workflow_requests WHERE run_id=?', (run_id,)).fetchone()
    return {'payload': json.loads(row['payload']), 'result': json.loads(row['result']) if row['result'] else None} if row else None


# Store the native API response without changing its existing shape.
def finish(run_id: str, result: dict):
    with connection() as conn:
        conn.execute('UPDATE workflow_requests SET result=? WHERE run_id=?',
                     (json.dumps(result, default=lambda value: value.model_dump(mode='json')), run_id))
