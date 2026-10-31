const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let META = null;
let openLeadId = null;

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : d?.errors ? d.errors.join("; ") : JSON.stringify(d));
  }
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 3200);
}

const STATE_TONE = {
  qualified: "ok", awaiting_reply: "info", pending_approval: "warn", sdr_followup: "warn", replied: "info",
  suppressed: "bad", blocked: "bad", nurture: "", existing_opportunity: "info", captured: "", enriched: "", icp_checked: "",
};
const chip = (text, tone = "") => `<span class="chip ${tone}">${esc(text)}</span>`;
const stateChip = (s) => chip((s || "").replace(/_/g, " "), STATE_TONE[s] ?? "");
const sigTone = { confirmed: "ok", inferred: "warn", missing: "bad" };
const productName = (k) => META?.products?.[k] || k || "—";
const ICP_TONE = { high_fit: "ok", medium_fit: "warn", low_fit: "bad", insufficient_data: "" };
const ICP_LABEL = { high_fit: "High Fit", medium_fit: "Medium Fit", low_fit: "Low Fit", insufficient_data: "Insufficient data" };
const icpChip = (icp) => chip(ICP_LABEL[icp.verdict] || icp.verdict, ICP_TONE[icp.verdict] ?? "");
const SOURCE_LABEL = { website_form: "Website", marketing_campaign: "Campaign", ad_form: "Ad form", webinar: "Webinar", event: "Event", outbound: "Outbound" };
const bantChips = (b) => b ? ["budget", "authority", "need", "timeline"].map((k) => `<span class="chip ${sigTone[b[k]]}" title="${k}: ${b[k]}">${k[0].toUpperCase()}</span>`).join(" ") : "<span class='muted'>no reply yet</span>";
const fullName = (p) => `${p?.first_name || ""} ${p?.last_name || ""}`.trim() || "—";

// ---------- tabs ----------
$$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  refresh();
}

