"""CampusMail AI - recruiter outreach control center for the outreach team.

Google login at the front door; team roles and access requests; multiple
Gmail send-accounts (private or shared); mail merge from Word/HTML templates;
local scheduler worker.  Run:  python app.py  ->  http://localhost:5001
"""
import base64
import csv
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   send_from_directory, session)

import ai_client
import db
import docx_convert
import gmail_client

BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / "uploads"
ATTACH_DIR = BASE / "data" / "attachments"
ALLOWED_ATTACH_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".png",
                      ".jpg", ".jpeg", ".zip", ".ppt", ".pptx", ".html"}
MAX_ATTACH_PER_BATCH = 10
MAX_ATTACH_TOTAL = 15 * 1024 * 1024
app = Flask(__name__, template_folder=str(BASE / "ui"), static_folder=str(BASE / "static"))
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
app.secret_key = "boot"
SCHEDULER_STATE = {"running": True, "last_tick": None, "last_error": ""}
TICK_COUNTER = {"n": 0}
DEV_LOGIN_EMAIL = os.environ.get("CAMPUSMAIL_AI_DEV_LOGIN", "").strip().lower()


# ---------------- auth helpers ----------------

@app.context_processor
def inject_globals():
    return {"me": current_user()}



def current_user():
    if DEV_LOGIN_EMAIL:
        u = db.dev_user(DEV_LOGIN_EMAIL)
        if u:
            return u
    uid = session.get("uid")
    if not uid:
        return None
    u = db.get_user(uid)
    if not u or u["status"] != "active":
        return None
    return u


def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrap(*a, **k):
        u = current_user()
        if not u:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Not signed in"}), 401
            return redirect("/login")
        request.user = u
        return f(*a, **k)
    return wrap


def admin_required(f):
    from functools import wraps
    @wraps(f)
    def wrap(*a, **k):
        u = current_user()
        if not u:
            return redirect("/login")
        if u["role"] != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Admin only"}), 403
            return redirect("/")
        request.user = u
        return f(*a, **k)
    return wrap


def api_err(message, code=400):
    return jsonify({"ok": False, "error": message}), code


def log(action, detail=""):
    actor = request.user["email"] if getattr(request, "user", None) else "system"
    db.log_activity(action, detail, actor)


def brand_team():
    """Team display name: setting -> env var (e.g. Vercel) -> neutral default."""
    v = (db.get_setting("brand_team_name", "") or "").strip()
    if not v:
        v = os.environ.get("CAMPUSMAIL_AI_BRAND_TEAM", "").strip()
    return v or "outreach team"


def brand_foot():
    v = (db.get_setting("brand_tagline", "") or "").strip()
    if not v:
        v = os.environ.get("CAMPUSMAIL_AI_BRAND_FOOT", "").strip()
    return v


@app.context_processor
def inject_brand():
    return {"brand_team": brand_team(), "brand_foot": brand_foot()}


def last_account_for(uid):
    c = db.conn()
    r = c.execute("SELECT value FROM user_settings WHERE user_id=? AND key='last_account_id'", (uid,)).fetchone()
    c.close()
    return int(r["value"]) if r and r["value"].isdigit() else 0


def save_last_account(uid, account_id):
    c = db.conn()
    c.execute("INSERT INTO user_settings(user_id, key, value) VALUES(?,?,?) "
              "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
              (uid, "last_account_id", str(account_id)))
    c.commit()
    c.close()


def fill_fields(text, mapping, row_values):
    def _repl(m):
        name = next(g for g in m.groups() if g).strip()
        val = mapping.get(name, "")
        if val.startswith("column:"):
            return str(row_values.get(val[len("column:"):], ""))
        return val
    return re.sub(docx_convert.PLACEHOLDER_RE, _repl, text)


def all_fields(subject, body):
    """Field names from subject + body, deduplicated, stable order."""
    f = docx_convert.detect_fields(subject or "")
    for x in docx_convert.detect_fields(body or ""):
        if x.lower() not in {y.lower() for y in f}:
            f.append(x)
    return f


def render_email(tpl, row, mapping):
    subject = fill_fields(tpl["subject"], mapping, dict(row))
    body = fill_fields(tpl["body_html"], mapping, dict(row))
    return subject, body


def _row_vals(r):
    """Contact row + any extra sheet columns, so template fields can map to them."""
    vals = dict(r)
    try:
        ex = json.loads(r["extra"] or "{}")
    except Exception:
        ex = {}
    for k, v in (ex.get("row") or {}).items():
        vals.setdefault(k, v)
    return vals


# ---------------- google sign-in ----------------

def google_login_ready():
    return bool(db.get_setting("google_client_id", "").strip())


@app.route("/api/google/start")
def google_start():
    cid = db.get_setting("google_client_id", "").strip()
    if not cid:
        return api_err("Google sign-in is not configured yet (Settings, Google sign-in).", 409)
    p = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": "http://localhost:5001/api/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "prompt": "select_account",
    })
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + p)


@app.route("/api/google/callback/request", methods=["POST"])
def google_request_note():
    d = request.get_json(force=True)
    email = (d.get("email") or "").strip().lower()
    u = db.get_user_by_email(email)
    if not u:
        return api_err("No request found for this email.", 404)
    if u["status"] != "pending":
        return api_err("Your request is no longer pending.")
    c = db.conn()
    c.execute("UPDATE users SET request_note=? WHERE id=?",
              ((d.get("note") or "").strip()[:300], u["id"]))
    c.commit()
    c.close()
    db.log_activity("request_note", email, "system")
    return jsonify({"ok": True})


@app.route("/api/google/callback")
def google_callback():
    code = request.args.get("code")
    err = request.args.get("error")
    if err or not code:
        return render_template("login.html", error="Google sign-in failed: " + (err or "no code"), request_open=False)
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": db.get_setting("google_client_id", ""),
        "client_secret": db.get_setting("google_client_secret", ""),
        "grant_type": "authorization_code",
        "redirect_uri": "http://localhost:5001/api/google/callback",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        tok = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        db.log_activity("google_auth_error", str(e)[:200], "system")
        return render_template("login.html", error="Google sign-in failed: " + str(e)[:200], request_open=False)
    access = tok["access_token"]
    req2 = urllib.request.Request("https://www.googleapis.com/oauth2/v2/userinfo",
                                  headers={"Authorization": "Bearer " + access})
    try:
        info = json.loads(urllib.request.urlopen(req2, timeout=30).read())
    except Exception as e:
        return render_template("login.html", error="Could not read your Google profile: " + str(e)[:200], request_open=False)
    email = info.get("email", "")
    name = info.get("name", email)
    u = db.upsert_google_user(email, name)
    admin_mail = db.get_setting("admin_email", "").strip().lower()
    if admin_mail and email.lower() == admin_mail and u["role"] != "admin":
        db.set_user_role(u["id"], "admin")
        u = db.get_user_by_email(email)
        db.log_activity("user_promoted", email + " promoted to admin (matches admin_email)", "system")
    if u["status"] != "active":
        return render_template("login.html", email=email, name=name, status=u["status"], request_open=False)
    session["uid"] = u["id"]
    db.log_activity("login", email, email)
    return redirect("/")


@app.route("/login")
def login_page():
    if current_user():
        return redirect("/")
    return render_template("login.html", request_open=True, google_ready=google_login_ready())


@app.route("/logout", methods=["POST"])
def logout():
    email = current_user()["email"] if current_user() else "unknown"
    session.clear()
    db.log_activity("logout", "", email)
    return jsonify({"ok": True})


@app.route("/api/login/state")
def login_state():
    u = current_user()
    return jsonify({
        "ok": True,
        "google_ready": google_login_ready(),
        "user_count": db.user_count(),
        "me": {"email": u["email"], "name": u["display_name"], "role": u["role"]} if u else None,
    })


# ---------------- admin ----------------

@app.route("/admin")
@admin_required
def admin_page():
    return render_template("admin.html", page="admin")


@app.route("/api/admin/data")
@admin_required
def api_admin_data():
    c = db.conn()
    pend = [dict(r) for r in c.execute("SELECT * FROM users WHERE status='pending' ORDER BY id").fetchall()]
    members = [dict(r) for r in c.execute("SELECT * FROM users ORDER BY id").fetchall()]
    c.close()
    return jsonify({"ok": True, "requests": pend, "members": members})


@app.route("/api/admin/requests", methods=["POST"])
@admin_required
def admin_requests():
    d = request.get_json(force=True)
    u = db.get_user(int(d.get("user_id")))
    if not u:
        return api_err("User not found", 404)
    if d.get("action") == "approve":
        db.set_user_status(u["id"], "active")
        log("request_approved", u["email"])
    elif d.get("action") == "deny":
        db.set_user_status(u["id"], "denied")
        log("request_denied", u["email"])
    else:
        return api_err("Unknown action")
    return jsonify({"ok": True})


@app.route("/api/admin/members", methods=["POST"])
@admin_required
def admin_members():
    d = request.get_json(force=True)
    u = db.get_user(int(d.get("user_id")))
    if not u:
        return api_err("User not found", 404)
    if d.get("action") == "role":
        role = d.get("role")
        if role not in ("admin", "user"):
            return api_err("Bad role")
        if u["id"] == request.user["id"]:
            return api_err("You cannot change your own role.")
        db.set_user_role(u["id"], role)
        log("role_changed", u["email"] + " -> " + role)
    elif d.get("action") == "remove":
        if u["id"] == request.user["id"]:
            return api_err("You cannot remove yourself.")
        db.remove_user(u["id"])
        log("member_removed", u["email"])
    else:
        return api_err("Unknown action")
    return jsonify({"ok": True})


@app.route("/api/admin/invite", methods=["POST"])
@admin_required
def admin_invite():
    d = request.get_json(force=True)
    email = (d.get("email") or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return api_err("Enter a valid email address.")
    u, how = db.invite_member(email, (d.get("name") or "").strip())
    log("invite", email + " (" + how + ")")
    return jsonify({"ok": True, "email": email, "how": how})


# ---------------- pages ----------------

@app.route("/")
@login_required
def page_dashboard():
    return render_template("dashboard.html", page="dashboard")


@app.route("/contacts")
@login_required
def page_contacts():
    return render_template("contacts.html", page="contacts")


@app.route("/templates")
@login_required
def page_templates():
    return render_template("templates.html", page="templates")


@app.route("/merge")
@login_required
def page_merge():
    return render_template("merge.html", page="merge")


@app.route("/queue")
@login_required
def page_queue():
    return render_template("queue.html", page="queue")


@app.route("/activity")
@login_required
def page_activity():
    return render_template("activity.html", page="activity")


@app.route("/settings")
@login_required
def page_settings():
    return render_template("settings.html", page="settings")


@app.route("/guide")
@login_required
def page_guide():
    return render_template("guide.html", page="guide")


@app.route("/campaigns")
@login_required
def page_campaigns():
    return render_template("campaigns.html", page="campaigns")


@app.route("/campaigns/<int:cid>")
@login_required
def page_campaign_detail(cid):
    return render_template("campaigns.html", page="campaigns")


@app.route("/assistant")
@login_required
def page_assistant():
    return render_template("assistant.html", page="assistant")


@app.route("/local_img/<name>")
@login_required
def local_img(name):
    safe = re.sub(r"[^A-Za-z0-9._\-]", "", name)
    return send_from_directory(str(BASE / "template_images"), safe)


# ---------------- state ----------------

@app.route("/api/state")
@login_required
def api_state():
    c = db.conn()
    stats = {
        "contacts": c.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status").fetchall(),
        "emails": c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status").fetchall(),
        "templates": c.execute("SELECT COUNT(*) n FROM email_templates").fetchone()["n"],
        "batches": c.execute("SELECT COUNT(*) n FROM batches").fetchone()["n"],
    }
    active = c.execute("SELECT id, name FROM email_templates WHERE is_active=1").fetchone()
    n_contacts = c.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
    first_send = c.execute("SELECT COUNT(*) n FROM emails WHERE status IN ('sent','drafted','scheduled')").fetchone()["n"]
    c.close()
    accounts = db.list_accounts(request.user["id"], request.user["id"])
    connected = [a for a in accounts if db.account_token_json(a["id"])]
    now = datetime.now().astimezone().isoformat()
    c = db.conn()
    due = c.execute("SELECT COUNT(*) n FROM emails WHERE status='scheduled' AND scheduled_at <= ?", (now,)).fetchone()["n"]
    nxt = c.execute("SELECT scheduled_at FROM emails WHERE status='scheduled' ORDER BY scheduled_at LIMIT 1").fetchone()
    sent_n = c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent'").fetchone()["n"]
    replied_n = c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent' "
                           "AND replied_at IS NOT NULL AND replied_at != ''").fetchone()["n"]
    reminders_n = c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=1").fetchone()["n"]
    c.close()
    return jsonify({
        "ok": True,
        "me": {"email": request.user["email"], "name": request.user["display_name"], "role": request.user["role"]},
        "stats": {
            "contacts": {r["status"]: r["n"] for r in stats["contacts"]},
            "emails": {r["status"]: r["n"] for r in stats["emails"]},
            "templates": stats["templates"],
            "batches": stats["batches"],
        },
        "active_template": dict(active) if active else None,
        "gmail": {"connected_count": len(connected),
                  "accounts": [{"id": a["id"], "email": a["email"] or a["label"]} for a in connected],
                  "any_connected": bool(connected)},
        "scheduler": {"running": SCHEDULER_STATE["running"],
                      "last_tick": SCHEDULER_STATE["last_tick"],
                      "last_error": SCHEDULER_STATE["last_error"],
                      "due_now": due,
                      "next_fire": nxt["scheduled_at"] if nxt else None},
        "reply": {"sent": sent_n, "replied": replied_n,
                  "rate": round(100.0 * replied_n / sent_n) if sent_n else 0,
                  "reminders": reminders_n},
        "settings": {k: db.get_setting(k) for k in
                     ("safe_mode", "max_sends_per_day", "default_stagger_min", "catchup_policy",
                      "ai_enabled", "ai_model")},
        "ai_has_key": "1" if db.get_setting("ai_api_key") else "0",
        "onboard": {"gmail_connected": bool(connected), "contacts": n_contacts,
                    "templates": stats["templates"], "batches": stats["batches"],
                    "first_send": first_send},
    })


