"""CampusMail AI - recruiter outreach control center for the outreach team.

Google login at the front door; team roles and access requests; multiple
Gmail send-accounts (private or shared); mail merge from Word/HTML templates;
local scheduler worker.  Run:  python app.py  ->  http://localhost:5001
"""
import base64
import csv
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   send_from_directory, session)

import ai_client
import db
import docx_convert
import filestore
import gmail_client
import services
import cards as cards_mod
import uuid

BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / "uploads"
ATTACH_DIR = BASE / "data" / "attachments"
WORKSPACES_DIR = BASE / "data" / "workspaces"
_TEAM_DIRS = {"uploads": UPLOAD_DIR, "attach": ATTACH_DIR, "images": BASE / "template_images"}
_TENANT_DIRS = ContextVar("campusmail_tenant_dirs", default=None)
_RESOLVED_USER = ContextVar("campusmail_resolved_user", default=None)


def _tenant_dirs():
    return _TENANT_DIRS.get() or _TEAM_DIRS


def upload_dir():
    return _tenant_dirs()["uploads"]


def attach_dir():
    return _tenant_dirs()["attach"]


def images_dir():
    return _tenant_dirs()["images"]


def workspace_key(email):
    return hashlib.sha1(email.strip().lower().encode("utf-8")).hexdigest()[:16]


def _personal_paths(email):
    key = workspace_key(email)
    return WORKSPACES_DIR / (key + ".db"), WORKSPACES_DIR / key


def _enter_personal_tenant(email, display_name, team_status):
    db_path, root = _personal_paths(email)
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("uploads", "attachments", "images"):
        (root / sub).mkdir(exist_ok=True)
    key = workspace_key(email)
    db.set_tenant_db(db_path)
    filestore.set_tenant(root / "attachments", root / "images", "ws/" + key)
    if not db_path.exists():
        db.init_db(seed=False)
    pu = db.get_user_by_email(email)
    if not pu:
        pu = db.upsert_google_user(email, display_name or email)
    if pu["role"] != "admin":
        db.set_user_role(pu["id"], "admin")
    if pu["status"] != "active":
        db.set_user_status(pu["id"], "active")
    pu = db.get_user_by_email(email)
    _TENANT_DIRS.set({"uploads": root / "uploads", "attach": root / "attachments",
                      "images": root / "images"})
    docx_convert.set_img_dir(root / "images")
    pu = dict(pu)
    pu["personal_workspace"] = True
    pu["team_status"] = team_status
    _RESOLVED_USER.set(pu)
ALLOWED_ATTACH_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".png",
                      ".jpg", ".jpeg", ".zip", ".ppt", ".pptx", ".html"}
MAX_ATTACH_PER_BATCH = 10
MAX_ATTACH_TOTAL = 15 * 1024 * 1024
app = Flask(__name__, template_folder=str(BASE / "ui"), static_folder=str(BASE / "static"))
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
app.secret_key = "boot"
ON_PA = str(BASE).startswith("/home/")  # PythonAnywhere homes are /home/<user>


@app.before_request
def bind_tenant():
    # every request starts on the team DB + team files; non-team members switch to their own
    db.set_tenant_db(db.DB_PATH)
    filestore.reset_tenant()
    uid = session.get("uid")
    if not uid:
        return
    u = db.get_user(uid)
    if not u:
        session.pop("uid", None)
        return
    if u["status"] != "active":
        _enter_personal_tenant(u["email"], u["display_name"], u["status"])
        return
    _RESOLVED_USER.set(u)


@app.teardown_request
def reset_tenant(exc=None):
    db.reset_tenant_db()
    filestore.reset_tenant()
SCHEDULER_STATE = {"running": True, "last_tick": None, "last_error": ""}
TICK_COUNTER = {"n": 0}
DEV_LOGIN_EMAIL = os.environ.get("CAMPUSMAIL_AI_DEV_LOGIN", "").strip().lower()


# ---------------- auth helpers ----------------

@app.context_processor
def inject_globals():
    u = current_user()
    return {"me": u, "me_email": u["email"] if u else None,
            "in_personal": bool(u and u.get("personal_workspace"))}



def current_user():
    if DEV_LOGIN_EMAIL:
        with db.team_context():
            u = db.dev_user(DEV_LOGIN_EMAIL)
        if u:
            _RESOLVED_USER.set(u)
            return u
    return _RESOLVED_USER.get()


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


def tpl_fields(tpl):
    """Field names for a template row. Legacy rows may store a plain count."""
    if not isinstance(tpl, dict):
        tpl = dict(tpl)
    raw = tpl.get("fields")
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        v = None
    if isinstance(v, list):
        return v
    return all_fields(tpl.get("subject") or "", tpl.get("body_html") or "")


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
        "redirect_uri": request.host_url.rstrip("/") + "/api/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "prompt": "select_account",
    })
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + p)


@app.route("/api/google/callback/request", methods=["POST"])
def google_request_note():
    d = request.get_json(force=True)
    email = (d.get("email") or "").strip().lower()
    with db.team_context():
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
        "redirect_uri": request.host_url.rstrip("/") + "/api/google/callback",
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
    session["uid"] = u["id"]
    if u["status"] != "active":
        return render_template("login.html", email=email, name=name, status=u["status"], request_open=False)
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
@login_required
def admin_page():
    return redirect("/team")


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
    try:
        action = d.get("action")
        if action == "approve":
            services.member_set_status(u["id"], "active", request.user)
        elif action == "deny":
            services.member_set_status(u["id"], "denied", request.user)
        elif action == "revert":
            services.member_set_status(u["id"], "pending", request.user)
        else:
            return api_err("Unknown action")
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


@app.route("/api/admin/members", methods=["POST"])
@admin_required
def admin_members():
    d = request.get_json(force=True)
    u = db.get_user(int(d.get("user_id")))
    if not u:
        return api_err("User not found", 404)
    try:
        if d.get("action") == "role":
            if d.get("role") not in ("admin", "user"):
                return api_err("Bad role")
            if u["id"] == request.user["id"]:
                return api_err("You cannot change your own role.")
            services.member_set_role(u["id"], d.get("role"), request.user)
        elif d.get("action") == "remove":
            if u["id"] == request.user["id"]:
                return api_err("You cannot remove yourself.")
            services.member_remove(u["id"], request.user)
        else:
            return api_err("Unknown action")
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


@app.route("/api/admin/invite", methods=["POST"])
@admin_required
def admin_invite():
    d = request.get_json(force=True)
    try:
        out = services.member_invite(d.get("email"), d.get("name") or "", request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True, "email": out["email"], "how": out["how"]})


# ---------------- pages ----------------

@app.route("/")
@login_required
def page_home():
    return render_template("home.html", page="home")


@app.route("/replies")
@login_required
def page_replies():
    return render_template("replies_new.html", page="replies")


@app.route("/library")
@login_required
def page_library():
    return render_template("library_new.html", page="library")


@app.route("/library/templates/<int:tid>")
@login_required
def page_template_editor(tid):
    return render_template("library_new.html", page="library", edit_id=tid)


@app.route("/team")
@login_required
def page_team():
    return render_template("team_new.html", page="team")


@app.route("/settings")
@login_required
def page_settings():
    return redirect("/settings/gmail")


@app.route("/settings/<sec>")
@login_required
def page_settings_section(sec):
    if sec not in ("gmail", "sending", "signature", "ai", "branding", "advanced"):
        sec = "gmail"
    return render_template("settings_new.html", page="settings", section=sec)


@app.route("/send")
@login_required
def page_send():
    return render_template("send_new.html", page="send", cid=None)


@app.route("/contacts")
@login_required
def page_contacts():
    return redirect("/library?tab=lists")


@app.route("/templates")
@login_required
def page_templates():
    tid = request.args.get("edit", type=int)
    return redirect("/library/templates/%d" % tid if tid else "/library?tab=templates")


@app.route("/merge")
@login_required
def page_merge():
    cid = request.args.get("campaign", type=int)
    return redirect("/campaigns/%d/send" % cid if cid else "/send")


@app.route("/queue")
@login_required
def page_queue():
    return redirect("/campaigns")


@app.route("/activity")
@login_required
def page_activity():
    return redirect("/team?tab=activity")


@app.route("/reports")
@login_required
def page_reports():
    return redirect("/campaigns")




@app.route("/guide")
@login_required
def page_guide():
    return redirect("/?help=1")


@app.route("/campaigns")
@login_required
def page_campaigns():
    return render_template("campaigns_new.html", page="campaigns")


@app.route("/campaigns/<int:cid>")
@login_required
def page_campaign_detail(cid):
    return render_template("campaign_hub.html", page="campaigns", cid=cid)


@app.route("/campaigns/<int:cid>/send")
@login_required
def page_campaign_send(cid):
    return render_template("send_new.html", page="send", cid=cid)


@app.route("/assistant")
@login_required
def page_assistant():
    return redirect("/?ask=1")


