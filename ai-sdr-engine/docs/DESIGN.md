# Azentio Sales Workspace — Design

How the AI SDR's two surfaces look, read and behave: the **public enquiry form** (`/contact`) that prospects use, and the
**Sales Workspace** (`/workspace`) that SDRs, AEs and sales ops use. Brand tokens are taken from azentio.com's own theme
CSS (captured 31 Oct 2026); layout and component choices are ours.

## 1. Principles

1. **Azentio, not "an AI tool".** Same logo, type, colours and button shapes as azentio.com. No purple/AI gradients, no robots, no sparkle-everything.
2. **Plain business language.** Users see *what happened and what to do next*, never how the system works. No model, provider, API, version, score-weights, queue or ID strings anywhere in the UI.
3. **One next step.** Every lead opens with a single "Next step" banner and one primary action. Secondary actions are visually quieter.
4. **Show the evidence.** Every qualification claim is backed by the buyer's own words in a quote, labelled with where it came from ("In their reply", "From their enquiry").
5. **A human approves everything that leaves.** Every email is reviewed; scheduled sends say when they will go; blocked drafts say why.

## 2. Brand tokens

| Token | Value | Use |
|---|---|---|
| `--ink` | `#16303d` | Primary text, primary buttons, dark "Next step" banner, top-level emphasis (Azentio's most-used colour) |
| `--ink-2` | `#2b4654` | Hover on ink, secondary data bars |
| `--ink-soft` | `#e8eef0` | Neutral badge fill, avatar fill, hover tint |
| `--mint` | `#55ffbe` | Azentio accent: CTA on dark, highlight underline, "hot" counts |
| `--mint-soft` / `--mint-ink` | `#ddfff2` / `#0b6b47` | Positive states: Confirmed, Qualified, Delivered |
| `--paper` | `#f5f7f7` | Page background |
| `--line` / `--line-strong` | `#dde5e7` / `#c6d2d5` | Card borders / input borders |
| `--muted` | `#5b6d75` | Secondary text (≥4.5:1 on white) |
| Amber `#fff3dc` / `#8a5300` | | "Likely", "Needs your approval", "Needs attention" |
| Red `#fde8e8` / `#a42727` | | "Unclear", blocked drafts, errors, destructive actions |
| Blue `#e7f0fb` / `#1d4f8c` | | "In conversation" |

Colour never carries meaning alone: every status chip has a word, every BANT tile has an icon **and** a label.

### Typography
- **Poppins** 400 / 500 / 600, self-hosted from `app/static/assets/fonts/` (OFL licence included), `font-display: swap`; fallback `system-ui`.
- Scale: page title 30/600 (with mint highlight under the key phrase, e.g. "in context."), section 18/600, card title 16/600, body 14/1.55, meta 12.5, labels 11 uppercase + 1.6px tracking ("eyebrow" with a 22px rule, as on azentio.com).
- Numbers in tables and KPIs use `font-variant-numeric: tabular-nums`.

### Shape, depth, motion
- Buttons: pills (`border-radius: 999px`), 40px min height (34px small). Primary = ink; CTA on dark = mint; ghost = outline; danger = red text.
- Cards: white, 1px border, 12px radius, soft two-layer shadow. Inputs: 10px radius, ink focus ring.
- Icons: inline SVG, Lucide paths, 1.75 stroke, 15/18/22px. No emoji as icons.
- Motion: 150–250ms ease-out for menus, modals, toasts and new activity rows; skeleton shimmer while loading; everything disabled under `prefers-reduced-motion`.

## 3. Public enquiry form (`/contact`)

Reuses the official-style layout from the team's brand capture: white header with the Azentio logo, two-column hero
("Let's move your business **forward.**" with mint underline, product links, geometric mint motif) and a line-style
form card.

- Fields: first/last name, business email, job title (optional), company, country, product interest, phone (optional), "How can we help?". Explicit, **unchecked** contact-permission box and a required Privacy Policy acknowledgement.
- Validation on submit, errors shown under the field, focus moves to the first problem.
- Receipt only after the server confirms, with a short reference.
- Variants by URL: `?source=webinar&event=…` (webinar registration), `?source=ad&asset=…&campaign=…` (content download). `?prefill=layla|yusuf` fills demo details for presenting.
- The public page does not link to the internal workspace.

## 4. Sales Workspace (`/workspace`)

Sticky white top bar: logo · "Sales Workspace" · five tabs with counts · "Add lead" · "Demo" menu · user avatar.
Every view is deep-linkable (`#/leads/<id>`, `#/approvals`, …).

| View | Purpose | Key elements |
|---|---|---|
| **Leads** | Work one lead at a time | Search + filter chips (All, Needs action, In progress, Qualified, Closed); lead rows with avatar, stage chip, fit badge, green dot when an email awaits approval. Detail: profile header → dark **Next step** banner with jump button → handoff → emails to review → *Buying signals* + *Fit* side by side → conversation (+ "Add a reply") → company & contact → campaign status. Right rail: live **Activity** feed (≥1360px). |
| **Approvals** | Human-in-control queue | Each draft with recipient, type, subject, 3-line preview; Review / Approve & send / Approve (scheduled). Bulk: "Approve & send all first emails", "Send all approved". |
| **Opportunities** | AE handoff | Card per qualified buyer: score ring, plain summary, four signals with quotes, Accept / Send back / Open lead. Accepted ones listed below. |
| **Campaigns** | Outbound | Template tiles (AI in AML, Islamic digital transformation, Next-gen lending) → editable name, theme, titles, markets, size, product → "Create campaign". Each campaign shows contacts / active / replied / not included, with plain reasons for exclusions. |
| **Insights** | Pipeline health | KPI tiles (Leads, Waiting for approval, Qualified, Opportunities); conversion funnel (captured → contacted → replied → qualified → opportunity, with %); sources bars; "Ready for follow-up" table; buying-signal coverage stacks with legend; leads by status. |

### Vocabulary (system → what users read)

| System value | Shown as |
|---|---|
| `ENGAGING` / `QUALIFYING` | In conversation / Reviewing reply |
| `REP_REVIEW` / `OPPORTUNITY` | Qualified / Opportunity |
| `HUMAN_REVIEW` / `NURTURE` | Needs attention / Nurture |
| `SUPPRESSED` / `CLOSED` | Opted out / Not a fit |
| BANT `confirmed / inferred / missing / conflict` | Confirmed / Likely / Unknown / Unclear |
| Budget, Authority, Need, Timeline | Budget, Decision maker, Need, Timeline ("buying signals") |
| ICP checks | "Fit for <product>": Market, Institution, Role, Size, Interest, each a sentence |
| Draft kinds | Reply to enquiry, Webinar follow-up, Ad follow-up, Follow-up question, Campaign email 1/2/3 of 3 |
| Error codes | Sentences with a recovery path, e.g. *"This email changed since it was opened. Review it and approve again."* |

All server-provided sentences pass through a cleaner that removes internal tokens, and the key explanations (fit,
routing reason, handoff summary) are written client-side from structured data rather than shown verbatim.

## 5. Email design

- **Copy:** opens with the prospect's situation, one product sentence in outcome terms from approved azentio.com claims, one easy question; 60–120 words; no clichés, buzzwords, pricing, SLAs, dates or invented facts. Signed by a named SDR ("Nadia Karim, Sales Development | Azentio"), with a one-line opt-out.
- **Look:** single 600px column on white; Azentio logo; Arial/Helvetica (web fonts are unreliable in mail clients); ink text, mint rule; signature block; small grey footer with the opt-out.
- **Delivery:** only after human approval; during the hackathon all mail goes to the team's controlled inbox and the footer names the intended recipient.

## 6. Accessibility & responsiveness

- Contrast ≥4.5:1 for text; visible 2px ink focus ring; skip link; one `h1` per view; `aria-current` on the active tab and selected lead; `aria-live` toasts; dialogs with Escape/close and focus return.
- Breakpoints: ≥1360 three panes (list · detail · activity); 1100–1359 two panes; <1100 single column; <760 icon-only tabs, single-column forms.
- All dynamic content rendered with `textContent` (no HTML injection from leads or replies).

## 7. Files

| File | What |
|---|---|
| `app/static/console.html` | Sales Workspace (single file: CSS + JS, no build step) |
| `app/static/contact.html` | Public enquiry form |
| `app/static/assets/brand.css` | Brand stylesheet for the public form |
| `app/static/assets/azentio-logo*.svg`, `azentio-logo-email.png` | Official logo (SVG for web, PNG for email) |
| `app/static/assets/fonts/` | Poppins 400/500/600 + OFL |
