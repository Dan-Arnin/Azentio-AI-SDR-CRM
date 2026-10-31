"""PostgreSQL access (psycopg 3). One connection per request; the context manager commits on success
and rolls back on any exception, so a failed request never leaves half-written rows."""
import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://sdr:sdr@localhost:5432/sdr")
ROOT = Path(__file__).resolve().parents[2]

__all__ = ["Jsonb", "connect", "get_conn", "migrate", "reset"]


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or os.environ.get("DATABASE_URL", DATABASE_URL), row_factory=dict_row)


def get_conn() -> Iterator[psycopg.Connection]:
    """FastAPI dependency."""
    with connect() as conn:
        yield conn


def _sql_files(folder: str) -> list[Path]:
    return sorted((ROOT / "db" / folder).glob("*.sql"))


def migrate(conn: psycopg.Connection, seed: bool = True) -> list[str]:
    """Apply db/migrations/*.sql (and db/seed/*.sql) once each, in filename order."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
    done = {r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
    applied = []
    for path in _sql_files("migrations") + (_sql_files("seed") if seed else []):
        key = f"{path.parent.name}/{path.name}"
        if key in done:
            continue
        conn.execute(path.read_text())
        conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", [key])
        applied.append(key)
    conn.commit()
    return applied


def reset(conn: psycopg.Connection) -> None:
    """Drop everything. Demo/test use only."""
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()


if __name__ == "__main__":
    import sys

    with connect() as c:
        if "--reset" in sys.argv:
            reset(c)
        print("applied:", migrate(c, seed="--no-seed" not in sys.argv) or "nothing new")
