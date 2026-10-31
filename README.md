# Azentio AI SDR CRM

Hackathon build for **Challenge A: AI SDR**: capture inbound leads, enrich them, score them against a product ICP, draft a qualifying email for a human to approve, read the reply, score BANT with quoted evidence, and hand a qualified opportunity to a rep.

All data is synthetic (`.example` domains). Every send is **simulated**. No real prospect is ever emailed.

## Repository layout and owners

| Path | What | Owner |
|---|---|---|
| `frontend/` | Public website and lead form, CSV import UI (not scaffolded yet) | **A** (website) |
| `backend/app/main.py`, `routers/`, `intake.py`, `pipeline.py`, `repo.py`, `contracts.py`, `db.py` | API, validation, deduplication, persistence, approval/send gates, tasks | **B** (CRM + backend) |
| `db/migrations/`, `db/seed/` | PostgreSQL schema (team schema + review additions) and synthetic seed | **B** |
| `backend/app/engine/` | Enrichment, ICP scoring, reply intent + BANT, routing policy | **C** (decision engine) |
| `backend/app/engagement/`, `backend/app/sequences.py` | Email drafts, follow-ups, outbound sequences | **D** (engagement) |
| `fixtures/` | Mock Apollo data, sample leads CSV, scripted replies with expected routes | everyone |
| `ai-sdr-engine/` | Decision + engagement engine with its own contact form and sales workspace (SQLite, OpenAI, Resend). See its `docs/` | engine team (PR 1) |
| `render.yaml` | Free-tier deployment for the backend and the engine (see [Deploy](#deploy-free-tier-render--neon)) | **B** |
| `reference/python-prototype/` | Earlier working prototype (FastAPI + SQLite + Claude) with its demo UI: shows the intended behaviour | read-only |

C and D plug in behind fixed interfaces, so they can replace the fixture logic without touching routes:
- `engine.Engine`: `enrich`, `score_icp`, `assess_reply`. See the docstring in [backend/app/engine/__init__.py](backend/app/engine/__init__.py) for the exact shapes.
- `engagement.compose()`

The backend runs end to end today on the **fixture** implementations: mock Apollo, rule-based ICP, and a deterministic BANT heuristic. No API keys are needed.

## Quick start

```bash
cp .env.example .env
docker compose up -d --build        # postgres + api on http://localhost:8000 (migrations run on start)
open http://localhost:8000/docs     # interactive API docs
```

Without Docker for the API (Python 3.11+):

```bash
docker compose up -d postgres
cd backend && pip install -r requirements.txt
python -m app.db                    # apply migrations + seed (add --reset to wipe)
uvicorn app.main:app --reload
```

Tests are end-to-end against a **disposable** Postgres, because they drop and recreate the schema:

```bash
docker run -d --rm --name sdr-test-pg -e POSTGRES_USER=sdr -e POSTGRES_PASSWORD=sdr -e POSTGRES_DB=sdr_test -p 5433:5432 postgres:16
cd backend && TEST_DATABASE_URL=postgresql://sdr:sdr@localhost:5433/sdr_test python -m pytest -q
```

## Deploy (free tier: Render + Neon)

[`render.yaml`](render.yaml) defines two free Render web services. Both redeploy on every push to the branch they were created from (normally `main`).

| Service | What | Needs |
|---|---|---|
| `azentio-sdr-backend` | `backend/` in Docker. Applies migrations and seed on each start (safe to repeat) | `DATABASE_URL` from Neon |
| `azentio-sdr-engine` | `ai-sdr-engine/`: contact form `/contact`, workspace `/workspace`, API `/v1` | `OPENAI_API_KEY` (optional: fixture mode without it) |

1. **Neon:** create a project in **AWS Asia Pacific (Singapore)**, the same region as the Render services → Connection details → turn **off** "Connection pooling" → copy the `postgresql://…?sslmode=require` string.
2. **Render:** Dashboard → New → Blueprint → select this repo and branch. Paste `DATABASE_URL` (and `OPENAI_API_KEY` if used) when asked. Secrets are entered only in Render, never committed.
3. **Check:** `https://<backend>.onrender.com/ready` lists the applied migrations; `https://<engine>.onrender.com/health` returns `ok`.
4. **Frontend (later):** deploy `frontend/` on Vercel, then add its URL to `CORS_ORIGIN` on the backend service.

Free-tier behaviour to plan around:
- Services sleep after 15 minutes idle; the first request takes 30–60 s. Open both `/health` URLs a few minutes before presenting.
- The engine stores data in SQLite on Render's temporary disk, so its records reset on every redeploy or restart. Re-run the demo flow after deploying. The backend's data lives in Neon and persists.
- Leave `EMAIL_TRANSPORT=simulated` unless sends are restricted to the controlled `DEMO_INBOX`.

Test the backend image locally exactly as Render runs it: `docker build -f backend/Dockerfile .` (build from the repo root).

## Working together (Git)

- `main` must always run. Work on a branch per workstream: `feat/website`, `feat/crm-backend`, `feat/engine`, `feat/engagement`.
- Pull before you start: `git pull --rebase origin main`.
- Open a pull request into `main`. Owner **B** (integration lead) merges after the tests pass.
- Changing the API or schema? Add a **new** migration file (`db/migrations/003_…sql`); never edit an applied one. Regenerate the contract with `python -m app.export_openapi` (in `backend/`) and commit `backend/openapi.json`.
- Never commit `.env`, API keys or real customer data.

## API (contract: [`backend/openapi.json`](backend/openapi.json))

Every operation from the team's original `openapi.json` (kept as `db/openapi.team-original.json`) exists with the same paths and request fields. Additions:
- `POST /v1/drafts/{id}/reject` and `GET /v1/suppressions`.
- Optional `body` / `expectedVersion` on approve.
- Lead list filters: `icpFit`, `bantStatus`, `readyForFollowup`, `productId`, `q`, `cursor`, `limit`.
- Typed responses.

Errors are `404 not_found`, `409 {code}` and `422 validation_failed`. The 409 codes are `identity_conflict`, `stale_version`, `contact_blocked`, `guardrail_block`, `not_approved`, `send_cap` and `reply_received`. New leads return `201`; an existing lead returns `200`.

**Demo flow:**
1. `POST /v1/leads?autoRun=true` → enrich → ICP → intro draft pending approval.
2. `POST /v1/drafts/{id}/approve`.
3. `POST /v1/drafts/{id}/send`.
4. `POST /v1/replies` → BANT + route.
5. `GET /v1/tasks`.
6. `POST /v1/tasks/handoff-{id}/accept`, which creates the opportunity.

## Contract decisions (owner B)

| Topic | Decision |
|---|---|
| IDs | Database integers, sent as strings |
| `source` | Accepts the openapi values and the schema values; stored as schema values. `website→website_enquiry`, `marketing_csv→marketing_campaign`, `ad→linkedin_ads`, `target_account→outbound`. Send `linkedin_ads` / `google_ads` directly when you know the platform |
| `contactPermission` | `allowed→opted_in`, `unknown→unknown`, `denied→opted_out` |
| Duplicates | Same `idempotency-key` or same `(source, sourceRecordId)` → original lead (200). Same contact + product → attached to the existing lead (`disposition: duplicate`). Phone owned by another email → `409 identity_conflict`, kept as a quarantined ingestion event |
| Accounts | Matched by company domain; never created from a personal mailbox (gmail etc.) |
| Approval | Every outbound email (intro, follow-up, sequence touch) needs approval. Send re-checks the approved content hash, suppression, consent, the daily cap, and whether the prospect already replied |
| Qualification rule | Ready for the rep when Need is confirmed, Timeline is confirmed within 12 months, and Budget or Authority is confirmed. One gap follow-up asks a **single** question (Need → Timeline → Authority → Budget). The rule is versioned in the `policies` table |
| Reply safety | Any reply stops the sequence before analysis. Instruction-like text (prompt injection) is logged as a guardrail event and routed to a human |

Schema review and rationale for the additions: [db/SCHEMA_REVIEW.md](db/SCHEMA_REVIEW.md).

## Level 1 coverage (challenge pack v1, section 13)

| Requirement | Where |
|---|---|
| Lead capture from website, campaigns, LinkedIn/Google ads, webinars, events; duplicates; account association | `intake.py`, `POST /v1/leads`, `/v1/imports`, `duplicate_matches` |
| Enrichment with missing and uncertain fields, never fabricated | `engine/fixture.py` → `enrichment_records.missing_fields` / `uncertain_fields` |
| Product-specific ICP, score, explanation, High/Medium/Low | `icp_profiles` + `icp_criteria` (incl. product relevance) → `icp_evaluations.breakdown` |
| Personalised intro with qualifying questions; human approval | `engagement.compose`, `/v1/drafts/{id}/approve` |
| BANT Confirmed/Inferred/Missing, evidence, score, next action | `bant_assessments` + `bant_signals` (`quote_verified`), `next_actions` |
| Dashboard by source, ICP, BANT status, stage; ready for follow-up | `GET /v1/dashboard`, `GET /v1/leads?readyForFollowup=true` |

## Next steps by owner

- **A:** build the website form and CSV import UI against `backend/openapi.json`. Generate the `idempotency-key` once per submit.
- **B:** CRM screens (lead table/detail, approvals, tasks); auth for operator routes if hosted.
- **C:** a model-backed `Engine` (keep the shapes, and keep quotes verbatim so `quote_verified` holds). Tune the policy in the `policies` table.
- **D:** model-written drafts behind `engagement.compose` (approved `product_facts` only). Cohort builder → `POST /v1/sequences`.
