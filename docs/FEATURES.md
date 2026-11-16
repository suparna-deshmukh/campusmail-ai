# CampusMail AI — Complete Platform Guide

A placement-team email outreach platform. You bring a contact list (recruiters /
companies) and an email template; the platform personalises it for every recipient,
sends it through your own Gmail account at the pace you choose, tracks who replied or
bounced, and lets an AI assistant help you — or run most of it — from a chat.

Built for the BITS Pilani (Goa) placement unit, but the team model works for any small
outreach team. Runs on the laptop (Flask app, SQLite) and mirrors to a cloud instance
(PythonAnywhere) through a sync relay, so both stay in step.

---

## 1. Roles

| Role | What they can do |
| --- | --- |
| **Admin** (e.g. Suparna Deshmukh) | Everything: campaigns, sends, team members & roles, settings, branding, danger zone. |
| **Member (user)** | Full day-to-day work: campaigns, templates, contact lists, sends, replies, team analytics (with per-person filters). Cannot manage members or platform settings. |
| **Not on the team** | Sees a personal blank workspace (their own private DB) where they can create their own team, campaigns and templates. Inviting them into the placement team switches them onto the shared workspace. |

---

## 2. Feature map by page

### Home (`/`)
- Greeting + "Live sending ON/OFF" status card (toggles safe mode).
- Stat tiles: Emails sent, Replies, Reply rate, Scheduled (real data only).
- **Needs your attention**: unread replies, bounced emails (with reasons), failed sends, upcoming sends in 24 h, old drafts — each with a one-click action ("Fix addresses", "Review", "Open replies").
- **Upcoming sends**: scheduled batches with time, campaign link and a "…" menu.
- **Your campaigns** cards: contacts emailed, progress bar, Sent / Replied / Reply rate, last activity, and a "Send emails" shortcut.

### Campaigns (`/campaigns`)
- Campaign list (name, contacts, sent, replied, reply rate, status badge).
- **Campaign hub** per campaign:
  - Overview (counts, notes, delete). The sent count splits into
    *delivered so far* and *verifying*: Gmail retries undelivered mail
    for up to ~72 h, so mail inside that window (setting
    `delivery_verify_hours`, 0–168 h, default 72) shows as verifying
    instead of being claimed delivered; bounced mail is terminal and
    stops all re-checking.
  - **Audience**: the contact lists linked to the campaign (rows, columns, preview) — add/remove lists, import new ones.
  - **Template**: which template the campaign uses; open the template editor.
  - **Send emails**: the 3-step send flow (below).
- Create/edit/delete campaigns (deleting removes queued + sent records from the app; copies already in Gmail are never touched).

### Sending emails (`/campaigns/<id>/send`) — 3 steps
1. **Audience** — pick the contact list(s), optional serial range (e.g. only rows 120–200), see exactly who will be included.
2. **Template & personalisation** — pick a template (or write one inline with an AI "write it for me" brief), map each template field (`{recruiter_name}`, `{company_name}`, …) to a contact-list column, or to a fixed literal value. Live previews of the first recipient and per-person previews rendered as real email "paper".
3. **Timing & action** — choose one:
   - **Queue** — personalised emails sit in the queue, nothing sent.
   - **Drafts** — creates real Gmail drafts (review in Gmail, nothing sent).
   - **Schedule** — sends automatically at a chosen time (5 min ahead to 45 days out), with optional stagger (minutes between emails) so Gmail doesn't flag you.
   - **Send now** — immediate (blocked while Safe mode is on).

Safety rails on every send: **daily cap** (default 100/day, configurable), **safe mode**
(hard block on immediate sends), CC list (max 20, supports per-row merge fields like
`cc: {company_hr}`), optional signature append, and batch attachments (PDFs etc. sent
with the mail). Reminders: schedule follow-up emails N days later (1–5 rounds) to
non-repliers.

### Replies (`/replies`)
- Inbox of everything that answered a sent email, with filters (campaign, member,
  status, search) and unread markers.
- Thread view: the original send, the reply, and **AI-suggested replies** you can edit
  and send from Gmail (or mark handled, snooze, or mark replied manually).
