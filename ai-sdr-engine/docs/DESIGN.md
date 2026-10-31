# Azentio Sales Workspace — Design System

How the AI SDR's surfaces look, read and behave: the **Sales Workspace** (`/workspace`, `app/static/console.html`) that SDRs,
AEs and sales ops use, and the public pages prospects see (`/contact`, `/trust`). Brand tokens come from azentio.com's own
theme CSS (captured 31 Oct 2026). Layout, components and voice are ours. The reference for density and calm is the
"compliance dashboard" family (white cards on a soft background, small-caps table headers, dot meters, donut summaries).

All workspace styles live in the single `<style>` block at the top of `console.html`; tokens are CSS custom properties on
`:root`. Components are plain class names rendered by small JS helpers (`pill`, `dotMeter`, `donut`, `cardHead`,
`sectionTitle`, `emptyCard`, `toast`, `confirmDialog`).

---

## 1. Principles

1. **Azentio, not "an AI tool".** Same logo, type, colours and pill buttons as azentio.com. No purple gradients, robots or sparkle-everything. The mint accent is used sparingly for the one thing that matters on screen.
2. **Plain business language.** Users see *what happened and what to do next*, never how the system works. No model, provider, vendor, API, endpoint, ID, environment, version, token-count or "demo / mock / simulated / test" wording anywhere.
3. **Nothing ever looks broken.** There is no error state in the UI. A failed load keeps the last good data or shows a calm placeholder; a failed action shows a neutral note with what happens next (§9).
4. **One next step.** Every lead opens with a single "Next step" banner and one primary action. Secondary actions are quieter (ghost / quiet buttons).
5. **Show the evidence.** Every qualification claim is backed by the buyer's own words in a quote, labelled with where it came from ("In their reply", "From their enquiry").
6. **A human approves everything that leaves.** Every message is reviewed; scheduled sends say when; held drafts say why; in-flight sends say what they're waiting for.
7. **Calm density.** Many small, well-labelled facts beat a few big ones. Group with cards and whitespace, not colour.

---

## 2. Tokens

### 2.1 Colour

| Token | Value | Use |
|---|---|---|
| `--ink` | `#16303d` | Primary text, primary buttons, active nav pill, dark banners/tiles |
| `--ink-2` | `#2b4654` | Hover on ink, secondary text on white, neutral bars |
| `--ink-soft` | `#e8eef0` | Neutral badge fill, avatar fill, hover tint |
| `--mint` | `#55ffbe` | Brand accent: CTA button, icon on ink, "hot" counts, title underline |
| `--mint-soft` / `--mint-ink` | `#ddfff2` / `#0b6b47` | Positive states: Confirmed, Qualified, Approved, Delivered |
| `--signal` | `#18c27f` | Data colour for "good" (dot meters, bars, donut segments) |
| `--paper` | `#f5f7f7` | Page background |
| `--white` / `--surface-2` | `#fff` / `#fafbfb` | Cards / card headers, read-only fields |
| `--line` / `--line-strong` | `#dde5e7` / `#c6d2d5` | Card borders and dividers / input borders |
| `--muted` | `#5b6d75` | Secondary text (5.4:1 on white, 5.0:1 on paper) |
| Amber `--amber-soft` / `--amber-ink` / `--amber` | `#fff3dc` / `#8a5300` / `#f2b84b` | Needs your approval, Likely, Needs attention |
| Blue `--blue-soft` / `--blue-ink` / `--blue` | `#e7f0fb` / `#1d4f8c` / `#5b8fd1` | In conversation, in-progress sends, specialist review, info toasts |
| Red `--red-soft` / `--red-ink` | `#fde8e8` / `#a42727` | **Only** destructive buttons (Discard, Delete) and inline field validation. Never for system state. |
| `--grey-soft` | `#eef1f2` | Closed / unknown / not yet |

