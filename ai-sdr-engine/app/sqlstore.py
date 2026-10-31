# app/sqlstore.py - One storage entry point: SQLite locally, PostgreSQL (e.g. Neon) when ENGINE_DATABASE_URL is set.
#
# Modules keep their sqlite3-style code (?-placeholders, `with conn:` commits, sqlite3.Row access, BEGIN IMMEDIATE,
# INSERT OR IGNORE/REPLACE, PRAGMA table_info, rowid ordering). On PostgreSQL this adapter translates those few
# SQLite-only forms, serialises BEGIN IMMEDIATE writers with an advisory lock (SQLite's database-wide write lock),
# keeps engine tables in their own schema, pools connections and reconnects after the database suspends.
from __future__ import annotations

import os
import queue
import re
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

_DEFAULT = object()
WRITE_LOCK = 774_211  # advisory lock key standing in for SQLite's single writer


def url() -> str:
    return os.environ.get("ENGINE_DATABASE_URL", "").strip()


def is_postgres() -> bool:
    return bool(url())


def connect(path: Any = None, timeout: Optional[float] = None, isolation_level: Any = _DEFAULT, **_: Any):
    """Drop-in for sqlite3.connect(path, timeout=..., isolation_level=...)."""
    if is_postgres():
        return PgConnection(autocommit_mode=isolation_level is None, timeout=timeout or 10.0)
    from app import config

    target = Path(path or config.DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"timeout": timeout or 5.0, "check_same_thread": False}
    if isolation_level is not _DEFAULT:
        kwargs["isolation_level"] = isolation_level
    return sqlite3.connect(str(target), **kwargs)


# ---------------------------------------------------------------- SQL translation (SQLite dialect -> PostgreSQL)

def _split_quoted(sql: str) -> list[tuple[bool, str]]:
    """[(is_literal, text)] so rewrites never touch quoted strings."""
    parts, buf, i, quote = [], [], 0, None
    while i < len(sql):
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    buf.append(sql[i + 1]); i += 1
                else:
                    parts.append((True, "".join(buf))); buf, quote = [], None
        elif ch in ("'", '"'):
            if buf:
                parts.append((False, "".join(buf)))
            buf, quote = [ch], ch
        else:
            buf.append(ch)
        i += 1
    if buf:
        parts.append((bool(quote), "".join(buf)))
    return parts


def _params(code: str) -> str:
    code = code.replace("%", "%%")
    code = re.sub(r"(?<![:\w])\?", "%s", code)
    return re.sub(r"(?<![:\w]):([A-Za-z_]\w*)", r"%(\1)s", code)


def _column_types(code: str) -> str:
    code = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", code, flags=re.I)
    code = re.sub(r"\bREAL\b", "DOUBLE PRECISION", code, flags=re.I)  # PostgreSQL REAL is 32-bit: epoch seconds would lose precision
    return re.sub(r"\bBLOB\b", "BYTEA", code, flags=re.I)


@lru_cache(maxsize=2048)
def translate(sql: str) -> str:
    stripped = sql.strip().rstrip(";")
    head = stripped[:40].upper()
    insert_ignore = bool(re.match(r"INSERT\s+OR\s+IGNORE\b", stripped, re.I))
    create = head.startswith("CREATE TABLE")
    out = []
    for literal, text in _split_quoted(stripped):
        if literal:
            out.append(text.replace("%", "%%"))
            continue
        if create:
            text = _column_types(text)
        text = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", text, flags=re.I)
        out.append(_params(text))
    result = "".join(out)
    if create and not re.search(r"\browid\b", result, re.I) and result.rstrip().endswith(")"):
        result = result.rstrip()[:-1].rstrip() + ", rowid BIGSERIAL)"  # SQLite's implicit insertion order (ORDER BY rowid)
    if insert_ignore and not re.search(r"\bON\s+CONFLICT\b", result, re.I):
        result += " ON CONFLICT DO NOTHING"
    return result


# ---------------------------------------------------------------- rows and cursors shaped like sqlite3's

class Row(tuple):
    """Index and key access, like sqlite3.Row (so dict(row) works too)."""

    def __new__(cls, values: Iterable[Any], keys: tuple[str, ...]):
        row = super().__new__(cls, values)
        row._keys = keys
        return row

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            return tuple.__getitem__(self, self._keys.index(key))
        return tuple.__getitem__(self, key)

    def keys(self) -> list[str]:
        return list(self._keys)


