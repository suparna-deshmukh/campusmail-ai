# MailForge — the outreach team

MailForge is a **local, self-contained web app** for the outreach team. You write (or upload) one
email, point it at your company sheet, and it renders **one personalised email per contact** —
with the right name, company, images, links, attachments and signature — then lets you:

- **Save everything as Gmail drafts** and review them in Gmail,
- **Schedule them** to go out at an exact date/time (a built-in worker sends them — no Gmail
  "send later" tricks, works on any account),
- **Send now** (behind a safe-mode switch and a typed confirmation),
- **Auto-reminder** recruiters who don't reply after N days,
- **Track every email**: sent / replied / reminders, per company and per day,
- **Let AI polish** emails, write templates, suggest subject lines and field mappings (optional).

Everything runs on one computer, in a browser, at **http://localhost:5001**. Data lives in a small
SQLite file inside the `data/` folder. Nothing is sent without a deliberate, confirmed action.

---

## 1. Running the platform

**Mac** — open a terminal, then:

```
cd /path/to/mailforge
./run.sh
```

**Windows** — double-click `run.bat` (or run it from a terminal).

The first run creates a Python virtual environment and installs the packages (needs internet
once). Afterwards it starts in seconds and prints `http://localhost:5001`.

Requirements: Python 3.9+ (the script finds it automatically). Keep the computer **on and the
terminal open** whenever you want scheduled sends to fire.

> The platform uses port **5001** (not 5000, which macOS AirPlay may occupy). If something else
> is using 5001, start it with a different port:
> `MAILFORGE_PORT=8080 ./run.sh` — but then use the matching address for setup steps below.

---

## 2. First login & team access