Rules: semantic tokens only (no raw hex in components except chart segment colours, which use the data colours above).
Colour never carries meaning alone — every pill has a word, every meter has a number and a label, every BANT tile has an icon **and** a label.

### 2.2 Typography

Poppins 400 / 500 / 600, self-hosted from `app/static/assets/fonts/` with `font-display: swap`; fallback `system-ui`.

| Role | Token | Size / weight / line-height | Use |
|---|---|---|---|
| Display | `--t-3xl` | 30 / 600 / 1.2 | Reserved (marketing pages) |
| Page title | `--t-2xl` | 24 / 600 / 1.25 (22 on mobile) | One `h1` per view, followed by a count badge when the page is a list |
| Large title | `--t-xl` | 20 / 600 | Lead / person name in a profile header |
| Section | `--t-lg` | 17 / 600 | `sectionTitle()` above groups of cards |
| Card title | `--t-md` | 15 / 600 | `h2` in `cardHead()` |
| Body | `--t-base` | 14 / 400 / 1.55 | Default text |
| Meta | `--t-sm` | 12.5 / 400 | Secondary lines, help text, table cells (13) |
| Label | `--t-xs` | 11 / 500, uppercase, 0.7–1.6px tracking | Table headers, nav group labels, banner labels |

Numbers in tables, tiles, meters and counts use `font-variant-numeric: tabular-nums`. Inputs are 16px on mobile to avoid iOS zoom.
Long-form text is capped at ~680px (`.lede`, summaries).

### 2.3 Spacing

4-pt scale: `--s-1` 4 · `--s-2` 8 · `--s-3` 12 · `--s-4` 16 · `--s-5` 20 · `--s-6` 24 · `--s-8` 32 · `--s-10` 40 · `--s-12` 48.
Card padding 20 (16 on mobile). Gap between cards 16. Section spacing 32 above a section title. Page padding 28 (14 on mobile).

### 2.4 Shape, elevation, layers, motion

| | Value |
|---|---|
| Radius | `--radius-xs` 6 (fit tags) · `--radius-sm` 8 (inner tiles) · `--radius` 12 (cards, toasts) · `--radius-lg` 16 (modals) · `--pill` 999 (buttons, badges, inputs in toolbars) |
| Elevation | `--shadow` (resting card) · `--shadow-2` (hover card, menus, toasts) · `--shadow-3` (modal, mobile drawer) |
| Z-index | sticky top bar 30 · sidebar 60 · menus 70 · modal scrim 80 · toasts 90 · skip link 100 |
| Motion | `--dur-1` 120ms (hover/press) · `--dur-2` 180ms (menus, toasts) · `--dur-3` 240ms (modals, drawer) · `--ease` `cubic-bezier(.2,.8,.2,1)` · transform/opacity only · all animation off under `prefers-reduced-motion` |
| Touch | `--tap` 40px / `--tap-sm` 36px on fine pointers; both become **44px** on coarse pointers and below 768px |

### 2.5 Iconography

Inline SVG, Lucide paths, 1.75 stroke, round caps. Sizes: 15px (`.sm`, inside buttons/pills), 18px (default, nav), 22px (`.lg`).
Icons are `aria-hidden`; icon-only buttons always have an `aria-label`. No emoji as icons. The ICONS map in `console.html`
is the only icon source.

---

## 3. Layout

### 3.1 App shell

```
┌──────────┬─────────────────────────────────────────────┐
│ Sidebar  │ Top bar: breadcrumb · Live · [Add lead]     │
│ 248px    ├─────────────────────────────────────────────┤
│ logo     │ main (max 1480px, 28px padding)             │
│ nav      │   page head: h1 + count · lede · actions    │
│ groups   │   tiles / tabs / filters                    │
│ ──────   │   cards                                     │
│ Workspace│                                             │
│ User     │                                             │
└──────────┴─────────────────────────────────────────────┘
```