class Cursor:
    def __init__(self, rows: list[tuple] | None = None, keys: tuple[str, ...] = (), rowcount: int = -1):
        self._rows = [Row(r, keys) for r in (rows or [])]
        self.rowcount = rowcount
        self.description = [(k,) for k in keys] if keys else None

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())


# ---------------------------------------------------------------- pool

_pool: "queue.LifoQueue[tuple[Any, float]]" = queue.LifoQueue(maxsize=8)
_ensured: set[str] = set()
_meta: dict[str, tuple[list[str], list[str]]] = {}
_lock = threading.Lock()
MAX_IDLE_SECONDS = 240  # Neon closes idle connections when compute suspends


def _schema() -> str:
    name = os.environ.get("ENGINE_DB_SCHEMA", "engine")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", name):
        raise ValueError("ENGINE_DB_SCHEMA must be a simple lowercase identifier")
    return name


def _new_raw():
    import psycopg

    conn = psycopg.connect(url(), autocommit=True, connect_timeout=15)
    schema = _schema()
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    return conn


def _checkout():
    while True:
        try:
            conn, idle_since = _pool.get_nowait()
        except queue.Empty:
            return _new_raw()
        if conn.closed or conn.broken or time.time() - idle_since > MAX_IDLE_SECONDS:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            continue
        return conn


def _checkin(conn) -> None:
    if conn.closed or conn.broken:
        return
    try:
        _pool.put_nowait((conn, time.time()))
    except queue.Full:
        conn.close()


def _as_sqlite_error(exc: Exception) -> Exception:
    import psycopg

    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    if isinstance(exc, (psycopg.errors.IntegrityError, psycopg.errors.RaiseException)):
        return sqlite3.IntegrityError(msg)
    if isinstance(exc, psycopg.errors.LockNotAvailable):
        return sqlite3.OperationalError("database is locked")
    if isinstance(exc, psycopg.OperationalError):
        return sqlite3.OperationalError(msg)
    return sqlite3.DatabaseError(msg)


