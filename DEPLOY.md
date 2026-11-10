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
  itself stays small for years (a rendered email is a few KB); uploaded attachments,
  templates and images are what consume space - prune old batches' attachments from
  time to time (or delete the attachment on a batch to free the file immediately).
- To back up: zip `data/` from the Bash console and download it.
- To tear everything down: delete the web app + the folder; nothing leaves PythonAnywhere.
