"""SQLite storage for CampusMail AI. Stdlib only - no ORM, works on Python 3.9+."""
import contextlib
import csv
import json
import os
import sqlite3
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
DB_PATH = DATA_DIR / "campusmail_ai.db"
PINNED_ADMIN = "f20230611@goa.bits-pilani.ac.in"

_TENANT_DB = ContextVar("campusmail_tenant_db", default=DB_PATH)


def tenant_db():
    return _TENANT_DB.get()


def set_tenant_db(path):
    """Point all db calls at `path` for the current request/thread context."""
    return _TENANT_DB.set(Path(path))


def reset_tenant_db():
    """Back to the team database."""
    _TENANT_DB.set(DB_PATH)


@contextlib.contextmanager
def tenant(path):
    tok = _TENANT_DB.set(Path(path))
    try:
        yield
    finally:
        _TENANT_DB.reset(tok)


@contextlib.contextmanager
def team_context():
    tok = _TENANT_DB.set(DB_PATH)
    try:
        yield
    finally:
        _TENANT_DB.reset(tok)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT, company TEXT, hr_name TEXT, hr_email TEXT,
  status TEXT DEFAULT 'new',
  error TEXT DEFAULT '',
  extra TEXT DEFAULT '{}',
  notes TEXT DEFAULT '',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS email_templates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, type TEXT, subject TEXT, body_html TEXT, fields TEXT,
  is_active INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, template_id INTEGER, mapping TEXT, account_id INTEGER DEFAULT 0,
  attachments TEXT DEFAULT '',
  reminder_days INTEGER DEFAULT 0,
  reminder_template_id INTEGER DEFAULT 0,
  reminder_max INTEGER DEFAULT 1,
  append_signature INTEGER DEFAULT 0,
  cc TEXT DEFAULT '',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS emails(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER, contact_id INTEGER, serial TEXT,
  to_addr TEXT, subject TEXT, body_html TEXT,
  status TEXT DEFAULT 'pending',
  scheduled_at TEXT, sent_at TEXT, error TEXT DEFAULT '',
  account_id INTEGER DEFAULT 0,
  message_id TEXT DEFAULT '',
  replied_at TEXT,
  bounced_at TEXT,
  bounce_reason TEXT DEFAULT '',
  reminder_count INTEGER DEFAULT 0,
  is_reminder INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS processed_dsn(
  account_id INTEGER NOT NULL,
  message_id TEXT NOT NULL,
  processed_at TEXT,
  PRIMARY KEY (account_id, message_id)
);
CREATE TABLE IF NOT EXISTS user_settings(
  user_id INTEGER, key TEXT, value TEXT,
  PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, actor TEXT, action TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE, display_name TEXT,
  role TEXT DEFAULT 'user',          -- admin | user
  status TEXT DEFAULT 'pending',     -- active | pending | denied
  request_note TEXT DEFAULT '',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS gmail_accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT, email TEXT, owner_id INTEGER,
  visibility TEXT DEFAULT 'private',
  token_json TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS campaigns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, description TEXT DEFAULT '',
  created_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS sheets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, campaign_id INTEGER DEFAULT 0, filename TEXT DEFAULT '',
  headers TEXT DEFAULT '[]', rows TEXT DEFAULT '[]', n_rows INTEGER DEFAULT 0,
  created_by TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
"""

DEFAULT_SETTINGS = {
    "safe_mode": "1",
    "max_sends_per_day": "100",
    "default_stagger_min": "0",
    "catchup_policy": "send_now",
    "ai_base_url": "https://api.openai.com/v1",
    "ai_api_key": "",
    "ai_model": "gpt-4o-mini",
    "ai_enabled": "0",
    "google_client_id": "",
    "google_client_secret": "",
    "admin_email": "",
    "cloud_relay_url": "",
    "cloud_relay_secret": "",
    "storage_backend": "local",
    "supabase_url": "",
    "supabase_service_key": "",
    "supabase_bucket": "campusmail-files",
    "brand_team_name": "",
    "brand_tagline": "Local tool - your data stays on this machine.",
}

COLUMN_ALIASES = {
    "serial no.": "serial", "serial no": "serial", "serial": "serial", "sn": "serial",
    "company": "company", "company name": "company", "organization": "company",
    "organisation": "company", "organization name": "company", "organisation name": "company",
    "hr name": "hr_name", "hr": "hr_name", "recruiter name": "hr_name",
    "recruiter": "hr_name", "name": "hr_name", "person name": "hr_name",
    "hr email": "hr_email", "email": "hr_email", "emails": "hr_email",
    "email address": "hr_email", "hr email id": "hr_email", "mail": "hr_email",
}


def now_iso():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def app_secret():
    v = get_setting("app_secret")
    if not v:
        v = os.urandom(24).hex()
        set_setting("app_secret", v)
    return v


# ---------- users ----------

def upsert_google_user(email, display_name, note=""):
    """First contact from a Google login. First user or the pinned admin email becomes admin."""
    c = conn()
    existing = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        c.close()
        return dict(existing)
    first = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 0
    is_admin = first or email.strip().lower() == PINNED_ADMIN
    role = "admin" if is_admin else "user"
    status = "active" if is_admin else "pending"
    c.execute("INSERT INTO users(email, display_name, role, status, request_note, created_at) "
              "VALUES(?,?,?,?,?,?)",
              (email, display_name or email, role, status, note if not first else "first account", now_iso()))
    c.commit()
    uid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.close()
    log_activity("user_first" if first else "user_request",
                 f"{email} ({display_name}) - " + ("first login, made admin" if first
                                                  else ("pinned admin" if is_admin else "request created")),
                 "system")
    return get_user_by_email(email)


def get_user_by_email(email):
    c = conn()
    r = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    c.close()
    return dict(r) if r else None


def get_user(uid):
    c = conn()
    r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    return dict(r) if r else None


def set_user_status(uid, status):
    c = conn()
    c.execute("UPDATE users SET status=? WHERE id=?", (status, uid))
    c.commit()
    c.close()


def set_user_role(uid, role):
    c = conn()
    c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    c.commit()
    c.close()


def remove_user(uid):
    c = conn()
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.commit()
    c.close()


def invite_member(email, display_name=""):
    """Pre-approve a member: they get access immediately on first Google login."""
    c = conn()
    r = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if r:
        c.execute("UPDATE users SET status='active' WHERE id=?", (r["id"],))
        c.commit()
        c.close()
        return dict(r), "updated"
    c.execute("INSERT INTO users(email, display_name, role, status, request_note, created_at) "
              "VALUES(?,?, 'user', 'active', 'invited by admin', ?)",
              (email, display_name or email, now_iso()))
    c.commit()
    c.close()
    log_activity("user_invite", f"Invited {email}")
    return get_user_by_email(email), "created"


def user_count():
    c = conn()
    n = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    c.close()
    return n


def list_users():
    c = conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM users ORDER BY id").fetchall()]
    c.close()
    return rows


# ---------- gmail accounts ----------

def list_accounts(owner_id, include_private_of=0):
    c = conn()
    rows = c.execute(
        "SELECT id, label, email, owner_id, visibility, created_at FROM gmail_accounts "
        "WHERE owner_id IN (?, ?) OR visibility='shared' ORDER BY id",
        (owner_id, include_private_of)).fetchall()
    owners = {r["id"]: dict(r) for r in c.execute("SELECT id, display_name FROM users").fetchall()}
    out = []
    for r in rows:
        d = dict(r)
        d["owner_name"] = owners.get(d["owner_id"], {}).get("display_name", "unknown")
        out.append(d)
    c.close()
    return out


def account_for(user_id, account_id):
    """Fetch an account if the user may use it (own or shared)."""
    c = conn()
    r = c.execute("SELECT * FROM gmail_accounts WHERE id=? AND (owner_id=? OR visibility='shared')",
                  (account_id, user_id)).fetchone()
    c.close()
    return dict(r) if r else None


def save_account_token(account_id, token_json):
    c = conn()
    c.execute("UPDATE gmail_accounts SET token_json=? WHERE id=?", (token_json, account_id))
    c.commit()
    c.close()


def set_account_visibility(account_id, visibility):
    c = conn()
    c.execute("UPDATE gmail_accounts SET visibility=? WHERE id=?", (visibility, account_id))
    c.commit()
    c.close()


def delete_account(account_id):
    c = conn()
    c.execute("DELETE FROM gmail_accounts WHERE id=?", (account_id,))
    c.commit()
    c.close()


def account_token_json(account_id):
    c = conn()
    r = c.execute("SELECT token_json FROM gmail_accounts WHERE id=?", (account_id,)).fetchone()
    c.close()
    return r["token_json"] if r and r["token_json"] else None


def conn():
    p = tenant_db()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _migrate(c):
    cols = {r["name"] for r in c.execute("PRAGMA table_info(emails)").fetchall()}
    for col, decl in (
            ("message_id", "TEXT DEFAULT ''"),
            ("replied_at", "TEXT"),
            ("bounced_at", "TEXT"),
            ("bounce_reason", "TEXT DEFAULT ''"),
            ("reminder_count", "INTEGER DEFAULT 0"),
            ("is_reminder", "INTEGER DEFAULT 0")):
        if col not in cols:
            c.execute("ALTER TABLE emails ADD COLUMN " + col + " " + decl)
    bcols = {r["name"] for r in c.execute("PRAGMA table_info(batches)").fetchall()}
    for col, decl in (
            ("attachments", "TEXT DEFAULT ''"),
            ("reminder_days", "INTEGER DEFAULT 0"),
            ("reminder_template_id", "INTEGER DEFAULT 0"),
            ("reminder_max", "INTEGER DEFAULT 1"),
            ("append_signature", "INTEGER DEFAULT 0"),
            ("cc", "TEXT DEFAULT ''"),
            ("campaign_id", "INTEGER DEFAULT 0"),
            ("sheet_id", "INTEGER DEFAULT 0")):
        if col not in bcols:
            c.execute("ALTER TABLE batches ADD COLUMN " + col + " " + decl)
    kcols = {r["name"] for r in c.execute("PRAGMA table_info(contacts)").fetchall()}
    for col, decl in (("sheet_id", "INTEGER DEFAULT 0"),):
        if col not in kcols:
            c.execute("ALTER TABLE contacts ADD COLUMN " + col + " " + decl)
    c.commit()


def dev_user(email):
    # DEV LOGIN: creates/returns an active admin row for the given email.
    c = conn()
    r = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not r:
        c.execute("INSERT INTO users(email, display_name, role, status, request_note, created_at) "
                  "VALUES(?,?,?,?,?,?)", (email, "Developer Login", "admin", "active",
                                         "dev login", now_iso()))
        c.commit()
    c.close()
    return get_user_by_email(email)


def init_db(seed=True):
    c = conn()
    c.executescript(SCHEMA)
    _migrate(c)
    for k, v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
    c.commit()
    if seed:
        seed_if_empty(c)
    c.close()


def get_setting(key, default=None):
    c = conn()
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def set_setting(key, value):
    c = conn()
    c.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    c.commit()
    c.close()


def all_settings():
    out = dict(DEFAULT_SETTINGS)
    c = conn()
    for r in c.execute("SELECT key, value FROM settings"):
        out[r["key"]] = r["value"]
    c.close()
    return out


def log_activity(action, detail="", actor="user"):
    c = conn()
    c.execute("INSERT INTO activity(ts, actor, action, detail) VALUES(?,?,?,?)",
              (now_iso(), actor, action, detail))
    c.commit()
    c.close()


def seed_if_empty(c):
    """First run: import the bundled Diwali sheet + campus invite template if present."""
    n = c.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
    if n == 0:
        csv_path = BASE / "data" / "companies.csv"
        if csv_path.exists():
            import_sheet_csv(c, str(csv_path), log=False)
    t = c.execute("SELECT COUNT(*) n FROM email_templates").fetchone()["n"]
    if t == 0:
        tpl_dir = BASE / "seed_template"
        body_file = tpl_dir / "body.html"
        subj_file = tpl_dir / "subject.txt"
        if body_file.exists() and subj_file.exists():
            body = body_file.read_text(encoding="utf-8")
            # point the image at the bundled copy
            img_src = tpl_dir / "image1.png"
            if img_src.exists():
                target = BASE / "template_images" / "signature.png"
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(img_src.read_bytes())
                import filestore
                if filestore.configured():
                    filestore.put("signature.png", target.read_bytes(), ns="images")
                    target.unlink()
                body = body.replace('src="image1.png"', 'src="local:signature.png"')
            subject = subj_file.read_text(encoding="utf-8").strip()
            fields = json.dumps(["recruiter_name", "company_name"])
            c.execute("INSERT INTO email_templates(name, type, subject, body_html, fields, is_active) "
                      "VALUES(?,?,?,?,?,1)",
                      ("Campus Recruiter Invite (Sem 2)", "html", subject, body, fields))
            c.commit()
            log_activity("system", "Seeded campus invite template", "system")


def import_sheet_csv(c, path, log=True):
    """Import a CSV with flexible headers. Returns (added, skipped)."""
    added = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header_map = {}
        for k in reader.fieldnames or []:
            key = (k or "").strip().lower()
            header_map[k] = COLUMN_ALIASES.get(key, key)
        for row in reader:
            mapped = {header_map.get(k, k): (v or "").strip() for k, v in row.items() if k}
            company = mapped.get("company", "")
            hr_name = mapped.get("hr_name", "")
            emails = mapped.get("hr_email", "")
            if not company or not hr_name or not emails:
                skipped += 1
                continue
            serial = mapped.get("serial", "")
            extra = {k: v for k, v in mapped.items()
                     if k not in ("serial", "company", "hr_name", "hr_email") and v}
            c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, extra, created_at) "
                      "VALUES(?,?,?,?, 'new', ?, ?)",
                      (serial, company, hr_name, emails, json.dumps(extra), now_iso()))
            added += 1
    c.commit()
    if log:
        log_activity("import", f"Imported {added} contacts ({skipped} skipped)")
    return added, skipped


def counts():
    c = conn()
    out = {}
    for r in c.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status"):
        out[r["status"]] = r["n"]
    for r in c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status"):
        out["email_" + r["status"]] = r["n"]
    out["total_contacts"] = sum(v for k, v in out.items() if not k.startswith("email_"))
    c.close()
    return out
