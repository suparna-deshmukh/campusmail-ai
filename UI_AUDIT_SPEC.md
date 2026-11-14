# CampusMail AI — Complete UI & User-Flow Specification (Design Audit Source Document)

> **Purpose of this document**
> This is an exhaustive, factual specification of the **current** CampusMail AI user interface: every
> screen, every tab, every button, every layout rule, every colour, every interaction, every modal,
> and every end-to-end user journey. It was extracted directly from the codebase on
> 2026-11-13 (files: `app.py`, `db.py`, `ui/*.html`, `static/css/app.css`, `static/js/app.js`,
> `README.md`).
>
> It is written so that a designer (or an AI working for one) can (a) rebuild a mental model of the
> product without touching the code, (b) generate a prompt that dramatically improves the visual
> design, and (c) audit every user flow and shorten it. Nothing here is an opinion — critique is the
> reader's job. Where copy in the UI is internally inconsistent with the code, that is flagged in
> §10 (Facts & inconsistencies).

---

## 1. Product overview

**What it is.** CampusMail AI is a **local, self-contained web app** used by a campus outreach team
to recruit recruiters: one email template is rendered into **one personalised email per company/HR
contact** (name, company, images, links, attachments, signature), then it can be

- saved as **Gmail drafts** for human review,
- **scheduled** to send at an exact date/time (built-in worker, or an optional cloud relay),
- **sent immediately** (only with Safe mode OFF + typed confirmation),
- followed by **automatic reminder emails** to recruiters who don't reply,
- and **tracked end-to-end**: sent / replied / bounced / reminders, per company, per member, per day.

**Who uses it.** A small outreach team on one machine (or a cheap always-on host). Roles:
`admin` and `member` (stored per Google account). The **first person ever to sign in becomes admin**.
Everyone on the approved team shares one workspace (accounts, templates, contacts, queue, reports).
Anyone else who signs in with Google gets a **personal isolated workspace** (separate SQLite db) and
automatically files a **request to join the team**, which an admin approves or declines — or the admin
can **invite an email** so the person gets in immediately.

**Key platform facts**

| Fact | Value |
|---|---|
| Runtime | Python 3.9+ / Flask, launched by `./run.sh` (Mac) or `run.bat` (Windows) |
| URL | `http://localhost:5001` (port 5001, not 5000 — macOS AirPlay; overridable via `CAMPUSMAIL_AI_PORT`) |
| Data | SQLite `data/campusmail_ai.db`; bundled sheet `data/companies.csv`; attachments `data/attachments/`; one db per personal workspace in `data/workspaces/` |
| Auth | Google OAuth (Web client) for login; separate Google OAuth (Desktop client) for each Gmail send-account |
| AI | Optional: OpenAI / Groq / Google Gemini / any OpenAI-compatible endpoint (key in Settings). Everything works without AI |
| Scheduler | Local worker ticks every ≤8 s; optional **Supabase cloud relay** (free tier, Edge Function, per-minute cron, up to 6 emails + 6 reminders/min) so scheduled sends fire while the laptop sleeps |
| Safety rails | **Safe mode ON by default** (drafts only, all live sending blocked); max live sends/day (default 100); stagger (minutes between emails); catch-up policy (send now vs mark missed); typed confirmation (type SEND / CONFIRM / STOP / DELETE / REMOVE) for destructive or live actions |
| Reply tracking | Worker polls the Gmail inbox for replies to each sent mail (and bounce DSNs); replies/bounces can also be marked by hand |

**Files that define the UI**

```
ui/base.html          shared shell (sidebar, topbar, modal, toasts)
ui/login.html         sign-in page (multiple states)
ui/auth_done.html     Gmail OAuth "connected" interstitial
ui/dashboard.html     home page
ui/contacts.html      company/HR list
ui/campaigns.html     campaign CRM (list + detail rendered by JS)
ui/templates.html     template library
ui/merge.html         5-step mail-merge wizard
ui/queue.html         queue + schedule page
ui/reports.html       outreach reports
ui/assistant.html     AI chat
ui/guide.html         9-step getting-started
ui/activity.html      audit log
ui/settings.html      all settings
ui/admin.html         team admin (admin only)
static/css/app.css    entire design system (356 lines, CSS custom properties)
static/js/app.js      all dynamic behaviour (1,680 lines, vanilla JS, no framework, no build step)
```

---

## 2. Rendering model (how the UI is built)

- **Flask + Jinja2** server-side templates; every page except login extends `base.html`.
- **Vanilla JS, no framework, no build step.** `static/js/app.js` defines `Pages.<name>` async
  initializers dispatched on `document.body[data-page]`. All dynamic content (tables, stat cards,
  campaign cards, chat, wizard state) is rendered client-side into placeholder `<div id=...>` /
  `<tbody id=...>` elements with `innerHTML`.
- **Global state poll:** `refreshState()` fetches `/api/state` every **15 s** (also on load) and
  updates: sidebar safe-mode line, sidebar Gmail account line, sidebar user name, and the topbar
  chips (safe mode, Gmail account, next send time, scheduler error). Pages hook `window.onState`
  for page-specific reactions (e.g. Queue enables/disables "Send now").
- **API helper:** `api(path, opts)` fetch wrapper — 401 on any API call hard-redirects to `/login`.
  All non-GET calls are POST with JSON bodies (file uploads use `FormData`).
- **Feedback primitives:** toasts (top-right, auto-dismiss 4.5 s), modals (backdrop + box), and
  `typeToConfirm(...)` — a destructive-action modal that requires typing a word (SEND / CONFIRM /
  STOP / DELETE / REMOVE) before the confirm button enables.
- **Email bodies** render inside sandboxed `<iframe class="email-frame" srcdoc=...>`; local template
  images are rewritten to `/local_img/<name>` URLs.
- **No client routing** — every nav item is a full page navigation. No URL state for filters
  (except `?campaign=` on merge and `?edit=`/`?reveal=` deep links into the template editor).
- **No responsive sidebar**: the 236 px sidebar is `position: fixed` at all viewports; content
  `max-width: 1280px`. The only media query collapses stat grids (1100 px: 4→2 cols, 3→1 col).

---

## 3. Design system (exact values from `static/css/app.css`)

### 3.1 Colour tokens (CSS custom properties on `:root`)

| Token | Hex | Used for |
|---|---|---|
| `--bg` | `#f4f5fa` | page background |
| `--surface` | `#ffffff` | cards, buttons, inputs, modal |
| `--border` | `#e4e7ef` | card borders, table rules, dividers |
| `--border-strong` | `#d3d8e4` | input borders, toggle off-track |
| `--text` | `#101828` | primary text |
| `--text-2` | `#475467` | secondary text (labels, hints-2) |
| `--text-3` | `#98a2b3` | tertiary/muted text |
| `--primary` | `#4f46e5` | brand indigo (primary buttons, links, focus, wizard active) |
| `--primary-600` | `#4338ca` | primary hover |
| `--primary-50` | `#eef2ff` | primary tint (active nav bg, wizard active bg, guide number bg, focus ring) |
| `--success` | `#16a34a` | success (toggles on, dots, done) |
| `--success-bg` | `#ecfdf5` | success chip/pill bg |
| `--warn` | `#d97706` | warning |
| `--warn-bg` | `#fffbeb` | warning chip/pill bg |
| `--danger` | `#dc2626` | danger |
| `--danger-bg` | `#fef2f2` | danger chip/pill bg |
| `--info` | `#0284c7` | info |
| `--info-bg` | `#f0f9ff` | info chip/pill bg |
| `--sidebar` | `#0f1222` | near-black sidebar background |
| `--sidebar-text` | `#a7adcb` | sidebar muted text |
| `--radius` | `12px` | card/stat radius |
| `--shadow` | `0 1px 2px rgba(16,24,40,.05), 0 1px 3px rgba(16,24,40,.06)` | card shadow |
| `--shadow-lg` | `0 12px 32px rgba(16,24,40,.14)` | modal/toast shadow |
| `--font` | `-apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif` | UI font |

**Additional hard-coded colours used in JS-rendered UI:**

| Where | Colour |
|---|---|
| Brand "M" logo mark (sidebar 30 px / login 40 px) | `linear-gradient(135deg, #6366f1, #8b5cf6)` |
| `.btn.ai` (AI buttons) | `linear-gradient(135deg, #7c3aed, #4f46e5)` (purple→indigo), white text |
| Pill "drafted" bg/text | `#f0f9ff` / `#0369a1` |
| Pill "scheduled" | `#fffbeb` / `#92400e` |
| Pill "sent" | `#ecfdf5` / `#15803d` |
| Pill "error" | `#fef2f2` / `#b91c1c` |
| Pill "new"/"pending" | `#f1f5f9` / `#475569` |
| Pill "cancelled" | `#f1f5f9` / `#64748b` |
| Pill "deleted" | `#f8fafc` / `#cbd5e1` (strikethrough) |
| Reports bar chart — drafted / scheduled / sent / replied / error | `#8b5cf6` / `#f59e0b` / `#10b981` / `#3b82f6` / `#ef4444` |
| Reports "reminder" pill | bg `#e0e7ff`, text `#3730a3` |
| Chat: user bubble | bg `var(--primary)`, white text; assistant bubble: white bg |
| Chat log container | bg `#fafbfc`, 1 px `--border`, radius 10 px |
| Image resize bar (`img-bar`) | bg `#111827`, buttons `#374151` hover `#4b5563` |
| Toast default / ok / err | bg `#111827` / `#065f46` / `#991b1b`, white text |
| Table header bg / row hover | `#fafbfe` / `#f8f9fd` |
| Sidebar active item bg | `rgba(99,102,241,.22)`; hover `rgba(255,255,255,.06)` |
| Topbar | `rgba(255,255,255,.86)` + `backdrop-filter: blur(8px)` |
| Modal backdrop | `rgba(15,18,34,.5)` |
| Danger-zone card border | `#fecaca` |
| Login page background | `radial-gradient(1200px 600px at 20% -10%, #eef2ff 0%, var(--bg) 55%)` |

### 3.2 Typography

- Base: `font-size: 14px; line-height: 1.5;` body text `--text`.
- Topbar `<h1>`: 16.5 px / 700 / letter-spacing -.1 px.
- Card title `<h3>` (`.card-head`): 14.5 px / 700.
- `.card-head .sub`: 12 px muted.
- Buttons: 13.5 px / 600 (`.btn.sm` 12.5 px).
- Table cells 13.5 px; `th` 11.5 px uppercase 700, letter-spacing .5 px, `--text-3`.
- `.hint`: 12 px `--text-3`. `.muted`: `--text-3`.
- Stat value: **27 px / 750**, letter-spacing -.5 px; stat label: 12 px uppercase 600.
- Wizard step text: 13 px / 600; step number circle: 21 px, 11.5 px.
- Chips: 12 px / 600. Pills: 11.5 px / 600.
- Log lines (`.logline`): ui-monospace, 12 px.
- Editor (`ed-area`): Arial/Helvetica 12.5 px (mimics email clients).
- Empty state `.big`: 34 px glyph. Login state `.big`: 42 px emoji glyph.