# ---------------- accounts ----------------

@app.route("/api/accounts")
@login_required
def api_accounts():
    rows = db.list_accounts(request.user["id"], request.user["id"])
    last = last_account_for(request.user["id"])
    for r in rows:
        r["connected"] = bool(db.account_token_json(r["id"]))
        r["mine"] = r["owner_id"] == request.user["id"]
        if r["email"]:
            r["label"] = r["email"]
    return jsonify({"ok": True, "accounts": rows, "last_account_id": last})


@app.route("/api/accounts/add", methods=["POST"])
@login_required
def api_account_add():
    d = request.get_json(force=True)
    if not gmail_client.oauth_ready():
        return api_err("Configure the Gmail OAuth client first (Settings, paste the client JSON).", 409)
    c = db.conn()
    c.execute("DELETE FROM gmail_accounts WHERE token_json IS NULL OR token_json=''")
    c.execute("INSERT INTO gmail_accounts(label, owner_id, visibility, created_at) VALUES(?,?,?,?)",
              ((d.get("label") or "").strip(), request.user["id"],
               "private" if d.get("private") else "shared", db.now_iso()))
    aid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    c.close()
    log("gmail_account_add", "account " + str(aid))
    url, _ = gmail_client.start_auth(aid)
    return jsonify({"ok": True, "auth_url": url, "account_id": aid})


@app.route("/api/auth/callback")
def gmail_oauth_callback():
    aid = request.args.get("state")
    code = request.args.get("code")
    if not aid or not code:
        return "OAuth failed: missing code/state", 400
    try:
        email = gmail_client.complete_auth(code, int(aid))
        db.log_activity("gmail_account", "Account " + str(aid) + " connected (" + (email or "") + ")", "system")
        return render_template("auth_done.html", email=email or "")
    except Exception as e:
        db.log_activity("gmail_auth_error", str(e)[:200], "system")
        return "Gmail connection failed: " + str(e)[:300], 400


@app.route("/api/accounts/<int:aid>", methods=["POST"])
@login_required
def api_account_update(aid):
    d = request.get_json(force=True)
    u = request.user
    c = db.conn()
    acc = c.execute("SELECT * FROM gmail_accounts WHERE id=?", (aid,)).fetchone()
    c.close()
    if not acc:
        return api_err("Account not found", 404)
    if acc["owner_id"] != u["id"] and u["role"] != "admin":
        return api_err("Only the owner (or an admin) can change this account.", 403)
    if d.get("visibility") in ("private", "shared"):
        db.set_account_visibility(aid, d["visibility"])
        log("account_visibility", "account " + str(aid) + " -> " + d["visibility"])
    if d.get("label"):
        c = db.conn()
        c.execute("UPDATE gmail_accounts SET label=? WHERE id=?", (d["label"], aid))
        c.commit()
        c.close()
    return jsonify({"ok": True})


@app.route("/api/accounts/<int:aid>/delete", methods=["POST"])
@login_required
def api_account_delete(aid):
    c = db.conn()
    acc = c.execute("SELECT * FROM gmail_accounts WHERE id=?", (aid,)).fetchone()
    c.close()
    if not acc:
        return api_err("Account not found", 404)
    if acc["owner_id"] != request.user["id"] and request.user["role"] != "admin":
        return api_err("Only the owner (or an admin) can remove this account.", 403)
    db.delete_account(aid)
    log("gmail_account_removed", "account " + str(aid))
    return jsonify({"ok": True})


# ---------------- contacts ----------------

@app.route("/api/contacts")
@login_required
def api_contacts():
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or ""
    c = db.conn()
    sql = "SELECT * FROM contacts WHERE 1=1"
    args = []
    if q:
        sql += " AND (company LIKE ? OR hr_name LIKE ? OR hr_email LIKE ? OR serial = ?)"
        args += ["%" + q + "%"] * 3 + [q]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER), id LIMIT 2000"
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    total = c.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
    c.close()
    return jsonify({"ok": True, "rows": rows, "total": total})


@app.route("/api/contacts", methods=["POST"])
@login_required
def api_contact_add():
    d = request.get_json(force=True)
    for k in ("company", "hr_name", "hr_email"):
        if not (d.get(k) or "").strip():
            return api_err("Field '" + k + "' is required.")
    c = db.conn()
    c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status) VALUES(?,?,?,?, 'new')",
              ((d.get("serial") or "").strip(), d["company"].strip(), d["hr_name"].strip(), d["hr_email"].strip()))
    c.commit()
    log("contact_add", d["hr_name"] + " @ " + d["company"])
    c.close()
    return jsonify({"ok": True})


@app.route("/api/contacts/import", methods=["POST"])
@login_required
def api_contact_import():
    f = request.files.get("file")
    if not f:
        return api_err("No file uploaded.")
    UPLOAD_DIR.mkdir(exist_ok=True)
    path = UPLOAD_DIR / f.filename
    f.save(str(path))
    c = db.conn()
    if f.filename.lower().endswith(".xlsx"):
        added, updated, skipped = import_xlsx(c, str(path))
    else:
        added, updated, skipped = import_csv(c, str(path))
    c.close()
    log("import", str(added) + " added, " + str(updated) + " updated, " + str(skipped) + " skipped")
    return jsonify({"ok": True, "added": added, "updated": updated, "skipped": skipped})


def import_csv(c, path):
    added = updated = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header_map = {}
        for k in reader.fieldnames or []:
            header_map[k] = db.COLUMN_ALIASES.get((k or "").strip().lower(), (k or "").strip())
        for row in reader:
            mapped = {header_map.get(k, k): (v or "").strip() for k, v in row.items() if k}
            if not mapped.get("company") or not mapped.get("hr_name") or not mapped.get("hr_email"):
                skipped += 1
                continue
            extra = {k: v for k, v in mapped.items() if k not in ("serial", "company", "hr_name", "hr_email") and v}
            existing = c.execute("SELECT id FROM contacts WHERE lower(hr_email)=lower(?)", (mapped["hr_email"],)).fetchone()
            if existing:
                c.execute("UPDATE contacts SET company=?, hr_name=?, serial=?, extra=? WHERE id=?",
                          (mapped["company"], mapped["hr_name"], mapped.get("serial", ""),
                           json.dumps(extra), existing["id"]))
                updated += 1
            else:
                c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, extra, created_at) "
                          "VALUES(?,?,?,?, 'new', ?, ?)",
                          (mapped.get("serial", ""), mapped["company"], mapped["hr_name"],
                           mapped["hr_email"], json.dumps(extra), db.now_iso()))
                added += 1
    c.commit()
    return added, updated, skipped


def import_xlsx(c, path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    added = updated = skipped = 0
    if rows:
        header = [str(h).strip() if h is not None else "" for h in rows[0]]
        for row in rows[1:]:
            raw = {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}
            mapped = {}
            for k, v in raw.items():
                mapped[db.COLUMN_ALIASES.get(str(k).lower().strip(), str(k).strip())] = str(v or "").strip()
            if not mapped.get("company") or not mapped.get("hr_name") or not mapped.get("hr_email"):
                skipped += 1
                continue
            existing = c.execute("SELECT id FROM contacts WHERE lower(hr_email)=lower(?)", (mapped["hr_email"],)).fetchone()
            if existing:
                c.execute("UPDATE contacts SET company=?, hr_name=?, serial=? WHERE id=?",
                          (mapped["company"], mapped["hr_name"], mapped.get("serial", ""), existing["id"]))
                updated += 1
            else:
                c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, created_at) "
                          "VALUES(?,?,?,?, 'new', ?)",
                          (mapped.get("serial", ""), mapped["company"], mapped["hr_name"],
                           mapped["hr_email"], db.now_iso()))
                added += 1
    c.commit()
    return added, updated, skipped


@app.route("/api/contacts/<int:cid>", methods=["POST"])
@login_required
def api_contact_update(cid):
    d = request.get_json(force=True)
    c = db.conn()
    sets, args = [], []
    for k in ("serial", "company", "hr_name", "hr_email", "status", "notes"):
        if k in d:
            sets.append(k + " = ?")
            args.append(d[k])
    if not sets:
        c.close()
        return api_err("Nothing to update.")
    args += [cid]
    c.execute("UPDATE contacts SET " + ", ".join(sets) + " WHERE id=?", args)
    c.commit()
    c.close()
    return jsonify({"ok": True})


@app.route("/api/contacts/delete", methods=["POST"])
@login_required
def api_contact_delete():
    d = request.get_json(force=True)
    ids = d.get("ids") or []
    c = db.conn()
    c.executemany("DELETE FROM contacts WHERE id=?", [(i,) for i in ids])
    c.commit()
    log("contact_delete", "Deleted " + str(len(ids)) + " contact(s)")
    c.close()
    return jsonify({"ok": True, "deleted": len(ids)})


