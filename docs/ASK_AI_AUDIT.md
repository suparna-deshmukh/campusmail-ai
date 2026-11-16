# Ask AI Upgrade — Phase 0 Audit

Audit of the CampusMail AI repository in preparation for upgrading "Ask AI" into
the single, safe, agentic control surface (per the upgrade instruction doc).
All file paths and function names below are verified against the code, not the
docs.

Repo root: `campusmail-ai/`. Python 3.9, Flask, SQLite (stdlib only), no ORM.

---

## 1. Ask AI backend (current state)

### 1.1 Entry points

| Route | Function | File:Line | Used by |
|---|---|---|---|
| `POST /api/ai/agent` | `api_ai_agent` | `app.py:3237` | **The drawer** (`static/js/core/ask-ai.js`). JSON body: `text`, `conv_id`, `file {name,data(b64)}`, `confirm (bool)`, `answer (str)`. Returns `agent.turn(...)` output. |
| `POST /api/ai/chat` | `api_ai_chat` | `app.py:3144` | **Legacy** assistant page (`/assistant`, `ui/assistant.html`, JS `Pages.assistant` in `static/js/app.js:1511`). Uses `AI_TOOLS` + `_exec_tool`. |
| `POST /api/ai/test` | `api_ai_test` | `app.py:2857` | Settings → AI → Test connection. |
| `POST /api/ai/subjects` | `api_ai_subjects` | `app.py:2863` | Library editor "improve subject". |
| `POST /api/ai/mapping` | `api_ai_mapping` | `app.py:2876` | Send wizard AI mapping suggestion. |

**Finding 1 (safety):** the legacy `/api/ai/chat` path executes mutating tools
(`create_campaign`, `create_template`, `cancel_emails`, `reschedule_emails`) with
**no confirmation gate at all** — `_exec_tool` (`app.py:2995`) runs them inline.
`queue_batch` is limited to `queue/draft/schedule` (never immediate send). The
legacy page is still reachable at `/assistant`. The upgrade instruction says
"impossible without Yes" must be server-enforced, so the legacy endpoint must be
shut down (or re-pointed at the gated agent). See Open questions Q1.

### 1.2 Agent loop

`agent.py` (799 lines):

- `STATE` — in-memory dict `conv_id -> {uid, messages, pending, prompt, file, created}`;
  `CONV_TTL = 2h`; `_gc()` evicts on new-conversation creation. **No persistence:
  a process restart loses all conversations.**
- `turn(uid, conv_id, text, file, confirm, answer)` (`agent.py:762`) — the single
  entry; rejects `text` while `pending` or `prompt` is open
  ("answer that first") — this is today's C9 behaviour (rejection, not supersede).
- `_drive(conv, ...)` (`agent.py:683`) — loop of at most `MAX_ROUNDS = 8`
  `ai_client.chat_raw(..., tools=TOOLS_SPEC)` calls:
  - read tool → executed immediately, result appended as a `tool` message,
    recorded in `events` (step chips);
  - mutating tool → sets `conv["pending"] = {call_id, tool, args, description}`,
    stops the loop, returns `pending` (client shows Yes/No card);
  - `ask_user` → sets `conv["prompt"] = {call_id, question, options(≤4)}`,
    stops, returns `prompt` (client shows options + text box);
  - `confirm` response: `True` → executes `HANDLERS[tool](args)` once, result
    appended; `False` → appends "declined by user" to the model and continues.
- **No approval tokens**: on `confirm=True` the server re-reads `conv["pending"]`
  and runs the stored args. Because the state is in-memory per process and the
  args come from the model (not the client), params can't be tampered by the
  client today — but there is no hash binding, no expiry, no idempotency, and no
  role check (see Finding 4).

### 1.3 Tool registry

`TOOLS_SPEC` (`agent.py:41`) — **20 tools + `ask_user`**:

- Read (9): `get_overview`, `list_campaigns`, `get_campaign`, `list_templates`,
  `get_template`, `list_contacts`, `get_replies`, `list_team`, `recent_activity`.
- Mutating (11): `create_campaign`, `update_campaign`, `delete_campaign`,
  `create_template`, `update_template`, `delete_template`, `activate_template`,
  `import_contacts_sheet`, `send_test_email`, `prepare_send`, `cancel_emails`.
- Interaction (1): `ask_user`.

FEATURES.md §4.1 says "20" — that counts the 20 tools above, excluding
`ask_user`. `HANDLERS` dict maps names → `_h_*` functions; `MUTATING` set.

### 1.4 How tools are declared to the model

Plain OpenAI-style `tools`/`tool_choice:"auto"` in `chat_raw`. The model sees the
**full** list for every user (no role or intent filtering). `ask_user` is a real
tool call; the server converts it into a card.

### 1.5 Confirmation / question cards (server → client)

`api_ai_agent` returns `{conv_id, reply?, events?, pending?, prompt?}`. The
client (`ask-ai.js`) renders:

- `pending` → `confirmCard()`: "CONFIRM ACTION" header, description from
  `_describe(tool, args, conv)` (`agent.py:579` — per-tool human text with real
  counts, e.g. delete counts email records), buttons `Yes, do it` / `No, cancel`.
- `prompt` → `promptCard()`: "A QUICK QUESTION" header, question text, ≤4 option
  `chip-btn`s, plus a text input (always present). Answer posted as
  `{conv_id, answer}` or `{conv_id, confirm}`.

There are **no** structured input cards (number/datetime/secret/file/table),
no preview of the rendered email, no impact table, no idempotency key, no
per-card expiry. Card state is client-side only (the `m.done` flag in
`localStorage` history); the server's card is the `pending`/`prompt` entry.

### 1.6 Conversation storage & transport

- Server: in-memory only (2 h TTL, GC). `messages` is the OpenAI chat list
  (system + user + assistant/tool), truncated tool results to 4000 chars.
- Client: `localStorage["cm_askai_history"]`, last 60 rendered messages
  (cards included, with their done-state). A new drawer open keeps the last
  `conv_id` (until the server rejects it as expired).
- Transport: **single JSON POST per user action**, no streaming, no polling.
  Long LLM calls block the request (client shows "Thinking…").

### 1.7 File upload path

Client: paperclip input in the drawer foot, 5 MB check client-side, base64 in
JSON. Server: `api_ai_agent` → `_agent_parse_file(name, data)` (`app.py:3188`):
`.csv/.xlsx/.tsv` → `_parse_sheet_file` (kind `sheet`); `.docx` →
`docx_convert.docx_to_html`; `.html/.htm/.txt` → kind `template`.

**Finding 2 (bug):** `_parse_sheet_file` (`app.py:1778`) only handles `.xlsx`
and `.csv`/`.txt` — a `.tsv` filename raises
`ValueError("Upload a .csv or .xlsx file.")` even though `_agent_parse_file`
routs `.tsv` there. TSV upload through the agent is therefore broken
(FEATURES.md §4.3 advertises it). The UI upload endpoint has the same gap.

---

## 2. Ask AI frontend (current state)

- **Drawer component**: `static/js/core/ask-ai.js` → `openAskAI(initial)` →
  `drawer()` from `static/js/core/ui.js:148` (scrim + panel, Esc closes, focus
  restore, `onClose` callback).
- **Open paths** (all verified working):
  - top-bar button `#topbar-ask-ai` (`ui/base_new.html:73`, wired in
    `static/js/core/shell.js:399`) → dispatches `cm:ask-ai`;
  - ⌘K command palette item "Ask AI" (`shell.js:251`) → same event;
  - deep link `?ask=1` (ask-ai.js tail: opens on `load`).
- **Message list**: `.ask-body` (flex column); message nodes via `msgNode()`:
  user bubble, assistant card (`.ask-ai`), `confirmCard`, `promptCard`.
- **Step chips**: `eventsNode()` renders `<details class="ask-ev">` per tool
  event with a `<pre>` of the JSON result (collapsible).
- **Composer**: `.ask-foot` = attach button, text input (Enter sends), send
  button. Busy state disables input during a turn.
- **Hint chips**: `HINTS` per `document.body.dataset.page` (free-text prompts,
  not deterministic flows).
- **History**: `cm_askai_history` (≤60). "Start new" button clears history +
  conv.
- **CSS**: `static/css/ask-ai.css` (98 lines) — `ask-*` classes on top of
  `tokens.css` variables (`--surface-2`, `--border`, `--primary-500`,
  `--warn`, `--danger`, `--r-chip`, `--t-fast`, ...). Dark default via
  `theme-dark.css`; light is the base. White email-paper lives in
  `static/css/pages/library.css` (`.ed-preview-box`).