// ---------- pipeline ----------
async function renderPipeline() {
  const [leads, stats] = await Promise.all([api("/api/leads"), api("/api/stats")]);
  const s = stats.by_state;
  const tiles = [
    ["Leads", leads.length], ["Ready for human follow-up", stats.ready_for_human], ["Awaiting reply", s.awaiting_reply || 0], ["Qualified → AE", s.qualified || 0],
    ["With human SDR", s.sdr_followup || 0], ["Pending approval", stats.pending_approvals],
    ["Suppressed / blocked", (s.suppressed || 0) + (s.blocked || 0)], ["Nurture", s.nurture || 0], ["Emails sent (simulated)", stats.emails_sent],
  ];
  $("#stats").innerHTML = tiles.map(([k, v]) => `<div class="tile"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
  fillFilter("#f-state", Object.keys(s));
  fillFilter("#f-source", Object.keys(stats.by_source));
  fillFilter("#f-icp", Object.keys(stats.by_icp));
  const fs = $("#f-state").value, fsrc = $("#f-source").value, ficp = $("#f-icp").value, fready = $("#f-ready").checked;
  const rows = leads.filter((l) => (!fs || l.state === fs) && (!fsrc || l.source === fsrc) && (!ficp || l.icp?.verdict === ficp) && (!fready || l.next.ready_for_human));
  $("#empty-pipeline").style.display = leads.length ? "none" : "block";
  $("#leads tbody").innerHTML = rows.map((l) => {
    const d = l.last_decision;
    return `<tr class="click" data-id="${l.id}">
      <td>${esc(fullName(l.person || l.capture))}<small>${esc(l.email)}</small></td>
      <td>${esc(l.company?.name || l.capture?.company)}<small>${esc(l.company?.country || "")} ${esc(l.company?.institution_type || "")}</small></td>
      <td>${esc(SOURCE_LABEL[l.source] || l.source)}</td>
      <td>${esc(productName(l.product))}</td>
      <td>${l.icp ? `${l.icp.score} ${icpChip(l.icp)}` : "—"}</td>
      <td class="nowrap">${bantChips(l.bant_status)}${d ? `<small>${d.bant_score}/100</small>` : ""}</td>
      <td>${stateChip(l.state)}${l.next.ready_for_human ? `<small>${chip("human follow-up", "warn")}</small>` : ""}</td>
      <td>${esc(l.next.action)}<small>Owner: ${esc(l.next.owner)}</small></td></tr>`;
  }).join("");
  $$("#leads tbody tr").forEach((tr) => tr.addEventListener("click", () => openLead(tr.dataset.id)));
}
function fillFilter(sel, values) {
  const el = $(sel), cur = el.value;
  el.innerHTML = `<option value="">All</option>` + values.sort().map((v) => `<option ${v === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
}
["#f-state", "#f-source", "#f-icp", "#f-ready"].forEach((s) => $(s).addEventListener("change", renderPipeline));

// ---------- lead drawer ----------
async function openLead(id) {
  openLeadId = id;
  const d = await api(`/api/leads/${id}`);
  const l = d.lead, p = l.person || {}, c = l.company || {};
  const last = d.analyses[d.analyses.length - 1];
  const tone = { qualified: "ok", sdr_followup: "warn", suppressed: "bad", blocked: "bad", existing_opportunity: "info", nurture: "" }[l.outcome?.route] ?? "info";
  $("#drawer-body").innerHTML = `
    <div class="row between gap" style="margin-top:12px"><div><h2 style="margin:0">${esc(fullName(p))}</h2>
      <div class="muted">${esc(p.title || "")} · ${esc(c.name || "")} · ${esc(l.email)}</div></div>${stateChip(l.state)}</div>
    ${l.outcome ? `<div class="outcome ${tone}"><b>${esc((l.outcome.route || "").replace(/_/g, " "))}</b> — ${esc(l.outcome.reason)}${l.owner ? `<div class="muted">Owner: ${esc(l.owner.name)} (${esc(l.owner.role)})</div>` : ""}</div>` : ""}
    <div class="outcome info"><b>Recommended next action</b>${d.next.ready_for_human ? ` ${chip("ready for human follow-up", "warn")}` : ""}<div>${esc(d.next.action)}</div><div class="muted">Owner: ${esc(d.next.owner)}</div></div>
    <h3>Capture</h3>
    <div class="kv"><div>Source</div><div>${esc(SOURCE_LABEL[l.source] || l.source)}${l.capture.webinar ? ` · ${esc(l.capture.webinar)}` : ""}${l.capture.event_name ? ` · ${esc(l.capture.event_name)}` : ""}${l.capture.campaign ? ` · ${esc(l.capture.campaign)}` : ""}${l.capture.ad_platform ? ` · ${esc(l.capture.ad_platform)} ad` : ""}${l.capture.asset ? ` · ${esc(l.capture.asset)}` : ""}${l.capture.sequence ? ` · sequence “${esc(l.capture.sequence)}”` : ""}</div>
      <div>Contact</div><div>${esc(fullName(l.capture))} · ${esc(l.capture.title || "no title given")} · ${esc(l.capture.company || "no company given")} · ${esc(l.capture.country || "no country given")}</div>
      <div>Product</div><div>${esc(productName(l.product))}${l.capture.product ? "" : " <span class='muted'>(inferred)</span>"}</div>
      ${l.capture.message ? `<div>Message</div><div>${esc(l.capture.message)}</div>` : ""}</div>
    ${renderEnrichment(l)}
    ${renderIcp(l.icp)}
    ${renderChecks(l)}
    <h3>Email thread</h3>
    ${d.messages.map(renderMail).join("") || "<p class='muted'>No messages.</p>"}
    ${["awaiting_reply", "replied", "sdr_followup", "pending_approval"].includes(l.state) || l.state === "qualified" ? renderReplyBox() : ""}
    ${d.analyses.map((a, i) => renderAnalysis(a, i === d.analyses.length - 1)).join("")}
    <h3>Audit trail</h3>
    <ol class="events">${d.events.map((e) => `<li><b>${esc(e.type.replace(/_/g, " "))}</b> ${esc(e.created_at.slice(11, 19))} — ${esc(eventText(e))}</li>`).join("")}</ol>`;
  $("#drawer").classList.add("open");
  bindMailActions($("#drawer-body"), () => openLead(id));
  const box = $("#reply-box");
  if (box) {
    $("#reply-preset").addEventListener("change", (e) => { $("#reply-text").value = META.samples.scripted_replies[e.target.value] || ""; });
    $("#reply-send").addEventListener("click", async () => {
      const body = $("#reply-text").value;
      try {
        const r = await api(`/api/leads/${id}/reply`, { json: { body } });
        toast(`Reply analysed → ${r.decision.route.replace("_", " ")}`);
        openLead(id); refresh();
      } catch (e) { toast(e.message); }
    });
  }
}
$("#drawer-close").addEventListener("click", () => { $("#drawer").classList.remove("open"); openLeadId = null; });

function eventText(e) {
  const d = e.detail || {};
  if (e.type === "state") return d.state;
  if (e.type === "enriched") return `${d.status} via ${d.source}`;
  if (e.type === "icp_checked") return `${d.score}/100 ${d.verdict}`;
  if (e.type === "reply_analysed") return `${d.intent} (${d.confidence}) via ${d.engine} → ${d.route}`;
  if (e.type === "email_sent") return `${d.subject} (simulated)`;
  if (e.type === "email_approved") return `by ${d.approver}${d.edited ? " (edited)" : ""}`;
  return Object.entries(d).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(", ");
}

function renderEnrichment(l) {
  const p = l.person || {}, c = l.company || {}, e = l.enrichment || {};
  const flags = [...(e.missing || []).map((m) => `<li class="err">Missing: ${esc(m)} (not guessed)</li>`),
                 ...(e.uncertain || []).map((u) => `<li>${chip("uncertain", "warn")} ${esc(u)}</li>`)];
  return `<h3>Enrichment ${chip("mock Apollo", "info")} ${e.status ? chip(`${e.status} match`, { full: "ok", partial: "warn", none: "bad" }[e.status]) : ""}</h3>
    <div class="kv"><div>Designation / seniority</div><div>${esc(p.title || "unknown")} · ${esc(p.seniority || "unknown")}${p._from_form ? " <span class='muted'>(from form)</span>" : ""}</div>
    <div>Industry</div><div>${esc(c.industry || "unknown")}</div>
    <div>Institution</div><div>${esc(c.name || "unknown")} · ${esc(c.institution_type || "type unknown")}</div>
    <div>Location</div><div>${esc(c.country || "?")} · ${esc(c.region || "no target region")}</div>
    <div>Size</div><div>${c.employees != null ? `${esc(c.employees)} employees` : "unknown"}${c.total_assets_usd_bn ? ` · USD ${esc(c.total_assets_usd_bn)}bn assets` : ""}</div></div>
    ${flags.length ? `<ul>${flags.join("")}</ul>` : "<p class='muted'>All enrichment fields present.</p>"}`;
}

function renderIcp(icp) {
  if (!icp) return "";
  return `<h3>ICP fit for ${esc(icp.product_name)}</h3>
    <div class="row between"><b>${icp.score}/100</b>${icpChip(icp)}</div>
    <div class="bar"><i style="width:${icp.score}%"></i></div>
    <div class="muted">${esc(icp.reason)}</div>
    <table><tbody>${icp.checks.map((c) => `<tr><td>${esc(c.criterion.replace("_", " "))}</td><td>${chip(c.result, { pass: "ok", fail: "bad", unknown: "warn" }[c.result])}</td><td>${c.points}/${c.max}</td><td><small>${esc(c.reason)}</small></td></tr>`).join("")}</tbody></table>`;
}

function renderChecks(l) {
  const crm = l.crm || {}, h = l.hygiene;
  const items = [];
  if (crm.existing_account) items.push(`Existing account: ${esc(crm.existing_account.name)} (${esc(crm.existing_account.relationship)}${crm.existing_account.products.length ? `, uses ${esc(crm.existing_account.products.map(productName).join(", "))}` : ""}) · owner ${esc(crm.existing_account.owner)}`);
  if (crm.existing_contact) items.push(`Existing CRM contact (owner ${esc(crm.existing_contact.owner)})`);
  (crm.open_opportunities || []).forEach((o) => items.push(`Open opportunity “${esc(o.name)}” · ${esc(o.stage)} · ${esc(o.owner)}`));
  (crm.duplicate_leads || []).forEach((o) => items.push(`Duplicate lead ${esc(o.id)} (${esc(o.state)})`));
  if (h) { items.push(`Consent: ${esc(h.consent)}`); h.blocks.forEach((b) => items.push(`<span class="err">Blocked: ${esc(b)}</span>`)); }
  if (!items.length && !l.crm) return "";
  return `<h3>CRM &amp; contact hygiene</h3><ul>${items.length ? items.map((i) => `<li>${i}</li>`).join("") : "<li>No CRM match, no duplicates.</li>"}</ul>`;
}

const MAIL_TONE = { sent: "ok", received: "info", pending_approval: "warn", rejected: "bad", blocked_guardrail: "bad", cancelled_reply: "" };
function renderMail(m) {
  const pending = m.status === "pending_approval";
  return `<div class="mail ${m.direction}" data-mid="${m.id}">
    <div class="mail-head"><span><b>${m.direction === "in" ? "Prospect reply" : esc(m.kind.replace(/_/g, " "))}</b> · ${esc(m.subject)}</span>
    <span>${chip(m.status.replace(/_/g, " "), MAIL_TONE[m.status] ?? "")} ${esc((m.sent_at || m.created_at).slice(11, 19))}</span></div>
    ${pending ? `<textarea class="mail-edit" rows="10">${esc(m.body)}</textarea>
      <div class="row gap" style="padding:8px"><button data-act="approve">Approve &amp; send</button><button class="danger" data-act="reject">Reject</button>
      <span class="muted">Human review: nothing is sent until approved.</span></div>` : `<pre>${esc(m.body)}</pre>`}
    ${m.direction === "out" && m.guardrail?.drafted_by ? `<div class="muted" style="padding:0 10px 8px;font-size:12px">Drafted by ${esc(m.guardrail.drafted_by)} · approved claims only · guardrail check ${m.guardrail.violations?.length ? "failed" : "passed"}${(m.guardrail.notes || []).map((n) => ` · ${esc(n)}`).join("")}</div>` : ""}
    ${m.guardrail?.violations?.length ? `<div class="err" style="padding:0 10px 8px">Guardrail: ${esc(m.guardrail.violations.join(", "))}</div>` : ""}</div>`;
}
function bindMailActions(root, after) {
  $$(".mail[data-mid]", root).forEach((el) => {
    const id = el.dataset.mid;
    el.querySelector("[data-act=approve]")?.addEventListener("click", async () => {
      try { await api(`/api/messages/${id}/approve`, { json: { approver: "SDR reviewer", body: el.querySelector(".mail-edit").value } }); toast("Approved and sent (simulated)"); after(); refresh(); }
      catch (e) { toast(e.message); }
    });
    el.querySelector("[data-act=reject]")?.addEventListener("click", async () => {
      const reason = prompt("Reason for rejecting this draft?") || "Rejected by reviewer";
      try { await api(`/api/messages/${id}/reject`, { json: { approver: "SDR reviewer", reason } }); toast("Rejected; task created for SDR"); after(); refresh(); }
      catch (e) { toast(e.message); }
    });
  });
}

function renderReplyBox() {
  const opts = Object.keys(META.samples.scripted_replies).map((k) => `<option value="${k}">${k.replace(/_/g, " ")}</option>`).join("");
  return `<div class="card" id="reply-box" style="margin-top:12px"><h2>Simulate a prospect reply</h2>
    <p class="muted">Replies arrive in the test inbox. Pick a scripted reply or type your own.</p>
    <select id="reply-preset"><option value="">— scripted reply —</option>${opts}</select>
    <textarea id="reply-text" rows="4" style="margin-top:8px" placeholder="Type the prospect's reply…"></textarea>
    <div style="margin-top:8px"><button id="reply-send">Receive reply &amp; analyse</button></div></div>`;
}

function renderAnalysis(a, latest) {
  const r = a.result, d = a.decision, m = r.merged_bant;
  const routeTone = { qualified: "ok", gap_followup: "warn", sdr: "warn", suppress: "bad", nurture: "", wait: "info" }[d.route] ?? "";
  const rows = ["budget", "authority", "need", "timeline"].map((k) => {
    const s = m[k];
    return `<tr><td><b>${k[0].toUpperCase() + k.slice(1)}</b></td><td>${chip(s.status, sigTone[s.status])}</td>
      <td>${s.evidence ? `<blockquote>${esc(s.evidence)}</blockquote><small>source: ${esc(s.source)}</small>` : "<span class='muted'>no evidence</span>"}${s.note ? `<small>${esc(s.note)}</small>` : ""}</td></tr>`;
  }).join("");
  const body = `<div class="kv"><div>Intent</div><div>${chip(r.intent.replace("_", " "), "info")} confidence ${r.intent_confidence}</div>
      <div>Engine</div><div>${esc(r.engine)}${r.engine_note ? ` <span class="muted">— ${esc(r.engine_note)}</span>` : ""}</div>
      <div>BANT score</div><div>${d.bant_score}/100 <span class="muted">(confirmed 25 · inferred 12 · missing 0; across the whole thread)</span></div>
      ${r.asks_for_pricing ? `<div>Pricing asked</div><div>${chip("yes — routed to AE, never quoted", "warn")}</div>` : ""}
      ${r.referral?.name || r.referral?.email ? `<div>Referral</div><div>${esc(r.referral.name)} ${esc(r.referral.title)} ${esc(r.referral.email)}</div>` : ""}</div>
    <table><tbody>${rows}</tbody></table>
    <div class="outcome ${routeTone}"><b>Decision: ${esc(d.route.replace("_", " "))}</b> — ${esc(d.reason)}${d.next_action ? `<div><b>Next action:</b> ${esc(d.next_action)}</div>` : ""}
      <ol class="trace">${d.trace.map((t) => `<li>${esc(t)}</li>`).join("")}</ol></div>`;
  return latest ? `<h3>Reply analysis (latest)</h3>${body}` : `<details><summary>Earlier analysis · ${esc(r.intent)} → ${esc(d.route)}</summary>${body}</details>`;
}

// ---------- inbound ----------
function fillSelect(sel, entries, withBlank) {
  $$(sel).forEach((el) => {
    el.innerHTML = (withBlank ? `<option value="">${withBlank}</option>` : "") + entries.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
  });
}
$$("[data-sample]").forEach((b) => b.addEventListener("click", () => {
  const s = META.samples.inbound[b.dataset.sample];
  const f = $("#lead-form");
  f.reset();
  Object.entries(s).forEach(([k, v]) => { const el = f.elements[k]; if (!el) return; if (el.type === "checkbox") el.checked = !!v; else el.value = v; });
}));
$("#lead-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, payload = {};
  [...f.elements].forEach((el) => { if (!el.name) return; payload[el.name] = el.type === "checkbox" ? el.checked : el.value; });
  Object.keys(payload).forEach((k) => payload[k] === "" && delete payload[k]);
  try {
    const l = await api("/api/leads/inbound", { json: payload });
    $("#lead-form-result").innerHTML = `<div class="outcome info">Captured <b>${esc(l.email)}</b> → ${stateChip(l.state)} ${l.outcome ? esc(l.outcome.reason) : ""}
      <div><button class="ghost" id="open-new">Open lead</button></div></div>`;
    $("#open-new").addEventListener("click", () => openLead(l.id));
    refresh();
  } catch (err) { $("#lead-form-result").innerHTML = `<div class="outcome bad">Rejected: ${esc(err.message)}</div>`; }
});
$("#webinar-sample").addEventListener("click", () => {
  const f = $("#webinar-form");
  f.elements.csv_text.value = META.samples.webinar_csv;
  f.elements.webinar.value = META.samples.webinar_name;
  f.elements.product.value = META.samples.webinar_product;
});
$("#import-sample").addEventListener("click", () => { $("#import-form").elements.text.value = JSON.stringify(META.samples.bulk_json, null, 2); });
$("#import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  if (!e.target.elements.file.files.length) fd.delete("file");
  try {
    const r = await api("/api/leads/import", { method: "POST", body: fd });
    $("#import-result").innerHTML = `<div class="outcome info"><b>${r.created.length}</b> leads captured, <b>${r.rejected.length}</b> rows rejected.
      <ul>${r.rejected.map((x) => `<li class="err">Row ${x.row} (${esc(x.email || "no email")}): ${esc(x.errors.join("; "))}</li>`).join("")}</ul>
      <p class="muted">See the Pipeline tab for how each lead was routed.</p></div>`;
    refresh();
  } catch (err) { $("#import-result").innerHTML = `<div class="outcome bad">${esc(err.message)}</div>`; }
});
$("#webinar-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  if (!e.target.elements.file.files.length) fd.delete("file");
  try {
    const r = await api("/api/leads/webinar", { method: "POST", body: fd });
    $("#webinar-result").innerHTML = `<div class="outcome info"><b>${r.created.length}</b> attendees captured, <b>${r.rejected.length}</b> rows rejected.
      <ul>${r.rejected.map((x) => `<li class="err">Row ${x.row} (${esc(x.email || "no email")}): ${esc(x.errors.join("; "))}</li>`).join("")}</ul>
      <p class="muted">See the Pipeline tab for how each attendee was routed.</p></div>`;
    refresh();
  } catch (err) { $("#webinar-result").innerHTML = `<div class="outcome bad">${esc(err.message)}</div>`; }
});