@app.route("/api/export/contacts.csv")
@login_required
def api_export_contacts():
    import io
    c = db.conn()
    rows = c.execute("SELECT serial, company, hr_name, hr_email, status, error, created_at FROM contacts "
                     "ORDER BY CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER), id").fetchall()
    c.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Serial", "Company", "HR Name", "HR Email", "Status", "Error", "Created At"])
    for r in rows:
        w.writerow([r["serial"], r["company"], r["hr_name"], r["hr_email"], r["status"], r["error"], r["created_at"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=campusmail_ai_contacts.csv"})


# ---------------- templates ----------------

@app.route("/api/templates")
@login_required
def api_templates():
    c = db.conn()
    rows = [dict(r) for r in c.execute(
        "SELECT id, name, type, subject, fields, is_active, created_at, body_html FROM email_templates ORDER BY id").fetchall()]
    for r in rows:
        r["fields"] = json.loads(r["fields"] or "[]")
    c.close()
    return jsonify({"ok": True, "templates": rows})


@app.route("/api/templates/upload", methods=["POST"])
@login_required
def api_template_upload():
    f = request.files.get("file")
    name = (request.form.get("name") or "").strip()
    subject = (request.form.get("subject") or "").strip()
    pasted = (request.form.get("body_html") or "").strip()
    if not f and not pasted:
        return api_err("Upload a file or paste the body text first.")
    UPLOAD_DIR.mkdir(exist_ok=True)
    if f:
        fname = f.filename.lower()
        if fname.endswith(".docx"):
            path = UPLOAD_DIR / f.filename
            f.save(str(path))
            body = docx_convert.docx_to_html(str(path))
            ttype = "docx"
        elif fname.endswith(".html"):
            body = docx_convert.html_template_body(f.read().decode("utf-8", errors="replace"))
            ttype = "html"
        else:
            body = "<p>" + f.read().decode("utf-8", errors="replace").replace("\n\n", "</p><p>") + "</p>"
            ttype = "txt"
    else:
        body = docx_convert.html_template_body(pasted)
        ttype = "html"
        name = name or "Pasted template - " + datetime.now().strftime("%Y-%m-%d")
    fields = all_fields(subject, body)
    c = db.conn()
    if not name:
        name = Path(f.filename).stem.replace("_", " ").replace("-", " ").title()
    c.execute("INSERT INTO email_templates(name, type, subject, body_html, fields, is_active) VALUES(?,?,?,?,?,0)",
              (name, ttype, subject, body, json.dumps(fields)))
    tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    log("template_add", "Template '" + name + "' (" + ttype + ", " + str(len(fields)) + " fields)")
    c.close()
    return jsonify({"ok": True, "id": tid, "fields": fields})


@app.route("/api/templates/ai", methods=["POST"])
@login_required
def api_template_ai():
    d = request.get_json(force=True)
    if not ai_client.ai_configured():
        return api_err("AI is not configured. Add the API key in Settings first.", 409)
    try:
        subject, body = ai_client.write_template(d.get("brief", ""), d.get("tone", "professional"))
    except Exception as e:
        return api_err("AI request failed: " + str(e), 502)
    fields = all_fields(subject, body)
    name = (d.get("name") or "").strip() or "AI template - " + datetime.now().strftime("%Y-%m-%d %H:%M")
    sig = db.get_setting("signature_html", "")
    if sig and "<img" not in body:
        body = docx_convert.html_template_body(body + "<p>" + sig + "</p>")
    else:
        body = docx_convert.html_template_body(body)
    c = db.conn()
    c.execute("INSERT INTO email_templates(name, type, subject, body_html, fields, is_active) VALUES(?,?,?,?,?,0)",
              (name, "ai", subject, body, json.dumps(fields)))
    tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    log("template_ai", "AI template '" + name + "'")
    c.close()
    return jsonify({"ok": True, "id": tid, "subject": subject, "fields": fields})


@app.route("/api/templates/<int:tid>", methods=["POST"])
@login_required
def api_template_update(tid):
    d = request.get_json(force=True)
    c = db.conn()
    cur = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    if not cur:
        c.close()
        return api_err("Template not found", 404)
    sets, args = [], []
    if "name" in d:
        sets.append("name = ?")
        args.append(d["name"])
    if "subject" in d:
        sets.append("subject = ?")
        args.append(d["subject"])
    if "body_html" in d:
        sets.append("body_html = ?")
        args.append(d["body_html"])
    if "subject" in d or "body_html" in d:
        sets.append("fields = ?")
        args.append(json.dumps(all_fields(d.get("subject", cur["subject"]),
                                        d.get("body_html", cur["body_html"]))))
    if sets:
        args.append(tid)
        c.execute("UPDATE email_templates SET " + ", ".join(sets) + " WHERE id=?", args)
        c.commit()
    c.close()
    return jsonify({"ok": True})


@app.route("/api/templates/<int:tid>/activate", methods=["POST"])
@login_required
def api_template_activate(tid):
    c = db.conn()
    c.execute("UPDATE email_templates SET is_active=0")
    c.execute("UPDATE email_templates SET is_active=1 WHERE id=?", (tid,))
    c.commit()
    log("template_activate", "Template " + str(tid) + " set active")
    c.close()
    return jsonify({"ok": True})


@app.route("/api/templates/<int:tid>/delete", methods=["POST"])
@login_required
def api_template_delete(tid):
    c = db.conn()
    c.execute("DELETE FROM email_templates WHERE id=?", (tid,))
    c.commit()
    log("template_delete", "Template " + str(tid) + " deleted")
    c.close()
    return jsonify({"ok": True})


@app.route("/api/templates/<int:tid>/preview")
@login_required
def api_template_preview(tid):
    c = db.conn()
    tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    row = c.execute("SELECT * FROM contacts ORDER BY id LIMIT 1").fetchone()
    c.close()
    if not tpl:
        return api_err("Template not found", 404)
    mapping = {}
    for f, col in docx_convert.suggest_mapping(json.loads(tpl["fields"] or "[]"),
                                                ["serial", "company", "hr_name", "hr_email"]).items():
        if col:
            mapping[f] = "column:" + col
    vals = dict(row) if row else {}
    subject, body = render_email(dict(tpl), vals, mapping)
    return jsonify({"ok": True, "subject": subject,
                    "body": gmail_client.inline_local_images(body),
                    "sample_row": vals, "mapping": mapping})


# ---------------- merge ----------------

@app.route("/api/merge/suggest")
@login_required
def api_merge_suggest():
    tid = request.args.get("template_id", type=int)
    c = db.conn()
    tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    c.close()
    if not tpl:
        return api_err("Template not found", 404)
    fields = json.loads(tpl["fields"] or "[]")
    cols = [x.strip() for x in (request.args.get("columns") or "").split(",") if x.strip()] \
        or ["serial", "company", "hr_name", "hr_email"]
    return jsonify({"ok": True,
                    "suggestion": docx_convert.suggest_mapping(fields, cols)})


@app.route("/api/merge/preview", methods=["POST"])
@login_required
def api_merge_preview():
    d = request.get_json(force=True)
    tid = d.get("template_id")
    mapping = d.get("mapping") or {}
    limit = int(d.get("limit") or 3)
    sheet_id = int(d.get("sheet_id") or 0)
    c = db.conn()
    tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    sfrom, sto = d.get("serial_from"), d.get("serial_to")
    w = " AND sheet_id=?" if sheet_id else ""
    args = [sheet_id] if sheet_id else []
    if sfrom:
        w += " AND CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER) >= ?"
        args.append(int(sfrom))
    if sto:
        w += " AND CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER) <= ?"
        args.append(int(sto))
    rows = c.execute("SELECT * FROM contacts WHERE 1=1" + w +
                     " ORDER BY CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER), id LIMIT ?",
                     args + [limit]).fetchall()
    total = c.execute("SELECT COUNT(*) n FROM contacts WHERE 1=1" + w, args).fetchone()["n"]
    c.close()
    if not tpl:
        return api_err("Template not found", 404)
    sig = ""
    if d.get("append_signature"):
        sig = db.get_setting("signature_html", "")
    out = []
    for r in rows:
        subject, body = render_email(dict(tpl), _row_vals(r), mapping)
        if sig:
            body += "<p style='margin-top:14px'>" + sig + "</p>"
        out.append({"serial": r["serial"], "company": r["company"], "hr_name": r["hr_name"],
                    "hr_email": r["hr_email"], "subject": subject, "body": body})
    return jsonify({"ok": True, "previews": out, "total": total, "mapping": mapping})


@app.route("/api/merge/generate", methods=["POST"])
@login_required
def api_merge_generate():
    return _run_merge(request.get_json(force=True))


def _run_merge(d):
    tid = d.get("template_id")
    mapping = d.get("mapping") or {}
    name = (d.get("name") or "").strip() or "Batch " + datetime.now().strftime("%Y-%m-%d %H:%M")
    account_id = int(d.get("account_id") or 0)
    campaign_id = int(d.get("campaign_id") or 0)
    sheet_id = int(d.get("sheet_id") or 0)
    action = d.get("action") or "queue"
    if action not in ("queue", "draft", "schedule", "send"):
        return api_err("Unknown action.")
    try:
        cc_list = parse_cc(d.get("cc"))
    except ValueError as e:
        return api_err(str(e), 400)
    if len(cc_list) > 20:
        return api_err("Up to 20 CC addresses.")
    sfrom, sto = d.get("serial_from"), d.get("serial_to")
    if (sfrom or sto) and int(sfrom or 0) > int(sto or 999999):
        return api_err("Serial range start is after its end.")
    when, stagger = None, 0
    if action == "schedule":
        when_str = (d.get("at") or "").strip()
        stagger = int(d.get("stagger") or 0)
        try:
            when = datetime.strptime(when_str, "%Y-%m-%d %H:%M").replace(tzinfo=datetime.now().astimezone().tzinfo)
        except ValueError:
            return api_err("Schedule time format must be YYYY-MM-DD HH:MM (local time).")
        now = datetime.now().astimezone()
        if when < now + timedelta(minutes=30):
            return api_err("Schedule time must be at least 30 minutes in the future.")
        if when > now + timedelta(days=45):
            return api_err("Schedule up to 45 days ahead only.")
    if action in ("draft", "schedule", "send"):
        try:
            _require_account(account_id, request.user)
        except RuntimeError as e:
            return api_err(str(e), 409)
        if action == "send" and db.get_setting("safe_mode") == "1":
            return api_err("Safe mode is ON - immediate sending is blocked. Pick Drafts or Schedule, or turn off Safe mode in Settings.", 409)
    c = db.conn()
    tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    if not tpl:
        c.close()
        return api_err("Template not found", 404)
    w = " AND sheet_id=?" if sheet_id else ""
    args = [sheet_id] if sheet_id else []
    if sfrom:
        w += " AND CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER) >= ?"
        args.append(int(sfrom))
    if sto:
        w += " AND CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER) <= ?"
        args.append(int(sto))
    rows = c.execute("SELECT * FROM contacts WHERE 1=1" + w +
                     " ORDER BY CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER), id", args).fetchall()
    if not rows:
        c.close()
        return api_err("No contacts in that serial range.", 404)
    append_sig = 1 if d.get("append_signature") else 0
    sig = db.get_setting("signature_html", "") if append_sig else ""
    cc_str = ", ".join(cc_list)
    c.execute("INSERT INTO batches(name, template_id, mapping, account_id, created_at, append_signature, cc, campaign_id, sheet_id) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (name, tid, json.dumps(mapping), account_id, db.now_iso(), append_sig, cc_str, campaign_id, sheet_id))
    bid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    eids = []
    for r in rows:
        subject, body = render_email(dict(tpl), _row_vals(r), mapping)
        if sig:
            body += "<p style='margin-top:14px'>" + sig + "</p>"
        c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, account_id, status) "
                  "VALUES(?,?,?,?,?,?,?,'pending')",
                  (bid, r["id"], r["serial"], r["hr_email"], subject, body, account_id))
        eids.append(c.execute("SELECT last_insert_rowid() id").fetchone()["id"])
    c.commit()
    extra = {"action": action}
    if action == "draft":
        made = failed = 0
        for eid in eids:
            r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
            try:
                batch = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
                attach = _load_attachments(dict(batch)) if batch else []
                gmail_client.create_draft(account_id, r["to_addr"], r["subject"], r["body_html"],
                                          attach or None, _cc_of(dict(batch)))
                c.execute("UPDATE emails SET status='drafted', account_id=?, error='' WHERE id=?", (account_id, eid))
                c.execute("UPDATE contacts SET status='drafted' WHERE id=?", (r["contact_id"],))
                made += 1
            except Exception as e:
                c.execute("UPDATE emails SET status='error', error=? WHERE id=?", (str(e)[:300], eid))
                failed += 1
        c.commit()
        extra.update(created_drafts=made, failed=failed)
    elif action == "schedule":
        for i, eid in enumerate(eids):
            t = when + timedelta(minutes=stagger * i)
            c.execute("UPDATE emails SET status='scheduled', account_id=?, scheduled_at=?, error='' WHERE id=?",
                      (account_id, t.isoformat(), eid))
            c.execute("UPDATE contacts SET status='scheduled' WHERE id=(SELECT contact_id FROM emails WHERE id=?)", (eid,))
        c.commit()
        for eid in eids:
            _cloud_push_email(eid, "scheduled")
        extra.update(scheduled=len(eids),
                     last=(when + timedelta(minutes=stagger * max(0, len(eids) - 1))).isoformat())
    elif action == "send":
        cap = int(db.get_setting("max_sends_per_day") or 100)
        sent_today = sends_today()
        ok = fail = 0
        for eid in eids:
            if sent_today + ok >= cap:
                break
            r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
            try:
                batch = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
                attach = _load_attachments(dict(batch)) if batch else []
                res = gmail_client.send_with_retry(account_id, r["to_addr"], r["subject"], r["body_html"],
                                                   attach or None, _cc_of(dict(batch)))
                mid = res.get("message_id", "") if isinstance(res, dict) else ""
                c.execute("UPDATE emails SET status='sent', account_id=?, sent_at=?, error='', message_id=? WHERE id=?",
                          (account_id, db.now_iso(), mid, eid))
                c.execute("UPDATE contacts SET status='sent' WHERE id=?", (r["contact_id"],))
                ok += 1
            except Exception as e:
                c.execute("UPDATE emails SET status='error', error=? WHERE id=?", (str(e)[:300], eid))
                fail += 1
            time.sleep(0.4)
        c.commit()
        extra.update(sent=ok, failed=fail)
    save_last_account(request.user["id"], account_id) if account_id else None
    log("batch_create", "Batch '" + name + "' - " + str(len(rows)) + " emails from template " + str(tid)
        + " (action " + action + ", cc " + str(len(cc_list)) + ")")
    c.close()
    return jsonify({"ok": True, "batch_id": bid, "created": len(rows), **extra})


# ---------------- campaigns ----------------

@app.route("/api/campaigns")
@login_required
def api_campaigns():
    c = db.conn()
    out = []
    for cam in c.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall():
        st = {r["status"]: r["n"] for r in c.execute(
            "SELECT e.status, COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
            "WHERE b.campaign_id=? GROUP BY e.status", (cam["id"],))}
        out.append({**dict(cam), "status_counts": st, "n_emails": sum(st.values()),
                    "n_batches": c.execute("SELECT COUNT(*) n FROM batches WHERE campaign_id=?", (cam["id"],)).fetchone()["n"],
                    "n_sheets": c.execute("SELECT COUNT(*) n FROM sheets WHERE campaign_id=?", (cam["id"],)).fetchone()["n"],
                    "n_contacts": c.execute(
                        "SELECT COUNT(*) n FROM contacts ct JOIN sheets s ON ct.sheet_id=s.id WHERE s.campaign_id=?",
                        (cam["id"],)).fetchone()["n"],
                    "last": c.execute("SELECT MAX(created_at) t FROM batches WHERE campaign_id=?", (cam["id"],)).fetchone()["t"]})
    c.close()
    return jsonify({"ok": True, "campaigns": out})


@app.route("/api/campaigns", methods=["POST"])
@login_required
def api_campaign_create():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name:
        return api_err("Give the campaign a name.")
    c = db.conn()
    c.execute("INSERT INTO campaigns(name, description, created_by, created_at) VALUES(?,?,?,?)",
              (name, (d.get("description") or "").strip(), request.user["email"], db.now_iso()))
    cid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    log("campaign_create", name)
    c.close()
    return jsonify({"ok": True, "id": cid})


@app.route("/api/campaigns/<int:cid>")
@login_required
def api_campaign_detail(cid):
    c = db.conn()
    cam = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not cam:
        c.close()
        return api_err("Campaign not found", 404)
    st = {r["status"]: r["n"] for r in c.execute(
        "SELECT e.status, COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
        "WHERE b.campaign_id=? GROUP BY e.status", (cid,))}
    batches = []
    for b in c.execute("SELECT * FROM batches WHERE campaign_id=? ORDER BY id DESC", (cid,)).fetchall():
        bc = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM emails WHERE batch_id=? GROUP BY status", (b["id"],))}
        batches.append({**dict(b), "counts": bc})
    sheets = [dict(r) for r in c.execute("SELECT * FROM sheets WHERE campaign_id=? ORDER BY id DESC", (cid,)).fetchall()]
    for sh in sheets:
        sh["headers"] = json.loads(sh["headers"] or "[]")
    n_contacts = c.execute(
        "SELECT COUNT(*) n FROM contacts ct JOIN sheets s ON ct.sheet_id=s.id WHERE s.campaign_id=?",
        (cid,)).fetchone()["n"]
    c.close()
    return jsonify({"ok": True, "campaign": dict(cam), "status_counts": st,
                    "batches": batches, "sheets": sheets, "n_contacts": n_contacts})


# ---------------- sheets ----------------

def _parse_sheet_file(f):
    """Return (headers, rows) from a CSV or XLSX upload. rows = list of {col: str} dicts."""
    fname = (f.filename or "").lower()
    raw_headers, raw_rows = [], []
    if fname.endswith(".xlsx"):
        from openpyxl import load_workbook
        import io
        wb = load_workbook(io.BytesIO(f.read()), read_only=True, data_only=True)
        data = [[("" if v is None else str(v).strip()) for v in row] for row in wb.active.iter_rows(values_only=True)]
        data = [r for r in data if any(x.strip() for x in r)]
        if data:
            raw_headers, raw_rows = data[0], data[1:]
    elif fname.endswith((".csv", ".txt")):
        import io
        text = f.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        raw_headers = [(h or "").strip() for h in (reader.fieldnames or [])]
        for row in reader:
            raw_rows.append([(row.get(h) or "").strip() for h in raw_headers])
    else:
        raise ValueError("Upload a .csv or .xlsx file.")
    headers, seen = [], {}
    for h in raw_headers:
        base = re.sub(r"[^a-z0-9]+", "_", h.lower()).strip("_") or "col"
        if base in seen:
            seen[base] += 1
            base = base + "_" + str(seen[base])
        else:
            seen[base] = 1
        headers.append(base)
    rows = [{headers[j]: (r[j] if j < len(r) else "") for j in range(len(headers))} for r in raw_rows[:5000]]
    return headers, rows


@app.route("/api/sheets/upload", methods=["POST"])
@login_required
def api_sheet_upload():
    f = request.files.get("file")
    if not f:
        return api_err("No file uploaded.")
    campaign_id = int(request.form.get("campaign_id") or 0)
    try:
        headers, rows = _parse_sheet_file(f)
    except ValueError as e:
        return api_err(str(e), 400)
    if not headers:
        return api_err("No columns found - the first row should be column names.")
    name = Path(f.filename).stem.replace("_", " ").replace("-", " ").strip() \
        or "Sheet " + datetime.now().strftime("%Y-%m-%d %H:%M")
    email_col = next((h for h in headers if re.search(r"email|mail", h, re.I)), "")
    comp_col = next((h for h in headers if re.search(r"company|organi[sz]ation|\\borg\\b|firm", h, re.I)), "")
    name_col = next((h for h in headers if h != comp_col and re.search(r"recruiter|hr|name|person|contact", h, re.I)), "")
    c = db.conn()
    c.execute("INSERT INTO sheets(name, campaign_id, filename, headers, rows, n_rows, created_by, created_at) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (name, campaign_id, f.filename, json.dumps(headers), json.dumps(rows),
               len(rows), request.user["email"], db.now_iso()))
    sid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    no_email = 0
    for i, r in enumerate(rows):
        em = (r.get(email_col) or "").strip()
        if not em:
            no_email += 1
        c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, sheet_id, extra, created_at) "
                  "VALUES(?,?,?,?, 'new', ?, ?, ?)",
                  (str(i + 1), (r.get(comp_col) or "").strip(), (r.get(name_col) or "").strip(), em,
                   sid, json.dumps({"sheet": name, "row": r}), db.now_iso()))
    c.commit()
    log("sheet_upload", name + " - " + str(len(rows)) + " rows"
        + (" (campaign " + str(campaign_id) + ")" if campaign_id else ""))
    c.close()
    return jsonify({"ok": True, "id": sid, "name": name, "headers": headers, "n_rows": len(rows),
                    "preview": rows[:3], "email_col": email_col, "name_col": name_col,
                    "company_col": comp_col, "no_email": no_email})