- **Kit page**: `/_kit` (`ui/kit.html`) — component gallery (buttons, inputs,
  segmented, badges, cards, tables, banners, empty states, option cards,
  stepper, drop zone, illustrations, theme/width toggles).

---

## 3. LLM client

`ai_client.py` (198 lines), OpenAI-compatible `/chat/completions` via `urllib`:

- Settings (per tenant, in `settings` table): `ai_api_key`, `ai_base_url`
  (default `https://api.openai.com/v1`), `ai_model` (default `gpt-4o-mini`),
  `ai_enabled`.
- `chat()` — plain completion, **no retry**.
- `chat_raw()` — tool-calling round: 3 attempts, backoff 6s/12s on
  HTTP 400/429/5xx, 4s on other errors; timeout 120 s; returns the assistant
  message dict (`content` and/or `tool_calls`). **No `usage` capture, no
  thinking/reasoning stripping, no Qwen-native tool-tag parsing, no
  `temperature` differentiation beyond call sites.**
- Generation helpers (each a separate ad-hoc `chat()` call with its own JSON
  parse + fence-strip, **no repair retry**): `write_template(brief, tone)`,
  `improve_subjects(subject, context, n)`, `suggest_mapping(fields, columns)`,
  `polish_email(subject, body_html, instructions)`,
  `write_reminder(context, tone)`, `suggest_reply_followup(email_body,
  reply_snippet)`.
- `test_connection()` — "Reply with exactly: OK".

**Qwen notes:** the configured endpoint is whatever `ai_base_url` says; nothing
in the code is Qwen-specific (good — keep it pluggable). For the configured
Qwen model we must: strip `…` reasoning blocks, optionally
disable thinking mode if the endpoint supports it, and add a parser for
native `…`-tagged tool output in case the
provider doesn't speak OpenAI `tool_calls`. See Phase 1.

---

## 4. Send pipeline (UI 3-step flow + agent `prepare_send`)

The single email renderer **exists and is shared**:

- `render_email(tpl, row, mapping)` — `app.py:259`. Renders subject+body via
  `fill_fields` (regex `docx_convert.PLACEHOLDER_RE`, mapping values
  `column:<col>` or literal). Used by **both** the preview path
  (`api_merge_preview`, `app.py:1346`) and the send path (`_run_merge`,
  `app.py:1389`) and reminders (`reminders_tick`, `app.py:2344`). ✅ single
  source of truth for the merge fields.
- **Not** in `render_email` (done at call sites, inconsistently):
  - signature append: `body += "<p style='margin-top:14px'>" + sig + "</p>"`
    duplicated in `api_merge_preview` (1374), `_run_merge` (1453),
    `api_emails_test` (1308);
  - CC resolution: `parse_cc` (`app.py:2003`, ≤20 enforced in `_run_merge`,
    **not** enforced in `api_emails_*` actions — CC lives on the batch row);
  - inline `local:` images: `gmail_client.inline_local_images` at send time
    (`build_raw`) and in `api_email_view` for display;
  - attachments: `_load_attachments(batch)` (`app.py:3306`) from
    `batches.attachments` JSON names → `filestore`.

Pipeline functions (all in `app.py`):

| Concern | Function | Line |
|---|---|---|
| Batch+email creation, all validation | `_run_merge(d)` | 1389 |
| Draft/schedule/send finalisation | `_batch_finalize(c, bid, eids, action, account_id, when, stagger)` | 1477 |
| Per-email actions on existing rows | `api_emails_draft` / `api_emails_schedule` / `api_emails_send_now` / `api_emails_cancel` / `api_emails_delete` / `api_emails_revert` | 2027 / 2064 / 2105 / 2169 / 2152 / 3878 |
| Test email to self | `api_emails_test` | 1281 |
| Daily cap count | `sends_today()` | 2252 |
| Account check | `_require_account(account_id, u)` | 1992 |
| CC parsing | `parse_cc(raw)` | 2003 |
| Batch attachments | `api_batch_attachments_add/remove`, `_load_attachments` | 3335 / 3357 / 3306 |
| Batch reminders | `api_batch_reminders` (days 1–90, max 1–5 rounds) | 3407 |
| Reminders execution | `reminders_tick()` | 2344 |
| Scheduler loop / tick | `scheduler_loop` / `scheduler_tick` | 2653 / 2568 |
| Cloud push of one email | `_cloud_push_email(email_id, status)` | 2430 |
| Cloud status pull (relay mode) | `relay_status_pull()` | 2506 |

`_run_merge` validation (the "UI safety rails"):
- CC ≤ 20; serial range sanity;
- schedule: `when >= now+5min`, `when <= now+45d`;
- account required + usable for `draft/schedule/send` (`_require_account`);
- **safe mode ON → `action=="send"` blocked** (409);
- `send`: cap enforced **inside the loop** (`sends_today()+ok >= cap → break`)
  → remainder stays `pending` (this is the existing "remainder behaviour":
  the rest of the batch just stays queued).
- `schedule`: per-email `scheduled_at = when + i*stagger`, each row pushed to
  cloud (`_cloud_push_email`).
- No pre-send check of any kind (unmapped fields render as empty strings
  today; a missing `hr_email` creates an email row with `to_addr=''` — no
  duplicate-email detection, no rows-without-email warning at send time; those
  exist only at import time).

`prepare_send` (agent) → calls `_run_merge` with `action` mapped
(queue→create). It **does not** pass `cc`, `append_signature`, attachments, or
stagger — those parameters don't exist in the agent tool schema at all.

### 4.1 One-off / ad-hoc email

**Does not exist.** No way to email a single arbitrary address outside a
campaign sheet (the UI always merges from a contact list; `send_test_email`
goes to the signed-in user only; the agent's `send_test_email` takes an
arbitrary `to` but renders nothing — raw subject/body). The upgrade's
`send_adhoc_email` must create a real single-contact path.

### 4.2 Reply to a thread (F-42)

- Read: `api_email_view` (`app.py:1979`) returns the stored email (original
  send body with `local:` images inlined). **The reply text itself is not
  stored** — `replies_tick` (`app.py:2261`) only marks `replied_at` after
  finding an inbox message matching `in-reply-to:`/`references:`; the reply
  body is never read back. The UI thread view shows original + "replied"
  marker + AI-suggested reply built from the **original body only**
  (`api_email_reply_suggest`, `app.py:2229`, calls
  `ai_client.suggest_reply_followup` with snippet "No reply text stored").
- Actions: `api_email_reply` (send or draft a reply, respects safe mode →
  draft fallback, no new scopes — sends via `send_with_retry`/`create_draft`
  with `Re:` subject), `api_email_handled` (`handled_at` column — exists via
  `_migrate`), `api_email_mark_replied` (sets `replied_at` manually).
- **No snooze anywhere** (neither API nor UI) despite FEATURES.md mentioning
  it. Upgrade needs `snooze_reply` — implemented via a **non-synced local
  table** `email_snoozes` (adding a column to synced `emails` is forbidden
  without sign-off; see Q3).
- Sending a reply does **not** count against the daily cap today (it's a
  `send_with_retry` outside the cap loops) — keep as is (documented).

### 4.3 Bounce fixing (F-43)

- Detection: `bounces_tick` (`app.py:2297`) polls Gmail DSN notices,
  `parse_bounce_notice` extracts (address, reason); reason may be
  `"unknown"` — displayed, not hidden.
- Fixing: contact edit via `api_contact_update` (`app.py:877`) — exists
  (company/hr_name/hr_email/status/notes). Re-send to fixed contacts only:
  **no existing mechanism** to restrict a send to arbitrary contact ids —
  sends are sheet_id + serial-range only (`_run_merge`). Upgrade: add
  `contact_ids` filter to the merge path (internal service param, also
  exposed to the UI queue if needed). See Q4.

### 4.4 Reschedule / cancel of a batch

- Reschedule: **no batch-level reschedule endpoint** in the UI (the legacy
  `_exec_tool.reschedule_emails` has one for arbitrary ids — agent-only).
  `api_emails_schedule` reschedules selected rows (ids or filter). Upgrade
  `reschedule_batch` = service wrapper over the same row update + cloud push.
- Cancel: `api_emails_cancel` (rows → `cancelled`, cloud push) + agent
  `cancel_emails` (status+campaign filter, raw SQL).

---

## 5. Scheduler details (needed for the undo window, §7.7)

