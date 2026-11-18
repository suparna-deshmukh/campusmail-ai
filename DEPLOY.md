# Hosting CampusMail AI on PythonAnywhere (free, no domain)

Hosts the platform as a public website: `https://<your-username>.pythonanywhere.com`.
Google login gates it; the shared workspace is for the team, and everyone else who
signs in gets their own personal, separate workspace (clean slate).

**Access model**
- `f20230611@goa.bits-pilani.ac.in` is the pinned admin (hardcoded in `db.py` as
  `PINNED_ADMIN`). The first login on a fresh deployment does NOT become admin.
- Admins invite/approve members (Team admin). Approved members share the team workspace.
- Anyone else (pending or declined request) automatically gets a personal workspace:
  own database file, own uploads/attachments/images, neutral branding. They can still
  use the full platform; their data never mixes with the team's.

## One-time setup

1. Create a **free** account at https://www.pythonanywhere.com - note your subdomain
   (`<you>.pythonanywhere.com`).
2. In the **Bash console**:
   ```
   git clone https://github.com/suparna-deshmukh/campusmail-ai.git
   cd campusmail-ai
   pip install --user -r requirements.txt
   ```
3. **Web** tab -> *Add a new web app* -> **Manual configuration** -> Python 3.12 (or
   newest available).
4. Set the **WSGI configuration file** path to:
   `/home/<you>/campusmail-ai/wsgi.py` (its contents are already correct).
5. *Reload* the web app. The site is now live at your subdomain.
6. **Google Cloud Console** -> your OAuth client -> *Authorized redirect URIs*: add
   ```
   https://<you>.pythonanywhere.com/api/google/callback
   ```
   (the app builds the redirect URI from its own host, so no code change is needed
   when moving hosts).
7. Open your subdomain, sign in with the admin account, then in **Settings** paste the
   Google sign-in client ID/secret and the Gmail client JSON, and set the team name.
8. **Team admin** -> invite teammates by email (or approve their access requests).

## Notes

- The built-in scheduler runs as a background thread of the web app - scheduled emails
  and follow-up reminders fire on the minute; the laptop no longer matters.
- Non-team sign-ins land in `data/workspaces/<hash>.db` + a matching folder for their
  files. The team's data lives in `data/campusmail_ai.db` as before.
- Free tier storage is **512 MB total** (including installed packages). The database
  itself stays small for years (a rendered email is a few KB). Uploaded sheets/templates
  are deleted right after processing; what grows is batch attachments + images, and
  **that is exactly what can move to Supabase**: Settings -> *File storage* -> switch the
  backend to Supabase Storage (~1 GB free bucket), then *Move existing files*. With the
  switch on, PA disk usage stays flat (packages + code + small DBs) and file traffic is
  effectively uncapped up to the 1 GB bucket. New uploads go straight to the bucket.
- To back up: zip `data/` from the Bash console and download it.
- To tear everything down: delete the web app + the folder; nothing leaves PythonAnywhere.

## Laptop <-> cloud sync (the relay)

The laptop and the PA site exchange changes through the Supabase relay
(`cloud_relay_url`). Each side identifies itself with a **relay instance
id** stored in the database as `sync_instance_id`.

**The two instances must never share an instance id.** The relay only shows
each side the *other* side's changes, so two hosts with the same id silently
stop seeing each other (mails/replies sent on one side never appear on the
other). The id is generated randomly the first time sync runs - so **never
copy `data/` between instances** (a backup/restore carries the id along).

Symptoms if the ids collide or sync stalls (visible in Settings -> sync
status, or `curl https://<you>.pythonanywhere.com/api/sync/status`):

- *"instance 24h row budget exhausted"* (429) - one id pushed >5,000 rows in
  24 h (two hosts sharing one id double the count). It clears on its own when
  the 24 h window rolls, or immediately with a fresh id (below).
- `fail_count` climbing - the client backs off (up to 30 min) after failed
  ticks; it resets once a tick succeeds.
- Pending rows not moving - the other side never applies them; check both
  sides report the **same** `relay` block and different `instance` values.

### Resyncing one side (full-state re-bootstrap)

Admin only. Resets the instance id, clears the failure backoff, wipes the
push watermarks (so the next tick pushes the whole current state), and forces
a tick immediately:

```
curl -X POST -H "Cookie: <session>" https://<you>.pythonanywhere.com/api/admin/sync/reset-identity
```

or, from a signed-in browser console: `fetch('/api/admin/sync/reset-identity',
{method:'POST'}).then(r=>r.json())`. Then run the same on the other side
(localhost:5001). Within a minute or two both sides converge; verify with
`/api/sync/status` on each (pending_rows -> 0, both instance ids distinct,
no `last_error`).

Quick manual trigger of one tick (no reset): `POST /api/admin/sync/force`.

### Production note

`CAMPUSMAIL_AI_DEV_LOGIN` auto-logs every visitor in as an **admin** without
Google sign-in. It is for throwaway local instances only - **never set it on
the deployed PA web app** (Web tab -> environment variables).
