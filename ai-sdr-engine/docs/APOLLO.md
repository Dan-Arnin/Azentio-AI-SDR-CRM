# Apollo enrichment

The supplied Apollo credential is stored only in the ignored server `.env` file, with owner-only permissions. No key is returned to the browser.

With `APOLLO_API_KEY` configured, `ENRICHMENT_PROVIDER` defaults to `apollo`. New company reviews use the read-only organization enrichment endpoint before any public-research fallback. Fictional `.example` domains never consume lookup credits or become live evidence. Existing records retain their original provenance until explicitly reviewed again.

When `DEMO_MODE=true`, exact company-domain matches in the declared fictional hackathon fixture use their labelled demo profiles before any live lookup. This also covers fictional prospects whose domains look real. With demo mode off, these profiles are not used. An Apollo miss is ordinary missing data, not an API error: public research may add evidence, otherwise supplied information is retained and missing fields remain visible. Authentication, permission and rate-limit failures remain errors.

`GET /v1/enrichment/status` reports the selected provider, whether a key is configured, whether optional people matching is enabled, and the cache duration. It does not return a credential or claim that a configured key has passed authentication.

Responses are mapped into company name, industry, country, employee count and other available company facts. Missing or zero/unknown headcount stays unknown. Institution type is inferred conservatively from returned industry/description and remains an inference. Apollo data never confirms buyer budget, authority, project need or timeline. Form conflicts are retained for human review.

Successful responses are cached server-side for six hours, with private cache-file permissions. A miss or lookup failure can fall back to separately labelled public research; API error codes remain visible. A live lookup never silently substitutes a synthetic profile.

Optional `APOLLO_PEOPLE_ENRICHMENT=true` enables exact, high-confidence work-email matching. It is disabled by default. Personal email/phone reveal and waterfall enrichment are disabled. Rejected/uncertain matches do not replace the buyer's identity.

The UI identifies live Apollo records, public research and legacy synthetic profiles separately. The source cleaner no longer removes `mock`, `fixture`, `Apollo` or `synthetic` labels.

Verification: `python -m unittest discover -s tests -v`. Tests use HTTP transport doubles, not real credits. A separate read-only auth canary and organization enrichment request establish actual account/API access.

Official references:
- https://docs.apollo.io/reference/authentication
- https://docs.apollo.io/reference/organization-enrichment
- https://docs.apollo.io/reference/people-enrichment
