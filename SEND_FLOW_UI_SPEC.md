# CampusMail AI — Send Flow UI Specification

> **Scope.** This document specifies, to the last detail, the UI that appears after a user
> clicks a **specific campaign** and then clicks the **"Send emails"** button — i.e. the page at
> `/campaigns/<cid>/send` (template `ui/send_new.html`, behaviour in
> `static/js/pages/send.js`, styles in `static/css/pages/send.css`). It covers **every**
> user flow that branch supports: choosing the audience, choosing the template, fixing field
> mapping, choosing the send account, picking an outcome (drafts / schedule / send-now / queue),
> the check step with per-recipient preview and quick checks, the confirmation modals,
> the creation progress screen, the success screens, every error state, and the
> post-send follow-up flows in the campaign hub and its Emails tab.
>
> **Source of truth.** Extracted verbatim from the code on 2026-11-15. All quoted strings are
> exact copy from the codebase. If this document and the code disagree, the code wins — but
> flag the discrepancy in the correction plan.
>
> **Intended use.** This file is handed to Claude (working with the codebase owner) to produce a
> correction/improvement plan for this part of the UI. Section 16 lists observed
> inconsistencies and rough edges collected while writing the spec; treat those as seeds, not
> conclusions. Section 17 attaches six reference screenshots of the key states.

> **Screenshot note.** The reference screenshots are **zoomed-out, full-page captures**
> (1440 px-wide viewport, entire scrollable page in one image, 1x pixel scale). Text is small
> by design — zoom in when reading them. They were captured on 2026-11-15 from the live
> local app (campaign "Diwali Outreach", 1,046 contacts, 3 templates, dark theme) — two of
> them use a mocked API response to force a state that the current workspace config doesn't
> produce (noted per image).

---

## 1. How the user gets to this screen (entry points)

The send page can be reached from **six** places. All of them render the same
`ui/send_new.html`; the difference is whether a campaign id is present
(`window.send_campaign_id`):