- **Sidebar** (white, right border): logo, "Sales Workspace" label, nav in groups — *Home*; **Pipeline**: Leads, Accounts, Opportunities; **Outreach**: Approvals, Campaigns, AI Studio; **Insights**: Performance. Active item = ink pill with mint icon (`aria-current="page"`). Counts: Leads (needs attention), Opportunities (mint, waiting), Approvals. Footer: Workspace menu (date, move ahead a day, public form, fresh workspace) and the "viewing as" user switcher; both open upward.
- **Rail** (768–1199px by default, or on desktop via the collapse button; choice saved in `localStorage["nav.rail"]`): 76px, icons only with tooltips, counts become corner badges, group labels become dividers.
- **Drawer** (<768px): sidebar slides in over a 48% scrim from a hamburger in the top bar; Escape, scrim click or the close button dismiss it and focus returns to the hamburger. Hidden drawer is `visibility: hidden` so it is out of the tab order.
- **Top bar** (sticky, translucent paper): breadcrumb `Group / Page / Item` (desktop), Live / Reconnecting status, primary "Add lead". On mobile: hamburger · logo · Add lead.

### 3.2 Breakpoints

| Width | Behaviour |
|---|---|
| ≥1600 | Leads: list · detail · activity rail (3 panes) |
| 1200–1599 | Full sidebar; leads list · detail; two-column account/campaign layouts |
| 1100–1199 | Rail sidebar; leads list · detail |
| 768–1099 | Rail sidebar; leads list **or** detail (detail has an "All leads" back link); single-column page layouts |
| <768 | Drawer nav; single column; tiles 2-up; forms 1-column; buttons in message footers go full width |

No page scrolls horizontally at 375px; wide tables sit in `.table-scroll`.

### 3.3 Page templates

- **List page** (Accounts, Approvals, Opportunities, Campaigns): page head (h1 + count badge, one-line lede, page actions) → stat tiles → filter chips → cards/table.
- **Split page** (Leads): sticky list pane (search, source select, status chips with live counts) + detail.
- **Record page** (Lead, Account, Person): back link → profile header card (avatar, name, sub-line, chips; owner block on the right) → status banner (Next step / Reason to reach out / research bar) → underline tabs where needed → sections.
- **Dashboard** (Home, Performance): hero/title → tiles → donut + legend cards → tables.

---

## 4. Components