// ---------- outbound ----------
$("#seq-form").elements.cohort_key.addEventListener("change", syncCohortDefaults);
function syncCohortDefaults() {
  const f = $("#seq-form"), c = META.cohorts[f.elements.cohort_key.value];
  if (!c) return;
  f.elements.theme.value = c.theme;
  f.elements.product.value = c.product;
  f.elements.name.value = c.label;
}
$("#seq-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const btn = f.querySelector("button[type=submit]");
  btn.disabled = true; btn.textContent = "Drafting…";
  try {
    await api("/api/sequences", { json: { cohort_key: f.elements.cohort_key.value, theme: f.elements.theme.value, product: f.elements.product.value, name: f.elements.name.value } });
    toast("Sequence drafted — review and approve below");
    renderSequences();
  } catch (err) { toast(err.message); }
  btn.disabled = false; btn.textContent = "Build cohort & draft sequence";
});

async function renderSequences() {
  const seqs = await api("/api/sequences");
  $("#sequences").innerHTML = seqs.map((s) => {
    const prev = s.cohort.preview, draft = s.status === "draft";
    return `<div class="card" data-sid="${s.id}">
      <div class="row between gap"><h2 style="margin:0">${esc(s.name)}</h2><span>${chip(s.status, { active: "ok", draft: "warn" }[s.status] || "")} ${chip(META.themes[s.theme]?.title || s.theme, "info")} ${chip(productName(s.product))}</span></div>
      <p class="muted">Copy drafted by ${esc(s.engine)} from approved messaging only${s.approved_by ? ` · approved by ${esc(s.approved_by)}` : ""}.</p>
      ${(s.steps.notes || []).map((n) => `<div class="err">${esc(n)}</div>`).join("")}
      <h3>Cohort: ${prev.included.length} included · ${prev.excluded.length} excluded</h3>
      <div class="table-wrap"><table><tbody>
        ${prev.included.map((r) => `<tr><td>${chip("include", "ok")}</td><td>${esc(r.name)}<small>${esc(r.title)}</small></td><td>${esc(r.company)}<small>${esc(r.country)} · ${esc(r.institution_type)}</small></td><td></td></tr>`).join("")}
        ${prev.excluded.map((r) => `<tr><td>${chip("exclude", "bad")}</td><td>${esc(r.name)}<small>${esc(r.title)}</small></td><td>${esc(r.company)}<small>${esc(r.country)} · ${esc(r.institution_type)}</small></td><td><small>${esc(r.reasons.join(" · "))}</small></td></tr>`).join("")}
      </tbody></table></div>
      <h3>Sequence steps (placeholders: {first_name}, {title}, {company})</h3>
      ${s.steps.steps.map((st, i) => {
        const v = s.steps.checks[i]?.violations || [];
        return `<div class="mail out"><div class="mail-head"><span><b>Step ${st.step}</b> · day ${st.day}</span>${v.length ? chip("guardrail: " + v.join(", "), "bad") : chip("guardrail check passed", "ok")}</div>
          ${draft ? `<div style="padding:8px"><input class="st-subject" value="${esc(st.subject)}"><textarea class="st-body" rows="7" style="margin-top:6px">${esc(st.body)}</textarea></div>` : `<pre><b>${esc(st.subject)}</b>\n\n${esc(st.body)}</pre>`}</div>`;
      }).join("")}
      <div class="row gap" style="margin-top:10px">
        ${draft ? `<button class="ghost" data-act="save">Save edits</button><button data-act="approve">Approve &amp; launch</button>` : ""}
        ${s.status === "active" ? `<button data-act="advance">Advance cadence (send next step)</button>` : ""}
      </div>
      ${s.enrollments.length ? `<h3>Enrollments</h3><div class="table-wrap"><table><tbody>${s.enrollments.map((e) => `<tr class="click" data-lead="${e.lead_id}"><td>${esc(e.email)}</td><td>next step ${e.next_step}</td><td>${chip(e.status.replace("_", " "), { active: "info", stopped_reply: "ok", completed: "" }[e.status] ?? "")}</td><td>${stateChip(e.state)}</td></tr>`).join("")}</tbody></table></div>` : ""}
    </div>`;
  }).join("");
  $$("#sequences [data-sid]").forEach((card) => {
    const sid = card.dataset.sid;
    const collect = () => $$(".mail", card).map((m, i) => ({ step: i + 1, day: [0, 3, 7][i], subject: $(".st-subject", m).value, body: $(".st-body", m).value }));
    card.querySelector("[data-act=save]")?.addEventListener("click", async () => {
      try { await api(`/api/sequences/${sid}/steps`, { method: "PUT", json: { steps: collect() } }); toast("Saved; guardrails re-checked"); renderSequences(); } catch (e) { toast(e.message); }
    });
    card.querySelector("[data-act=approve]")?.addEventListener("click", async () => {
      try {
        await api(`/api/sequences/${sid}/steps`, { method: "PUT", json: { steps: collect() } });
        const r = await api(`/api/sequences/${sid}/approve`, { json: { approver: "Sales manager" } });
        toast(`Launched: ${r.enrolled.length} contacts enrolled; first sends await approval`);
        refresh();
      } catch (e) { toast(e.message); }
    });
    card.querySelector("[data-act=advance]")?.addEventListener("click", async () => {
      try { const r = await api(`/api/sequences/${sid}/advance`, { method: "POST" }); toast(`Sent ${r.sent.length}, held ${r.held.length} (awaiting approval)`); refresh(); } catch (e) { toast(e.message); }
    });
    $$("[data-lead]", card).forEach((tr) => tr.addEventListener("click", () => openLead(tr.dataset.lead)));
  });
}