### 3.3 Layout geometry

| Element | Geometry |
|---|---|
| Sidebar | fixed left, **width 236 px**, full height, bg `#0f1222`, z-index 40 |
| Main | `margin-left: 236px`, min-height 100vh, flex column |
| Topbar | sticky top, z-30, padding `14px 28px`, 1 px bottom border, frosted white |
| Content | padding `26px 28px 60px`, **max-width 1280 px** |
| `.grid` | gap 18 px; `.cols-4` / `.cols-3` / `.cols-2` equal columns; @≤1100 px: cols-4→2, cols-3→1 (cols-2 stays 2) |
| Cards | 1 px `--border`, radius 12 px, `--shadow`; `.card-head` padding `16px 20px`, bottom border; `.card-body` padding `20px` |
| Stat card | padding `16px 18px`; accent variants add a **3 px top border** (`accent`=primary, `green`, `amber`, `red`) |
| Table | full width, sticky header row, row hover `#f8f9fd`, cell padding `10px 14px` |
| Search row (`.search-row`) | flex, gap 10 px, wraps, margin-bottom 16 px; inputs max-width 340 px |
| Modal | centered, top-anchored (`align-items:flex-start`, 6 vh top pad), max-width **560 px** (`.wide` = **900 px**), radius 14 px, `pop .14s` animation (translateY 8 px + fade) |
| Toasts | fixed top-right (18 px), column gap 10 px, max-width 380 px, radius 10 px, `slidein .18s` |

### 3.4 Component inventory (exact specs)

**Buttons `.btn`** — inline-flex, gap 7 px, radius 9 px, padding `8px 15px`, 13.5 px/600, 1 px
`--border-strong` border, white bg; hover border `--text-3`; disabled opacity .5.

| Variant | Style |
|---|---|
| (default) | white bg, dark text |
| `.primary` | bg/border `--primary`, white; hover `--primary-600` |
| `.danger` | bg/border `--danger`, white; hover `#b91c1c` |
| `.ghost` | transparent, no border, `--text-2` text; hover bg `#f1f5f9` |
| `.ai` | purple→indigo gradient, white text (used for every AI feature) |
| `.sm` | padding `5px 10px`, 12.5 px, radius 7 px |
| `.block` | full width, centered |

**Chips `.chip`** — pill (radius 999 px), 12 px/600, 1 px border, contains a 7 px `.dot`.
Variants: default (gray), `.on` (green: bg `--success-bg`, text `#065f46`, border `#bbf7d0`),
`.warn` (amber: `#fffbeb`/`#92400e`/`#fde68a`), `.danger` (red: `--danger-bg`/`#991b1b`/`#fecaca`).

**Pills `.pill`** — status labels, radius 999 px, 11.5 px/600, colours per §3.1. One pill per
status value: `new`, `pending`, `drafted`, `scheduled`, `sent`, `replied` (same style as sent),
`error`, `cancelled`, `deleted`.

**Inputs `.input`** — padding `9px 12px`, radius 9 px, 13.5 px, border `--border-strong`;
focus: border `--primary` + `box-shadow 0 0 0 3px rgba(79,70,229,.12)`. Textarea min-height 90 px,
vertical resize. Labels: 12.5 px/600 `--text-2`, 5 px below-gap. `.field` margin-bottom 14 px.

**Toggle `.toggle`** — 40 × 22 px, radius 999 px, off-track `#d3d8e4`, on-track `--success`;
white 18 px knob; used for Safe mode and AI enable (both save **on click, immediately**).

**Banners `.banner`** — radius 10 px, padding `11px 15px`, 13.5 px. Variants:
`banner-info` (`#f0f9ff`/`#075985`/border `#bae6fd`), `banner-warn` (`#fffbeb`/`#92400e`/`#fde68a`),
`banner-err` (`#fef2f2`/`#991b1b`/`#fecaca`), `banner-ok` (`#ecfdf5`/`#15803d`/`#bbf7d0`).

**Checklist chip `.check`** — pill with 15 px square checkbox; done = green bg + ✓.

**Wizard steps `.wiz-steps .step`** — pill chips, gap 8 px; 21 px numbered circle (off: `#eef0f6`
bg/`--text-2`); `.active` = primary border/text on `--primary-50` bg, circle primary/white;
`.done` = green text/border `#bbf7d0`, circle green/white.

**Empty state `.empty`** — centered, 54 px top/bottom padding, 34 px glyph, muted text.

**Divider `.divider`** — 1 px `--border`, 18 px vertical margin.

**Account row `.acct-row`** — flex, gap 12 px, 10 px vertical padding, bottom border; 9 px status dot.

**Template editor**
- `.ed-toolbar` — buttons 32 px min, 6 px radius: **B** (bold), **I** (italic), **U** (underline),
  **• list** (unordered), **1. list** (ordered), 12 px spacer, **↺ undo**, **↻ redo**.
- `.ed-doc` — white doc box: "Subject:" label line (contenteditable, 13 px/600, dashed
  primary underline on focus) + 1 px divider + `.ed-area` contenteditable (min-height 340 px,
  max 520 px scroll, Arial 12.5 px).
- Clicking an image in the body opens a floating dark **image bar** with width controls
  `25% / 50% / 75% / 100% / Fit / ✖ (remove)`.

**Login page** — full-viewport centred `.login-wrap`; `.login-card` max-width 440 px, padding
`30px 30px 22px`; brand row = 40 px gradient "M" + "CampusMail AI" (19 px/750) + team name
(12.5 px muted); footer line under card 12 px `--text-3`. Google button: primary, `.big-btn`
(padding 12 px 16 px, 14.5 px), official 4-colour 18 px Google "G" SVG.

---

## 4. App shell (every logged-in page)

### 4.1 Sidebar (236 px, dark `#0f1222`)

Top — **logo block** (padding `22px 20px 18px`):
- 30 × 30 px rounded (8 px) gradient square with letter **"M"** (15 px, white)
- "CampusMail AI" (17 px/700, white) with the team brand name underneath as `<small>`
  (11 px, `--sidebar-text`). The brand name is admin-configurable in Settings
  (`brand_team_name`), shown on the login page and in navigation.

**Navigation** (padding `8px 12px`; each item: flex, gap 11 px, padding `9px 12px`, radius 9 px,
13.5 px/500, `--sidebar-text`; a 19 px-wide Unicode glyph icon column). **12 items** —
the 12th is admin-only:

| # | Glyph | Label | Route | Notes |
|---|---|---|---|---|
| 1 | `&#9632;` ▢ | Dashboard | `/` | |
| 2 | `&#9679;` ● | Contacts | `/contacts` | |
| 3 | `&#9635;` ☑ | Campaigns | `/campaigns` | |
| 4 | `&#9998;` ✎ | Templates | `/templates` | |
| 5 | `&#9881;` ⚙ | Mail Merge | `/merge` | gear glyph — **same glyph as Settings (#11)** |
| 6 | `&#9202;` ⏲ | Queue and Schedule | `/queue` | |
| 7 | `&#9636;` ▤ | Outreach Reports | `/reports` | |
| 8 | `&#10022;` ✦ | AI Assistant | `/assistant` | |
| 9 | `&#9733;` ★ | Getting started | `/guide` | |
| 10 | `&#9776;` ☰ | Activity | `/activity` | |
| 11 | `&#9881;` ⚙ | Settings | `/settings` | gear glyph (duplicated with #5) |
| 12 | `&#9829;` ♥ | Team admin | `/admin` | **rendered only when `me.role == 'admin'`** |

Active item: bg `rgba(99,102,241,.22)`, white text. Hover: bg `rgba(255,255,255,.06)`, white.
No section headers, no search, no collapse/hamburger at any width.

**Sidebar footer** (`.side-foot`, border-top `rgba(255,255,255,.08)`, padding `14px 16px`, 12 px):
1. **Safe-mode line**: 8 px dot (`#64748b` gray, or **green `#22c55e` when safe mode is OFF** —
   i.e. green = live sending armed) + text `Safe mode ON - drafts only` or
   `Safe mode OFF - live sends armed`.
2. **Gmail line**: `.acct` (12.5 px/600, `#e6e8f5`, word-break) — all connected send-account
   emails joined by ", ", or `No send account`.
3. **User row**: user's display name (11.5 px/500) + right-aligned **"Sign out"** (ghost `.sm`,
   11 px, `#a7adcb`) → POST `/logout` → `/login`.

### 4.2 Topbar (sticky, frosted)

- Left: page `<h1>` (per page).
- Right (`.chips`, flex gap 8 px) — rebuilt by the 15 s state poll, **up to 4 chips** in order:
  1. **Safe-mode chip** — `.warn` (amber) `Safe mode: no live sends` **or** `.on` (green)
     `Live sending armed`.
  2. **Gmail chip** (`.on`, only if ≥1 connected account) — first account's email + `+N` if more.
  3. **Next send chip** (only when the scheduler has a next fire) — amber dot +
     `Next send: <local time>`.
  4. **Scheduler error chip** (`.danger`, only on error) — first 28 chars of the last worker error.

### 4.3 Global overlays

- **Personal-workspace banner** (non-team members only, top of `.content`):
  - *pending*: warn banner — "⏳ **Your request to join the {team} is under review.** You are
    working in your own personal workspace for now - your data is separate from the team's. As
    soon as an admin approves your request, you will get access to the shared workspace." +
    optional **note textarea** (placeholder "e.g. I handle the core committee / company visits...")
    + **"Send note"** button + status hint.
  - *denied*: warn banner — "✖ **Your request to join the {team} was not approved.** This is your
    personal workspace - your data here is separate from the team's."
- **Modal system** (one global instance): `.modal-backdrop` (rgba(15,18,34,.5)) + `.modal`
  (max 560 px / 900 px wide) with `.m-head` (title + × close), `.m-body`, `.m-foot` (right-aligned
  buttons). Closes on backdrop click, ×, or Escape. **Wide (900 px) modals are exactly three:**
  Template editor, Template preview, Queue email preview.
- **Toast stack** (top-right): dark `#111827` default, green `#065f46` (ok), red `#991b1b` (err);
  auto-removed after 4.5 s.

---

## 5. Screen-by-screen specification

### 5.1 Login — `/login` (no sidebar)

Page title: `Sign in - CampusMail AI | {team}`. Centered card (max 440 px) on the radial-gradient
background. **Brand header** always visible (40 px "M" + name + team sub). The body has **5 states**:

1. **Fresh (no email in session)**
   - If Google sign-in is configured *and* the app is in "accepting" mode: full-width **big
     "Sign in with Google"** button (Google 4-colour SVG), a divider, and a muted note:
     "Sign in with your **institutional Google account**. Team members share one workspace;
     everyone else gets their own personal workspace and can request team access."
   - If Google is **not** configured: warn banner "Google sign-in is not set up yet. Ask the admin
     (the first person to set up this platform) to paste the Google sign-in client ID and secret in
     **Settings → Google sign-in**. Then reload this page."
2. **Request pending** (signed in, not approved): big ⏳ glyph (42 px), **h2 "Request under
   review"**, "Signed in as **{email}**. Your request to join the {team} is waiting for an admin to
   approve it. You will get in once the team reviews it." + note textarea (rows 2, same
   placeholder as the in-app banner) + centered **"Send note"** (default) + **"Check again"**
   (ghost, reloads page) + centered status hint ("Note sent to the team.").