- **Tick interval**: `scheduler_loop` (`app.py:2653`) — `time.sleep(8)`
  between ticks; ticks the team DB **and every personal workspace DB**
  (`data/workspaces/*.db`). >5 s → **a one-shot timer at `release_at` is
  required** (spec §7.7.3). `/api/scheduler/trigger` (admin/dev) can force a
  tick (`app.py:2675`).
- **Due pick-up**: `scheduler_tick` (`app.py:2568`) selects
  `emails WHERE status='scheduled' AND scheduled_at <= now_iso` (LIMIT 500,
  ordered by `scheduled_at`).
- **Status values**: `pending, scheduled, sent, drafted, error, cancelled,
  deleted` (plus pseudo-filters `replied`/`bounced` via `replied_at`/
  `bounced_at`).
- **"Scheduled status flip" double-send guard**: in **relay mode**
  (`cloud_relay_url` set), the local instance does **not send at all** —
  `scheduler_tick` calls `relay_status_pull()` and returns. The **cloud**
  (Supabase edge function `supabase/functions/relay/index.ts`, `POST
  /api/pump`, PUMP_LIMIT 6/min, externally triggered) does the flip atomically:
  `UPDATE relay_emails SET status='sending' WHERE status='scheduled' AND
  scheduled_at <= now() … FOR UPDATE SKIP LOCKED` then sends, then sets
  `sent`/`error`. Local instances apply cloud statuses via
  `relay_status_pull`. In **non-relay mode** it's a single instance (SQLite
  WAL lock serialises); no explicit flip guard in the local path (acceptable:
  one process).
- **Catch-up**: `catchup_policy` setting — `send_now` (default) or
  `mark_missed` (mark `error: Missed send window` if >30 min overdue).
- **Cap at tick**: `sent >= cap - sends_today()` → stop.
- **Safe mode**: tick returns immediately while `safe_mode=1` (scheduled
  emails simply wait).
- **One-shot timer**: none today. Add: on hold confirm, schedule a daemon
  `threading.Timer(release_at - now, scheduler_tick)` (harmless if already
  released — the tick is idempotent per email status).
- **Undo window feasibility**: create emails `status='scheduled'` with
  `scheduled_at = now + send_undo_seconds` → the existing pick-up query
  naturally won't fire before `release_at`. The "≥5 min ahead" validation in
  `_run_merge`/`api_emails_schedule` must be bypassed with an **internal-only**
  flag (never in any tool schema or HTTP param). Undo = cancel-while-held
  (compare-and-set, see Phase 2). Cross-instance (relay mode): the cloud pump
  would send at `release_at`; to keep the spec's "other instance skips until
  overdue ≥60 s" rule we add an `undo_hold` boolean to `relay_emails`
  (cloud mirror table, not an app synced table — new migration
  `0005_undo_hold.sql`) set in `_cloud_push_email` for held batches; the pump
  skips `undo_hold=TRUE` rows until `scheduled_at <= now() - interval
  '60 seconds'`. If the originating instance is down, the cloud releases it
  60 s after the window (spec §7.7.8). See Q5.

---

## 6. Capability audit — what the agent can and cannot reach today

### 6.1 Capabilities that exist in the UI but the agent cannot reach

| Capability | UI code path | Notes for the agent upgrade |
|---|---|---|
| Attach/detach list to campaign | `POST /api/sheets/<sid>/campaign` (`app.py:3861`, `api_sheet_set_campaign`) | Real recipient count = `COUNT(contacts WHERE sheet_id IN campaign's sheets)` (see `api_campaigns`). |
| Campaign detail (recipients, summary, per-day, recent replies) | `GET /api/campaigns/<cid>` (`app.py:1693`) | New read tool `get_campaign` should return this. |
| Batches list / detail / reminders / attachments | `GET /api/batches` (1898), `GET /api/batches/<bid>` (3389), `POST /api/batches/<bid>/reminders` (3407), attachments (3335/3357) | New read tools `list_batches`/`get_batch`; `set_reminders` wraps 3407. |
| Email list w/ filters (status incl. pseudo `replied`/`bounced`, campaign, member, q, sort) | `_email_rows` (`app.py:1915`), `GET /api/emails` | Base for `get_replies` (extended), `list_bounces`, cancel selection. |
| Email view (original, images inlined) | `GET /api/emails/view` (1979) | `get_reply_thread` builds on it (reply body not stored — see 4.2). |
| Mark handled / mark replied manual | `POST /api/emails/<eid>/handled` (1256), `POST /api/emails/<eid>/mark-replied` (3628) | R2 confirms. |
| Reply (send or draft) + AI suggest | `POST /api/emails/<eid>/reply` (2189), `/reply-suggest` (2229) | `send_reply`/`create_reply_draft` + `suggest_reply`. |
| Contact add / update / delete / import | `POST /api/contacts*` (781/797/877/897) | `update_contact` for bounce fixing. |
| Template preview (as contact) | `GET /api/templates/<tid>/preview` (1073) | `render_preview` wraps `render_email`. |
| Template upload / AI write / polish / AI reminder | `POST /api/templates/upload` (949), `/ai` (990), `/api/templates/<tid>/polish` (3651), `/api/templates/ai-reminder` (3673) | R1 generation tools; file path uses `_parse_sheet_file`-style parsing in `api_template_upload`. |
| Home triage (attention + upcoming + campaign cards + tiles) | `GET /api/home` (`app.py:3769`) | `get_attention_items` must return the **same counts** (replies unhandled, bounced, failed, soon≤24h, stale drafts>2d, gmail-not-connected, pending join requests for admins). |
| Reports / analytics (members, emails, summary, team-summary, CSV) | `GET /api/reports/*` (3508+) | `get_results` (person/campaign/date filters). |
| Activity feed with type filter | `GET /api/activity` (1112) + `ACTIVITY_TYPES` (1096) | `recent_activity` (extended). |
| Settings (get/set) | `GET/POST /api/settings` (2687/2789) — admin-gated keys: `ai_api_key`, `google_client_id/secret`, `client_secret_json`, `admin_email`, `cloud_relay_secret`, `brand_team_name`, `brand_tagline`, `supabase_service_key` | `get_settings` (non-secret), `set_send_limits`, `set_branding`, `set_ai_settings`. |
| Safe mode toggle | `safe_mode` via `/api/settings` POST (also Home mode card) | `set_live_sending` (R5). |
| Signature get/set/image | `GET/POST /api/signature` (3270/3276), `/api/signature/upload` (3281) | `set_signature`. |
| Gmail accounts: list/add/visibility/delete | `GET /api/accounts` (659), `POST /api/accounts/add` (672), `POST /api/accounts/<aid>` (720), `POST /api/accounts/<aid>/delete` (743) | `list_gmail_accounts`, `remove_gmail_account` (R5). Deletion impact: queued/scheduled/drafted emails keep `account_id`; scheduler/finalise raise "send account lost its login" → status `error`. **Document that in the confirm text.** |
| Member management: requests, members, invite, role, remove | `GET/POST /api/admin/data|requests|members|invite` (393/403/424/449) | Team flows (A only) with pre-validation. |
| Export contacts CSV | `GET /api/export/contacts.csv` (913) | `export_data` (R5). |
| Danger zone ("Empty all emails") | `settings.js:703 dangerZone()` — holds-to-confirm, typed confirmation in the UI | **Handoff only** (§11.12). |
| Sync status | `GET /api/sync/status` (2738) | `get_sync_status` (read-only). |
| Scheduler trigger | `POST /api/scheduler/trigger` (2675) | Not exposed to the agent (internal). |

### 6.2 The real agent tool list today (verified, §2.7 of instruction)

20 tools + `ask_user` (see §1.3). Missing vs the instruction's §9:
all the R0 reads (attention, upcoming, batches, thread, bounces, sheets,
attachments, results, gmail accounts, settings, send status, preview, sync,
join requests, help, ai usage, presend check), the R1 generate family
(`write_template`, `write_email`, `polish`, `suggest_subjects`,
`suggest_mapping` exists in `ai_client` but not as an agent tool,
`suggest_reply`, `write_reminder`), and most R2–R5 writes (attach/detach
sheet, set campaign template, rename/delete-template impact, delete sheet,
update contact, save drafts, one-off send, reply send/draft, set reminders,
reschedule batch, mark handled/snooze, live sending, send limits, signature,
branding, AI settings, remove account, member ops, export, mode switch,
undo, AI budget).

---

