# Trust & governance page

`/console/trust` (redirects to `/workspace#/trust`). It shows every control with live numbers and the records behind them, so a judge can check human oversight, consent and audit in about two minutes.

## Sign-in and roles (checked on the server)

| Demo user | Role | Sees | Can do |
|---|---|---|---|
| Hana Al Suwaidi (`mgr.hana`) | Sales Manager | Whole page; audit entries for sales actions | Export the evidence pack, clear or confirm flags (reason required), add or remove manual suppression entries, pause or resume campaigns |
| Ravi Menon (`admin.ravi`) | Portal Admin | Everything, including sign-in, data-request, retention and reset events | All of the above, plus export and erase requests and the retention period |
| Nadia Karim (`sdr.omar`) | SDR | "My trust summary": flagged drafts and opt-outs on her leads, her own audit entries | Open a flagged draft to fix it |

One shared demo password from `CONSOLE_PASSWORD` (Render generates it). Sessions are signed cookies (`SESSION_SECRET`). Five wrong passwords lock that user for 15 minutes. Every governance API call returns 401 when signed out and 403 without the permission. Existing workspace routes keep working without sign-in; when someone is signed in, approvals and sends are recorded under their name instead of the string the client sends.

## Where each figure comes from

`GET /v1/governance/report?days=&campaign=` (`app/governance/report.py`) computes everything from the engine store. `GET /v1/governance/records/{kind}` returns the records behind each figure, for the drill-downs.

| Panel | Source |
|---|---|
| Human oversight (T4) | Drafts (`approvedBy`, `approvedAt`, `version`, `status`, `mode`); reply assessments (`route`) |
| Evidence (T3) | Verified buyer quotes in assessments; `Assessment.droppedQuotes` (new); confirmed signals later downgraded |
| Guardrails (T1, T2, S5) | Wording flags from each draft's `guardrailFlags`; hidden-instruction flags from replies; model-call log with **field names only** (`AiCall.fields`) |
| Consent & suppression (C1, C3, C5) | Suppression list (email, `@domain`, or `sha256:` after erasure); `send.blocked` audit events; contact permission |
| Sending & channels (C2, C4) | Messages sent today vs the 20/day and 2/contact caps; paused campaigns |
| Data rights & retention (C6, C7) | Export and erase requests; anonymised leads; retention setting and the daily run |
| Audit (S1) | `gov_audit`, append-only: SQLite triggers refuse UPDATE and DELETE, and the code has no edit or delete path |

Governance tables (`gov_audit`, `gov_flags`, `gov_requests`, `gov_settings`) are not touched by "Start a fresh workspace". Each audit event is also sent through the CRM outbox (`audit.recorded`), so the hosted backend keeps a durable copy in Neon even though the engine's disk is temporary on Render's free tier.

## New controls in the engine

- **Wording checks** (`drafting.check_guardrails`): overclaim, regulator claim, outside link and missing opt-out line now block a draft, like pricing and guarantees already did. A manager can clear a false positive with a reason; when every blocking flag is cleared, the draft returns for approval.
- **Hidden instructions in replies** (`qualify.assess_reply`): a reply that tries to instruct the system keeps the previous score, goes to a person, and is flagged with the quoted text.
- **Send gate** (`transport.check_send_gates`): the suppression list is checked live at every send (email, blocked domain, erased hash), and paused campaigns cannot send.
- **Erasure** (`governance/rights.py`): anonymises the lead, its messages, drafts, quotes, tasks and run notes; keeps the opt-out as a hash; emits `lead.erased` so the CRM backend scrubs its copy.

## Evidence pack

`GET /v1/governance/export` downloads `evidence-pack_<date>_<days>d.zip` with `controls.csv`, `audit.csv`, `panels.json` and `summary.html`. The export itself is audit-logged.

## Not built (shown as such on the page)

LinkedIn/Google Ads, WhatsApp, ad lead forms, ad audience sync and business-hours deferral. Their controls appear in the register as **Not built**, never as zero.

## Controls register

`app/governance/controls.py` is the single list used by the page, the evidence pack and Ask AI. It is **provisional** until the team's main-tab list is pasted in; status is set in code (Live only when the check passes in the demo).

## Tests

`uv run --with pytest python -m pytest -q tests` (12 checks from the spec: role access, one audit event per approve/reject/edit, append-only audit, reason required, suppression at send, injection, opt-outs, prompt log without values, evidence pack counts, erasure).