| Component | Class / helper | Spec |
|---|---|---|
| **Button** | `.btn` + `mint` / `ghost` / `quiet` / `danger`, `.sm`, `.icon`, `.block` | Pill, 13px/500, min-height `--tap`. Primary = ink; CTA = mint (one per view or card); ghost = white + strong border; quiet = text; danger = red text, only for Discard/Delete. Busy: spinner + `aria-busy` + disabled. Press: scale .98. |
| **Pill / badge** | `.badge` (+ tone), `pill(label, tone, icon?)`, `.badge.dot` | 22px tall, 11.5px/500. Status pills have a leading 6px dot in the tone's data colour. Tones: mint, amber, blue, ink, grey, outline (red reserved). |
| **Fit tag** | `.fit.High/.Medium/.Low` | 6px radius, 600 weight, "High fit · 80". |
| **Count** | `.count` (`.hot` = mint) | 20px pill, 11px/600, next to titles and nav items. |
| **Dot meter** | `dotMeter(n, total, label, tone)` | Row of 8px dots (filled = `--signal`, or ink/amber) + "**3/4** confirmed". Always has a word. `role="img"` with a full sentence label. Used for buying signals, fit checks passing, campaign replies, accounts with opportunities. |
| **Progress bar** | `.bar` (`.mint`) | 6px track, ink or signal fill, `role="img"` with value. |
| **Donut** | `donut(segments, {size, stroke, center, sub, label})` + `legendRows()` | SVG ring, 2px gaps between segments, centre figure + caption, legend with counts. ≤5 segments. `role="img"` with a summary sentence. |
| **Card** | `.card`, `cardHead(title, count, viewAllHref | node)`, `.card-body`, `.card-foot` | White, 1px line border, 12px radius, resting shadow. Header: title + count badge on the left, "View all ›" or a meter/badge on the right. |
| **Section title** | `sectionTitle(title, count, ...actions)` | 17/600 + count, actions pushed right. |
| **Stat tile** | `.tiles > .card.tile` (`.dark` for the one to look at) | Label with icon, 28px tabular value, caption or `.delta` pill (▲/▼ + value, mint/red tint, "No change"). |
| **Table** | `table.list` in `.table-scroll` | Headers 11px uppercase muted with 0.7px tracking; 12px cell padding (20px at card edges); row hover `--surface-2`; clickable rows are keyboard-focusable; numbers right-aligned. |
| **Tabs** | `.utabs` (buttons with `role="tab"`/`aria-selected`, or links with `aria-current`) | 44px tall, 2px ink underline on the active tab, optional count. Used for lead Overview/Agent activity, Performance/Pipeline health, Add-lead sources. |
| **Filter chips** | `.segs > .seg` (`aria-pressed`) with `.n` count | Pills; selected = ink. Wrap on narrow widths. |
| **Segmented control** | `.seg-box > .seg` | Inset pill group for periods (7/14/30 days) and small view switches. |
| **Form field** | `.fld` + `input/select/textarea.field`, `.help`, `.req`, `.ferr` | Visible label above, 10px radius, ink focus ring (3px halo). Required marked with `*`. Validation text in `.ferr` (red, `role="alert"`) right below the form or field; focus moves to the first problem. |
| **Message composer** | `.card.email` (`.email-top`, `.status-line`, `.notice`, fields, textarea, `.email-foot`) | Header: channel icon, kind, status pill, "Written in" style tag, unsaved badge. Blue status line for in-flight sends (draft `nextAction`). Footer: counter/copy left; Discard, Save, primary right. |
| **Banner** | `.next` (ink) / `.next.calm` (white) / `.reason` (mint) / `.notice.grey/.blue/.amber/.mint` | One per context. Ink "Next step" for action needed; calm white when we're waiting; amber only for content warnings (e.g. a draft mentions pricing). |
| **Empty state** | `emptyCard(icon, title, text)`, `.empty.compact` | Mint-soft icon circle, 15px title, one helpful sentence, optional action. |
| **Skeleton** | `.skel`, `.skel-stack` | Shimmer blocks shaped like the content; `aria-busy` on the container. |
| **Toast** | `toast(msg, "ok" | "info")` | Bottom-right (full width on mobile), max 3. `ok` = ink with mint check (4.5s); `info` = white with blue info icon (7s). Dismiss button. `role="status"`, polite live region, never steals focus. There is no error toast. |
| **Modal** | `openModal(node, "sm"?)`, `confirmDialog(title, text, okLabel, danger)` | 48% scrim, 16px radius, focus moves in, Tab is trapped, Escape/scrim/close button dismiss, focus returns to the opener. Native `confirm()` is never used. |
| **Avatar** | `.av` (`.sm` 30, default 40, `.lg` 56 ink/mint, `.xl` 64 rounded square for companies), `.who-chip` 24 | Initials only. |
| **Menu** | `.menu` + `wireMenu(btn, menu)` | Opens upward from the sidebar footer, 44px items, Escape closes and returns focus. |

---

## 5. Pages