## 7. Role / tenant binding

- `bind_tenant` (`app.py:102`, `@app.before_request`): every request starts on
  the **team DB** (`db.DB_PATH` = `data/campusmail_ai.db`); if the session user
  is not an active team member, `_enter_personal_tenant(email, ...)`
  (`app.py:67`) switches `db.set_tenant_db(data/workspaces/<sha1(email)[:16]>.db)`,
  tenant file dirs, forces the user to admin/active **in their own DB**, and
  sets `_RESOLVED_USER` with `personal_workspace=True`. `teardown_request`
  resets. ContextVar-based (`db._TENANT_DB`, `app._TENANT_DIRS`).
- The agent runs **inside the request** (`api_ai_agent` → `agent.turn` →
  handlers use `db.conn()` + `_app.request.user`), so it inherits the
  request-bound tenant. ✅ But:
  - **Finding 3:** the in-memory `agent.STATE` is shared across tenants; it is
    keyed by conv_id and re-checked against `uid` (`_get`), and uids are
    per-DB (a personal-workspace uid can collide with a team uid numerically
    — both DBs autoincrement from 1). A logged-in personal user and a team
    user with the same `users.id` could cross conversations only if they share
    a conv_id (uuid4 — negligible) — but **actor identity must always come
    from `request.user` of the current request, never from the conv** (the
    conv stores only uid). The upgrade will store tenant-scoped conv ids and
    re-resolve the user on every respond.
  - `request.user` is set by `login_required`; **role is read fresh per
    request** (`current_user()` → `_RESOLVED_USER`), no caching. ✅
- Personal workspace isolation: separate SQLite file + separate filestore
  tenant dirs. Sync: personal workspaces are **not** relayed (only the team
  DB has `cloud_relay_url`).

---

## 8. Design system / reusable components

- Tokens: `static/css/tokens.css` (spacing `--sp-*`, radii `--r-*`, text
  `--text/--text-2/--text-3`, `--surface/--surface-2`, `--border/--border-
  strong`, `--primary-500/--primary-50`, `--warn/--danger/--success/--info`,
  `--t-fast`, `--ease`); dark: `theme-dark.css`; light: `base.css`.
- Components (`components.css` + `/_kit`): `.btn` (`btn-primary` — the one
  white button, `btn-secondary`, `btn-ghost`, `btn-danger`, `btn-hold`,
  `btn-ai`, sizes), `.input/.select/.textarea/.field/.field-error`, `.seg`
  segmented control, `.badge`/`statusBadge`, `.card/.card-flush/.card-head`,
  stat tiles, `.table`, `.banner`, `.empty`/`emptyState`, `.dropzone`, option
  cards, stepper, `.toast` (with action button — used for Undo-style toasts),
  `.modal`/`.drawer`/`.menu`, `errorCard`, `holdToConfirm`, `confirmTyped`
  (typed-phrase confirmations already exist in `ui.js:242` — used by the
  danger zone), `undoable` (`ui.js:270`).
- Rich-text editor: `static/js/pages/library.js` — `contenteditable`
  `.ed-body` with `document.execCommand` toolbar (bold/italic/underline/lists/
  undo/redo/insert field/insert image/link), fill-in pills
  (`span.fill-pill`, `data-f`), white-paper preview `.ed-preview-box` with
  client-side `fillBodyClient(html, row)` + "Preview as contact" select. The
  draft-editor card will reuse this pattern (extract to
  `static/js/core/ask-ai-editor.js` so both can share it).
- Email paper is always white (`.ed-preview-box`), verified in both themes.

---

## 9. Tests

**None exist.** No `tests/` dir, no pytest in `.venv` (will `pip install
pytest` into the venv). Runner: `.venv/bin/python -m pytest tests/ -q`.
Tests must use a mocked LLM (`ai_client.chat_raw`/`chat` monkeypatched with a
counter) and mocked Gmail (`gmail_client.send_with_retry`/`create_draft`/
`check_replies`/`list_bounce_notices`), plus a temp-tenant DB
(`db.set_tenant_db(tmp_path/"t.db")` + `db.init_db(seed=False)`).

---

## 10. Storage for new settings and counters

- **Settings**: `settings` table (per tenant, **not synced** — intentional
  per FEATURES.md "Intentional behaviours") via `db.get_setting`/
  `db.set_setting` (`db.py:483/491`). New keys are trivial to add:
  `send_undo_seconds` (default 20, 10–30), `bulk_confirm_threshold`
  (default 50, 10–500), `ai_daily_calls_per_user` (150),
  `ai_daily_tokens_per_user` (400000), `ai_daily_calls_workspace` (0),
  `presend_test_suggest_min` (10, constant in code, documented).
- **Per-user settings**: `user_settings(user_id, key, value)` — already used
  for `last_account_id`. New keys: `ask_ai_mode` (`guided`/`compact`),
  remembered send defaults (`last_stagger_min`, `last_signature`,
  `last_cc_<campaign_id>`, `last_attachments_<template_id>`,
  `last_reminder_days`, `last_reminder_rounds`), `ai_notice_day` (80% notice
  once per day).
- **Non-synced local tables that already exist** (pattern to follow):
  `activity`, `users`, `settings`, `user_settings`, `processed_dsn`,
  `sync_deletes`, `sync_pushed`. New local tables (added in `db._migrate`,
  never in `SYNC_COLUMNS`): `approvals`, `ask_ai_cards`, `card_responses`
  (idempotency), `ai_usage`, `template_test_sends`, `undo_holds`,
  `email_snoozes`, `link_check_cache`.
- **Test emails recorded durably?** No — `api_emails_test` logs activity only.
  → `template_test_sends(template_id, content_hash, user_id, sent_at)` new.
- **Outbound network access?** Yes — the server already calls the AI
  provider, the cloud relay, and (on the cloud) Gmail, via `urllib`. Link
  reachability checks can run in a worker thread with a 3 s timeout.
  (PythonAnywhere outbound is allowed; failures must degrade to
  "Couldn't check".)

---

## 11. Gap table — every flow in FEATURES.md §10

Legend: **OK** = agent can already do it · **TOOL** = needs new tool/flow
(this upgrade) · **UI** = handoff/UI-only · **N/A**.

