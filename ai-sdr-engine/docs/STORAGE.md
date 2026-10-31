# Storage: SQLite locally, PostgreSQL (Render Postgres) when hosted

All engine storage goes through `app/sqlstore.py`. Modules still write sqlite3-style code; only their `connect` call changed.

| Setting | Behaviour |
|---|---|
| `ENGINE_DATABASE_URL` unset | SQLite file at `DB_PATH` (default `data/sdr.sqlite3`), exactly as before |
| `ENGINE_DATABASE_URL=postgresql://…` | PostgreSQL, tables in the schema `ENGINE_DB_SCHEMA` (default `engine`), so they never collide with the CRM backend's tables in the same database |

On PostgreSQL the adapter:

- translates the SQLite-only forms the engine uses: `?` and `:name` placeholders, `INSERT OR IGNORE`, `INSERT OR REPLACE`, `PRAGMA table_info`, `rowid` ordering (an extra `rowid BIGSERIAL` column), `REAL` → `DOUBLE PRECISION` (PostgreSQL `REAL` is 32-bit and would round epoch timestamps), `executescript`;
- keeps SQLite's transaction behaviour: an implicit `BEGIN` before writes, `BEGIN IMMEDIATE` serialised with an advisory lock (SQLite's single writer), `with conn:` commits or rolls back;
- returns rows with index and key access (like `sqlite3.Row`) and raises `sqlite3` exception types, so calling code is unchanged;
- pools connections and reconnects once when the database has dropped an idle connection (restart, maintenance or suspend).

The audit log's append-only guard has a PostgreSQL trigger equivalent (`governance/store.py`).

Tests run on both: `uv run --with pytest python -m pytest -q tests` (SQLite) and the same with `ENGINE_DATABASE_URL` set (PostgreSQL, throwaway schema).