// ---------- approvals ----------
async function renderApprovals() {
  const rows = await api("/api/approvals");
  $("#approvals").innerHTML = rows.length
    ? rows.map((m) => `<div class="card"><div class="muted">To ${esc(m.lead_email)} · template ${esc(m.template_key)}</div>${renderMail(m)}</div>`).join("")
    : "<p class='muted center'>Nothing waiting for approval.</p>";
  bindMailActions($("#approvals"), renderApprovals);
}

// ---------- alerts ----------
async function renderAlerts() {
  const rows = await api("/api/alerts");
  const groups = [["ae_alert", "Sales rep alerts (AE)"], ["sdr_task", "Human SDR tasks"], ["suppressed", "Suppressed (logged)"]];
  $("#alerts").innerHTML = groups.map(([t, title]) => {
    const items = rows.filter((a) => a.type === t);
    return `<h3>${title} · ${items.length}</h3>` + (items.length ? items.map(renderAlert).join("") : "<p class='muted'>None.</p>");
  }).join("");
  $$("#alerts [data-lead]").forEach((b) => b.addEventListener("click", () => openLead(b.dataset.lead)));
  $$("#alerts [data-ack]").forEach((b) => b.addEventListener("click", async () => { await api(`/api/alerts/${b.dataset.ack}/ack`, { method: "POST" }); refresh(); }));
}
function renderAlert(a) {
  const s = a.summary;
  if (a.type === "suppressed") return `<div class="card alert-card"><b>${esc(s.email)}</b> — ${esc(s.reason)} <span class="meta">${esc(a.created_at)}</span></div>`;
  const bant = s.bant && Object.keys(s.bant).length
    ? `<table><tbody>${Object.entries(s.bant).map(([k, v]) => `<tr><td><b>${k}</b></td><td>${chip(v.status, sigTone[v.status])}</td><td>${v.evidence ? `<blockquote>${esc(v.evidence)}</blockquote>` : ""}</td></tr>`).join("")}</tbody></table>` : "";
  return `<div class="card alert-card">
    <div class="row between gap"><div><b>${esc(s.lead.name)}</b>, ${esc(s.lead.title || "")} · ${esc(s.account.name || "")}
      <div class="meta">To ${esc(a.assignee)} · ${esc(a.created_at)} · source ${esc(s.source)} · ${esc(productName(s.product))}${s.crm_opportunity_id ? ` · CRM ${esc(s.crm_opportunity_id)}` : ""}</div></div>
      <span>${chip(a.status, a.status === "acknowledged" ? "" : "warn")}</span></div>
    ${s.task ? `<div class="outcome warn">${esc(s.task)}</div>` : ""}
    <div class="kv" style="margin-top:8px"><div>Account</div><div>${esc(s.account.country || "")} · ${esc(s.account.institution_type || "")} · ${s.account.employees ?? "?"} employees · ICP ${s.icp.score ?? "—"} (${esc(s.icp.verdict || "—")})</div>
      ${s.bant_score != null ? `<div>BANT score</div><div>${s.bant_score}/100</div>` : ""}
      <div>Next step</div><div><b>${esc(s.recommended_next_step)}</b></div></div>
    ${bant}
    <details><summary>Email thread (${s.thread.length})</summary>${s.thread.map((m) => `<div class="mail ${m.direction}"><div class="mail-head"><b>${m.direction === "in" ? "Prospect" : "Agent"}</b> ${esc(m.subject)}</div><pre>${esc(m.body)}</pre></div>`).join("")}</details>
    <div class="row gap"><button class="ghost" data-lead="${a.lead_id}">Open lead</button>${a.status !== "acknowledged" ? `<button data-ack="${a.id}">Acknowledge</button>` : ""}</div></div>`;
}