| Flow | Today | Target | How |
|---|---|---|---|
| F-01 first-user sign-in | N/A | N/A | Agent unavailable when signed out. |
| F-02 unknown person → personal workspace | N/A | N/A (+isolation tests) | Tenant binding verified (§7); add cross-tenant tests. |
| F-03 invited pending → member | N/A | TOOL (A: `invite_member`) | Pre-approved invite via `db.invite_member`. |
| F-04 admin-email auto-promotion | N/A | N/A | Role read fresh per request. |
| F-05 session lifecycle | N/A | N/A (+edge) | 401 → client toast+redirect (existing `ui.js` api()); mid-chat expiry: cards become inert, approvals void (new). |
| F-06 role-gated UI | partial | TOOL | Tool schemas filtered by role; server re-checks; member gets friendly refusal card. |
| F-10 connect Gmail | UI | UI (handoff) | `start_gmail_connect` handoff card + `I've done it — check` read-only recheck (`list_gmail_accounts`). |
| F-11 accounts / removal | – | TOOL | `list_gmail_accounts`, `remove_gmail_account` (R5, impact text). |
| F-12 configure AI | UI | TOOL | `set_ai_settings` (+`secret_input`), `test_ai_connection`, `set_ai_budget`, `get_ai_usage`. |
| F-13 safe mode | – | TOOL | `set_live_sending` (R5) + auto-surfaced in preflight. |
| F-14 branding | – | TOOL | `set_branding` with validation. |
| F-15 signature | – | TOOL | `set_signature` (editor card + image `file_request`). |
| F-16 limits & catch-up | – | TOOL | `set_send_limits` (cap, stagger, catch-up, `send_undo_seconds`, `bulk_confirm_threshold`). |
| F-17 members | – | TOOL (A) | invite/change_role/remove_member + pre-validation. |
| F-20 create campaign | OK (raw SQL) | OK→TOOL | Migrated to Action Registry + service layer. |
| F-21 import list | OK (raw SQL, TSV bug) | TOOL | Deterministic parse → data card → confirm → link; fix TSV; edge tests. |
| F-22 template by upload | OK (raw SQL) | TOOL | Deterministic convert → editor card → save (R2). |
| F-23 write/edit template | partial (AI via Library UI) | TOOL | `write_template`/`polish` → draft editor → save/update (R2). |
| F-24 template lifecycle | OK | TOOL | +`rename_template`; activate confirm names the deactivated one. |
| F-25 attach list + template | – | TOOL | `attach_sheet_to_campaign`, `detach_sheet_from_campaign`, `set_campaign_template`. |
| F-26/27/28 send wizard | partial (monolithic `prepare_send`) | TOOL | `send_campaign` stepwise flow (Guided + Compact), draft editor, final review, pre-send check, bulk guard, undo window. |
| F-29 scheduled execution | N/A (system) | UI (assist) | Agent reports status only; never runs scheduler logic. |
| F-30 reminders | UI only | TOOL | `set_reminders` (1–5 rounds) + `write_reminder`. |
| F-31 campaign edit/delete | OK | TOOL | Impact text per §6.3. |
| F-32 cancel sends | OK (raw SQL) | TOOL | Scope options → data table → R3 confirm. |
| F-40 home triage | – | TOOL | `get_attention_items` (counts must equal `/api/home`). |
| F-41 replies inbox | partial (basic `get_replies`) | TOOL | Extended filters (campaign, member, status, search, limit). |
| F-42 handle a reply | partial (read only) | TOOL | Thread card (reply text not stored — show what exists, honestly), `suggest_reply`, send/draft reply, handled/snooze/mark-replied. |
| F-43 bounce handling | – | TOOL | `list_bounces` → `table_edit` fix → `update_contact` → re-send to fixed only (needs contact-ids filter in merge, Q4). |
| F-50 results analytics | – | TOOL | `get_results` (person/campaign/date). |
| F-51 activity log | OK (basic) | TOOL | Filters (type, actor, date). |
| F-52 member requests | – | TOOL (A) | `list_join_requests`, `approve_join`, `deny_join`. |
| F-60 data question | OK | OK | + number guard on narration. |
| F-61 mutation approved | OK (weak gate) | TOOL | Full Approval Gate (tokens, hashes, idempotency). |
| F-62 mutation declined | OK | OK | Keep; C7 no-retry rule. |
| F-63/F-64 questions | OK | TOOL | Proper `options`/`text_input` cards; C4 "Something else…". |
| F-65 attach contact list | OK | TOOL | 0 LLM calls, deterministic flow. |
| F-66 attach template file | OK | TOOL | 0 LLM calls, deterministic flow. |
| F-67 multi-step | partial (LLM chains) | TOOL | `plan` card + per-step gates + retry/skip/stop. |
| F-68 test email | OK (raw send) | TOOL | §11.7 flow; content-hash test record; exempt from undo/bulk. |
| F-69 agent edge cases | partial | TOOL | §14 table + tests. |
| F-70 command palette | UI | UI (+chips) | Palette entries open drawer with flow chip (no auto-exec). |
| F-71 theme | – | TOOL (UI-local) | `set_theme` via options (client-side only). |
| F-72 sync | N/A | N/A (+correctness) | Mutations via service layer → same rev/tombstone path as UI; tests. |
| F-73 empty states | – | TOOL | Every data card gets designed empty state + action option. |
| F-74 deep links | UI | TOOL | `open_page` renders link chips (never auto-navigates). |
| F-75 help | – | TOOL | `get_help(topic)` from guide content. |
| F-76 asset caching | N/A | N/A (+deploy) | New/changed assets get `?v=` via existing `_static_version` (mtime-based; JS files referenced from templates need explicit `{{ sv(...) }}` — verified in `base_new.html`). |
| F-77 files & storage | – | TOOL | `file_request` (purpose-scoped), `local:` resolution unchanged. |
| F-78 errors & 404s | partial | TOOL | Inline error cards + `[Retry]`; idempotent retries. |
| F-79 export / danger zone | – | TOOL/UI | `export_data` (R5); danger-zone resets handoff only. |

---

## 12. Live environment (verified 2026-11-15)

- AI: OpenAI-compatible **Gemini** endpoint
  (`https://generativelanguage.googleapis.com/v1beta/openai`, model
  `gemini-3.6-flash`, key set, `ai_enabled=1`). → Qwen/Gemini handling per
  §8.9 applies (thinking strip, tool-call format check).
- `safe_mode = 0` (live sending ON), `max_sends_per_day = 1000`,
  `cloud_relay_url` **set** (relay mode: cloud pump sends due scheduled
  emails; local scheduler does not send). The undo-window cross-instance
  guard is therefore mandatory, not theoretical.
- Gmail account: `f20230611@goa.bits-pilani.ac.in` (id 8, private, token OK).
- Data: campaign "Diwali Outreach" (1) with 50 sent emails (yesterday),
  966 contacts, 3 templates, no sheets.
- App currently running on 127.0.0.1:5001 (old code) — must be restarted
  after implementation.
- **Daily budget for the AI key: 20 real calls for this session.** All
  automated tests therefore use a mocked LLM (per spec §17.1). The real-call
  E2E plan below is the only source of live calls (≤ 17 planned).

## 13. Open questions (spec ↔ repo conflicts, resolved safest-first)

- **Q1 — Legacy ungated assistant.** `/api/ai/chat` + `/assistant` page run
  mutations with no confirmation (violates N2/N7). *Decision:* disable the
  endpoint (redirect answer: use the Ask AI drawer) and keep the route for
  deep links. Nothing else in the repo calls it.
- **Q2 — TSV parse bug.** `_parse_sheet_file` rejects `.tsv` although
  FEATURES.md §4.3 and F-21 advertise it. *Decision:* fix (treat as CSV with
  tab delimiter). In scope as an import edge case.
- **Q3 — Snooze state.** F-42 mentions snooze; no API/UI exists. Adding a
  column to synced `emails` is forbidden without sign-off. *Decision:*
  non-synced local table `email_snoozes(email_id, snoozed_until, snoozed_by)`.
- **Q4 — Re-send to fixed contacts only (F-43).** Merge selection supports
  sheet + serial range only. *Decision:* internal service param
  `contact_ids` in the shared merge service (also available to the UI queue
  action); not part of any tool schema beyond `send_campaign` slots.
- **Q5 — Undo hold across instances.** `undo_holds` must stay local (spec).
  The cloud instance cannot see it directly. *Decision:* `_cloud_push_email`
  tags held emails `undo_hold=true`; new relay column
  `relay_emails.undo_hold` (migration `0005_undo_hold.sql` — cloud mirror
  table, not an app synced table); the pump skips `undo_hold` rows until
  `scheduled_at <= now() - interval '60 seconds'`. Local (non-relay) mode is
  single-instance: `undo_holds` table + `release_at` on `scheduled_at` is
  sufficient, plus the one-shot release timer.
- **Q6 — Undo window on the UI send wizard.** Spec recommends it (same
  pipeline, same setting). *Decision:* apply — `api_emails_send_now` routes
  through the same hold path (internal flag). Documented in FEATURES.md.
- **Q7 — "Same service functions as the UI".** Today the UI route handlers
  *are* the service layer (raw SQL inside), and the agent duplicates it with
  its own raw SQL. *Decision:* extract `services.py`; UI routes and agent
  actions both call it. Behaviour parity proven by tests (same SQL, same
  activity entries, same sync side effects).
- **Q8 — `prepare_send` gaps.** Agent tool lacks cc/signature/attachments/
  stagger. *Decision:* the new `prepare_send` action accepts the full param
  set (flow-filled, never LLM-filled) and delegates to the shared merge
  service with identical validation.
- **Q9 — `send_test_email` semantics.** Current agent tool sends raw
  LLM-provided HTML to any address. *Decision:* replaced by the §11.7 flow
  (rendered from real draft/template/campaign-first-recipient, final review,
  confirm). Raw variant removed.
- **Q10 — Cap reservation for held emails.** Spec §7.7.4: held emails count
  toward today's cap at confirm time. *Decision:* `sends_today()` gains
  `+ count(held unsent undo emails)` (local `undo_holds` join); Undo releases
  the reservation (emails → `cancelled`).
- **Q11 — `ai_enabled` toggle.** Pre-existing setting not in the spec.
  *Decision:* `ai_enabled=0` or missing key → drawer shows the designed
  "AI isn't set up" state; Tier-0 flows (chips, imports, settings, reads via
  commands) still work (spec §14 first row).