3. **Request denied**: big ✖ glyph, **h2 "Request not approved"**, "Signed in as **{email}**. Your
   earlier request was declined. Please talk to the {team} lead." + "Check again" (ghost).
4. **Approved, not yet redirected**: big ✅ glyph, **h2 "Welcome, {name-or-email}"**,
   "Taking you in...", **"Continue"** primary button → `/`.
5. **Error banner** (any state): red banner with the error text + **"Try again"** (sm, →
   `/api/google/start`) when Google is ready.

Footer under the card: `{brand_foot} One shared {team} workspace + personal workspaces for
everyone else.` (brand_foot is an optional admin-set line).

### 5.2 Gmail-connected interstitial — `/api/auth/callback` landing (`auth_done.html`)

Standalone centered card (max 420 px, padding 36 px): ✅ (40 px) + **h2 "Gmail connected"** +
"You can go back to CampusMail AI and continue." + **"Back to CampusMail AI"** (primary → `/settings`).

### 5.3 Dashboard — `/`

Page title: **Dashboard**. Four stacked blocks:

**A. Setup checklist card** (left border 4 px `--primary`):
- Left: bold "Setup checklist" + hint "What's left before your first send".
- Middle: row of **7 `.check` pills** (wrap, gap 8 px): `Sign in` (always done),
  `Team sign-in set up` (admin) / `Team is set up` (member), `Gmail account connected`,
  `Company list loaded`, `Template ready`, `First batch generated`, `First drafts/sends done`.
  Done = green + ✓.
- Right (margin-left auto): either a **primary `.sm` nudge button "Next: {first undone item} →"**
  (links to the relevant page: /settings, /settings, /contacts, /templates, /merge, /queue) or a
  green chip **"All set - ready to send"**.

**B. Stat cards — `.grid.cols-4` — exactly 10 cards** (so 3 rows: 4 + 4 + 2):

| # | Label | Accent | Sub-hint |
|---|---|---|---|
| 1 | Contacts in list | accent (indigo top) | ready to process |
| 2 | Drafted | — | in Gmail drafts |
| 3 | Scheduled | amber | waiting for worker |
| 4 | Sent | green | delivered |
| 5 | Replied | accent | recruiters responded |
| 6 | Bounced | red if > 0 | address wrong / not exist |
| 7 | Queue (pending) | — | generated, not sent |
| 8 | Scheduled emails | amber | in send queue |
| 9 | Sent emails | green | total sent so far |
| 10 | Errors | red if > 0 | need attention |

(Contacts-level stats 1–6 count contacts; 7–10 count queue emails. Initial paint shows one
"Loading..." card; cards render after `/api/state`.)

**C. `.grid.cols-2`**
- **Active template card** — head: "Active template" + actions: **"New mail merge"** (sm primary →
  `/merge`) and **"Manage"** (sm ghost → `/templates`). Body: active template name bold +
  "(id N)", or muted "No template yet - add one in Templates."
- **Batches card** — head: "Batches" + sub "N total". Table `# | Name | Created | Emails |
  Status` (Status = pills with counts, e.g. `sent 12 pending 3`). Empty: "No batches yet - run a
  Mail Merge."

**D. Recent activity card** — head: "Recent activity" + **"View all"** (sm ghost → `/activity`).
Table `When (UTC) | Action (pill) | Who | Detail`, latest 6 rows. Empty: "No activity yet."

### 5.4 Contacts — `/contacts`

Page title: **Contacts**.

**Toolbar (`.search-row`)**
- Search input (debounce 300 ms) placeholder `Search company, person, email or serial...`
- Status select (max-width 160): `All statuses / new / drafted / scheduled / sent / replied / error`
- Chip: `N contacts` (total after filter)
- Right-aligned button group: **"Import sheet"** (hidden file input `.csv,.xlsx`),
  **"Export CSV"** (→ `/api/export/contacts.csv`), **"Add contact"**, **"Delete selected (N)"**
  (danger, N = checkbox count).

**Table** — columns: `[checkbox] | Serial (tabular nums) | Company | HR name | HR email |
Status (pill) | [Edit (sm ghost)]`. Empty: "No contacts match."

**Footer bar** (card-head styled, top border): left `N shown, page X of Y` (50 rows/page);
right: **"← Prev"** / **"Next →"** (`.sm`).

**Modals & behaviours**
- **Add contact**: fields `Company`, `HR name`, `HR email`, `Serial (optional)`; footer
  Cancel / **Add** (primary). Toast "Contact added".
- **Edit contact** (per-row): title "Edit contact - {company}"; fields `Company`, `HR name`,
  `HR email`, `Notes` (textarea rows 3); Cancel / **Save**.
- **Import**: toast `Imported: X added, Y updated (Z skipped)`. Re-importing the same sheet
  **updates** rows instead of duplicating.
- **Delete selected**: type-to-confirm **DELETE** — "This removes N contact(s) from the list.
  Generated emails in the queue are not touched."
- Contact status is derived from the latest email sent to that contact (new → drafted →
  scheduled → sent → replied; error on failure).

### 5.5 Campaigns — `/campaigns` and `/campaigns/<id>` (CRM)

Page title: **Campaigns**. Both list and detail are rendered by JS into `#cam-view`.

