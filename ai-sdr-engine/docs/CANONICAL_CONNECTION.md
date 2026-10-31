# Canonical product connection

The selected backend is this application on `http://localhost:8077`.

- Prospect form: `/contact`
- Sales workspace: `/workspace#/leads`
- Exact lead: `/workspace#/leads/<leadId>`
- Native workflow API: `/v1`
- Public form receipt adapter: `POST /public/enquiries`

The form and workspace use the same origin, native pipeline and `data/sdr.sqlite3`. No second assessment engine or database synchronization is involved. The public adapter delegates to `pipeline.ingest_lead` once, binds retries to the supplied details, and stores privacy acknowledgement/contact permission in `public_enquiry_receipts` in that same database. Submission returns the native lead ID, so the receipt's “View in sales workspace” link opens the precise record rather than the most recent unrelated lead.

The older application on port 8000 is configured with `SDR_CANONICAL_BACKEND=http://localhost:8077` and runs only a redirect app. `/` goes to the canonical form; `/workspace` goes to the canonical workspace. Legacy `/v1` calls return 410 rather than writing incompatible records. Its existing database is preserved in that project's own `data/` folder; records have not been silently migrated.

The supplied current Resend account is configured server-side. The selected adapter uses the narrow Resend MCP transport. All verification delivery is restricted to the configured controlled inbox; provider acceptance is distinct from inbox delivery. Unknown outcomes block the draft and require reconciliation rather than automatic retry. The configured models remain GPT-6 Luna through the API-key backend.

The connection work also fixes newer BANT evidence being ignored after confirmation, validates supplied per-field judgment confidence, blocks stale/suppressed handoff acceptance, and prevents sends with unknown contact permission. These are targeted controls, not a representative model-quality evaluation.

Remaining backend work is independent of the wiring: real enrichment must replace or clearly identify fixture profiles; interrupted background jobs and webhook deliveries need durable recovery; automated incoming-email ingestion needs a verified receiving configuration. Existing source contracts and operational records were preserved.