@app.route("/local_img/<name>")
@login_required
def local_img(name):
    safe = re.sub(r"[^A-Za-z0-9._\-]", "", name)
    data = filestore.get(safe, ns="images")
    if data is None:
        return api_err("Not found.", 404)
    return Response(data, mimetype=mimetypes.guess_type(safe)[0] or "application/octet-stream")


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
    bounced_n = c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent' "
                           "AND bounced_at IS NOT NULL AND bounced_at != ''").fetchone()["n"]
    reminders_n = c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=1").fetchone()["n"]
    needs_n = c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent' "
                        "AND replied_at IS NOT NULL AND replied_at != '' "
                        "AND (handled_at IS NULL OR handled_at = '')").fetchone()["n"]
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
        "hosted": "1" if ON_PA else "0",
        "scheduler": {"running": SCHEDULER_STATE["running"],
                      "last_tick": SCHEDULER_STATE["last_tick"],
                      "last_error": SCHEDULER_STATE["last_error"],
                      "due_now": due,
                      "next_fire": nxt["scheduled_at"] if nxt else None},
        "reply": {"sent": sent_n, "replied": replied_n, "needs_reply": needs_n, "bounced": bounced_n,
                  "rate": round(100.0 * replied_n / sent_n) if sent_n else 0,
                  "reminders": reminders_n},
        "settings": {k: db.get_setting(k) for k in
                     ("safe_mode", "max_sends_per_day", "default_stagger_min", "catchup_policy",
                      "ai_enabled", "ai_model")},
        "sent_today": services.sends_today(),
        "remaining_today": max(0, int(db.get_setting("max_sends_per_day") or 100) - services.sends_today()),
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
    existing_id = d.get("existing_id")
    if existing_id:
        acc = c.execute("SELECT * FROM gmail_accounts WHERE id=?", (int(existing_id),)).fetchone()
        if not acc:
            c.close()
            return api_err("Account not found", 404)
        if acc["owner_id"] != request.user["id"] and request.user["role"] != "admin":
            c.close()
            return api_err("Only the owner (or an admin) can reconnect this account.", 403)
        aid = int(existing_id)
        c.commit()
        c.close()
        log("gmail_account_add", "reconnect account " + str(aid))
    else:
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
    try:
        services.account_remove(aid, request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


# ---------------- contacts ----------------

@app.route("/api/contacts")
@login_required
def api_contacts():
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or ""
    sheet_id = request.args.get("sheet_id", type=int) or 0
    c = db.conn()
    sql = "SELECT * FROM contacts WHERE 1=1"
    args = []
    if q:
        sql += " AND (company LIKE ? OR hr_name LIKE ? OR hr_email LIKE ? OR serial = ?)"
        args += ["%" + q + "%"] * 3 + [q]
    if status:
        sql += " AND status = ?"
        args.append(status)
    if sheet_id:
        sql += " AND sheet_id = ?"
        args.append(sheet_id)
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
    upload_dir().mkdir(exist_ok=True)
    path = upload_dir() / f.filename
    f.save(str(path))
    c = db.conn()
    if f.filename.lower().endswith(".xlsx"):
            added, updated, skipped = import_xlsx(c, str(path))
    else:
        added, updated, skipped = import_csv(c, str(path))
    c.close()
    path.unlink(missing_ok=True)  # parsed into the DB; the file itself is not needed
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
    fields = {k: d[k] for k in ("company", "hr_name", "hr_email", "status", "notes") if k in d}
    if not fields:
        return api_err("Nothing to update.")
    try:
        services.contact_update(cid, actor=request.user, **fields)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


@app.route("/api/contacts/delete", methods=["POST"])
@login_required
def api_contact_delete():
    d = request.get_json(force=True)
    ids = d.get("ids") or []
    c = db.conn()
    c.executemany("DELETE FROM contacts WHERE id=?", [(i,) for i in ids])
    c.commit()
    import sync_relay
    for i in ids:
        sync_relay.record_delete("contacts", i)
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
        "SELECT id, name, type, kind, subject, fields, is_active, created_at, updated_at, body_html "
        "FROM email_templates ORDER BY id").fetchall()]
    for r in rows:
        r["fields"] = tpl_fields(r)
        r["n_campaigns"] = c.execute(
            "SELECT COUNT(DISTINCT campaign_id) n FROM batches "
            "WHERE (template_id=? OR reminder_template_id=?) AND campaign_id > 0",
            (r["id"], r["id"])).fetchone()["n"]
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
    upload_dir().mkdir(exist_ok=True)
    if f:
        fname = f.filename.lower()
        if fname.endswith(".docx"):
            path = upload_dir() / f.filename
            f.save(str(path))
            body = docx_convert.docx_to_html(str(path))
            path.unlink(missing_ok=True)  # converted into the DB; the file itself is not needed
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
    c.execute("INSERT INTO email_templates(name, type, kind, subject, body_html, fields, is_active) VALUES(?,?,?,?,?,?,0)",
              (name, "ai", (d.get("kind") or "outreach").strip()[:20], subject, body, json.dumps(fields)))
    tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    log("template_ai", "AI template '" + name + "'")
    c.close()
    return jsonify({"ok": True, "id": tid, "subject": subject, "fields": fields})


