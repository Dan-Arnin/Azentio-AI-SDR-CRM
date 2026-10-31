# Seed data (demo workspace)

Snapshot of the demo workspace: 42 leads, 4 campaigns, drafts, replies, agent runs and CRM events.
All prospects are fictional; personal inboxes, phone numbers and private domains were replaced with
`example.com` placeholders. The Apollo cache/canary hold public company data only.

Use it locally (SQLite mode):

```bash
cd ai-sdr-engine
mkdir -p data
cp seed-data/sdr-demo.sqlite3 data/sdr.sqlite3
cp seed-data/apollo-cache.sqlite3 seed-data/clock.json data/
```

The app writes to `data/` (git-ignored), so the seed files stay unchanged.

Hosted (PostgreSQL, e.g. Render): set `ENGINE_SEED_FILE=seed-data/sdr-demo.sqlite3` (already in `render.yaml`).
At startup the engine copies every table of this snapshot into its database **once**, and only when the workspace has
no leads yet, so it never mixes with or overwrites real data (`app/seed_import.py`; a `seed_imports` row records it).
Then run `POST /v1/crm/resync` on the backend to copy the snapshot's CRM events into the CRM.
The demo clock (`clock.json`) is restored with the import but lives on the host's disk, so it resets on a restart.
