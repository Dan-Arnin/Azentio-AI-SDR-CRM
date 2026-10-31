-- Level 2: this database becomes the durable CRM behind ai-sdr-engine.
-- The engine pushes signed webhook envelopes {id, type, leadId, occurredAt, payload}; we keep every one
-- (append-only, idempotent on the engine's event id) and fold them into a per-lead projection.

CREATE TABLE crm_events (
  id             text PRIMARY KEY,               -- engine event id (crm_…), also its Idempotency-Key
  type           text NOT NULL,                  -- lead.upserted, lead.assessed, message.sent, alert.ae_handoff, …
  engine_lead_id text,
  occurred_at    timestamptz NOT NULL,
  payload        jsonb NOT NULL,
  body_sha256    text NOT NULL,                  -- a reused id with a different body is rejected
  received_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX crm_events_lead_idx ON crm_events (engine_lead_id, occurred_at);
CREATE INDEX crm_events_type_idx ON crm_events (type);

CREATE TABLE crm_leads (
  engine_lead_id      text PRIMARY KEY,
  engine_version      int NOT NULL DEFAULT 0,    -- snapshot fields only move forward (events can arrive out of order)
  account_id          int REFERENCES accounts(id),
  full_name           text,
  email               text,
  title               text,
  company_name        text,
  country             text,
  region              text,
  product_key         text,                      -- engine product id, e.g. aml_compliance
  source              text,
  motion              text NOT NULL DEFAULT 'inbound' CHECK (motion IN ('inbound','outbound')),
  stage               text,
  fit                 text,
  icp_score           int,
  qualification_score int,
  bant                jsonb,
  route               text,
  reply_intent        text,
  sdr_owner           text,
  ae_owner_id         int REFERENCES users(id),
  routing_reason      text,
  emails_drafted      int NOT NULL DEFAULT 0,
  emails_sent         int NOT NULL DEFAULT 0,    -- every send passed a human approval in the engine
  replies             int NOT NULL DEFAULT 0,
  suppressed          boolean NOT NULL DEFAULT false,
  last_event_type     text,
  last_event_at       timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX crm_leads_motion_idx ON crm_leads (motion);

-- Opportunities created from engine handoffs: external_ref = 'engine:<leadId>', product kept as text.
ALTER TABLE opportunities ADD COLUMN product_key text;
ALTER TABLE opportunities ADD COLUMN routing_reason text;
