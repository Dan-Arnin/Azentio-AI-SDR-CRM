# Architecture note: AI SDR (Level 1)

## Components

```
 Inbound sources                      Outbound
 (web form, event, ad, webinar CSV)   (cohort definition + theme)
          │                                   │
          ▼                                   ▼
   pipeline.capture ── validate ──┐    outbound.preview ── exclusions with reasons
          │                       │           │
          ▼                       │           ▼
   enrichment (Mock Apollo) ──────┤    emails.draft_steps (Claude, approved claims only)
   crm_check (dupes, open opps)   │           │  guardrail check → template fallback
   icp.score (per product)        │           ▼
   hygiene.check (suppression,    │    human approves sequence → enrollments
     consent, 1 active sequence)  │           │
          │                       │           ▼
          ▼                       │    outbound.advance (cadence tick)
   emails.qualifying_email        │           │
          │                       ▼           │
          └──────► queue_email ── approval mode (first N sends) ── send (simulated, logged)
                                                  │
                         prospect reply ──────────┘  (any reply stops the sequence)
                                │
                                ▼
                  analyzer.analyse  ── Claude structured output ─┐
                                │     or deterministic engine   │ evidence quotes verified
                                ▼                               │ against reply text
                  merge_bant (whole thread) ◄───────────────────┘
                                │
                                ▼
                  decide (deterministic rule + trace)
        ┌───────────┬──────────────┬─────────────┬──────────┬────────┐
        ▼           ▼              ▼             ▼          ▼        ▼
   qualified   gap follow-up   SDR task      suppress    nurture   wait (OOO)
   AE alert +  (max 1, back    (partial,     (opt-out,
   CRM opp     to approval)    referral,     not
                               low conf.)    interested)
```

## Data flow and state

- **SQLite** (`app/db.py`) stores leads, messages, analyses, alerts, sequences, enrollments, suppressions, CRM opportunities and an `events` audit log. Every state transition writes an event, and the lead drawer shows the full trail.
- **Lead states:** `captured → enriched → icp_checked → pending_approval → awaiting_reply → replied → qualified | sdr_followup | suppressed | nurture | blocked | existing_opportunity`.
- **Config** (`app/data/config.json`) holds per-product ICPs, regions, consent rules, approved messaging, banned terms, owners, guardrail limits and cohorts. Sales ops can tune it without code changes.

## Models and tools

- **Claude `claude-opus-5-5`** through the Anthropic Python SDK, with structured outputs (`output_config.format` JSON schema), `effort: low`, and server-side refusal fallback. It is used for:
  1. **Reply analysis**: intent, confidence, BANT signals with verbatim evidence, referral contact and pricing-ask flag.
  2. **Outbound copy drafting**: three steps, constrained to the approved claims.
- **Deterministic engine** (`analyzer._heuristic`): the same output shape, used when no credentials are present, when `SDR_ENGINE=heuristic`, or when a Claude call fails. The pipeline never blocks on the model.
- **FastAPI** JSON API plus a static vanilla-JS dashboard. There is no build step.

## Where AI is trusted, and where it is not

| Decision | Made by | Why |
|---|---|---|
| Intent and BANT extraction | Claude (or patterns) | Reading free-text replies is language work |
| Whether the evidence is real | Code | Each quote must appear in the reply or the title, otherwise it is downgraded to *missing* |
| Qualified / partial / suppress | Code (`decide`) | Must be explainable and repeatable; a trace is shown |
| ICP fit | Code | Rules that sales ops own |
| Email copy | Templates; Claude for outbound drafts | Drafts pass a banned-terms check, and humans approve first sends |
| Sending, opportunity creation, follow-up to a referral | Humans approve, or a human owns the task | Human stays in control |

## Moving to production

- Replace `MockApollo` with an Apollo people/organization match client. Replace `crm_check` and `crm_opportunities` with HubSpot or Salesforce.
- Replace the simulated `send` with SMTP or Gmail API on a team-controlled domain, and ingest replies by IMAP or webhook into `receive_reply`.
- Move the cadence tick to a scheduler (cron or queue) that respects send windows and the daily cap.
- Add an evaluation set of labelled replies to track BANT extraction accuracy for each engine.
