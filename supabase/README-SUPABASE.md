# CampusMail AI Cloud Scheduler — Supabase edition (one-time setup, $0, no credit card)

A Supabase project (free tier: Postgres + Edge Functions) holds scheduled emails and
sends them at the exact minute — your laptop can be asleep or off.

**Architecture**

- `migrations/0001_init.sql` — queue tables (`relay_emails`, `relay_token_cache`,
  `relay_config`) + a `pg_cron` job that pings the relay's `/api/pump` every minute.
- `migrations/0002_reminders.sql` — reminder columns (pre-rendered reminder subject/body,
  replied_at, reminder counter).
- `functions/relay/index.ts` — the Edge Function. Routes:
  - `POST /api/emails` — upsert queue rows (called by the platform)
  - `GET  /api/status` — recent results (pulled by the platform)
  - `POST /api/pump`  — send up to 6 due emails via the Gmail API (called by pg_cron)
  - `GET  /health`    — liveness
- Auth: every call carries the `x-relay-secret` header; JWT verification is off
  (deployed with `--no-verify-jwt`).

**What the platform does with it (Settings → "Cloud scheduler")**

Paste the relay base URL + secret there. When set, scheduled/stop/reschedule/delete/
save-as-draft are mirrored to Supabase instantly and the local scheduler defers to the
cloud; results flow back automatically (sent / error + message-id → replies detected).

**Limits**

- 6 sends/minute. A batch at 1-minute stagger goes out exactly on schedule; a 0-min
  stagger batch of 800 drains in ~2.5 hours.
- Free Postgres pauses after a week with no activity — the per-minute cron keeps the
  project awake while it exists; a paused project resumes on the next request.
- **Auto follow-up reminders run in the cloud.** Each pushed mail carries its rendered
  reminder subject/body + reply state; the relay sends reminder N at
  `sent_at + N × reminder_days` if there is no reply. Reminders created here appear in the
  platform queue as normal (reminder) rows.
- Safe mode ON blocks everything, cloud included.
