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