class PgConnection:
    """sqlite3.Connection look-alike over a pooled psycopg connection (autocommit at the driver; transactions explicit)."""

    def __init__(self, autocommit_mode: bool, timeout: float):
        self._autocommit_mode = autocommit_mode  # sqlite3 isolation_level=None: no implicit BEGIN before writes
        self._timeout_ms = int(timeout * 1000)
        self._raw = None
        self.row_factory = None  # accepted and ignored: rows always allow index and key access
        self.isolation_level = None if autocommit_mode else ""

    # -- connection management
    def _conn(self):
        if self._raw is None or self._raw.closed:
            self._raw = _checkout()
            self._raw.execute(f"SET lock_timeout = {self._timeout_ms}")
        return self._raw

    @property
    def in_transaction(self) -> bool:
        from psycopg.pq import TransactionStatus

        return self._raw is not None and not self._raw.closed and self._raw.info.transaction_status != TransactionStatus.IDLE

    def commit(self) -> None:
        if self.in_transaction:
            self._run("COMMIT")

    def rollback(self) -> None:
        if self.in_transaction:
            try:
                self._raw.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - a broken connection has nothing to roll back
                pass

    def close(self) -> None:
        if self._raw is not None:
            self.rollback()
            _checkin(self._raw)
            self._raw = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):  # like sqlite3: commit or roll back, do not close
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    # -- execution
    def _run(self, sql: str, params: Any = None, retry: bool = True):
        import psycopg

        if params is None:
            sql = sql.replace("%%", "%")
        try:
            cur = self._conn().execute(sql, params)
        except psycopg.OperationalError as exc:
            if retry and not self.in_transaction and (self._raw is None or self._raw.closed or self._raw.broken):
                self._raw = None  # the database suspended or dropped us: reconnect once outside a transaction
                return self._run(sql, params, retry=False)
            raise _as_sqlite_error(exc) from exc
        except psycopg.Error as exc:
            raise _as_sqlite_error(exc) from exc
        keys = tuple(d.name for d in cur.description) if cur.description else ()
        return Cursor(cur.fetchall() if keys else None, keys, cur.rowcount)

    def execute(self, sql: str, params: Any = None) -> Cursor:
        text = sql.strip()
        upper = text[:30].upper()
        if upper.startswith("PRAGMA"):
            return self._pragma(text)
        if re.match(r"BEGIN\b", upper):
            if not self.in_transaction:
                self._run("BEGIN")
            if re.match(r"BEGIN\s+(IMMEDIATE|EXCLUSIVE)", upper):
                self._run("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            return Cursor()
        if re.match(r"CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\s+IF\s+NOT\s+EXISTS", upper):
            return self._ensure(text)
        if not self._autocommit_mode and not self.in_transaction and re.match(r"(INSERT|UPDATE|DELETE|REPLACE)\b", upper):
            self._run("BEGIN")  # sqlite3's legacy implicit transaction before a write
        if isinstance(params, (list, tuple)):
            params = tuple(int(p) if isinstance(p, bool) else p for p in params)
        if re.match(r"INSERT\s+OR\s+REPLACE\b", upper):
            return self._run(self._replace(text), params)
        return self._run(translate(text), params)

    def executemany(self, sql: str, seq: Iterable[Any]) -> Cursor:
        for params in seq:
            self.execute(sql, params)
        return Cursor()

    def executescript(self, script: str) -> Cursor:
        for statement in _statements(script):
            self.execute(statement)
        return Cursor()

    # -- helpers for SQLite-only forms
    def _ensure(self, text: str) -> Cursor:
        key = re.sub(r"\s+", " ", text)[:200]
        if key in _ensured:
            return Cursor()
        nested = self.in_transaction
        if nested:
            self._run("SAVEPOINT sqlstore_ddl")
        try:
            self._run(translate(text))
            if nested:
                self._run("RELEASE SAVEPOINT sqlstore_ddl")
        except sqlite3.DatabaseError as exc:
            # concurrent CREATE ... IF NOT EXISTS can race in PostgreSQL; the object exists either way
            if "already exists" not in str(exc) and "duplicate key" not in str(exc):
                raise
            if nested:
                self._run("ROLLBACK TO SAVEPOINT sqlstore_ddl")
        with _lock:
            _ensured.add(key)
        _meta.clear()
        return Cursor()

    def _columns(self, table: str) -> tuple[list[str], list[str]]:
        if table not in _meta:
            rows = self._run("""SELECT a.attname, COALESCE(i.indisprimary, false)
                                FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                                LEFT JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary AND a.attnum = ANY(i.indkey)
                                WHERE c.relname = %s AND n.nspname = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
                             (table, _schema())).fetchall()
            _meta[table] = ([r[0] for r in rows if r[0] != "rowid"], [r[0] for r in rows if r[1]])
        return _meta[table]

    def _replace(self, text: str) -> str:
        m = re.match(r"INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*(\(([^)]*)\))?\s*VALUES", text, re.I)
        if not m:
            raise sqlite3.OperationalError("unsupported INSERT OR REPLACE form")
        table = m.group(1)
        cols, pk = self._columns(table)
        target = [c.strip() for c in m.group(3).split(",")] if m.group(3) else cols
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in target if c not in pk) or f"{pk[0]} = EXCLUDED.{pk[0]}"
        body = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO\s+\w+\s*(\([^)]*\))?", f"INSERT INTO {table} ({', '.join(target)})", text, flags=re.I)
        return translate(body) + f" ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {updates}"

    def _pragma(self, text: str) -> Cursor:
        m = re.match(r"PRAGMA\s+table_info\((\w+)\)", text, re.I)
        if not m:
            return Cursor()  # busy_timeout, journal_mode, ...: not applicable
        cols, pk = self._columns(m.group(1))
        return Cursor([(i, c, "", 0, None, int(c in pk)) for i, c in enumerate(cols)], ("cid", "name", "type", "notnull", "dflt_value", "pk"))


def _statements(script: str) -> list[str]:
    """Split a script on semicolons outside quotes and BEGIN ... END trigger bodies."""
    out, buf, depth = [], [], 0
    for literal, text in _split_quoted(script):
        if literal:
            buf.append(text)
            continue
        for token in re.split(r"(;|\bBEGIN\b|\bEND\b)", text, flags=re.I):
            up = token.upper()
            if up == "BEGIN":
                depth += 1
            elif up == "END" and depth:
                depth -= 1
            if token == ";" and depth == 0:
                stmt = "".join(buf).strip()
                if stmt:
                    out.append(stmt)
                buf = []
            else:
                buf.append(token)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def reset_metadata() -> None:
    """Forget cached table metadata (tests that drop the schema)."""
    _ensured.clear()
    _meta.clear()