| Page | Route | Layout and key elements |
|---|---|---|
| **Home** | `#/home` | Greeting + date + "N things need you today" → assistant card (question box, suggestion chips, answers with linked lead names) → **Your day**: 4 cards (Tasks, To approve, New replies, Waiting on a reply) each with a count and "View all" footer → **Pipeline at a glance**: leads-by-status donut + 8 KPI tiles with change pills, period switch. |
| **Leads** | `#/leads`, `#/leads/<id>` | List pane (search, source, status chips with counts; rows with stage pill, "N/4 signals" meter, fit, owner chip, approval dot) + detail: profile header with owner select → tabs (Overview · Agent activity) → Next step banner → Handoff → Messages to review (email / LinkedIn / text cards) → writing style → Qualification (Buying signals with quotes · Fit with "N/5 passing") → Conversation (+ draft LinkedIn note / text, add a reply) → Company & contact → Campaign. |
| **Accounts** | `#/accounts` | Tiles (accounts, with open opportunities, profiles ready) → table (company, contacts, opportunities pill, profile pill) + sticky "Research a company" form. |
| **Account** | `#/account/<domain>` | Hero (logo tile, site, chips, summary, owner) → research bar (date, sources, refresh) → Fit per product cards → Why now signals timeline → priorities + talking points → people table → news; right column: key facts, leadership, opportunities & campaigns, activity, sources. The seller's own company shows a calm note instead of fit. |
| **Person** | `#/person/<id>` | Profile header → Reason to reach out → Next best action → Buying signals + Fit → Engagement timeline → Public profile research. |
| **Approvals** | `#/approvals[?c=<campaign>]` | Tiles (needs approval, specialist review, ready, scheduled) → source chips (All, Replies to enquiries, each campaign) and a channel switch (Email, LinkedIn, WhatsApp, Text), all with counts → queue cards (avatar, name, channel pill, campaign pill, status pill, subject, two-line preview, in-flight status) with Review / Approve / Send. Bulk actions in the page head. |
| **Opportunities** | `#/opportunities` | Tiles → "Ready to accept" cards (qualification donut, summary, routing reason, engagement, four signals with quotes and a meter, owner, Accept / Send back) → Accepted. |
| **Campaigns** | `#/campaigns` | Campaign cards (status pill, stats, channel panel for Email / LinkedIn / WhatsApp / Text, reply progress, exclusions, Review emails / See contacts / Generate replies) + sticky "New campaign" form with templates and inline validation. |
| **Performance** | `#/performance`, `#/insights` | Underline tabs: Outreach performance (funnel tiles, inbound vs outbound table, campaign table, agent-vs-human donut, decision log) · Pipeline health (tiles, conversion funnel, status donut, ready-for-follow-up table, buying-signal coverage, sources). |
| **AI Studio** | `#/studio` | Writing style cards (default pill, instructions, channels, Edit / Try it / Set as default / Delete) → "Try a style on a real lead" side-by-side preview. Upload and editor in modals. |

---

## 6. Voice & tone

- **Who we write for:** a busy SDR or AE. Short sentences, present tense, second person, outcome first.
- **Say what happens next.** "Queued. It sends automatically as soon as they accept your LinkedIn connection request." beats "Status: pending".
- **Calm, never alarming.** No "failed", "error", "invalid", "something went wrong", exclamation marks or blame.
- **Vocabulary map** (system value → what users read):

| System value | Shown as |
|---|---|
| `ENGAGING` / `QUALIFYING` | In conversation / Reviewing reply |
| `REP_REVIEW` / `OPPORTUNITY` | Qualified / Opportunity |
| `HUMAN_REVIEW` / `NURTURE` | Needs attention / Nurture |
| `SUPPRESSED` / `CLOSED` / `QUARANTINED` | Opted out / Not a fit / Identity check |
| BANT `confirmed / inferred / missing / conflict` | Confirmed / Likely / Unknown / Unclear |
| Budget, Authority, Need, Timeline | Budget, Decision maker, Need, Timeline ("buying signals") |
| ICP checks | "Fit for <product>": Market, Institution, Role, Size, Interest — each a sentence |
| Draft `BLOCKED` | Specialist review (blue, shield icon) |
| Draft `APPROVED` + in-flight `nextAction` | Approved · in progress + the sentence in a blue status line |
| Company-data provider / web research / LinkedIn automation / messaging provider | company records / public research / LinkedIn / WhatsApp or SMS |
| Agent step modes | AI · Rule · Lookup · System |