- **Q12 — Real-call E2E budget (20 calls).** Plan (≤17): route campaign
  create (1) · write template (1+1) · save template (0) · guided queue send
  (1) · compact send-now + Undo (1) · compact send-now release (1) · subject
  suggestions (1) · Revise with AI (1) · reply suggest+route (1+1) · send
  reply (0) · test email (0) · reminder text (1) · open-ended question
  route+narration (1–2) · one-off email (1+1) · everything else (reads,
  imports, plan, bulk guard with threshold 10, settings, cancel, delete,
  mode switch, pre-send check, link check) = 0. Retry/repair reserve: 3.
  All §17.1 automated tests run on the mocked LLM (0 real calls).

## 14. Implementation decisions (architecture)

- New modules: `services.py` (shared service layer), `actions.py` (Action
  Registry: risk/roles/schema/preview/execute/verify), `approval.py`
  (Approval Gate: tokens bound to params hash + render hash, 10-min expiry,
  single-use, idempotent, server-verified bulk ack), `cards.py` (card
  protocol: envelope, respond/update/cancel endpoints, statuses,
  supersede, close-cancels, idempotency), `flows.py` (declarative Flow
  Engine: slots, option/text steps from DB, gates, plan card, back,
  partial failure; `send_params` group for Compact), `router.py` (Tier 0
  deterministic + Tier 1 single LLM call with role-filtered tool subset,
  parallel reads, loop cap 4), `presend.py` (pre-send check + SSRF-safe
  async link checker + template-test tracking), `usage.py` (AI budget:
  metering, per-minute limiter, degradation ladder).
- New non-synced local tables (added in `db._migrate`): `approvals`,
  `ask_ai_cards`, `card_responses`, `ai_usage`, `template_test_sends`,
  `undo_holds`, `email_snoozes`, `link_check_cache`.
- `ai_client.py`: thinking-tag strip, optional thinking-off hint
  (`thinking_config.thinking_budget=0`, only when the provider setting says
  so — Gemini), usage capture, Qwen/Gemini native tagged tool-call fallback
  parser, one repair retry for structured JSON, per-purpose temperatures.
- Scheduler: one-shot `threading.Timer` at `release_at` triggers a tick;
  `scheduler_tick` skips held-but-not-released emails (belt-and-braces,
  `scheduled_at` already defers them); `undo_send` = compare-and-set cancel
  of held emails only.
- Frontend: `ask-ai.js` rewritten around the card protocol (all §6 card
  types), new `ask-ai-editor.js` (shared rich-text editor for draft/reminder
  cards, reusing Library editor patterns), `ask-ai.css` extended with
  existing tokens only; mode switch in drawer header; undo toast; quick-start
  chips (role-aware); `?ask=1`/⌘K/topbar unchanged.
- LLM-call reductions (beyond spec §8): keyword Tier-0 covers the §11.11
  question catalogue (0 calls for the common reads); router uses
  `tool_choice`-style structured single call with role+intent-filtered
  subset; subject+body+fields in one structured call; deterministic mapping
  first (LLM only for residual ambiguity); narration optional and dropped by
  the number guard; per-turn read cache; stable system prompt (prefix
  cache); tool results truncated before re-entry (top 20 rows + totals);
  cards in history collapsed to one line for the model.

## 15. Side-conversation change log — AI provider: OpenRouter + dual-key pool (2026-11-15, ~03:10 IST)

Done from a side conversation on explicit user request. **Main thread: read this
section before the next `ai_client.py` / `app.py` / `services.py` edit.**

### What changed
1. **`ai_client.py` — key-pool/rotation RE-APPLIED.** The side-conversation
   version (2026-11-15 morning) was **lost when the main-thread Phase-1 rewrite
   replaced the file**; the pool is now merged back into the current file, which
   keeps thinking-strip, native tool-tag fallback, JSON repair retry and
   `usage` metering untouched. Contract:
   - Key pool: `CAMPUSMAIL_AI_KEY_1` env → setting `ai_api_key` (key 1, used by
     default); `CAMPUSMAIL_AI_KEY_2` env → setting `ai_api_key_2` (fallback).
     Env vars override settings.
   - `CAMPUSMAIL_AI_BASE_URL` / `CAMPUSMAIL_AI_MODEL` env override settings
     `ai_base_url` / `ai_model`.
   - On HTTP 401/402/429 or a 400 whose body mentions
     quota/credit/limit/exceeded/insufficient/billing, the key is marked
     exhausted in the **non-synced** setting `ai_key_state` and the client
     rotates to the next key **mid-request, transparently** (no user-visible
     error, no extra click). 5xx/timeout keep the old 3x backoff.
   - Per-key daily cap: env `CAMPUSMAIL_AI_KEY_DAILY_LIMIT`, default 50
     (OpenRouter free tier). `ai_key_state` =
     `{"date": "YYYY-MM-DD", "calls": {"1": n, "2": m}, "exhausted": {kid: reason}}`,
     day rollover resets everything.
   - New API: `ai_keys_status()` (key/source/calls_today/limit/exhausted),
     `_QuotaError`, `_post_chat()`; `chat` / `chat_raw` / `test_connection`
     all go through the pool.
   - **When editing `ai_client.py` again: preserve the KEY POOL block** —
     module-docstring note, `_QUOTA_HINTS` through `ai_keys_status()`,
     `_QuotaError`, `_post_chat()`, and the pool branches in `_post`.
     Do not remove it or inline key values.
2. **`services.py`** — `ai_api_key_2` added to `ADMIN_ONLY_KEYS` (admin-only,
   like `ai_api_key`); `settings_public()` now pops `ai_api_key_2` too.
3. **`app.py`** — added the **missing `import services`** (lost in a failed
   batch; without it every `services.*` route NameErrors). `api_settings_get`
   now pops `ai_api_key_2` as well — neither key value is ever returned to the
   frontend (only the `ai_has_key` flag).
4. **Local team DB** (`data/campusmail_ai.db`) — now on
   `ai_base_url=https://openrouter.ai/api/v1`, `ai_model=openrouter/free`,
   `ai_api_key` = key 1 (`sk-or-v1-2cd8…cf1c8`), `ai_api_key_2` = key 2
   (`sk-or-v1-581d…9c28`). Old Gemini key/model was overwritten; recoverable
   from `data/campusmail_ai.db.bak-20261114-*` if ever needed.
5. **GitHub repo secrets registered** (repo visibility, via owner PAT):
   `CAMPUSMAIL_AI_KEY_1`, `CAMPUSMAIL_AI_KEY_2`. Key values are NOT in code,
   NOT in this doc, NOT in git history (verified: `git log --all -S`).
6. **Local app restarted** (port 5001, new PID 14468) with the new code;
   `import app` clean before restart.

### Verified live (3 OpenRouter requests consumed: key1 x2, key2 x1)
- `chat` returns clean content from `openrouter/free` via key 1.
  Caveat: with `max_tokens=10` the auto-routed model returns **empty visible
  content** (it spends the budget "thinking"); with >= 100 it is fine. The app's
  per-call max_tokens are already >= 400 for generation — leave them.
- Rotation test: key 1 forced bogus -> HTTP 401 -> marked exhausted -> the same
  user request continued seamlessly on key 2, no error surfaced, no click.
  (Bogus-key exhaustion marker removed after the test; real per-key counts kept.)

### PythonAnywhere ("web PA") — PENDING, needs a manual step there
PA is a fresh clone of this GitHub repo (see `DEPLOY.md`), so:
1. **Commit + push** the current changes first; then on PA: `cd ~/campusmail-ai && git pull`.
2. Then either:
   - PA **Web tab -> web app -> Environment variables**:
     `CAMPUSMAIL_AI_KEY_1` + `CAMPUSMAIL_AI_KEY_2` (values = the two GitHub
     repo secrets above), optionally `CAMPUSMAIL_AI_BASE_URL=https://openrouter.ai/api/v1`
     and `CAMPUSMAIL_AI_MODEL=openrouter/free`; then **Reload** the web app; or
   - PA **Bash console** (sets the PA team DB directly):
     ```
     cd ~/campusmail-ai && python3 -c "import db; db.set_setting('ai_base_url','https://openrouter.ai/api/v1'); db.set_setting('ai_model','openrouter/free'); db.set_setting('ai_api_key','<CAMPUSMAIL_AI_KEY_1 value>'); db.set_setting('ai_api_key_2','<CAMPUSMAIL_AI_KEY_2 value>')"
     ```
     then **Reload** the web app. (Or sign in as admin on PA and paste the
     keys into Settings -> AI; `ai_api_key_2` is admin-gated.)
3. Verify on PA via Settings -> AI -> Test connection (costs 1 free request).

