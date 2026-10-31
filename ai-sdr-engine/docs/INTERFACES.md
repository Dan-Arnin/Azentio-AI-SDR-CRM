# Azentio AI SDR Engine — Module Interfaces (frozen for parallel build)

Python 3.12+ / FastAPI / OpenAI Agents SDK / SQLite. Scope = spec owners **C (decision engine)** + **D (engagement)** +
the API + CRM-push layer. Website (A) and CRM UI (B) are built by teammates and talk to us over HTTP/webhooks.

## Frozen files — DO NOT EDIT (report needed changes instead)
- `app/contracts.py` — all shared Pydantic models (LeadInput, Lead, Enrichment, IcpResult, Assessment, Draft, Message, ReplyInput, Campaign, Enrollment, Task, HandoffCard, CrmEvent, Run, DecisionResult, BantField, Evidence)
- `app/db.py` — `put(kind, obj)`, `get(kind, id, Model)`, `list_(kind, Model, lead_id=None)`, `find(kind, Model, field__nested=value)`, `reset()`. Kinds: leads, assessments, drafts, messages, campaigns, enrollments, tasks, crm_events, runs, suppressions
- `app/config.py` — env settings (.env: OPENAI_API_KEY, all models gpt-6-luna, DECISION_PROVIDER=openai_decisions, CRM_WEBHOOK_URL, ...)
- `app/llm/client.py` — `structured(name, instructions, user_input, OutputModel, model=None, effort="low") -> (obj, meta)`,
  `build_agent(...)`, `run_agent(agent, input) -> (final_output, meta)`, `LLMUnavailable`. Uses the user's ChatGPT subscription
  (Codex backend) by default; stream-only, so ALWAYS go through these helpers, never `Runner.run`.

## Running code
- `export UV_CACHE_DIR=/tmp/claude-501/uvcache; uv run python ...` from repo root.
- Any command that calls the LLM must run with the Bash sandbox disabled (macOS keychain SSL lookup is blocked in sandbox).
- No test files (project rule). Verify with small throwaway scripts under `/tmp/claude-501/` only.
- Every file starts with `# path - one-line purpose`; each function has a 1-line comment above it. No commented-out code.

## Module ownership + signatures

### Agent ENGINE-1 — enrichment, ICP, identity (`app/engine/enrichment.py`, `app/engine/icp.py`, `app/engine/identity.py`, `fixtures/apollo_mock.json`, `fixtures/icp_policy.json`, `fixtures/crm_seed.json`)
```python
def enrich(inp: LeadInput) -> Enrichment
def score_icp(inp: LeadInput, enr: Enrichment) -> IcpResult
def search_cohort(cohort: CohortDefinition) -> list[LeadInput]          # source="target_account"
def check_identity(inp: LeadInput) -> IdentityResult                    # defined in identity.py
def consent_check(inp: LeadInput, enr: Enrichment | None) -> ConsentResult
```
`IdentityResult(BaseModel)`: `match: Literal["new","existing_lead","conflict"]`, `leadId: str|None`, `accountKey: str|None`,
`existingOpportunity: dict|None` ({id, ownerId, stage, productId, accountName}), `reasons: list[str]`.
`ConsentResult(BaseModel)`: `allowed: bool`, `reason: str`, `regime: str|None` (e.g. "GDPR").

### Agent QUALIFY-2 — decisions + extraction + BANT/routing (`app/llm/decisions.py`, `app/engine/extraction.py`, `app/engine/qualify.py`)
```python
async def decide(reply_text: str, spans: dict) -> DecisionResult                 # decisions.py, provider by config.DECISION_PROVIDER
async def extract_bant(reply: Message, thread: list[Message], lead: Lead) -> Extraction   # extraction.py
def initial_assessment(lead: Lead) -> Assessment                                  # qualify.py, pre-reply (form inquiry + ICP)
async def assess_reply(lead: Lead, reply: Message, thread: list[Message], prior: Assessment | None) -> Assessment
def next_question_for(missing: list[str]) -> str | None
```

### Agent ENGAGE-3 — drafts, guardrails, approvals, transport, sequences (`app/engagement/*.py`, `fixtures/knowledge.json`)
```python
async def draft_for_lead(lead: Lead, kind: DraftKind, assessment: Assessment | None = None,
                         theme: str | None = None, touch: int | None = None) -> Draft      # drafting.py
def check_guardrails(subject: str, body: str, claim_ids: list[str]) -> list[str]            # drafting.py
def edit_draft(draft: Draft, subject: str | None, body: str | None) -> Draft                # approvals.py (new version, stale approval)
def approve_draft(draft: Draft, reviewer: str, content_hash: str | None) -> Draft          # approvals.py (409-style ValueError if stale)
def reject_draft(draft: Draft, reason: str) -> Draft
def simulate_send(draft: Draft, lead: Lead) -> Message                                     # transport.py (gates: APPROVED, hash, suppression, caps)
def demo_now() -> datetime; def advance(hours: float) -> datetime                          # clock.py
async def create_campaign(inp: CampaignInput, leads: list[Lead]) -> Campaign               # sequences.py (enroll + draft 3 touches)
def due_touches() -> list[Draft]                                                           # sequences.py (APPROVED & due & enrollment ACTIVE)
def stop_on_reply(lead_id: str, reason: str = "reply") -> list[Enrollment]                 # sequences.py (STOPPED_REPLY, pending drafts -> STALE)
```

### Agent API-4 — pipeline, API, CRM push, live events (`app/pipeline.py`, `app/events.py`, `app/crm/*.py`, `app/api/*.py`, `app/main.py`)
Orchestrates everything above; the ONLY layer that changes `Lead.stage`, creates Tasks and emits CRM events.