Server sentences pass through `clean()` (scrubs vendor names, internal tokens, snake_case, "demo/mock/simulated") and
`safeMsg()` (drops a sentence entirely if it still looks technical). Source links to internal tools are never shown.

### Empty states

Title says what will appear; body says how it gets there. "No campaigns yet — Pick a template to reach the right people with a three-email sequence."

---

## 7. Never-show-errors rules

1. `api()` failures become a code; `errText()` turns every code into a calm sentence with the next step (e.g. *"The email hasn't gone out yet. It's still approved, so you can send it again in a moment."*). Unknown codes: *"That didn't go through just now. Nothing was lost, so you can try again in a moment."*
2. `act()` shows that sentence as an **info** toast, keeps the page data, and refreshes if the item changed underneath.
3. Background loads (`loadCore`, `grab`, `loadDetail`) keep the last good value on failure; the top bar switches from **Live** to **Reconnecting…**. With no data yet, views show skeletons, then a calm placeholder ("Your day is on its way… This page refreshes on its own").
4. Accepted-for-later actions (`linkedin-launch`, `send-sms` returning 202 `{status, text}`) show `text` via `outcome()` and the draft card shows its `nextAction` sentence until the send is confirmed.
5. A view that throws while rendering is replaced by a calm "Getting this page ready" card; nothing technical reaches the screen.
6. Validation (user input) is the only place red text appears, right under the form, with focus on the field.

---

## 8. Accessibility

- Contrast ≥4.5:1 for text (muted `#5b6d75` on white and paper passes); data colours ≥3:1 against their track.
- Visible 2px ink focus ring on every interactive element (`:focus-visible`); skip link; one `h1` per view; breadcrumb with `aria-current`.
- Keyboard: tabs, chips, menus, dialogs and clickable table rows all work with Tab/Enter/Escape; dialogs trap focus and restore it.
- Touch targets ≥44px on touch devices and phones; 8px+ between targets.
- Status is never colour-only (pills have words, meters have numbers, BANT tiles have icons and labels; charts have text labels and an `aria-label` summary).
- `aria-live="polite"` toasts and assistant answers; `aria-busy` on loading regions; `prefers-reduced-motion` removes all animation.
- All dynamic content is rendered with `textContent` (no HTML injection from leads, replies or research).

---

## 9. Public enquiry form (`/contact`) and trust page (`/trust`)

Same brand tokens and Poppins. White header with the Azentio logo, two-column hero ("Let's move your business **forward.**"
with mint underline) and a line-style form card. Fields: first/last name, business email, job title (optional), company,
country, product interest, phone (optional), message; explicit **unchecked** contact-permission box and a required privacy
acknowledgement. Validation under each field, focus to the first problem; receipt only after the server confirms.
Variants by URL: `?source=webinar&event=…`, `?source=ad&asset=…&campaign=…`. The public pages never link to the workspace.

## 10. Email design

- **Copy:** opens with the prospect's situation, one product sentence in outcome terms from approved azentio.com claims, one easy question; 60–120 words; no clichés, pricing, SLAs, dates or invented facts. Signed by a named SDR with a one-line opt-out.
- **Look:** single 600px column on white; Azentio logo; Arial/Helvetica; ink text, mint rule; small grey footer with the opt-out.
- **Delivery:** only after human approval.

## 11. Files

| File | What |
|---|---|
| `app/static/console.html` | Sales Workspace (single file: tokens, components, views; no build step) |
| `app/static/contact.html`, `app/static/trust.html` | Public pages |
| `app/static/assets/azentio-logo*.svg`, `azentio-logo-email.png` | Official logo (SVG for web, PNG for email) |
| `app/static/assets/fonts/` | Poppins 400/500/600 + OFL |