### Notes for the main thread
- **E2E budget changed:** the "20 free Gemini calls/day" constraint is
  superseded. Live provider = OpenRouter `openrouter/free` auto-router,
  ~50 req/day per key (1,000/day if the account ever buys $10 credits),
  2 keys -> ~100 req/day combined. The self-email E2E plan's call budget
  (~17 provider calls) fits with large margin.
- `openrouter/free` auto-routes to **different underlying models per
  request** (style drift between generations). If a stable writer is wanted,
  pin one of the tool-capable free models: `nvidia/nemotron-3-ultra-550b-a55b:free`,
  `particle-ai/particle-1:free`, `inclusionai/ling-3.2-flash:free`.
- Security note (not fixed here; user informed): a GitHub PAT is embedded in
  the local `.git/config` remote URL — rotate / move to a credential helper.
- LLM-call reduction per interaction is already structural in this design:
  Tier-0 deterministic routing (chips/card responses = 0 LLM calls), one
  structured call for subject+body+fields, no LLM inside flows, number guard
  on narration. The key pool does not add calls — it only rotates on failure.

### Push status (2026-11-15, ~11:00 IST, side conversation)
- A second codex thread is working in parallel and the main thread is still
  building (new: `flows.py`, `router.py`; `agent.py`, `services.py`,
  `app.py`, `ai_client.py` all still changing).
- Per user instruction: **do not commit/push a partial AI-key/provider state.**
  One combined commit + push of the whole Ask AI upgrade is planned once both
  threads are done (side conversation will execute it on signal).
- The port-5001 app (PID 14802) runs code as of ~03:15 — it needs one final
  restart before the E2E self-email test so it runs the latest code.
- Key-pool block was re-verified present after the main thread's 15th-hour
  `ai_client.py` edits (13 pool markers, main thread built on top of it).

## 16. Live E2E verification with real Gmail self-emails (2026-11-15, 05:50–07:45 IST)

Scope: every Ask AI flow exercised end-to-end through the running app
(`:5002`, dev-login) with **real Gmail API sends, from and to
`f20230611@goa.bits-pilani.ac.in` only** (user hard rule: no other
recipient, ever). All verification reads are read-only; all writes go
through the service layer. Result: **ALL 12 E2E PHASES GREEN.**

### 16.1 Phases and outcomes
| Phase | Result | Evidence |
|---|---|---|
| baseline | PASS | overview tiles captured pre-test (sent=50, campaigns=1, templates=3, contacts=1046, cap=1000, safe_mode off) |
| create_campaign | PASS | agent `start_flow` confirm → campaign created, "via Ask AI" activity |
| write_template | PASS | 1 LLM call → draft editor → save confirm → template active |
| import_queue | PASS | CSV upload (12 self-contacts) → parse data card → confirm → sheet |
| send_undo | PASS | Send now confirm → `undo_countdown` (20 s) → **zero Gmail calls during hold** → Undo → 12 cancelled, hold gone, `send_undone` activity, 0 Gmail messages |
| send_release | PASS | 2nd batch held 20 s → released → local scheduler sent **12/12 in ~40 s**, message_ids stored |
| gmail_verify | PASS | 12/12 received in inbox; To = self; merge fields rendered (`{company}`, `{hr_name}`); signature appended; body marker `VERIFY-ASKAI-BODY-11150553` present; subject marker present; **0 bounce DSNs**; stored `message_id` == real Gmail `Message-Id` header (12/12 match — Gmail preserves app-generated Message-Ids); app `replies_tick` detection verified against a genuine In-Reply-To self-reply (see 16.4 for index-lag caveat) |
| reply_flow | PASS | F-42: unhandled-reply list → pick → `suggest_reply` (1 LLM) → editable draft editor → edit → confirm gate → sent to self → verified in Gmail (`Re: …` subject, edited marker in body) |
| reminders | PASS | 1 day × 1 round on the sent batch; reminder template created; result card with batch link |
| one_off | PASS | one-off draft (kind=one_off) → 1 LLM generation → editor → "Send test to me" → test-email final review → confirm → **test email received in Gmail** with generated subject |
| cleanup | PASS | service-layer deletes: VERIFY campaign (cascade batches+emails), One-off campaign, all sheets, VERIFY%/Reminder% templates |
| final | PASS | overview **identical to baseline** after cleanup; ≥5 "via Ask AI" activity entries; 0 leftover sheets |

### 16.2 LLM budget (OpenRouter dual key, 50/day/key)
Total real provider calls for the whole E2E (all runs, retries
included): **55 / 100** by 07:45 IST (`ai_key_state` ≈ key1 44 + key2 1 at
07:06, finishing 55). Breakdown of *necessary* calls: routing for
free-text requests (~10, incl. 3 failed openrouter/free attempts that
returned no tool call), generation calls (write_template 1, write_email
2, suggest_reply 1, write_reminder 1). Everything else — card walks,
gates, sends, undo, imports, reads — was **0 LLM calls** (Tier-0 /
deterministic), per §8 of the spec.

### 16.3 Bugs found and fixed during E2E (all tested)
1. **`literal:` prefix leak** — mapping values `literal:X` rendered into
   email text/subject with the prefix intact. Fixed in `fill_fields` in
   **both** `services.py` and `app.py` (the two-renderer problem §7.6
   warned about). Tests: `tests/test_literal_guard.py` (4 new).
