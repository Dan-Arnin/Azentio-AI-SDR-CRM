#!/usr/bin/env bash
# scripts/demo.sh - End-to-end scripted demo of the Azentio AI SDR backend (inbound website lead -> AE handoff, then outbound campaign -> reply -> sequence stop)
set -euo pipefail

BASE="${BASE:-http://localhost:8077}"
RUN_TIMEOUT="${RUN_TIMEOUT:-240}"
REVIEWER="${REVIEWER:-sdr.omar}"
COMPLETE_BANT="Funding is approved. I lead the selection committee. We are replacing our legacy lending platform and plan vendor selection in Q1 2027."

if [ -t 1 ]; then B=$'\033[1m'; C=$'\033[36m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else B=""; C=""; G=""; Y=""; R=""; N=""; fi

# Print a numbered step header
step() { printf '\n%s━━ %s %s━━%s\n' "$B$C" "$1" "$N$C" "$N"; }

# Print an info line
info() { printf '%s›%s %s\n' "$G" "$N" "$*"; }

# Print a warning line
warn() { printf '%s! %s%s\n' "$Y" "$*" "$N"; }

# Abort with an error message
die() { printf '%s✗ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }

# Shared python helpers prepended to every JSON query
PYLIB='
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw) if raw.strip() else {}
except Exception:
    d = {}
def items(x, *keys):
    if isinstance(x, list): return x
    for k in keys + ("items",):
        if isinstance(x, dict) and isinstance(x.get(k), list): return x[k]
    return []
def latest_asm(detail):
    lead = detail.get("lead") or {}
    asms = detail.get("assessments") or []
    for a in asms:
        if a.get("id") == lead.get("latestAssessmentId"): return a
    return sorted(asms, key=lambda a: a.get("createdAt") or "")[-1] if asms else None
'

# Run a python snippet against JSON on stdin (d = parsed JSON)
pj() { python3 -c "$PYLIB
$1" | sed -e 's/mock_apollo/Apollo/g' -e 's/mock_enrichment/enrichment/g'; }

# Pretty-print JSON on stdin (jq if present)
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else python3 -m json.tool; fi; }

# HTTP call returning the body; fails loudly on non-2xx with the API error code
api() {
  local method="$1" path="$2" body="${3:-}" extra="${4:-}" out code
  out="$(mktemp)"
  if [ -n "$body" ]; then
    code=$(curl -sS -o "$out" -w '%{http_code}' -X "$method" "$BASE$path" -H 'Content-Type: application/json' ${extra:+-H "$extra"} --data "$body") || { rm -f "$out"; die "$method $path: backend unreachable at $BASE"; }
  else
    code=$(curl -sS -o "$out" -w '%{http_code}' -X "$method" "$BASE$path") || { rm -f "$out"; die "$method $path: backend unreachable at $BASE"; }
  fi
  if [ "${code:0:1}" != "2" ]; then
    printf '%s✗ %s %s -> HTTP %s%s\n' "$R" "$method" "$path" "$code" "$N" >&2
    cat "$out" >&2; echo >&2; rm -f "$out"; exit 1
  fi
  cat "$out"; rm -f "$out"
}

# Generate a UUID for Idempotency-Key headers
uuid() { python3 -c 'import uuid; print(uuid.uuid4())'; }

# Wait for a background run to finish (succeeded|failed) with a timeout
wait_run() {
  local run_id="$1" label="${2:-run}" start status
  [ -z "$run_id" ] || [ "$run_id" = "None" ] && { warn "no runId for $label"; return 0; }
  start=$(date +%s)
  printf '%s…%s waiting for %s %s ' "$Y" "$N" "$label" "$run_id"
  while true; do
    status=$(api GET "/v1/runs/$run_id" | pj 'print(d.get("status",""))')
    case "$status" in
      succeeded) printf '%s✓%s\n' "$G" "$N"; break ;;
      failed) printf '%s✗%s\n' "$R" "$N"; api GET "/v1/runs/$run_id" | pj 'print("  errorCode:", d.get("errorCode")); [print("  -", s.get("name"), s.get("status"), s.get("detail")) for s in d.get("steps", [])]'; break ;;
    esac
    [ $(( $(date +%s) - start )) -gt "$RUN_TIMEOUT" ] && { printf '\n'; die "$label $run_id timed out after ${RUN_TIMEOUT}s"; }
    printf '.'; sleep 1
  done
  api GET "/v1/runs/$run_id" | pj '[print("   ·", s.get("name"), "-", s.get("detail", "")) for s in d.get("steps", [])]'
}