// ---------- CRM & guardrails ----------
async function renderCrm() {
  const d = await api("/api/crm");
  $("#crm-opps").innerHTML = d.opportunities.length
    ? `<table><tbody>${d.opportunities.map((o) => `<tr><td>${esc(o.name)}<small>${esc(o.id)}</small></td><td>${esc(o.stage)}</td><td>${esc(o.owner)}</td><td><small>${Object.entries(o.bant).map(([k, v]) => `${k}: ${v}`).join(" · ")}</small></td></tr>`).join("")}</tbody></table>`
    : "<p class='muted'>None yet.</p>";
  $("#crm-supp").innerHTML = d.suppressions.length
    ? `<table><tbody>${d.suppressions.map((s) => `<tr><td>${esc(s.email)}</td><td><small>${esc(s.reason)}</small></td></tr>`).join("")}</tbody></table>`
    : "<p class='muted'>Empty.</p>";
  const g = META.guardrails;
  $("#rule").innerHTML = `<p>${esc(META.qualification_rule)}</p><ul>
    <li>Intent confidence below ${g.min_confidence} → human SDR (escalate by default)</li>
    <li>Partially qualified → at most ${g.max_gap_followups} gap-filling follow-up, then human SDR</li>
    <li>Not interested / unsubscribe → suppressed and blocked from further outreach</li>
    <li>ICP: High Fit ≥ ${META.icp_thresholds.high}; Medium Fit ${META.icp_thresholds.medium}–${META.icp_thresholds.high - 1}; Low Fit below that or failing geography/institution type → nurture; 3+ unknown criteria → human review</li></ul>`;
  $("#guardrails").innerHTML = `<ul>
    <li><b>Human review:</b> ${g.approval_mode === "always" ? "every drafted email is reviewed and approved before sending" : `first ${g.approval_first_n_sends} sends of each sequence/template need approval`}</li>
    <li><b>One active sequence per contact</b>; existing CRM opportunities excluded; duplicates routed to a human</li>
    <li><b>Send cap:</b> ${g.daily_send_cap} emails/day; opt-out footer on every email</li>
    <li><b>Regional consent:</b> EU contacts need recorded consent (GDPR) or outreach is blocked</li>
    <li><b>No invention:</b> copy uses approved claims only; banned terms (pricing, discounts, guarantees, SLAs…) block a draft</li>
    <li><b>Evidence check:</b> every BANT quote from the model must appear verbatim in the reply or it is downgraded</li>
    <li><b>Simulated email:</b> nothing is sent to real addresses; all contacts are synthetic (.example domains)</li></ul>`;
}