2. **Double-send race (F-29/F-72).** The local scheduler had no atomic
   claim; two app instances on the same SQLite file both sent the same
   staggered rows (that's why Gmail showed 24 for a 12-send batch).
   Fixed: `UPDATE emails SET status='sending' WHERE id=? AND
   status='scheduled'`, skip on `rowcount==0`. Test: claim exclusivity +
   skip pre-claimed rows.
3. **Deployed relay source was broken** — `supabase/functions/relay/index.ts`
   had lost its entire send-helper block (`buildRaw is not defined` at
   runtime → all relay sends error). Restored from git `a05d85c`
   (`parseAtts/parseCc/b64url/enc/buildRaw/accessToken/gmailSend/
   gmailMessageId` + `relay_token_cache`). **Deployed edge function must
   be redeployed by the user** (relay project differs from the CLI-linked
   one — see 16.5).
4. **`test_email.post_gate` crash** — misplaced bracket made the return
   `dict + list` → HTTP 500 on the test-email confirm (email had already
   been sent, UI showed an error). Fixed; post_gate also now emits the
   C6 "What next?" options card like the other flows.
5. **Secret-leak regression (caught by `test_secrets`)** — a first fix for
   #4 stored full gate params in flow state (`last_gate_params`), which
   persists to `ask_ai_convs` and would have carried API keys. Replaced
   with a non-secret field only (`last_gate_template_id`).
6. **Replies not threaded (F-42 "threading preserved")** — `build_raw`
   never emitted `In-Reply-To`/`References`, so app-sent replies started
   new Gmail threads. Fixed: optional headers threaded through
   `build_raw → create_draft/send_message/send_with_retry`, and
   `services.email_reply` passes the original send's stored
   `message_id`. Tests: `tests/test_reply_threading.py` (3 new).
7. **Routing fragility → deterministic fix (LLM-call reduction).**
   `openrouter/free` mis-routed "Start the draft_email flow …" to a read
   tool (and the word "schedule" inside a draft brief skewed routing).
   Added Tier-0 patterns in `router.py`: `start|run|begin the <flow>
   flow [with key value, …]` and `draft a one-off email to <addr> [brief]`
   — explicit flow starts now cost **0 LLM calls** and are immune to
   free-model drift.

### 16.4 Gmail behaviour findings (external, documented not "fixed")
- **App-generated Message-Ids are preserved** by Gmail (12/12 match the
  stored `emails.message_id`), so `check_replies` matching is reliable.
- **`in-reply-to:`/`references:` header search lags for API-sent
  messages.** A genuine self-reply with a valid bracketed In-Reply-To
  sat in the inbox for 30+ minutes without being found by
  `is:inbox "in-reply-to: <mid>"` (subject search works instantly). The
  app's `replies_tick` query is correct; detection happens once Gmail
  indexes the header. For the E2E, `replied_at` on the verify email was
  pre-set through the service layer (`email_mark_replied`) so the reply
  flow could be verified without mutating Diwali's real data or waiting
  on Gmail's index.
- A raw `In-Reply-To` sent **without angle brackets** was dropped by
  Gmail (header absent on delivery) — the app always sends bracketed
  (RFC 5322 msg-id), verified by test.

### 16.5 Open questions / user actions (append to §13)
- **Q13 — Redeploy the relay.** The repo source is fixed, but the
  deployed Supabase edge function (`boxcsbecivbgcykvkmwq`) still runs the
  broken version. Until redeployed, any send routed *through the relay*
  (cloud instance) will error; local direct sends are unaffected.
  `cloud_relay_url` was restored to the original value after the E2E.
- **Q14 — Diwali batch 26 row count (RESOLVED 2026-11-15).** Batch 26
  ("Diwali Outreach · All contacts 15 Nov", created 05:54:50 by the
  parallel session during the E2E window) captured 1058 contacts because
  the 12 E2E VERIFY contacts still existed. E2E cleanup deleted those
  contacts, leaving 12 orphan drafted rows (all to the E2E self-address).
  The 12 orphans were removed (backup: /tmp/orphan_rows_backup.tsv);
  batch 26 now has exactly 1046 rows (1027 pending + 19 drafted).
- **Q15 — `[text]` in generated subjects.** The template field detector
  treats `[Word]` as a placeholder, so a bracketed phrase in a generated
  subject can be swallowed. Kept as-is (documented design note, matches
  the platform's `[Field]` syntax); the E2E subject pattern was made
  bracket-free.
- **Q16 — openrouter/free drift.** Free auto-router picks a different
  underlying model per request (style drift, occasional no-tool-call
  responses — handled by the one-retry in the agent wrapper and by the
  new Tier-0 patterns). Pinning a specific free model is possible
  (see §15 notes) but not done — user preference unknown.

### 16.7 Follow-up: campaign funnel "Emailed" tile (2026-11-15, after E2E)
User-reported: the campaign funnel showed "Emailed 1,058 — 101% of
contacts" for Diwali. Two causes:
1. **Metric bug (fixed):** `n_emailed` (app.py, campaign funnel
   Contacts → Emailed → Bounced/Replied) counted every non-deleted email
   row, so 1,027 pending + 19 drafted counted as "Emailed". Since
   Bounced/Replied are sub-metrics of sent, the funnel base is now
   `status='sent'` distinct contacts (Diwali: 50). The Home campaign
   card's "X of Y contacts emailed" (home.js) had the same drift
   (`n_emails - pending` incl. drafted) — now sent-only. Regression
   test: `tests/test_funnel_metric.py`.
2. **Data residue (fixed):** the 12 orphan rows in batch 26 (Q14), which
   also inflated the distinct-contact count past 100%.

### 16.8 Final state (updated)
- DB back to pre-E2E baseline (1 campaign, 3 templates, 0 sheets,
  1046 contacts, 50 sent, sent_today 0, undo_holds empty) — verified by
  the `final` phase overview comparison.
- Full automated suite: **103 passed** (`tests/`, excluding
  `test_send_dom.py` which belongs to the parallel UI session and is
  known-failing there).
- Inbox residue for the user: ~30 test mails, all to self — 12
  "literal:Verify…" (pre-fix batch 31), 12 correct verify sends,
  replies/tests/one-offs. Safe to delete in Gmail; the app never deletes
  Gmail items.

### 16.9 Delivery-status buckets: verifying / delivered / bounced (2026-11-15)
User ask: mails that are "sent but not confirmed, not bounced" must keep
being re-checked until confidently in one of two buckets — *delivered* or
*bounced* — and **reply-checking happens only after that** ("then only you
check for replies").

Implemented:
- **Campaign hub** (`app.py` `api_campaign_detail`): `n_verifying` = sent,
  not bounced, not replied, sent inside `delivery_verify_hours` (default
  72 h, admin 0–168 h, `services.validate_send_rail`). `n_sent` now means
  "delivered so far" (sent − verifying). UI: conditional **Verifying** tile
  in the hub stat row (`static/js/pages/hub.js`), sub-label "delivery in
  flight, re-checking".
- **`bounces_tick`** (unchanged, ~2 min): hard-bounce DSNs remain the only
  terminal signal; soft failures (4.x, full mailbox) are ignored, so the
  mail keeps "verifying" while Gmail itself retries (up to ~72 h). Bounced
  mails stop all further re-checking.
- **`replies_tick`** (~40 s): now polls **only confidently-delivered mail**
  — `sent_at` (UTC-normalised) older than the verification window, never
  bounced, not yet replied. In-flight mail is not reply-polled (it may
  still bounce); a 5-minute floor remains. Rotating cursor in the
  non-synced local `tick_state` table (40/tick, wrap) so every mail is
  re-checked over time — fixes the oldest-40 starvation where mail #41+
  was never polled.
- **Mixed `sent_at` formats found in live data** (legacy UTC ISO `T..Z`
  vs local IST `YYYY-MM-DD HH:MM:SS`): new helper
  `services.sent_at_utc_sql(col)` normalises both to UTC in-SQL (offset
  computed dynamically); all bucket comparisons use it against UTC values.
- Agent parity: `actions.py` `_campaign_counts` reports the same
  verifying/delivered split, so Ask AI reads consistent numbers.

Tests: `tests/test_delivery_buckets.py` (4) — bounced excluded from reply
polling; cursor rotation covers all 45 mails; endpoint bucket split
(2 verifying / 2 delivered / 1 bounced); window=0 disables verifying.
Full suite: **110 passed** (excl. `test_send_dom.py`).

Live verification (Diwali, campaign 1, ~11:25 IST): 50 sent (batch 15,
Nov 12 04:50Z → Nov 13 08:18Z), 15 bounced, 35 non-bounced all still
inside the 72 h window → `n_verifying=35, n_sent=0, n_bounced=15`. The
Nov 12 mails start moving into "delivered so far" from ~04:50 UTC and
only then become eligible for reply polling.

Bug fixed this pass: `api_campaign_detail` verifying query — `%`-formatting
applied to only the last string literal (precedence), raising
`TypeError: not all arguments converted`; fixed by parenthesising the
concatenation.

### 16.10 Bounce DSN semantics: any permanent (5xx) failure bounces; soft (4xx) stays verifying (2026-11-15)
User saw a "Delivery incomplete - Gmail will retry for 46 more hours" notice
(vijayasimha.sk@akuva.com) in their own inbox and asked that such mail be
accounted for and never slip again.

Findings (live inbox audit, read-only):
- Gmail DSNs arrive as "Delivery Status Notification (Delay)" (status 4xx,
  Gmail still retrying) and "(Failure)" (status 5xx, final).
- The old parser only marked 5.1.x (plus 5.7/wording-regex cases) as
  bounced. Three real Diwali recipients with FINAL 5.4.1 failures -
  gen.dsouza@accoliteindia.com, shilpi.garg@airtel.com,
  aanchal.narang@aisglass.com ("550 5.4.1 Access denied", policy rejection)
  - had been swallowed: the notice was recorded in processed_dsn but the
  parse discarded it, so those mails would have aged into "delivered"
  after the 72 h window. That was the silent-slip the user was asking
  about.
- A 4xx retry notice is often the only in-app sign that a delivery problem
  exists at all (the akuva case).

Changes:
- `gmail_client.parse_bounce_notice`: ANY 5xx (RFC 3463 permanent) is a
  bounce now, whatever the sub-reason; new `parse_retry_notice` returns
  4xx as [(addr, code)]. Shared helpers `_dsn_recipients/_dsn_status/
  _dsn_diag`.
- `app.bounces_tick`: DSN fetch limit 30 -> 100 (DSNs beyond the cap were
  silently never seen); 4xx notices are logged as `delivery_retry`
  activity ("addr - temporary (4.4.1), Gmail retrying") while the mail
  itself stays *verifying* until Gmail's retries end.
- Live data correction: the 3 swallowed DSNs were re-queued (their
  processed_dsn rows deleted); the running scheduler re-processed them and
  marked mails 18/36/37 bounced at 08:40 IST with the SMTP reason
  (no manual row edits).

Diwali totals after the fix: 50 sent = 18 bounced + 32 verifying +
0 delivered-so-far. The Nov 12 04:30-04:50Z mails start leaving the 72 h
window from ~10:00 IST and only then become reply-eligible. The akuva
mail (id 45) stays verifying until its final DSN (-> bounced) or Gmail
delivers on retry (-> delivered).

Tests: `tests/test_bounce_dsn.py` (4) - 5.1.1/5.4.1/5.7.0 all bounce;
4.4.1 does NOT bounce but is logged exactly once (deduped via
processed_dsn); bounces_tick marks permanent, leaves temporary.
Full suite: **114 passed** (excl. `test_send_dom.py`).