# Poll until a python predicate over JSON from GET <path> prints a non-empty value
wait_for() {
  local path="$1" expr="$2" label="$3" timeout="${4:-60}" start val
  start=$(date +%s)
  while true; do
    val=$(api GET "$path" | pj "$expr" || true)
    [ -n "$val" ] && [ "$val" != "None" ] && { echo "$val"; return 0; }
    [ $(( $(date +%s) - start )) -gt "$timeout" ] && die "timed out waiting for $label"
    sleep 1
  done
}

# Show lead stage + ICP result
show_icp() {
  api GET "/v1/leads/$1" | pj '
l = d.get("lead", {}); icp = l.get("icp") or {}
print("  stage:", l.get("stage"), "| owner:", l.get("ownerId"))
print("  ICP score:", icp.get("score"), "| fit:", icp.get("fit"), "| status:", icp.get("status"), "| policy:", icp.get("policyVersion"))
for c in icp.get("checks", []):
    mark = {True: "✓", False: "✗"}.get(c.get("passed"), "?")
    print("   ", mark, (c.get("rule") or "").ljust(18), str(c.get("points")) + "/" + str(c.get("weight")), "-", c.get("detail"))
for r in icp.get("reasons", []): print("    reason:", r)
e = l.get("enrichment") or {}
print("  enrichment:", e.get("provider"), "found" if e.get("found") else "not found", "| missing:", e.get("missing"), "| conflicts:", len(e.get("conflicts") or []))'
}

# Show latest assessment with BANT evidence excerpts
show_assessment() {
  api GET "/v1/leads/$1" | pj '
l = d.get("lead", {}); a = latest_asm(d)
print("  stage:", l.get("stage"))
if not a:
    print("  (no assessment)"); sys.exit()
print("  route:", a.get("route"), "| intent:", a.get("replyIntent"), a.get("intentConfidence"), "| BANT score:", a.get("bantScore"), "| qual:", a.get("qualificationScore"), "| mode:", a.get("mode"))
for k in ("budget", "authority", "need", "timeline"):
    f = (a.get("bant") or {}).get(k) or {}
    ex = [e.get("excerpt") for e in f.get("evidence", []) if e.get("excerpt")]
    origins = sorted({e.get("origin") for e in f.get("evidence", [])})
    print("   ", k.ljust(9), (f.get("status") or "missing").ljust(9), ("\"" + ex[0] + "\"") if ex else "", origins if origins else "")
for r in a.get("routeReasons", []): print("    route reason:", r)
if a.get("nextQuestion"): print("  next question:", a.get("nextQuestion"))
dec = a.get("decision") or {}
if dec: print("  judge:", dec.get("provider"), dec.get("model"), "->", dec.get("intent"), dec.get("intentConfidence"))'
}

# ---------------------------------------------------------------------------------------------------------------

step "0. Health check ($BASE)"
for i in $(seq 1 30); do curl -sf "$BASE/health" >/dev/null 2>&1 && break; [ "$i" = 30 ] && die "backend not reachable at $BASE/health"; sleep 1; done
api GET /health | pretty

step "1. Reset demo data"
api POST /v1/demo/reset | pretty

