-- Proposed additions to base_schema.sql for the finalized AI SDR.
-- Applies on top of the base schema (PostgreSQL 14+). Each block says which requirement it serves:
--   [L1] official Level 1 spec (challenge pack v1, section 13)
--   [BRIEF] participant brief must-haves / guardrails (AI_SDR_RFP_Agent-Challengers.pdf)
--   [PLAN] team build spec (Azentio_AI_SDR_Team_Build_Spec.pdf)
--   [API] needed by an endpoint already in openapi.json

-- ───────────── 1. Enum alignment ─────────────

-- [BRIEF] outbound replies must enter the same flow, so leads need an outbound source.
ALTER TABLE leads DROP CONSTRAINT leads_source_check;
ALTER TABLE leads ADD CONSTRAINT leads_source_check CHECK (source IN
  ('website_enquiry','marketing_campaign','linkedin_ads','google_ads','webinar','event','outbound'));

-- [PLAN][BRIEF] human review, suppression and existing-opportunity are real routes, not just "disqualified".
ALTER TABLE leads DROP CONSTRAINT leads_stage_check;
ALTER TABLE leads ADD CONSTRAINT leads_stage_check CHECK (stage IN
  ('new','enriched','scored','engaged','qualifying','qualified','handed_off','nurture','disqualified','duplicate',
   'human_review','suppressed','blocked','existing_opportunity'));

-- [L1] "highlight missing information without fabricating": ICP must be able to say it cannot decide.
ALTER TABLE leads DROP CONSTRAINT leads_icp_fit_check;
ALTER TABLE leads ADD CONSTRAINT leads_icp_fit_check CHECK (icp_fit IN ('high','medium','low','insufficient_data'));
ALTER TABLE icp_evaluations DROP CONSTRAINT icp_evaluations_fit_check;
ALTER TABLE icp_evaluations ADD CONSTRAINT icp_evaluations_fit_check CHECK (fit IN ('high','medium','low','insufficient_data'));

-- [BRIEF] a human SDR owns partial / ambiguous leads; AEs are routed by region.
ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('admin','reviewer','sales_rep','sdr'));
ALTER TABLE users ADD COLUMN region text;

-- ───────────── 2. Leads: idempotency, concurrency, routing ─────────────

-- [API][PLAN] POST /v1/leads has an idempotency-key header and LeadInput.sourceRecordId.
ALTER TABLE leads ADD COLUMN source_record_id text;
ALTER TABLE leads ADD COLUMN version int NOT NULL DEFAULT 1;               -- optimistic concurrency / stale approvals
ALTER TABLE leads ADD COLUMN route text CHECK (route IN
  ('rep_review','human_review','gap_followup','nurture','suppress','wait','existing_opportunity'));
ALTER TABLE leads ADD COLUMN outcome_reason text;
CREATE UNIQUE INDEX leads_source_record_uq ON leads (source, source_record_id) WHERE source_record_id IS NOT NULL;
-- One live lead per contact and product; duplicates point at the original.
CREATE UNIQUE INDEX leads_contact_product_uq ON leads (contact_id, product_id) WHERE duplicate_of_lead_id IS NULL;
CREATE INDEX leads_ready_idx ON leads (ready_for_followup) WHERE ready_for_followup;

ALTER TABLE ingestion_events ADD COLUMN idempotency_key text;
ALTER TABLE ingestion_events ADD COLUMN source_record_id text;
CREATE UNIQUE INDEX ingestion_idempotency_uq ON ingestion_events (idempotency_key) WHERE idempotency_key IS NOT NULL;

-- [PLAN] enquiry context per source (webinar topic, ad asset/platform, campaign) survives for drafting.
ALTER TABLE leads ADD COLUMN source_context jsonb NOT NULL DEFAULT '{}';

-- ───────────── 3. Accounts and opportunities ─────────────

-- [L1] "existing account associations"; [PLAN] protect the current account owner.
ALTER TABLE accounts ADD COLUMN owner_id int REFERENCES users(id);