// ---------- boot ----------
async function refresh() {
  const active = $(".tab.active").id.replace("tab-", "");
  const stats = await api("/api/stats");
  $("#badge-approvals").textContent = stats.pending_approvals || "";
  $("#badge-alerts").textContent = stats.open_alerts || "";
  if (active === "pipeline") renderPipeline();
  if (active === "outbound") renderSequences();
  if (active === "approvals") renderApprovals();
  if (active === "alerts") renderAlerts();
  if (active === "crm") renderCrm();
}
$("#reset").addEventListener("click", async () => {
  if (!confirm("Clear all demo leads, messages and alerts?")) return;
  await api("/api/reset", { method: "POST" });
  $("#drawer").classList.remove("open");
  toast("Demo data cleared");
  refresh();
});

(async function boot() {
  META = await api("/api/meta");
  $("#engine").textContent = `Reply analysis: ${META.engine}`;
  const products = Object.entries(META.products);
  fillSelect("#lead-form select[name=product]", products, "Infer from message");
  fillSelect("#webinar-form select[name=product], #seq-form select[name=product]", products);
  fillSelect("#import-form select[name=product]", products, "— none (use row or infer) —");
  fillSelect("#seq-form select[name=theme]", Object.entries(META.themes).map(([k, t]) => [k, t.title]));
  fillSelect("#seq-form select[name=cohort_key]", Object.entries(META.cohorts).map(([k, c]) => [k, c.label]));
  syncCohortDefaults();
  refresh();
})();