@app.route("/api/sheets")
@login_required
def api_sheets():
    c = db.conn()
    rows = [dict(r) for r in c.execute(
        "SELECT id, name, campaign_id, filename, headers, n_rows, created_at FROM sheets ORDER BY id DESC").fetchall()]
    for r in rows:
        r["headers"] = json.loads(r["headers"] or "[]")
    c.close()
    return jsonify({"ok": True, "sheets": rows})


@app.route("/api/sheets/<int:sid>")
@login_required
def api_sheet_get(sid):
    c = db.conn()
    r = c.execute("SELECT * FROM sheets WHERE id=?", (sid,)).fetchone()
    c.close()
    if not r:
        return api_err("Sheet not found", 404)
    d = dict(r)
    d["headers"] = json.loads(d["headers"] or "[]")
    d["rows"] = json.loads(d["rows"] or "[]")
    return jsonify({"ok": True, "sheet": d})


@app.route("/api/sheets/<int:sid>/delete", methods=["POST"])
@login_required
def api_sheet_delete(sid):
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM batches WHERE sheet_id=?", (sid,)).fetchone()["n"]
    if n:
        c.close()
        return api_err("This sheet is used by " + str(n) + " batch(es), so it cannot be deleted.")
    c.execute("DELETE FROM sheets WHERE id=?", (sid,))
    c.commit()
    log("sheet_delete", "Sheet " + str(sid) + " deleted")
    c.close()
    return jsonify({"ok": True})


@app.route("/api/batches")
@login_required
def api_batches():
    c = db.conn()
    cams = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM campaigns").fetchall()}
    rows = []
    for b in c.execute("SELECT * FROM batches ORDER BY id DESC LIMIT 50").fetchall():
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM emails WHERE batch_id=? GROUP BY status", (b["id"],))}
        rows.append({**dict(b), "counts": counts,
                     "campaign_name": cams.get(b["campaign_id"] or 0, "")})
    c.close()
    return jsonify({"ok": True, "batches": rows})


# ---------------- emails (queue) ----------------

def _email_rows(params):
    c = db.conn()
    sql = "SELECT * FROM emails WHERE 1=1"
    args = []
    status = params.get("status")
    if status == "replied":
        sql += " AND replied_at IS NOT NULL AND replied_at != ''"
    elif status:
        sql += " AND status = ?"
        args.append(status)
    if params.get("batch_id"):
        sql += " AND batch_id = ?"
        args.append(params["batch_id"])
    sql += " ORDER BY CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER), id LIMIT 3000"
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    return rows


@app.route("/api/emails")
@login_required
def api_emails():
    rows = _email_rows(request.args)
    return jsonify({"ok": True, "rows": rows, "total": len(rows)})


def _selected_ids(params):
    ids = params.get("ids")
    if ids:
        return [int(i) for i in ids]
    return [r["id"] for r in _email_rows(params) if r["status"] not in ("sent", "deleted")]


@app.route("/api/emails/view")
@login_required
def api_email_view():
    c = db.conn()
    r = c.execute("SELECT * FROM emails WHERE id=?", (request.args.get("id"),)).fetchone()
    c.close()
    if not r:
        return api_err("Not found", 404)
    r = dict(r)
    r["body"] = gmail_client.inline_local_images(r.pop("body_html"))
    return jsonify({"ok": True, "email": r})


def _require_account(account_id, u):
    acc = db.account_for(u["id"], account_id)
    if not acc:
        raise RuntimeError("Send account not found or not available to you.")
    if not db.account_token_json(account_id):
        raise RuntimeError("That Gmail account is not connected yet (Settings, Gmail accounts).")
    return acc

CC_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def parse_cc(raw):
    out = []
    for part in (raw or "").replace(";", ",").split(","):
        a = part.strip()
        if not a:
            continue
        if not CC_RE.match(a):
            raise ValueError("Invalid CC address: " + a)
        if a.lower() not in [x.lower() for x in out]:
            out.append(a)
    return out


def _cc_of(batch):
    if batch and batch.get("cc"):
        try:
            return parse_cc(batch["cc"]) or None
        except ValueError:
            return None
    return None


@app.route("/api/emails/draft", methods=["POST"])
@login_required
def api_emails_draft():
    d = request.get_json(force=True)
    account_id = int(d.get("account_id") or 0)
    ids = _selected_ids(d)
    if not ids:
        return api_err("Nothing selected.")
    try:
        _require_account(account_id, request.user)
    except RuntimeError as e:
        return api_err(str(e), 409)
    save_last_account(request.user["id"], account_id)
    c = db.conn()
    made = failed = 0
    for eid in ids:
        r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
        if not r or r["status"] == "deleted":
            continue
        try:
            batch = c.execute("SELECT * FROM batches WHERE id=?", (r["batch_id"],)).fetchone()
            attach = _load_attachments(dict(batch)) if batch else []
            gmail_client.create_draft(account_id, r["to_addr"], r["subject"], r["body_html"],
                                      attach or None, _cc_of(dict(batch)))
            c.execute("UPDATE emails SET status='drafted', account_id=?, error='' WHERE id=?", (account_id, eid))
            c.execute("UPDATE contacts SET status='drafted' WHERE id=?", (r["contact_id"],))
            made += 1
            _cloud_push_email(eid, "drafted")
        except Exception as e:
            c.execute("UPDATE emails SET status='error', error=? WHERE id=?", (str(e)[:300], eid))
            failed += 1
    c.commit()
    log("drafts_create", str(made) + " drafts (" + str(failed) + " failed) from account " + str(account_id))
    c.close()
    return jsonify({"ok": True, "created": made, "failed": failed})


@app.route("/api/emails/schedule", methods=["POST"])
@login_required
def api_emails_schedule():
    d = request.get_json(force=True)
    account_id = int(d.get("account_id") or 0)
    ids = _selected_ids(d)
    if not ids:
        return api_err("Nothing selected.")
    try:
        _require_account(account_id, request.user)
    except RuntimeError as e:
        return api_err(str(e), 409)
    when_str = (d.get("at") or "").strip()
    stagger = int(d.get("stagger") or 0)
    try:
        when = datetime.strptime(when_str, "%Y-%m-%d %H:%M").replace(tzinfo=datetime.now().astimezone().tzinfo)
    except ValueError:
        return api_err("Time format must be YYYY-MM-DD HH:MM (local time).")
    now = datetime.now().astimezone()
    if when < now + timedelta(minutes=30):
        return api_err("Schedule time must be at least 30 minutes in the future.")
    if when > now + timedelta(days=45):
        return api_err("Up to 45 days ahead only.")
    save_last_account(request.user["id"], account_id)
    c = db.conn()
    n = 0
    for i, eid in enumerate(ids):
        t = when + timedelta(minutes=stagger * i)
        c.execute("UPDATE emails SET status='scheduled', account_id=?, scheduled_at=?, error='' WHERE id=?",
                  (account_id, t.isoformat(), eid))
        c.execute("UPDATE contacts SET status='scheduled' WHERE id=(SELECT contact_id FROM emails WHERE id=?)", (eid,))
        n += 1
    c.commit()
    for eid in ids:
        _cloud_push_email(eid, "scheduled")
    log("schedule", str(n) + " emails scheduled for " + when_str + " (stagger " + str(stagger) + " min, account " + str(account_id) + ")")
    c.close()
    return jsonify({"ok": True, "scheduled": n, "first": when.isoformat(),
                    "last": (when + timedelta(minutes=stagger * max(0, len(ids) - 1))).isoformat()})


