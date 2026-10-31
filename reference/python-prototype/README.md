# AI SDR — Level 1 prototype (Azentio GTM hackathon, Challenge A)

An AI SDR that captures inbound leads and runs outbound sequences. Both motions feed one qualification flow: read the reply, score it on BANT with quoted evidence, and route the result. Qualified leads go to the AE, partially qualified ones to a human SDR, and not-interested contacts to suppression.

All data is synthetic, on `.example` domains. Emails are **simulated and logged**, never sent to real addresses. Product messaging is labelled sample content.

## Run

```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --port 8765          # open http://localhost:8765
```

| Env var | Default | Effect |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | If set, replies are analysed and sequences drafted by Claude (`claude-opus-5-5`) |
| `SDR_ENGINE` | `auto` | `heuristic` forces the deterministic engine; `llm` forces Claude |
| `SDR_MODEL` | `claude-opus-5-5` | Model id |
| `SDR_DB` | `./sdr.db` | SQLite file |

Without credentials the app runs fully on the deterministic engine, which is useful as a fallback during a live demo. The header shows which engine is active.

Tests (both demo scenarios plus guardrails):

```bash
SDR_ENGINE=heuristic python3 -m unittest discover -s tests -v
```

## Official Level 1 spec (challenge pack v1, section 13) → where it is

| Required capability | Where |
|---|---|
| Lead capture from website, marketing campaigns, LinkedIn/Google ads, webinars, events; contact, company, source, product interest, enquiry context | `Inbound` tab: single-lead form, webinar CSV, bulk CSV/JSON import (`POST /api/leads/import`) |
| Duplicate leads and existing account associations | `crm_check`: CRM contact, **account** (customer/prospect + owner), open opportunity, duplicate lead |
| Enrichment: industry, geography, institution type, employee size, designation, seniority; highlight missing/uncertain | Mock Apollo; the lead view lists every missing field ("not guessed") and uncertain value (self-reported, guessed seniority, personal email) |
| Product-specific ICP on geography, institution type, size, role, product relevance; score with explanation; High/Medium/Low Fit | `app/icp.py` + `config.json` (5 weighted criteria, each with a reason) |
| AI-generated personalised intro email by source and product, with qualifying questions; human review/approval | Claude writes subject and opener, code adds the approved claim, questions and footer; **every draft needs approval** (`approval_mode: always`) |
| BANT from replies: Confirmed / Inferred / Missing, evidence, qualification score, recommended next action | `analyzer.py` + `pipeline.decide`; the lead view shows quotes, the 0–100 score, decision trace and next action |
| Dashboard: by source, ICP score, BANT status, stage; lead detail; leads ready for human follow-up | `Pipeline` tab (filters for source, stage, ICP fit, "ready for human follow-up"; BANT letters per lead) |

## Brief must-haves (participant brief) → where it is

| # | Must-have | Where |
|---|---|---|
| 1 | Two or more inbound sources, with the source recorded | Website form, event, ad form-fill, webinar CSV (`Inbound` tab) |
| 2 | Enrich and decide ICP fit with reasoning | Mock Apollo (`app/enrichment.py`), per-product ICP scorecard (`app/icp.py`) |
| 3 | Qualifying email → read reply → extract BANT | `emails.qualifying_email`, `analyzer.analyse` (each signal carries a verbatim quote) |
| 4 | Route: qualified → AE, partial → SDR, not interested → suppression | `pipeline.decide` / `pipeline.act` |
| 5 | Cohort → personalised multi-step sequence → stop on reply | `Outbound` tab, `app/outbound.py` |
| 6 | Outbound replies use the same qualification flow | `pipeline.receive_reply` serves both motions |
| 7 | Rep alert with an opportunity summary | `Rep alerts` tab: lead, account, BANT evidence, thread, next step, CRM opportunity id |

Nice-to-haves also included: distinct event, webinar and ad workflows; duplicate and existing-opportunity detection; intent classes beyond BANT (referral, out-of-office, unsubscribe, not now); a pipeline dashboard.

## Qualification rule (stated for the judges)

**Qualified** = Need confirmed **and** Timeline confirmed and within 12 months **and** (Budget **or** Authority) confirmed.

- "Confirmed" means the prospect said it explicitly (or, for Authority, the enrichment title supports it). "Inferred" signals count towards the 0–100 BANT score but never qualify a lead on their own.
- Signals accumulate across the thread. A gap answered in a later reply completes the picture.
- Partially qualified leads get at most **one** gap-filling follow-up, then go to a human SDR.
- If intent confidence is below 0.6, or the reply is unclear, the lead goes to a human (escalate by default).

## Demo script (about 5 minutes)

Click **Reset demo data** first.

**Run 1: website enquiry → AE alert**
1. `Inbound` → **Sample: website enquiry** → **Capture lead**. Sara is enriched: Najd Capital Bank, Islamic bank, KSA, scoring 100/100 (High Fit) for Kastle.
2. **Open lead.** The qualifying email is **pending approval**; every draft is reviewed. Edit it if you like, then **Approve & send**.
3. Under *Simulate a prospect reply*, pick **qualified** → **Receive reply & analyse**. All four BANT signals are confirmed, each with its quote, and the decision trace shows why it qualified.
4. `Rep alerts`: Aisha Rahman (AE, GCC) receives the summary, BANT evidence, thread and next step. `CRM & guardrails` shows the new opportunity.

**Run 2: outbound sequence reply → same flow**
1. `Outbound` → cohort *Heads of Compliance across banks in the GCC* → **Build cohort & draft sequence**. 2 contacts are included and 5 excluded, each with a reason: open opportunity (including the one Run 1 just created), exchange house, insurer, EU without consent.
2. **Approve & launch.** Each send lands in `Approvals`; approve them.
3. Open Hessa's lead and send the scripted **partial** reply. Need is confirmed, timeline is missing, so the agent stops the sequence and queues **one** gap follow-up. Approve it.
4. Send the scripted **gap answer**. Need from the first reply plus timeline and authority from the second make the lead qualified, and the AE is alerted.

**Guardrail moments worth showing:**
- Scripted **pricing ask**: the follow-up says pricing is for the AE and the agent never quotes.
- **unsubscribe**: the contact is added to the suppression list.
- **referral**: an SDR task with the referred contact.
- **ambiguous**: escalated to a human.
- Webinar sample CSV: the bad row is rejected with a reason, the EU contact is blocked (GDPR), the Low Fit contact goes to nurture, and the existing customer contact goes to their AE.
- Bulk import sample JSON: a personal-email lead with no company data gets "Insufficient data" and goes to a human. Every missing field is listed and none is guessed.

## Not in Level 1 (next levels)

- Real Apollo and CRM connectors, which plug in behind `MockApollo` and `crm_check`.
- A real mailbox: SMTP to a team-controlled test inbox, plus IMAP polling for replies.
- Real cadence scheduling. A cadence tick is manual (**Advance cadence**).
- Authentication and roles, and multi-rep territories beyond region.
