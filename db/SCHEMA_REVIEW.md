# Schema and API review: AI SDR backend

Inputs reviewed: `migrations/001_base_schema.sql` (the team's PostgreSQL schema) and `openapi.json` ("Azentio AI SDR Backend" 0.1.0).
They were checked against the official Level 1 spec (challenge pack v1, section 13), the participant brief and the team build spec.

The proposed SQL is in [`migrations/002_review_additions.sql`](migrations/002_review_additions.sql). It applies cleanly on top of the base schema in PostgreSQL 16, and its unique indexes were tested.

## 1. Mismatches between the schema and the API

These need resolving before the website, engine and engagement owners code against either file.

| Topic | `base_schema.sql` | `openapi.json` | Recommendation |
|---|---|---|---|
| Backend stack | (n/a) | FastAPI-generated (`HTTPValidationError`, `Body_import_csv_…`) | Update the team plan, which says Node/Fastify, so nobody builds the wrong stack |
| IDs | `int` identity | strings (`leadId`, `messageId`, `threadId`) | Keep int PKs and send them as strings in the API; document it |
| Lead source | `website_enquiry, marketing_campaign, linkedin_ads, google_ads, webinar, event` | `website, webinar, event, ad, marketing_csv, target_account` | Use the **schema** list plus `outbound`. Level 1 names LinkedIn and Google ads separately |
| Consent | `opted_in, implied, unknown, opted_out` | `allowed, unknown, denied` | Map `allowed→opted_in`, `denied→opted_out`, or change one side |
| ICP result | `icp_fit high/medium/low` + `icp_score` | filter `icpStatus` (team-plan `matches_icp/needs_info…`) | Level 1 requires score + High/Medium/Low. Add `insufficient_data` and filter on `icpFit` |
| Lead state | `stage` (10 values) | filters `stage` and `route` | Add `route` to leads (proposed) so the filter has a column |
| Drafts | `messages` rows with a status | `/v1/drafts/{id}/…` | Fine: draft id = message id. Say so in the API description |
| Tasks | `next_actions` + `handoffs` | `/v1/tasks`, `/tasks/{id}/accept` | Define `/tasks` as a view over both. Accept on a handoff creates the opportunity |
| Opportunities | **no table** | `LeadInput.existingOpportunityId` | Add `opportunities` (proposed) |
| Imports | **no table** | `POST /v1/imports`, `GET /v1/imports/{id}` | Add `imports` and `import_rows` (proposed) |
| Sequences | **no table** | `/v1/sequences`, `/tick` | Add `sequences`, `sequence_steps`, `enrollments`, `demo_clock` (proposed) |
| Policies | ICP only (`icp_profiles`) | `/v1/policies`, `/v1/config` | Add a versioned `policies` table for BANT, routing, guardrail and messaging rules (proposed) |

## 2. Tables to add (all in `proposed_additions.sql`)

| Table | Why it is needed |
|---|---|
| `opportunities` | The brief says to create or update the CRM opportunity. Also needed for the existing-opportunity check and for AE accept |
| `imports`, `import_rows` | Per-row outcomes for CSV imports; rejected rows must be visible with reasons |
| `sequences`, `sequence_steps`, `enrollments` | Brief must-have 5 (outbound sequence, stop on reply). A partial unique index enforces **one active sequence per contact** |
| `demo_clock` | Backs `/sequences/{id}/tick` (advance hours instead of waiting days) |
| `policies` | Versioned BANT, routing, guardrail and messaging policy. Assessments record which version decided |

## 3. Columns and constraints to add to existing tables

- **leads:**
  - `source_record_id` with unique `(source, source_record_id)`, so repeated submissions don't duplicate.
  - Unique live lead per `(contact, product)`.
  - `version` for stale-approval checks.
  - `route`, `outcome_reason`, and `source_context` (webinar topic, ad asset/platform, campaign).
  - Stages `human_review`, `suppressed`, `blocked`, `existing_opportunity`.
  - `icp_fit` gains `insufficient_data`.
- **ingestion_events:** `idempotency_key` (unique) for the `idempotency-key` header, plus `source_record_id` and `import_id`.
- **accounts:** `owner_id`, so leads from an existing account go to the current owner.
- **users:** role `sdr` (who owns partial or ambiguous leads) and `region` (route AEs by region).
- **conversations / messages:**
  - `conversations.thread_id`, and unique `messages.external_message_id`, so a re-sent reply is ignored.
  - `version` and `content_hash` (approval goes stale if the text changes).
  - `sequence_step_id`, `scheduled_for`.
  - Statuses `cancelled`, `blocked`, `stale`.
- **approvals:** `content_hash` and `message_version`, so you can prove which exact text was approved.
- **bant_assessments:**
  - `reply_intent` (interested, not_now, not_interested, wrong_person, referral, unsubscribe, out_of_office, unclear) and `intent_confidence`.
  - `referral`, `asks_for_pricing`, `rule_trace`, `policy_version`, `engine`.
  - `recommended_action` gains `human_review`, `suppress`, `wait`.
- **bant_signals:**
  - Status `conflict`.
  - Evidence sources `form` and `public_research`.
  - `quote_verified`: true only if the quote appears verbatim in its source (the anti-hallucination check).
- **next_actions:** types `human_review`, `referral_review`, `account_owner_review`.
- **handoffs:** status `returned`, plus `bant_assessment_id`, `opportunity_id`, a frozen `card`, and `decision_reason`.

## 4. API changes (not schema)

1. **Add response models.** Every 200 response is currently untyped (`{}`), so the website and CRM owners cannot code against it. Add `LeadSummary`, `LeadDetail`, `ImportResult`, `Run`, `Task`, `Sequence` and `Dashboard`.
2. **Status codes.** Use 201 for a new lead and 200 for an existing one. Return 409 for `identity_conflict`, `stale_version`, `contact_blocked`, `active_sequence` and `existing_opportunity`. Right now only 200 and 422 are declared.
3. **Draft editing and rejection.** Add `PATCH /v1/drafts/{id}` (edits create a new version and invalidate approval) and `POST /v1/drafts/{id}/reject`. `ApproveInput` should carry the `version` or `contentHash` being approved.
4. **Lead list filters for the Level 1 dashboard.** Add `icpFit`, `bantStatus`, `readyForFollowup`, `productId`, `ownerId` and `q`, plus `cursor`/`limit`.
5. **Sequences.**
   - `SequenceInput` takes `leadIds`, but the brief says sequences start from a **cohort definition**. Add `POST /v1/cohorts/preview`, which returns included and excluded contacts with reasons.
   - Add `POST /v1/sequences/{id}/approve`.
   - Expose enrollment states and stop reasons.
6. **Handoffs.** Add `POST /v1/tasks/{id}/return` alongside accept.
7. **Config the Level 1 spec asks for.** "Configure product-specific ICP criteria" needs `GET`/`PUT /v1/icp-profiles/{productId}` (editing creates a new version). Add `GET /v1/products/{id}/facts` for the approved claims.
8. **Observability.** Add `GET /v1/leads/{id}/runs` and `GET /v1/runs/{id}/steps`, so the agent tables can be shown in the UI. This counts towards the architecture and guardrails scores.
9. **Suppression.** Add `GET /v1/suppressions`, so judges can see opt-outs being enforced.

## 5. Already strong; keep as is

- `product_facts` as the only citable claims, with approver. This is exactly the brief's "approved messaging only".
- `enrichment_records` with `missing_fields` and `uncertain_fields`, which is the Level 1 "highlight missing or uncertain" requirement.
- `icp_criteria` including `product_relevance`, and per-dimension `breakdown` with explanation.
- `bant_signals` with `evidence_quote`, which covers Level 1 "display supporting evidence".
- `agents`, `agent_versions`, `agent_runs`, `agent_steps` and `tool_calls`, which give a full trace of every agent decision.
- `guardrail_events`, `suppressions`, `audit_log`, and `response_time_seconds` (the brief's "faster first response" metric).

## 6. Optional, after the core flow works

- `research_sources` (url, title, fetched_at, excerpt per account), if the team does the public-research stretch goal.
- `jobs` and `outbox` (the build spec's durable worker), if the demo needs to survive restarts while sending. Otherwise run work in-process.
- `decision_comparisons`, for the "Decisions vs Jev" shadow-mode lab, if attempted.