1. Open **http://localhost:5001** → **Sign in with Google**.
2. The **first person ever to sign in becomes the admin** (that's how the team owner is set).
3. Everyone else who signs in gets a **request under review** screen (they can add a note).
   The admin approves or declines it under **Team admin**. Admins can also **invite an email**
   directly — invited people get in immediately.
4. Members: everyone on the team sees the **same** saved send-accounts, templates, contacts,
   queue and reports. Anything one person connects or creates stays for the whole team.
   (A send-account can be switched to *private* by its owner if it should stay personal.)
5. Every action is written to the **Activity** log with the person's name.

---

## 3. One-time setup (admin, ~10–15 minutes, no installs)

### Step A — Google sign-in (team login)

This is what powers the login button.

1. Go to **console.cloud.google.com** (use the team's Google account). Create a project, e.g. `mailforge`.
2. **APIs & Services → OAuth consent screen** → **External**:
   - App name: `MailForge (outreach team)`, support email: the team's email.
   - **Scopes**: add `openid`, `email`, `profile` (User info/email/profile).
   - **Test users**: add every team member's email.
     (If the screen offers **Internal** and the domain is verified, pick that instead and skip test users.)
3. **APIs & Services → Credentials → Create credentials → OAuth client ID** → type **Web application**:
   - Authorized redirect URIs, add exactly: `http://localhost:5001/api/google/callback`
   - Create. Copy the **Client ID** and **Client secret**.
4. In MailForge: **Settings → Google sign-in (team login)** → paste both → **Save**.
   Also set your own email as the **Admin email** (it always keeps the admin role).

### Step B — Gmail sending client (drafts + scheduled sends)

Same Google Cloud project.

1. **APIs & Services → Library** → search **Gmail API** → **Enable**.
2. **Credentials → Create credentials → OAuth client ID** → type **Desktop app** → Create →
   **Download JSON** (a small file). Open it, copy everything.
3. In MailForge: **Settings → Gmail API client** → paste the whole JSON → **Save**.
4. **Settings → Gmail send-accounts → Connect a Gmail account** → the browser opens Google →
   sign in with the account that should send (e.g. `team@yourdomain.com`) →
   allow. It now appears in the list **for the whole team**, saved permanently.

The platform only asks for *compose* (create drafts, send) and *read* (to detect replies) access —
it never reads mail content beyond checking whether a reply arrived.

You can connect **more accounts** later; each shows up in every "send from" dropdown.

### Step C — AI (optional, key can stay blank)

**Settings → AI assistant**: pick a provider (OpenAI or Google Gemini or any
OpenAI-compatible endpoint), paste the API key, **Test**. AI adds: template writer,
reminder writer, "polish this email" on queued emails, subject-line improver, and
field-mapping suggestions. Everything works fine without it.

---

## 4. The everyday workflow

1. **Contacts** — *Import sheet* with your CSV/Excel (columns like `Serial No.`, `Company`,
   `HR Name`, `HR Email` — names are guessed; re-importing updates, never duplicates).
   The bundled sheet (966 rows, 884 companies) is already loaded on first start.
2. **Templates** — *Upload template*. Word (.docx) files convert to email HTML with bold,
   colours, bullets, hyperlinks and **images at original quality** (stored once, attached at
   send time). Replaceable values are written as `{field}`, `[Field]` or «field» and detected
   automatically. *AI: create a template* / *AI: write reminder template* work from a brief.
   **Email signature**: Settings → *Email signature* (text or uploaded image) is auto-appended
   to plain-text and AI templates; Word templates keep their own built-in signature.
   **Edit** any template to type over it like Word (bold/italic/bullets toolbar); images in the
   body are visible — click one to resize it (25/50/75/100% or Fit) or remove it.
3. **Campaigns** (your CRM home) — a campaign groups one outreach effort: the sheets you merge,
   the batches you create, and their full performance (contacts, drafts, scheduled, sent,
   replies, errors). Create one here or straight from the mail merge. Each campaign page shows
   its batches and sheets, and *Start mail merge* jumps in with it pre-selected.
4. **Mail Merge** (5 steps):
   1. **Campaign** — open an existing one or create a new one,
   2. Pick the template,
   3. **Data source** — use the main contact list, reuse an uploaded sheet, or **upload a new
      sheet for this merge** (CSV or .xlsx, first row = column names). Every row becomes one
      email; all its columns become available in the next step,
   4. Map each field to a column (suggestions pre-filled with sample values from your sheet;
      AI can double-check),
   5. **Preview** 3 real emails, then choose **attachments** (up to 10 files, 15 MB — they go
      out with every email in the batch), tick **automatic follow-up reminders** (days to wait,
      which reminder template, how many retries), set the serial range, CC list and what happens
      next (queue / Gmail drafts / schedule / send now), name the batch, pick the send account →
      **Generate queue**. This only creates records — nothing is sent or drafted yet.
5. **Queue and Schedule** — select rows (or leave it on "pending"):
   - **Create drafts** → they appear in the account's Gmail Drafts folder. Review them there.
   - **Schedule send** → `YYYY-MM-DD HH:MM` local time + optional stagger (minutes between
     emails). The built-in worker sends them at that moment from the chosen account.
   - **Send now** → only when **Safe mode** is OFF (Settings), and it asks you to type SEND.
   - **Stop / Reschedule / Delete / Save as draft** — full control over scheduled rows,
     individually or in bulk (a "Stopped" tab keeps the ones you halted).
   - Open any row (**View**) to see the rendered email, **mark it replied**, **Reply**
     (opens a pre-filled compose; in safe mode it lands as a Gmail draft), get an
     **AI-suggested reply**, or let **AI polish** it (subject + body, saved on confirm).
6. **Outreach Reports** — everything bucketised: sends per day (14-day chart), pipeline
   buckets, top companies with reply rates, and a table of **every** email (searchable,
   CSV-exportable). Replies are detected automatically by checking the sender's inbox for a
   reply to each sent message; you can also mark them by hand.
7. **AI Assistant** (nav → AI Assistant) — a normal chat box (your AI key from Settings).
   Ask for status ("how many are sent this week?"), or let it act: it can create
   campaigns and templates, list sheets, and even **generate + schedule batches from a
   serial range** (it never sends immediately). After the first message it behaves like
   a regular chat with memory of the conversation.

### Safety rails (Settings → Sending rules)

- **Safe mode ON** (default): only drafts can be created; all live sending is blocked.
- **Max live sends per day** (default 100) across all accounts.
- **Stagger** to spread large batches over time.
- **If the platform was off when a send was due**: send as soon as it runs again (default),
  or mark them *missed* instead.
- The **danger zone** can empty the queue (never touches Gmail itself).

---

## 5. Cloud scheduler — send while the laptop is off (built-in, $0)

The platform ships with a **cloud relay** (`cloud/`, a Cloudflare Worker + D1 + R2) that
holds scheduled emails and sends them at the exact minute, 24/7 — no laptop, no VPS.

**Set it up once (10–15 minutes, step-by-step in `cloud/README-CLOUD.md`):**

1. Free Cloudflare account → `npx wrangler login` (one browser click).
2. Create the D1 database + R2 bucket (two commands), point `wrangler.toml` at them.
3. In Settings → **Cloud scheduler**, click **"Show relay setup values"** — it shows the
   5 values (relay secret, Gmail client id/secret, sender email, refresh token) with copy
   buttons; paste each with `npx wrangler secret put …`.
4. `npx wrangler deploy`, then paste the relay URL + secret back into Settings →
   the chip turns **relay ON**.

With the relay ON:

- Scheduling, **stop / reschedule / delete / save-as-draft** all happen in the platform
  and are mirrored to the cloud instantly — stop it here and it won't send there.
- Send results (status, error, message-id → replies are still detected) flow back to the
  queue automatically.
- Clear the relay URL + secret in Settings to go back to 100%-local scheduling.

**Limits to know:** the relay sends up to 10 emails/minute (an 800-mail batch at 1-min
stagger goes out exactly on schedule; 0-min stagger drains over ~80 min). Auto follow-up
**reminders run on the platform**, so keep it running during reminder windows. Safe mode
still blocks everything, cloud included.

If you'd rather not use the relay, the alternatives are: leave it running on an
always-on machine, or run the same app on a small VPS (any $5/mo box, Python 3.9+;
the Google redirect URIs would need the server address added). For a outreach cell,
the relay or a plain VPS are the right size — no big queue infrastructure needed.

---

## 6. Troubleshooting

- **"Port 5001 already in use"** → `lsof -i :5001` (Mac) / `netstat -ano | findstr 5001`
  (Windows), or start with another port: `MAILFORGE_PORT=8080 ./run.sh`.
- **Google error "redirect_uri_mismatch"** → the authorized redirect URI must be exactly
  `http://localhost:5001/api/google/callback` (or the matching host/port you chose).
- **"Google sign-in is not set up yet"** on the login page → Step A above.
- **Connect account says "configure the Gmail OAuth client first"** → Step B, item 3.
- **Consent screen error for a teammate** → they must be added as a *test user* (Step A, item 2),
  or the app must be in production/verified state.
- **Nothing sends at the scheduled time** → the computer was asleep or the terminal was closed.
  The send happens on the next tick (≤8 s) once the app is running again (or is marked
  missed, per Settings).
- **Reset everything** → stop the app, delete `data/mailforge.db*`. (Keep `data/companies.csv`
  if you want the bundled sheet re-seeded.)

## 7. File layout

```
app.py            Flask app: pages, APIs, scheduler worker, reports, reminders
db.py             SQLite schema + helpers (users, accounts, templates, batches, emails, activity)
gmail_client.py   Gmail OAuth + drafts/send, attachments, reply detection
docx_convert.py   Word → email HTML (bold, colour, bullets, links, images), field detection
ai_client.py      OpenAI-compatible AI calls (writer, polisher, subject improver, mapping)
ui/               Jinja pages (login, dashboard, contacts, templates, merge, queue, reports,
                  guide, activity, settings, admin, AI assistant)
cloud/            Cloudflare relay: worker + D1 schema + wrangler config + README-CLOUD.md
static/           app.css, app.js (vanilla JS), img/guide/ (step screenshots)
seed_template/    bundled template + subject + signature image (used on a brand-new DB)
template_images/  images from templates/signatures (stored once, inlined at send time)
data/             mailforge.db (the database), companies.csv (bundled sheet), attachments/
run.sh / run.bat  launchers (auto-create the virtualenv)
```
