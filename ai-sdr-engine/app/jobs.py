# app/jobs.py - Durable SQLite model jobs with cross-process leases, heartbeat and bounded recovery
from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from app import config

Execute = Callable[[str, Optional[str], str, dict[str, Any]], Awaitable[Any]]
_ALLOWED_PAYLOAD = {'kind', 'stage', 'version', 'inputVersion', 'leadVersion', 'draftKind'}
_SENSITIVE = re.compile(r'key|token|secret|password|credential|authorization|body|text|email|inquiry', re.I)


# Admit only record references or bounded workflow selectors, never source text or secrets.
def _safe_payload(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    value = payload or {}
    if not isinstance(value, dict):
        raise ValueError('job_payload_invalid')
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 80 or _SENSITIVE.search(key):
            raise ValueError('job_payload_invalid')
        allowed = key in _ALLOWED_PAYLOAD or key.lower().endswith(('id', 'ids', '_id', '_ids'))
        items = item if isinstance(item, list) else [item]
        if not allowed or len(items) > 100:
            raise ValueError('job_payload_invalid')
        if any(isinstance(x, bool) or not isinstance(x, (str, int, type(None))) or
               (isinstance(x, str) and (len(x) > 200 or '\n' in x or '\r' in x)) for x in items):
            raise ValueError('job_payload_invalid')
    return value


# Convert epoch lease times to explicit UTC timestamps in API metadata.
def _iso(value: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


class JobRunner:
    # Bind a trusted workflow callback and one canonical database without loading environment files.
    def __init__(self, execute: Execute, db_path=None, *, lease_seconds: float = 300,
                 heartbeat_seconds: float = 30, poll_seconds: float = 0.5, max_attempts: int = 3):
        self.execute = execute
        self.path = Path(db_path if db_path is not None else config.DB_PATH)
        self.owner = uuid4().hex
        self.lease_seconds = max(0.05, float(lease_seconds))
        self.heartbeat_seconds = min(max(0.01, float(heartbeat_seconds)), self.lease_seconds / 3)
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.max_attempts = max(1, min(3, int(max_attempts)))
        self.task: Optional[asyncio.Task] = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS sdr_jobs (
                run_id TEXT PRIMARY KEY, lead_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                owner TEXT, lease_until REAL, available_at REAL NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, error_code TEXT)''')
            conn.execute('CREATE INDEX IF NOT EXISTS ix_sdr_jobs_claim ON sdr_jobs(status,available_at,lease_until)')

    # Use independent connections and immediate transactions to serialize claims across processes.
    @contextmanager
    def _tx(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Render persisted metadata without traceback, request body, credentials or provider responses.
    def _view(self, row) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        return {'id': row['run_id'], 'runId': row['run_id'], 'leadId': row['lead_id'],
                'kind': row['kind'], 'payload': json.loads(row['payload']), 'status': row['status'],
                'attempts': row['attempts'], 'owner': row['owner'], 'leaseUntil': _iso(row['lease_until']),
                'availableAt': _iso(row['available_at']), 'createdAt': _iso(row['created_at']),
                'updatedAt': _iso(row['updated_at']), 'errorCode': row['error_code']}

    # Persist one immutable job per run before returning the submission receipt.
    def submit(self, run_id: str, lead_id: Optional[str], kind: str, payload=None) -> dict[str, Any]:
        if not run_id or len(run_id) > 200 or not kind or len(kind) > 80:
            raise ValueError('job_reference_invalid')
        encoded = json.dumps(_safe_payload(payload), sort_keys=True, separators=(',', ':'))
        current = time.time()
        with self._tx() as conn:
            old = conn.execute('SELECT * FROM sdr_jobs WHERE run_id=?', (run_id,)).fetchone()
            if old is not None:
                if (old['lead_id'], old['kind'], old['payload']) != (lead_id, kind, encoded):
                    raise ValueError('job_conflict')
                return self._view(old)
            conn.execute('''INSERT INTO sdr_jobs
                (run_id,lead_id,kind,payload,status,available_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)''', (run_id, lead_id, kind, encoded, 'queued', current, current, current))
            return self._view(conn.execute('SELECT * FROM sdr_jobs WHERE run_id=?', (run_id,)).fetchone())

    # Fetch one durable job for run polling.
    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._tx() as conn:
            return self._view(conn.execute('SELECT * FROM sdr_jobs WHERE run_id=?', (run_id,)).fetchone())

    # List recent jobs with bounded output.
    def list_jobs(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._tx() as conn:
            rows = conn.execute('SELECT * FROM sdr_jobs ORDER BY created_at DESC LIMIT ?',
                                (max(1, min(int(limit), 1000)),)).fetchall()
            return [self._view(row) for row in rows]

    # Atomically acquire the next queued or abandoned run, enforcing a total attempt bound.
    def claim(self) -> Optional[dict[str, Any]]:
        current = time.time()
        with self._tx() as conn:
            conn.execute('''UPDATE sdr_jobs SET status='failed',owner=NULL,lease_until=NULL,
                error_code='attempt_limit',updated_at=? WHERE attempts>=? AND
                (status='queued' OR (status='running' AND lease_until<=?))''',
                (current, self.max_attempts, current))
            row = conn.execute('''SELECT * FROM sdr_jobs WHERE attempts<? AND
                ((status='queued' AND available_at<=?) OR (status='running' AND lease_until<=?))
                ORDER BY created_at,run_id LIMIT 1''', (self.max_attempts, current, current)).fetchone()
            if row is None:
                return None
            conn.execute('''UPDATE sdr_jobs SET status='running',owner=?,lease_until=?,
                attempts=attempts+1,updated_at=? WHERE run_id=?''',
                (self.owner, current + self.lease_seconds, current, row['run_id']))
            return self._view(conn.execute('SELECT * FROM sdr_jobs WHERE run_id=?', (row['run_id'],)).fetchone())

    # Extend only this owner's current lease; a lost lease fences final metadata writes.
    def heartbeat(self, run_id: str) -> bool:
        current = time.time()
        with self._tx() as conn:
            changed = conn.execute('''UPDATE sdr_jobs SET lease_until=?,updated_at=?
                WHERE run_id=? AND status='running' AND owner=?''',
                (current + self.lease_seconds, current, run_id, self.owner)).rowcount
            return changed == 1

    # Keep long model work leased, cancelling its coroutine if another owner has taken over.
    async def _keep_lease(self, run_id: str, work: asyncio.Task):
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not self.heartbeat(run_id):
                work.cancel()
                return

    # Commit a completion only while this runner still owns the run.
    def _finish(self, job, error: bool = False, interrupted: bool = False):
        current = time.time()
        with self._tx() as conn:
            saved = conn.execute('SELECT * FROM sdr_jobs WHERE run_id=?', (job['runId'],)).fetchone()
            if not saved or saved['owner'] != self.owner or saved['status'] != 'running':
                return
            status = 'queued' if interrupted or (error and saved['attempts'] < self.max_attempts) else 'failed' if error else 'succeeded'
            delay = 0 if interrupted else min(5, saved['attempts']) if error else 0
            conn.execute('''UPDATE sdr_jobs SET status=?,owner=NULL,lease_until=NULL,
                available_at=?,updated_at=?,error_code=? WHERE run_id=? AND owner=?''',
                (status, current + delay, current, 'job_execution_failed' if error else None,
                 job['runId'], self.owner))

    # Execute one durable unit; all callback exceptions become safe bounded retry metadata.
    async def once(self) -> bool:
        job = self.claim()
        if job is None:
            return False
        work = asyncio.create_task(self.execute(job['kind'], job['leadId'], job['runId'], job['payload']))
        lease = asyncio.create_task(self._keep_lease(job['runId'], work))
        try:
            await work
        except asyncio.CancelledError:
            self._finish(job, interrupted=True)
            if asyncio.current_task().cancelling():
                raise
        except Exception:
            self._finish(job, error=True)
        else:
            self._finish(job)
        finally:
            lease.cancel()
            with suppress(asyncio.CancelledError):
                await lease
        return True

    # Poll durable state so jobs persisted before a restart are automatically resumed.
    async def _loop(self):
        while True:
            try:
                found = await self.once()
            except asyncio.CancelledError:
                raise
            except Exception:
                found = False
            if not found:
                await asyncio.sleep(self.poll_seconds)

    # Start exactly one local worker loop; multiple processes coordinate through leases.
    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())
        return self.task

    # Cancel owned work and requeue any interrupted lease for another startup to resume.
    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        with self._tx() as conn:
            current = time.time()
            conn.execute('''UPDATE sdr_jobs SET status='queued',owner=NULL,lease_until=NULL,
                available_at=?,updated_at=? WHERE status='running' AND owner=?''',
                (current, current, self.owner))

    # Cancel a pending run without making it eligible for recovery or another claim.
    def cancel(self, run_id: str) -> bool:
        with self._tx() as conn:
            return conn.execute('''UPDATE sdr_jobs SET status='cancelled',owner=NULL,lease_until=NULL,
                updated_at=? WHERE run_id=? AND status IN ('queued','running')''',
                (time.time(), run_id)).rowcount == 1


_runner: Optional[JobRunner] = None


# Configure the shared pipeline runner once during application startup.
def configure(execute: Execute, db_path=None, **options) -> JobRunner:
    global _runner
    _runner = JobRunner(execute, db_path, **options)
    return _runner


# Persist a pipeline run through the configured shared runner.
def enqueue(run_id: str, lead_id: Optional[str], kind: str, payload=None) -> dict[str, Any]:
    if _runner is None:
        raise RuntimeError('job_runner_not_configured')
    return _runner.submit(run_id, lead_id, kind, payload)