- Bounce handling: delivery-failure notices (Google's "5.1.1 mailbox not found" etc.)
  are detected by polling the Gmail account's bounce notifications; bounced contacts
  are flagged with the reason so you can fix the list and re-send. Any *permanent*
  failure (final "Delivery Status Notification (Failure)", whatever the reason -
  bad address, policy block, ...) marks the mail bounced; *temporary* problems
  ("Gmail will retry for N more hours") leave it as *verifying* and are logged in
  the activity feed.
- Reply polling runs every ~40 s over all sent mail (rotating, nothing is
  starved). A mail is checked for replies only once it is confidently
  delivered — past the delivery window above — so the Replies inbox only
  contains answers to mail that actually got through.

### Library (`/library`)
- **Templates**: your uploaded/created email templates. Word (.docx), HTML or plain
  text upload; rich editor with a toolbar (bold, italic, underline, lists, undo/redo,
  insert fill-in field, insert image), a live white-paper preview that renders exactly
  what the recipient sees, and fill-in ("mail merge") field chips. One template can be
  marked **active** (default for new sends). Preview as a specific contact.
- **Contact lists (sheets)**: uploaded CSV/XLSX lists with column detection
  (company / recruiter / HR email), row counts, campaign linkage, delete protection
  while a batch uses them.
- **Attachments**: files (e.g. JDs, brochures) stored and attachable to batches.

### Team (`/team`)
- **Results**: per-member and per-campaign analytics — total, sent, replied, bounced,
  reply rate — with **person, campaign and date filters** (any member can look at
  anyone's numbers).
- **Activity**: the full activity log (sends, imports, sync events, bounces, admin
  actions).
- **Members** (admin only): invite by email, change roles (admin/user), remove.

### Settings (admin)
- **Gmail & accounts**: step-by-step guide to connecting Gmail (OAuth), multiple
  accounts, per-account status, last used, remove.
- **Sending**: daily cap, default stagger, and "what happens if the app was off when
  a send was due" (send when back vs mark as missed).
- **Signature**: HTML signature editor with live preview, image upload for logo/photo.
- **AI features**: on/off toggle, API key, model, base URL, connection test (see §5).
- **Team branding** (admin): team name shown in the sidebar, login footer line,
  admin contact email.
- **Advanced**: sync status, data export, danger zone.

### Help (`/help`)
In-app guide: how to connect Gmail, create a campaign, import a list, write/merge a
template, schedule sends, read replies, and what safe mode does.

### Global
- **Command palette (⌘K / Ctrl+K)**: quick actions — Send emails, New campaign, Import
  contact list, New template, Toggle live sending, Open replies, Ask AI.
- **Ask AI** (top bar sparkle): the agentic assistant — §4.
- **Theme**: deep-black dark theme by default (Linear/Vercel-style), light theme
  available; per-choice persisted.
- **Activity log**: everything notable is recorded (who did what, when).

---

## 3. Gmail integration

- Connect via Google OAuth from Settings (step-by-step built in). Scopes requested are
  the minimum needed: **`gmail.compose`** (create drafts / send / read message ids)
  and **`gmail.readonly`** (detect replies and bounce notifications). Nothing else.
- Multiple accounts supported; each send batch is tied to one account.
- Drafts and sends go through the **Gmail API** (not the web UI), so they appear
  normally in your Sent folder and threading/replies work.
- **Reply detection**: the scheduler periodically checks each sent email's thread in
  Gmail for incoming messages from the recipient and marks the email replied.
- **Bounce detection**: polls Google's bounce notification messages; bounces are
  attributed to the original email + contact with the SMTP reason stored.
- Known limitation: the `gmail.modify` scope is not requested, so the app cannot
  move/delete items already in Gmail (e.g. it can't un-send a test mail from your
  inbox) — it only ever creates drafts/sends new messages and reads.

---

## 4. Ask AI — the agentic assistant

The Ask AI drawer (top-bar sparkle, ⌘K → "Ask AI", or `?ask=1`) is a full
agent over the platform. It can do **anything your role can do in the UI**,
so you can live in the chat. It is not a chatbot that guesses: every number
comes from a tool call against the real database, and **every action that
changes anything passes a server-side approval gate** — the app cannot run a
mutation without the exact card you approved (approval tokens are bound to
the parameter hash; edits void the old approval; every card response is
idempotent, so double-clicks and two tabs can never double-send).

### 4.1 Risk levels — what needs what

| Level | What | How it runs |
| --- | --- | --- |
| **R0 Read** | Overview, campaigns, templates, lists, replies, threads, bounces, batches, schedule, analytics, team, activity, accounts, settings, help, AI usage | Runs immediately, rendered as a data card. No confirmation. |
| **R1 Generate** | Write/polish a template or email, suggest subjects, suggest a reply, write a reminder | One LLM call; the result lands in an **editable draft editor** — nothing is saved or sent by generation alone. |
| **R2 App write** | Create/edit campaigns, save/rename/activate templates, import lists, attach lists/templates, update contacts (bounce fix), mark reply handled/snoozed, reminders text, settings | A **confirm card** (exact before → after). |
| **R3 Destructive** | Delete campaign/template/list, cancel emails, remove member, change role | Confirm card with **impact counts** and "cannot be undone in the app; Gmail copies are not touched". |
| **R4 Outbound** | Drafts, test email, schedule, send now, one-off email, send reply, reminders scheduling | **Final review card** (the exact rendered email, recipient browser, safety panel, pre-send check) → confirm with a labelled button like `Send 240 emails now`. |
| **R5 Platform-critical** | Safe-mode toggle, AI settings (key via masked input), remove Gmail account, data export | Admin only + confirm card with consequence text. Danger-zone resets are a Settings handoff only. |

Main tools: `get_overview`, `get_attention_items` (Home "needs your
attention", same counts as Home), `get_replies`/`get_reply_thread`,
`list_bounces`, `get_upcoming_sends`/`list_batches`, `get_results`
(analytics), `render_preview`, `list_gmail_accounts`, `get_settings`,
`get_send_status`, `get_help`, `get_ai_usage`, `run_presend_check`;
`create/edit/delete_campaign`, `attach/detach_sheet`, `set_campaign_template`,
`create/update/rename/activate/delete_template`, `import_contacts_sheet`,
`delete_sheet`, `update_contact`, `prepare_send` (queue / drafts / schedule /
send now), `send_adhoc_email` (one-off), `send_test_email`, `send_reply` /
`create_reply_draft` (threaded with the original Message-Id),
`set_reminders` (1–5 rounds), `reschedule_batch`, `cancel_emails`,
`mark_reply_handled`/`snooze_reply`/`mark_replied_manual`, `set_live_sending`,
`set_send_limits`, `set_signature`, `set_branding`, `set_ai_settings`,
`invite_member`/`change_role`/`remove_member`, `approve/deny_join`,
`remove_gmail_account`, `start_gmail_connect` (handoff), `export_data`,
`undo_send`.

### 4.2 How the conversation works

- **Questions are buttons.** Choices are option cards (≤ 4 options +
  "Something else…"); free input appears as the right input card in the
  chat (text / number / date-time / masked secret / file drop zone /
  editable table) — never "type your answer in a message".
- **One decision per card; edits are free, executing is not.** You can edit
  every field in a draft/review card without approval; only the labelled
  primary action executes. Any edit after an approval voids it.
- **Typing while a card is pending** cancels nothing silently: the pending
  card is marked *superseded* (inert, greyed) and your new message is
  handled normally. "Cancel" words cancel the card.
- **Multi-step requests show a plan first** (read-only step list with risk
  badges); each step still gets its own gate. If step N fails, steps 1…N−1
  stay done and you choose retry / skip / stop.
- **Closing the drawer** cancels every pending interaction; draft-editor
  buffers are kept for 2 h and offered as "Resume unsaved draft?".
- **Two modes** (drawer header): **Guided** (default — one question per
  card) and **Compact** (one editable review card gathers timing/stagger/CC/
  signature/attachments/reminders for a send). Same actions, same gates,
  same pre-send check in both; Compact never pre-selects the send action or
  the schedule time, and remembers your usual stagger/CC/signature choice
  (never the action itself).
- Conversations last ~2 h in memory; message history is kept locally in the
  browser. Answered cards collapse to one line and stay expandable.

### 4.3 Sending email from the chat (the flagship flow)

1. You ask (e.g. "draft the Diwali follow-up and send it tomorrow 10 am").
   One LLM call writes subject + body into the **draft editor**: From
   account, recipients/audience, CC (≤ 20, merge fields allowed), subject
   (+ "Suggest subjects"), the white-paper rich-text body with merge-field
   chips and "Preview as contact", attachments, rendered signature toggle,
   and `Revise with AI…` (uses your current edited text as the base).
2. **What next?** — `Save as template` · `Send` · `Schedule` · `Queue` ·
   `Gmail drafts` · `Send test to me` · `Discard`.
3. **Final review** (R4): the exact email as sent (merge fields resolved,
   signature appended, images resolved), a recipient browser ("Recipient 1
   of 240"), the send facts (time, stagger → "last email ≈ 14:35",
   reminders) and the **safety panel** (safe mode, today X/cap, remaining,
   account/token status, rows without email, duplicates).
4. **Pre-send check** (deterministic, zero LLM) above the buttons: ✗
   blocking lines keep the button disabled, ⚠ warnings and ℹ suggestions
   never block. Includes merge-field health (blank values list the affected
   serials), link syntax checks (and best-effort reachability that can
   never block a send), and "template never tested — send a test to me"
   for ≥ 10 recipients.
5. **Bulk guard**: at or above the bulk threshold (default 50, admin-set
   10–500) the confirm needs a "I've reviewed N recipients" checkbox, and
   at 4× the threshold a typed recipient count that must match. Verified
   server-side.
6. **Send now has an undo window** (default 20 s, admin-set 10–30): after
   you confirm, a countdown card with one **Undo** (no confirmation) holds
   the batch — nothing reaches Gmail during the hold, and the cap
   reservation is released on undo. After release it says plainly that
   mail already sent cannot be recalled.
7. Result cards are deterministic (counts, IDs, deep links); next steps are
   offered as buttons. Everything is logged as "… via Ask AI".

Replies (F-42): the unhandled-reply list → pick a reply → `Draft a reply`
(one LLM suggestion, fully editable, threaded on the original
Message-Id) → `Send reply` / `Save as Gmail draft` / `Open in Gmail`, plus
`Mark handled` / `Snooze` (1 d / 3 d / 1 wk / custom). Bounces (F-43):
`list_bounces` with reasons → edit the corrected addresses in a table →
`Update N contacts` → optional **re-send to the fixed contacts only**.

### 4.4 File uploads in the chat

Paperclip or in-card file drop zone (max 5 MB): `.csv` / `.xlsx` / `.tsv`
→ contact list (parse preview first); `.docx` / `.html` / `.txt` → template;
`.pdf` / `.png` / `.jpg` → only as attachments / signature images /
template images. Wrong types are rejected with a clear message.

### 4.5 AI budget

Each user has a daily AI budget (calls + tokens; admin can set per-user and
workspace limits in Settings → AI). At 80% you get one inline notice; at
100% the AI degrades gracefully — reads, chips and deterministic flows
keep working, free text gets button options instead of LLM answers, and
drafts open empty with "write it yourself". Counters are server-side; the
key pool (two keys, 50 calls/day each) rotates transparently.

### 4.6 Example session

```
You:   What needs my attention?
AI:    [data card] 15 bounced, 2 replies waiting, 12 due tomorrow. (0 LLM calls)

You:   Draft the Diwali follow-up and send it tomorrow 10 am, stagger 2 min
AI:    [draft editor — subject + body, editable] … [Continue]
AI:    What next?  [Send] [Schedule] [Queue] [Gmail drafts] [More…]
You:   (Schedule) → date-time card (min +5 min, max +45 days) → stagger card
AI:    [final review — rendered email, 240 recipients, safety panel, all checks passed]
       [Schedule 240 emails for 20 Nov, 10:00]
You:   (confirm)
AI:    ✓ Scheduled 240 emails for 20 Nov 10:00 — first at 10:00, last ≈ 10:47.

You:   Send the welcome test to me
AI:    [final review of the test render] [Send test to f20230611@goa…]
You:   (confirm)
AI:    ✓ Sent. [Gmail received; verified]
```

### 4.7 Guardrails

- **Server-enforced approval gate.** Mutations run only through
  `execute_gated` with a valid, unexpired, unused approval token bound to
  the exact parameters and preview you saw. The model and the client never
  submit execution parameters. Direct calls to mutating functions raise.
- **Role-aware.** Admin-only tools are not even offered to members, and the
  server re-checks on every action; a demoted user mid-chat gets a friendly
  403 message, not a blank state.
- **Same rails as the UI.** Safe mode, daily cap, 5 min → 45 day schedule
  window, CC ≤ 20, idempotency, and the scheduled-send double-send guard —
  one shared service layer for UI and agent.
- **No Gmail deletion, no new OAuth scopes.** The assistant never claims it
  can un-send or delete Gmail items; every delete card says "Gmail copies
  are not touched." Scopes remain `gmail.compose` + `gmail.readonly`.
- **Secrets stay secret.** API keys travel only through the masked
  secret-input card; they never enter LLM prompts, logs, activity entries
  or browser storage (covered by tests).
- **Prompt-injection aware.** Email bodies, contact data, replies and
  uploaded files are treated as untrusted data; at worst an injection can
  *propose* an action, which you still see and approve.
- **LLM-call economy.** Explicit requests route deterministically (0 LLM
  calls); free text uses exactly one routing call; generation is one
  structured call (one repair attempt only); narration is number-guarded
  and dropped if it can't be verified. Typical full send flow: 2–4 calls.

---

## 5. How the AI is configured

Settings → **AI features** (admin):
- **On/off toggle** for all AI features.
- **API key** (stored in the app's settings table, never sent anywhere except the
  provider's API; blank by default).
- **Model** — any model name the endpoint understands.
- **Base URL** — any **OpenAI-compatible** `/chat/completions` endpoint. This is what
  makes the provider pluggable.

**Current setup on this instance:**
- Base URL: `https://generativelanguage.googleapis.com/v1beta/openai` (Google's OpenAI-compatible Gemini endpoint)
- Model: `gemini-3.6-flash`
- Key: set (visible in Settings; the key itself is not echoed back to the UI)
- **Test connection** button verifies key + endpoint + model in one call.

Because the client is OpenAI-compatible, switching providers (OpenAI, Groq,
OpenRouter, a local server, etc.) is a paste of new base URL + model + key.

### Where AI is used across the app
| Feature | What the AI does |
| --- | --- |
| Ask AI agent | Tool-calling loop (up to 8 steps per turn) over the 20 tools in §4.1 |
| Template editor / send wizard | Writes a template from a plain-English brief ("audience, the ask, anything personal"), polish instructions |
| Subject lines | Suggests/improves subject lines |
| Mail merge mapping | Suggests which list column fills which template field |
| Replies | Suggests a reply to a recruiter's message (edit before sending) |
| Reminders | Writes polite follow-up/reminder emails |

---

## 6. Mail merge & personalisation

- Placeholders in templates: `{field}`, `[Field Name]`, `{{field}}` are all recognised
  (the editor wraps them in indigo chips).
- In the send flow you map each field to a **column of your list** or to a **literal
  value** (the same text for everyone, e.g. your name).
- Previews render the real merged email (recipient paper) for any contact, including
  images (`local:` references to files you uploaded) and attachments.
- CC lines support merge fields too.

---

## 7. Data & sync (laptop ⇄ cloud)

- Local store: SQLite (`data/campusmail_ai.db`). Tables: `users`, `campaigns`,
  `email_templates`, `sheets`, `batches`, `contacts`, `emails`, `gmail_accounts`,
  `activity`, `settings`, `user_settings`.
- **Sync relay v2** (Supabase-hosted edge function) keeps the laptop and the
  PythonAnywhere copy in step:
  - Diff-only pull by a monotonic `rev` (last-write-wins), explicit row ids,
    tombstones for deletes (a deleted row is never resurrected).
  - Guardrails in the relay: version handshake, per-pull caps, 24 h egress budgets,
    kill switch — designed so a sync bug can never burn bandwidth (see
    `docs/SYNC_V2.md` and `docs/SYNC_INCIDENT_FORENSICS.md`).
  - Synced tables: campaigns, email_templates, sheets, batches, contacts, emails.
    (Users, settings and activity are per-instance on purpose.)
- **Files** (signature images, batch attachments, template images) live in a Supabase
  storage bucket shared by both instances.
- **Cloud sending**: when the laptop is asleep, scheduled sends ride on the cloud
  instance via the relay's send queue ("Live sending ON" card shows the state).

### Current real data on this instance
- 1 campaign — **Diwali Outreach** (966 contacts, 50 emails sent: 35 delivered,
  15 bounced, 0 scheduled, 0 replies yet).
- 3 templates — *Post-Diwali Outreach* (the one the 50 mails were sent with, active),
  *BITS Sem 2 (Word upload)*, *BITS Goa Placement Invite (Sem 2)*.
- 2 team members (Suparna Deshmukh — admin; Aditya A — member).

---

## 8. Safety model, in one paragraph

Nothing sends without a deliberate choice (queue / drafts / schedule / send-now),
immediate sends are hard-blocked by safe mode, every day has a send cap, every
AI-driven change is gated by a Yes/No card, Gmail access is scoped to
compose + read-only, and deletes never touch what's already in your Gmail account.
The activity log records all of it.

---

## 9. Running it

- **Local**: `python app.py` → http://localhost:5001 (env `CAMPUSMAIL_AI_PORT` to change).
- **Cloud (PythonAnywhere)**: same code; keep it current with `git pull`, restart the
  web app, and run `python scripts/purge_stale.py` (dry-run first, then `--yes`) after
  big sync changes.
- Design system: CSS tokens in `static/css/tokens.css` + `theme-dark.css`; component
  gallery at `/_kit` for dev reference.

---

## 10. User flows — complete catalog (planning input)

This section enumerates **every user flow the platform supports or can be in**,
per actor, with the surfaces involved and the states/edge cases each one must
handle. Use it to build test/audit plans.

**Actors:** `X` = not signed in · `N` = new, not yet on the team (pending) ·
`M` = active team member (role `user`) · `A` = admin.

**Conventions:** each flow lists *entry → steps → surfaces → states to verify
(empty / loading / error / success) → edge cases*. "Safe mode" = live sending
switch OFF in Settings. "Cap" = daily send limit.

### A. Auth & access

- **F-01 Google sign-in, first user** (`X→A`)
  - Entry: `/login` → "Continue with Google". Steps: OAuth → Google profile read
    → user created as **admin** (first account) → redirect to `/`.
  - Edge: Google profile read fails (login page shows the error, no half-created
    user); OAuth client not configured ("Google ready" false on login page).
- **F-02 Google sign-in, unknown person** (`X→N`)
  - User created with status `pending` → `bind_tenant` routes them to a **personal
    workspace** (`data/workspaces/<hash>.db`): they are admin *of their own*
    private instance (own campaigns/templates/lists), completely isolated from
    the team's data. Login page may also show a "waiting for an invite / request"
    state with an optional note.
  - Edge: sign out/in must land back in the personal workspace; personal data must
    never appear in the team DB or vice versa; deleting personal workspace data is
    theirs only.
- **F-03 Invited pending user joins the team** (`N→M`)
  - Admin invites their email (Team → Members, F-52). On their next request they
    are `active` and land on the **team** workspace with all shared data.
  - Edge: invitation for an existing-but-pending email (status flip only); invite
    typo emails; user logs in on two instances (laptop + cloud) after invite.
- **F-04 Admin-email auto-promotion** (`N/M→A`)
  - If a signing-in email equals Settings → Branding → "Admin email", the user is
    auto-promoted to admin (logged as `user_promoted`).
- **F-05 Session lifecycle** (all)
  - Signed-in session cookie; expired/absent session on any `/api/*` → JSON error,
    on any page → redirect to `/login`; logout clears the session.
  - Edge: closing the drawer/palette mid-request; refreshing a deep link while
    logged out then logging back in.
- **F-06 Role-gated UI** (`M` vs `A`)
  - Members: Team → Members tab hidden (API 403s too); Settings limited to
    non-admin sections (branding/advanced are admin-only — the client redirects
    members to the Gmail section).
  - Edge: a demoted admin still holding the page must get 403 on the next admin
    call, not a white screen.

### B. Admin setup

- **F-10 Connect a Gmail account** (`A`)
  - Settings → Gmail: step-by-step guide (create OAuth client, paste client
    secret, authorize) → OAuth popup/callback → account listed with status.
  - States: client-secret-not-set (guide shown), mid-OAuth, success, error.
- **F-11 Multiple accounts / removal** (`A`)
  - Add a second account; "last used" tracking per user; remove an account
    (in-flight/queued emails on it → defined behaviour: they error or stay).
- **F-12 Configure AI** (`A`)
  - Settings → AI features: toggle, API key (stored, never re-shown), model, base
    URL, **Test connection** (OK/fail with reason). Applies to every AI feature
    incl. Ask AI; without a key, AI surfaces show a "set a key" state, not errors.
- **F-13 Live sending switch (safe mode)** (`A`)
  - Home status card or Settings: ON/OFF. OFF = immediate sends blocked everywhere
    (UI, agent, scheduler) with a clear message; drafts/schedule still allowed.
- **F-14 Branding** (`A`)
  - Team name (sidebar), login footer line, admin contact email — live effect on
    login page + sidebar; invalid/empty values handled.
- **F-15 Signature** (`A`)
  - Editor with live preview; image upload (logo/photo) stored in file store;
    signature append toggle per batch.
- **F-16 Sending limits & behaviour** (`A`)
  - Daily cap, default stagger (minutes), catch-up policy when the app was off at
    send time: "send as soon as back" vs "mark missed".
- **F-17 Member management** (`A`)
  - Invite by email (+ optional name), change role admin/user, remove member.
  - Edge: inviting self, removing self/last admin, re-inviting a removed user.

### C. Campaign lifecycle

- **F-20 Create campaign** (`M/A`)
  - UI: Campaigns → New (name, description). Or Ask AI (F-61). Empty campaign =
    no list, no template, zeroed stats, "No contact list yet" state.
- **F-21 Import a contact list** (`M/A`)
  - Upload CSV/XLSX (Library → lists or campaign audience): automatic column
    detection (company / recruiter / HR email), row count, rows-without-email
    warning, sheet linked to a campaign or not. Delete protection when a batch
    uses the sheet.
  - Edge: messy headers, duplicate emails, 1-row files, huge files, wrong file
    type, .tsv, Unicode names.
- **F-22 Create a template by upload** (`M/A`)
  - .docx / .html / .txt upload → converted (docx→HTML) → editor. Fields auto-
    detected from placeholders; images embedded via `local:` file references.
- **F-23 Write/edit a template** (`M/A`)
  - Editor: toolbar (bold/italic/underline/lists/undo/redo), insert fill-in field
    (chips), insert image, white-paper preview (light **and** dark theme), preview
    as a specific contact, autosave indicator, dirty state, AI "write it for me"
    from a brief, AI polish.
- **F-24 Template lifecycle** (`M/A`)
  - List, rename, edit, **activate** (default for new sends; only one active),
    delete (with sync tombstone), preview modal.
- **F-25 Attach list + template to campaign** (`M/A`)
  - Campaign hub: audience tab (add/remove lists, see real recipient count),
    template tab (choose template).
- **F-26 Send step 1 — audience** (`M/A`)
  - Pick list(s) + optional serial range; recipient preview table; empty-list
    guard ("No one to preview").
- **F-27 Send step 2 — template & mapping** (`M/A`)
  - Choose template; map each field → list column or literal value (AI can
    suggest mapping); per-recipient previews rendered as real email paper.
- **F-28 Send step 3 — action** (`M/A`)
  - **Queue** (nothing sent) / **Drafts** (Gmail drafts) / **Schedule** (time 5
    min ahead → 45 days out, stagger) / **Send now** (blocked by safe mode;
    capped by daily cap; remainder behaviour defined). CC list (≤20, merge
    fields allowed), signature append, batch attachments.
  - Edge: cap reached mid-batch, no account selected, account token expired,
    schedule in the past, schedule > 45 days, empty selection.
- **F-29 Scheduled send execution** (system)
  - Scheduler tick fires due batches; app was off → catch-up policy (F-16).
  - Edge: laptop asleep at due time → cloud instance sends via relay (F-72);
    double-send protection if both instances are awake.
- **F-30 Follow-up reminders** (`M/A`)
  - On a batch: remind non-repliers after N days, up to 5 rounds; reminder emails
    tracked separately (`is_reminder`), cloud-originated reminders recorded.
- **F-31 Campaign edit/delete** (`M/A`)
  - Rename/description (UI + agent); delete removes batches + email records,
    unlinks sheets, records tombstones (cloud deletes too). Gmail copies untouched
    — stated in the confirmation.
- **F-32 Cancel sends** (`M/A`)
  - Cancel queued / drafted / scheduled emails individually or by campaign
    (UI queue actions + agent `cancel_emails`); cancelled rows excluded from
    stats; already-sent rows unaffected.

### D. Daily operations

- **F-40 Home triage** (`M/A`)
  - "Needs your attention" items each with their action: unread replies → Replies;
    bounced → fix list & re-send; failed sends → review; upcoming 24 h → open
    batch; old drafts → review. Every count must reconcile with the pages they
    link to.
- **F-41 Replies inbox** (`M/A`)
  - Filters (campaign, member, status, search), unread markers, empty state,
    pagination/limit for large inboxes.
- **F-42 Handle a reply** (`M/A`)
  - Thread view (original + reply), AI-suggested reply (editable), mark handled,
    manual "mark replied", link out to Gmail.
- **F-43 Bounce handling** (`M/A`)
  - Bounce list with SMTP reason per address; contact flagged `bounced`; fix the
    address and re-send to fixed contacts only.
  - Edge: Gmail bounce notifications missing (fallback: unknown reason shown,
    not hidden).

### E. Team & analytics

- **F-50 Results analytics** (`M/A`)
  - Per member / per campaign: total, sent, replied, bounced, reply rate;
    filters: **person**, campaign, date range. Every member can view everyone's
    numbers.
- **F-51 Activity log** (`M/A`)
  - Filterable feed of actions (sends, imports, sync, bounces, admin ops).
    Transient errors must not spam it (throttled).
- **F-52 Member management** (`A`)
  - See F-17; plus pending-request view (who asked to join) and approve/deny.

### F. Ask AI agent

- **F-60 Data question** (`M/A`) — read tools run immediately, results shown as
  step chips, answer must match DB exactly (verify numbers).
- **F-61 Mutation, approved** — confirmation card → Yes → action executes,
  result reported, activity logged, sync tombstone/push where applicable.
- **F-62 Mutation, declined** — No → model told it was declined, adapts, never
  silently retries the same action.
- **F-63 Question with options** — option buttons (≤4) always plus free-text box.
- **F-64 Question, free text** — e.g. "which email?" → typed answer continues the
  same conversation.
- **F-65 Attach contact list** — CSV/XLSX → described (columns, rows) → import
  confirm (Yes/No) → sheet created, optionally campaign-linked.
- **F-66 Attach template file** — DOCX/HTML/TXT → offered as new template (confirm)
  → editable in Library afterwards.
- **F-67 Multi-step request** — e.g. "create campaign, import this list, draft
  the emails": sequential confirmations, each step verifiable, partial-failure
  behaviour (step 3 fails → steps 1–2 stay, error explained).
- **F-68 Test email to self** — real Gmail send (confirm card states recipient +
  subject); used to verify signature/attachment/image rendering.
- **F-69 Agent edge cases** — AI key missing (friendly pointer to Settings);
  conversation > 2 h (expired → "ask again"); user types while a confirmation is
  pending (must be rejected gracefully, not corrupted); drawer closed mid-turn;
  provider error mid-loop (retry/backoff in client, error shown, state intact);
  unsupported file type (clear message); > 5 MB file (rejected).

### G. Cross-cutting & system flows

- **F-70 Command palette (⌘K)** — all quick actions work from any page; typing
  filters; Enter executes; Esc closes.
- **F-71 Theme** — dark (default) / light; choice persists; **every** surface,
  chart, modal, empty state and the white email-paper must be correct in both.
- **F-72 Sync laptop ⇄ cloud** — create/edit/delete on either instance appears on
  the other (rev-based, tombstones win); files via shared storage; relay
  down → throttled error log + one "recovered" entry; schedule rides on whichever
  instance is awake (no double send).
- **F-73 Fresh-instance / empty states** — every page has a designed empty state
  (no campaigns, no lists, no templates, no replies, no members, no activity);
  first-run seeding behaviour documented and idempotent.
- **F-74 Deep links** — campaign links, `?tab=` filters, `?ask=1` (opens Ask AI),
  invite links; all survive logout → login.
- **F-75 Help** — guide content matches current UI (steps, names, screenshots).
- **F-76 Assets & caching** — all static assets versioned (`?v=`); a code deploy
  must not leave a browser on stale CSS/JS (per-file versioning in the CSS bundle).
- **F-77 Files & storage** — signature image, template images, batch attachments:
  upload, reference in email (`local:`), resolve at send time, delete/cleanup.
- **F-78 Errors & 404s** — unknown routes, API 4xx/5xx surfaces (toast or inline),
  network down mid-action (no partial writes, retry affordance).
- **F-79 Data export / danger zone** (`A`) — export data, reset flows with
  explicit confirmations.

### Actor × surface access matrix

| Surface | X | N (personal) | M | A |
| --- | --- | --- | --- | --- |
| Login / request to join | ✓ | ✓ | ✓ | ✓ |
| Personal workspace (own team) | — | ✓ (full, as admin) | — | — |
| Home / Campaigns / Send / Replies / Library | — | ✓ (own data) | ✓ (team data) | ✓ |
| Ask AI agent | — | ✓ | ✓ | ✓ |
| Team → Results / Activity | — | ✓ | ✓ | ✓ |
| Team → Members | — | ✓ | — | ✓ |
| Settings (gmail, sending, signature, ai) | — | ✓ | ✓ | ✓ |
| Settings (branding, advanced) | — | ✓ | — | ✓ |
| Scheduler (send, replies, bounces, sync) | — | ✓ (own) | shared | shared |

### Intentional behaviours (do not "fix")

- Non-invited users see a **personal blank workspace** by design, not an error.
- The template **editor/preview is white paper** even in dark mode (it represents
  the real email the recipient gets).
- Deleting campaigns/templates/sheets **never touches Gmail** (no `gmail.modify`
  scope on purpose) — Gmail copies remain.
- `users`, `settings`, `activity` are **not synced** between instances (per
  instance); the cloud keeps its own copy of those.
- Only **one white primary button per view**; in-card buttons are secondary.
- Scheduled sends may fire from **either** instance (laptop or cloud) — that's how
  "laptop asleep" works; guard against double-sends is the scheduled status flip.