| # | Entry point | URL | Campaign pre-selected? |
|---|---|---|---|
| 1 | **Campaigns list** → row action button "Send emails" (and the row kebab menu's "Open" goes to the hub, not here) | `/campaigns/<cid>/send` | Yes |
| 2 | **Campaign hub** (`/campaigns/<cid>`) → primary header button "Send emails" | `/campaigns/<cid>/send` | Yes |
| 3 | **Campaign hub → Overview → next-step card** links: "Send your first emails." / "Set up follow-up" | `/campaigns/<cid>/send` | Yes |
| 4 | **Campaign hub → Emails tab** empty state button "Send emails" | `/campaigns/<cid>/send` | Yes |
| 5 | **Command palette** (⌘K, top bar) → "Send emails" | `/send` | No — campaign picker shown in Step 1 |
| 6 | Any leftover draft: **Campaigns list** → top banner "You started a send and didn't finish it." → "Continue" | `/send?campaign=<cid>` | Yes (from the saved draft) |

The user story asked for is **entry 1/2**: on the Campaigns list the user clicks a campaign row
(the whole row is clickable → hub) or the row's "Send emails" ghost button; on the hub the user
clicks the primary "Send emails" button. Both land on the same screen with the campaign fixed.

### 1.1 The Campaigns list (where "Send emails" lives first)

- Route `/campaigns`, template `ui/campaigns_new.html`, script `static/js/pages/campaigns.js`.
- Page head: title "Campaigns", subtitle "Every outreach effort in one place.", right side
  primary button "New campaign".
- Toolbar: search input (placeholder "Search campaigns", 250 ms debounce), segmented filter
  [All | Active | Finished] with live count badges, sort select (Recent activity / Name /
  Created).
- Table columns: Campaign (name + description subline), Status badge (Draft / Active /
  Finished), Contacts, Sent, Bounced (highlighted red `is-bounced` when > 0), Replied,
  Reply rate, Last activity (relative time), and a row of actions: ghost button
  **"Send emails"** (href `/campaigns/<id>/send`) + a ⋮ kebab (Open / Rename / Delete).
  At ≤767 px the numeric columns and the row buttons hide; the card stays tappable.
- Status logic (`statusOf`): no emails → "draft"; any ready/drafted/scheduled/failed email, or
  last activity within 14 days → "active"; otherwise "finished".
- **Unfinished-send banner**: on load the page scans `localStorage` for a `cm_send_*` key
  whose `saved_at` is less than 7 days old. If found, an info banner is prepended above the
  table: icon `send`, text "You started a send and didn't finish it. ", primary small button
  **Continue** (`/send?campaign=<cid>`) and ghost small button **Discard** (removes the key).
- Empty states: no campaigns at all → illustration + "No campaigns yet" +
  "A campaign holds the companies you email, the emails you send and the replies you get." +
  "Create your first campaign"; no matches → "No campaigns match" + "Clear filters".
- New campaign modal: fields Name (placeholder "Diwali wishes to recruiters") and
  Description (optional, "What is this campaign for?"), buttons "Cancel" / "Create campaign";
  on create → navigate to `/campaigns/<id>` (the hub).

### 1.2 The campaign hub (what you see when you click the campaign row)

- Route `/campaigns/<cid>`, template `ui/campaign_hub.html`, script `static/js/pages/hub.js`.
- Breadcrumb: `Campaigns › <campaign name>`. Document title becomes
  "<campaign name> - CampusMail AI".
- Header: campaign name (click to rename inline — input appears in place, Enter saves,
  Escape cancels, blur saves) + status badge (Draft / Active / Finished) + note line
  (description, or link "Add a note") + primary button **"Send emails"** + kebab menu
  (Rename / Edit note / Export report (CSV) / Delete campaign; delete of a campaign that has
  sent mail requires typing the campaign name — `confirmTyped`).
- Tabs (underline style, sticky, in URL): **Overview** (no count) / **Emails** (count) /
  **Audience** (count).
- Overview: at most one "next step" card (lightbulb icon; first matching rule wins):
  1. no contacts → "Add the companies you want to email." + [Add contacts → Audience tab]
  2. no emails → "Your list is ready. Send your first emails." + **[Send emails]**
  3. ready emails → "N emails ready. Save them as Gmail drafts to review." + [Create drafts]
  4. drafted → "N drafts waiting in Gmail. Review them there, or schedule them from here." +
     [Open Gmail (new tab, mail.google.com/mail/?view=drafts)] + [Schedule them]
  5. scheduled → "N emails go out <upcomingWhen>." + [View emails]
  6. bounced → "N emails bounced. Fix the addresses and resend." + [Fix addresses]
  7. failed → "N emails failed. Fix the problem and resend." + [View failed]
  8. sent-unreplied for > 3 days → "N people have not replied. Send them a follow-up." +
     **[Set up follow-up → /campaigns/<cid>/send]**
  Then stat tiles (Sent / Replied / Reply rate / Bounced, count-up animation), funnel card
  (Contacts → Emailed → Replied), 14-day sends-per-day chart (placeholder "The chart appears
  after a few days of sends."), "where every email stands" segment bar, recent replies,
  per-account send mix, recent activity.
- Loading failure states: 404 → empty state "This campaign no longer exists" +
  "It may have been deleted. Everything about it is gone." + [Back to campaigns];
  other errors → danger banner with Retry.

---

## 2. App shell present on the send page (context)

The send page extends `ui/base_new.html`. Everything around the flow:

- **Left sidebar** (fixed; mobile: off-canvas drawer via top-bar burger + scrim):
  - Brand: send-plane mark + "CampusMail AI" (+ optional team name line).
  - Nav: Home (`/`), **Campaigns** (`/campaigns` — active on the send page too),
    Replies (`/replies`, with unread badge), Library (`/library`), Team (`/team`, **admin only**).
  - Footer: **mode card** (the Safe-mode indicator, see §4.5), "Help" (opens guide),
    Settings, and the user account row (avatar, name, chevron → account menu).
- **Top bar** (sticky, frosted): burger (mobile), breadcrumb
  `Campaigns › Send emails`, command palette button "Search or jump to…" with ⌘K kbd chip,
  "Ask AI" button (only visible when AI is configured), system-status icon (red dot when
  there is a problem; popover with details and actions), "Reconnecting…" chip (hidden
  normally), and a thin progress line under the bar (used by long operations).
- **Bottom tabs** (mobile): Home / Campaigns / Replies / Library / More(settings).
- **Skip link** ("Skip to content"), theme script (default **dark**, `localStorage cm_theme`,
  `?theme=` override; dark is neutral near-black `#0A0B0E` with indigo accents).
- **Personal workspace banner** (only if the signed-in user is not on the team):
  "You are in your own workspace while your request is reviewed. …" with optional
  "Add a note for the admins", or "Your request was not approved. …"
- **Global feedback primitives** used throughout this flow (from `static/js/core/ui.js`):
  - **Toasts** top-right, max 3 stacked, icon by type (success / error / info), auto-dismiss
    6 s (10 s when they carry an action button like "Undo" or "Open Gmail"), pause on hover,
    slide-out animation.
  - **Modals**: scrim + centered box (or `modal-wide`), title + optional sub, body, foot
    buttons; focus is trapped and restored, Escape closes (unless `closable: false`),
    scrim mousedown closes.
  - **Hold-to-confirm button**: pressing and holding fills a progress bar left→right with a
    live % label; releasing early cancels; keyboard Space/Enter can hold. Used in
    `confirmSendNow` (2 s) and in the Emails tab "send now" (see §11).
  - **`withLoading(btn, fn)`**: swaps the button to a loading state, disables it, restores
    after the promise settles.
  - **`errorCard`**: red banner with message, optional collapsible "Details" (mono error
    text) and a "Retry" button.
  - **`emptyState`**: 96 px two-tone spot illustration + title + text + optional buttons.

---

## 3. The send page skeleton, state, persistence

### 3.1 Route and DOM

- `GET /campaigns/<cid>/send` → `send_new.html` with `window.send_campaign_id = <cid>`.
  `GET /send` renders the same template with `cid = None`.
- Breadcrumb: `Campaigns › Send emails`. Title: "Send emails - CampusMail AI".
- Body: single mount point `<div id="send-root">` inside `.page-in`. **Everything below is
  rendered client-side** by `render()` on every state change — the whole page re-renders
  (full `innerHTML` rebuild) on each interaction.
- Page head (in-flow, not the app top bar):
  - Step title (24 px / 650): **"What are you sending?"** (step 1) or **"Check the emails"**
    (step 2).
  - Step sub: "Pick who to email, what to say, and what happens next." /
    "This is exactly what each person will get."
  - Right side: **stepper** — two pill buttons "What" and "Check". Current step highlighted
    (`is-current`), completed steps show a check icon and are **clickable to go back**;
    the forward step is disabled. Plus an icon button (✕, aria-label "Close").
- **Close (✕)**: if there is browser history, `history.back()`, otherwise navigate to
  `/campaigns/<cid>` (or `/campaigns` if no campaign).
- **Sticky footer** (both steps): a bar pinned to the bottom of the viewport
  (`position: sticky; bottom: 0`, blurred surface, top border) containing [left slot]
  [centered summary line] [primary button]. On step 2 the left slot holds the "Back" ghost
  button.
- **URL state**: the current step is written into the query string as `?step=1|2`
  (`history.replaceState`). Browser back/forward (`popstate`) can only **move backwards**
  (to a lower step); moving forward is never restored.
- **Keyboard**: ⌘/Ctrl+Enter anywhere (outside inputs) clicks the current step's primary
  footer button if it isn't disabled.

### 3.2 State object (persisted)

```
S = {
  step: 1|2,
  campaignId,            // 0 = not chosen (only on /send)
  listId,                // 0 = "All contacts"; otherwise a sheet id
  contactIds: [],        // subset of contacts within the list; empty = whole list
  templateId,            // 0 = none
  mapping: {},           // template field -> "column:<col>" | fixed text
  mappedFor: "tpl:list", // guard: mapping is valid for this tpl+list pair
  matchOpen: bool,       // match-fix panel expanded
  account,               // gmail account id (0 = none)
  append_signature: true,
  cc: "",
  reminder_days: 0, reminder_max: 1, reminder_template_id: 0,
  when: "",              // ISO local-ish datetime string for schedule
  stagger: 0,            // minutes between emails
  outcome: "draft"       // draft | schedule | send | queue
}
```

- **Persistence**: every change calls `save()` → `localStorage["cm_send_" + campaignId]`
  (key `cm_send_global` when no campaign). On load, if a saved state exists for this
  campaign it is restored (`Object.assign(S, saved)`).
- **Draft expiry**: the Campaigns list banner (§1.1) treats saved state as "unfinished"
  for 7 days.
- On success (or when the flow completes) the key is deleted.
- **Attachments are NOT persisted**: file handles live in a JS array only. The UI says so:
  "Re-select the files if you refresh the page."
- Stagger memory: the time picker writes the last chosen stagger to
  `localStorage["cm_stagger"]` (also `defaultStaggerMin` from settings is the fallback).

### 3.3 Data loaded on entry

One parallel `Promise.all` of five APIs:

1. `/api/campaigns` → list of campaigns
2. `/api/sheets` → uploaded contact lists (name, n_rows, headers, campaign_id)
3. `/api/templates` → templates (name, subject, body_html, fields[], is_active, kind
   "outreach"|"reminder")
4. `/api/accounts` → **connected** Gmail accounts only (+ `last_account_id`); failure
   swallowed → `[]`
5. `/api/state` → settings (`safe_mode`, `ai_enabled`, `ai_has_key`,
   `default_stagger_min`, `max_sends_per_day`), onboarding contact count, signed-in
   user email (used for the test mail)

Defaults applied on first visit (no saved state):
- single campaign in the workspace → it is pre-selected;
- default template = first `is_active`, else first template;
- account = `last_account_id`, else first connected account.

After loading, the app calls `selectList(S.listId)` (fetches the sheet's rows if a sheet is
selected) and then **`autoMatch()`** — always, so the mapping shown on first visit is the
mapped one (the comment in code: the "no fields mapped -> 2 mapped" mismatch came from
skipping this).

If the whole load fails: the page is replaced by an error card
"Could not load the send flow. Check your connection and try again." + Retry.

---

## 4. Step 1 — "What are you sending?"

Single scrolling screen, one column (`max-width` constrained), section overlines in
uppercase 11 px letter-spaced labels. Order, top to bottom:

### 4.1 Campaign line

- **With campaign (the normal case for this spec):** a static line `.send-cap`
  reading `Campaign: **<campaign name>**` (bold name). It cannot be changed here.
- **Without campaign (`/send`):** a select labelled "Campaign" with all campaigns plus a
  "+ New campaign" option (placeholder "Choose a campaign"). Choosing a campaign resets
  `contactIds` and re-renders. Choosing "+ New campaign" opens the **New campaign modal**:
  field Name (placeholder "e.g. Summer 2027 recruiter outreach"), button "Create & use";
  on create → the new campaign becomes the active one.
- **Campaign deleted edge case:** if `S.campaignId` no longer exists in `/api/campaigns`,
  the panel is replaced by a danger banner: icon `alert-triangle`,
  "This campaign no longer exists. Pick another one.", ghost small button **Choose**
  (sets `campaignId = 0` → shows the picker).

### 4.2 Overline "Who to email" — list cards

- One `.opt-card` per contact list (sheet) + a final card "All contacts".
  Card content: `users` icon, list name, count chip "N contacts", and either:
  - up to 6 column-header chips (`mono-chip` code style) if the sheet has headers, or
  - a description: "List for <campaign name>" / "Uploaded list" /
    "Every contact in your main list." (for All contacts; its headers are
    company / hr_name / hr_email).
- Selected card has `is-selected` styling + a radio indicator (`.opt-radio`) on the right.
- Selecting a card: sets `listId`, **clears `contactIds`**, fetches that sheet's rows,
  re-runs `autoMatch()`, re-renders.
- Contact count for "All contacts" comes from `/api/state` onboarding contacts.

### 4.3 Contact tools (below the list cards)

A row of three controls (`.ct-tools`):

1. **Choose who to email** (secondary small button, `check-square` icon). Label flips to
   **"Chosen: N · change"** once a subset is picked. Opens the **contact picker modal**.
2. **Paste a table** (ghost small, `clipboard` icon) → **paste modal**:
   - Title "Paste a table"; sub: "It joins <campaign name> as a contact list. " +
     "Columns can be company, recruiter/HR name, email - in any order." (the campaign
     clause only when a campaign is set).
   - Fields: "Name" input (placeholder "List name (optional)") and a 9-row textarea whose
     placeholder shows a tab-separated sample
     (`Company\tRecruiter name\tHR email\nAccolite\tGenevieve Dsouza\t…`).
   - Buttons: Cancel / **Add this list** (loading state while POSTing `/api/sheets/paste`).
   - On success: the new sheet is selected automatically, `contactIds` cleared,
     `autoMatch()` re-run, toast "List added: <name> (N rows)." (+ "N were duplicates."
     when applicable).
3. **Upload drop** (dashed drop-zone button): `upload` icon, text
   "Upload a list: CSV or Excel. First row = column names." and a right-aligned faint
   caption "Adds to **<campaign name>**" (or "a campaign" on `/send`).
   Hidden file input accepts `.csv,.xlsx`. On upload (`/api/sheets/upload`): the sheet is
   prepended, selected, `contactIds` cleared, `autoMatch()` re-run, toast
   "List added: <name> (N rows)."; errors toast the server message.

#### 4.3.1 Contact picker modal (wide)

- Title "Choose who to email"; sub "<list name> · N contacts. Leave nothing selected to
  email everyone."
- Toolbar: search input (placeholder "Search company, name, email or No.", 150 ms
  debounce; matches company, name, email, or serial No.), push, ghost small buttons
  **Select all** (selects all *visible* rows) and **Clear**, and a right-aligned count
  label "**N of M chosen**" (updates live).
- List: one row per contact — checkbox, serial number (or "—"), company,
  "<name> · <email>". Checkboxes toggle the selection set.
  - No matches → "No contacts match."
  - **Only the first 500 visible rows are rendered**; if more match: caption
    "Showing the first 500 matches - narrow the search to pick more."
  - If the audience is empty the modal never opens: toast
    "There are no contacts in this list yet." (error).
- Foot: Cancel / **Use selection** (primary). "Use selection" stores
  `contactIds` (sorted ascending) and re-renders. Closing any other way keeps the
  previous selection (no partial apply).

### 4.4 Overline "What to say" — template cards

- One `.opt-card.tpl-card` per template. Card layout:
  - **Thumbnail** (`.tpl-thumb`): a sandboxed iframe rendering the template's HTML
    (scaled-down `srcdoc`, local images rewritten to `/local_img/<name>` URLs).
  - Name + badges: **"Default"** (when `is_active`) and/or **"Follow-up"** (when
    `kind == "reminder"`).
  - Line 2: subject, or "No subject".
  - Line 3: "N fields to fill in" or "No fill-in fields - every email is identical".
  - An "Edit" ghost small button (pencil) linking to `/library/templates/<id>`
    (stops propagation so the card isn't selected).
  - Radio indicator on the right; `is-selected` when chosen.
- No templates at all: info banner "No templates yet. Write one with AI or upload an
  HTML file."
- Below the stack, a row of two actions:
  1. **Upload a template** (ghost) → hidden file input (`.html,.htm,.txt`) →
     `/api/templates/upload`; on success the template is selected, toast "Template added.",
     `autoMatch()` re-run.
  2. **Write one with AI** (`.btn-ai`, sparkles) → **AI template modal**:
     - Title "Write a template with AI"; sub "You review and edit it before anyone gets it."
     - Fields: Name (placeholder "e.g. Summer 2027 outreach"), Subject (placeholder
       "e.g. Internship drive at our campus"), Brief (3-row textarea, placeholder "What
       should this email say? Audience, the ask, anything personal."), Tone select
       (Friendly / Professional / Short and direct).
     - Buttons: Cancel / **Write template** (loading while `/api/templates/ai` runs).
     - On success: template selected, toast "Template written. Open it to edit.", and
       **redirect to the template editor** `/library/templates/<id>` (the user leaves the
       send page; the saved state under `cm_send_<cid>` lets them come back via the
       Continue banner or by re-entering the flow).
- Selecting a template re-runs `autoMatch()` and re-renders.

### 4.5 Field matching (between "What to say" and "Send from")

Behaviour depends on whether the template has fill-in fields:

- **No fields:** info note — "This template has no fill-in fields, so every email is
  identical." Nothing blocks.
- **All fields mapped:** green note (check-circle) "We matched all N fields automatically."
  + a collapsible `<details>` summary **"See how"** that, when opened, shows the match
  panel table (below) for inspection.
- **One or more unmapped fields:** warning note (alert-triangle) "N field(s) need your
  help." + inline link **Fix now** / **Hide** (toggles `matchOpen`). When open, the match
  panel renders directly in the flow. **Unmatched fields block the primary CTA** (see §4.9).

#### Match panel (`.match-table`)

- Table header: "In your email" | (arrow) | "Filled from".
- One row per template field: the field name as a `mono-chip`; an arrow icon; a select with
  options:
  - "Pick a column" (empty value),
  - one option per list column, shown as "<column>, e.g. <first non-empty value, 24 chars>"
    (sample only when a sheet's rows are loaded),
  - "Type my own text..." — choosing it reveals a text input (placeholder "Fixed text,
    e.g. Campus Placement Cell"),
  - "Leave blank".
- Rows without a mapping get `is-unmatched` (warning colour).
- Changing anything commits immediately: mapping value becomes `column:<col>`, a fixed
  string, or removed; the row re-renders after 350 ms; `mappedFor` is marked so later
  auto-matching won't override the user's choice.
- **AI matching:** when AI is enabled (`ai_enabled` + key present), the table gets a final
  row with a "Match with AI" AI-style button (sparkles). It calls `/api/ai/mapping`
  (fields + columns), replaces the whole mapping, toasts "Matched with AI. Review the
  choices.", and re-renders.
- `autoMatch()` itself (on template/list change): calls `/api/merge/suggest` with the
  template fields and the current columns; keeps any field the user already mapped for this
  template+list pair, fills the rest from suggestions; opens the match panel automatically
  if anything is left unmapped (`matchOpen = true`).

### 4.6 Overline "Send from" — account select

- A `<select>` (max-width 440 px) listing **connected** Gmail accounts (email/label).
  Default: last used account, else first. Changing it re-renders (and re-saves).
- Help line under it: "The Gmail account that sends. Add more in Settings." — or, with no
  connected accounts: "No connected account - connect one in Settings first."
  (The CTA still requires an account at send time — see §7.1.)

### 4.7 Overline "What happens next" — outcome cards

- **If Safe mode is on** (default), an amber banner sits above the cards:
  shield icon, "Safe mode is on - immediate sending is blocked. " + link
  **Turn on live sending** → opens the **live sending modal**:
  - Title "Turn on live sending?"; sub "You'll be able to send real emails immediately.
    Drafts and schedules work either way."
  - Amber warn banner: "Live sending can't be recalled. Start with drafts until you trust
    the setup."
  - Buttons: **Keep safe mode** (ghost) / **Turn on live sending** (danger, loading state;
    POSTs `safe_mode: "0"`). On success: toast "Live sending is on.", re-render — the
    "Send now" card becomes enabled.
- Four `.opt-card` rows (icon, title, optional tag, description, radio):

  | id | Icon | Title | Tag | Description | Enabled? |
  |---|---|---|---|---|---|
  | `draft` | file-pen | **Save as drafts** | "Safest" | "Nothing is sent. Every email lands in your Gmail Drafts, so you can review or delete them first." | always |
  | `schedule` | clock | **Schedule for later** | — | "We send at the time you pick. You can reschedule or cancel any time before then." | always |
  | `send` | send | **Send now** | — | "Real emails go out immediately, one after another. This can't be recalled." | **disabled while Safe mode is on** (hover title: "Safe mode is on. Turn it off to send immediately.") |
  | `queue` | inbox | **Keep in queue** | — | "They wait in your email list. Send or schedule them whenever you're ready." | always |

- Selecting **Schedule for later** reveals the **When block** (§4.8) directly below.
  Default outcome on a fresh flow: `draft`.

### 4.8 The "When" block (only when outcome = schedule)

- Card with a clock icon, either the formatted chosen time (`fmtLong`, exact format
  "<Wed> <d> <Mon> at HH:MM", e.g. "Mon 17 Nov at 09:00") or the label **"Pick a time"**,
  plus, when stagger > 0, a faint "spaced N min apart", and a right-aligned ghost small
  button **"Choose time"** (or **"Change"** once set).
- The button opens the **shared time picker modal** (`static/js/components/timepicker.js`),
  titled "When should it go?":
  - **Quick pick chips** (only future ones, ≥ 10 min ahead): "In 1 hour", "Today 18:00",
    "Tomorrow 09:00", "Tomorrow 10:00", "Monday 09:00", "Next week".
  - **Date** input + **Time** select in a 2-column grid. Time is in **15-minute slots**
    (00:00 … 23:45).
  - A zone line: clock icon + "<time zone label> — <full datetime>".
  - Validation: a chosen time less than **5 minutes** from now → red error
    "Pick a time at least 5 minutes from now.", the date/time fields get `invalid`
    styling, and "Choose time" is disabled.
  - **"Space them out"** segmented control: "All at once" / "1 min apart" / "2 min apart" /
    "5 min apart" / "Custom" (custom reveals a number input, 1–120 minutes).
  - **Live line** below: for 1 email: "1 email at HH:MM." For many, all at once:
    "N emails all at once at HH:MM." With stagger: "N emails, one every X minute(s).
    First at HH:MM, last at HH:MM."
  - **Daily limit meter**: if `max_sends_per_day > 0` and the batch size N exceeds it:
    red line "Your daily limit is <cap>. This send puts N on one day. " + link
    **Change limit** → `/settings`.
  - Foot: **Cancel** / **Choose time** (primary). Cancel → promise rejects with
    "cancelled" (no toast; state unchanged). Confirming stores `S.when` (backend format)
    and `S.stagger`, saves the stagger to `cm_stagger`, and re-renders.
  - Initial values: previously chosen `S.when`/`S.stagger`, else "now + 1 hour" and the
    default stagger (saved `cm_stagger` → settings `default_stagger_min` → 0).

### 4.9 Overline "Extras" — add-ons accordion

A card with four collapsible rows (`.addon`); each header shows icon, title, a **state
chip** (right of the title) and a rotating chevron. The first (Signature) is expanded by
default when its state is on.

1. **Signature** — state chip "On"/"Off" (default **On**).
   Body: checkbox + "Append your saved signature to every email".
2. **CC** — state chip "None" or the CC value.
   Body: input (placeholder "name@company.com, another@company.com", commits on change) +
   help "Copied onto every email in this batch."
   Server limit: max 20 CC addresses (enforced by `/api/merge/generate` → error toast).
3. **Follow-up reminder** — state chip "Every Nd" or "Off" (default Off).
   Body: checkbox "Follow up with people who don't reply" (checking it sets
   `reminder_days` from the number input, default 3); row "Every [1–90] day(s),
   [1× | 2× | 3×]"; row "Using [No reminder template | …list of templates with
   kind=reminder…]". Turning the checkbox off resets `reminder_days` to 0.
4. **Attachments** — state chip "None" or "N file(s)".
   Body: "Add files" ghost small button (multi file input), a list of the chosen files
   (paperclip icon, name, size in KB, per-file ✕ remove), and help "Sent with every email
   in this batch. Re-select the files if you refresh the page."
   **15 MB total cap**: adding files that would push the total over 15 MB → error toast
   "Attachments must stay under 15 MB in total." and the files are not added.

### 4.10 Step-1 sticky footer

- Center summary line (title tooltip carries the disabled reason):
  "N people · [N of ]<list total> in <list name> · <campaign name> · <template name>".
- Primary button: **"Check the N emails"** (N = chosen contacts, else list count).
- **Disabled reasons** (button `title` explains when disabled):
  - no contacts: "Pick some contacts to continue"
  - no template: "Pick a template to continue"
  - unmapped fields (highest priority): "Resolve the highlighted fields first"
- Clicking advances to step 2 (`S.step = 2`), re-renders, updates `?step=2`.

---

## 5. Step 2 — "Check the emails"

Layout: `.send-wrap.two-col` — a grid `8fr / 4fr` (single column below 1024 px).

### 5.1 Left column — the preview

- Header row: bold label **"What they'll get"**, pushed right: two icon buttons
  (aria-labels "Previous person" / "Next person") that cycle through the rendered
  previews (wrap-around).
- Caption line (12.5 px, muted):
  - while loading: "Loading previews…"
  - loaded: "Person 1 of 8 shown · N people in total" (8 = the preview cap; N = the real
    total, formatted with thousands separators)
  - empty audience: "No recipients in this selection."
- The preview itself: **`.preview-paper`** — a white iframe (min-height 460 px, card
  border/radius) that renders the **real merged email** for the current recipient:
  - "To: <email> · <name> · <company>" (12.5 px, name/company omitted when absent),
  - the merged **subject** (bold, underlined by a divider),
  - the merged HTML body (local images via `/local_img/…`),
  - **every filled-in value is highlighted** in the body: dotted indigo underline +
    light indigo background (class `cm-fill`) — the client walks the rendered DOM and
    wraps each filled value (column-mapped values resolved against that recipient's row,
    plus fixed-text values).
  - No recipient → the frame shows "No one to preview - this list is empty."
- Previews come from `POST /api/merge/preview` (template, mapping, sheet, contact_ids,
  `limit: 8`, append_signature) — i.e. **at most 8 sample emails** are pre-rendered even
  for a 5,000-person list; the caption's "in total" number is the true audience size.
- If the preview request fails: the caption shows the error message verbatim.

### 5.2 Right column — checks + summary

**Quick checks card** (`#checks-card`, h3 "Quick checks"):
- Starts with an info line "Running…" (spinner-style), then the checks are computed
  client-side over the audience rows:
  1. green: "**N** person/people will get this"
  2. green "Every row has an email address" **or** amber: "**N** row(s) have no email
     address - they'll be skipped"
  3. amber (only when present): "**N** duplicate email(s) - each one still gets an email"
  4. green "All N fill-in fields filled for everyone" / green "No fill-in fields - every
     email is identical" / amber "Fills left empty: <field>, <field> (3 blanks) - **fix**"
     (the "fix" link jumps back to step 1 with the match panel open)
  5. empty audience: amber "This selection has no recipients yet." (and only that line)
- Below the checks: a full-width secondary button **"Send me a test"** (send icon).
  - Requires an account (toast "Pick a send account first." if missing).
  - POST `/api/emails/test` with the current template/mapping/audience/account/signature.
  - Success toasts (8 s): "Test saved to your Gmail Drafts (Safe mode is on)." or
    "Test sent to <your email> - check your inbox."
  - Failures toast the server message.

**Summary card** (h3 "Summary"), rows with icons:
- **To** — "[N of ]<list name> · <campaign name>"
- **Template** — template name (or "—")
- **From** — selected account email (or "No account picked")
- **Signature** — "appended" (row only when on)
- **CC** — the CC value (row only when set)
- **Follow-up** — "every N day(s), up to M×" (row only when on)
- **Attachments** — "N file(s)" (row only when files chosen)
- Outcome line (bold, above a divider):
  - draft: "Saved as Gmail drafts - nothing is sent"
  - schedule: "Scheduled for <long datetime> · spaced N min apart" (or
    "Scheduled (pick a time first)" when no time yet)
  - send: "Sent immediately, one after another"
  - queue: "Kept in your email queue"

### 5.3 Step-2 sticky footer

- Left: ghost **← Back** button (to step 1; state preserved).
- Center summary: "N people · <template name>" (updated after previews load).
- Primary button (label by outcome):
  - **Create N drafts** / **Schedule N emails** / **Send N emails now** /
    **Queue N emails** (singular "email" when N = 1).
- Disabled while checks run and whenever `preview_total == 0`
  (title "This selection has no recipients").

---

## 6. Confirmation modals (step 2 → create)

`onPrimary3()` guards first:
- no account → toast "Pick a send account first." (error)
- schedule without a time → toast "Pick a time first." (error)

Then, per outcome:

### 6.1 Send now (the only destructive confirmation on this page)

Modal:
- Title **"Send N real emails?"**; sub "They go out from <account email> immediately,
  one after another, and can't be recalled."
- Amber banner: "If this is the first time, 'Send me a test' is the safe move."
- Help line: "Hold the button for 2 seconds to confirm."
- Foot: **Cancel** (ghost) / **"Hold to send N"** (danger, **hold-to-confirm, 2 s**,
  progress fill + live %). Escaping/closing without the hold resolves false → nothing
  happens.

### 6.2 Schedule

Modal:
- Title "Schedule N emails?"; sub "<long datetime>[ · spaced N minutes apart]. You can
  reschedule or cancel until then."
- Foot: **Cancel** / **Schedule N emails** (plain click confirm).

### 6.3 Drafts / Queue

No confirmation modal — the primary button goes straight to creation.

---

## 7. The creation phase (after confirm)

The whole page is replaced by a **progress panel** (`.send-success` container, title
"Creating your send…", list of status lines; each line starts as a spinning loader,
becomes a green check with result text, or an amber warning on partial failure).

### 7.1 Without attachments (the common path)

One call: `POST /api/merge/generate` with
`{name: "<campaign> · <list>( (N chosen)) <dd Mon>", template_id, mapping, campaign_id,
sheet_id, contact_ids?, account_id, action: <outcome>, at, stagger, cc,
append_signature}`.

- Line 1: "Creating N emails…" → done: "Batch created - N emails queued" (queue) or
  "Batch created - N emails <created_drafts> drafts created / scheduled / sent"
  (exact: "N draft(s) created" / "N scheduled" / "N sent" depending on the outcome
  fields returned).

### 7.2 With attachments (3-step)

1. `POST /api/merge/generate` with `action: "create"` → line "Creating N emails…" →
   "Batch created".
2. `POST /api/batches/<id>/attachments` (multipart, all files) → line "Attaching N
   file(s)…" → "Attachments added" (failure → amber line "Attachment upload failed: <msg>"
   and the flow continues without them).
3. `POST /api/batches/<id>/send-as` with the real outcome → line "Finishing…" →
   "N drafts created" / "N scheduled" / "N sent".

### 7.3 Follow-up reminders (when enabled)

4th line: "Setting up follow-up reminders…" → `POST /api/batches/<id>/reminders`
(days 1–90, max 1–3, optional reminder template id) → "Follow-ups on: every N day(s), up
to M×" (failure → amber "Reminders not set: <msg>").

### 7.4 Failure

Any error replaces the panel with an error card "Couldn't finish. <server message>" +
**Retry** (re-runs the same creation) and, below it, a ghost button "Back to campaigns".

### 7.5 Server-side guards that surface as errors here

- **Safe mode ON** + action `send` → 409 "Safe mode is ON - immediate sending is blocked.
  Pick Drafts or Schedule, or turn off Safe mode in Settings." (normally unreachable
  because the card is disabled, but reachable if the user toggles settings mid-flow or
  via a stale UI).
- **Daily cap** (`max_sends_per_day`, default 100): immediate sends stop at the cap; the
  result's `sent` reflects only what went out — the success screen then reports the
  actual number and, if `failed > 0`, shows the failed banner (§8).
- No contacts in selection → "No contacts in that selection."
- Unknown action / bad CC (>20) → server error toasts.

---

## 8. Success screens

After creation the `cm_send_<cid>` draft is deleted and a centered success screen renders:
96 px check-circle illustration, title (20 px bold), sub (muted, max 46 ch), optional
failed banner, and two centered buttons: **"View campaign"** (primary, →
`/campaigns/<cid>` or `/campaigns`) and **"Back to campaigns"** (ghost).

Copy by outcome (N from the API result, falling back to the audience count):

| Outcome | Title | Sub |
|---|---|---|
| draft | "N draft(s) in your Gmail" | "Open your Drafts folder to review them - delete what you don't want, edit, or send from there." |
| schedule | "N email(s) is/are scheduled" | "Goes out <long datetime>[, spaced N minutes apart]. You can reschedule or cancel until then." |
| send | "N email(s) is/are on their way" | "Sent from <account email>. Replies will show up in your Replies tab." |
| queue | "N email(s) is/are in your queue" | "They're waiting in the campaign's email list. Send or schedule them whenever you're ready." |

If `failed > 0` in the result: amber banner "N email(s) failed - open the campaign to see
why and retry."

---

## 9. Complete inventory of transient UI in this flow

### 9.1 Modals (9)

| Modal | Triggered from | Title | Foot buttons |
|---|---|---|---|
| New campaign | campaign picker "+ New campaign" | "New campaign" | Cancel / Create & use |
| Choose who to email (wide) | "Choose who to email" | "Choose who to email" | Cancel / Use selection |
| Paste a table (wide) | "Paste a table" | "Paste a table" | Cancel / Add this list |
| Write a template with AI | "Write one with AI" | "Write a template with AI" | Cancel / Write template |
| Turn on live sending | safe-mode banner link / sidebar mode card | "Turn on live sending?" | Keep safe mode / Turn on live sending (danger) |
| When should it go? | "Choose time" / "Change" | "When should it go?" | Cancel / Choose time |
| Send N real emails? | "Send N emails now" | "Send N real emails?" | Cancel / Hold to send N (2 s hold) |
| Schedule N emails? | "Schedule N emails" | "Schedule N emails?" | Cancel / Schedule N emails |
| (Emails tab, post-send) drafts / schedule / send-now | see §11 | … | … |

### 9.2 Toasts (all exact messages in this flow)

- "List added: <name> (N rows)." (+ "N were duplicates.")
- "Template added." / "Template written. Open it to edit."
- "Matched with AI. Review the choices."
- "Live sending is on."
- "There are no contacts in this list yet." (error)
- "Attachments must stay under 15 MB in total." (error)
- "Pick a send account first." (error)
- "Pick a time first." (error)
- "Test saved to your Gmail Drafts (Safe mode is on)." / "Test sent to <email> - check
  your inbox."
- Server errors (verbatim message) for upload/paste/test/generate failures.

### 9.3 Banners

- Campaign-deleted danger banner (§4.1).
- Safe-mode amber banner (§4.7).
- Failed-count warning banner on success (§8).
- Campaigns-list "unfinished send" info banner (§1.1).

---

## 10. Full user-flow walkthroughs

### Flow A — Happy path: drafts (default, safe mode)

1. Campaigns list → row "Send emails" → `/campaigns/<cid>/send`.
2. Step 1 loads: campaign line, list cards (first sheet auto-selected, or "All
   contacts"), auto-matched fields (green note), default account, outcome "Save as
   drafts" selected, signature on.
3. Footer: "Check the N emails" (enabled once contacts + template + mapping are good).
4. Step 2: preview of person 1 (fills highlighted), checks all green, summary card,
   "Send me a test" available.
5. Click "Create N drafts" (or ⌘↵). Progress: "Creating your send…" → "Creating N
   emails…" → "N drafts created".
6. Success screen "N drafts in your Gmail" → "View campaign" → hub, where the next-step
   card now says "N drafts waiting in Gmail…" with [Open Gmail] / [Schedule them].

### Flow B — Schedule with stagger

Same as A until step 1: pick "Schedule for later" → When block appears → "Choose time"
modal → pick chip "Tomorrow 09:00", stagger "5 min apart", confirm → When block shows
the time + "spaced 5 min apart". Step 2 → "Schedule N emails" → confirm modal →
progress ("Batch created" + "N scheduled") → success "N emails are scheduled".
Reschedule/cancel later is possible from the hub Emails tab (status `Scheduled` rows get
Reschedule / Stop actions).

### Flow C — Send now (live)

Requires Safe mode OFF (else the card is disabled; the banner offers "Turn on live
sending"). With live mode: choose "Send now", step 2, optionally "Send me a test" first,
then "Send N emails now" → confirm modal → **hold the danger button for 2 s** → progress
("N sent", capped by the daily limit) → success "N emails are on their way" → replies
later appear in the Replies tab and the hub stats.

### Flow D — Fix mapping mid-flow

Template with fields, list without obvious columns → amber "N fields need your help." +
"Fix now" → per-field selects; pick columns (with live samples), or "Type my own text…",
or "Match with AI" (if AI on). "See how" shows the same table read-only-style once all
green. Step-1 CTA re-enables. Step 2 checks report remaining per-row blanks
("Fills left empty: hr_name (12 blanks) - fix" → jump back to the panel).

### Flow E — Sub-audience

"Choose who to email" → search "swiggy" → tick 3 rows ("3 of 4,812 chosen") → "Use
selection" → button label "Chosen: 3 · change" → footer "Check the 3 emails". Switching
list cards later **clears the subset**.

### Flow F — Add data while composing

No good list → "Paste a table" (from Excel) or click the upload drop → new sheet
selected automatically → auto-match runs against the new headers → continue. New
template needed → "Write one with AI" → brief + tone → generated template is selected
and the user is redirected to the editor to review/edit; the send state is preserved
(`cm_send_<cid>`), and the Campaigns list's Continue banner (or re-entering the flow)
restores it.

### Flow G — Abandon and return

Leave at any point (✕, browser nav). State is in `localStorage`. Within 7 days the
Campaigns list shows "You started a send and didn't finish it. [Continue] [Discard]".
Re-entering `/campaigns/<cid>/send` also restores automatically. Successful completion
clears the draft.

### Flow H — No data at all (fresh workspace)

On `/send`: campaign picker must be used (or "+ New campaign"). No lists → only
"Choose who to email" (toast: no contacts yet) / paste / upload available. No templates
→ info banner + the two creation buttons. No connected account → help line under
"Send from"; "Send me a test" and the final CTA toast "Pick a send account first." until
one is connected in Settings.

---

## 11. Post-send flows the user lands on (context for corrections)

### 11.1 Hub Emails tab (where every created email lives)

- Route `/campaigns/<cid>?tab=emails` (script `static/js/pages/emails.js`).
- Toolbar: status filter chips with counts (All / Ready / In Gmail drafts / Scheduled /
  Sent / Replied / Bounced / Failed / Stopped), a send-member filter, sort select, search
  (250 ms debounce, `/` shortcut). Rows are paged (50 per page). A "Next send: …" caption
  shows the next scheduled time. If the scheduler is stopped with overdue emails:
  amber banner with "Run now" (triggers `/api/scheduler/trigger`).
- Row: checkbox, No., Company, Name, To, Subject, **status badge** (vocabulary: Ready /
  In Gmail drafts / Scheduled / Sent / Replied / Bounced / Failed / Stopped; precedence
  Bounced > Replied > Sent for a sent mail), When column ("Scheduled for Today 14:12"
  style via `upcomingWhen`), kebab menu. Row click opens the **email drawer** (full email
  preview, recipient, timeline of events, per-email actions).
- **Bulk selection bar** (appears when rows are ticked) offers actions valid for *all*
  selected: Create drafts / Schedule / (Reschedule) / Send now / Stop / Remove.
- Action tiers:
  - **Create drafts** (review modal): "Save N emails as drafts in <account>? Nothing is
    sent." + Send-from select + recipient preview (first 3) → success toast
    "N draft(s) saved in <account>." with **Open Gmail** action; partial failures →
    error toast "N … could not be saved. View them in the Failed filter." + View action.
  - **Schedule / Reschedule**: time picker (same modal) → review modal with the checkbox
    **"I checked the preview and the recipients"** (confirm disabled until ticked) +
    Send-from + recipient preview → toast "N emails scheduled for <when>." with **Undo**
    (reverts the previous status/times).
  - **Send now** (only when Safe mode is off; otherwise the sidebar mode card opens):
    review modal "Send N emails now?" + same checkbox + **"Hold to send N emails"** →
    after the hold, a **10-second cancel countdown** replaces the modal body:
    "Sending N emails in 10 … You can still change your mind." with a cancel action,
    then the send executes (this is the undo window the send.js flow does **not** have).
  - **Stop** (scheduled/queued): stops the emails → status "Stopped".
  - **Remove**: deletes from the app (Gmail drafts untouched).
- Deep links used by the hub next-step cards: `?tab=emails&status=<s>` and
  `?do=drafts|schedule` (batch actions run automatically on load).

### 11.2 Replies

- Replies tab (`/replies`) shows inbound replies matched to sent emails; marking/replying
  happens there; the hub's Replied stat and funnel update from it.
- Bounces (DSN) are detected automatically and show as "Bounced" with a "Fix addresses"
  next-step card.

---

## 12. Backend endpoints this UI calls (for the correction plan)

| Endpoint | Method | Used for |
|---|---|---|
| `/api/campaigns` | GET | campaign list + counts |
| `/api/campaigns` | POST | "Create & use" in picker |
| `/api/sheets` | GET | list cards |
| `/api/sheets/<id>` | GET | sheet rows (for samples + checks) |
| `/api/sheets/upload` | POST (multipart) | CSV/XLSX upload |
| `/api/sheets/paste` | POST | paste-a-table |
| `/api/contacts[?sheet_id=]` | GET | audience rows |
| `/api/templates` | GET | template cards |
| `/api/templates/upload` | POST (multipart) | HTML template upload |
| `/api/templates/ai` | POST | AI-written template |
| `/api/accounts` | GET | connected send accounts |
| `/api/state` | GET | settings, safe mode, AI on, my email, stagger, daily cap |
| `/api/settings` | POST | toggle safe mode (live sending modal) |
| `/api/merge/suggest` | GET | field→column suggestions |
| `/api/ai/mapping` | POST | AI mapping |
| `/api/merge/preview` | POST | up to 8 rendered previews |
| `/api/emails/test` | POST | "Send me a test" (draft in safe mode) |
| `/api/merge/generate` | POST | create batch + execute outcome |
| `/api/batches/<id>/attachments` | POST (multipart) | attach files to batch |
| `/api/batches/<id>/send-as` | POST | finalize after attachments |
| `/api/batches/<id>/reminders` | POST | follow-up setup |
| `/api/emails/draft` `/api/emails/schedule` `/api/emails/revert` `/api/scheduler/trigger` | POST | hub Emails-tab actions |

---

## 13. Status & vocabulary reference (as shown on these screens)

- Campaign status: Draft (circle-dashed) / Active (zap) / Finished (check).
- Email status: Ready / In Gmail drafts / Scheduled / Sent / Replied / Bounced / Failed /
  Stopped.
- Contact status: Not contacted / Ready / In drafts / Scheduled / Sent / Replied /
  Bounced / Failed.
- Outcomes (send flow): drafts / schedule / send / queue — with the exact card copy in
  §4.7.
- Modes: "Drafts only" (sidebar card: "Nothing is sent. Emails are saved in Gmail for you
  to review.") vs "Live sending ON" ("Scheduled and immediate sends will go out.").

---

## 14. Observed rough edges & inconsistencies (seeds for the correction plan)

Facts collected while extracting the spec; verify each against intent:

1. **Two different "send now" confirmations.** The send flow uses a 2 s hold and then
   sends immediately with **no cancel window**; the hub Emails-tab "send now" adds a
   10-second countdown cancel. A batch of thousands sent from the send flow cannot be
   stopped. (The backend supports an `internal_hold_seconds` undo window that the send
   flow never uses.)
2. **Preview cap of 8** is invisible to the user except via "Person 1 of 8 shown" — a
   5,000-person send is checked on 8 samples; the checks themselves do run over all rows,
   but the *body* preview doesn't.
3. **Full re-render on every change**: typing in the CC input or follow-up days input only
   commits on `change`, but every commit re-renders the whole page (focus loss for
   anything not re-focused; the match panel re-renders after a 350 ms timeout).
4. **Attachments not persisted** across refresh (disclosed in help text, but the flow
   still requires re-selecting mid-batch if the user refreshes after adding them).
5. **AI template creation redirects away** from the send flow to the template editor;
   the user must navigate back (state is saved, but it's an interruption with no
   "back" affordance in the editor for this specific case).
6. **Step-2 footer summary says "Checking…"** until previews load, while the primary
   button label already shows the final count — brief inconsistency on slow networks.
7. **Safe mode can be toggled mid-flow** from the sidebar without the send flow noticing
   (no `cm:state` listener in send.js) — the "Send now" card only re-evaluates on the
   next local re-render.
8. **Campaigns-list Continue banner** links to `/send?campaign=<cid>`, but the page also
   restores from `cm_send_<cid>` on `/campaigns/<cid>/send`; both work, but the banner
   always uses the generic `/send` URL.
9. **`contactIds` is silently cleared** when the user picks a different list card (by
   design, but not announced) — a 500-row selection is lost without a toast.
10. **Test mail uses the full audience's first row** for fills; in safe mode it's saved to
    Drafts — easy to miss it wasn't sent (the toast clarifies, but the button label
    doesn't say "test" mode differs).
11. **Duplicate emails still each get an email** (check wording says so) — worth a
    product decision, flagged for the plan.
12. **The "See how" disclosure** (all-matched case) renders the match panel only when
    opened; the table is fully interactive there, not read-only — changing a mapping from
    the "see how" view is possible but unexpected.

---

## 15. File map for this flow

```
ui/send_new.html                 page template (mount point, crumb, scripts)
ui/campaigns_new.html            campaigns list (entry point)
ui/campaign_hub.html             campaign detail (entry point)
ui/base_new.html                 app shell (sidebar, topbar, bottom tabs, mode card)
static/js/pages/send.js          the entire send flow (1,255 lines)
static/js/pages/campaigns.js     campaigns list behaviour
static/js/pages/hub.js           campaign hub behaviour
static/js/pages/emails.js        Emails tab (post-send management)
static/js/components/timepicker.js  shared time picker modal
static/js/core/ui.js             modal / toast / holdToConfirm / errorCard / emptyState
static/js/core/shell.js          sidebar mode card, palette, state polling
static/js/core/copy.js           status vocab + message builders
static/js/core/format.js         fmtInt/fmtLong/parseWhen/localToUrl etc.
static/css/pages/send.css        send-flow layout (stepper, opt-cards, preview paper,
                                 checks, addons, success screen, countdown)
app.py                           routes + API endpoints (§12)
services.py                      merge() pipeline, daily cap, batch_send_as
presend.py                       server-side pre-send checks (used by AI path)
approval.py                      approval tokens (AI/agent-initiated actions)
```

---

## 17. Screenshot reference (zoomed-out, full-page captures)

Location: `docs/screenshots/send-flow/`. All six are **zoomed-out full-page captures**
(1440 px viewport, entire scrollable page, 1x scale) taken from the live local app on
2026-11-15: dark theme, campaign "Diwali Outreach" (1,046 contacts, template "Post-Diwali
Outreach" with fields `Recruiter Name` + `Organisation Name`, Gmail account "Suparna
(BITS)"). No real email was created or sent while capturing (the create endpoint was
mocked where needed).

| # | File | State shown | How the state was produced |
|---|---|---|---|
| 1 | `01-step1-default.png` | Step 1 "What are you sending?" complete: campaign line, "All contacts" list card, contact tools, three template cards (Default selected), green "We matched all 2 fields automatically." + "See how", Send-from select, all four outcome cards (drafts selected), Extras accordion (Signature On), sticky footer "Check the 1,046 emails" | Natural first visit to `/campaigns/1/send` |
| 2 | `02-step1-match-panel.png` | Step 1 with the field-matching warning ("2 fields need your help." + Hide) and the open match table: per-field dropdowns showing the options (Pick a column / company / hr_name / hr_email / Type my own text… / Leave blank) | `/api/merge/suggest` mocked to return no suggestions, so both fields stay unmapped |
| 3 | `03-step1-timepicker.png` | Step 1 with outcome "Schedule for later": When block ("Pick a time") and the time-picker modal open — quick chips, date + 15-min time slot, zone line "Asia/Kolkata (IST) — …", "Space them out" segment, live first/last line | Outcome pre-set to `schedule` in the saved flow state, then "Choose time" clicked |
| 4 | `04-step1-safemode-modal.png` | Step 1 with Safe mode ON: amber banner "Safe mode is on - immediate sending is blocked.", the disabled "Send now" card, sidebar mode card "Drafts only", and the "Turn on live sending?" modal (amber warn + Keep safe mode / Turn on live sending) | `/api/state` mocked with `safe_mode: "1"` (the current workspace has Safe mode OFF) |
| 5 | `05-step2-check.png` | Step 2 "Check the emails": "What they'll get" preview (Person 1 of 8 shown · 1,046 people in total) with To line + subject, Quick checks card (all green) + "Send me a test", Summary card (To / Template / From / Signature / outcome line), footer "Create 1,046 drafts" | Natural click through from Step 1 (draft outcome) |
| 6 | `06-send-confirm-modal.png` | Step 2 (send outcome, Safe mode off) with the "Send 1,046 real emails?" confirmation modal: sub-line with the sending account, "If this is the first time…" warn, "Hold the button for 2 seconds to confirm.", Cancel / "Hold to send 1,046" | Outcome pre-set to `send`; modal captured before holding (nothing was sent) |