@app.route("/api/emails/send-now", methods=["POST"])
@login_required
def api_emails_send_now():
    if db.get_setting("safe_mode") == "1":
        return api_err("Safe mode is ON. Turn it off in Settings to allow immediate sending.", 409)
    d = request.get_json(force=True)
    account_id = int(d.get("account_id") or 0)
    ids = _selected_ids(d)
    if not ids:
        return api_err("Nothing selected.")
    try:
        _require_account(account_id, request.user)
    except RuntimeError as e:
        return api_err(str(e), 409)
    cap = int(db.get_setting("max_sends_per_day") or 100)
    sent_today = sends_today()
    if sent_today >= cap:
        return api_err("Daily cap reached (" + str(cap) + ").", 409)
    save_last_account(request.user["id"], account_id)
    c = db.conn()
    ok = fail = 0
    for eid in ids:
        if sent_today + ok >= cap:
            break
        r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
        if not r:
            continue
        try:
            batch = c.execute("SELECT * FROM batches WHERE id=?", (r["batch_id"],)).fetchone()
            attach = _load_attachments(dict(batch)) if batch else []
            res = gmail_client.send_with_retry(account_id, r["to_addr"], r["subject"], r["body_html"],
                                               attach or None, _cc_of(dict(batch)))
            mid = res.get("message_id", "") if isinstance(res, dict) else ""
            c.execute("UPDATE emails SET status='sent', account_id=?, sent_at=?, error='', message_id=? WHERE id=?",
                      (account_id, db.now_iso(), mid, eid))
            c.execute("UPDATE contacts SET status='sent' WHERE id=?", (r["contact_id"],))
            ok += 1
        except Exception as e:
            c.execute("UPDATE emails SET status='error', error=? WHERE id=?", (str(e)[:300], eid))
            fail += 1
        time.sleep(0.4)
    c.commit()
    log("send_now", str(ok) + " sent now (" + str(fail) + " failed) from account " + str(account_id))
    c.close()
    return jsonify({"ok": True, "sent": ok, "failed": fail})


@app.route("/api/emails/delete", methods=["POST"])
@login_required
def api_emails_delete():
    d = request.get_json(force=True)
    ids = d.get("ids") or []
    if not ids:
        return api_err("Nothing selected.")
    c = db.conn()
    c.executemany("UPDATE emails SET status='deleted', scheduled_at=NULL WHERE id=?", [(i,) for i in ids])
    c.commit()
    log("emails_delete", "Removed " + str(len(ids)) + " email(s) from queue")
    c.close()
    for i in ids:
        _cloud_push_email(i, "deleted")
    return jsonify({"ok": True, "removed": len(ids)})


@app.route("/api/emails/cancel", methods=["POST"])
@login_required
def api_emails_cancel():
    d = request.get_json(force=True)
    ids = d.get("ids") or []
    if not ids:
        return api_err("Nothing selected.")
    c = db.conn()
    n = 0
    for i in ids:
        r = c.execute("SELECT id, status FROM emails WHERE id=?", (i,)).fetchone()
        if r and r["status"] in ("scheduled", "pending"):
            c.execute("UPDATE emails SET status='cancelled', scheduled_at=NULL, error='' WHERE id=?", (i,))
            n += 1
    c.commit()
    log("emails_cancel", "Stopped " + str(n) + " email(s)")
    c.close()
    for i in ids:
        _cloud_push_email(i, "cancelled")
    return jsonify({"ok": True, "cancelled": n})


@app.route("/api/emails/<int:eid>/reply", methods=["POST"])
@login_required
def api_email_reply(eid):
    d = request.get_json(force=True)
    body = (d.get("body") or "").strip()
    if not body:
        return api_err("Write the reply first.")
    account_id = int(d.get("account_id") or 0)
    as_draft = bool(d.get("as_draft"))
    c = db.conn()
    r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    if not r:
        c.close()
        return api_err("Email not found", 404)
    r = dict(r)
    subject = ("Re: " if not r["subject"].lower().startswith("re:") else "") + r["subject"]
    try:
        _require_account(account_id, request.user)
        if as_draft or db.get_setting("safe_mode") == "1":
            batch = c.execute("SELECT * FROM batches WHERE id=?", (r["batch_id"],)).fetchone()
            gmail_client.create_draft(account_id, r["to_addr"], subject,
                                      "<p>" + body.replace("\n", "<br>") + "</p>", None, _cc_of(dict(batch)) if batch else None)
            c.close()
            log("reply_draft", "Reply draft for email " + str(eid))
            return jsonify({"ok": True, "drafted": True})
        batch = c.execute("SELECT * FROM batches WHERE id=?", (r["batch_id"],)).fetchone()
        res = gmail_client.send_with_retry(account_id, r["to_addr"], subject,
                                           "<p>" + body.replace("\n", "<br>") + "</p>",
                                           None, _cc_of(dict(batch)) if batch else None)
        c.close()
        log("reply_sent", "Manual reply sent for email " + str(eid))
        return jsonify({"ok": True, "sent": True})
    except RuntimeError as e:
        c.close()
        return api_err(str(e), 409)
    except Exception as e:
        c.close()
        return api_err(str(e)[:200], 500)


@app.route("/api/emails/<int:eid>/reply-suggest", methods=["POST"])
@login_required
def api_email_reply_suggest(eid):
    c = db.conn()
    r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    if not r:
        c.close()
        return api_err("Email not found", 404)
    r = dict(r)
    c.close()
    if not ai_client.ai_configured():
        return api_err("Set an AI key in Settings first.", 409)
    snippet = "No reply text stored - reply politely to their message."
    try:
        draft = ai_client.suggest_reply_followup(r["body_html"][:1500], snippet)
        return jsonify({"ok": True, "draft": draft})
    except Exception as e:
        return api_err(str(e)[:200], 500)
    return jsonify({"ok": True, "deleted": len(ids)})


# ---------------- scheduler ----------------

def sends_today():
    c = db.conn()
    local_today = datetime.now().strftime("%Y-%m-%d")
    n = c.execute("SELECT COUNT(*) n FROM emails WHERE status='sent' AND sent_at >= ?",
                  (local_today + " 00:00:00",)).fetchone()["n"]
    c.close()
    return n


def replies_tick():
    """Poll inboxes for replies to sent mails (needs the readonly scope)."""
    c = db.conn()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM emails WHERE status='sent' AND message_id != '' "
        "AND (replied_at IS NULL OR replied_at = '') ORDER BY sent_at LIMIT 40").fetchall()]
    c.close()
    if not rows:
        return
    by_acct = {}
    for r in rows:
        if r["account_id"]:
            by_acct.setdefault(r["account_id"], []).append((r["id"], r["message_id"]))
    for acct, pairs in by_acct.items():
        if not db.account_token_json(acct):
            continue
        try:
            found = gmail_client.check_replies(acct, pairs)
        except Exception as e:
            db.log_activity("reply_poll_error", str(e)[:200], "scheduler")
            continue
        if not found:
            continue
        c = db.conn()
        for lid in found:
            c.execute("UPDATE emails SET replied_at=? WHERE id=?", (db.now_iso(), int(lid)))
            r0 = c.execute("SELECT contact_id FROM emails WHERE id=?", (int(lid),)).fetchone()
            if r0:
                c.execute("UPDATE contacts SET status='replied' WHERE id=?", (r0["contact_id"],))
        c.commit()
        c.close()
        for lid in found:
            db.log_activity("reply_detected", "email " + str(lid) + " replied", "scheduler")
            _cloud_push_email(int(lid))


def reminders_tick():
    """Send follow-up reminders for batches configured with reminder rules."""
    if db.get_setting("safe_mode") == "1":
        return
    now = datetime.now().astimezone()
    c = db.conn()
    pending_logs = []
    batches = [dict(r) for r in c.execute(
        "SELECT * FROM batches WHERE reminder_days > 0 AND reminder_template_id > 0").fetchall()]
    for b in batches:
        t = c.execute("SELECT * FROM email_templates WHERE id=?", (b["reminder_template_id"],)).fetchone()
        if not t:
            continue
        tpl = dict(t)
        try:
            batch_mapping = json.loads(b["mapping"] or "{}")
        except Exception:
            batch_mapping = {}
        mapping = {}
        for f in json.loads(tpl["fields"] or "[]"):
            if f in batch_mapping:
                mapping[f] = batch_mapping[f]
            else:
                sug = docx_convert.suggest_mapping([f], ["serial", "company", "hr_name", "hr_email"]).get(f, "")
                if sug:
                    mapping[f] = "column:" + sug
        due = [dict(r) for r in c.execute(
            "SELECT * FROM emails WHERE batch_id=? AND status='sent' AND is_reminder=0 "
            "AND (replied_at IS NULL OR replied_at = '') AND reminder_count < ? ORDER BY sent_at",
            (b["id"], b["reminder_max"])).fetchall()]
        for e in due:
            if not e["sent_at"] or not e["account_id"]:
                continue
            try:
                base = datetime.fromisoformat(e["sent_at"]).replace(tzinfo=now.tzinfo)
            except ValueError:
                continue
            due_at = base + timedelta(days=b["reminder_days"] * (e["reminder_count"] + 1))
            if now < due_at:
                continue
            contact = c.execute("SELECT * FROM contacts WHERE id=?", (e["contact_id"],)).fetchone()
            if not contact:
                continue
            subj, body = render_email(tpl, dict(contact), mapping)
            subj = subj.replace("{first_subject}", e["subject"])
            body = body.replace("{first_subject}", e["subject"])
            try:
                if not db.account_token_json(e["account_id"]):
                    raise RuntimeError("send account lost its login")
                gmail_client.send_with_retry(e["account_id"], e["to_addr"], subj, body,
                                              _load_attachments(b) or None, _cc_of(b))
            except Exception as ex:
                c.execute("UPDATE emails SET error=? WHERE id=?", (str(ex)[:300], e["id"]))
                pending_logs.append(("reminder_error", "serial " + str(e["serial"]) + ": " + str(ex)[:200]))
                break
            c.execute("UPDATE emails SET reminder_count=reminder_count+1 WHERE id=?", (e["id"],))
            c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, "
                      "status, sent_at, account_id, is_reminder, created_at) "
                      "VALUES(?,?,?,?,?,?, 'sent', ?, ?, 1, ?)",
                      (b["id"], e["contact_id"], e["serial"], e["to_addr"], subj, body,
                       db.now_iso(), e["account_id"], db.now_iso()))
            pending_logs.append(("reminder_sent",
                            "serial " + str(e["serial"]) + " -> " + e["to_addr"] +
                            " (reminder " + str(e["reminder_count"] + 1) + ")"))
            time.sleep(0.4)
    c.commit()
    c.close()
    for action, detail in pending_logs:
        db.log_activity(action, detail, "scheduler")




# ---------------- cloud relay sync ----------------

def _cloud_cfg():
    url = (db.get_setting("cloud_relay_url", "") or "").strip().rstrip("/")
    secret = (db.get_setting("cloud_relay_secret", "") or "").strip()
    return url, secret


def _relay_enabled():
    url, secret = _cloud_cfg()
    return bool(url and secret)