@app.route("/api/templates/<int:tid>", methods=["POST"])
@login_required
def api_template_update(tid):
    d = request.get_json(force=True)
    try:
        services.template_update(tid,
                                  name=d.get("name") if "name" in d else None,
                                  subject=d.get("subject") if "subject" in d else None,
                                  body_html=d.get("body_html") if "body_html" in d else None,
                                  actor=request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


@app.route("/api/templates/<int:tid>/activate", methods=["POST"])
@login_required
def api_template_activate(tid):
    try:
        services.template_activate(tid, request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


@app.route("/api/templates/<int:tid>/delete", methods=["POST"])
@login_required
def api_template_delete(tid):
    try:
        services.template_delete(tid, request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
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
    for f, col in docx_convert.suggest_mapping(tpl_fields(tpl),
                                                ["serial", "company", "hr_name", "hr_email"]).items():
        if col:
            mapping[f] = "column:" + col
    vals = dict(row) if row else {}
    subject, body = render_email(dict(tpl), vals, mapping)
    return jsonify({"ok": True, "subject": subject,
                    "body": gmail_client.inline_local_images(body),
                    "sample_row": vals, "mapping": mapping})


# ---------------- replies / library / team (additive) ----------------

ACTIVITY_TYPES = {
    "campaigns": ("campaign_create", "campaign_update", "campaign_delete", "batch_create",
                  "batch_reminders", "batch_attachments"),
    "sending": ("send_now", "schedule", "drafts_create", "reply_sent", "reply_draft",
                "emails_cancel", "emails_revert", "email_update", "email_ai_polish", "mark_replied"),
    "contacts": ("contact_add", "contact_delete", "import", "sheet_upload", "sheet_delete",
                 "sheet_campaign", "sheet_update", "practice_list"),
    "templates": ("template_add", "template_ai", "template_ai_reminder", "template_ai_polish",
                  "template_activate", "template_delete", "signature_save", "signature_image"),
    "settings": ("settings", "gmail_account_add", "gmail_account_removed", "relay",
                 "storage_migrate", "deploy", "account_visibility"),
    "team": ("invite", "request_approved", "request_denied", "request_reverted",
             "role_changed", "member_removed"),
}


@app.route("/api/activity")
@login_required
def api_activity():
    sql = "SELECT id, ts, actor, action, detail FROM activity"
    args = []
    person = (request.args.get("person") or "").strip()
    if person:
        sql += " WHERE actor = ?"
        args.append(person)
    t = request.args.get("type") or ""
    if t in ACTIVITY_TYPES:
        acts = ACTIVITY_TYPES[t]
        if args:
            sql += " AND"
        else:
            sql += " WHERE"
        sql += " action IN (" + ",".join("?" * len(acts)) + ")"
        args += list(acts)
    q = (request.args.get("q") or "").strip()
    if q:
        like = "%" + q + "%"
        sql += (" AND" if args else " WHERE") + " (action LIKE ? OR detail LIKE ?)"
        args += [like, like]
    limit = max(1, min(int(request.args.get("limit") or 300), 300))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    c = db.conn()
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    return jsonify({"ok": True, "rows": rows})


@app.route("/api/reports/team-summary")
@login_required
def api_team_reports_summary():
    where = ["e.status != 'deleted'", "b.id IS NOT NULL"]
    args = []
    cid = request.args.get("campaign_id")
    if cid:
        where.append("b.campaign_id = ?")
        args.append(int(cid))
    member = (request.args.get("member") or "").strip()
    if member:
        where.append("b.created_by = ?")
        args.append(member)
    period = request.args.get("period") or "all"
    if period in ("7", "30"):
        since = (datetime.now() - timedelta(days=int(period))).strftime("%Y-%m-%d %H:%M")
        where.append("COALESCE(NULLIF(e.sent_at, ''), e.created_at) >= ?")
        args.append(since)
    c = db.conn()
    rows = [dict(r) for r in c.execute(
        "SELECT e.id, e.status, e.sent_at, e.replied_at, e.bounced_at, e.to_addr, "
        "b.created_by AS member, co.company "
        "FROM emails e JOIN batches b ON b.id = e.batch_id "
        "LEFT JOIN contacts co ON co.id = e.contact_id WHERE " + " AND ".join(where),
        args).fetchall()]
    c.close()

    def agg(rs):
        sent = [r for r in rs if r["status"] == "sent"]
        replied = [r for r in sent if r["replied_at"]]
        bounced = [r for r in sent if r["bounced_at"]]
        return {"n_emails": len(rs), "n_sent": len(sent), "n_replied": len(replied),
                "n_bounced": len(bounced),
                "rate": round(100.0 * len(replied) / len(sent), 1) if sent else 0}

    by_member = []
    for m in sorted({(r["member"] or "").strip() for r in rows if r["member"]}, key=str.lower):
        a = agg([r for r in rows if (r["member"] or "").strip() == m])
        a["member"] = m
        by_member.append(a)
    comp = {}
    for r in rows:
        name = (r["company"] or "").strip() or (r["to_addr"].split("@")[0] if r["to_addr"] else "") or "Unknown"
        comp.setdefault(name, []).append(r)
    by_company = []
    for name, rs in sorted(comp.items(), key=lambda kv: (-agg(kv[1])["n_replied"], kv[0]))[:10]:
        a = agg(rs)
        a["company"] = name
        by_company.append(a)
    return jsonify({"ok": True, "totals": agg(rows), "by_member": by_member, "by_company": by_company})


@app.route("/api/team/members")
@login_required
def api_team_members():
    """Active team roster — any team member may read it (used by the
    person filter on the Results tab, which is not admin-only)."""
    c = db.conn()
    rows = [dict(r) for r in c.execute(
        "SELECT id, email, display_name, role FROM users WHERE status='active' "
        "ORDER BY display_name COLLATE NOCASE, email").fetchall()]
    c.close()
    return jsonify({"ok": True, "members": rows})


@app.route("/api/templates", methods=["POST"])
@login_required
def api_template_create():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    subject = (d.get("subject") or "").strip()
    body = (d.get("body_html") or "").strip()
    if not name:
        return api_err("Give the template a name.")
    if not body:
        return api_err("Write the email body first.")
    kind = (d.get("kind") or "outreach").strip().lower()
    if kind not in ("outreach", "reminder"):
        kind = "outreach"
    try:
        out = services.template_create(name, subject, body, kind, request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True, "id": out["id"], "fields": out["fields"]})


@app.route("/api/sheets/<int:sid>", methods=["POST"])
@login_required
def api_sheet_rename(sid):
    d = request.get_json(force=True)
    try:
        services.sheet_rename(sid, d.get("name"), request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


@app.route("/api/emails/<int:eid>/handled", methods=["POST"])
@login_required
def api_email_handled(eid):
    d = request.get_json(force=True)
    try:
        services.email_handled(eid, bool(d.get("handled")), request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


# ---------------- merge ----------------

@app.route("/api/emails/test", methods=["POST"])
@login_required
def api_emails_test():
    """Render the first row of the chosen list and deliver one email to the
    signed-in user's own address: as a Gmail draft in Drafts-only mode, or
    live in Live mode."""
    d = request.get_json(force=True)
    tid = d.get("template_id")
    mapping = d.get("mapping") or {}
    sheet_id = int(d.get("sheet_id") or 0)
    account_id = int(d.get("account_id") or 0)
    c = db.conn()
    tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    w = " AND sheet_id=?" if sheet_id else ""
    args = [sheet_id] if sheet_id else []
    r = c.execute("SELECT * FROM contacts WHERE 1=1" + w +
                  " ORDER BY CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER), id LIMIT 1", args).fetchone()
    c.close()
    if not tpl:
        return api_err("Template not found.", 404)
    if not r:
        return api_err("No contacts in that list.", 404)
    if not account_id:
        return api_err("Pick a send account first.", 400)
    try:
        _require_account(account_id, request.user)
    except RuntimeError as e:
        return api_err(str(e), 409)
    subject, body = render_email(dict(tpl), _row_vals(r), mapping)
    if d.get("append_signature"):
        body = services.append_signature(body)
    to = request.user["email"]
    try:
        out = services.test_email_send(account_id, to, subject, body, request.user,
                                       template_id=tid, content_hash=services.template_content_hash(dict(tpl)))
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    except Exception as e:
        return api_err(str(e)[:200], 409)
    return jsonify({"ok": True, "mode": out["mode"]})


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
    contact_ids = [int(i) for i in (d.get("contact_ids") or []) if str(i).isdigit()]
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
    if contact_ids:
        w += " AND id IN (%s)" % ",".join("?" * len(contact_ids))
        args += contact_ids
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
    # D-32/B2: hold keys are server-sanctioned only. Raw client values used
    # to bypass safe mode, so strip them; a plain "hold": true opts into the
    # configured undo window (send_undo_seconds). Safe mode still blocks.
    d = dict(d or {})
    d.pop("internal_hold", None)
    d.pop("internal_hold_seconds", None)
    if d.get("action") == "send" and d.get("hold"):
        d["internal_hold_seconds"] = int(db.get_setting("send_undo_seconds") or 0)
    try:
        out = services.merge(d, request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify(out)


@app.route("/api/batches/<int:bid>/send-as", methods=["POST"])
@login_required
def api_batch_send_as(bid):
    """Finalize a created batch: draft, schedule, or send now - used after
    attachments have been uploaded to the batch."""
    d = request.get_json(force=True)
    # D-32/B2: server-sanctioned hold, same rule as /api/merge/generate.
    hold = 0
    if (d.get("action") or "") == "send" and d.get("hold"):
        hold = int(db.get_setting("send_undo_seconds") or 0)
    try:
        out = services.batch_send_as(bid, d.get("action") or "", d.get("at") or "",
                                     d.get("stagger") or 0, int(d.get("account_id") or 0),
                                     request.user, internal_hold_seconds=hold)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify(out)


@app.route("/api/batches/<int:bid>/undo", methods=["POST"])
@login_required
def api_batch_undo(bid):
    """Cancel a held batch while its undo window is open. Idempotent (D-15)."""
    try:
        out = services.undo_send(bid, request.user["id"], request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True, **out})


# ---------------- campaigns ----------------

@app.route("/api/campaigns")
@login_required
def api_campaigns():
    c = db.conn()
    out = []
    for cam in c.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall():
        st = {r["status"]: r["n"] for r in c.execute(
            "SELECT e.status, COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
            "WHERE b.campaign_id=? AND e.status != 'deleted' GROUP BY e.status", (cam["id"],))}
        sent = st.get("sent", 0)
        replied = c.execute(
            "SELECT COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
            "WHERE b.campaign_id=? AND e.status='sent' AND e.replied_at IS NOT NULL AND e.replied_at!=''",
            (cam["id"],)).fetchone()["n"]
        bounced = c.execute(
            "SELECT COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
            "WHERE b.campaign_id=? AND e.status='sent' AND e.bounced_at IS NOT NULL AND e.bounced_at!=''",
            (cam["id"],)).fetchone()["n"]
        n_sheets = c.execute("SELECT COUNT(*) n FROM sheets WHERE campaign_id=?", (cam["id"],)).fetchone()["n"]
        n_contacts = c.execute(
            "SELECT COUNT(*) n FROM contacts ct JOIN sheets s ON ct.sheet_id=s.id WHERE s.campaign_id=?",
            (cam["id"],)).fetchone()["n"]
        if n_sheets == 0:
            n_contacts = c.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
        delivered = max(0, sent - bounced)
        out.append({**dict(cam), "status_counts": st, "n_emails": sum(st.values()),
                    "n_sent": sent, "n_replied": replied, "n_bounced": bounced, "n_delivered": delivered,
                    "rate": round(100.0 * replied / delivered) if delivered else 0,
                    "n_batches": c.execute("SELECT COUNT(*) n FROM batches WHERE campaign_id=?", (cam["id"],)).fetchone()["n"],
                    "n_sheets": n_sheets,
                    "n_contacts": n_contacts,
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


@app.route("/api/campaigns/<int:cid>", methods=["POST"])
@login_required
def api_campaign_update(cid):
    d = request.get_json(force=True)
    try:
        services.campaign_update(cid, name=d.get("name") if "name" in d else None,
                                 description=d.get("description") if "description" in d else None,
                                 actor=request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


@app.route("/api/campaigns/<int:cid>/delete", methods=["POST"])
@login_required
def api_campaign_delete(cid):
    try:
        services.campaign_delete(cid, request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


@app.route("/api/campaigns/<int:cid>")
@login_required
def api_campaign_detail(cid):
    c = db.conn()
    cam = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not cam:
        c.close()
        return api_err("Campaign not found", 404)
    # optional analytics view: filter everything to one sender (team member)
    sender = (request.args.get("sender") or "").strip()
    sender_uid = None
    if sender:
        su = c.execute("SELECT id FROM users WHERE email=?", (sender,)).fetchone()
        if su:
            sender_uid = su["id"]
    send_by = "JOIN gmail_accounts ga ON e.account_id = ga.id" if sender_uid else ""
    send_w = "AND ga.owner_id = ?" if sender_uid else ""
    send_p = (sender_uid,) if sender_uid else ()
    st = {r["status"]: r["n"] for r in c.execute(
        "SELECT e.status, COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
        "WHERE b.campaign_id=? GROUP BY e.status", (cid,))}
    batches = []
    for b in c.execute("SELECT * FROM batches WHERE campaign_id=? ORDER BY id DESC", (cid,)).fetchall():
        bc = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM emails WHERE batch_id=? GROUP BY status", (b["id"],))}
        bn = c.execute("SELECT COUNT(*) n FROM emails WHERE batch_id=? "
                       "AND bounced_at IS NOT NULL AND bounced_at != ''", (b["id"],)).fetchone()["n"]
        batches.append({**dict(b), "counts": bc, "bounced": bn})
    sheets = [dict(r) for r in c.execute("SELECT * FROM sheets WHERE campaign_id=? ORDER BY id DESC", (cid,)).fetchall()]
    for sh in sheets:
        sh["headers"] = json.loads(sh["headers"] or "[]")
    n_sheets = c.execute("SELECT COUNT(*) n FROM sheets WHERE campaign_id=?", (cid,)).fetchone()["n"]
    n_contacts = c.execute(
        "SELECT COUNT(*) n FROM contacts ct JOIN sheets s ON ct.sheet_id=s.id WHERE s.campaign_id=?",
        (cid,)).fetchone()["n"]
    recipients = []
    if n_sheets == 0:
        # no linked lists: the audience is the whole contact list (the send
        # flow defaults to "All contacts" for sheet-less campaigns)
        n_contacts = c.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
        recipients = [dict(r) for r in c.execute("""
            SELECT co.id AS contact_id, co.company, co.hr_name, co.hr_email,
                   MAX(CASE WHEN e.bounced_at IS NOT NULL AND e.bounced_at != '' THEN 1 ELSE 0 END) AS bounced,
                   MAX(CASE WHEN e.replied_at IS NOT NULL AND e.replied_at != '' THEN 1 ELSE 0 END) AS replied
            FROM emails e JOIN batches b ON e.batch_id = b.id
            LEFT JOIN contacts co ON co.id = e.contact_id
            WHERE b.campaign_id = ? AND e.status != 'deleted' AND e.contact_id > 0
            GROUP BY co.id
            ORDER BY COALESCE(NULLIF(co.hr_name, ''), co.company, co.hr_email)
        """, (cid,)).fetchall()]
    # UI redesign (hub overview): derived status summary, sends per day,
    # recent replies. Additive fields only.
    srow = c.execute("""
        SELECT COUNT(*) AS n_emails,
          SUM(CASE WHEN e.status='pending' THEN 1 ELSE 0 END) AS n_ready,
          SUM(CASE WHEN e.status='drafted' THEN 1 ELSE 0 END) AS n_drafted,
          SUM(CASE WHEN e.status='scheduled' THEN 1 ELSE 0 END) AS n_scheduled,
          SUM(CASE WHEN e.status='sent' AND COALESCE(e.bounced_at,'')='' AND COALESCE(e.replied_at,'')='' THEN 1 ELSE 0 END) AS n_sent,
          SUM(CASE WHEN COALESCE(e.replied_at,'')!='' THEN 1 ELSE 0 END) AS n_replied,
          SUM(CASE WHEN COALESCE(e.bounced_at,'')!='' THEN 1 ELSE 0 END) AS n_bounced,
          SUM(CASE WHEN e.status='error' THEN 1 ELSE 0 END) AS n_failed,
          SUM(CASE WHEN e.status='cancelled' THEN 1 ELSE 0 END) AS n_stopped,
          SUM(CASE WHEN e.status='sent' AND COALESCE(e.replied_at,'')='' AND COALESCE(e.bounced_at,'')='' THEN 1 ELSE 0 END) AS n_sent_unreplied,
          MAX(CASE WHEN e.status='sent' AND COALESCE(e.bounced_at,'')='' THEN e.sent_at END) AS last_sent_at,
          MIN(CASE WHEN e.status='scheduled' THEN e.scheduled_at END) AS next_scheduled_at
        FROM emails e JOIN batches b ON e.batch_id=b.id %s
        WHERE b.campaign_id=? AND e.status != 'deleted' %s
    """ % (send_by, send_w), (cid,) + send_p).fetchone()
    summary = dict(srow)
    for k in ("n_emails", "n_ready", "n_drafted", "n_scheduled", "n_sent",
              "n_replied", "n_bounced", "n_failed", "n_stopped", "n_sent_unreplied"):
        summary[k] = summary.get(k) or 0
    per_day = {r["d"]: r["n"] for r in c.execute("""
        SELECT date(e.sent_at) d, COUNT(*) n
        FROM emails e JOIN batches b ON e.batch_id=b.id %s
        WHERE b.campaign_id=? AND e.status='sent' AND e.sent_at IS NOT NULL AND e.sent_at != '' %s
        GROUP BY d""" % (send_by, send_w), (cid,) + send_p).fetchall()}
    n_emailed = c.execute(
        "SELECT COUNT(DISTINCT e.contact_id) n FROM emails e JOIN batches b ON e.batch_id=b.id %s "
        "WHERE b.campaign_id=? AND e.status != 'deleted' %s" % (send_by, send_w),
        (cid,) + send_p).fetchone()["n"]
    recent_replies = [dict(r) for r in c.execute("""
        SELECT e.id, e.to_addr, e.replied_at, co.company, co.hr_name
        FROM emails e JOIN batches b ON e.batch_id=b.id %s LEFT JOIN contacts co ON co.id=e.contact_id
        WHERE b.campaign_id=? AND e.replied_at IS NOT NULL AND e.replied_at != '' %s
        ORDER BY e.replied_at DESC LIMIT 5""" % (send_by, send_w), (cid,) + send_p).fetchall()]
    # who has sent in this campaign (for the per-sender analytics filter)
    senders = [dict(r) for r in c.execute("""
        SELECT COALESCE(u.email, 'unknown') AS email,
               COALESCE(NULLIF(u.display_name, ''), COALESCE(u.email, 'Unknown')) AS name,
               COUNT(e.id) AS n
        FROM emails e JOIN batches b ON e.batch_id=b.id
        LEFT JOIN gmail_accounts ga ON e.account_id=ga.id
        LEFT JOIN users u ON u.id=ga.owner_id
        WHERE b.campaign_id=? AND e.status != 'deleted'
        GROUP BY COALESCE(u.email, 'unknown'), 2
        ORDER BY n DESC""", (cid,)).fetchall()]
    c.close()
    return jsonify({"ok": True, "campaign": dict(cam), "status_counts": st,
                    "batches": batches, "sheets": sheets, "n_contacts": n_contacts, "recipients": recipients,
                    "summary": summary, "per_day": per_day, "recent_replies": recent_replies,
                    "n_emailed": n_emailed, "senders": senders, "sender": sender or None})


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
    elif fname.endswith(".tsv"):
        import io
        text = f.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        raw_headers = [(h or "").strip() for h in (reader.fieldnames or [])]
        for row in reader:
            raw_rows.append([(row.get(h) or "").strip() for h in raw_headers])
    elif fname.endswith((".csv", ".txt")):
        import io
        text = f.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        raw_headers = [(h or "").strip() for h in (reader.fieldnames or [])]
        for row in reader:
            raw_rows.append([(row.get(h) or "").strip() for h in raw_headers])
    else:
        raise ValueError("Upload a .csv, .tsv or .xlsx file.")
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
    name = (request.form.get("name") or "").strip() \
        or Path(f.filename).stem.replace("_", " ").replace("-", " ").strip() \
        or "List " + datetime.now().strftime("%Y-%m-%d %H:%M")
    email_col = next((h for h in headers if re.search(r"email|mail", h, re.I)), "")
    comp_col = next((h for h in headers if re.search(r"company|organi[sz]ation|\\borg\\b|firm", h, re.I)), "")
    name_col = next((h for h in headers if h != comp_col and re.search(r"recruiter|hr|name|person|contact", h, re.I)), "")
    try:
        out = services.sheet_upload(name, campaign_id, headers, rows, f.filename, request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True, "id": out["sheet_id"], "sheet_id": out["sheet_id"], "name": out["name"],
                    "headers": headers, "n_rows": out["rows"],
                    "preview": rows[:3], "email_col": email_col, "name_col": name_col,
                    "company_col": comp_col, "no_email": out["no_email"],
                    "duplicates": out["duplicates"]})


def _parse_pasted_table(text):
    """Pasted-from-Excel/Sheets text -> (headers, rows-of-dicts)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Paste a table first.")
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if len(lines) < 2:
        raise ValueError("Paste a header row plus at least one data row.")
    first = lines[0]
    if "\t" in first:
        grid = [ln.split("\t") for ln in lines]
    elif "," in first and ";" not in first:
        grid = [ln.split(",") for ln in lines]
    elif ";" in first:
        grid = [ln.split(";") for ln in lines]
    else:
        grid = [ln.split() for ln in lines]
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    headers = [(h or "").strip() for h in grid[0]]
    data = [[(v or "").strip() for v in r] for r in grid[1:]]
    data = [r for r in data if any(v for v in r)]
    if not data:
        raise ValueError("No data rows found in the pasted text.")
    known = ("serial", "no", "company", "organi", "firm", "recruiter", "hr", "name", "email", "mail")
    recognized = [h for h in headers if any(k in re.sub(r"[^a-z]", "", h.lower()) for k in known)]
    if not recognized:
        headers = (["serial", "company", "hr_name", "hr_email"] if width >= 4
                   else ["company", "hr_name", "hr_email"] if width == 3
                   else ["company", "hr_email"] if width == 2 else ["hr_email"])
    rows = [dict(zip(headers, r)) for r in data]
    return headers, rows


@app.route("/api/sheets/paste", methods=["POST"])
@login_required
def api_sheet_paste():
    d = request.get_json(force=True)
    campaign_id = int(d.get("campaign_id") or 0)
    name = (d.get("name") or "").strip() or "Pasted list - " + datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        headers, rows = _parse_pasted_table(d.get("text") or "")
    except ValueError as e:
        return api_err(str(e), 400)
    try:
        out = services.sheet_upload(name, campaign_id, headers, rows, "paste", request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True, "id": out["sheet_id"], "sheet_id": out["sheet_id"], "name": out["name"],
                    "headers": headers, "n_rows": out["rows"], "no_email": out["no_email"],
                    "duplicates": out["duplicates"]})


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
    try:
        services.sheet_delete(sid, request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
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
    """Email list. Additive filters (UI redesign): campaign_id, q, sort, dir,
    and 'bounced' as a pseudo-status. Defaults preserve the old behaviour."""
    c = db.conn()
    sql = ("SELECT e.*, co.company, co.hr_name, b.name AS batch_name, c.id AS campaign_id, "
           "c.name AS campaign_name, b.created_by AS member "
           "FROM emails e LEFT JOIN contacts co ON co.id = e.contact_id "
           "LEFT JOIN batches b ON b.id = e.batch_id "
           "LEFT JOIN campaigns c ON c.id = b.campaign_id")
    sql += " WHERE e.status != 'deleted'"
    args = []
    status = params.get("status")
    if status == "replied":
        sql += " AND e.replied_at IS NOT NULL AND e.replied_at != ''"
    elif status == "bounced":
        sql += " AND e.bounced_at IS NOT NULL AND e.bounced_at != ''"
    elif status:
        sql += " AND e.status = ?"
        args.append(status)
    if params.get("campaign_id"):
        sql += " AND b.campaign_id = ?"
        args.append(params["campaign_id"])
    if params.get("batch_id"):
        sql += " AND e.batch_id = ?"
        args.append(params["batch_id"])
    if params.get("by") and params.get("campaign_id"):
        sql += " AND b.created_by = ?"
        args.append(params["by"])
    if params.get("q"):
        like = "%" + str(params["q"]) + "%"
        sql += " AND (e.to_addr LIKE ? OR e.subject LIKE ? OR co.company LIKE ? OR co.hr_name LIKE ?)"
        args += [like, like, like, like]
    sort = params.get("sort")
    d = "DESC" if params.get("dir") == "desc" else "ASC"
    if sort == "status":
        sql += " ORDER BY e.status " + d + ", e.id ASC"
    elif sort == "time":
        sql += " ORDER BY COALESCE(e.sent_at, e.scheduled_at, e.created_at) " + d
    elif sort == "replied":
        sql += " ORDER BY e.replied_at " + d
    elif sort == "company":
        sql += " ORDER BY co.company " + d + ", e.id ASC"
    else:
        sql += " ORDER BY CAST(COALESCE(NULLIF(e.serial,''),'0') AS INTEGER), e.id"
    sql += " LIMIT 3000"
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
    ids = _selected_ids(d)
    if not ids:
        return api_err("Nothing selected.")
    try:
        out = services.emails_draft(ids, int(d.get("account_id") or 0), request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True, **out})


@app.route("/api/emails/schedule", methods=["POST"])
@login_required
def api_emails_schedule():
    d = request.get_json(force=True)
    ids = _selected_ids(d)
    if not ids:
        return api_err("Nothing selected.")
    try:
        out = services.emails_schedule(ids, d.get("at") or "", d.get("stagger") or 0,
                                       int(d.get("account_id") or 0), request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True, **out})


@app.route("/api/emails/send-now", methods=["POST"])
@login_required
def api_emails_send_now():
    d = request.get_json(force=True)
    ids = _selected_ids(d)
    if not ids:
        return api_err("Nothing selected.")
    hold = 0 if d.get("_agent_direct") else int(db.get_setting("send_undo_seconds") or 0)
    try:
        if hold > 0:
            # Undo window for immediate sends (spec 7.7, decision Q6): the batch is
            # held as scheduled with release_at = now + hold. The agent's own
            # prepare_send action manages its hold explicitly (_agent_direct).
            out = services.send_now_held(ids, int(d.get("account_id") or 0),
                                         request.user, internal_hold_seconds=hold)
        else:
            out = services.emails_send_now(ids, int(d.get("account_id") or 0), request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True, **out})


@app.route("/api/emails/delete", methods=["POST"])
@login_required
def api_emails_delete():
    d = request.get_json(force=True)
    ids = d.get("ids") or []
    if not ids:
        return api_err("Nothing selected.")
    out = services.emails_delete(ids, request.user)
    return jsonify({"ok": True, "removed": out["removed"]})


@app.route("/api/emails/cancel", methods=["POST"])
@login_required
def api_emails_cancel():
    d = request.get_json(force=True)
    ids = d.get("ids") or []
    if not ids:
        return api_err("Nothing selected.")
    out = services.emails_cancel(ids, request.user)
    return jsonify({"ok": True, "cancelled": out["cancelled"]})


@app.route("/api/emails/<int:eid>/reply", methods=["POST"])
@login_required
def api_email_reply(eid):
    d = request.get_json(force=True)
    try:
        out = services.email_reply(eid, d.get("body"), int(d.get("account_id") or 0),
                                   as_draft=bool(d.get("as_draft")), actor=request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    except Exception as e:
        return api_err(str(e)[:200], 500)
    return jsonify({"ok": True, **out})


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


def bounces_tick():
    """Scan send-account inboxes for delivery failures and mark matching mails as
    bounced (recipient address wrong or does not exist). State is mirrored to the
    cloud relay so every instance shows the same analytics."""
    c = db.conn()
    accts = [dict(r) for r in c.execute("SELECT id FROM gmail_accounts").fetchall()]
    c.close()
    for a in accts:
        if not db.account_token_json(a["id"]):
            continue
        try:
            notices = gmail_client.list_bounce_notices(a["id"])
        except Exception as e:
            db.log_activity("bounce_poll_error", str(e)[:200], "scheduler", throttle=1800)
            continue
        if not notices:
            continue
        c = db.conn()
        marked = []
        for n in notices:
            if c.execute("SELECT 1 FROM processed_dsn WHERE account_id=? AND message_id=?",
                         (a["id"], n["message_id"])).fetchone():
                continue
            for addr, reason in gmail_client.parse_bounce_notice(n["text"], n["subject"]):
                row = c.execute("SELECT id FROM emails WHERE lower(to_addr)=lower(?) AND status='sent' "
                                "AND (bounced_at IS NULL OR bounced_at='') ORDER BY id DESC LIMIT 1",
                                (addr,)).fetchone()
                if not row:
                    continue
                c.execute("UPDATE emails SET bounced_at=?, bounce_reason=? WHERE id=?",
                          (db.now_iso(), reason, row["id"]))
                c.execute("UPDATE contacts SET status='bounced' "
                          "WHERE id=(SELECT contact_id FROM emails WHERE id=?) AND status NOT IN ('replied','bounced')",
                          (row["id"],))
                marked.append((row["id"], addr, reason))
            c.execute("INSERT OR IGNORE INTO processed_dsn(account_id, message_id, processed_at) VALUES(?,?,?)",
                      (a["id"], n["message_id"], db.now_iso()))
        c.commit()
        c.close()
        for eid, addr, reason in marked:
            db.log_activity("bounce_detected", str(eid) + " " + addr + " - " + reason[:140], "scheduler")
            try:
                _cloud_push_email(eid)
            except Exception:
                pass


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
        "bounced_at": r["bounced_at"] or "", "bounce_reason": (r["bounce_reason"] or ""),
        "undo_hold": bool(services.undo_hold_for_batch(r["batch_id"])) if r["batch_id"] else False,
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
        SCHEDULER_STATE["relay_down"] = True
        db.log_activity("relay_pull_error", str(e)[:150], "scheduler", throttle=1800)
        return
    if SCHEDULER_STATE.get("relay_down"):
        SCHEDULER_STATE["relay_down"] = False
        db.log_activity("relay_recovered", "Cloud relay reachable again", "scheduler")
    c = db.conn()
    pending_logs = []
    for e in data.get("emails", []):
        lid, st = e.get("local_id"), e.get("status")
        if not lid or st not in ("sent", "error", "scheduled", "cancelled", "drafted"):
            continue
        row = c.execute("SELECT status FROM emails WHERE id=?", (lid,)).fetchone()
        if row and row["status"] == "deleted":
            continue  # locally deleted - never resurrect from cloud status
        if not row and e.get("is_reminder") and st == "sent":
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
            c.execute("UPDATE contacts SET status='sent' "
                      "WHERE id=(SELECT contact_id FROM emails WHERE id=?) AND status NOT IN ('replied','bounced')", (lid,))
        if st == "sent" and e.get("bounced_at"):
            c.execute("UPDATE emails SET bounced_at=?, bounce_reason=COALESCE(NULLIF(?,''), bounce_reason) "
                      "WHERE id=? AND (bounced_at IS NULL OR bounced_at='')",
                      (e["bounced_at"], e.get("bounce_reason") or "", lid))
            c.execute("UPDATE contacts SET status='bounced' "
                      "WHERE id=(SELECT contact_id FROM emails WHERE id=?) AND status NOT IN ('replied','bounced')", (lid,))
    c.commit()
    c.close()
    for detail in pending_logs:
        db.log_activity("reminder_sent_cloud", detail, "scheduler")


def _release_due_holds():
    """Undo window (spec 7.7): release held batches once release_at is due.
    If safe mode was switched ON during the window, the emails stay queued."""
    c = db.conn()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = c.execute("SELECT * FROM undo_holds WHERE release_at <= ?", (now_str,)).fetchall()
    for h in rows:
        bid = h["batch_id"]
        if db.get_setting("safe_mode") == "1":
            n = c.execute("UPDATE emails SET status='pending', scheduled_at=NULL, error='' "
                          "WHERE batch_id=? AND status='scheduled'", (bid,)).rowcount
            c.execute("DELETE FROM undo_holds WHERE batch_id=?", (bid,))
            c.commit()
            db.log_activity("hold_release_blocked",
                            "Batch %s - undo window over but safe mode is ON: %d email(s) kept queued" % (bid, n),
                            "scheduler")
        else:
            c.execute("DELETE FROM undo_holds WHERE batch_id=?", (bid,))
            c.commit()
            db.log_activity("hold_released", "Batch %s - undo window over, emails may send" % bid, "scheduler")
            for er in c.execute("SELECT id FROM emails WHERE batch_id=? AND status='scheduled'", (bid,)).fetchall():
                try:
                    _cloud_push_email(er["id"], "scheduled")
                except Exception:
                    pass
    c.close()


def scheduler_tick():
    SCHEDULER_STATE["last_tick"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        _release_due_holds()
    except Exception as e:
        SCHEDULER_STATE["last_error"] = str(e)[:200]
    if db.get_setting("safe_mode") == "1":
        return
    if _relay_enabled():
        relay_status_pull()
        if TICK_COUNTER["n"] % 15 == 0:
            try:
                bounces_tick()
            except Exception as e:
                SCHEDULER_STATE["last_error"] = str(e)[:200]
        TICK_COUNTER["n"] += 1
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
    sent_start = sends_today()
    c = db.conn()
    for r in due:
        if db.get_setting("safe_mode") == "1":
            break
        if sent_start + sent >= cap:
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
            c.commit()  # release the write lock before log_activity opens its own connection
            db.log_activity("scheduler_sent", "serial " + r["serial"] + " -> " + r["to_addr"], "scheduler")
            sent += 1
        except Exception as e:
            c.execute("UPDATE emails SET status='error', error=? WHERE id=?", (str(e)[:300], r["id"]))
            c.commit()
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
    if TICK_COUNTER["n"] % 15 == 0:
        try:
            bounces_tick()
        except Exception as e:
            SCHEDULER_STATE["last_error"] = str(e)[:200]


def scheduler_loop():
    import sync_relay
    while SCHEDULER_STATE["running"]:
        try:
            filestore.reset_tenant()
            scheduler_tick()
            if WORKSPACES_DIR.exists():
                for ws_db in sorted(WORKSPACES_DIR.glob("*.db")):
                    key = ws_db.stem
                    wdir = WORKSPACES_DIR / key
                    with db.tenant(ws_db):
                        filestore.set_tenant(wdir / "attachments", wdir / "images", "ws/" + key)
                        scheduler_tick()
        except Exception as e:
            SCHEDULER_STATE["last_error"] = str(e)[:200]
        try:
            sync_relay.sync_tick()
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
    s.pop("ai_api_key_2", None)
    s.pop("app_secret", None)
    s.pop("supabase_service_key", None)
    s["supabase_key_set"] = "1" if db.get_setting("supabase_service_key") else "0"
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


@app.route("/api/sync/status")
@login_required
def api_sync_status():
    """Local sync state + best-effort relay status for the Settings UI."""
    import sync_relay
    import urllib.request as _ur
    url = (db.get_setting("cloud_relay_url", "") or "").strip().rstrip("/")
    secret = (db.get_setting("cloud_relay_secret", "") or "").strip()
    c = db.conn()
    try:
        pending = []
        for table in sync_relay.SYNC_COLUMNS:
            pushed = dict(c.execute("SELECT row_id, version_ms FROM sync_pushed "
                                     "WHERE table_name=? AND kind='row'", (table,)).fetchall())
            n_rows = 0
            scol = [k for k in ("id", "updated_at", "updated_at_ms")
                    if k in sync_relay.SYNC_COLUMNS[table]]
            for r in c.execute("SELECT %s FROM %s" % (",".join(scol), table)).fetchall():
                ms = int(r["updated_at_ms"] or 0) if "updated_at_ms" in r.keys() else 0
                if not ms and "updated_at" in r.keys():
                    ms = sync_relay._ts_ms(r["updated_at"])
                if ms > int(pushed.get(r["id"], 0) or 0):
                    n_rows += 1
            pushed_del = dict(c.execute("SELECT row_id, version_ms FROM sync_pushed "
                                         "WHERE table_name=? AND kind='del'", (table,)).fetchall())
            n_dels = 0
            for r in c.execute("SELECT row_id, deleted_at FROM sync_deletes WHERE table_name=?", (table,)).fetchall():
                if sync_relay._ts_ms(r["deleted_at"]) > int(pushed_del.get(r["row_id"], 0) or 0):
                    n_dels += 1
            pending.append({"table": table, "rows": n_rows, "deletes": n_dels})
    finally:
        c.close()
    relay = None
    if url and secret:
        try:
            req = _ur.Request(url + "/api/sync/status", headers={"x-relay-secret": secret})
            relay = json.loads(_ur.urlopen(req, timeout=8).read().decode())
        except Exception as e:
            relay = {"ok": False, "error": str(e)[:150]}
    fails = int(db.get_setting("sync_fail_count", "0") or 0)
    return jsonify({
        "ok": True,
        "client_version": sync_relay.VERSION,
        "relay_configured": bool(url and secret),
        "relay_url": url,
        "instance": db.get_setting("sync_instance_id", ""),
        "rev_watermark": int((db.get_setting("sync_v2_rev", "") or "0").split("|")[0] or 0),
        "fail_count": fails,
        "halted": sync_relay.SYNC_STATE.get("halted", False),
        "pending": pending,
        "pending_rows": sum(p["rows"] for p in pending),
        "relay": relay,
    })


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings_post():
    d = request.get_json(force=True)
    try:
        services.settings_update(request.user, d)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


@app.route("/api/storage")
@login_required
def api_storage_get():
    return jsonify({"ok": True, "storage": {
        "backend": db.get_setting("storage_backend", "local"),
        "bucket": db.get_setting("supabase_bucket", ""),
        "url_set": "1" if db.get_setting("supabase_url") else "0",
        "key_set": "1" if db.get_setting("supabase_service_key") else "0",
        "local": filestore.local_stats(),
    }})


@app.route("/api/storage/migrate", methods=["POST"])
@login_required
def api_storage_migrate():
    if request.user["role"] != "admin":
        return api_err("Admin only.", 403)
    try:
        ok, fail = filestore.migrate_to_supabase()
    except Exception as e:
        return api_err(str(e), 409)
    log("storage_migrate", "moved " + str(ok) + " file(s) to Supabase, " + str(fail) + " failed")
    return jsonify({"ok": fail == 0, "moved": ok, "failed": fail})


@app.route("/api/deploy", methods=["POST"])
@admin_required
def api_deploy():
    if not ON_PA:
        return api_err("One-click deploy is only available on the hosted PythonAnywhere instance. " "Your local app picks up changes when you run ./run.sh again.", 409)
    try:
        r = subprocess.run(["git", "pull", "--ff-only"], cwd=str(BASE),
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return api_err("git pull failed: " + (r.stderr or r.stdout or "").strip()[:300], 409)
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "-q", "-r", "requirements.txt"],
                       cwd=str(BASE), capture_output=True, timeout=600)
    except Exception as e:
        return api_err("Deploy failed: " + str(e)[:200], 500)
    log("deploy", "git pull OK - worker restarting to load new code")

    def _restart():
        time.sleep(2)  # let the response flush, then die; PA respawns us with the new code
        os._exit(1)

    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"ok": True, "detail": "New code pulled. Restarting - reload the page in about 10 seconds."})


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
     "description": "Generate a batch of emails from a template + data (sheet_id, or blank for the main contact list). action: 'queue' (hold), 'draft' (Gmail drafts) or 'schedule' (automatic send at 'at', format YYYY-MM-DD HH:MM local, at least 5 min ahead). Never sends immediately.",
     "parameters": {"type": "object", "properties": {
         "campaign_id": {"type": "integer"}, "template_id": {"type": "integer"},
         "sheet_id": {"type": "integer"}, "serial_from": {"type": "integer"}, "serial_to": {"type": "integer"},
         "action": {"type": "string", "enum": ["queue", "draft", "schedule"]},
         "at": {"type": "string"}, "stagger": {"type": "integer"}, "name": {"type": "string"}},
         "required": ["template_id", "action"]}}},
    {"type": "function", "function": {"name": "list_emails",
     "description": "List queue emails with id, who (recruiter name), company, subject and status. "
     "Optional status filter: sent, scheduled, pending, drafted, cancelled, error, 'replied' "
     "(emails the recruiter answered) or 'bounced' (delivery failed - address wrong or does not exist).",
     "parameters": {"type": "object", "properties": {
         "status": {"type": "string"}, "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "cancel_emails",
     "description": "Stop queue/scheduled emails so they will not be sent.",
     "parameters": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]}}},
    {"type": "function", "function": {"name": "reschedule_emails",
     "description": "Move queue/scheduled emails to a new time (YYYY-MM-DD HH:MM local, at least 5 min ahead).",
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
    replied_n = c.execute("SELECT COUNT(*) n FROM emails WHERE replied_at IS NOT NULL AND replied_at != ''").fetchone()["n"]
    bounced_n_ctx = c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent' "
                               "AND bounced_at IS NOT NULL AND bounced_at != ''").fetchone()["n"]
    recent_sent = [dict(r) for r in c.execute(
        "SELECT e.to_addr, e.sent_at, ct.company, ct.hr_name FROM emails e "
        "LEFT JOIN contacts ct ON ct.id = e.contact_id WHERE e.status='sent' "
        "ORDER BY e.sent_at DESC, e.id DESC LIMIT 5").fetchall()]
    c.close()
    safe = db.get_setting("safe_mode") == "1"
    recent = ", ".join(
        (x["company"] or x["hr_name"] or x["to_addr"]) + (" on " + x["sent_at"][:10] if x["sent_at"] else "")
        for x in recent_sent) or "none yet"
    return "\n".join([
        "You are the built-in assistant of CampusMail AI, a CRM for the outreach team.",
        "You help by chatting and by doing safe actions directly in the platform: creating campaigns and templates, "
        "planning batches (queue / Gmail drafts / scheduled sends), stopping or rescheduling emails, and reporting status.",
        "Hard rules: you can NEVER send emails immediately and must never claim you sent something. If the user asks to "
        "send now, explain that live sending is a manual step in Queue and Schedule (and needs Safe mode OFF).",
        "Recruiter replies are tracked automatically in the platform: an email is marked replied (replied_at) when the "
        "recruiter answers. The context below already gives sent/replied counts, and list_emails with status=\"replied\" "
        "lists them. Never tell the user you cannot see replies or check their inbox.",
        "If an action is missing a detail (which sheet, which time, which template, how many rows), ask ONE short "
        "question instead of guessing.",
        "",
        "Answer style (the chat box renders PLAIN TEXT - it does not render markdown):",
        "- Never use markdown: no **bold**, no # headings, no backticks, no '- ' bullet lines, no tables.",
        "- Start directly with the answer. No preambles like 'Here is what I can see:' and no closing lines like "
        "'Would you like help with anything else?'.",
        "- Keep answers to 2-5 short lines. For numbers use simple 'Label: value' lines, one per line.",
        "- Name people by recruiter name and company, never by full email address.",
        "- Be concise and friendly. After doing something, state exactly what was done (names, counts, times).",
        "",
        "Current context:",
        "- Signed in: " + request.user["email"] + " (" + (request.user.get("role") or "user") + ")",
        "- Safe mode: " + ("ON (only queue/draft/schedule possible)" if safe else "OFF"),
        "- Gmail send accounts available: " + (", ".join(a["email"] + " (id " + str(a["id"]) + ")" for a in usable) or "none"),
        "- Campaigns: " + (", ".join(str(x["id"]) + " " + x["name"] for x in camps) or "none"),
        "- Templates: " + (", ".join(str(x["id"]) + " " + x["name"] + " [" + ", ".join(json.loads(x["fields"] or "[]")) + "]" for x in tpls) or "none"),
        "- Data sheets: " + (", ".join(str(x["id"]) + " " + x["name"] + " (" + str(x["n_rows"]) + " rows: " + ", ".join(json.loads(x["headers"] or "[]")) + ")" for x in sheets) or "none"),
        "- Email queue counts: " + (", ".join(k + "=" + str(v) for k, v in sorted(st.items())) or "empty"),
        "- Sent: " + str(st.get("sent", 0)) + " | Replied: " + str(replied_n) + " | Bounced: " + str(bounced_n_ctx) + " | Latest sent: " + recent,
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
            cinfo = {}
            cids = [r["contact_id"] for r in rows if r.get("contact_id")]
            if cids:
                ph = ",".join("?" * len(cids))
                for cr in c.execute("SELECT id, hr_name, company FROM contacts WHERE id IN (" + ph + ")", cids):
                    cinfo[cr["id"]] = (cr["hr_name"] or "", cr["company"] or "")
            return {"ok": True, "emails": [
                {"id": r["id"], "serial": r["serial"], "to": r["to_addr"],
                 "who": (cinfo.get(r.get("contact_id"), ("", ""))[0]) or r["to_addr"],
                 "company": cinfo.get(r.get("contact_id"), ("", ""))[1],
                 "subject": r["subject"], "status": r["status"],
                 "scheduled_at": r["scheduled_at"], "sent_at": r["sent_at"],
                 "replied_at": r.get("replied_at") or ""} for r in rows]}
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
            if when < now + timedelta(minutes=5):
                return {"ok": False, "error": "time must be at least 5 minutes in the future"}
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
    return api_err("The Ask AI chat moved to the new drawer - use POST /api/ai/agent.", 410)
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


# ---------------- Ask AI agent (confirmations + uploads) ----------------

def _agent_parse_file(name, data):
    """Classify an uploaded file for the agent: contact sheet (.csv/.xlsx) or template (.docx/.html/.txt)."""
    import io

    class _F:
        def __init__(self, name, data):
            self.filename = name
            self._b = io.BytesIO(data)

        def read(self, *a):
            return self._b.read(*a)

    n = name.lower()
    if n.endswith((".csv", ".xlsx", ".tsv")):
        try:
            headers, rows = _parse_sheet_file(_F(name, data))
        except ValueError as e:
            return {"name": name, "kind": "bad", "error": str(e)}
        if not rows:
            return {"name": name, "kind": "bad", "error": "The file has no data rows."}
        return {"name": name, "kind": "sheet", "headers": headers, "rows": rows, "filename": name}
    if n.endswith(".docx"):
        import tempfile, os
        import docx_convert
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
            tf.write(data)
            path = tf.name
        try:
            html = docx_convert.docx_to_html(path)
        finally:
            os.unlink(path)
        if not html.strip():
            return {"name": name, "kind": "bad", "error": "Could not read that .docx file."}
        return {"name": name, "kind": "template", "html": html}
    if n.endswith((".html", ".htm")):
        return {"name": name, "kind": "template", "html": data.decode("utf-8", "replace")}
    if n.endswith(".txt"):
        import html as _html
        paras = [p.strip() for p in data.decode("utf-8", "replace").splitlines() if p.strip()]
        if not paras:
            return {"name": name, "kind": "bad", "error": "The text file is empty."}
        return {"name": name, "kind": "template",
                "html": "".join("<p>" + _html.escape(x) + "</p>" for x in paras)}
    return {"name": name, "kind": "bad",
            "error": "Unsupported file type. Attach a .csv, .xlsx, .docx, .html or .txt file."}


_card_id = None


def _agent_agent_error(out):
    code = out.get("code") or 502
    return jsonify({"ok": False, "error": out.get("error") or "AI request failed.", "code": code}), code


def _card_file_from_response(response):
    """Parse a file uploaded through a card. Attachments go to filestore;
    contact sheets / templates are parsed server-side and attached as
    response['parsed'] (the raw bytes never reach the model)."""
    f = response.get("file") or {}
    if not (f.get("name") and f.get("data")):
        return True
    try:
        data = base64.b64decode(f["data"])
    except Exception:
        return "Could not read the attached file."
    if len(data) > 5 * 1024 * 1024:
        return "File too large (max 5 MB)."
    card = cards_mod.get_card(_card_id)
    purpose = (card.get("payload") or {}).get("purpose") if card else None
    if purpose == "attachment":
        ext = Path(f["name"]).suffix.lower() or ".bin"
        name = "att_" + uuid.uuid4().hex[:10] + ext
        filestore.put(name, data)
        log("agent_attachment", f["name"], )
        response["file"] = {"name": f["name"], "stored_name": name}
        return True
    parsed = _agent_parse_file(f["name"], data)
    if parsed.get("kind") == "bad":
        return parsed.get("error") or "Could not read that file."
    response["parsed"] = parsed
    response["file"] = {"name": f["name"]}
    return True


@app.route("/api/ai/agent", methods=["POST"])
@login_required
def api_ai_agent():
    import agent as ag
    d = request.get_json(force=True)
    text = (d.get("text") or "").strip()
    file = None
    f = d.get("file") or {}
    if f.get("name") and f.get("data"):
        try:
            data = base64.b64decode(f["data"])
        except Exception:
            return api_err("Could not read the attached file.", 400)
        if len(data) > 5 * 1024 * 1024:
            return api_err("File too large (max 5 MB).", 400)
        file = _agent_parse_file(f["name"], data)
        if file.get("kind") == "bad" and not text:
            return api_err(file.get("error") or "Could not read that file.", 400)
    try:
        out = ag.turn(request.user["id"], conv_id=d.get("conv_id"), text=text or None, file=file)
    except Exception as e:
        return api_err("AI request failed: " + str(e)[:200], 502)
    if not out.get("ok"):
        return _agent_agent_error(out)
    return jsonify(out)


@app.route("/api/ai/cards/<card_id>/respond", methods=["POST"])
@login_required
def api_ai_card_respond(card_id):
    global _card_id
    import agent as ag
    _card_id = card_id
    d = request.get_json(force=True) or {}
    response = d.get("response") or {}
    ok = _card_file_from_response(response)
    if ok is not True:
        return api_err(str(ok), 400)
    out = ag.respond(card_id, request.user["id"], response, d.get("idempotency_key") or "")
    if not out.get("ok"):
        return _agent_agent_error(out)
    return jsonify(out)


@app.route("/api/ai/cards/<card_id>/update", methods=["POST"])
@login_required
def api_ai_card_update(card_id):
    import agent as ag
    d = request.get_json(force=True) or {}
    out = ag.update(card_id, request.user["id"], d.get("fields") or {},
                    d.get("version"), d.get("idempotency_key") or "")
    if not out.get("ok"):
        return _agent_agent_error(out)
    return jsonify(out)


@app.route("/api/ai/cards/<card_id>/cancel", methods=["POST"])
@login_required
def api_ai_card_cancel(card_id):
    import agent as ag
    out = ag.cancel(card_id, request.user["id"])
    if not out.get("ok"):
        return _agent_agent_error(out)
    return jsonify(out)


@app.route("/api/ai/convs/<conv_id>")
@login_required
def api_ai_conv_get(conv_id):
    import agent as ag
    out = ag.restore(conv_id, request.user["id"])
    if not out.get("ok"):
        return _agent_agent_error(out)
    return jsonify(out)


@app.route("/api/ai/convs/<conv_id>/close", methods=["POST"])
@login_required
def api_ai_conv_close(conv_id):
    import agent as ag
    return jsonify(ag.close(conv_id, request.user["id"]))


@app.route("/api/ai/restore")
@login_required
def api_ai_restore():
    import agent as ag
    cid = ag.latest_conv(request.user["id"])
    if not cid:
        return jsonify({"ok": True, "conv_id": None})
    out = ag.restore(cid, request.user["id"])
    if not out.get("ok"):
        return jsonify({"ok": True, "conv_id": None})
    return jsonify(out)


# ---------------- signature ----------------

@app.route("/api/signature")
@login_required
def api_signature_get():
    return jsonify({"ok": True, "html": db.get_setting("signature_html", "")})


@app.route("/api/signature", methods=["POST"])
@login_required
def api_signature_set():
    d = request.get_json(force=True)
    services.set_signature(d.get("html"), request.user)
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
    filestore.put(name, data, ns="images")
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
        data = filestore.get(safe)
        if data is not None:
            ctype = mimetypes.guess_type(safe)[0] or "application/octet-stream"
            out.append((safe, data, ctype))
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
    existing = sum(filestore.size(n) for n in names if filestore.exists(n))
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
        filestore.put(safe, data)
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
        filestore.delete(name)
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
    try:
        services.batch_reminders(bid, d.get("days") or 0, d.get("max") or 1,
                                 int(d.get("template_id") or 0), request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


MEMBER_EXPR = "COALESCE(NULLIF(b.created_by,''), NULLIF(cam.created_by,''), 'team')"


def _reports_rows(status="", q="", member=""):
    c = db.conn()
    sql = ("SELECT e.id, e.serial, e.to_addr, e.subject, e.status, e.sent_at, e.scheduled_at, "
           "e.replied_at, e.bounced_at, e.bounce_reason, e.reminder_count, e.is_reminder, e.error, e.batch_id, "
           "co.company, co.hr_name FROM emails e LEFT JOIN contacts co ON co.id=e.contact_id "
           "LEFT JOIN batches b ON b.id=e.batch_id LEFT JOIN campaigns cam ON cam.id=b.campaign_id "
           "WHERE e.status != 'deleted'")
    args = []
    if member:
        if member == "team":
            sql += " AND " + MEMBER_EXPR + " = 'team'"
        else:
            sql += " AND " + MEMBER_EXPR + " = ?"
            args.append(member)
    if status == "replied":
        sql += " AND e.replied_at IS NOT NULL AND e.replied_at != ''"
    elif status == "bounced":
        sql += " AND e.bounced_at IS NOT NULL AND e.bounced_at != ''"
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


@app.route("/api/reports/members")
@login_required
def api_reports_members():
    c = db.conn()
    users = [dict(r) for r in c.execute(
        "SELECT email, display_name FROM users WHERE status='active' ORDER BY display_name, email").fetchall()]
    creators = [r["member"] for r in c.execute(
        "SELECT DISTINCT " + MEMBER_EXPR + " AS member FROM batches b "
        "LEFT JOIN campaigns cam ON cam.id=b.campaign_id "
        "WHERE COALESCE(NULLIF(b.created_by,''), NULLIF(cam.created_by,'')) IS NOT NULL").fetchall()]
    c.close()
    out = []
    for u in users:
        out.append({"email": u["email"], "name": (u["display_name"] or u["email"]).split("@")[0]})
    for m in sorted(set(creators)):
        if not any(x["email"] == m for x in out):
            out.append({"email": m, "name": m.split("@")[0]})
    return jsonify({"ok": True, "members": out})


@app.route("/api/reports/emails")
@login_required
def api_reports_emails():
    rows = _reports_rows(request.args.get("status") or "", (request.args.get("q") or "").strip(),
                         (request.args.get("member") or "").strip())
    return jsonify({"ok": True, "rows": rows})


@app.route("/api/reports/emails.csv")
@login_required
def api_reports_csv():
    import io
    rows = _reports_rows(request.args.get("status") or "", (request.args.get("q") or "").strip(),
                         (request.args.get("member") or "").strip())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Serial", "Company", "HR Name", "To", "Subject", "Status",
                "Scheduled", "Sent", "Replied", "Bounced", "Bounce reason", "Reminders", "Is Reminder", "Error"])
    for r in rows:
        w.writerow([r["id"], r["serial"], r["company"] or "", r["hr_name"] or "", r["to_addr"],
                    r["subject"], r["status"], r["scheduled_at"] or "", r["sent_at"] or "",
                    r["replied_at"] or "", r["bounced_at"] or "", r["bounce_reason"] or "",
                    r["reminder_count"], "yes" if r["is_reminder"] else "", r["error"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=campusmail_ai_outreach.csv"})


@app.route("/api/reports/summary")
@login_required
def api_reports_summary():
    member = (request.args.get("member") or "").strip()
    msql, margs = "", []
    if member:
        msql = " AND " + MEMBER_EXPR + (" = 'team'" if member == "team" else " = ?")
        if member != "team":
            margs.append(member)
    mj = ("LEFT JOIN batches b ON b.id=e.batch_id LEFT JOIN campaigns cam ON cam.id=b.campaign_id")
    c = db.conn()
    emails = {r["status"]: r["n"] for r in c.execute(
        "SELECT status, COUNT(*) n FROM emails e " + mj + " WHERE is_reminder=0 AND e.status != 'deleted'" + msql,
        margs).fetchall()}
    reminders = c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=1").fetchone()["n"]
    sent_n = c.execute("SELECT COUNT(*) n FROM emails e " + mj + " WHERE is_reminder=0 AND status='sent'" + msql,
                       margs).fetchone()["n"]
    replied_n = c.execute("SELECT COUNT(*) n FROM emails e " + mj +
                          " WHERE is_reminder=0 AND status='sent' AND replied_at IS NOT NULL AND replied_at != ''" + msql,
                          margs).fetchone()["n"]
    bounced_n = c.execute("SELECT COUNT(*) n FROM emails e " + mj +
                          " WHERE is_reminder=0 AND status='sent' AND bounced_at IS NOT NULL AND bounced_at != ''" + msql,
                          margs).fetchone()["n"]
    days = []
    for i in range(13, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        n = c.execute("SELECT COUNT(*) n FROM emails e " + mj +
                      " WHERE status='sent' AND sent_at >= ? AND sent_at < ?" + msql,
                      margs + [d + " 00:00:00", d + " 23:59:59"]).fetchone()["n"]
        days.append({"date": d[5:], "n": n})
    companies = [dict(r) for r in c.execute(
        "SELECT co.company, COUNT(*) total, "
        "SUM(CASE WHEN e.status='sent' THEN 1 ELSE 0 END) sent, "
        "SUM(CASE WHEN e.replied_at IS NOT NULL AND e.replied_at != '' THEN 1 ELSE 0 END) replied, "
        "SUM(CASE WHEN e.bounced_at IS NOT NULL AND e.bounced_at != '' THEN 1 ELSE 0 END) bounced "
        "FROM emails e JOIN contacts co ON co.id = e.contact_id " + mj +
        " WHERE e.is_reminder=0 AND e.status != 'deleted'" + msql +
        " GROUP BY co.company ORDER BY total DESC LIMIT 12", margs).fetchall()]
    members = [dict(r) for r in c.execute(
        "SELECT " + MEMBER_EXPR + " AS member, COUNT(*) total, "
        "SUM(CASE WHEN e.status='sent' THEN 1 ELSE 0 END) sent, "
        "SUM(CASE WHEN e.replied_at IS NOT NULL AND e.replied_at != '' THEN 1 ELSE 0 END) replied, "
        "SUM(CASE WHEN e.bounced_at IS NOT NULL AND e.bounced_at != '' THEN 1 ELSE 0 END) bounced "
        "FROM emails e " + mj + " WHERE e.is_reminder=0 AND e.status != 'deleted' "
        "GROUP BY member ORDER BY sent DESC, total DESC").fetchall()]
    c.close()
    reply_rate = round(100.0 * replied_n / sent_n) if sent_n else 0
    return jsonify({"ok": True, "emails": emails, "reminders": reminders,
                    "sent": sent_n, "replied": replied_n, "bounced": bounced_n, "reply_rate": reply_rate,
                    "days": days, "companies": companies, "members": members})


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
    if "to_addr" in d:
        addr = (d["to_addr"] or "").strip()
        if not CC_RE.match(addr):
            c.close()
            return api_err("Enter a valid email address.")
        cur = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
        if not cur:
            c.close()
            return api_err("Email not found", 404)
        if cur["status"] != "sent" or not cur["bounced_at"]:
            c.close()
            return api_err("Only a bounced email can have its address corrected here.")
        c.execute("UPDATE emails SET to_addr=?, status='pending', bounced_at=NULL, bounce_reason='', "
                  "error='', updated_at=? WHERE id=?", (addr, db.now_iso(), eid))
        if cur["contact_id"]:
            c.execute("UPDATE contacts SET hr_email=?, status='new', updated_at=? WHERE id=?",
                      (addr, db.now_iso(), cur["contact_id"]))
        log("email_update", "Address for email " + str(eid) + " fixed to " + addr)
    if sets:
        args.append(eid)
        c.execute("UPDATE emails SET " + ", ".join(sets) + " WHERE id=?", args)
        log("email_update", "email " + str(eid))
    c.commit()
    c.close()
    return jsonify({"ok": True})


@app.route("/api/emails/<int:eid>/mark-replied", methods=["POST"])
@login_required
def api_email_mark_replied(eid):
    d = request.get_json(force=True)
    on = bool(d.get("replied"))
    try:
        services.email_mark_replied(eid, on, request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
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




# ==================== UI redesign additions (additive only) ====================

def _css_bundle_mtime(path):
    """Max mtime of a CSS file plus everything it @imports (cache-bust the whole bundle)."""
    p = BASE / "static" / path.lstrip("/")
    import re as _re
    try:
        mts = [p.stat().st_mtime]
        for rel in _re.findall(r'@import url\("[^"]+"\)', p.read_text()):
            rel = _re.match(r'@import url\("([^"]+)"\)', rel).group(1)
            try:
                mts.append((p.parent / rel).stat().st_mtime)
            except OSError:
                pass
        return max(mts)
    except OSError:
        return 0


def _static_version(path):
    """Cache-busting query for /static files: ?v=<mtime>.<content-hash>.

    The hash is over the file bytes, so two edits landing in the same
    second still produce distinct version tags (a bare mtime tag lets a
    1-year immutable browser cache serve stale bytes forever)."""
    try:
        if path.lstrip("/").endswith(".css"):
            return "?v=" + str(int(_css_bundle_mtime(path)))
        p = BASE / "static" / path.lstrip("/")
        digest = hashlib.md5(p.read_bytes()).hexdigest()[:10]
        return "?v=%d.%s" % (int(p.stat().st_mtime), digest)
    except OSError:
        return ""


app.jinja_env.globals["sv"] = lambda path: _static_version(path)


@app.route("/static/css/app.css")
def _css_app_bundle():
    """Serve app.css with per-file ?v= on every @import so imported files can't be served stale."""
    import re as _re
    css_dir = BASE / "static" / "css"
    src = (css_dir / "app.css").read_text()

    def _ver(m):
        rel = m.group(1)
        try:
            return '@import url("%s?v=%d");' % (rel, int((css_dir / rel).stat().st_mtime))
        except OSError:
            return m.group(0)

    out = _re.sub(r'@import url\("([^"]+)"\);', _ver, src)
    resp = Response(out, mimetype="text/css")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.after_request
def _static_cache(resp):
    if request.path.startswith("/static/"):
        qs = (request.query_string or b"").decode()
        if "v=" in qs:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            # unversioned assets (e.g. @imported CSS): revalidate via ETag
            resp.headers["Cache-Control"] = "no-cache"
    elif resp.is_json or (resp.content_type or "").startswith("text/html"):
        # the page shell carries the ?v= tags; it must never be cached,
        # otherwise clients keep requesting stale asset versions
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/_kit")
def page_kit():
    """Dev-only component gallery (section 4). No data, no login."""
    return render_template("kit.html")


@app.route("/api/home")
@login_required
def api_home():
    """Attention list + upcoming sends + campaign cards for Home (6.4)."""
    c = db.conn()
    now = datetime.now().astimezone().isoformat()
    out = {}

    # attention: replies waiting, bounced, failed, scheduled within 24h,
    # stale drafts, gmail problems, join requests
    rows = []
    q = c.execute("SELECT COUNT(*) n FROM emails WHERE status='sent' AND replied_at IS NOT NULL "
                  "AND replied_at != '' AND (handled_at IS NULL OR handled_at = '')").fetchone()["n"]
    if q:
        rows.append({"type": "replies", "count": q, "campaign": ""})
    q = c.execute("SELECT COUNT(*) n FROM emails WHERE status='sent' AND bounced_at IS NOT NULL "
                  "AND bounced_at != ''").fetchone()["n"]
    if q:
        rows.append({"type": "bounced", "count": q, "campaign": ""})
    q = c.execute("SELECT COUNT(*) n FROM emails WHERE status='error'").fetchone()["n"]
    if q:
        rows.append({"type": "failed", "count": q, "campaign": ""})
    in24 = (datetime.now() + timedelta(hours=24)).astimezone().isoformat()
    q = c.execute("SELECT COUNT(*) n FROM emails WHERE status='scheduled' AND scheduled_at <= ?",
                  (in24,)).fetchone()["n"]
    if q:
        rows.append({"type": "soon", "count": q, "campaign": ""})
    # stale drafts (created > 2 days ago, still in drafts, nobody sent)
    c2 = db.conn()
    cutoff = (datetime.now() - timedelta(days=2)).astimezone().isoformat()
    q = c2.execute("SELECT COUNT(*) n FROM emails WHERE status='drafted' AND created_at < ?", (cutoff,)).fetchone()["n"]
    c2.close()
    if q:
        rows.append({"type": "stale_drafts", "count": q, "campaign": ""})
    # gmail not connected
    if not any(db.account_token_json(a["id"]) for a in db.list_accounts(request.user["id"], request.user["id"])):
        rows.append({"type": "gmail", "count": 1, "campaign": ""})
    # admin: pending join requests
    if request.user["role"] == "admin":
        c2 = db.conn()
        q = c2.execute("SELECT COUNT(*) n FROM users WHERE status='pending'").fetchone()["n"]
        c2.close()
        if q:
            rows.append({"type": "requests", "count": q, "campaign": ""})
    c.close()
    out["attention"] = rows

    # upcoming sends: next 3 scheduled groups
    c = db.conn()
    up = c.execute("""
        SELECT e.scheduled_at, c.name AS campaign, c.id AS campaign_id, COUNT(*) n
        FROM emails e
        JOIN batches b ON e.batch_id = b.id
        LEFT JOIN campaigns c ON c.id = b.campaign_id
        WHERE e.status = 'scheduled'
        GROUP BY e.scheduled_at, c.id
        ORDER BY e.scheduled_at
        LIMIT 3
    """).fetchall()
    out["upcoming"] = [{"when": r["scheduled_at"], "campaign": r["campaign"] or "No campaign",
                        "campaign_id": r["campaign_id"], "count": r["n"]} for r in up]

    # campaign cards (max 4, newest activity first)
    cams = []
    for cam in c.execute("SELECT * FROM campaigns ORDER BY updated_at DESC, id DESC").fetchall():
        st = {r["status"]: r["n"] for r in c.execute(
            "SELECT e.status, COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
            "WHERE b.campaign_id=? AND e.status != 'deleted' GROUP BY e.status", (cam["id"],))}
        replied = c.execute("SELECT COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
                            "WHERE b.campaign_id=? AND e.status != 'deleted' "
                            "AND e.replied_at IS NOT NULL AND e.replied_at != ''",
                            (cam["id"],)).fetchone()["n"]
        sent = st.get("sent", 0)
        bounced = c.execute("SELECT COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
                            "WHERE b.campaign_id=? AND e.status != 'deleted' "
                            "AND e.bounced_at IS NOT NULL AND e.bounced_at != ''",
                            (cam["id"],)).fetchone()["n"]
        delivered = max(0, sent - bounced)
        total = sum(st.values())
        n_sheets = c.execute("SELECT COUNT(*) n FROM sheets WHERE campaign_id=?", (cam["id"],)).fetchone()["n"]
        contacts = c.execute(
            "SELECT COUNT(*) n FROM contacts ct JOIN sheets s ON ct.sheet_id=s.id WHERE s.campaign_id=?",
            (cam["id"],)).fetchone()["n"]
        if n_sheets == 0:
            contacts = c.execute(
                "SELECT COUNT(DISTINCT e.contact_id) n FROM emails e JOIN batches b ON e.batch_id=b.id "
                "WHERE b.campaign_id=? AND e.status != 'deleted' AND e.contact_id > 0",
                (cam["id"],)).fetchone()["n"]
        cams.append({"id": cam["id"], "name": cam["name"], "description": cam["description"],
                     "n_emails": total, "n_contacts": contacts, "sent": sent,
                     "replied": replied, "bounced": bounced, "delivered": delivered,
                     "rate": round(100.0 * replied / delivered) if delivered else 0,
                     "status_counts": st,
                     "last": cam["updated_at"]})
    c.close()
    out["campaigns"] = cams[:4]

    # result tiles (30 d default window; period filter is client-side cosmetic only
    # until the API grows period params — see CHANGELOG-UI.md)
    c = db.conn()
    tiles = {
        "sent": c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent'").fetchone()["n"],
        "replies": c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent' "
                             "AND replied_at IS NOT NULL AND replied_at != ''").fetchone()["n"],
        "scheduled": c.execute("SELECT COUNT(*) n FROM emails WHERE status='scheduled'").fetchone()["n"],
        "bounced": c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent' "
                             "AND bounced_at IS NOT NULL AND bounced_at != ''").fetchone()["n"],
    }
    tiles["delivered"] = max(0, tiles["sent"] - tiles["bounced"])
    c.close()
    out["tiles"] = {**tiles, "rate": round(100.0 * tiles["replies"] / tiles["delivered"]) if tiles["delivered"] else 0}

    # onboarding progress (6.3)
    c = db.conn()
    ob = {
        "gmail": bool([a for a in db.list_accounts(request.user["id"], request.user["id"])
                       if db.account_token_json(a["id"])]),
        "contacts": c.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"] > 0,
        "templates": c.execute("SELECT COUNT(*) n FROM email_templates").fetchone()["n"] > 0,
        "first_send": c.execute("SELECT COUNT(*) n FROM emails WHERE status != 'deleted'").fetchone()["n"] > 0,
    }
    c.close()
    out["onboard"] = ob
    return jsonify({"ok": True, **out})


@app.route("/api/sheets/practice", methods=["POST"])
@login_required
def api_sheet_practice():
    """Create the 5-row practice list addressed to the signed-in user (6.3)."""
    email = request.user["email"]
    c = db.conn()
    existing = c.execute("SELECT id FROM sheets WHERE name='Practice list' AND campaign_id=0").fetchone()
    if existing:
        sid = existing["id"]
    else:
        c.execute("INSERT INTO sheets(name, campaign_id, filename, headers, rows, n_rows, created_by, created_at) "
                  "VALUES(?,?,?, ?, ?, ?, ?, ?)",
                  ("Practice list", 0, "practice",
                   json.dumps(["company", "HR name", "HR email"]),
                   json.dumps([["Me (practice)", request.user["display_name"] or "Practice", email]] * 5),
                   5, request.user["email"], db.now_iso()))
        sid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    log("practice_list", "created practice list")
    c.close()
    return jsonify({"ok": True, "id": sid})


@app.route("/api/sheets/<int:sid>/campaign", methods=["POST"])
@login_required
def api_sheet_set_campaign(sid):
    """Attach a contact list to a campaign, or remove it (0). Audience tab."""
    d = request.get_json(force=True)
    try:
        services.sheet_set_campaign(sid, int(d.get("campaign_id") or 0), request.user)
    except services.ServiceError as e:
        return api_err(e.message, e.code)
    return jsonify({"ok": True})


@app.route("/api/emails/revert", methods=["POST"])
@login_required
def api_emails_revert():
    """Undo helper: put emails back to the status/times the caller passed."""
    d = request.get_json(force=True)
    out = services.emails_revert(d.get("items") or [], request.user)
    return jsonify({"ok": True, "reverted": out["reverted"]})
# ==================== end UI redesign additions ====================


def bootstrap():
    db.init_db()
    app.secret_key = db.app_secret()
    threading.Thread(target=scheduler_loop, daemon=True).start()


def main():
    bootstrap()
    print()
    print("  CampusMail AI - outreach team")
    print("    http://localhost:5001")
    print()
    app.run(host="127.0.0.1", port=int(os.environ.get("CAMPUSMAIL_AI_PORT", "5001")), debug=False, threaded=True)


if __name__ == "__main__":
    main()
