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
  - Overview (counts, notes, delete).
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
  are flagged with the reason so you can fix the list and re-send.

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

The Ask AI drawer (top bar sparkle, or ⌘K → "Ask AI") is a full agent over the
platform. It is *not* a chatbot that guesses: every number it quotes comes from a
tool call against your real database, and **every action that changes anything is
shown to you as a confirmation card before it runs**.

### 4.1 What it can do (its tools)

**Read (runs immediately, shown as little "zap" step chips):**
| Tool | What it sees |
| --- | --- |
| `get_overview` | Campaigns, contacts, sent, replied, bounced, scheduled, queued, templates |
| `list_campaigns` / `get_campaign` | Campaigns with email counts; per-campaign deep stats, lists, batches |
| `list_templates` / `get_template` | Templates with fields; full body HTML |
| `list_contacts` | Total + sample rows, searchable |
| `get_replies` | Who replied, when |
| `list_team` | Members, roles |
| `recent_activity` | Recent log entries |

**Change (always requires your Yes/No confirmation first):**
| Tool | What it does |
| --- | --- |
| `create_campaign` / `update_campaign` / `delete_campaign` | Campaign CRUD. Delete confirmation states exactly how many email records will be removed and that Gmail copies are untouched. |
| `create_template` / `update_template` / `delete_template` / `activate_template` | Template CRUD; activate = default template for new sends. If you attach a .docx/.html/.txt, it can create the template straight from the file. |
| `import_contacts_sheet` | Imports the CSV/XLSX you attached as a new contact list, optionally linked to a campaign. |
| `send_test_email` | Sends a real test email from your connected Gmail (e.g. to yourself to check signature/images/attachments). |
| `prepare_send` | The full send pipeline: personalises the template for the campaign's list (auto-mapping fields → columns) and runs it as **queue / drafts / schedule (at a time you give) / send now** — same safety rails as the UI (safe mode, daily cap, schedule limits). |
| `cancel_emails` | Stops queued / drafted / scheduled emails so they won't go out. |

**Interaction:**
| Tool | What you see |
| --- | --- |
| `ask_user` | A question card: a few clickable option buttons **and/or** a text box when it needs you to type something (a name, an address, a time). It waits for your answer and continues. |

### 4.2 How the conversation works

1. You type (and optionally attach a file). The assistant may call several read tools
   in one turn — each appears as a collapsible step chip so nothing is hidden.
2. If it wants to **change** something, it stops and shows:

   > **CONFIRM ACTION** — Create campaign "Winter 2027"
   > `[Yes, do it]` `[No, cancel]`

   "No" tells the model it was declined; it adapts instead of retrying.
3. If it needs **input**, it shows:

   > **A QUICK QUESTION** — Which email should the test mail go to?
   > `[f20230611@goa...ac.in]` `[adityaa@goa...ac.in]` `[type your answer…]`

4. It continues only after you answer, and answers your follow-ups in the same
   conversation. Conversations last ~2 hours (in memory) and restart fresh each time
   you open the drawer; your message history is kept locally in the browser.

### 4.3 File uploads in the chat

Paperclip button next to the input (max 5 MB):
- **`.csv` / `.xlsx` / `.tsv`** → parsed as a contact list; it reports columns + row
  count and offers to import (confirmation card first).
- **`.docx` / `.html` / `.txt`** → converted to the email HTML and offered as a new
  template (you can edit it in the Library afterwards).
- Other types are rejected with a clear message.

### 4.4 Example session

```
You:   Summarise what needs my attention
AI:    15 emails bounced (bad addresses), 2 replies waiting. [zap chips shown]

You:   Create a campaign for the winter internship season
AI:    CONFIRM ACTION — Create campaign "Winter Internship 2027"?  [Yes, do it] [No, cancel]
You:   (clicks Yes, do it)
AI:    Created — campaign #2. Want me to import a contact list?

You:   (attaches winter_recruiters.xlsx) Import this one
AI:    I found winter_recruiters.xlsx: 240 rows — company, hr_name, hr_email.
       CONFIRM ACTION — Import attached file "winter_recruiters.xlsx" as a contact list (240 rows)?  [Yes, do it] [No, cancel]
You:   (Yes)
AI:    Imported as sheet #7 and linked it to Winter Internship 2027.

You:   Draft the emails from my Diwali template, don't send
AI:    CONFIRM ACTION — Create 240 Gmail draft(s) for campaign "Winter Internship 2027" (nothing is sent)  [Yes, do it] [No, cancel]
```

### 4.5 Guardrails

- It cannot bypass safe mode or the daily send cap — the same code path as the UI is used.
- It never deletes Gmail copies of anything; "delete" only removes app records.
- Mutations are impossible without your explicit Yes.
- If AI is not configured (no key), the drawer tells you to add a key in Settings → AI features.

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
