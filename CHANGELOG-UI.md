# UI redesign — changelog

Branch `redesign`, 2026-11-14. Phases 1–2 of the redesign build plan
(`CampusMail AI Redesign Build Plan.md`): the new shell, Home, Campaigns list,
the campaign hub (Overview / Emails / Audience), the email drawer, and the
shared time picker. All backend changes are additive (plan §1): no old
endpoint, column or template was renamed or removed.

## Second pass, 2026-11-14 (demo retheme, black dark mode, UI/UX audit)

Full screenshot pass over every screen (desktop light/dark, 390 px phones,
menus, drawers, pickers, popovers, empty and error states), with DOM
cross-checks for each capture.

- **Demo data is the Sem 2 Placement Campaign.** The seeded main campaign is
  now "Sem 2 Placement Campaign" (template "Sem 2 placement outreach",
  batches "Sem 2 placement wave 1/2"), and every seeded email body is a
  realistic 3-paragraph Sem 2 placement-drive mail. The other two campaigns
  (Winter internships, Alumni network) are unchanged.
- **Dark mode is neutral black, not purple.** The previous dark tokens had a
  purple tint; the palette is now a professional near-black
  (`#0A0B0E` background, `#141519` / `#1C1E24` surfaces, `#272A31` borders,
  `#F4F5F7` / `#A8ACB5` / `#7E838E` text) with the indigo accent kept for
  primary actions and focus, Linear-style. All new pages are token-driven,
  so the change lands in `static/css/tokens.css` alone; the legacy pages
  keep `legacy.css` until Phase 8.
- **Bug fixes found in the audit**
  - `static/js/pages/emails.js` `targetRows` read `s.state` before the
    state argument existed on every checkbox click ("Cannot read
    properties of null (reading 'state')"); it now takes the state from
    the caller.
  - `static/js/core/ui.js` `menu()` read `it.text`, but most call sites
    pass `it.label` - every kebab menu (hub header, row kebab, drawer
    kebab, audience kebab, add-list) rendered icons with no labels. The
    reader now accepts both.
  - At 390 px the emails toolbar selects (`#em-batch`, `#em-by`,
    `#em-sort`) shrank below their widths instead of wrapping - one
    rendered as a bare dropdown arrow. `flex: none` on the toolbar
    selects; they now wrap onto their own lines.
  - At 390 px the campaigns table compressed eight columns into 356 px:
    the status badge clipped and the numeric columns were unusable.
    Contacts / Sent / Replied / Reply rate / Last activity are hidden at
    <=767 px (`camp-hide-m`, matching the app's existing stacked-card
    mobile pattern), the row "Send emails" button hides with the kebab
    (the card is still tappable to open), and campaign name + note get a
    single-line ellipsis.
  - The drawer timeline showed "just now" for *future* scheduled times
    (`relTime` clamps negative deltas). The "Scheduled for" node and the
    table's When column now use `upcomingWhen` ("Today 14:12" /
    "Tomorrow 14:12" / "Tue 14:12"), and the duplicated caption under the
    node is gone.
  - Every page load logged a `/favicon.ico` 404. Added
    `static/favicon.svg` (the M mark) and `link rel=icon` in
    `base.html`, `base_new.html` and `kit.html`; the full-page sweep is
    now zero-error on all 14 routes.
- **Screenshots** in `docs/screenshots/` re-captured at the current state
  (19 files; campaign ids re-fetched after the reseed).


## What is in

- **Shell (Phase 1).** New template `ui/base_new.html`: 248 px sidebar (5 main
  items, mode card, user block), 60 px sticky topbar with search palette,
  theme switch and system-status popover; icon rail at ≤1100 px; phone
  drawer + bottom tab bar + hamburger at ≤768 px. Design tokens in
  `static/css/tokens.css`, base styles in `static/css/base.css`, layout in
  `static/css/layout.css`, component styles in `static/css/components.css`.
  Inter variable font (88 KB, ligatures off) in `static/fonts/`; monoline SVG
  icon sprite in `static/icons/sprite.svg`.
- **Home** (`ui/home.html`, `static/js/pages/home.js`): greeting + one-line
  attention summary, four stat tiles with count-up, upcoming sends, open
  replies, campaign cards. Skeleton / empty / error states.
