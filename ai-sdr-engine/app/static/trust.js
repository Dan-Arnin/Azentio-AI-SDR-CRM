"use strict";
// Trust & governance page (#/trust). Roles are checked on the server; every figure opens the records behind it.
// Uses the console's shared helpers (h, icon, $, S, toast, openModal, closeModal, fmtDate, initials, refresh).

const TR = { user: undefined, users: [], pick: "mgr.hana", report: null, summary: null, days: 30, campaign: "", channel: "", area: "", err: null, signErr: "" };
const TR_AREAS = ["Security", "Compliance", "Trust", "Paid ads"];
const TR_PANEL_AREAS = { oversight: ["Trust"], evidence: ["Trust"], guardrails: ["Trust", "Security"], consent: ["Compliance"], sending: ["Compliance", "Paid ads"], dataRights: ["Compliance"], audit: ["Security"] };
const TR_CHANNELS = [["", "All channels"], ["email", "Email"], ["sms", "SMS"], ["whatsapp", "WhatsApp"], ["linkedin", "LinkedIn"], ["linkedin_ads", "LinkedIn Ads"], ["google_ads", "Google Ads"]];
const TR_BUILT = ["", "email", "sms", "whatsapp", "linkedin"];
const trQuery = () => "days=" + TR.days + (TR.campaign ? "&campaign=" + encodeURIComponent(TR.campaign) : "") + (TR.channel && TR_BUILT.includes(TR.channel) ? "&channel=" + TR.channel : "");
const TR_ACTIONS = {
  "draft.approved": "approved a draft", "draft.rejected": "rejected a draft", "draft.edited": "edited a draft", "send.sent": "sent an email",
  "send.blocked": "send blocked", "campaign.launched": "launched a campaign", "campaign.paused": "paused a campaign", "campaign.resumed": "resumed a campaign",
  "suppression.added": "added a suppression entry", "suppression.removed": "removed a suppression entry", "flag.cleared": "cleared a guardrail flag",
  "flag.confirmed": "confirmed a guardrail flag", "guardrail.injection_blocked": "blocked a hidden instruction", "lead.owner_changed": "changed a lead owner",
  "auth.sign_in": "signed in", "auth.sign_out": "signed out", "auth.sign_in_failed": "failed to sign in", "auth.sign_in_locked": "sign-in locked",
  "data_request.started": "started a data request", "data_request.completed": "completed a data request", "retention.changed": "changed the retention period",
  "retention.run": "ran retention", "demo.reset": "reset the workspace", "evidence_pack.exported": "exported the evidence pack",
};
const TR_BLOCK = { contact_blocked: "Suppression list or no consent", send_cap: "Daily cap reached", campaign_paused: "Campaign paused", sequence_stopped: "Sequence stopped", not_approved: "Not approved", stale_version: "Changed after approval" };
const TR_PEOPLE = { "mgr.hana": "Hana Al Suwaidi", "admin.ravi": "Ravi Menon", "sdr.omar": "Nadia Karim", system: "System", prospect: "Prospect" };

// ---------- data ----------
async function gapi(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  if (opts.raw && r.ok) return r;
  let data = null; try { data = await r.json(); } catch (_) {}
  if (!r.ok) { const code = (data && ((data.detail && data.detail.code) || (data.error && data.error.code))) || "http_" + r.status; const e = new Error(code); e.code = code; e.status = r.status; throw e; }
  return data;
}
const TR_ERR = { invalid_credentials: "That password isn't right.", too_many_attempts: "Too many attempts. Try again in 15 minutes.", reason_required: "Add a one-line reason first.",
  opt_out_protected: "Opt-outs and erasures can't be removed. Only manual entries can.", subject_mismatch: "That email doesn't match the request.", invalid_value: "Enter an email address or a domain such as @bank.example.",
  forbidden: "Your role can't do that.", sign_in_required: "Please sign in again.", already_completed: "That request is already completed." };
const trErr = e => TR_ERR[e && e.code] || "Something went wrong. Please try again.";

async function loadTrust() {
  try {
    TR.user = (await gapi("/v1/auth/me")).user;
    if (!TR.user) { TR.users = (await gapi("/v1/auth/users")).items || []; TR.report = TR.summary = null; return; }
    if (TR.user.permissions.includes("viewGovernance")) {
      TR.report = await gapi("/v1/governance/report?" + trQuery()); TR.summary = null;
    } else { TR.summary = await gapi("/v1/governance/me?days=" + TR.days); TR.report = null; }
    TR.err = null;
  } catch (e) { TR.err = e.code; }
}
async function trDo(btn, fn, ok) {
  if (btn) { btn.disabled = true; btn.setAttribute("aria-busy", "true"); }
  try { const r = await fn(); if (ok) toast(typeof ok === "function" ? ok(r) : ok); await refresh(true); return r; }
  catch (e) { toast(trErr(e), "err"); throw e; }
  finally { if (btn && btn.isConnected) { btn.disabled = false; btn.removeAttribute("aria-busy"); } }
}
async function trDownload(path, fallbackName, opts) {
  const r = await gapi(path, { ...(opts || {}), raw: true });
  const name = ((r.headers.get("content-disposition") || "").match(/filename="([^"]+)"/) || [])[1] || fallbackName;
  const url = URL.createObjectURL(await r.blob());
  const a = h("a", { href: url, download: name }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 2000);
  return name;
}