def _cloud_push_email(email_id, status=None):
    """Best-effort: mirror one email row to the cloud relay (which sends it)."""
    url, secret = _cloud_cfg()
    if not url or not secret:
        return
    c = db.conn()
    r = c.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
    if not r:
        c.close()
        return
    r = dict(r)
    batch = c.execute("SELECT * FROM batches WHERE id=?", (r["batch_id"],)).fetchone()
    cam = None
    if batch and (batch["campaign_id"] or 0):
        cam = c.execute("SELECT name FROM campaigns WHERE id=?", (batch["campaign_id"],)).fetchone()
    rem_days = rem_max = 0
    rem_subj = rem_body = ""
    if batch:
        b = dict(batch)
        if (b.get("reminder_days") or 0) > 0 and (b.get("reminder_template_id") or 0) > 0:
            t = c.execute("SELECT * FROM email_templates WHERE id=?",
                          (b["reminder_template_id"],)).fetchone()
            if t:
                rem_days, rem_max = int(b["reminder_days"]), int(b.get("reminder_max") or 1)
                try:
                    bmap = json.loads(b.get("mapping") or "{}")
                except Exception:
                    bmap = {}
                mapping = {}
                for f in json.loads(dict(t)["fields"] or "[]"):
                    if f in bmap:
                        mapping[f] = bmap[f]
                    else:
                        sug = docx_convert.suggest_mapping([f], ["serial", "company", "hr_name", "hr_email"]).get(f, "")
                        if sug:
                            mapping[f] = "column:" + sug
                contact = c.execute("SELECT * FROM contacts WHERE id=?", (r["contact_id"],)).fetchone()
                row = _row_vals(contact) if contact else {}
                rem_subj, rem_body = render_email(dict(t), row, mapping)
                rem_subj = rem_subj.replace("{first_subject}", r["subject"])
                rem_body = rem_body.replace("{first_subject}", r["subject"])
    c.close()
    # the relay has no local image files - send fully rendered HTML
    try:
        r["body_html"] = gmail_client.inline_local_images(r["body_html"])
    except Exception:
        pass
    atts = []
    if batch:
        for aname, abyte, amime in _load_attachments(dict(batch)):
            atts.append({"name": aname, "mime": amime,
                         "b64": base64.b64encode(abyte).decode("ascii")})
    payload = {"emails": [{
        "local_id": r["id"], "account_id": r["account_id"], "to_addr": r["to_addr"],
        "cc": batch["cc"] if batch else "", "subject": r["subject"],
        "body_html": r["body_html"], "attachments": atts,
        "status": status or r["status"], "scheduled_at": r["scheduled_at"],
        "batch_name": batch["name"] if batch else "",
        "campaign_name": cam["name"] if cam else "",
        "is_reminder": r["is_reminder"], "serial": r["serial"],
        "sent_at": r["sent_at"], "message_id": r["message_id"], "error": r["error"],
        "reminder_days": rem_days, "reminder_max": rem_max,
        "reminder_subject": rem_subj, "reminder_body": rem_body,
        "replied_at": r["replied_at"],
    }]}
    try:
        req = urllib.request.Request(url + "/api/emails",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "x-relay-secret": secret}, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def relay_status_pull():
    """Pull send results back from the cloud relay into the local DB."""
    url, secret = _cloud_cfg()
    if not url or not secret:
        return
    try:
        req = urllib.request.Request(url + "/api/status", headers={"x-relay-secret": secret})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
    except Exception as e:
        db.log_activity("relay_pull_error", str(e)[:150], "scheduler")
        return
    c = db.conn()
    pending_logs = []
    for e in data.get("emails", []):
        lid, st = e.get("local_id"), e.get("status")
        if not lid or st not in ("sent", "error", "scheduled", "cancelled", "drafted"):
            continue
        exists = c.execute("SELECT 1 FROM emails WHERE id=?", (lid,)).fetchone()
        if not exists and e.get("is_reminder") and st == "sent":
            parent = c.execute("SELECT * FROM emails WHERE id=?", (e.get("parent_local_id") or 0,)).fetchone()
            if parent:
                c.execute("INSERT INTO emails(id, batch_id, contact_id, serial, to_addr, subject, body_html, "
                          "status, scheduled_at, sent_at, error, account_id, message_id, "
                          "reminder_count, is_reminder, created_at) "
                          "VALUES(?, ?, ?, ?, ?, ?, ?, 'sent', ?, ?, '', ?, ?, 0, 1, ?)",
                          (lid, parent["batch_id"], parent["contact_id"],
                           e.get("serial") or parent["serial"], e.get("to_addr") or "",
                           e.get("subject") or "", e.get("body_html") or "",
                           None, e.get("sent_at") or db.now_iso(), parent["account_id"],
                           e.get("message_id") or "", db.now_iso()))
                pending_logs.append("serial " + str(parent["serial"]) + " - reminder sent from cloud")
            continue
        c.execute("UPDATE emails SET status=?, error=? WHERE id=?",
                  (st, e.get("error") or "", lid))
        if e.get("reminder_count"):
            c.execute("UPDATE emails SET reminder_count=? WHERE id=?", (int(e["reminder_count"]), lid))
        if st == "sent":
            sent_at, mid = e.get("sent_at") or None, e.get("message_id") or ""
            if mid:
                c.execute("UPDATE emails SET sent_at=COALESCE(?, sent_at), message_id=? WHERE id=?", (sent_at, mid, lid))
            elif sent_at:
                c.execute("UPDATE emails SET sent_at=COALESCE(?, sent_at) WHERE id=?", (sent_at, lid))
            c.execute("UPDATE contacts SET status='sent' WHERE id=(SELECT contact_id FROM emails WHERE id=?)", (lid,))
    c.commit()
    c.close()
    for detail in pending_logs:
        db.log_activity("reminder_sent_cloud", detail, "scheduler")


def scheduler_tick():
    SCHEDULER_STATE["last_tick"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if db.get_setting("safe_mode") == "1":
        return
    if _relay_enabled():
        relay_status_pull()
        return

    now = datetime.now().astimezone()
    now_iso_str = now.isoformat()
    c = db.conn()
    due = c.execute("SELECT * FROM emails WHERE status='scheduled' AND scheduled_at <= ? "
                    "ORDER BY scheduled_at LIMIT 500", (now_iso_str,)).fetchall()
    missed_ids = set()
    if db.get_setting("catchup_policy") == "mark_missed":
        for r in due:
            try:
                due_at = datetime.fromisoformat(r["scheduled_at"])
                if (now - due_at) > timedelta(minutes=30):
                    c.execute("UPDATE emails SET status='error', error='Missed send window (platform was off)' WHERE id=?", (r["id"],))
                    missed_ids.add(r["id"])
            except ValueError:
                pass
        if missed_ids:
            c.commit()
            db.log_activity("sends_missed", str(len(missed_ids)) + " email(s) past their window", "scheduler")
    c.close()
    if missed_ids:
        due = [r for r in due if r["id"] not in missed_ids]
    if not due:
        return
    cap = int(db.get_setting("max_sends_per_day") or 100)
    sent = 0
    c = db.conn()
    for r in due:
        if db.get_setting("safe_mode") == "1":
            break
        if sent >= cap - sends_today():
            break
        try:
            if not r["account_id"]:
                raise RuntimeError("no send account assigned")
            if not db.account_token_json(r["account_id"]):
                raise RuntimeError("send account lost its login")
            batch = c.execute("SELECT * FROM batches WHERE id=?", (r["batch_id"],)).fetchone()
            attach = _load_attachments(dict(batch)) if batch else []
            res = gmail_client.send_with_retry(r["account_id"], r["to_addr"], r["subject"], r["body_html"],
                                               attach or None, _cc_of(dict(batch)))
            mid = res.get("message_id", "") if isinstance(res, dict) else ""
            c.execute("UPDATE emails SET status='sent', sent_at=?, error='', message_id=? WHERE id=?",
                      (db.now_iso(), mid, r["id"]))
            c.execute("UPDATE contacts SET status='sent' WHERE id=?", (r["contact_id"],))
            db.log_activity("scheduler_sent", "serial " + r["serial"] + " -> " + r["to_addr"], "scheduler")
            sent += 1
        except Exception as e:
            c.execute("UPDATE emails SET status='error', error=? WHERE id=?", (str(e)[:300], r["id"]))
            db.log_activity("scheduler_error", "serial " + str(r["serial"]) + ": " + str(e)[:200], "scheduler")
            SCHEDULER_STATE["last_error"] = str(e)[:200]
            break
        time.sleep(0.4)
    c.commit()
    c.close()
    TICK_COUNTER["n"] += 1
    try:
        reminders_tick()
    except Exception as e:
        SCHEDULER_STATE["last_error"] = str(e)[:200]
    if TICK_COUNTER["n"] % 5 == 0:
        try:
            replies_tick()
        except Exception as e:
            SCHEDULER_STATE["last_error"] = str(e)[:200]


def scheduler_loop():
    while SCHEDULER_STATE["running"]:
        try:
            scheduler_tick()
        except Exception as e:
            SCHEDULER_STATE["last_error"] = str(e)[:200]
        time.sleep(8)


@app.route("/api/scheduler/trigger", methods=["POST"])
@login_required
def api_scheduler_trigger():
    try:
        scheduler_tick()
        return jsonify({"ok": True, "triggered": True})
    except Exception as e:
        return api_err(str(e))


# ---------------- settings ----------------

@app.route("/api/settings", methods=["GET"])
@login_required
def api_settings_get():
    s = db.all_settings()
    s.pop("ai_api_key", None)
    s.pop("app_secret", None)
    s["ai_has_key"] = "1" if db.get_setting("ai_api_key") else "0"
    s["client_secret_set"] = "1" if gmail_client.oauth_ready() else "0"
    s["google_ready"] = "1" if google_login_ready() else "0"
    return jsonify({"ok": True, "settings": s})


@app.route("/api/settings/relay-pack")
@login_required
def api_settings_relay_pack():
    """One-click pack of the 5 values needed to configure the Supabase relay."""
    if request.user["role"] != "admin":
        return api_err("Only the admin can view the relay setup pack.", 403)
    account_id = int(request.args.get("account_id") or 0)
    c = db.conn()
    acc = c.execute("SELECT * FROM gmail_accounts WHERE id=?", (account_id,)).fetchone()
    c.close()
    if not acc:
        return api_err("Pick a connected Gmail account first.", 409)
    acc = dict(acc)
    secret = (db.get_setting("cloud_relay_secret", "") or "").strip()
    if not secret:
        import secrets as _s
        secret = _s.token_urlsafe(32)
        db.set_setting("cloud_relay_secret", secret)
        log("relay", "Relay secret generated")
    tok = {}
    try:
        tok = json.loads(db.account_token_json(acc["id"]) or "{}")
    except Exception:
        tok = {}
    if not tok.get("refresh_token"):
        return api_err("This account has no refresh token saved. Reconnect the Gmail account first.", 409)
    if not (db.get_setting("google_client_id", "") or "").strip():
        return api_err("Paste the Gmail client JSON in Settings first (needed for GMAIL_CLIENT_ID / SECRET).", 409)
    return jsonify({"ok": True, "pack": {
        "RELAY_SECRET": secret,
        "GMAIL_CLIENT_ID": db.get_setting("google_client_id", "").strip(),
        "GMAIL_CLIENT_SECRET": db.get_setting("google_client_secret", "").strip(),
        "GMAIL_SENDER_EMAIL": acc["email"] or "",
        "GMAIL_REFRESH_TOKEN": tok.get("refresh_token", ""),
    }})


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings_post():
    d = request.get_json(force=True)
    u = request.user
    for k in ("safe_mode", "max_sends_per_day", "default_stagger_min", "catchup_policy",
              "ai_base_url", "ai_model", "ai_enabled", "cloud_relay_url"):
        if k in d:
            db.set_setting(k, d[k])
    for k in ("ai_api_key", "google_client_id", "google_client_secret", "client_secret_json",
              "admin_email", "cloud_relay_secret", "brand_team_name", "brand_tagline"):
        if k in d and u["role"] == "admin":
            db.set_setting(k, d[k])
    log("settings", "Settings updated")
    return jsonify({"ok": True})


@app.route("/api/ai/test", methods=["POST"])
@login_required
def api_ai_test():
    return jsonify(ai_client.test_connection())


@app.route("/api/ai/subjects", methods=["POST"])
@login_required
def api_ai_subjects():
    if not ai_client.ai_configured():
        return api_err("AI is not configured. Add the API key in Settings first.", 409)
    d = request.get_json(force=True)
    try:
        subjects = ai_client.improve_subjects(d.get("subject", ""), d.get("context", ""), int(d.get("n") or 3))
    except Exception as e:
        return api_err("AI request failed: " + str(e), 502)
    return jsonify({"ok": True, "subjects": subjects})


@app.route("/api/ai/mapping", methods=["POST"])
@login_required
def api_ai_mapping():
    if not ai_client.ai_configured():
        return api_err("AI is not configured. Add the API key in Settings first.", 409)
    d = request.get_json(force=True)
    try:
        mapping = ai_client.suggest_mapping(d.get("fields", []), d.get("columns", []))
    except Exception as e:
        return api_err("AI request failed: " + str(e), 502)
    return jsonify({"ok": True, "mapping": mapping})


# ---------------- AI assistant (chat + agentic) ----------------

AI_TOOLS = [
    {"type": "function", "function": {"name": "list_campaigns",
     "description": "List all campaigns with ids and names.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "create_campaign",
     "description": "Create a new campaign to group outreach work.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"}, "description": {"type": "string"}},
         "required": ["name"]}}},
    {"type": "function", "function": {"name": "list_templates",
     "description": "List email templates with id, name, subject and replaceable fields.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_template",
     "description": "Get one template's full subject and body HTML.",
     "parameters": {"type": "object", "properties": {
         "id": {"type": "integer"}}, "required": ["id"]}}},
    {"type": "function", "function": {"name": "create_template",
     "description": "Create a new email template. body_html MUST be real HTML markup (for example '<p>Dear {recruiter_name}, Thank you ...</p>'), never a plain-text description of the body. Per-recipient values use {field} placeholders like {recruiter_name}. Use inline styles only.",
     "parameters": {"type": "object", "properties": {
         "name": {"type": "string"}, "subject": {"type": "string"}, "body_html": {"type": "string"}},
         "required": ["name", "subject", "body_html"]}}},
    {"type": "function", "function": {"name": "list_sheets",
     "description": "List uploaded data sheets with ids, columns and row counts.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "queue_batch",
     "description": "Generate a batch of emails from a template + data (sheet_id, or blank for the main contact list). action: 'queue' (hold), 'draft' (Gmail drafts) or 'schedule' (automatic send at 'at', format YYYY-MM-DD HH:MM local, at least 30 min ahead). Never sends immediately.",
     "parameters": {"type": "object", "properties": {
         "campaign_id": {"type": "integer"}, "template_id": {"type": "integer"},
         "sheet_id": {"type": "integer"}, "serial_from": {"type": "integer"}, "serial_to": {"type": "integer"},
         "action": {"type": "string", "enum": ["queue", "draft", "schedule"]},
         "at": {"type": "string"}, "stagger": {"type": "integer"}, "name": {"type": "string"}},
         "required": ["template_id", "action"]}}},
    {"type": "function", "function": {"name": "list_emails",
     "description": "List recent queue emails with id, to, subject and status.",
     "parameters": {"type": "object", "properties": {
         "status": {"type": "string"}, "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "cancel_emails",
     "description": "Stop queue/scheduled emails so they will not be sent.",
     "parameters": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]}}},
    {"type": "function", "function": {"name": "reschedule_emails",
     "description": "Move queue/scheduled emails to a new time (YYYY-MM-DD HH:MM local, at least 30 min ahead).",
     "parameters": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "integer"}},
         "at": {"type": "string"}, "stagger": {"type": "integer"}},
         "required": ["ids", "at"]}}},
]