- **Campaigns list** (`ui/campaigns_new.html`, `static/js/pages/campaigns.js`):
  status segments with counts, search, table with per-campaign status.
- **Campaign hub (Phase 2, `ui/campaign_hub.html` + `static/js/pages/hub.js`)**.
  - Overview (plan 7.3): exactly one next-step banner (8 rules, first match
    wins), four stat tiles linking into filtered Emails views, contacts →
    emailed → replied funnel, 14-day sends-per-day chart, "where every email
    stands" segment bar, recent replies, per-account send mix, recent
    activity.
  - Emails tab (plan 7.4, `static/js/pages/emails.js`, the old Queue):
    status segments with live counts (zero-count chips hidden except All),
    search with 250 ms debounce and `/` shortcut, send / member / sort
    selects, URL-addressable state (`?tab=emails&status=&q=&batch=&sort=`),
    next-send caption, engine-overdue banner with Run now, 50-row paging,
    floating selection bar, per-row and bulk actions at the right
    confirmation tier (drafts, schedule/reschedule, send now with
    hold-to-confirm + 10 s cancel, stop, remove, mark replied/not replied).
  - Email drawer (plan 7.5): timeline, white email panel (sandboxed iframe),
    attachment chips, facts block, failed/bounced banners, status-aware
    footer actions, reply composer (auto-drafts in Drafts-only mode).
  - Audience (plan 7.6): list table with column chips, upload with
    `campaign_id`, attach/detach existing lists, remove from campaign.
  - Header: click-to-rename title, campaign status badge, note, kebab with
    Export CSV (client-side) and Delete (typed confirmation when sent
    emails exist).
- **Shared time picker** (`static/js/components/timepicker.js`, plan 8.4):
  future-only quick chips, date + 15-minute time, zone caption, "at least 5
  minutes ahead" inline validation, space-them-out (All at once / 1 / 2 /
  5 min / custom) with live "first at …, last at …" line, daily-limit meter
  from `/api/state`.
- **Bug fixes found during this work**
  - `static/js/core/ui.js` used `statusOf` without importing it from
    `copy.js` — every email-table row crashed on render. Import added.
  - `static/js/pages/home.js` `load()` queried template nodes
    (`#home-greet`, …) after `skeletons()` had wiped `#home-root`, so Home
    rendered blank. `load()` now rebuilds the static shell before filling it.
  - `app.py` `page_campaign_detail` did not pass `cid` to the template, so
    the hub script received `undefined`.
  - `static/js/core/shell.js` referenced an undefined `shortcutsModal` in
    `initShell()`, which aborted shell init before state polling started.
    `shortcutsModal()` added and wired to the Help button.
  - `scripts/seed_demo.py` seeded emails with `contact_id = row index`
    instead of real contact IDs; it now records `last_insert_rowid()` per
    sheet, so reseeding works after contact autoincrement drift. It also
    wrote a literal `...` placeholder into every email body; it now writes
    a short realistic email so the drawer preview is meaningful.
  - The email drawer read `full.company` / `full.hr_name` / `full.body_html`
    from `GET /api/emails/view`, but that endpoint returns no
    company/hr_name and exposes the body as `body` — so the drawer title
    fell back to the raw address and the email panel was empty. The title
    and sub now come from the table row (`seedRow`), and the panel uses
    `full.body`.
  - `static/js/core/ui.js` `drawer()` referenced `close` in the head
    button's `onclick` before its `const` declaration (temporal dead zone)
    — every drawer crashed on open. The handler is now a closure.
  - `ui/kit.html` imported `openPalette` from `ui.js` (it is exported by
    `shell.js`) and read `r[7]` in its demo table instead of `r[6]` — the
    kit page failed to load its module. Both fixed.

## Deviations from the plan (with reasons)

- **`base_new.html` naming.** The new shell template is `base_new.html`;
  `base.html` still serves the legacy pages. The rename to `base.html`
  happens in Phase 8, when the legacy pages are retired, so both can never
  collide mid-rollout.
- **`legacy.css`** carries the transition styles for old pages (login,
  settings, merge, queue, …) so they keep their current look until each is
  redesigned; new pages never load it.
