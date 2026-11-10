# CampusMail AI Cloud Scheduler — one-time setup

This folder contains a tiny "relay" that runs in the cloud (Cloudflare, free tier).
Once it is deployed, scheduled emails are sent **from the cloud at the exact minute** —
your laptop can be asleep, locked, or off. Everything else (drafts, reviewing,
stop/reschedule/delete, reply) keeps working on the platform as usual and is
mirrored to the cloud automatically.

> Cost: **$0**. Cloudflare Workers + D1 + R2 free tier is far beyond a small team's needs.

---

## Step 1 — Create a free Cloudflare account

1. Go to https://dash.cloudflare.com/sign-up and sign up (any email you use).
2. You'll land in the Cloudflare dashboard. That's all you need from the website —
   the rest is done in a terminal (we can do this together in one session).

## Step 2 — Deploy the relay (terminal, ~5 commands)

On a Mac, open **Terminal** and run:

```bash
cd campusmail-ai/cloud
npx wrangler login        # opens the browser once, you click "Authorize"
npx wrangler d1 create campusmail-ai-relay-db
```

The `d1 create` command prints a `database_id`. Open `wrangler.toml` in this folder
and paste that id in place of `PASTE_YOUR_D1_DATABASE_ID_HERE`. Then:

```bash
npx wrangler d1 execute campusmail-ai-relay-db --file=./schema.sql --remote
npx wrangler r2 bucket create campusmail-ai-bodies
```

(If `campusmail-ai-bodies` already exists or the name is taken, pick another name and
also update `bucket_name` in `wrangler.toml`.)

### Step 3 — Set the 5 secrets

Get all 5 values in one click from the platform:

1. Open CampusMail AI → **Settings** → "Cloud scheduler (optional)" card.
2. Pick the Gmail account the relay should send from in the dropdown.
3. Click **"Show relay setup values"** → it shows `RELAY_SECRET`,
   `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_SENDER_EMAIL`,
   `GMAIL_REFRESH_TOKEN` with a Copy button on each (or "Copy all").

Then in the terminal, one command per value (it asks for the value, paste, Enter):

```bash
npx wrangler secret put RELAY_SECRET
npx wrangler secret put GMAIL_CLIENT_ID
npx wrangler secret put GMAIL_CLIENT_SECRET
npx wrangler secret put GMAIL_SENDER_EMAIL
npx wrangler secret put GMAIL_REFRESH_TOKEN
```

### Step 4 — Deploy

```bash
npx wrangler deploy
```

It prints a URL like `https://campusmail-ai-relay.<your-subdomain>.workers.dev`.

## Step 5 — Point the platform at it

Back in CampusMail AI → **Settings** → "Cloud scheduler (optional)":

- **Relay URL** → paste the `https://...workers.dev` URL
- **Relay secret** → paste the `RELAY_SECRET` value
- Click **Save relay settings** → the chip turns into **relay ON**

Done. From now on:

- Scheduled emails are pushed to the cloud the moment you schedule them.
- The cloud sends each one at its minute; results (sent / error, with message-id
  so replies are still tracked) flow back to the queue automatically.
- **Stop / Reschedule / Delete / Save as draft** on the platform update the cloud
  instantly — an email you stop here will not be sent, even if the laptop is off.
- Clear the two fields and save to switch back to 100%-local scheduling.

---

## Good to know

- **Volume:** the relay sends up to 10 emails per minute. An 800-email batch at
  1-minute stagger is sent exactly as scheduled; at 0-minute stagger it drains
  over ~80 minutes (each Gmail message still gets its own moment).
- **Safe mode:** when safe mode is ON in Settings, nothing is sent anywhere —
  the local scheduler and the relay both hold. Turn safe mode OFF before
  relying on the cloud to send.
- **Reminders** (auto follow-up when no reply) run on this platform, not the
  relay — keep the platform on for reminder windows.
- **One Gmail account** is configured in the relay (`GMAIL_SENDER_EMAIL`).
  To add a second sender later, we extend the relay to hold one refresh token
  per account.
- **Refreshing the token:** the relay refreshes the Gmail access token
  automatically; the refresh token itself doesn't expire, so no maintenance.