### Agent CONSOLE-5 — live demo console + demo script + architecture note (`app/static/console.html`, `scripts/demo.sh`, `fixtures/leads.csv`, `fixtures/scripted_replies.json`, `docs/ARCHITECTURE.md`)

## HTTP API (all JSON, prefix /v1)
| Method/path | Body | Result |
|---|---|---|
| POST /v1/leads | LeadInput, header `Idempotency-Key` | 201 `{leadId, disposition: created\|existing\|quarantined\|existing_opportunity\|blocked, runId}` — async run: identity→enrich→ICP→initial assessment→draft (awaits approval) |
| POST /v1/imports | multipart `file` (CSV) + `source` | 202 `{importId, rows:[{row, status, leadId?, errors?}]}` |
| GET /v1/leads | `?stage=&source=&fit=&productId=` | `{items:[LeadSummary]}` |
| GET /v1/leads/{id} | | `{lead, assessments, drafts, messages, tasks, enrollments, runs}` |
| POST /v1/leads/{id}/replies | ReplyInput | 202 `{runId, messageId, stoppedEnrollments}` — stop sequence FIRST, then assess+route |
| POST /v1/leads/{id}/runs | `{kind: enrich\|assess\|draft}` | 202 `{runId}` |
| GET /v1/runs/{id} | | Run |
| GET /v1/drafts | `?status=DRAFT` | approval queue |
| PATCH /v1/drafts/{id} | `{subject?, body?}` | new Draft version |
| POST /v1/drafts/{id}/approve | `{reviewer, contentHash}` | Draft |
| POST /v1/drafts/{id}/reject | `{reason}` | Draft |
| POST /v1/drafts/{id}/send | | Message (simulated) |
| POST /v1/leads/{id}/linkedin-note | | Draft (`kind: linkedin_note`, ≤280 chars, research-anchored, awaits approval) |
| POST /v1/drafts/{id}/linkedin-launch | | 202 `{status: sending\|queued, text}` — APPROVED note to `LINKEDIN_TEST_PROFILE_URL` only. Connected → direct message (Message Sender worker); invite pending → queued as the Outreach workflow's 0-day follow-up (sends on acceptance); not invited → invite (Auto Connect worker, note saved as invite text). A Message is recorded only after the run log confirms delivery; the draft's `nextAction` carries the status sentence. Never errors on provider failure |
| POST /v1/drafts/{id}/send-sms | `{channel: sms\|whatsapp}` | Message on success, else 202 `{status: queued, text}` (channel not available; draft stays approved with a `nextAction` note) |
| GET /v1/integrations/phantombuster/runs/{id} | | `{status, exitCode, logTail}` — the run log is the only proof of what reached LinkedIn |
| POST /v1/campaigns | CampaignInput | Campaign + enrollments + touch drafts |
| GET /v1/campaigns/{id} | | Campaign detail |
| POST /v1/campaigns/{id}/channels | `{channel: linkedin\|whatsapp\|sms}` | 202 `{status: drafting, text}` — adds the channel and writes one approval-ready draft per active contact in the background (LinkedIn note / WhatsApp / text). `POST /v1/campaigns` also accepts `channels` (email always included) and drafts the extra channels after creation. `GET /v1/campaigns` items carry `channels` and `channelStats.{email,linkedin,whatsapp,sms}.{review,approved,sent}` |
| POST /v1/demo/clock | `{advanceHours}` | `{now, sent:[...], skipped:[...]}` |
| GET /v1/tasks, PATCH /v1/tasks/{id} | `{status}` | AE alerts / SDR tasks; accepting ae_handoff -> OPPORTUNITY |
| GET /v1/alerts | | open ae_handoff tasks with HandoffCard |
| GET /v1/dashboard | | counts by source/stage/fit/bant + ready-for-human list |
| GET /v1/events/stream | | SSE live event feed |
| GET /v1/crm/outbox | | CRM webhook deliveries |
| POST /mock-crm/webhook, GET /mock-crm/events | | built-in CRM sink for demo |
| POST /v1/demo/reset | | wipe DB |
| GET /health | | backend, models, decision provider |

## CRM webhook events (POST to CRM_WEBHOOK_URL, header `X-SDR-Signature: sha256=<hmac>`)
`lead.upserted`, `lead.enriched`, `lead.assessed`, `draft.pending_approval`, `message.sent`, `reply.received`,
`sequence.stopped`, `alert.ae_handoff`, `task.sdr_followup`, `lead.suppressed`, `lead.stage_changed`. Envelope:
`{id, type, leadId, occurredAt, payload}`.

## Policy (spec C2 — demo values)
- Products: `lending_platform` (bank/NBFC), `islamic_banking` (islamic_bank), `aml_compliance` (AMLOCK: bank/islamic_bank/NBFC). Unknown product → human review. Policy version demo-icp-v2.
- ICP geography: AE, SA, BH, QA, KW, OM, IN. Size ≥200 preferred (not hard rejection). Fit: High ≥75, Medium 45–74, Low <45.
- BANT confirmed only by explicit buyer statement with verified exact excerpt. Title alone ⇒ authority inferred.
  Company revenue / pricing question ⇒ budget NOT confirmed.
- Route precedence: suppression/contact hold → existing-opportunity owner → outside_icp close → intent route → BANT route.
  REP_REVIEW = ICP match + all 4 BANT confirmed + intent interested (conf ≥0.85). Else HUMAN_REVIEW.
  not_now → NURTURE; unsubscribe/not_interested → SUPPRESSED; wrong_person/referral/out_of_office/other → HUMAN_REVIEW.
- One gap-fill follow-up per cycle: first unresolved of Need → Timeline → Authority → Budget.
- Drafts never contain pricing, discounts, SLAs, guarantees or unapproved capability claims; every send needs human approval.