- **`/_kit` dev gallery.** Component gallery accessible without a session
  (dev-only QA tool; the plan is silent on it). Remove or gate before any
  external exposure.
- **`/replies`, `/library`, `/team`** render friendly "arriving soon"
  placeholders (their real pages are Phases 3–4). `/send` and
  `/campaigns/<id>/send` redirect to `/merge` (the mail-merge wizard) until
  the 3-step send flow lands in Phase 3, per plan §13.
- **`/queue` and `/reports` already redirect to `/campaigns`** (early
  redirects, plan §3).
- **Schedule Undo** uses the new `POST /api/emails/revert` helper, which
  restores each email's prior status and `scheduled_at` exactly; the plan
  did not specify the undo mechanism.
- **Recent activity** in the hub is app-wide: activity rows have no
  campaign id, and adding one was out of scope.
- **Emails "Sent" segment and filter show pure sent only** (replied and
  bounced are their own segments); the API `status=sent` returns raw sent,
  and the tab filters out replied/bounced client-side so segment counts and
  table never disagree.
- **CSV export** is generated client-side from the loaded rows (no new
  backend endpoint).
- **Phone tables use the stacked-card pattern** (plan §12): `components.css`
  stacks rows into tappable cards at <=767 px (thead hidden, row kebab and
  checkbox hidden, row tap opens the drawer). The campaigns table additionally
  hides its secondary numeric columns at that width (second pass, above) so
  each card stays one compact unit.

## Decisions where the plan was silent

- **Dev hooks:** `?theme=light|dark` forces the theme (for screenshots and
  QA); on the hub, `?debug=drawer` / `?debug=picker` auto-opens the email
  drawer / time picker. Harmless in normal use, but intended for
  development.
- **Segment and hub counts** are computed server-side in
  `GET /api/campaigns/<id>` (`summary`, `per_day`, `recent_replies`) so the
  hub and the emails tab can never disagree; the client renders what the
  API says.
- **Font:** Inter var, single 88 KB woff2, `font-feature-settings: "liga" 0,
  "calt" 0` (no ligatures, per plan typography notes); the plan did not fix
  a file or weight strategy.
- **Number formatting** is en-IN grouping via `fmtInt` everywhere
  (glossary: "No." for serial, "list" for sheet, "send" for batch,
  "space them out" for stagger, "sending engine" for worker).

## Backend additions (for review)

All additive; existing behaviour untouched.

- `GET /api/campaigns/<cid>` response extended: `summary`
  (`n_emails, n_ready, n_drafted, n_scheduled, n_sent` (pure), `n_replied,
  n_bounced, n_failed, n_stopped, n_sent_unreplied, last_sent_at,
  next_scheduled_at`), `per_day` (sent counts by local date), `recent_replies`
  (5, with company/hr_name).
- `GET /api/emails`: rows now include `company` and `hr_name`
  (unconditional `LEFT JOIN contacts`); new `by` filter (batch `created_by`,
  requires `campaign_id`).
- `POST /api/campaigns/<cid>` — rename / set note.
- `POST /api/campaigns/<cid>/delete` — delete campaign (refused by the UI
  tier when sent emails exist unless confirmed).
- `POST /api/sheets/practice` — create the practice list.
- `POST /api/sheets/<sid>/campaign` — attach/detach a list from a campaign.
- `POST /api/emails/revert` — `{items: [{id, status, scheduled_at}]}`;
  restores prior status/time; only touches pending/scheduled/cancelled
  rows (the Undo helper for Schedule).

No new database columns in this branch (`emails.handled_at` and the
`/api/emails/<id>/handled` endpoint are Phase 3, with Replies).

## Deferred (not in this commit)

- **Phase 3:** the 3-step send flow (`ui/send.html` + `static/js/pages/send.js`
  are stubs; the CSS classes exist in `static/css/pages/send.css`), the
  Replies page (9.1) with needs-reply handling, the email drawer's inline
  edit + "Polish with AI".
- **Phases 4–8:** Library, Team, Settings redesign, AI features, legacy
  retirement (including the `base_new.html` rename).
- **Phone stacked-card tables** and the remaining 390 px polish.

## Screenshots

`docs/screenshots/` holds the current state at 1440 px (light + dark) for
every screen above, plus Home, Replies and the send entry point at 390 px
(plan §14.5).