-- [BRIEF] "create or update the CRM lead or opportunity"; [API] LeadInput.existingOpportunityId and
-- POST /v1/tasks/{id}/accept need somewhere to land. There is no opportunities table today.
CREATE TABLE opportunities (
  id             int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  external_ref   text UNIQUE,                    -- e.g. existingOpportunityId from a CRM import
  account_id     int NOT NULL REFERENCES accounts(id),
  product_id     int REFERENCES products(id),
  source_lead_id int REFERENCES leads(id),
  owner_id       int REFERENCES users(id),
  name           text NOT NULL,
  stage          text NOT NULL DEFAULT 'discovery' CHECK (stage IN ('discovery','qualified','proposal','won','lost')),
  status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  bant_snapshot  jsonb,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX opportunities_account_idx ON opportunities (account_id) WHERE status = 'open';

-- ───────────── 4. Imports with per-row outcomes ─────────────

-- [API] POST /v1/imports + GET /v1/imports/{import_id}; [PLAN] "rejected rows downloadable with reason".
CREATE TABLE imports (
  id            int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source        text,
  filename      text,
  status        text NOT NULL DEFAULT 'processing' CHECK (status IN ('processing','completed','failed')),
  total_rows    int NOT NULL DEFAULT 0,
  accepted_rows int NOT NULL DEFAULT 0,
  rejected_rows int NOT NULL DEFAULT 0,
  created_by    int REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE import_rows (
  id         int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  import_id  int NOT NULL REFERENCES imports(id),
  row_number int NOT NULL,
  raw        jsonb NOT NULL,
  status     text NOT NULL CHECK (status IN ('created','duplicate','rejected')),
  lead_id    int REFERENCES leads(id),
  errors     jsonb NOT NULL DEFAULT '[]',
  UNIQUE (import_id, row_number)
);
ALTER TABLE ingestion_events ADD COLUMN import_id int REFERENCES imports(id);

-- ───────────── 5. Outbound sequences ─────────────

-- [BRIEF must-have 5] cohort -> multi-step sequence -> stop on reply; [API] /v1/sequences and /tick.
CREATE TABLE sequences (
  id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        text NOT NULL,
  product_id  int REFERENCES products(id),
  theme       text NOT NULL,
  cohort      jsonb NOT NULL,                    -- definition + preview with per-contact exclusion reasons
  status      text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','paused','completed')),
  approved_by int REFERENCES users(id),
  approved_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE sequence_steps (
  id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sequence_id int NOT NULL REFERENCES sequences(id),
  step_number int NOT NULL,
  day_offset  int NOT NULL,
  subject     text NOT NULL,
  body        text NOT NULL,                     -- template with {first_name}, {company} ...
  UNIQUE (sequence_id, step_number)
);
CREATE TABLE enrollments (
  id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sequence_id int NOT NULL REFERENCES sequences(id),
  contact_id  int NOT NULL REFERENCES contacts(id),
  lead_id     int REFERENCES leads(id),
  state       text NOT NULL DEFAULT 'active' CHECK (state IN ('active','stopped_reply','stopped_suppression','completed')),
  next_step   int NOT NULL DEFAULT 1,
  next_due_at timestamptz,
  stop_reason text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
-- [BRIEF guardrail] one active sequence per contact.
CREATE UNIQUE INDEX enrollments_one_active_uq ON enrollments (contact_id) WHERE state = 'active';

-- [API] POST /v1/sequences/{id}/tick advances a demo clock instead of waiting real days.
CREATE TABLE demo_clock (
  id         int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  offset_hrs int NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ───────────── 6. Messages: threads, replies, stale approvals ─────────────

-- [API] ReplyInput has messageId + threadId; a re-sent webhook must not create a second reply.
ALTER TABLE conversations ADD COLUMN thread_id text UNIQUE;
ALTER TABLE messages ADD COLUMN external_message_id text;
CREATE UNIQUE INDEX messages_external_uq ON messages (external_message_id) WHERE external_message_id IS NOT NULL;
CREATE INDEX messages_lead_idx ON messages (lead_id);

-- [PLAN] editing text, a new reply or a policy change makes an approval stale.
ALTER TABLE messages ADD COLUMN version int NOT NULL DEFAULT 1;
ALTER TABLE messages ADD COLUMN content_hash text;
ALTER TABLE messages ADD COLUMN sequence_step_id int REFERENCES sequence_steps(id);
ALTER TABLE messages ADD COLUMN scheduled_for timestamptz;
ALTER TABLE messages DROP CONSTRAINT messages_kind_check;
ALTER TABLE messages ADD CONSTRAINT messages_kind_check CHECK (kind IN ('intro','followup','reply','sequence_step'));
ALTER TABLE messages DROP CONSTRAINT messages_status_check;
ALTER TABLE messages ADD CONSTRAINT messages_status_check CHECK (status IN
  ('draft','pending_approval','approved','rejected','sent_simulated','received','cancelled','blocked','stale'));
ALTER TABLE approvals ADD COLUMN content_hash text;
ALTER TABLE approvals ADD COLUMN message_version int;

-- ───────────── 7. Reply intent and BANT evidence ─────────────

-- [BRIEF] classify intent (interested, not now, wrong person, referral, unsubscribe) and route on it.
ALTER TABLE bant_assessments ADD COLUMN reply_intent text CHECK (reply_intent IN
  ('interested','not_now','not_interested','wrong_person','referral','unsubscribe','out_of_office','unclear'));
ALTER TABLE bant_assessments ADD COLUMN intent_confidence numeric(3,2);
ALTER TABLE bant_assessments ADD COLUMN referral jsonb;                     -- {name, email, title}
ALTER TABLE bant_assessments ADD COLUMN asks_for_pricing boolean NOT NULL DEFAULT false;
ALTER TABLE bant_assessments ADD COLUMN rule_trace jsonb NOT NULL DEFAULT '[]';  -- why the route was chosen
ALTER TABLE bant_assessments ADD COLUMN policy_version text;
ALTER TABLE bant_assessments ADD COLUMN engine text;                         -- model id or 'heuristic'
ALTER TABLE bant_assessments DROP CONSTRAINT bant_assessments_recommended_action_check;
ALTER TABLE bant_assessments ADD CONSTRAINT bant_assessments_recommended_action_check CHECK (recommended_action IN
  ('handoff_to_rep','send_followup','nurture','disqualify','human_review','suppress','wait'));

-- [PLAN] conflicting evidence and form/public-research origins must look different from buyer statements.
ALTER TABLE bant_signals DROP CONSTRAINT bant_signals_status_check;
ALTER TABLE bant_signals ADD CONSTRAINT bant_signals_status_check CHECK (status IN ('confirmed','inferred','missing','conflict'));
ALTER TABLE bant_signals DROP CONSTRAINT bant_signals_evidence_source_check;
ALTER TABLE bant_signals ADD CONSTRAINT bant_signals_evidence_source_check CHECK (evidence_source IN ('message','enrichment','form','public_research'));
ALTER TABLE bant_signals ADD COLUMN quote_verified boolean;   -- quote found verbatim in its source (anti-hallucination check)

ALTER TABLE next_actions DROP CONSTRAINT next_actions_action_type_check;
ALTER TABLE next_actions ADD CONSTRAINT next_actions_action_type_check CHECK (action_type IN
  ('review_draft','send_followup','request_info','handoff_to_rep','nurture','disqualify','rewrite_draft',
   'human_review','referral_review','account_owner_review'));

-- [PLAN] AE can accept or return; accepted handoff creates the opportunity.
ALTER TABLE handoffs DROP CONSTRAINT handoffs_status_check;
ALTER TABLE handoffs ADD CONSTRAINT handoffs_status_check CHECK (status IN ('pending','accepted','declined','returned'));
ALTER TABLE handoffs ADD COLUMN bant_assessment_id int REFERENCES bant_assessments(id);
ALTER TABLE handoffs ADD COLUMN opportunity_id int REFERENCES opportunities(id);
ALTER TABLE handoffs ADD COLUMN card jsonb;                    -- frozen handoff card: evidence, thread, next step
ALTER TABLE handoffs ADD COLUMN decision_reason text;

-- ───────────── 8. Versioned policy (ICP thresholds exist; BANT, messaging and guardrails do not) ─────────────

-- [API] GET /v1/policies and /v1/config; [PLAN] policy/knowledge versions on every assessment.
CREATE TABLE policies (
  id         int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind       text NOT NULL CHECK (kind IN ('bant','routing','guardrail','messaging','consent')),
  version    int NOT NULL,
  content    jsonb NOT NULL,       -- e.g. qualification rule, banned terms, send caps, consent regions
  active     boolean NOT NULL DEFAULT false,
  created_by int REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, version)
);
CREATE UNIQUE INDEX policies_one_active_uq ON policies (kind) WHERE active;