step "2. Website enquiry arrives (Layla Haddad, Gulf Crescent Bank)"
LEAD_BODY=$(python3 -c '
import json, uuid, datetime
print(json.dumps({"sourceRecordId": "web-" + uuid.uuid4().hex[:10], "source": "website", "fullName": "Layla Haddad",
  "email": "layla.haddad@gulfcrescentbank.ae", "companyName": "Gulf Crescent Bank", "companyDomain": "gulfcrescentbank.ae",
  "country": "AE", "productId": "lending_platform", "role": "Head of Digital Lending", "phone": "+971 50 555 0101",
  "contactPermission": "allowed", "inquiry": "We are replacing our lending platform. Please arrange a discussion.",
  "submittedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()}))')
R=$(api POST /v1/leads "$LEAD_BODY" "Idempotency-Key: $(uuid)")
echo "$R" | pretty
LEAD_ID=$(echo "$R" | pj 'print(d["leadId"])')
RUN_ID=$(echo "$R" | pj 'print(d.get("runId"))')
wait_run "$RUN_ID" "intake run (identity → enrich → ICP → assess → draft)"

step "3. Enrichment + ICP score with reasons"
show_icp "$LEAD_ID"

step "4. Qualifying email draft awaiting human approval"
DRAFT_ID=$(wait_for "/v1/leads/$LEAD_ID" 'print(next((x["id"] for x in sorted(d.get("drafts", []), key=lambda x: x.get("createdAt") or "", reverse=True) if x.get("status") == "DRAFT"), ""))' "pending draft" 90)
DRAFT=$(api GET "/v1/leads/$LEAD_ID" | pj "print(json.dumps(next(x for x in d['drafts'] if x['id'] == '$DRAFT_ID')))")
echo "$DRAFT" | pj 'print("  kind:", d["kind"], "| v" + str(d["version"]), "| to:", d["to"], "| guardrails:", d.get("guardrailFlags") or "passed")
print("  subject:", d["subject"]); print("  " + d["body"].replace("\n", "\n  "))'
HASH=$(echo "$DRAFT" | pj 'print(d.get("contentHash", ""))')

step "5. Human approves (contentHash-bound) and the simulated send goes out"
api POST "/v1/drafts/$DRAFT_ID/approve" "{\"reviewer\": \"$REVIEWER\", \"contentHash\": \"$HASH\"}" | pj 'print("  status:", d.get("status"), "| approvedBy:", d.get("approvedBy"))'
api POST "/v1/drafts/$DRAFT_ID/send" | pj 'print("  message:", d.get("id"), d.get("direction"), d.get("origin"), "|", d.get("subject"))'

step "6. Buyer replies with complete BANT"
info "\"$COMPLETE_BANT\""
R=$(api POST "/v1/leads/$LEAD_ID/replies" "$(python3 -c 'import json,sys; print(json.dumps({"bodyText": sys.argv[1], "origin": "scripted"}))' "$COMPLETE_BANT")")
echo "$R" | pretty
wait_run "$(echo "$R" | pj 'print(d.get("runId"))')" "reply run (decide → extract BANT → route)"

step "7. Assessment: BANT with verbatim evidence"
show_assessment "$LEAD_ID"

step "8. AE alert with handoff card"
TASK_ID=$(wait_for /v1/alerts "print(next((t['id'] for t in items(d) if t.get('leadId') == '$LEAD_ID'), ''))" "AE alert" 30 || true)
api GET /v1/alerts | pj "
for t in items(d):
    if t.get('leadId') != '$LEAD_ID': continue
    c = t.get('card') or {}
    print('  task:', t['id'], t['type'], '->', t['ownerId'], '|', t['title'])
    print('  summary:', c.get('summary'))
    print('  next action:', c.get('recommendedNextAction'), '| unresolved:', c.get('unresolved'))"

step "9. AE accepts → OPPORTUNITY"
api PATCH "/v1/tasks/$TASK_ID" '{"status": "accepted"}' | pj 'print("  task status:", d.get("status"))'
api GET "/v1/leads/$LEAD_ID" | pj 'print("  lead stage:", d["lead"]["stage"])'

# ---------------------------------------------------------------------------------------------------------------

step "10. Outbound campaign: Islamic digital transformation (CTO / Heads of Transformation / CEOs, GCC Islamic banks)"
R=$(api POST /v1/campaigns '{"name": "GCC Islamic Digital Transformation Q4", "theme": "Islamic digital transformation",
  "cohort": {"productId": "islamic_banking", "roles": ["Chief Transformation Officer", "Head of Transformation", "CEO"],
             "countries": ["GCC"], "institutionTypes": ["islamic_bank"], "limit": 10}, "touchDays": [0, 3, 7]}')
CAMPAIGN_ID=$(echo "$R" | pj 'print((d.get("campaign") or d).get("id"))')
echo "$R" | pj '
c = d.get("campaign") or d
print("  campaign:", c.get("id"), c.get("status"), "| enrolled:", len(d.get("enrollments") or c.get("enrollmentIds") or []), "| drafts:", len(d.get("drafts") or []))
for x in c.get("exclusions", []): print("  excluded:", x.get("email") or x.get("leadId"), "-", x.get("reason"))
for x in d.get("drafts", []): print("   ", x.get("kind").ljust(17), x.get("status").ljust(6), x.get("to"), "| due", x.get("dueAt"))'
wait_run "$(echo "$R" | pj 'print(d.get("runId"))')" "campaign run"

step "11. Human approves every touch-1 draft"
api GET "/v1/drafts?status=DRAFT" | pj 'print("\n".join(x["id"] + " " + x.get("contentHash", "") for x in items(d) if x.get("kind") == "outbound_touch_1"))' | while read -r did dhash; do
  [ -z "$did" ] && continue
  api POST "/v1/drafts/$did/approve" "{\"reviewer\": \"$REVIEWER\", \"contentHash\": \"$dhash\"}" | pj 'print("  approved", d.get("id"), d.get("kind"), "->", d.get("to"))'
done

step "12. Advance demo clock +24h → due touches are sent"
api POST /v1/demo/clock '{"advanceHours": 24}' | pj '
print("  now:", d.get("now"))
for s in d.get("sent", []): print("  sent   ", s.get("kind"), s.get("leadId"), s.get("messageId"))
for s in d.get("skipped", []): print("  skipped", s.get("kind"), s.get("leadId"), s.get("code"))
print("  waiting approval:", len(d.get("waitingApproval", [])))'

step "13. One contact replies → sequence stops (remaining touches STALE) → BANT flow"
OUT_LEAD=$(api GET "/v1/campaigns/$CAMPAIGN_ID" | pj 'print(next((e["leadId"] for e in d.get("enrollments", []) if e.get("state") == "ACTIVE"), ""))')
[ -z "$OUT_LEAD" ] && die "no active enrollment to reply from"
api GET "/v1/leads/$OUT_LEAD" | pj 'i = d["lead"]["input"]; print("  replying as:", i["fullName"], "-", i.get("role"), "@", i["companyName"])'
R=$(api POST "/v1/leads/$OUT_LEAD/replies" "$(python3 -c 'import json,sys; print(json.dumps({"bodyText": sys.argv[1], "origin": "scripted"}))' "$COMPLETE_BANT")")
echo "$R" | pj 'print("  stoppedEnrollments:", d.get("stoppedEnrollments"))'
wait_run "$(echo "$R" | pj 'print(d.get("runId"))')" "reply run"
api GET "/v1/leads/$OUT_LEAD" | pj '
for e in d.get("enrollments", []): print("  enrollment", e["id"], e["state"], "| stopReason:", e.get("stopReason"))
for x in sorted(d.get("drafts", []), key=lambda x: x.get("kind")): print("   ", x["kind"].ljust(17), x["status"])'
show_assessment "$OUT_LEAD"
api GET "/v1/leads/$OUT_LEAD" | pj '
for t in d.get("tasks", []): print("  task:", t["type"], "->", t["ownerId"], "|", t["status"], "|", t["title"])'

step "14. CRM push summary"
api GET /v1/crm/outbox | pj '
from collections import Counter
xs = items(d)
print("  events:", len(xs))
for (t, s), n in sorted(Counter((x.get("type"), x.get("status")) for x in xs).items()): print("   ", str(t).ljust(24), str(s).ljust(10), n)'

printf '\n%s✓ Demo complete.%s Open %s/ for the live console.\n' "$B$G" "$N" "$BASE"