def _assistant_context():
    c = db.conn()
    camps = [dict(r) for r in c.execute("SELECT * FROM campaigns ORDER BY id").fetchall()]
    tpls = [dict(r) for r in c.execute("SELECT * FROM email_templates ORDER BY id").fetchall()]
    sheets = [dict(r) for r in c.execute("SELECT * FROM sheets ORDER BY id").fetchall()]
    all_accts = [dict(r) for r in c.execute("SELECT * FROM gmail_accounts").fetchall()]
    usable = [a for a in all_accts if db.account_for(request.user["id"], a["id"]) and db.account_token_json(a["id"])]
    st = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status").fetchall()}
    c.close()
    safe = db.get_setting("safe_mode") == "1"
    return "\n".join([
        "You are the built-in assistant of CampusMail AI, a CRM for the outreach team.",
        "You help by chatting and by doing safe actions directly in the platform: creating campaigns and templates, "
        "planning batches (queue / Gmail drafts / scheduled sends), stopping or rescheduling emails, and reporting status.",
        "Hard rules: you can NEVER send emails immediately and must never claim you sent something. If the user asks to "
        "send now, explain that live sending is a manual step in Queue and Schedule (and needs Safe mode OFF).",
        "If an action is missing a detail (which sheet, which time, which template, how many rows), ask ONE short "
        "question instead of guessing.",
        "Be concise and friendly. After doing something, state exactly what was done (names, counts, times).",
        "",
        "Current context:",
        "- Signed in: " + request.user["email"] + " (" + (request.user.get("role") or "user") + ")",
        "- Safe mode: " + ("ON (only queue/draft/schedule possible)" if safe else "OFF"),
        "- Gmail send accounts available: " + (", ".join(a["email"] + " (id " + str(a["id"]) + ")" for a in usable) or "none"),
        "- Campaigns: " + (", ".join(str(x["id"]) + " " + x["name"] for x in camps) or "none"),
        "- Templates: " + (", ".join(str(x["id"]) + " " + x["name"] + " [" + ", ".join(json.loads(x["fields"] or "[]")) + "]" for x in tpls) or "none"),
        "- Data sheets: " + (", ".join(str(x["id"]) + " " + x["name"] + " (" + str(x["n_rows"]) + " rows: " + ", ".join(json.loads(x["headers"] or "[]")) + ")" for x in sheets) or "none"),
        "- Email queue counts: " + (", ".join(k + "=" + str(v) for k, v in sorted(st.items())) or "empty"),
        "- Local date/time: " + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
    ])


def _exec_tool(name, a):
    c = db.conn()
    try:
        if name == "list_campaigns":
            return {"ok": True, "campaigns": [
                {"id": x["id"], "name": x["name"]} for x in c.execute("SELECT id, name FROM campaigns ORDER BY id").fetchall()]}
        if name == "create_campaign":
            nm = (a.get("name") or "").strip()
            if not nm:
                return {"ok": False, "error": "name required"}
            c.execute("INSERT INTO campaigns(name, description, created_by, created_at) VALUES(?,?,?,?)",
                      (nm, (a.get("description") or "").strip(), request.user["email"], db.now_iso()))
            cid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            c.commit()
            db.log_activity("campaign_create", nm + " (by AI assistant)", "ai")
            return {"ok": True, "id": cid, "name": nm}
        if name == "list_templates":
            return {"ok": True, "templates": [
                {"id": x["id"], "name": x["name"], "subject": x["subject"],
                 "fields": json.loads(x["fields"] or "[]"), "active": bool(x["is_active"])}
                for x in c.execute("SELECT * FROM email_templates ORDER BY id").fetchall()]}
        if name == "get_template":
            x = c.execute("SELECT * FROM email_templates WHERE id=?", (int(a.get("id") or 0),)).fetchone()
            if not x:
                return {"ok": False, "error": "template not found"}
            return {"ok": True, "id": x["id"], "name": x["name"], "subject": x["subject"], "body_html": x["body_html"]}
        if name == "create_template":
            nm = (a.get("name") or "AI template " + datetime.now().strftime("%Y-%m-%d %H:%M")).strip()
            subj = (a.get("subject") or "").strip()
            body = (a.get("body_html") or "").strip()
            if not subj or not body:
                return {"ok": False, "error": "subject and body_html are required"}
            fields = all_fields(subj, body)
            c.execute("INSERT INTO email_templates(name, type, subject, body_html, fields, is_active) VALUES(?,?,?,?,?,0)",
                      (nm, "html", subj, body, json.dumps(fields)))
            tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            c.commit()
            db.log_activity("template_add", nm + " (by AI assistant)", "ai")
            return {"ok": True, "id": tid, "name": nm, "fields": fields}
        if name == "list_sheets":
            return {"ok": True, "sheets": [
                {"id": x["id"], "name": x["name"], "n_rows": x["n_rows"],
                 "columns": json.loads(x["headers"] or "[]")}
                for x in c.execute("SELECT * FROM sheets ORDER BY id").fetchall()]}
        if name == "queue_batch":
            t = c.execute("SELECT * FROM email_templates WHERE id=?", (int(a.get("template_id") or 0),)).fetchone()
            if not t:
                return {"ok": False, "error": "template not found"}
            if int(a.get("sheet_id") or 0):
                sh = c.execute("SELECT * FROM sheets WHERE id=?", (int(a["sheet_id"]),)).fetchone()
                if not sh:
                    return {"ok": False, "error": "sheet not found"}
                cols = json.loads(sh["headers"] or "[]")
            else:
                cols = ["serial", "company", "hr_name", "hr_email"]
            mapping = {k: "column:" + v for k, v in
                       docx_convert.suggest_mapping(json.loads(t["fields"] or "[]"), cols).items() if v}
            acct = 0
            if a.get("action") in ("draft", "schedule"):
                last = last_account_for(request.user["id"])
                if db.account_for(request.user["id"], last) and db.account_token_json(last):
                    acct = last
                else:
                    for x in c.execute("SELECT id FROM gmail_accounts ORDER BY id").fetchall():
                        if db.account_for(request.user["id"], x["id"]) and db.account_token_json(x["id"]):
                            acct = x["id"]
                            break
                if not acct:
                    return {"ok": False, "error": "no connected Gmail send account available"}
            d = {
                "template_id": t["id"], "action": a.get("action") or "queue",
                "sheet_id": int(a.get("sheet_id") or 0), "campaign_id": int(a.get("campaign_id") or 0),
                "serial_from": a.get("serial_from"), "serial_to": a.get("serial_to"),
                "at": a.get("at") or "", "stagger": int(a.get("stagger") or 0),
                "name": (a.get("name") or "").strip() or "AI batch " + datetime.now().strftime("%Y-%m-%d %H:%M"),
                "mapping": mapping, "account_id": acct,
            }
            res = _run_merge(d)
            if res.status_code != 200:
                return {"ok": False, "error": (res.get_json() or {}).get("error", "merge failed")}
            out = res.get_json() or {}
            out.pop("ok", None)
            return out
        if name == "list_emails":
            rows = _email_rows({"status": a.get("status") or "", "limit": min(int(a.get("limit") or 20), 50)})
            return {"ok": True, "emails": [
                {"id": r["id"], "serial": r["serial"], "to": r["to_addr"], "subject": r["subject"],
                 "status": r["status"], "scheduled_at": r["scheduled_at"], "sent_at": r["sent_at"]} for r in rows]}
        if name == "cancel_emails":
            ids = [int(i) for i in (a.get("ids") or [])][:200]
            if not ids:
                return {"ok": False, "error": "no ids given"}
            n = 0
            for i in ids:
                r = c.execute("SELECT id, status FROM emails WHERE id=?", (i,)).fetchone()
                if r and r["status"] in ("scheduled", "pending"):
                    c.execute("UPDATE emails SET status='cancelled', scheduled_at=NULL, error='' WHERE id=?", (i,))
                    n += 1
            c.commit()
            db.log_activity("emails_cancel", "Stopped " + str(n) + " (by AI assistant)", "ai")
            for i in ids:
                _cloud_push_email(i, "cancelled")
            return {"ok": True, "cancelled": n}
        if name == "reschedule_emails":
            ids = [int(i) for i in (a.get("ids") or [])][:200]
            when_str = (a.get("at") or "").strip()
            if not ids or not when_str:
                return {"ok": False, "error": "ids and at are required"}
            try:
                when = datetime.strptime(when_str, "%Y-%m-%d %H:%M").replace(tzinfo=datetime.now().astimezone().tzinfo)
            except ValueError:
                return {"ok": False, "error": "time format must be YYYY-MM-DD HH:MM"}
            now = datetime.now().astimezone()
            if when < now + timedelta(minutes=30):
                return {"ok": False, "error": "time must be at least 30 minutes in the future"}
            if when > now + timedelta(days=45):
                return {"ok": False, "error": "up to 45 days ahead only"}
            stagger = int(a.get("stagger") or 0)
            n = 0
            for i, eid in enumerate(ids):
                r = c.execute("SELECT id, status FROM emails WHERE id=?", (eid,)).fetchone()
                if r and r["status"] in ("scheduled", "pending"):
                    t = when + timedelta(minutes=stagger * i)
                    c.execute("UPDATE emails SET status='scheduled', scheduled_at=?, error='' WHERE id=?", (t.isoformat(), eid))
                    n += 1
            c.commit()
            for eid in ids:
                _cloud_push_email(eid, "scheduled")
            db.log_activity("schedule", "AI rescheduled " + str(n) + " to " + when_str, "ai")
            return {"ok": True, "rescheduled": n, "first": when.isoformat()}
        return {"ok": False, "error": "unknown tool " + name}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    finally:
        c.close()


@app.route("/api/ai/chat", methods=["POST"])
@login_required
def api_ai_chat():
    if not ai_client.ai_configured():
        return api_err("Set an AI key in Settings first.", 409)
    d = request.get_json(force=True)
    msgs = d.get("messages") or []
    if not msgs:
        return api_err("Empty conversation.")
    full = [{"role": "system", "content": _assistant_context()}]
    for m in msgs[-20:]:
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip():
            full.append({"role": m["role"], "content": m["content"]})
    events, reply = [], ""
    try:
        for _step in range(5):
            msg = ai_client.chat_raw(full, max_tokens=1500, temperature=0.4, tools=AI_TOOLS)
            tcs = msg.get("tool_calls") or []
            if not tcs:
                reply = (msg.get("content") or "").strip()
                break
            full.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
            for tc in tcs:
                fn = tc.get("function", {})
                nm = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                res = _exec_tool(nm, args)
                events.append({"tool": nm, "result": res})
                db.log_activity("ai_tool", nm + " -> " + json.dumps(res)[:140], "ai")
                full.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": json.dumps(res)[:4000]})
        else:
            reply = reply or "Done - the actions I took are listed above the message."
    except Exception as e:
        if events:
            return jsonify({"ok": True, "reply": "The action was carried out (see the action chips above), but I "
                                    "could not generate my summary. " + str(e)[:120], "events": events})
        return api_err("AI request failed: " + str(e)[:200], 502)
    return jsonify({"ok": True, "reply": reply, "events": events})


# ---------------- activity ----------------

@app.route("/api/activity")
@login_required
def api_activity():
    c = db.conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 300").fetchall()]
    c.close()
    return jsonify({"ok": True, "rows": rows})


# ---------------- signature ----------------

@app.route("/api/signature")
@login_required
def api_signature_get():
    return jsonify({"ok": True, "html": db.get_setting("signature_html", "")})


@app.route("/api/signature", methods=["POST"])
@login_required
def api_signature_set():
    d = request.get_json(force=True)
    db.set_setting("signature_html", (d.get("html") or "").strip())
    log("signature_save", "")
    return jsonify({"ok": True})