**List view**
- Header row: flex hint text ("Campaigns are your CRM home - each one holds the sheets you merge,
  the batches you create, and their full performance.") + **"+ New campaign"** (primary).
- `.grid.cols-2` of campaign cards: name (15 px/700), optional description (muted), meta line
  (12 px): `N contacts · M batch(es) · K sheet(s) · last activity <time>`; status pills with
  counts (`drafted n / scheduled n / sent n / replied n / error n`); buttons: **"Open"** (sm,
  → detail) and **"Start mail merge"** (sm primary, → `/merge?campaign=<id>`).
- Empty state: ☑ 34 px + "No campaigns yet. Create one to organise your outreach."
- **New campaign modal**: hint "Give it a name you will recognise - e.g. Summer 2027 recruiter
  outreach, Winter internships."; fields `Campaign name` (placeholder "e.g. Summer 2027 recruiter
  outreach"), `Note (optional)` (placeholder "e.g. 2027 batch, all departments"); Cancel /
  **Create**. On create → navigates to the new campaign's detail page. Validation: name required
  (toast "Give the campaign a name").

**Detail view** (`/campaigns/<id>`)
- Back link "← All campaigns" (hint-styled).
- Header: campaign name (20 px/800), description (muted), meta (12 px): `Created <time> by
  <email>`; right: **"+ Start mail merge in this campaign"** (primary → `/merge?campaign=<id>`).
- **7 stat cards** (`.grid.cols-4` → 4 + 3 rows): Contacts ("in this campaign's sheets"),
  Emails (accent, "across all batches"), Drafted ("in Gmail drafts"), Scheduled (amber,
  "waiting to send"), Sent (green, "delivered"), Replied (accent, "recruiters responded"),
  Errors (red if >0, "need attention").
- Section **Batches in this campaign** — table `Batch (bold) | Created | Status (pills with
  counts) | [Queue (sm link → /queue)]`. Empty: "No batches yet - start a mail merge in this
  campaign."
- Divider.
- Section **Sheets in this campaign** — table `Sheet (bold) | Rows | Columns (inline code chips) |
  Uploaded | [Delete (sm ghost)]` (type-to-confirm DELETE: "Its contacts stay in your list; only
  the sheet record is removed."). Empty: "No sheets yet - upload one during a mail merge in this
  campaign."

---

### 5.6 Templates — `/templates`

Page title: **Templates**.

**Toolbar** — left: an informational chip: "Upload any .docx / .html / .txt template - replaceable
fields are detected automatically". Right (3 buttons):
- **"✦ AI: create a template"** (`.btn.ai`)
- **"✦ AI: write reminder template"** (`.btn.ai`)
- **"Upload template"** (primary)

**Grid** — `.grid.cols-3` of template cards (18 px padding):
- Name (14.5 px/700)
- `Subject: {subject}` or "(none)" (muted)
- Detected **field chips** (11 px chips, one per replaceable field, e.g. `Recruiter Name`,
  `Company`), or muted "no fields"
- Button row (gap 7 px, wraps): **Edit** (sm primary), **Preview** (sm), **✦ AI polish** (sm ai —
  **only when an AI key is configured**), **Set active** (sm), **Delete** (sm ghost)
- If this is the active template: **"ACTIVE"** pill absolute-positioned top-right
  (`.pill.drafted` style).
- Empty state: ✎ (34 px) + "No templates yet." + "Upload a Word/HTML/text template, or create one
  with AI."

**Modals**
- **Upload template**: hint — "The **subject is entered separately** from the body. Body: upload a
  Word (.docx) / HTML / text file, or paste it. Mark replaceable values as `{field}`, `[Field]` or
  guillemet-Field. Images in Word files come along automatically." Fields: `Template name`
  (placeholder "e.g. Diwali Outreach"), `Subject line (required - may include fields like
  [Company Name])` (placeholder "e.g. Invitation from the campus: 2027 Graduating Batch Drives"),
  `Body - upload a file` (file `.docx,.html,.txt`), `...or paste the body text here (leave one of
  the two empty)` (textarea rows 5, placeholder "Dear [Recruiter Name], ..."). Footer: Cancel /
  **Upload**. Validation: subject required; one of file/text required. Toast "Template added - N
  fields detected".
- **Edit template (WIDE modal)**: hint "The whole template is below - **subject on the first
  line**, then the body. Click anywhere and type, erase or press Enter just like in Word. Field
  codes like **[Recruiter Name]** work in both." → `Template name` input → **editor toolbar**
  (B / I / U / • list / 1. list / ↺ / ↻) → **doc box**: "Subject:" contenteditable line, divider,
  body contenteditable (click-to-edit; images clickable for the floating resize bar
  `25% / 50% / 75% / 100% / Fit / ✖`). Footer: Cancel / **Save changes**. Deep-link support:
  `/templates?edit=<id>&reveal=img` opens it and scrolls to the first image with the bar open.
  Save normalizes whitespace in the subject and re-encodes local image paths as `local:<name>`.
- **Template preview (WIDE modal)**: hint "Sample - rendered with your **first contact** from the
  sheet, so you can see exactly how the fields fill..." + bold `Subject: ...` line + 480 px email
  iframe. Close.
- **✦ AI: create a template**: fields `Template name` (placeholder "e.g. New Year Outreach"),
  `Brief - what is this email about, who is it for, what should it achieve?` (textarea rows 5,
  placeholder "e.g. Invite recruiters to our campus recruiter drive in Jan 2027. Mention 464
  students, 93% internship completion, ask for JDs."), `Tone` select
  (`professional / warm / concise / formal`). Footer: Cancel / **✦ Generate** (ai). Toast "AI
  template created".
- **✦ AI: write a reminder template** (requires AI key, else toast "Set an AI key in Settings
  first"): `What is the reminder about?` (textarea rows 3, placeholder "e.g. invitation for the
  2027 graduating batch, drives starting January 2027") + `Tone` select
  (`professional / warm / concise`). Cancel / **✦ Generate**. Toast "Reminder template created".
- **Per-card AI polish** (inline button, no modal): POSTs, toast "Template polished by AI".
- **Delete**: type-to-confirm DELETE — "Delete this template? Queued emails already generated keep
  their rendered bodies."

### 5.7 Mail Merge — `/merge` (the 5-step wizard)

Page title: **Mail Merge**. Single card containing: the **step indicator**, a **dataset info
line**, and **five `.wiz-panel` divs** (only one visible at a time).

**Step indicator** (`.wiz-steps`): 5 pill steps — `1 Campaign`, `2 Template`, `3 Data source`,
`4 Map fields`, `5 Preview & generate`. Current = `.active` (indigo), earlier = `.done` (green).
No back-navigation by clicking completed steps (buttons only).

**Dataset info line** (above the panel, hint style, live):
`Campaign: <name or "not chosen"> · Data: main contact list (N contacts)` **or** `sheet '<name>'
(N rows, M columns)`.

**Guard:** if no templates exist, all panels are hidden and the page shows: "No templates yet -
create one in the **Templates** page first, then come back."

---

**Step 1 — "Which campaign is this for?"**
- Hint: "A campaign groups one outreach effort - the sheets you use, the batches you create, and
  how they perform. Pick an existing campaign or start a new one."
- One **radio card per campaign** (clickable card: radio + bold name + muted description + 11.5 px
  meta `N contacts, M batch(es), K emails (S sent, R replied)`). Empty: muted "No campaigns yet -
  create one below to keep this outreach organised."
- Field "…or create a new campaign": name input (max-width 380, placeholder "e.g. Summer 2027
  recruiter outreach") + **"Create & use"** (creates the campaign, selects it, and advances to
  step 2).
- Selecting a campaign auto-fills the step-5 batch name with `{campaign name} {today's date}`.
- Deep link: `/merge?campaign=<id>` pre-selects it.
- Footer: **"Next: pick template →"** (primary). Validation: campaign required (toast "Pick a
  campaign or create one first").

**Step 2 — "Pick the email template"**
- One **radio card per template**: bold name (+ "active" pill if the active template), muted field
  list. Selected card gets an indigo outline. The **active template is pre-selected** by default.
- **"⬆ Upload a new template"** button → modal (same fields as Templates' upload modal but with
  `Template name (optional)` + subject + file-or-paste; on create, refreshes the radio list).
- Footer: **"← Back"** (ghost) · **"Next: who to write to →"** (primary). Validation: template
  required.

**Step 3 — "Who are we writing to?"**
- Hint: "Pick the data source. With a sheet, you choose in the next step which of its columns
  replace each template field."
- **Radio card A — "Use the main contact list"**: muted `N contacts, columns: serial company
  hr_name hr_email`.
- **Radio card B — "Use an uploaded sheet"** + sheet `<select>` (options `name - N rows`, or
  "- none uploaded yet -").
- **Card C — "Upload a new sheet for this merge"**: sub-note "CSV or Excel (.xlsx). First row =
  column names (e.g. Company Name, HR Name, HR Email). Every row becomes one email." + file input
  (`.csv,.xlsx`) + **"Upload sheet"** (primary sm) + status text ("Uploading..."). On success a
  **sheet summary** appears: `name - N row(s) · columns: [inline codes]`, and if rows lack emails:
  amber warning "N row(s) have no email in column '<col>' - those emails cannot be sent."
  Upload toast: "Sheet ready - N rows. Next, pick what replaces each field."
- Footer: **"← Back"** · **"Next: map fields →"** (primary). Validation: if sheet source, a sheet
  must be picked/uploaded (toast "Pick a sheet from the list" / "Upload the sheet first").

**Step 4 — "What replaces each field?"**
- Table (max-width 820 px): `Template field (220 px, bold) | Replaced with`.
- Each field row has **three controls**: mode select (`Use column` / `Constant text` / `Blank`),
  a **column select** (max-width 240; options are the data columns, each annotated with a sample
  value, e.g. `HR Name  (e.g. Priya Sharma)`; pre-selected from a server-side suggestion
  `/api/merge/suggest`), and a **constant text input** (max-width 200) — the column select and
  constant input show/hide per mode.
- Sample line below: `First row of your data - <col> = "<value>" · <col> = "<value>" ...` (only
  when a sheet with preview rows is in use).
- If the template has **no fields**: single row "This template has no replaceable fields - every
  email will be identical."
- Footer: **"← Back"** · **"✦ Suggest with AI"** (ai, **only with AI key** — re-runs AI mapping,
  toast "AI suggestions applied - review them below") · **"Preview →"** (primary).

**Step 5 — "Preview with real data"** (the biggest step; sections separated by dividers)
1. **Total line**: "Generating will add **N** emails to the queue (one per row) as **pending**.
   Nothing is sent yet."
2. **Previews**: 3 rendered emails (server-rendered with real rows). Each: preview note
   `Serial N · {hr_name} · {company} · {hr_email}` + `Subject: ...` + 380 px email iframe. A
   "Updating previews..." hint badge appears while re-fetching (triggered by range or signature
   changes).
3. **Signature append** (shown only if a signature exists in Settings): checkbox
   "Append my saved signature (Settings → Signature) to every email in this batch" + hint "Tick
   this only if your template does not already end with your signature. The preview above updates
   when you toggle it."
4. **Attachments (optional - go out with every email in this batch)**: **"Add files"** button +
   hidden multi-file input (accepts `.pdf,.doc,.docx,.xls,.xlsx,.csv,.txt,.png,.jpg,.jpeg,.zip,
   .ppt,.pptx,.html`) + info line ("No attachments yet. PDFs, spreadsheets, brochures - up to 10
   files, 15 MB total. Each file is stored once, attached at send time." → when files chosen:
   "N file(s), X KB total - stored once, attached at send time") + **file chips**
   (`name + KB + ✖` remove).
5. **Automatic follow-up reminders (optional)**: checkbox "Send a reminder automatically if the
   recruiter has not replied" → reveals a box (border, `--bg`) with `.grid.cols-3`:
   `If no reply after (days)` (number, min 1 max 90, **default 3**) · `Reminder template` (select
   of all templates) · `Maximum reminders` (select `1 (default) / 2 / 3`) + **"✦ AI: write a
   reminder template"** (ai sm) + hint "You can also upload your own reminder as a Word/HTML
   template in Templates and pick it here." (The AI reminder modal is the same one from
   Templates; the generated template becomes the selected option.)
6. **Range, CC and what happens next**:
   - `Serial range (optional - blank = everyone in the sheet)`: two 92 px number inputs
     `from` / `to`.
   - `CC on every email (optional, comma separated)`: input (max-width 330, placeholder
     "e.g. team@yourdomain.com, name@company.com"); each address is regex-validated
     (toast "Invalid CC address: ...").
   - `After generating` select (max-width 290) — **4 options with exact copy**:
     - `queue` → "Put in queue only (draft later from Queue page)"
     - `draft` → "Create Gmail drafts now"
     - `schedule` → "Schedule the send at a time"
     - `send` → "Send immediately (Safe mode must be OFF)"
   - If `schedule`: reveals `Send at (local time)` (placeholder `YYYY-MM-DD HH:MM   e.g.
     2026-11-12 10:30`) + `Stagger (minutes between emails; 0 = all at once)` (number, default 0).
   - If `send`: reveals red warning: "**"Send immediately" sends RIGHT NOW from the selected
     account - it cannot be undone.**"
7. **Generate row**: `Batch name` (default "Outreach batch", flex, min 220 px) · `Send from
   (Gmail account)` select (min-width 240; filled with connected accounts, last-used preferred,
   own accounts tagged "(mine)", or "+ connect one in Settings") · **"← Back"** (ghost) ·
   **"Generate"** (primary).

**Generate → confirmation modal** (type-to-confirm): title "Generate batch". Body: "This renders
emails for **serials X to Y** (or **all N rows**) (preview above shows the first 3) and: " +
action-specific sentence —
- queue: "put in the queue as **pending** (nothing sent)"
- draft: "created as **Gmail drafts** (nothing sent)"
- schedule: "scheduled for **<time>**"
- send: "sent **immediately** - cannot be undone!"
plus optional lines "CC on every email: **...**", "Includes **N attachment(s)** on every email.",
"Auto-reminder: **N day(s)** after send, max **M×** if no reply."
Confirm word: **CONFIRM** (or **SEND** for immediate send); button label "Generate".
On success: uploads attachments to the new batch, attaches reminder config, toast
"Batch created - N emails queued|drafted|scheduled|sent - ...", then **redirects to `/queue`**.

### 5.8 Queue and Schedule — `/queue`

Page title: **Queue and Schedule**. Three stacked blocks.

**A. Worker bar** (card, `padding:12px 18px`, flex): left — monospace `.logline`:
`Worker: running|stopped · due now: N · next fire: <time>|none` (+ red `· <last error>` if any);
right — **"Run worker now"** (sm) → POST `/api/scheduler/trigger`, toast "Scheduler ticked".

**B. Control row (`.search-row`)**
- **8 status tabs** (`.btn.sm`, active tab gets primary styling) with **live counts** in parens:
  `All (N)`, `Pending (N)`, `Drafted (N)`, `Scheduled (N)`, `Sent (N)`, `Replied (N)`,
  `Errors (N)`, `Stopped (N)` — mapped to statuses `"" / pending / drafted / scheduled / sent /
  replied / error / cancelled`.
- Right group: chip `selected: N (empty = pending rows)`; chip-label **"Send from"** + account
  `<select>` (compact 12.5 px); buttons: **"Create drafts"** (default), **"Schedule /
  Reschedule"** (primary), **"Stop"** (default), **"Send now"** (**danger, disabled while Safe
  mode is ON**, tooltip "Safe mode is ON").
- **Selection semantics:** row checkboxes (plus a select-all header checkbox). If **no rows are
  selected, bulk actions apply to all *pending* rows in the current tab filter**.

**C. Queue table** — columns: `[select-all checkbox] | Serial | To | Subject (ellipsis, max
280 px) | Status (pill) | When | [row actions]`.
- "When" cell (muted, multi-line): `scheduled <time>` (prefixed "**reminder of** " for reminder
  mails), `sent <time>`, `replied <time>` (green), `reminders sent: N`, and any error (red, first
  60 chars).
- **Row actions** (nowrap, sm ghost): **View** (always) · **Stop** (pending/scheduled only) ·
  **Reschedule** (scheduled only) · **Reply** (sent/replied only) · **Remove** (always).
- Empty: "Queue is empty - generate a batch in Mail Merge first."

**Modals & actions**
- **Create drafts** (bulk): type-to-confirm CONFIRM — "Creates **N** draft(s) in the selected
  Gmail account's Drafts folder. Nothing is sent." → toast "N drafts created" / "N drafted, M
  failed" (err).
- **Schedule / Reschedule** (bulk, or single-row Reschedule): modal **"Schedule send"** —
  `Send at (local time)` input, `Stagger (minutes between emails; 0 = all at once)` (default from
  Settings), live estimate line "N email(s); last one goes out at <computed time>". Cancel /
  **Schedule**. Toast "N emails scheduled - they send themselves at the chosen time".
- **Stop** (bulk or single row): type-to-confirm STOP — "Stops **N** email(s) - they will NOT be
  sent or drafted. You can draft or delete them later." → toast "N stopped".
- **Send now** (bulk): blocked with toast while Safe mode ON ("Safe mode is ON - turn it off in
  Settings first"); otherwise type-to-confirm **SEND** — "This sends **N** email(s) RIGHT NOW from
  the selected Gmail account. This cannot be undone." → toast "N emails sent" / "N sent, M failed".
- **Remove** (single row): type-to-confirm DELETE — "Remove this email from the queue? It will not
  be sent or drafted."
- **Email preview (WIDE modal)**: meta line `Serial N · To: <addr> · <status pill> · scheduled
  <t> · sent <t> · replied <t> (green) · reminders: N · reminder email` + action row + 340 px
  email iframe + Close.
  - Actions for `sent` mails: **"Mark as replied"** (sm primary) or **"Mark as NOT replied"**
    (sm, when already replied) — manual overrides of auto reply detection.
  - **"✦ AI: polish this email"** (sm ai, only with AI key) → **AI polish modal**: optional
    instructions input (placeholder "e.g. make it shorter, more formal, mention the January
    start"); while working: "Working..."; result = `New subject` input + `New body (HTML)`
    textarea (rows 8) + 220 px rendered preview; footer Cancel / **Save polished version**
    (updates the queued email; toast "Polished email saved - queue updated").
- **Reply modal** (from sent/replied rows): title "Reply to <addr>"; hint `Subject: Re:
  <original subject>`; reply textarea (rows 7, placeholder "Write your reply...");
  **"✦ Draft with AI"** (sm ai — fills the textarea with an AI-suggested reply); context hint:
  Safe mode ON → "Safe mode ON - it will be saved as a Gmail draft (nothing is sent)." else
  "Sends from the selected account." Footer: Cancel + **"Save as draft"** (safe mode) /
  **"Send reply"** (live mode).

---

### 5.9 Outreach Reports — `/reports`

Page title: **Outreach Reports**. Five stacked blocks:

**A. Stat cards — `.grid.cols-4`, exactly 5** (so row 1 = 4 cards, row 2 = 1 card alone):

| Label | Accent | Hint |
|---|---|---|
| Sent emails | green | outreach delivered |
| Replied | accent (indigo) | recruiters responded |
| Bounced | red if > 0 | address wrong / not exist |
| Reply rate (`N%`) | — | of sent emails |
| Reminders sent | amber | automatic follow-ups |

**B. `.grid.cols-2`**
- **"Sends per day - last 14 days"** — pure-CSS bar chart: 14 flex columns, bar = `--primary`,
  max height 80 px scaled to the day max, value number above each bar (11 px/600, hidden when 0),
  date label below (10 px muted), zero days rendered as 1 px stubs at 25% opacity.
- **"Pipeline buckets"** (sub "every email, where it is") — one row per status: label (bold,
  capitalized) + `N (P%)`, with a 10 px rounded progress bar. Fixed order & colours:
  pending (gray `--text-3`), drafted `#8b5cf6`, scheduled `#f59e0b`, sent `#10b981`,
  replied `#3b82f6`, error `#ef4444`. Empty: "No emails yet - run a mail merge to start tracking."

**C. "Top companies by outreach volume"** — table `Company | Total | Sent | Replied | Bounced
(red if >0) | Reply rate`. Empty: "Nothing here yet."

**D. "By team member"** (sub "each member's outreach, replies and bounces") — table
`Member (bold name + muted email, "(team)" for unattributed) | Total | Sent | Replied | Bounced
(red) | Reply rate`.

**E. "Every email - trackable"** — filter bar (wraps): search input (260 px, placeholder
"Search company, person, email, subject...", 300 ms debounce) · member select (max 190 px,
"All team members" + each member's name) · status select (max 150 px: `All statuses / pending /
drafted / scheduled / sent / replied / bounced / error` — note: **no "cancelled"**) · **"Export
CSV"** (sm, → `/api/reports/emails.csv`, respects current filters).
Table: `Serial | Company | To | Subject (ellipsis 260 px) | Status (pill; reminder mails get an
extra indigo "reminder" pill) | Sent | Replied (green) | Bounced (red, ⚠ glyph, hover title =
bounce reason) | Rem. (count)`. Empty: "No emails match." The member filter also re-scopes the
summary cards/tables (C–D) above.

### 5.10 AI Assistant — `/assistant`

Page title: **AI Assistant**. One card containing:

- Intro hint (13.5 px): "Chat with the platform. It can create campaigns and templates, plan mail
  merges (queue, Gmail drafts, or scheduled sends), stop or reschedule emails, and report on how
  things are going. It **never sends anything** without your manual step."
- **Chat log** (max-height 520 px, scroll, bg `#fafbfc`, 1 px border, radius 10 px, 14 px pad):
  - **User bubbles**: right-aligned, max-width 88%, bg `--primary`, white text, radius 12 px,
    13.5 px, pre-wrap.
  - **Assistant bubbles**: left-aligned, white bg, dark text; plain text only (the system prompt
    forbids markdown). After tool calls, a row of small chips: `⚙ <tool> → <result JSON, 180
    chars>`.
  - While awaiting: muted "Thinking..." line.
  - First visit greeting: "Hi! I'm your CampusMail AI assistant. Ask me about your campaigns, or
    tell me to do something - create a template, plan a batch, schedule or stop emails. I can't
    send anything without your manual step."
  - History persisted in `localStorage["mf_chat"]` (last 30 messages) and re-rendered on load.
- **Input row**: textarea (rows 1, flex, vertical resize, placeholder `e.g. "Schedule the first
  5 rows of my newest sheet for tomorrow 10:30 as drafts"`) + **"Send"** (primary). Enter sends,
  Shift+Enter newlines.
- **Suggestion chips** (4, `.btn.sm`, click = send that text):
  1. "What's the status of my campaigns?"
  2. "Draft a polite Diwali follow-up template"
  3. "Schedule the first 5 rows of my newest sheet for tomorrow 10:30 as drafts"
  4. "How many emails did I send and how many replies came back?"
- **No AI key state**: chat log shows empty state ✦ — "AI is not set up yet. Add your API key in
  Settings (AI section), then come back." — and Send is disabled.

**Agent capabilities (10 function tools exposed to the model):**
`list_campaigns`, `create_campaign`, `list_templates`, `get_template`, `create_template`
(real HTML body with `{field}` placeholders), `list_sheets`, `queue_batch` (template + sheet or
main list + serial range; action `queue|draft|schedule` only — **immediate send is impossible**;
schedule requires a time ≥5 min ahead), `list_emails` (status filter incl. `replied`, `bounced`),
`cancel_emails`, `reschedule_emails`.
The model's system context includes: current user + role, safe-mode state, usable Gmail accounts,
all campaigns/templates/sheets (with fields & row counts), queue status counts, sent/replied/
bounced totals, last 5 sent (name + company + date), and local date/time. It must ask one short
clarifying question rather than guess missing details, and it is forbidden from markdown,
preambles, and claiming it sent mail.

### 5.11 Getting started — `/guide`

Page title: **Getting started**. Max-width 900 px. Screenshots auto-hide if they fail to load.

**Intro card**: h2 "Welcome to CampusMail AI" + copy: "CampusMail AI lets the outreach team write
one email, fill it automatically for every company on the sheet, and then either save everything
as Gmail drafts or schedule it to go out at an exact time. Nothing is ever sent without you
pressing a confirm button." + buttons **"Start a mail merge →"** (primary) and **"Jump to step
1"** (ghost anchor).

**9 numbered steps** (`.guide-step` card: 44 px number column — 19 px/800 primary on
`--primary-50`, right border — + body padding `0 22px 20px`; each has an h3, muted copy, a
screenshot box (10 px pad, `#f8f9fd` bg) and hints):

| # | Anchor | h3 | Key content |
|---|---|---|---|
| 1 | `#step1` | Sign in with your Google account | screenshot `01-login.png`; hint: admin can **invite your email** (Team admin) or **approve your request**; login remembered on this computer |
| 2 | `#step-google` | One-time setup: connect team sign-in (admin only, ~10 minutes) | screenshot `02-settings.png`; Google Cloud OAuth client instructions (linked from Settings) |
| 3 | `#step-gmail` | Connect a Gmail send account | screenshot `03-accounts.png`; Desktop-type OAuth JSON paste + connect flow; "You can connect more accounts (e.g. a second team mailbox) - each stays available in the dropdown wherever you choose 'send from'." |
| 4 | `#step4` | Load the company list (Contacts) | screenshot `04-contacts.png`; columns `Serial No. / Company / HR Name / HR Email` "other names work too - the platform guesses"; re-import updates rows |
| 5 | `#step5` | Add your email template | screenshot `05-templates.png`; Word fields in `{recruiter_name}` or `[Company]`; "Images in the Word file come along automatically"; "You can also let **✦ AI create a template** from a short brief" |
| 6 | `#step6` | Run the mail merge | screenshot `06-merge.png`; ⚠ copy says "**three steps**" (template → columns → preview → Generate) — the actual wizard has **five** steps |
| 7 | `#step7` | Draft, schedule or send | screenshot `07-queue.png`; 3 bullets: **Create drafts** / **Schedule send** (example `2026-11-12 10:30` + stagger + reminders note) / **Send now** ("only possible when Safe mode is off, and it always asks you to type SEND"); hint about the powered-on requirement + catch-up policy |
| 8 | `#step8` | Watch and adjust | screenshot `08-dashboard.png`; Dashboard pipeline, Activity log, Contacts statuses, Team admin |
| 9 | `#step9` | Track replies, follow-ups and reports | screenshot `09-reports.png`; auto reply detection; manual marking from Queue preview; "Other useful places: **Settings → Email signature**, **Mail Merge step 3** (attachments + follow-up reminders), **Templates** (AI can polish a template or write a reminder for you)" |

### 5.12 Activity — `/activity`

Page title: **Activity**. Single card, table `When (UTC) | Action (pill) | Actor | Detail` —
full audit log (every action is written with the person's name). Empty: "No activity yet."
No filters, search, or pagination.

### 5.13 Settings — `/settings`

Page title: **Settings**. Ten cards, stacked (first two in a `.grid.cols-2`):

**A. Gmail send-accounts** (sub "saved for the whole team")
- Account rows (`.acct-row`): 9 px dot (green = connected / gray = not), email (bold, 13.5 px),
  `(added by <owner>)` if not yours, red "not connected yet" if no token; right: **"shared with
  team"** checkbox (`.checkbox-row`, 12 px — enabled only for own accounts or admins; toggles
  shared/private visibility with toast "Now shared with the team" / "Now private to you") and
  **"Remove"** (sm ghost, own-or-admin; type-to-confirm REMOVE: "This removes the stored login
  for this Gmail account. Drafts already in Gmail are not affected.").
- Empty: "No Gmail send-accounts yet. Connect one below."
- Hint: "New accounts are **shared with the whole team** by default - anyone on the team can pick
  the account to draft or send from. Switch one to private in its row if you want it to stay yours
  alone."
- **"Connect a Gmail account"** (primary) → modal "Connect a Gmail account": "Step 1 of 2. Give
  the account a name, then we open Google in this tab - sign in with the account that should send,
  and allow access. It is saved here for the whole team." Fields: `Label (optional)` (placeholder
  "e.g. Suparna or the team mailbox"), checkbox "Keep private to me (hide from the team)".
  Footer: Cancel / **"Continue to Google"** → redirects to Google OAuth; the user lands on the
  `auth_done` interstitial (→ back to /settings).

**B. Sending rules**
- **Safe mode toggle** (40×22): label "Safe mode" + live description:
  - ON: "ON - only drafts can be created. Scheduled and immediate sending is blocked."
  - OFF: "OFF - scheduled and immediate sends are ARMED. Be careful."
  The toggle saves **immediately on click** (no Save button); toast uses **err styling when
  arming** ("Safe mode OFF - live sending armed") and ok styling when engaging.
- `Max live sends per day (all accounts)` (number, min 1, max-width 120; default 100)
- `Default stagger between emails (minutes)` (number, min 0)
- `If the platform was off when a send was due` — select: "Send it as soon as the worker runs
  again" (default) / "Mark as missed (error) instead"
- **"Save sending rules"** (primary). (Note: the Safe-mode toggle does not use this button.)

**C. Email signature** (sub "added to text/AI templates automatically")
- Hint: "Word/HTML templates already contain their own signature (like the campus invite). For
  plain-text and AI-generated templates, whatever you save here is appended at the bottom - text,
  colored text or an image all work. Upload a signature image to get its code, then paste it
  below."
- **"Upload signature image"** (+ hidden file input `.png,.jpg,.jpeg,.gif,.webp`) → appends the
  image HTML code into the box; note "Image code added to the box below - press Save signature."
- Signature HTML textarea (rows 4, placeholder `<p><b>Suparna Deshmukh</b> - Outreach Cell</p>`)
- **Live preview** (140 px iframe in a bordered box with a "Live preview" label; re-renders on
  every keystroke, Arial 12 px)
- **"Save signature"** (primary). Toast: "Signature saved - new text/AI templates will use it".

**D. Google sign-in (team login)** — **admin only** (sub "admin only")
- Hint: "Used on the login page. Step-by-step: **Getting started, step 1**" (link to `/guide#step-google`).
- `.grid.cols-3`: `Client ID` (placeholder `.apps.googleusercontent.com...`) · `Client secret`
  (password, placeholder `GOCSPX...`, spans 2 cols)
- `Admin email (this Google account is always the admin)` (max 340)
- `Team display name (shown on the login page + navigation)` (max 340) + hint: "Stored only on
  this deployment (never in the code or the repo). Blank = the neutral default. An environment
  variable `CAMPUSMAIL_AI_BRAND_TEAM` on a hosted deployment overrides this."
- `Login footer line (optional)` (max 340) + hint about `CAMPUSMAIL_AI_BRAND_FOOT`.
- **"Save Google sign-in"** (primary).

**E. Gmail API client (paste the JSON)** — **admin only**
- Hint linking to `/guide#step-gmail` ("The JSON you download from Google Cloud (OAuth client,
  Desktop type)").
- JSON textarea (rows 6, placeholder `{"installed":{"client_id":...}`); **"Save client JSON"**
  (validates JSON client-side: "That is not valid JSON - copy the whole downloaded file") + state
  chip: "Gmail client JSON is set" (green) / "Gmail client JSON missing" (warn) / "checking...".

**F. AI assistant** (sub "optional - key can stay blank")
- **Toggle** "Enable AI features (template writer, subject improver, field-mapping suggester)" —
  saves on click.
- `.grid.cols-4`: `Provider` select (**OpenAI / Groq / Google Gemini / Other (OpenAI-compatible)**)
  · `Base URL (fills itself from provider)` (spans 2; defaults per provider, e.g.
  `https://api.openai.com/v1`, `https://api.groq.com/openai/v1`) · `Model` (per-provider default,
  e.g. `gpt-4o-mini`)
- `API key (blank until you have one)` (password, max-width 460)
- **"Test AI connection"** (default) + **"Save AI settings"** (primary).

**G. Cloud scheduler (optional)** (sub "send scheduled emails even when this laptop is off")
- Hint: deploy `supabase/` once; paste relay address + secret; "When set, scheduled emails are sent
  from the cloud on the minute - this laptop can sleep. Stop / reschedule / delete / save-as-draft
  still work right here and are mirrored to the cloud automatically. Leave blank to run 100%
  locally."
- `.grid.cols-2`: `Relay URL` (placeholder `https://YOUR-PROJECT-REF.functions.supabase.co/relay`)
  · `Relay secret` (password)
- **"Save relay settings"** (primary) + state chip (`relay off` / on).
- Divider, then first-time setup row: "First-time setup (admin):" + Gmail account select (min
  260 px, "choose the Gmail account…") + **"Show relay setup values"** (modal listing 5
  `name=value` rows with per-row **Copy** buttons + "Copy all (name=value)").

**H. File storage (optional)** (sub "keep attachments & images off this machine's disk")
- Hint: local disk by default; switch to **Supabase Storage** (~1 GB free, private bucket e.g.
  `campusmail-files`); "Switching back to local disk does not move files back - they stay in the
  bucket."
- `.grid.cols-2`: `Backend` select (local disk / Supabase Storage) · `Bucket name`
- `.grid.cols-2`: `Supabase project URL` (placeholder `https://xxxxxxxx.supabase.co`) ·
  `Service role key` (password, "leave blank to keep current")
- **"Save storage settings"** (primary) · **"Move existing files to Supabase"** (shown only in
  Supabase mode) · state chip (`local disk`).

**I. Deployment** (sub "hosted instance") — **hidden by default**; shown only on hosted
deployments: hint "Pull the latest code from the GitHub repo and restart this instance to load
it." + **"Deploy latest code"** (primary).

**J. Danger zone** (card border `#fecaca`, red title)
- Muted: "Empty the entire email queue (generated emails only - drafts already in Gmail are
  untouched)." + **"Empty queue"** (danger, right-aligned). Type-to-confirm **CONFIRM** —
  "Removes ALL generated emails from the queue (drafts already in Gmail are not affected)."
  Deletes all emails row-by-row; toast "Queue emptied". Never touches Gmail.

### 5.14 Team admin — `/admin` (**admin only**; nav item "♥ Team admin")

Page title: **Team admin**.

- Info banner: "Everything a team member does is recorded in **Activity** (link). Saved Gmail
  send-accounts stay with the platform for the whole team."
- `.grid.cols-2`:
  - **Pending access requests**: one row per request: `email` (bold) + ` - display name` (muted),
    optional request note in quotes (hint style), right: **"Approve"** (sm primary) / **"Decline"**
    (sm). Empty: "No pending requests." Toasts: "Approved" / "Declined".
  - **Invite a member** (sub "They get in immediately with that Google account"): `Email address
    (their work or Google account)` (placeholder `name@your-institution.edu`) · `Display name
    (optional)` · **"Invite to the team"** (primary) + hint "Invited members sign in from the
    login page with that Google account - no request needed." Toasts: "Invited - they can sign in
    now" / "Already a member - access re-enabled".
- **Team members** table: `Email | Name | Role | Status | Joined (UTC) | [actions]`
  - Role: `admin` (pill, drafted-style) or `member` (pill, new-style)
  - Status: `active` (sent-style green pill) / `pending` (scheduled-style amber pill) / anything
    else → error-style pill
  - Actions: **"Make admin"** / **"Make member"** (sm ghost, role toggle) + **"Remove"** (sm
    ghost; type-to-confirm REMOVE: "Remove **<email>** from the platform? Their personal
    preferences are removed; team data and saved send-accounts remain.").

---

## 6. End-to-end user journeys (current state, step by step)

**J1 — First-run setup (admin, ~10–15 min, outside the UI mostly)**
1. `./run.sh` / `run.bat` → app at `http://localhost:5001` (first run creates venv, installs deps).
2. Google Cloud console: create project → OAuth consent screen (External) → add scopes
   `openid/email/profile` + test users → create **Web** OAuth client with redirect URI exactly
   `http://localhost:5001/api/google/callback` → copy client ID + secret.
3. Login page → **Settings → Google sign-in (team login)** → paste ID + secret, set **Admin
   email**, optionally team display name + login footer line → Save. (Until this is set, the login
   page only shows the "not set up yet" warning.)
4. Google Cloud: enable **Gmail API** → create **Desktop** OAuth client → download JSON →
   **Settings → Gmail API client** → paste whole JSON → Save.
5. **Settings → Gmail send-accounts → Connect a Gmail account** → label → Continue to Google →
   sign in + allow → land on `auth_done` → back to Settings. Account now shared with the team.
6. *(Optional)* **Settings → AI assistant** → provider/model/key → Test.
7. *(Optional)* **Settings → Cloud scheduler** → deploy `supabase/` → "Show relay setup values"
   → paste 5 values into Supabase → paste Relay URL + secret back → Save.

**J2 — Teammate joins (request flow)**
1. Teammate opens URL → **Sign in with Google** (institutional account).
2. Lands on login page state "Request under review" (⏳) — optional note textarea → "Send note".
3. Meanwhile the teammate is **not** in the app; if they had accessed the app they'd work in their
   personal workspace with the pending banner.
4. Admin → **Team admin** → Pending access requests → **Approve** (or **Decline**).
5. Teammate → "Check again" → ✅ "Welcome" → Continue → shared workspace.
   *(Alternative:* admin **Invites** the email in Team admin → teammate signs in straight in, no
   request.)*

**J3 — Load the company list**
1. Nav → **Contacts** → **Import sheet** → choose `.csv`/`.xlsx`.
2. Toast "Imported: X added, Y updated (Z skipped)". Table shows Serial/Company/HR name/HR email/
   status; search + status filter; pagination 50/page; per-row Edit (company, name, email, notes);
   multi-select delete (type DELETE); Export CSV; manual Add contact.

**J4 — Create a template (3 entry paths)**
- A: **Templates → Upload template** → name + subject + `.docx`/`.html`/`.txt` file or pasted body
  → fields auto-detected (toast lists them).
- B: **✦ AI: create a template** → name + brief + tone → Generate.
- C: After upload, **Edit** (wide modal, Word-like editor + toolbar + image resize bar) → Save.
- **Preview** renders with the first contact; **Set active** marks the default template; **✦ AI
  polish** rewrites in place; Delete (type DELETE).
- Reminder templates are the same object type: upload or **✦ AI: write reminder template**.

**J5 — Run a mail merge (the core journey)**
1. Nav → **Mail Merge** (or "New mail merge" from Dashboard / "Start mail merge" from a Campaign).
2. **Step 1**: pick (or create) campaign → Next.
3. **Step 2**: pick template (active pre-selected) or upload one inline → Next.
4. **Step 3**: data source — main contact list, an uploaded sheet (select), or upload a new sheet
  inline (summary + missing-email warning) → Next.
5. **Step 4**: for each template field choose Use column / Constant / Blank (server suggestion
  pre-filled; sample-value hints; ✦ Suggest with AI optional) → Preview.
6. **Step 5**: review 3 real previews; optionally append signature, add attachments (≤10 files,
  ≤15 MB), configure auto-reminder (days / template / max 1–3, or AI-write one); set serial
  range, CC list, **After generating** (queue / draft / schedule / send); if schedule → time +
  stagger; if send → red "cannot be undone" warning; batch name + send-from account.
7. **Generate** → type CONFIRM (or SEND) → toast "Batch created - N emails ..." → **auto-redirect
   to /queue**.

**J6 — Drafts workflow (Safe mode ON, the default)**
1. Queue page (already there after J5, or nav) → tab **Pending (N)**.
2. Optionally check rows (empty selection = all pending in tab) → pick **Send from** account.
3. **Create drafts** → type CONFIRM → toast "N drafts created" → rows become `drafted`.
4. Human reviews/edits/sends from **Gmail Drafts** (outside the app).
5. Rows stay `drafted` in the app; can be scheduled later, stopped (type STOP), or removed.

**J7 — Scheduled sends**
- **Path A (from the wizard, J5 step 6-7):** pick "Schedule the send at a time" → time + stagger →
  CONFIRM → rows `scheduled`.
- **Path B (from Queue):** select pending/drafted rows (or tab + empty selection) → **Schedule /
  Reschedule** → modal (time, stagger, live "last one goes out at" estimate) → Schedule →
  `scheduled`. Single-row **Reschedule** on scheduled rows uses the same modal.
- **Local mode:** the worker (tick ≤8 s) sends due emails; Queue's worker bar shows running /
  due-now / next fire / last error; "Run worker now" forces a tick.
- **Cloud mode:** same UI; the Supabase relay fires at the minute (≤6 emails + 6 reminders/min);
  local actions (stop/reschedule/delete/save-as-draft) mirror to the cloud instantly.
- **Catch-up:** if the app was off at send time → per Settings: send on next run (default) or mark
  as missed (error).

**J8 — Send now (live, irreversible)**
1. **Settings → Sending rules** → flip **Safe mode** toggle OFF (immediate; "armed" toast).
2. Queue → select rows (or pending default) + account → **Send now** (danger, enabled) → type
   **SEND** → toast "N emails sent". Subject to the per-day cap (default 100/account-set).

**J9 — Replies & follow-up conversation**
1. Worker auto-detects recruiter replies (and bounce DSNs) for sent mails → row shows `replied`
   (green) / bounce flag; contact status updates.
2. Manual: Queue → row **View** (wide modal) → for sent mails **Mark as replied / Mark as NOT
   replied** (toggles `replied_at`; also stops cloud reminders).
3. Queue → row **Reply** → modal prefilled "Re: subject" → write or **✦ Draft with AI** →
   **Send reply** (live) or **Save as draft** (safe mode).

**J10 — Automatic reminders**
1. Configured in the wizard step 5 (checkbox + days + reminder template + max) or after batch
   creation via `/api/batches/<id>/reminders`.
2. Per sent email, when `sent_at + N × reminder_days` passes with no reply, reminder N is queued
   and sent (local worker or cloud), rendered from the batch's reminder template with the
   recipient's fields. `is_reminder` rows show "reminder of" in the When column;
   `reminders sent: N` accumulates; marking replied skips remaining reminders.
3. Reminders respect Safe mode and the daily cap.

**J11 — Reporting & export**
1. Nav → **Outreach Reports** → 5 summary cards, 14-day send chart, pipeline buckets, top
   companies, per-member table, full email table with search/member/status filters.
2. **Export CSV** downloads the filtered email table. Contact list CSV export on the Contacts
   page.

**J12 — AI assistant actions**
1. Nav → **AI Assistant** → type or click a suggestion.
2. Model answers in plain text and/or calls tools (create campaign/template, queue/draft/schedule
   a batch from a serial range, cancel/reschedule, list status). Each tool call renders as a
   `⚙ tool → result` chip under the bubble.
3. Immediate send is impossible via the assistant (tool set excludes it); safe mode still applies
   to whatever it schedules.

**J13 — Team administration**
1. **Team admin** → approve/decline pending requests; invite by email (immediate access).
2. Member table: make admin/member; remove (type REMOVE) — personal prefs removed, team data +
   send-accounts remain.
3. Admin can also set the admin email, branding (team name + login footer), Google client
   credentials, and Gmail client JSON in Settings (admin-only cards D & E).

**J14 — Personal workspace (non-team users)**
1. Non-invited Google user signs in → request auto-filed → works in an **isolated personal
   workspace** (own db in `data/workspaces/`) with the pending/denied banner at the top of every
   page; can add a note to the request from the banner.
2. On approval → same Google sign-in now lands in the **shared team workspace** (their personal
   workspace data does not merge).

**J15 — Cleanup / danger**
1. **Settings → Danger zone → Empty queue** → type CONFIRM → all generated queue emails deleted
   (Gmail drafts untouched).
2. Per-row/per-batch removals: Queue Remove (DELETE), account Remove (REMOVE), sheet Delete
   (DELETE), template Delete (DELETE), contacts Delete selected (DELETE).

---

## 7. Data model & status vocabularies (as seen in the UI)

**Email statuses** (the `status` field of table `emails`):
`pending` → `drafted` | `scheduled` → `sent` → (`replied` flag via `replied_at`) | `error` (with
`error` text); plus `cancelled` (Stopped tab) and `bounced` (flag `bounced_at` + `bounce_reason`
on sent mails; counted separately in reports).
`emails` fields: id, batch_id, contact_id, serial, to_addr, subject, body_html, status,
scheduled_at, sent_at, error, account_id, message_id, replied_at, bounced_at, bounce_reason,
reminder_count, is_reminder, created_at, updated_at.

**Contact statuses** (derived, shown in Contacts filter + pills):
`new, drafted, scheduled, sent, replied, error`.

**User model**: Google `email` (identity), `display_name`, `role` (`admin` | `user`),
`status` (`pending` | `active` | denied), `request_note`, `created_at`. Admin email setting is
always-admin.

**Gmail accounts**: owner, `label`, `email`, connected (token present), `visibility`
(`shared` | `private`), `last_account_id` (preferred send-from).

**Other tables**: `settings` (kv: safe_mode, max_sends_per_day, default_stagger_min,
catchup_policy, ai_*, relay_*, storage_*, google_client_*, admin_email, brand_team_name,
brand_tagline, client_secret_json, signature html...), `email_templates` (name, subject,
body_html, fields json, is_active), `batches` (name, campaign_id, sheet_id, account_id,
append_signature, cc, reminder config), `campaigns` (name, description, created_by), `sheets`
(name, headers json, rows, campaign_id), `activity` (ts, action, actor, detail),
`processed_dsn`, `user_settings`, `sync_deletes`.

---

## 8. API surface (what the UI can do, grouped)

- **Pages**: `/` (dashboard) · `/login` · `/logout` (POST) · `/admin` · `/campaigns` ·
  `/campaigns/<id>` · `/contacts` · `/templates` · `/merge` · `/queue` · `/reports` ·
  `/activity` · `/settings` · `/guide` · `/assistant` · `/local_img/<name>` (template images)
- **Auth**: `/api/google/start` · `/api/google/callback` · `/api/google/callback/request`
  (POST: join-request note) · `/api/login/state`
- **Admin**: `/api/admin/data` · `/api/admin/requests` (POST approve/deny) ·
  `/api/admin/members` (POST role/remove) · `/api/admin/invite` (POST)
- **State**: `/api/state` (15 s poll: settings, me, gmail accounts, scheduler, stats,
  active_template, onboard checklist, ai_has_key)
- **Accounts**: `/api/accounts` · `/api/accounts/add` (POST) · `/api/auth/callback` ·
  `/api/accounts/<id>` (POST visibility) · `/api/accounts/<id>/delete` (POST)
- **Contacts**: `/api/contacts` (GET q/status, POST add) · `/api/contacts/import` (POST) ·
  `/api/contacts/<id>` (POST update) · `/api/contacts/delete` (POST) ·
  `/api/export/contacts.csv`
- **Templates**: `/api/templates` (GET) · `/api/templates/upload` (POST) ·
  `/api/templates/ai` (POST) · `/api/templates/ai-reminder` (POST) · `/api/templates/<id>`
  (POST save) · `/api/templates/<id>/activate` · `/api/templates/<id>/delete` ·
  `/api/templates/<id>/preview` · `/api/templates/<id>/polish` (POST)
- **Merge**: `/api/merge/suggest` (GET) · `/api/merge/preview` (POST) · `/api/merge/generate`
  (POST)
- **Campaigns/sheets/batches**: `/api/campaigns` (GET/POST) · `/api/campaigns/<id>` (GET) ·
  `/api/sheets/upload` (POST) · `/api/sheets` (GET) · `/api/sheets/<id>` (GET) ·
  `/api/sheets/<id>/delete` (POST) · `/api/batches` (GET) · `/api/batches/<id>` (GET) ·
  `/api/batches/<id>/attachments` (POST/DELETE) · `/api/batches/<id>/reminders` (POST)
- **Emails/queue**: `/api/emails` (GET status filter) · `/api/emails/view` (GET) ·
  `/api/emails/draft` (POST) · `/api/emails/schedule` (POST) · `/api/emails/send-now` (POST) ·
  `/api/emails/delete` (POST) · `/api/emails/cancel` (POST) · `/api/emails/<id>` (POST update) ·
  `/api/emails/<id>/reply` (POST) · `/api/emails/<id>/reply-suggest` (POST) ·
  `/api/emails/<id>/polish` (POST) · `/api/emails/<id>/mark-replied` (POST)
- **Scheduler**: `/api/scheduler/trigger` (POST)
- **Reports**: `/api/reports/members` · `/api/reports/emails` (GET q/status/member) ·
  `/api/reports/emails.csv` · `/api/reports/summary` (GET member)
- **Settings/infra**: `/api/settings` (GET/POST) · `/api/settings/relay-pack` (GET) ·
  `/api/storage` (GET) · `/api/storage/migrate` (POST) · `/api/deploy` (POST)
- **AI**: `/api/ai/test` (POST) · `/api/ai/subjects` (POST) · `/api/ai/mapping` (POST) ·
  `/api/ai/chat` (POST, agentic)
- **Misc**: `/api/activity` (GET) · `/api/signature` (GET/POST) · `/api/signature/upload` (POST)

---

## 9. Flow-length census (neutral step counts, for the audit)

Counted as *user interactions* (click/keystroke groups; a type-to-confirm counts as its own step;
navigation clicks count):

| Journey | Path | Interactions (approx) |
|---|---|---|
| Admin first send (from zero) | J1+J3+J4+J5+J6 | ~25–30 (setup dominates) |
| New batch, all data exists | J5 → J6 | 5 wizard steps + Generate + CONFIRM + Create drafts + CONFIRM ≈ **9** |
| Draft pending rows already in queue | J6 | select + Create drafts + CONFIRM ≈ **3** |
| Schedule pending rows | J7-B | select + Schedule modal + Schedule ≈ **3** |
| Reschedule one scheduled row | J7-B | Reschedule + modal + Schedule ≈ **3** |
| Stop one scheduled row | J7 | Stop + type STOP ≈ **2** |
| Send now (safe mode already OFF) | J8 | select + Send now + type SEND ≈ **3** |
| Send now (safe mode ON) | J8 | Settings → toggle + select + Send now + type SEND ≈ **4–5** (Settings is a different page) |
| Mark a sent mail replied | J9 | View + Mark as replied ≈ **2** |
| Reply to a recruiter | J9 | Reply + (AI draft) + Send ≈ **2–3** |
| Turn off safe mode | — | 1 click (Settings) |
| Empty the queue | J15 | Settings → Empty queue + type CONFIRM ≈ **2** |
| Invite a teammate | J13 | Team admin → email + Invite ≈ **2** |
| Approve a request | J2 | Team admin → Approve ≈ **1** |
| Export full email report | J11 | 1 click |
| Ask AI for status | J12 | 1 message |

**Observations that follow from the table (facts, not verdicts):**
- The wizard is the only 5-step funnel in the app; steps 1–4 are each one decision each, but
  step 5 packs **9 distinct decisions** (preview, signature, attachments, reminder ×4, range, CC,
  action, time/stagger, name/account) into one screen.
- Safe mode lives on a different page than the send button (Settings vs Queue).
- There is no direct "draft the whole campaign" one-click from Campaigns; the shortest
  campaign→drafts path is Campaigns → Start mail merge → wizard → Queue → Create drafts.
- Queue has no text search (Reports has one); Activity has no filters; Contacts is the only
  paginated table (50/page); no table is sortable.
- Type-to-confirm is required for: generate batch (CONFIRM/SEND), create drafts (CONFIRM),
  stop (STOP), send now (SEND), every delete/remove (DELETE/REMOVE), empty queue (CONFIRM).
  Scheduling and reply-sending have **no** typed confirmation.
- The wizard does not remember progress if you leave the page (no URL step state).
- Bulk actions fall back to "all pending in current tab" when nothing is selected.

---

## 10. Facts & inconsistencies found in the current code (verify before auditing)

1. **Glyph collision**: Mail Merge and Settings both use the gear glyph `&#9881;`.
2. **Stale guide copy**: Getting started step 6 says the merge has "**three steps**"; the wizard
   has five (Campaign, Template, Data source, Map fields, Preview & generate).
3. **Dashboard stat grid**: 10 cards in a 4-col grid → an unbalanced 3rd row of 2; Reports has 5
   cards in 4 cols → a lone 5th card.
4. **Reports status filter** lacks `cancelled` (Stopped), which the Queue tabs expose.
5. **Contacts status filter** lacks `cancelled`/`bounced` too (contact statuses don't include
   them, but bounced mails' contacts stay `sent`).
6. **Safe-mode toggle** saves instantly (no Save button next to it) while the other Sending-rules
   fields need "Save sending rules" — two saving mechanisms in one card.
7. **Green dot semantics**: in the sidebar, the green dot means *live sending armed* (dangerous
   state = green); the safe state shows a gray dot.
8. **Arming safe-mode-off uses an error-styled toast** (red) — a warning styled as an error.
9. **"Send now" in Queue** is the only place live sending is possible outside the wizard's
   "After generating = send" option (which requires Safe mode OFF and the SEND word).
10. **Login page**: no email/password fallback; if Google isn't configured the page shows only a
    warning banner with no admin shortcut.
11. **Wizard step 5 attachments** are selected client-side only until Generate (uploaded
    post-batch-creation); leaving the page loses them.
12. **Campaign detail "Queue" link** goes to the global queue, not a campaign-filtered queue
    (queue has no campaign filter).
13. **No campaign filter** exists anywhere on the Queue page (only on Reports via member).
14. **Modal widths**: only 3 of ~15 modals use the 900 px wide variant; long email subjects/
    bodies in narrow modals wrap heavily.
15. **`Send from` select** appears in 3 places (wizard, Queue, Settings relay pack) — each
    independently populated; "(mine)" tag only in the shared picker.
16. **Activity page** has no filter by actor/action and no pagination (single table, all rows).
17. **Queue tab labels** use plural "Errors" and "Stopped" for the statuses `error` and
    `cancelled`, while every pill elsewhere shows the singular status word — two different names
    for the same state (tab "Stopped" = pill "cancelled").
18. **Two different AI reminder modals exist** (Templates page and wizard step 5) with nearly
    identical fields and different endpoint calls to the same API.
19. **Reply modal** switches its confirm button label between "Save as draft" and "Send reply"
    based on safe mode — but the button style is the same (primary).
20. **Wizard "After generating = send"** option label says "(Safe mode must be OFF)" but does not
    disable itself when Safe mode is ON; the attempt surfaces as an error later.
21. **Topbar chips** can render 4 chips at once on a narrow viewport with no overflow handling.
22. **`max-width:1280px` content** inside an unbounded main area → on wide monitors content is
    left-aligned with large right whitespace (no centering).
23. **No dark mode**, no i18n, all copy is English; times in tables are local (labels say UTC in
    Activity/Dashboard headers but values render via `toLocaleString()` in the user's locale).
24. **Contact "Status" column** and the Queue "Status" column use different status vocabularies
    with overlapping names (e.g. `new` vs `pending`).
25. **First-run chicken-and-egg**: until Google sign-in is configured, the login page shows only
    a warning banner — but Settings (where the credentials are pasted) requires a signed-in
    session, and every app page is login-gated. The only non-UI escape hatch is the environment
    variable `CAMPUSMAIL_AI_DEV_LOGIN`, which auto-creates a "Developer Login" admin row. The
    README's "first person to sign in becomes admin" therefore presumes Google credentials were
    set up out of band.

---

## 11. Quick reference: every button in the app (by label)

| Screen | Buttons (exact labels) |
|---|---|
| Shell (sidebar) | Sign out |
| Shell (banner, personal ws) | Send note |
| Login | Sign in with Google · Try again · Send note · Check again · Continue |
| auth_done | Back to CampusMail AI |
| Dashboard | New mail merge · Manage · View all · Next: {item} → |
| Contacts | Import sheet · Export CSV · Add contact · Delete selected (N) · Edit · ← Prev · Next → |
| Campaigns (list) | + New campaign · Open · Start mail merge |
| Campaigns (detail) | + Start mail merge in this campaign · Queue · Delete (sheet) |
| Templates | ✦ AI: create a template · ✦ AI: write reminder template · Upload template · Edit · Preview · ✦ AI polish · Set active · Delete |
| Templates modals | Cancel/Upload · Cancel/Save changes · ✦ Generate · Cancel/Close |
| Merge wizard | Create & use · Next: pick template → · ⬆ Upload a new template · ← Back · Next: who to write to → · Upload sheet · Next: map fields → · ✦ Suggest with AI · Preview → · Add files · ✦ AI: write a reminder template · ← Back · Generate |
| Queue | Run worker now · 8 tabs (All/Pending/Drafted/Scheduled/Sent/Replied/Errors/Stopped) · Create drafts · Schedule / Reschedule · Stop · Send now · row: View/Stop/Reschedule/Reply/Remove · modal: Mark as replied / Mark as NOT replied · ✦ AI: polish this email · ✦ Draft with AI · Save as draft / Send reply · Save polished version · Schedule · Run worker now |
| Reports | Export CSV |
| AI Assistant | Send · 4 suggestion chips |
| Guide | Start a mail merge → · Jump to step 1 |
| Settings | Connect a Gmail account · Remove (acct) · Save sending rules · Upload signature image · Save signature · Save Google sign-in · Save client JSON · Test AI connection · Save AI settings · Save relay settings · Show relay setup values · Save storage settings · Move existing files to Supabase · Deploy latest code · Empty queue |
| Team admin | Approve · Decline · Invite to the team · Make admin / Make member · Remove |
| Global (modals) | Cancel · {Confirm word} (Generate / Create drafts / Stop / Send now / Delete / Remove / Schedule) · Copy · Copy all (name=value) |

---

*End of specification. Source: `/Users/apple/campusmail-ai` — `app.py` (3,028 lines),
`db.py` (535), `static/js/app.js` (1,680), `static/css/app.css` (356), 14 × `ui/*.html`,
`README.md`. Re-verify any quoted copy against the files before quoting it back at the codebase.*
