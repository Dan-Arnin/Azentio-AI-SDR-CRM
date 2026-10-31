---
owns:
  - app/api/accounts.py
  - app/integrations/firecrawl.py
  - app/engine/dossier.py
touches:
  - app/static/console.html
depends_on:
  - app/contracts.py
  - app/llm/client.py
  - app/llm/calllog.py
---
# Account (company) pages and person profiles

Status: In progress. Inspired by Valley (joinvalley.co): "signal → research → reason to reach out → message", with research
and conversation history in one place, adapted for banking (financials, leadership, regulatory/digital initiatives).

## Rules
- Public research uses the **Firecrawl API** (`FIRECRAWL_API_KEY`): search + scrape public pages, then gpt-6-luna extracts
  structured facts. **Every fact/leader/signal/priority must carry a source URL and a verbatim quote that appears in the
  scraped text** (drop anything that fails verification). Unknown stays unknown; never invent numbers.
- **Demo companies** (domains present in `fixtures/apollo_mock.json` companies) are fictional: never browse them; return
  `status: "skipped_demo"` and build the page from profile + workspace data.
- Every Firecrawl request and model call is recorded in the AI call log (provider `firecrawl` / `openai_agents_sdk`).
- Dossiers are cached in the `dossiers` DB kind (`id = "co:<domain>"` or `"person:<leadId>"`) and re-researchable on demand.
- No pricing/commitment content; talking points may only cite approved Azentio claims (`fixtures/knowledge.json` ids).

## API contract
`GET /v1/accounts/{domain}` →
```
{ "domain", "name", "isDemoCompany": bool,
  "profile": {"industry","country","hq","employeeCount","revenueBand","institutionType","website"},
  "fit": [{"productId","product","score","fit","status","reasons":[str]}],          // ICP per Azentio product
  "accountOwner": {"id","name"} | null,
  "dossier": Dossier | null,
  "contacts": [{"leadId","name","title","stage","assignee","fit","qualificationScore","lastActivityAt"}],
  "campaigns": [{"id","name","contacts"}],
  "opportunities": [{"leadId","name","ownerId","ownerName","stage"}],
  "activity": [{"at","leadId","name","kind","text"}]   // latest 15; kind: email_sent|reply|linkedin|text|stage|handoff
}
```
`POST /v1/accounts/{domain}/research` body `{"companyName"?: str}` → `Dossier` (synchronous, ≤ ~120s).
`POST /v1/research/company` body `{"companyName": str, "domain": str}` → `{"domain", "dossier": Dossier}` (ad-hoc research of any real company; its account page then works even with no leads).
`GET /v1/accounts` → `{"items":[{"domain","name","isDemoCompany","contacts","openOpportunities","researchedAt"}]}`.

Dossier:
```
{ "researchedAt", "status": "complete"|"partial"|"failed"|"skipped_demo", "message": str,
  "summary": str,
  "facts": [{"label","value","asOf","sourceUrl","quote"}],        // e.g. Revenue, Net profit, Total assets, Customers, Branches, Employees, Founded, Listed on
  "leadership": [{"name","title","sourceUrl","quote"}],            // CEO first
  "news": [{"title","date","summary","url"}],
  "signals": [{"type","text","date","sourceUrl","quote"}],         // type: expansion|digital|regulatory|leadership_change|funding|partnership|hiring|results|other
  "priorities": [{"text","sourceUrl","quote"}],
  "talkingPoints": [{"text","claimId"}],                           // tie a priority/signal to ONE approved Azentio claim
  "sources": [{"url","title"}],
  "usage": {"firecrawlCalls","aiCalls","seconds"} }
```
`GET /v1/people/{leadId}` →
```
{ "leadId","name","title","seniority","department","email","phone","country","source","productId","stage",
  "assignee": {"id","name"} | null, "accountOwner": {"id","name"} | null,
  "company": {"domain","name"},
  "fit": {"score","fit","reasons":[str]} | null,
  "reasonToReachOut": str,                        // the signal: their enquiry / event / ad / campaign theme, in one sentence
  "intentStrength", "qualificationScore",
  "bant": {"budget"|"authority"|"need"|"timeline": {"status","quote"}},
  "insights": {"buyingSignals":[{signal,excerpt}],"objections":[{type,excerpt,handling}],"competitors":[{name,excerpt}]},
  "engagement": {"emailsSent","replies","linkedin","texts","timeline":[{"at","channel":"email"|"linkedin"|"text"|"form","direction","subject","excerpt"}]},
  "nextBestAction": str,
  "research": PersonResearch | null }
```
`POST /v1/people/{leadId}/research` → `PersonResearch`:
`{"researchedAt","status","message","summary","mentions":[{"title","date","url","quote"}],"talkingPoints":[{"text","claimId"}],"sources":[{"url","title"}],"usage":{...}}`
(person research = Firecrawl search for "<name>" + "<company>" public mentions: news quotes, conference talks, press releases; skipped_demo for demo companies.)

## Account profile inference

Fit is scored from the account profile: directory/enrichment data first, then lead inputs, then verified research facts
(`Institution type`, `Headquarters country`, …). Researched wording is normalised ("Islamic bank" → `islamic_bank`,
"Kingdom of Bahrain" → `BH`). When research has no explicit fact, country comes from the domain's country TLD or from
country names, capitals and currency codes in the verified summary/facts (e.g. "KWD" → Kuwait), and institution type
from the company name/summary ("Islamic", "Sharia", "Finance House" → Islamic bank; "bank" → bank). Fit reasons are plain
sentences built from the scoring checks (`plain_reasons`), never internal rule text. Islamic banks are a partial fit for
Azentio Lending (it supports conventional and Islamic finance).