// ---------- small pieces ----------
const trRange = () => TR.report || TR.summary ? (p => fmtDay(p.start) + " – " + fmtDay(p.end))((TR.report || TR.summary).period) : "";
const trNone = () => "No activity in period";
const trWho = id => TR_PEOPLE[id] || (PEOPLE[id] ? PEOPLE[id][0] : id);
function trStatus(s) {
  const map = { Live: ["live", "check", "Live"], Partial: ["partial", "alert", "Partial"], "Not built": ["nb", "minus", "Not built"], open: ["open", "alert", "Open"], cleared: ["live", "check", "Cleared"],
    confirmed: ["partial", "alert", "Confirmed"], superseded: ["nb", "minus", "Superseded"], paused: ["partial", "pause", "Paused"], active: ["live", "check", "Active"], completed: ["nb", "check", "Completed"], drafting: ["nb", "clock", "Drafting"] };
  const [cls, ic, word] = map[s] || ["nb", "minus", s];
  return h("span", { class: "tr-status " + cls }, icon(ic), word);
}
const trSim = () => h("span", { class: "tr-tag", title: "Runs against simulated data" }, "Simulated");
const trNotBuilt = label => h("div", { class: "tr-kv" }, h("span", null, label), h("span", { class: "tr-tag nb" }, "Not built"));
const trIds = ids => h("div", { class: "tr-ids" }, ids.map(i => h("span", { class: "tr-id" }, i)));
// A clickable figure: opens the records behind it
function trStat(value, label, drill, none) {
  const empty = value == null || (none && !value);
  return h("button", { class: "tr-stat", type: "button", disabled: !drill, onclick: drill ? () => trDrill(drill) : null, "aria-label": label + ": " + (empty ? trNone() : value) + (drill ? ". Open records" : "") },
    h("b", { class: empty ? "none" : null }, empty ? "No activity" : value), h("span", null, label));
}
function trRing(pct) {
  const r = 20, c = 2 * Math.PI * r, s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 48 48"); s.setAttribute("class", "tr-ring"); s.setAttribute("aria-hidden", "true");
  for (const [cls, dash] of [["bg", c], ["fg", c * (pct || 0) / 100]]) {
    const e = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    e.setAttribute("cx", 24); e.setAttribute("cy", 24); e.setAttribute("r", r); e.setAttribute("class", cls); e.setAttribute("stroke-dasharray", dash + " " + c); s.append(e);
  }
  return s;
}
function trMark(text, phrases) {
  const list = (phrases || []).filter(Boolean).sort((a, b) => b.length - a.length);
  if (!list.length) return [text];
  const re = new RegExp("(" + list.map(p => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "gi");
  return String(text || "").split(re).map((part, i) => i % 2 ? h("mark", { class: "tr-hl" }, part) : part);
}
function trPanel(key, title, lede, ids, ...body) {
  if (TR.area && !(TR_PANEL_AREAS[key] || []).includes(TR.area)) return null;
  return h("section", { class: "card tr-panel", "aria-labelledby": "tp-" + key },
    h("div", { class: "card-head" }, h("div", null, h("h2", { id: "tp-" + key }, title), lede && h("p", null, lede)), trIds(ids)), h("div", { class: "card-body" }, body));
}

// ---------- sign-in and header ----------
function trSignIn() {
  const pw = h("input", { class: "field", type: "password", id: "tr-pw", autocomplete: "current-password", "aria-describedby": "tr-pw-err" });
  const submit = async e => {
    e.preventDefault(); const btn = e.currentTarget.querySelector("button[type=submit]");
    if (!pw.value) { TR.signErr = "Enter the demo password."; render(); return; }
    try { btn.disabled = true; await gapi("/v1/auth/login", { method: "POST", body: { userId: TR.pick, password: pw.value } }); TR.signErr = ""; await refresh(true); }
    catch (err) { TR.signErr = trErr(err); render(); setTimeout(() => { const p = $("#tr-pw"); p && p.focus(); }, 0); }
  };
  return h("div", null,
    h("div", { class: "page-head" }, h("div", null, h("p", { class: "eyebrow" }, "Trust & governance"), h("h1", null, "Human oversight, consent ", h("span", { class: "u" }, "and audit")),
      h("p", { class: "lede" }, "Sign in to see live evidence for every control. Managers and admins see the whole page; SDRs see their own trust summary."))),
    h("form", { class: "card tr-signin", onsubmit: submit, novalidate: true }, h("div", { class: "card-body" },
      h("h2", null, "Sign in"),
      h("div", { class: "tr-users", role: "group", "aria-label": "Demo user" }, (TR.users || []).map(u => h("button", { type: "button", class: "tr-user", "aria-pressed": String(TR.pick === u.id), onclick: () => { TR.pick = u.id; render(); } },
        h("span", { class: "av" }, initials(u.name)), h("span", null, h("b", null, u.name), h("small", null, u.roleLabel))))),
      h("label", { class: "fld", for: "tr-pw" }, h("span", null, "Demo password"), pw),
      TR.signErr ? h("p", { class: "tr-err", id: "tr-pw-err", role: "alert" }, TR.signErr) : null,
      h("div", { style: "display:flex;justify-content:flex-end;margin-top:14px" }, h("button", { class: "btn", type: "submit" }, icon("shield", "sm"), "Sign in")),
      h("p", { class: "muted small", style: "margin:12px 0 0" }, "The password is set on the server (CONSOLE_PASSWORD). Sign-in attempts are recorded in the audit log."))));
}
function trHeader(title, lede) {
  const g = TR.report || TR.summary;
  return h("div", { class: "page-head tr-page-head" },
    h("div", null, h("p", { class: "eyebrow" }, "Trust & governance"), h("h1", null, title[0], h("span", { class: "u" }, title[1])), h("p", { class: "lede" }, lede)),
    h("div", { class: "tr-head-right" }, g && g.generatedAt ? h("span", { class: "tr-live" }, "Live · computed " + fmtDate(g.generatedAt)) : null,
      h("span", { class: "tr-me" }, h("span", null, h("b", null, TR.user.name), " · " + TR.user.roleLabel),
        h("button", { class: "btn sm quiet", type: "button", onclick: e => trDo(e.currentTarget, () => gapi("/v1/auth/logout", { method: "POST" }), "Signed out") }, "Sign out"))));
}

// ---------- SDR summary ----------
function trSdr() {
  const s = TR.summary;
  return h("div", null, trHeader(["My trust ", "summary"], "Your flagged drafts, opt-outs on your leads and your own audit entries for the last " + s.period.days + " days."),
    h("div", { class: "tr-grid" },
      h("section", { class: "card tr-panel" }, h("div", { class: "card-head" }, h("h2", null, "Drafts flagged for wording"), h("span", { class: "count" + (s.flaggedDrafts.length ? " hot" : "") }, s.flaggedDrafts.length)),
        h("div", { class: "card-body" }, s.flaggedDrafts.length ? s.flaggedDrafts.map(f => h("div", { class: "tr-kv" }, h("span", null, h("b", null, f.lead), " · " + f.reason, h("div", { class: "muted small" }, "“" + f.excerpt + "”")),
          h("a", { class: "btn sm ghost", href: "#/leads/" + f.leadId }, icon("edit", "sm"), "Fix draft"))) : h("p", { class: "muted" }, "No flagged drafts. " + trNone().toLowerCase() + "."))),
      h("section", { class: "card tr-panel" }, h("div", { class: "card-head" }, h("h2", null, "Opt-outs on your leads"), h("span", { class: "count" }, s.optOuts.length)),
        h("div", { class: "card-body" }, s.optOuts.length ? s.optOuts.map(o => h("div", { class: "tr-kv" }, h("span", null, h("b", null, o.name), " · " + fmtDate(o.at)), h("b", null, o.sentAfter + " sent after"))) : h("p", { class: "muted" }, trNone() + " (" + trRange() + ")")))),
    h("section", { class: "card tr-panel tr-full" }, h("div", { class: "card-head" }, h("h2", null, "Your audit entries")), h("div", { class: "card-body" }, trAuditList(s.myAudit))));
}

// ---------- full page ----------
function trFilters() {
  const r = TR.report;
  const set = (k, v) => { TR[k] = v; refresh(true); };
  return h("div", { class: "tr-filters", role: "group", "aria-label": "Filters" },
    h("div", { class: "grp" }, "Period", h("div", { class: "segs" }, [7, 14, 30, 90].map(d => h("button", { class: "seg", type: "button", "aria-pressed": String(TR.days === d), onclick: () => set("days", d) }, d + " days")))),
    h("label", { class: "grp" }, "Campaign", h("select", { onchange: e => set("campaign", e.target.value) }, h("option", { value: "" }, "All campaigns"),
      (r.campaigns || []).map(c => h("option", { value: c.id, selected: TR.campaign === c.id }, c.name)))),
    h("label", { class: "grp" }, "Channel", h("select", { onchange: e => { TR.channel = e.target.value; if (TR_BUILT.includes(TR.channel)) refresh(true); else render(); } },
      TR_CHANNELS.map(([v, l]) => h("option", { value: v, selected: TR.channel === v }, l + (TR_BUILT.includes(v) ? "" : " (not built)"))))),
    h("label", { class: "grp" }, "Area", h("select", { onchange: e => { TR.area = e.target.value; render(); } }, h("option", { value: "" }, "All areas"), TR_AREAS.map(a => h("option", { value: a, selected: TR.area === a }, a)))),
    h("span", { class: "spacer" }),
    h("button", { class: "btn sm", type: "button", onclick: e => trDo(e.currentTarget, () => trDownload("/v1/governance/export?" + trQuery(), "evidence-pack.zip"), n => "Downloaded " + n) },
      icon("upload", "sm"), "Export evidence pack"));
}
function trTile(t) {
  const n = t.now || {}, p = t.prev;
  let main, sub = null, delta = null;
  if (t.kind === "ratio") {
    main = n.den ? h("div", { class: "row" }, trRing(n.pct), h("div", null, h("div", { class: "v" }, Math.round(n.pct) + "%"), h("div", { class: "sub" }, n.num + " of " + n.den)))
      : h("div", { class: "v none" }, trNone());
    if (n.den && p && p.den) { const d = Math.round(n.pct - p.pct); delta = d ? (d > 0 ? "▲ " : "▼ ") + Math.abs(d) + " pts vs previous" : "No change vs previous"; }
    if (t.target) sub = "Target " + t.target + "%";
  } else {
    main = h("div", { class: "v" }, n.num);
    if (p) { const d = n.num - p.num; delta = d ? (d > 0 ? "▲ " : "▼ ") + Math.abs(d) + " vs previous" : "No change vs previous"; }
  }
  const tone = !delta || /No change/.test(delta) ? "" : (/▲/.test(delta) === !t.goodDown ? "up" : "down");
  return h("button", { class: "tr-tile", type: "button", onclick: () => trDrill(t.drill), "aria-label": t.label + ". Open records" },
    h("span", { class: "k" }, t.label), main, sub ? h("span", { class: "sub" }, sub) : null, delta ? h("span", { class: "d " + tone }, delta) : null);
}
// "status: DRAFT → APPROVED · version: 2" from an audit entry's before/after
function trChange(a) {
  const b = a.before || {}, f = a.after || {}, show = v => { const t = typeof v === "object" ? JSON.stringify(v) : String(v); return /^[0-9a-f]{24,}$/.test(t) ? t.slice(0, 10) + "…" : t.length > 70 ? t.slice(0, 70) + "…" : t; };
  const parts = [...new Set([...Object.keys(b), ...Object.keys(f)])].filter(k => JSON.stringify(b[k]) !== JSON.stringify(f[k]))
    .map(k => k in b && k in f ? `${k}: ${show(b[k])} → ${show(f[k])}` : k in f ? `${k}: ${show(f[k])}` : `${k} was ${show(b[k])}`);
  return [parts.join(" · "), a.detail].filter(Boolean).join(" · ");
}
const trDuration = d => d == null ? null : d >= 1 ? Math.round(d * 10) / 10 + " d" : d * 24 >= 1 ? Math.round(d * 24) + " h" : Math.max(1, Math.round(d * 1440)) + " min";

function trAuditList(rows) {
  if (!rows || !rows.length) return h("p", { class: "muted" }, trNone() + " (" + trRange() + ")");
  return h("ul", { class: "tr-audit" }, rows.map(a => {
    const chg = trChange(a);
    return h("li", null, h("span", { class: "av" }, initials(trWho(a.actor))),
      h("div", null, h("div", { class: "what" }, h("b", null, trWho(a.actor)), " ", h("span", { class: "muted" }, "(" + (a.role || "").replace(/_/g, " ") + ")"), " " + (TR_ACTIONS[a.action] || a.action),
        a.lead_id ? [" · ", h("a", { href: "#/leads/" + a.lead_id }, "lead")] : (a.record_id ? " · " + a.record_type + " " + String(a.record_id).slice(0, 24) : "")),
        chg ? h("div", { class: "chg" }, chg.slice(0, 260)) : null),
      h("time", { datetime: a.at }, fmtDate(a.at)));
  }));
}

function trFull() {
  const r = TR.report, P = r.panels, canAdmin = TR.user.permissions.includes("handleDataRequests");
  if (!TR_BUILT.includes(TR.channel)) {
    const label = (TR_CHANNELS.find(c => c[0] === TR.channel) || [])[1];
    return h("div", null, trHeader(["Human oversight, consent ", "and audit"], ""), trFilters(),
      h("section", { class: "card" }, h("div", { class: "card-body empty" }, h("div", { class: "ico" }, icon("minus")), h("h3", null, label + " is not built in this workspace"),
        h("p", null, "There is no " + label + " activity to report. Its controls are listed as Not built in the register below."))), trRegister());
  }
  const ov = P.oversight, ev = P.evidence, gr = P.guardrails, cs = P.consent, sd = P.sending, dr = P.dataRights;
  const mix = ev.bantMix, mixTot = Object.values(mix).reduce((a, b) => a + b, 0);
  const wording = Object.entries(gr.wording);
  return h("div", null,
    trHeader(["Human oversight, consent ", "and audit"], "Every control with live numbers and the records behind them. Period: " + trRange() + "."),
    trFilters(),
    h("div", { class: "tr-tiles" }, r.tiles.map(trTile)),
    h("div", { class: "tr-grid" },
      trPanel("oversight", "Human oversight", "A person approves every send; edits and rejections are recorded.", ["T4"],
        h("div", { class: "tr-stats" }, trStat(ov.approved, "Drafts approved", "approvals", true), trStat(ov.rejected, "Rejected", "approvals"), trStat(ov.editedBeforeApproval, "Edited before approval", "approvals"),
          trStat(ov.awaiting, "Awaiting approval", "approvals")),
        h("div", { class: "tr-sec" }, h("h3", null, "Replies"), h("div", { class: "tr-kv" }, h("span", null, "Routed automatically by the AI"), h("b", null, ov.repliesByAi)),
          h("div", { class: "tr-kv" }, h("span", null, "Handed to a person"), h("b", null, ov.repliesByPerson)),
          h("div", { class: "tr-kv" }, h("span", null, "Median time from draft to approval"), h("b", null, ov.medianMinutesToApproval != null ? ov.medianMinutesToApproval + " min" : trNone()))),
        h("div", { class: "tr-sec" }, h("h3", null, "Drafts by engine"), Object.keys(ov.draftsByEngine).length ? Object.entries(ov.draftsByEngine).map(([k, v]) => h("div", { class: "tr-kv" }, h("span", null, k), h("b", null, v))) : h("p", { class: "muted small" }, trNone()))),
      trPanel("evidence", "Evidence", "BANT quotes must appear word for word in the buyer's reply.", ["T3"],
        h("div", { class: "tr-stats" }, trStat(ev.verified, "Quotes verified", "quotes", true), trStat(ev.dropped, "Quotes dropped", "quotes"), trStat(ev.downgradedLeads, "Leads downgraded", "quotes")),
        h("div", { class: "tr-sec" }, h("h3", null, "Buying-signal mix across leads"), mixTot ? [
          h("div", { class: "tr-bar", role: "img", "aria-label": `${mix.confirmed} confirmed, ${mix.inferred} inferred, ${mix.conflict} conflicting, ${mix.missing} missing` },
            [["confirmed", "#18c27f"], ["inferred", "#f2b84b"], ["conflict", "#e06b6b"], ["missing", "#dfe5e7"]].map(([k, c]) => h("span", { style: `width:${mix[k] / mixTot * 100}%;background:${c}` }))),
          h("div", { class: "tr-legend" }, [["Confirmed", "#18c27f", mix.confirmed], ["Inferred", "#f2b84b", mix.inferred], ["Conflict", "#e06b6b", mix.conflict], ["Missing", "#dfe5e7", mix.missing]].map(([l, c, v]) => h("span", null, h("i", { style: "background:" + c }), l + " " + v)))]
          : h("p", { class: "muted small" }, trNone()))),
      trPanel("guardrails", "Guardrails", "Wording checks before approval, hidden-instruction checks on replies, and what the model is sent.", ["T1", "T2", "A4", "S5"],
        h("div", { class: "tr-stats" }, trStat(wording.reduce((a, [, v]) => a + v.open, 0), "Open wording flags", "flags"), trStat(gr.injections.length, "Hidden instructions blocked", "flags"),
          trStat(gr.callsWithPhone, "Model calls with a phone number", "prompts")),
        h("div", { class: "tr-sec" }, h("h3", null, "Wording flags by reason"), wording.length ? wording.map(([, v]) => h("div", { class: "tr-kv" }, h("span", null, v.label),
          h("span", null, v.open ? trStatus("open") : null, " ", h("b", null, `${v.open} open · ${v.cleared} cleared · ${v.confirmed} confirmed`)))) : h("p", { class: "muted small" }, trNone())),
        gr.injections.length ? h("div", { class: "tr-sec" }, h("h3", null, "Hidden-instruction attempts"), gr.injections.slice(0, 3).map(i => h("div", { class: "tr-kv" },
          h("span", null, "“" + i.excerpt + "”"), h("button", { class: "tr-link", type: "button", onclick: () => trDrill("flags") }, "Open")))) : null,
        h("div", { class: "tr-sec" }, h("h3", null, "Fields sent to the model, per step"), Object.keys(gr.promptLog).length ? Object.entries(gr.promptLog).map(([k, v]) => h("div", { class: "tr-kv" },
          h("span", null, h("b", null, k.replace(/_/g, " ")), h("div", { class: "muted small" }, v.fields.join(", "))), h("b", null, v.calls + " calls"))) : h("p", { class: "muted small" }, "No model calls in period (the workspace ran on rules and templates)."),
          trNotBuilt("Ad copy wording check"))),
      trPanel("consent", "Consent & suppression", "The suppression list is checked at every send, not only at intake.", ["C1", "C3", "C5", "A3", "A5"],
        h("div", { class: "tr-stats" }, trStat(cs.suppressionSize, "On the suppression list", "suppressions"), trStat(cs.sendsBlocked, "Sends blocked by the list", "blocked"),
          trStat(cs.noConsentHeld, "Leads held: no consent", "suppressions")),
        h("div", { class: "tr-sec" }, h("h3", null, "Suppression list by reason"), Object.keys(cs.byReason).length ? Object.entries(cs.byReason).map(([k, v]) => h("div", { class: "tr-kv" }, h("span", null, k), h("b", null, v))) : h("p", { class: "muted small" }, "The list is empty.")),
        h("div", { class: "tr-sec" }, h("h3", null, "Consent by channel"), Object.entries(cs.byChannel).map(([k, v]) => h("div", { class: "tr-kv" }, h("span", null, k), h("b", null, `${v.granted} granted · ${v.notGranted} not granted · ${v.withdrawn} withdrawn`))),
          h("div", { class: "tr-kv" }, h("span", null, "SMS, WhatsApp and LinkedIn"), h("span", { class: "tr-tag", title: "Sends go only to controlled test recipients" }, "Test recipients only")),
          trNotBuilt("Ad lead forms"))),
      trPanel("sending", "Sending & channels", "Daily caps, campaign pauses and where prospects come from.", ["C2", "C4", "A1", "A2", "A6"],
        h("div", { class: "tr-kv" }, h("span", null, "Sent today vs daily cap"), h("b", null, sd.sentToday + " of " + sd.dailyCap)),
        h("div", { class: "tr-bar", role: "img", "aria-label": sd.sentToday + " of " + sd.dailyCap + " sent today" }, h("span", { style: `width:${Math.min(100, sd.sentToday / sd.dailyCap * 100)}%;background:var(--ink)` })),
        h("div", { class: "tr-kv" }, h("span", null, "Per-contact daily cap"), h("b", null, sd.perContactCap)),
        h("div", { class: "tr-kv" }, h("span", null, "Sent in period by channel"), h("button", { class: "tr-link", type: "button", onclick: () => trDrill("sends") },
          Object.entries(sd.sentByChannel || {}).map(([k, v]) => ({ email: "Email", sms: "SMS", whatsapp: "WhatsApp", linkedin: "LinkedIn" })[k] + " " + v).join(" · "))),
        h("div", { class: "tr-kv" }, h("span", null, "Sends held by the cap"), h("button", { class: "tr-link", type: "button", onclick: () => trDrill("blocked") }, String(sd.capBlocks))),
        h("div", { class: "tr-sec" }, h("h3", null, "Campaigns"), sd.campaigns.length ? sd.campaigns.map(c => h("div", { class: "tr-kv" },
          h("span", null, h("b", null, c.name), " ", trStatus(c.status), " ", trSim(), h("div", { class: "muted small" }, `${c.enrolled} enrolled · ${c.sentToday} sent today` + (c.blockedPaused ? ` · ${c.blockedPaused} held while paused` : ""))),
          h("button", { class: "btn sm ghost", type: "button", onclick: e => trDo(e.currentTarget, () => gapi(`/v1/governance/campaigns/${c.id}/${c.status === "paused" ? "resume" : "pause"}`, { method: "POST" }), c.status === "paused" ? "Campaign resumed" : "Campaign paused") },
            icon(c.status === "paused" ? "forward" : "pause", "sm"), c.status === "paused" ? "Resume" : "Pause"))) : h("p", { class: "muted small" }, "No campaigns yet.")),
        h("div", { class: "tr-sec" }, trNotBuilt("Sends deferred to business hours"), trNotBuilt("Ad spend vs budget"), trNotBuilt("Ad audience sync (opted-out people excluded)"))),
      trPanel("dataRights", "Data rights & retention", "Export and erase requests; inactive leads are anonymised after the retention period.", ["C6", "C7"],
        h("div", { class: "tr-stats" }, trStat(dr.open, "Open requests", canAdmin ? "requests" : null), trStat(dr.completed, "Completed", canAdmin ? "requests" : null),
          trStat(trDuration(dr.avgDaysToComplete), "Average time to complete", canAdmin ? "requests" : null), trStat(dr.anonymisedLeads, "Leads anonymised", null)),
        h("div", { class: "tr-sec" }, h("div", { class: "tr-kv" }, h("span", null, "Retention period"), h("b", null, dr.retention.days + " days")),
          h("div", { class: "tr-kv" }, h("span", null, "Next retention run"), h("b", null, dr.retention.nextRun ? fmtDate(dr.retention.nextRun) : "On next page load"))),
        canAdmin ? h("div", { class: "tr-sec", style: "display:flex;gap:8px;flex-wrap:wrap" },
          h("button", { class: "btn sm", type: "button", onclick: () => trDrill("requests") }, icon("usercheck", "sm"), "Export or erase…"),
          h("button", { class: "btn sm ghost", type: "button", onclick: () => trRetention(dr.retention.days) }, icon("clock", "sm"), "Change retention"))
          : h("p", { class: "muted small tr-sec" }, "Export, erase and retention changes are for the Portal Admin."))),
    trPanel("audit", "Audit log", "Latest 20 entries. Append-only: the database refuses edits and deletes.", ["S1"],
      trAuditList(P.audit), h("div", { style: "margin-top:12px" }, h("button", { class: "btn sm ghost", type: "button", onclick: () => trDrill("audit") }, "View all entries"))),
    trRegister(),
    h("div", { class: "tr-foot" }, h("span", null, "Figures computed on the server from the workspace store. All prospect data is synthetic."), h("span", null, "Period " + trRange())));
}

function trRegister() {
  const reg = TR.report.register, rows = reg.rows.filter(c => !TR.area || c.area === TR.area);
  const cls = s => s === "Live" ? "" : s === "Partial" ? "partial" : "nb";
  return h("section", { class: "card tr-full", "aria-labelledby": "tr-reg" },
    h("div", { class: "card-head" }, h("div", null, h("h2", { id: "tr-reg" }, "Controls register"), h("p", { class: "muted small", style: "margin:4px 0 0" }, reg.rows.length + " controls · status is set in code; evidence is live.")),
      reg.source === "provisional" ? h("span", { class: "tr-tag", title: "Replace with the agreed main-tab list" }, "Provisional list") : null),
    h("div", { class: "card-body" },
      h("div", { class: "tr-cats" }, TR_AREAS.map(a => { const cs = reg.rows.filter(c => c.area === a), live = cs.filter(c => c.status === "Live").length;
        return h("button", { class: "tr-cat", type: "button", "aria-pressed": String(TR.area === a), onclick: () => { TR.area = TR.area === a ? "" : a; render(); }, "aria-label": `${a}: ${live} of ${cs.length} live. Filter register` },
          h("div", { class: "top" }, h("b", null, a), h("span", null, h("b", null, live), "/" + cs.length + " live")), h("div", { class: "tr-dots", "aria-hidden": "true" }, cs.map(c => h("i", { class: cls(c.status), title: c.id + " " + c.status })))); })),
      h("p", { class: "tr-legend", style: "margin:0 0 6px" }, h("span", null, h("i", { style: "background:#18c27f" }), "Live"), h("span", null, h("i", { style: "background:#f2b84b" }), "Partial"), h("span", null, h("i", { style: "background:#dfe5e7" }), "Not built")),
      h("table", { class: "tr-table cards" }, h("thead", null, h("tr", null, ["ID", "Control", "Area", "Priority", "Status", "Evidence", ""].map(x => h("th", { scope: "col" }, x)))),
        h("tbody", null, rows.map(c => h("tr", null, h("td", { class: "idc", "data-l": "ID" }, c.id), h("td", null, h("b", null, c.name), c.note ? h("span", { class: "note" }, c.note) : null),
          h("td", { "data-l": "Area" }, c.area), h("td", { "data-l": "Priority" }, c.priority), h("td", null, trStatus(c.status)),
          h("td", { "data-l": "Evidence" }, c.drill && c.status !== "Not built" ? h("button", { class: "tr-link", type: "button", onclick: () => trDrill(c.drill) }, c.evidence) : h("span", { class: "tr-nolink" }, c.evidence)),
          h("td", null, c.simulated ? trSim() : null)))))));
}

function viewTrust() {
  if (TR.user === undefined) return h("p", { class: "muted" }, "Loading…");
  if (!TR.user) return trSignIn();
  if (TR.err) return h("div", { class: "card" }, h("div", { class: "card-body empty" }, h("h3", null, TR.err === "forbidden" ? "Your role can't open this page" : "The governance report couldn't load"), h("p", null, trErr({ code: TR.err }))));
  return TR.summary ? trSdr() : TR.report ? trFull() : h("p", { class: "muted" }, "Loading…");
}

// ---------- drill-down ----------
const TR_TITLES = { sends: "Emails sent", approvals: "Drafts and approvals", quotes: "BANT quotes", optouts: "Opt-outs", flags: "Guardrail flags", audit: "Audit log",
  requests: "Export and erase requests", suppressions: "Suppression list", blocked: "Blocked sends", prompts: "Model calls (field names only)" };
const leadCell = (id, name) => id ? h("a", { href: "#/leads/" + id, onclick: closeModal }, name || "Lead") : (name || "–");
const TR_COLS = {
  sends: [["When", r => fmtDate(r.at)], ["Lead", r => leadCell(r.leadId, r.lead)], ["Channel", r => r.channel], ["Subject", r => r.subject], ["Approved by", r => r.approvedBy ? trWho(r.approvedBy) : h("b", { class: "tr-err" }, "Not approved")], ["Approved", r => fmtDate(r.approvedAt)]],
  approvals: [["Created", r => fmtDate(r.at)], ["Lead", r => leadCell(r.leadId, r.lead)], ["Subject", r => r.subject], ["Status", r => r.status.toLowerCase().replace("_", " ")], ["Version", r => r.version], ["Approved by", r => r.approvedBy ? trWho(r.approvedBy) : "–"], ["Engine", r => r.engine]],
  quotes: [["Lead", r => leadCell(r.leadId, r.lead)], ["Signal", r => r.field], ["Quote", r => "“" + r.excerpt + "”"], ["Result", r => r.result === "verified" ? h("span", { class: "tr-status live" }, icon("check"), "Verified") : h("span", { class: "tr-status partial" }, icon("alert"), "Dropped: " + (r.reason || "not in reply"))]],
  optouts: [["Lead", r => leadCell(r.leadId, r.name)], ["Opted out", r => fmtDate(r.at)], ["Reason", r => r.reason], ["Emails sent afterwards", r => h("b", { class: r.sentAfter ? "tr-err" : null }, r.sentAfter)], ["Sends blocked afterwards", r => r.blockedAfter]],
  audit: [["When", r => fmtDate(r.at)], ["Person", r => trWho(r.actor)], ["Role", r => (r.role || "").replace(/_/g, " ")], ["Action", r => TR_ACTIONS[r.action] || r.action], ["Record", r => r.lead_id ? leadCell(r.lead_id, r.record_type) : [r.record_type, r.record_id].filter(Boolean).join(" ")],
    ["Change", r => trChange(r).slice(0, 300)]],
  blocked: [["When", r => fmtDate(r.at)], ["Lead", r => leadCell(r.lead_id, "lead")], ["Reason", r => TR_BLOCK[r.detail] || r.detail], ["By", r => trWho(r.actor)]],
  prompts: [["When", r => fmtDate(r.at)], ["Step", r => r.step], ["Model", r => r.model], ["Fields sent", r => r.fields.join(", ")]],
};

async function trDrill(kind) {
  if (!kind) return;
  let data;
  try { data = await gapi(`/v1/governance/records/${kind}?` + trQuery()); }
  catch (e) { toast(trErr(e), "err"); return; }
  const rows = data.rows || [];
  const body = h("div", { class: "modal-body" });
  const search = h("input", { class: "field tr-search", type: "search", placeholder: "Search these records", "aria-label": "Search records" });
  const paint = () => {
    const q = search.value.trim().toLowerCase();
    const list = q ? rows.filter(r => JSON.stringify(r).toLowerCase().includes(q)) : rows;
    const extra = kind === "suppressions" ? trSuppressionForm() : kind === "requests" ? trRequestForm() : null;
    let content;
    if (!list.length) content = h("p", { class: "muted" }, (q ? "No records match." : trNone()) + " (" + trRange() + ")");
    else if (kind === "flags") content = list.map(trFlagCard);
    else if (kind === "suppressions") content = trTable([["Entry", r => r.value], ["Type", r => r.category], ["Reason", r => r.reason], ["Added", r => fmtDate(r.at)],
      ["", r => r.reason.startsWith("manual") ? h("button", { class: "btn sm danger", type: "button", onclick: e => trDo(e.currentTarget, () => gapi("/v1/governance/suppressions/" + encodeURIComponent(r.id), { method: "DELETE" }), "Entry removed").then(() => trDrill(kind)).catch(() => {}) }, "Remove") : h("span", { class: "muted small" }, "Protected")]], list);
    else if (kind === "requests") content = trTable([["Kind", r => r.kind], ["Requested", r => fmtDate(r.requested_at)], ["By", r => trWho(r.requested_by)], ["Completed", r => r.completed_at ? fmtDate(r.completed_at) : trStatus("open")], ["Outcome", r => r.outcome || "–"],
      ["", r => r.completed_at ? null : h("button", { class: "btn sm", type: "button", onclick: () => trCompleteRequest(r) }, "Run")]], list);
    else content = trTable(TR_COLS[kind], list);
    body.replaceChildren(...[extra, rows.length > 6 ? search : null, h("p", { class: "muted small", style: "margin:0 0 10px" }, list.length + " record" + (list.length === 1 ? "" : "s") + " · " + trRange())].filter(Boolean), ...[].concat(content));
    hydrateIcons(body);
  };
  search.addEventListener("input", paint);
  paint();
  openModal(h("div", { class: "tr-drill" }, h("div", { class: "modal-head" }, h("h2", { id: "modal-title" }, TR_TITLES[kind] || kind), h("button", { class: "btn quiet x", type: "button", "aria-label": "Close", onclick: closeModal }, icon("x"))), body));
}
function trTable(cols, rows) {
  return h("table", { class: "tr-table cards" }, h("thead", null, h("tr", null, cols.map(([l]) => h("th", { scope: "col" }, l)))),
    h("tbody", null, rows.map(r => h("tr", null, cols.map(([l, f]) => { const v = f(r); return h("td", { "data-l": l || null }, v == null || v === "" ? "–" : v); })))));
}
function trFlagCard(f) {
  const can = TR.user.permissions.includes("manageGuardrails") && f.status === "open";
  const reason = h("input", { class: "field", placeholder: "One-line reason (required)", "aria-label": "Reason for " + f.label, maxlength: 300 });
  const err = h("p", { class: "tr-err", role: "alert", hidden: true });
  const go = async (status, btn) => {
    if (!reason.value.trim()) { err.textContent = TR_ERR.reason_required; err.hidden = false; reason.focus(); return; }
    try { await trDo(btn, () => gapi(`/v1/governance/flags/${encodeURIComponent(f.id)}/${status}`, { method: "POST", body: { reason: reason.value.trim() } }), status === "clear" ? "Flag cleared" : "Flag confirmed"); trDrill("flags"); } catch (_) {}
  };
  reason.addEventListener("input", () => { err.hidden = true; });
  return h("div", { class: "tr-flag" },
    h("div", { class: "top" }, h("span", null, h("b", null, f.label), " · ", leadCell(f.lead_id, f.lead), f.draftStatus ? h("span", { class: "muted small" }, " · draft " + f.draftStatus.toLowerCase()) : null), trStatus(f.status)),
    f.subject ? h("div", { class: "small", style: "margin-top:6px" }, h("b", null, "Subject: "), trMark(f.subject, f.highlights)) : null,
    h("pre", null, trMark(f.text || f.excerpt, f.highlights)),
    f.note ? h("p", { class: "muted small" }, (f.status === "superseded" ? "" : (f.status + " by " + trWho(f.cleared_by) + ": ")) + f.note) : null,
    can ? h("div", { class: "acts" }, reason, h("button", { class: "btn sm ghost", type: "button", onclick: e => go("clear", e.currentTarget) }, icon("check", "sm"), "Clear (false positive)"),
      h("button", { class: "btn sm", type: "button", onclick: e => go("confirm", e.currentTarget) }, icon("alert", "sm"), "Confirm")) : null, can ? err : null);
}
function trSuppressionForm() {
  const value = h("input", { class: "field", placeholder: "email@bank.example or @bank.example", "aria-label": "Email or domain" });
  const why = h("input", { class: "field", placeholder: "Reason", "aria-label": "Reason" });
  return h("form", { class: "tr-flag", onsubmit: e => { e.preventDefault(); trDo(e.submitter, () => gapi("/v1/governance/suppressions", { method: "POST", body: { value: value.value, reason: why.value } }), "Added to the suppression list").then(() => trDrill("suppressions")).catch(() => {}); } },
    h("b", null, "Add an entry"), h("div", { class: "acts", style: "margin-top:8px" }, value, why, h("button", { class: "btn sm", type: "submit" }, icon("plus", "sm"), "Add")));
}
function trRequestForm() {
  const kind = h("select", { class: "field", "aria-label": "Request type", style: "max-width:140px" }, h("option", { value: "export" }, "Export"), h("option", { value: "erase" }, "Erase"));
  const email = h("input", { class: "field", type: "email", placeholder: "Subject's email address", "aria-label": "Subject email" });
  return h("form", { class: "tr-flag", onsubmit: e => { e.preventDefault(); trDo(e.submitter, () => gapi("/v1/governance/data-requests", { method: "POST", body: { kind: kind.value, email: email.value } }), r => `Request started · ${r.matchingLeads} matching lead record(s)`).then(() => trDrill("requests")).catch(() => {}); } },
    h("b", null, "Start a request"), h("p", { class: "muted small", style: "margin:4px 0 0" }, "Only a hash of the email is stored. Re-enter it to run the request."),
    h("div", { class: "acts", style: "margin-top:8px" }, kind, email, h("button", { class: "btn sm", type: "submit" }, "Start")));
}
function trCompleteRequest(r) {
  const email = h("input", { class: "field", type: "email", "aria-label": "Subject email", placeholder: "Subject's email address" });
  const run = async btn => {
    try {
      if (r.kind === "export") await trDo(btn, () => trDownload(`/v1/governance/data-requests/${r.id}/complete`, "export.json", { method: "POST", body: { kind: r.kind, email: email.value } }), n => "Export downloaded: " + n);
      else await trDo(btn, () => gapi(`/v1/governance/data-requests/${r.id}/complete`, { method: "POST", body: { kind: r.kind, email: email.value } }), x => "Done: " + x.outcome);
      trDrill("requests");
    } catch (_) {}
  };
  openModal(h("div", null, h("div", { class: "modal-head" }, h("h2", { id: "modal-title" }, (r.kind === "erase" ? "Erase" : "Export") + " personal data"), h("button", { class: "btn quiet x", type: "button", "aria-label": "Close", onclick: closeModal }, icon("x"))),
    h("div", { class: "modal-body" }, h("p", null, r.kind === "erase" ? "Anonymises every lead with this email. Business facts stay; the opt-out is kept as a hash so it keeps working. This cannot be undone." : "Downloads everything held about this person as one JSON file."),
      h("label", { class: "fld" }, h("span", null, "Email address"), email)),
    h("div", { class: "modal-foot" }, h("button", { class: "btn ghost", type: "button", onclick: () => trDrill("requests") }, "Back"), h("button", { class: "btn" + (r.kind === "erase" ? " danger" : ""), type: "button", onclick: e => run(e.currentTarget) }, r.kind === "erase" ? "Erase" : "Export"))));
}
function trRetention(days) {
  const input = h("input", { class: "field", type: "number", min: 30, max: 3650, value: days, "aria-label": "Retention period in days" });
  openModal(h("div", null, h("div", { class: "modal-head" }, h("h2", { id: "modal-title" }, "Retention period"), h("button", { class: "btn quiet x", type: "button", "aria-label": "Close", onclick: closeModal }, icon("x"))),
    h("div", { class: "modal-body" }, h("p", null, "Leads with no activity for this many days are anonymised by the daily retention run."), h("label", { class: "fld" }, h("span", null, "Days (30–3650)"), input)),
    h("div", { class: "modal-foot" }, h("button", { class: "btn ghost", type: "button", onclick: e => trDo(e.currentTarget, () => gapi("/v1/governance/retention/run", { method: "POST" }), x => `Retention ran · ${x.anonymised} anonymised`).then(closeModal).catch(() => {}) }, "Run now"),
      h("button", { class: "btn", type: "button", onclick: e => trDo(e.currentTarget, () => gapi("/v1/governance/retention", { method: "PUT", body: { days: Number(input.value) } }), "Retention period saved").then(closeModal).catch(() => {}) }, "Save"))));
}