@app.route("/api/signature/upload", methods=["POST"])
@login_required
def api_signature_upload():
    f = request.files.get("file")
    if not f:
        return api_err("No file uploaded.")
    ext = Path(f.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return api_err("Upload a PNG, JPG, GIF or WebP image.")
    data = f.read()
    if len(data) > 4 * 1024 * 1024:
        return api_err("Image must be under 4 MB.")
    name = "sig_" + datetime.now().strftime("%Y%m%d%H%M%S") + ext
    (BASE / "template_images").mkdir(exist_ok=True)
    (BASE / "template_images" / name).write_bytes(data)
    log("signature_image", name)
    return jsonify({"ok": True, "html": '<img src="local:' + name + '" alt="signature" style="max-width:220px;">'})


# ---------------- batch attachments ----------------

def _load_attachments(batch):
    if not batch:
        return []
    try:
        names = json.loads(batch.get("attachments") or "[]")
    except Exception:
        names = []
    out = []
    for name in names:
        safe = re.sub(r"[^A-Za-z0-9._\-]", "_", str(name))
        f = ATTACH_DIR / safe
        if f.exists():
            ctype = mimetypes.guess_type(safe)[0] or "application/octet-stream"
            out.append((safe, f.read_bytes(), ctype))
    return out


@app.route("/api/batches/<int:bid>/attachments", methods=["POST"])
@login_required
def api_batch_attachments_add(bid):
    c = db.conn()
    b = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    c.close()
    if not b:
        return api_err("Batch not found", 404)
    b = dict(b)
    try:
        names = json.loads(b.get("attachments") or "[]")
    except Exception:
        names = []
    files = request.files.getlist("files")
    if not files:
        return api_err("No files uploaded.")
    if len(names) + len(files) > MAX_ATTACH_PER_BATCH:
        return api_err("Maximum " + str(MAX_ATTACH_PER_BATCH) + " attachments per batch.")
    ATTACH_DIR.mkdir(exist_ok=True)
    existing = sum((ATTACH_DIR / n).stat().st_size for n in names if (ATTACH_DIR / n).exists())
    added = 0
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_ATTACH_EXT:
            return api_err("File type not allowed: " + f.filename)
        data = f.read()
        existing += len(data)
        if existing > MAX_ATTACH_TOTAL:
            return api_err("Total attachments must stay under 15 MB per batch.")
        safe = "b" + str(bid) + "_" + re.sub(r"[^A-Za-z0-9._\-]", "_", Path(f.filename).name)
        (ATTACH_DIR / safe).write_bytes(data)
        names.append(safe)
        added += 1
    c = db.conn()
    c.execute("UPDATE batches SET attachments=? WHERE id=?", (json.dumps(names), bid))
    c.commit()
    c.close()
    log("batch_attachments", "batch " + str(bid) + ": +" + str(added) + " file(s)")
    return jsonify({"ok": True, "count": len(names)})


@app.route("/api/batches/<int:bid>/attachments", methods=["DELETE"])
@login_required
def api_batch_attachments_remove(bid):
    d = request.get_json(force=True)
    name = re.sub(r"[^A-Za-z0-9._\-]", "_", str(d.get("name") or ""))
    c = db.conn()
    b = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    c.close()
    if not b:
        return api_err("Batch not found", 404)
    try:
        names = json.loads(b["attachments"] or "[]")
    except Exception:
        names = []
    if name in names:
        names.remove(name)
        f = ATTACH_DIR / name
        if f.exists():
            f.unlink()
    c = db.conn()
    c.execute("UPDATE batches SET attachments=? WHERE id=?", (json.dumps(names), bid))
    c.commit()
    c.close()
    log("batch_attachments", "batch " + str(bid) + ": removed " + name)
    return jsonify({"ok": True, "count": len(names)})


@app.route("/api/batches/<int:bid>")
@login_required
def api_batch_get(bid):
    c = db.conn()
    b = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    c.close()
    if not b:
        return api_err("Batch not found", 404)
    b = dict(b)
    try:
        b["attachments_list"] = json.loads(b.get("attachments") or "[]")
    except Exception:
        b["attachments_list"] = []
    return jsonify({"ok": True, "batch": b})


# ---------------- batch reminders ----------------

@app.route("/api/batches/<int:bid>/reminders", methods=["POST"])
@login_required
def api_batch_reminders(bid):
    d = request.get_json(force=True)
    days = int(d.get("days") or 0)
    maxr = max(1, min(5, int(d.get("max") or 1)))
    tid = int(d.get("template_id") or 0)
    if days and not (0 < days <= 90):
        return api_err("Reminder delay must be 1-90 days.")
    if tid:
        c = db.conn()
        t = c.execute("SELECT id FROM email_templates WHERE id=?", (tid,)).fetchone()
        c.close()
        if not t:
            return api_err("Reminder template not found.", 404)
    c = db.conn()
    b = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    c.close()
    if not b:
        return api_err("Batch not found", 404)
    c = db.conn()
    c.execute("UPDATE batches SET reminder_days=?, reminder_template_id=?, reminder_max=? WHERE id=?",
              (days, tid, maxr, bid))
    c.commit()
    c.close()
    log("batch_reminders", "batch " + str(bid) + ": every " + str(days) + " day(s), max " + str(maxr))
    return jsonify({"ok": True})


# ---------------- outreach reports ----------------

@app.route("/reports")
@login_required
def page_reports():
    return render_template("reports.html", page="reports")


def _reports_rows(status="", q=""):
    c = db.conn()
    sql = ("SELECT e.id, e.serial, e.to_addr, e.subject, e.status, e.sent_at, e.scheduled_at, "
           "e.replied_at, e.reminder_count, e.is_reminder, e.error, e.batch_id, "
           "co.company, co.hr_name FROM emails e LEFT JOIN contacts co ON co.id=e.contact_id "
           "WHERE e.status != 'deleted'")
    args = []
    if status == "replied":
        sql += " AND e.replied_at IS NOT NULL AND e.replied_at != ''"
    elif status:
        sql += " AND e.status = ?"
        args.append(status)
    if q:
        sql += " AND (e.to_addr LIKE ? OR e.subject LIKE ? OR co.company LIKE ? OR co.hr_name LIKE ?)"
        args += ["%" + q + "%"] * 4
    sql += " ORDER BY e.id DESC LIMIT 500"
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    return rows


@app.route("/api/reports/emails")
@login_required
def api_reports_emails():
    rows = _reports_rows(request.args.get("status") or "", (request.args.get("q") or "").strip())
    return jsonify({"ok": True, "rows": rows})


@app.route("/api/reports/emails.csv")
@login_required
def api_reports_csv():
    import io
    rows = _reports_rows(request.args.get("status") or "", (request.args.get("q") or "").strip())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Serial", "Company", "HR Name", "To", "Subject", "Status",
                "Scheduled", "Sent", "Replied", "Reminders", "Is Reminder", "Error"])
    for r in rows:
        w.writerow([r["id"], r["serial"], r["company"] or "", r["hr_name"] or "", r["to_addr"],
                    r["subject"], r["status"], r["scheduled_at"] or "", r["sent_at"] or "",
                    r["replied_at"] or "", r["reminder_count"], "yes" if r["is_reminder"] else "", r["error"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=campusmail_ai_outreach.csv"})


@app.route("/api/reports/summary")
@login_required
def api_reports_summary():
    c = db.conn()
    contacts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status").fetchall()}
    emails = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM emails WHERE is_reminder=0 GROUP BY status").fetchall()}
    reminders = c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=1").fetchone()["n"]
    sent_n = c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent'").fetchone()["n"]
    replied_n = c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent' "
                          "AND replied_at IS NOT NULL AND replied_at != ''").fetchone()["n"]
    days = []
    for i in range(13, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        n = c.execute("SELECT COUNT(*) n FROM emails WHERE status='sent' AND sent_at >= ? AND sent_at < ?",
                      (d + " 00:00:00", d + " 23:59:59")).fetchone()["n"]
        days.append({"date": d[5:], "n": n})
    companies = [dict(r) for r in c.execute(
        "SELECT co.company, COUNT(*) total, "
        "SUM(CASE WHEN e.status='sent' THEN 1 ELSE 0 END) sent, "
        "SUM(CASE WHEN e.replied_at IS NOT NULL AND e.replied_at != '' THEN 1 ELSE 0 END) replied "
        "FROM emails e JOIN contacts co ON co.id = e.contact_id "
        "WHERE e.is_reminder=0 GROUP BY co.company ORDER BY total DESC LIMIT 12").fetchall()]
    c.close()
    reply_rate = round(100.0 * replied_n / sent_n) if sent_n else 0
    return jsonify({"ok": True, "contacts": contacts, "emails": emails, "reminders": reminders,
                    "sent": sent_n, "replied": replied_n, "reply_rate": reply_rate,
                    "days": days, "companies": companies})


# ---------------- email updates: polish / mark replied ----------------

@app.route("/api/emails/<int:eid>/polish", methods=["POST"])
@login_required
def api_email_polish(eid):
    if not ai_client.ai_configured():
        return api_err("AI is not configured. Add the API key in Settings first.", 409)
    d = request.get_json(silent=True) or {}
    c = db.conn()
    r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    c.close()
    if not r:
        return api_err("Email not found", 404)
    try:
        subj, body = ai_client.polish_email(r["subject"], r["body_html"], d.get("instructions", ""))
    except Exception as e:
        return api_err("AI request failed: " + str(e), 502)
    log("email_ai_polish", "email " + str(eid))
    return jsonify({"ok": True, "subject": subj, "body": body})


@app.route("/api/emails/<int:eid>", methods=["POST"])
@login_required
def api_email_update(eid):
    d = request.get_json(force=True)
    c = db.conn()
    sets, args = [], []
    if "subject" in d:
        sets.append("subject = ?")
        args.append(d["subject"])
    if "body_html" in d:
        sets.append("body_html = ?")
        args.append(d["body_html"])
    if sets:
        args.append(eid)
        c.execute("UPDATE emails SET " + ", ".join(sets) + " WHERE id=?", args)
        c.commit()
    c.close()
    log("email_update", "email " + str(eid))
    return jsonify({"ok": True})


@app.route("/api/emails/<int:eid>/mark-replied", methods=["POST"])
@login_required
def api_email_mark_replied(eid):
    d = request.get_json(force=True)
    on = bool(d.get("replied"))
    c = db.conn()
    r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    if not r:
        c.close()
        return api_err("Email not found", 404)
    c.execute("UPDATE emails SET replied_at=? WHERE id=?", (db.now_iso() if on else None, eid))
    if on:
        c.execute("UPDATE contacts SET status='replied' WHERE id=?", (r["contact_id"],))
    c.commit()
    c.close()
    log("mark_replied", "email " + str(eid) + (" marked replied" if on else " marked not replied"))
    _cloud_push_email(eid)
    return jsonify({"ok": True})


# ---------------- AI template helpers ----------------

@app.route("/api/templates/<int:tid>/polish", methods=["POST"])
@login_required
def api_template_polish(tid):
    if not ai_client.ai_configured():
        return api_err("AI is not configured. Add the API key in Settings first.", 409)
    c = db.conn()
    tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    c.close()
    if not tpl:
        return api_err("Template not found", 404)
    try:
        subj, body = ai_client.polish_email(tpl["subject"], tpl["body_html"])
    except Exception as e:
        return api_err("AI request failed: " + str(e), 502)
    fields = docx_convert.detect_fields(body)
    c = db.conn()
    c.execute("UPDATE email_templates SET subject=?, body_html=?, fields=? WHERE id=?",
              (subj, body, json.dumps(fields), tid))
    c.commit()
    c.close()
    log("template_ai_polish", "Template " + str(tid) + " polished")
    return jsonify({"ok": True, "subject": subj, "fields": fields})


@app.route("/api/templates/ai-reminder", methods=["POST"])
@login_required
def api_template_ai_reminder():
    if not ai_client.ai_configured():
        return api_err("AI is not configured. Add the API key in Settings first.", 409)
    d = request.get_json(force=True)
    try:
        subj, body = ai_client.write_reminder(d.get("context", ""), d.get("tone", "professional"))
    except Exception as e:
        return api_err("AI request failed: " + str(e), 502)
    fields = docx_convert.detect_fields(body)
    name = "AI reminder - " + datetime.now().strftime("%Y-%m-%d %H:%M")
    c = db.conn()
    c.execute("INSERT INTO email_templates(name, type, subject, body_html, fields, is_active) VALUES(?,?,?,?,?,0)",
              (name, "ai", subj, docx_convert.html_template_body(body), json.dumps(fields)))
    tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    c.close()
    log("template_ai_reminder", "AI reminder template " + str(tid))
    return jsonify({"ok": True, "id": tid, "subject": subj, "fields": fields})



def main():
    db.init_db()
    app.secret_key = db.app_secret()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print()
    print("  CampusMail AI - outreach team")
    print("    http://localhost:5001")
    print()
    app.run(host="127.0.0.1", port=int(os.environ.get("CAMPUSMAIL_AI_PORT", "5001")), debug=False, threaded=True)


if __name__ == "__main__":
    main()
