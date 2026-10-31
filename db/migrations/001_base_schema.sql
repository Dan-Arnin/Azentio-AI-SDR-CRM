-- AI SDR CRM schema (PostgreSQL 14+; also runs on PGlite).
-- Conventions: integer identity keys, text + CHECK instead of enums, jsonb for evidence/raw payloads.

-- ───────────────────────── People ─────────────────────────
CREATE TABLE users (
  id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        text NOT NULL,
  email       text NOT NULL UNIQUE,
  role        text NOT NULL CHECK (role IN ('admin','reviewer','sales_rep')),
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ───────────────────────── CRM core ─────────────────────────
CREATE TABLE accounts (
  id               int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name             text NOT NULL,
  domain           text UNIQUE,
  industry         text,
  institution_type text,
  country          text,
  region           text,
  employee_count   int,
  website          text,
  is_customer      boolean NOT NULL DEFAULT false,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE contacts (
  id             int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id     int REFERENCES accounts(id),
  first_name     text NOT NULL,
  last_name      text NOT NULL,
  email          text NOT NULL,
  phone          text,
  job_title      text,
  seniority      text CHECK (seniority IN ('C-level','VP','Head','Director','Manager','Individual')),
  linkedin_url   text,
  consent_status text NOT NULL DEFAULT 'unknown' CHECK (consent_status IN ('opted_in','implied','unknown','opted_out')),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX contacts_email_uq ON contacts (lower(email));
CREATE INDEX contacts_account_idx ON contacts (account_id);

CREATE TABLE campaigns (
  id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name        text NOT NULL,
  source      text NOT NULL CHECK (source IN ('website_enquiry','marketing_campaign','linkedin_ads','google_ads','webinar','event')),
  channel     text,
  started_on  date NOT NULL,
  budget_usd  int,
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','paused'))
);

CREATE TABLE products (
  id        int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  slug      text NOT NULL UNIQUE,
  name      text NOT NULL,
  category  text NOT NULL,
  summary   text NOT NULL,
  keywords  text[] NOT NULL DEFAULT '{}',
  active    boolean NOT NULL DEFAULT true
);

-- Only rows in this table may be cited by the Outreach Writer.
CREATE TABLE product_facts (
  id           int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  product_id   int NOT NULL REFERENCES products(id),
  fact_type    text NOT NULL CHECK (fact_type IN ('capability','proof_point','compliance','integration')),
  statement    text NOT NULL,
  approved_by  int REFERENCES users(id),
  approved_at  timestamptz,
  source_ref   text
);
CREATE INDEX product_facts_product_idx ON product_facts (product_id);

CREATE TABLE leads (
  id                    int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  contact_id            int NOT NULL REFERENCES contacts(id),
  account_id            int REFERENCES accounts(id),
  campaign_id           int REFERENCES campaigns(id),
  product_id            int REFERENCES products(id),
  source                text NOT NULL CHECK (source IN ('website_enquiry','marketing_campaign','linkedin_ads','google_ads','webinar','event')),
  enquiry_text          text,
  stage                 text NOT NULL DEFAULT 'new' CHECK (stage IN ('new','enriched','scored','engaged','qualifying','qualified','handed_off','nurture','disqualified','duplicate')),
  icp_score             int,
  icp_fit               text CHECK (icp_fit IN ('high','medium','low')),
  bant_score            int,
  bant_status           text NOT NULL DEFAULT 'not_started' CHECK (bant_status IN ('not_started','partial','qualified','unqualified')),
  ready_for_followup    boolean NOT NULL DEFAULT false,
  owner_id              int REFERENCES users(id),
  duplicate_of_lead_id  int REFERENCES leads(id),
  first_draft_at        timestamptz,
  first_response_at     timestamptz,
  response_time_seconds int,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX leads_stage_idx ON leads (stage);
CREATE INDEX leads_source_idx ON leads (source);
CREATE INDEX leads_contact_idx ON leads (contact_id);
CREATE INDEX leads_account_idx ON leads (account_id);

CREATE TABLE lead_stage_history (
  id              int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id         int NOT NULL REFERENCES leads(id),
  from_stage      text,
  to_stage        text NOT NULL,
  changed_by_type text NOT NULL CHECK (changed_by_type IN ('agent','user','system')),
  changed_by      text,
  reason          text,
  at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX lead_stage_history_lead_idx ON lead_stage_history (lead_id);

-- ───────────────────────── Capture ─────────────────────────
CREATE TABLE ingestion_events (
  id            int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source        text NOT NULL,
  campaign_id   int REFERENCES campaigns(id),
  raw_payload   jsonb NOT NULL,
  status        text NOT NULL CHECK (status IN ('processed','duplicate','failed')),
  lead_id       int REFERENCES leads(id),
  dedupe_result jsonb,
  received_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE duplicate_matches (
  id                 int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id            int NOT NULL REFERENCES leads(id),
  matched_lead_id    int REFERENCES leads(id),
  matched_account_id int REFERENCES accounts(id),
  match_type         text NOT NULL CHECK (match_type IN ('exact_email','phone','name_domain','domain_account','company_name')),
  confidence         numeric(3,2) NOT NULL,
  resolution         text NOT NULL CHECK (resolution IN ('marked_duplicate','linked_account','needs_review')),
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX duplicate_matches_lead_idx ON duplicate_matches (lead_id);

-- ───────────────────────── Agents ─────────────────────────
CREATE TABLE agents (
  id                 int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  key                text NOT NULL UNIQUE,
  name               text NOT NULL,
  role               text NOT NULL,
  description        text NOT NULL,
  provider           text NOT NULL,
  model              text NOT NULL,
  tools              jsonb NOT NULL DEFAULT '[]',
  status             text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused')),
  current_version_id int,
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agent_versions (
  id            int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent_id      int NOT NULL REFERENCES agents(id),
  version       int NOT NULL,
  system_prompt text NOT NULL,
  config        jsonb NOT NULL DEFAULT '{}',
  change_note   text,
  created_by    int REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (agent_id, version)
);
ALTER TABLE agents ADD CONSTRAINT agents_current_version_fk FOREIGN KEY (current_version_id) REFERENCES agent_versions(id);

CREATE TABLE agent_runs (
  id               int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  agent_id         int NOT NULL REFERENCES agents(id),
  agent_version_id int REFERENCES agent_versions(id),
  lead_id          int REFERENCES leads(id),
  parent_run_id    int REFERENCES agent_runs(id),
  trigger          text NOT NULL CHECK (trigger IN ('inbound_lead','prospect_reply','manual','delegated')),
  status           text NOT NULL CHECK (status IN ('running','succeeded','failed','awaiting_human')),
  summary          text,
  input            jsonb,
  output           jsonb,
  error            text,
  tokens_in        int NOT NULL DEFAULT 0,
  tokens_out       int NOT NULL DEFAULT 0,
  cost_usd         numeric(10,5) NOT NULL DEFAULT 0,
  started_at       timestamptz NOT NULL,
  finished_at      timestamptz,
  duration_ms      int
);
CREATE INDEX agent_runs_agent_idx ON agent_runs (agent_id);
CREATE INDEX agent_runs_lead_idx ON agent_runs (lead_id);
CREATE INDEX agent_runs_parent_idx ON agent_runs (parent_run_id);

CREATE TABLE agent_steps (
  id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id      int NOT NULL REFERENCES agent_runs(id),
  step_index  int NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('plan','tool_call','llm_call','decision','delegate','handoff')),
  title       text NOT NULL,
  content     text,
  duration_ms int,
  at          timestamptz NOT NULL
);
CREATE INDEX agent_steps_run_idx ON agent_steps (run_id);

CREATE TABLE tool_calls (
  id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id      int NOT NULL REFERENCES agent_runs(id),
  step_id     int REFERENCES agent_steps(id),
  tool_name   text NOT NULL,
  input       jsonb,
  output      jsonb,
  status      text NOT NULL CHECK (status IN ('ok','error')),
  duration_ms int,
  at          timestamptz NOT NULL
);
CREATE INDEX tool_calls_run_idx ON tool_calls (run_id);

-- ───────────────────────── Enrichment ─────────────────────────
CREATE TABLE enrichment_records (
  id               int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id          int NOT NULL REFERENCES leads(id),
  provider         text NOT NULL,
  status           text NOT NULL CHECK (status IN ('complete','partial','failed')),
  data             jsonb NOT NULL,          -- { field: { value, confidence, source } }
  missing_fields   jsonb NOT NULL DEFAULT '[]',
  uncertain_fields jsonb NOT NULL DEFAULT '[]',
  raw_response     jsonb,
  agent_run_id     int REFERENCES agent_runs(id),
  enriched_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX enrichment_lead_idx ON enrichment_records (lead_id);

-- ───────────────────────── ICP ─────────────────────────
CREATE TABLE icp_profiles (
  id               int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  product_id       int NOT NULL REFERENCES products(id),
  name             text NOT NULL,
  version          int NOT NULL DEFAULT 1,
  high_threshold   int NOT NULL DEFAULT 75,
  medium_threshold int NOT NULL DEFAULT 45,
  active           boolean NOT NULL DEFAULT true,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE icp_criteria (
  id             int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  icp_profile_id int NOT NULL REFERENCES icp_profiles(id),
  dimension      text NOT NULL CHECK (dimension IN ('institution_type','geography','org_size','role','product_relevance')),
  weight         int NOT NULL,
  rule           jsonb NOT NULL,
  description    text
);

CREATE TABLE icp_evaluations (
  id             int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id        int NOT NULL REFERENCES leads(id),
  icp_profile_id int NOT NULL REFERENCES icp_profiles(id),
  score          int NOT NULL,
  fit            text NOT NULL CHECK (fit IN ('high','medium','low')),
  missing_count  int NOT NULL DEFAULT 0,
  breakdown      jsonb NOT NULL,            -- [{ dimension, weight, points, status, evidence, explanation }]
  summary        text NOT NULL,
  agent_run_id   int REFERENCES agent_runs(id),
  evaluated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX icp_eval_lead_idx ON icp_evaluations (lead_id);

-- ───────────────────────── Conversation ─────────────────────────
CREATE TABLE conversations (
  id              int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id         int NOT NULL REFERENCES leads(id),
  channel         text NOT NULL DEFAULT 'email',
  subject         text NOT NULL,
  status          text NOT NULL CHECK (status IN ('draft_pending','awaiting_reply','replied','closed')),
  started_at      timestamptz NOT NULL,
  last_message_at timestamptz NOT NULL
);
CREATE INDEX conversations_lead_idx ON conversations (lead_id);

CREATE TABLE messages (
  id                   int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  conversation_id      int NOT NULL REFERENCES conversations(id),
  lead_id              int NOT NULL REFERENCES leads(id),
  direction            text NOT NULL CHECK (direction IN ('outbound','inbound')),
  sender_type          text NOT NULL CHECK (sender_type IN ('agent','human','prospect')),
  kind                 text NOT NULL DEFAULT 'intro' CHECK (kind IN ('intro','followup','reply')),
  subject              text,
  body                 text NOT NULL,
  status               text NOT NULL CHECK (status IN ('draft','pending_approval','approved','rejected','sent_simulated','received')),
  qualifying_questions jsonb NOT NULL DEFAULT '[]',
  product_facts_used   jsonb NOT NULL DEFAULT '[]',
  generated_by_run_id  int REFERENCES agent_runs(id),
  created_at           timestamptz NOT NULL,
  sent_at              timestamptz
);
CREATE INDEX messages_conv_idx ON messages (conversation_id);
CREATE INDEX messages_status_idx ON messages (status);

CREATE TABLE approvals (
  id            int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_id    int NOT NULL REFERENCES messages(id),
  reviewer_id   int NOT NULL REFERENCES users(id),
  decision      text NOT NULL CHECK (decision IN ('approved','edited_approved','rejected')),
  original_body text NOT NULL,
  final_body    text,
  comment       text,
  decided_at    timestamptz NOT NULL
);

-- ───────────────────────── Qualification ─────────────────────────
CREATE TABLE bant_assessments (
  id                 int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id            int NOT NULL REFERENCES leads(id),
  trigger_message_id int REFERENCES messages(id),
  budget_status      text NOT NULL CHECK (budget_status IN ('confirmed','inferred','missing')),
  authority_status   text NOT NULL CHECK (authority_status IN ('confirmed','inferred','missing')),
  need_status        text NOT NULL CHECK (need_status IN ('confirmed','inferred','missing')),
  timeline_status    text NOT NULL CHECK (timeline_status IN ('confirmed','inferred','missing')),
  score              int NOT NULL,
  recommended_action text NOT NULL CHECK (recommended_action IN ('handoff_to_rep','send_followup','nurture','disqualify')),
  action_detail      text NOT NULL,
  sales_ready        boolean NOT NULL,
  summary            text NOT NULL,
  agent_run_id       int REFERENCES agent_runs(id),
  assessed_at        timestamptz NOT NULL
);
CREATE INDEX bant_lead_idx ON bant_assessments (lead_id);

CREATE TABLE bant_signals (
  id              int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  assessment_id   int NOT NULL REFERENCES bant_assessments(id),
  dimension       text NOT NULL CHECK (dimension IN ('budget','authority','need','timeline')),
  status          text NOT NULL CHECK (status IN ('confirmed','inferred','missing')),
  value           text,
  evidence_quote  text,
  evidence_source text CHECK (evidence_source IN ('message','enrichment')),
  message_id      int REFERENCES messages(id),
  confidence      numeric(3,2)
);
CREATE INDEX bant_signals_assessment_idx ON bant_signals (assessment_id);

CREATE TABLE next_actions (
  id           int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id      int NOT NULL REFERENCES leads(id),
  action_type  text NOT NULL CHECK (action_type IN ('review_draft','send_followup','request_info','handoff_to_rep','nurture','disqualify','rewrite_draft')),
  description  text NOT NULL,
  due_at       timestamptz,
  status       text NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','dismissed')),
  assigned_to  int REFERENCES users(id),
  created_by_run_id int REFERENCES agent_runs(id),
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX next_actions_lead_idx ON next_actions (lead_id);

CREATE TABLE handoffs (
  id            int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lead_id       int NOT NULL REFERENCES leads(id),
  from_agent_id int REFERENCES agents(id),
  to_user_id    int REFERENCES users(id),
  summary       text NOT NULL,
  status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','declined')),
  created_at    timestamptz NOT NULL,
  accepted_at   timestamptz
);

-- ───────────────────────── Safety ─────────────────────────
CREATE TABLE guardrail_events (
  id         int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id     int REFERENCES agent_runs(id),
  lead_id    int REFERENCES leads(id),
  message_id int REFERENCES messages(id),
  rule       text NOT NULL,
  result     text NOT NULL CHECK (result IN ('pass','warn','block')),
  details    text,
  at         timestamptz NOT NULL
);
CREATE INDEX guardrail_run_idx ON guardrail_events (run_id);

CREATE TABLE suppressions (
  id         int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email      text,
  domain     text,
  reason     text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (email IS NOT NULL OR domain IS NOT NULL)
);

CREATE TABLE audit_log (
  id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_type text NOT NULL,
  entity_id   int,
  action      text NOT NULL,
  actor_type  text NOT NULL CHECK (actor_type IN ('user','agent','system')),
  actor       text,
  details     jsonb,
  at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_entity_idx ON audit_log (entity_type, entity_id);
