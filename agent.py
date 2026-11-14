"""Ask AI - agentic assistant.

The LLM can call platform tools. Read-only tools run immediately; every
mutating tool pauses the conversation and asks the user to confirm with a
Yes/No card. The model can also ask the user questions (options or free text)
via ask_user. Uploaded files (contact sheets, .docx/.html templates) are
parsed and offered to the model as context.
"""
import json
import time
import uuid

import ai_client
import db

STATE = {}          # conv_id -> {uid, messages, pending, prompt, file, created}
MAX_ROUNDS = 8
CONV_TTL = 2 * 3600

SYSTEM = """You are the built-in assistant inside CampusMail AI, a placement-team email outreach platform (like a smarter version of the web UI).

Ground rules:
- Use the read tools to get REAL data before answering any question about numbers, campaigns, contacts, replies or templates. Never invent or guess figures.
- You can create and edit campaigns, templates and contact lists, and prepare email sends - but every mutating tool is shown to the user as a confirmation card and only runs if they press "Yes, do it". You do not need to ask again in words; just call the tool.
- When you need a decision, preference or missing detail from the user (names, dates, which template, yes/no on a plan), call ask_user instead of guessing. Use options when the answer is one of a few choices; use free text when you need them to type something.
- If a contact file or template file was attached to the user's message, say what you found in it (columns, row count, or the template text) and offer to use it.
- Be concise and practical: short paragraphs or a few bullet lines. Plain text only (no markdown tables, no headers).
- If something fails, say what happened in one line and suggest the next step.
- You never delete the user's sent history. Deleting a campaign removes its queued/sent email records from the app (Gmail copies are untouched) - mention that in the confirmation when relevant."""


# ---------------------------------------------------------------- tools

def _spec(name, desc, props, required=None, mutating=False):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": required or []}}}


TOOLS_SPEC = [
    _spec("get_overview", "Current platform totals: campaigns, contacts, emails sent, replied, bounced, scheduled, open replies. Call first when the user asks 'how is it going' or for a summary.", {}),
    _spec("list_campaigns", "List all campaigns with id, name, status and email counts.", {}),
    _spec("get_campaign", "Detailed stats for one campaign: sent/replied/bounced/scheduled, linked contact list, batches.",
          {"campaign_id": {"type": "integer"}}, ["campaign_id"]),
    _spec("list_templates", "List email templates with id, name, subject, active flag and personalisation fields.", {}),
    _spec("get_template", "Full template detail including the email body (HTML) and fields.",
          {"template_id": {"type": "integer"}}, ["template_id"]),
    _spec("list_contacts", "Sample of contacts (id, serial, company, person, email, status) plus the total count. Use search to filter.",
          {"search": {"type": "string"}, "limit": {"type": "integer"}}),
    _spec("get_replies", "Emails that received a reply: recipient, serial, reply time.",
          {"limit": {"type": "integer"}}),
    _spec("list_team", "Team members with role and how many emails they sent.", {}),
    _spec("recent_activity", "Latest activity log entries (imports, sends, sync).",
          {"limit": {"type": "integer"}}),
    _spec("create_campaign", "Create a new campaign (empty - no emails yet).",
          {"name": {"type": "string"}, "description": {"type": "string"}}, ["name"], mutating=True),
    _spec("update_campaign", "Rename a campaign and/or change its description.",
          {"campaign_id": {"type": "integer"}, "name": {"type": "string"}, "description": {"type": "string"}},
          ["campaign_id"], mutating=True),
    _spec("delete_campaign", "Delete a campaign. Removes its queued and sent email records from the app; Gmail copies are not touched.",
          {"campaign_id": {"type": "integer"}}, ["campaign_id"], mutating=True),
    _spec("create_template", "Save a new email template. If a template file (.docx/.html/.txt) was attached and body_html is omitted, the file content is used.",
          {"name": {"type": "string"}, "subject": {"type": "string"},
           "body_html": {"type": "string"}, "kind": {"type": "string", "enum": ["outreach", "reminder"]}},
          ["name", "subject"], mutating=True),
    _spec("update_template", "Edit a template's name, subject and/or body.",
          {"template_id": {"type": "integer"}, "name": {"type": "string"}, "subject": {"type": "string"},
           "body_html": {"type": "string"}}, ["template_id"], mutating=True),
    _spec("delete_template", "Delete a template.", {"template_id": {"type": "integer"}}, ["template_id"], mutating=True),
    _spec("activate_template", "Make one template the active one (used by new sends).",
          {"template_id": {"type": "integer"}}, ["template_id"], mutating=True),
    _spec("import_contacts_sheet", "Import the contact file the user attached (CSV/XLSX) as a new contact list, optionally linked to a campaign.",
          {"name": {"type": "string"}, "campaign_id": {"type": "integer"}}),
    _spec("send_test_email", "Send a real test email from the connected Gmail account (e.g. to the user themselves to check signature, attachment rendering, images).",
          {"to": {"type": "string"}, "subject": {"type": "string"}, "body_html": {"type": "string"}},
          ["to", "subject", "body_html"], mutating=True),
    _spec("prepare_send", "Prepare an email send for a campaign: personalises the template for the campaign's contact list. action: 'queue' = build the queue only (nothing sent, not even drafts), 'draft' = create Gmail drafts (nothing sent), 'schedule' = send automatically at 'at', 'send' = send immediately (safe mode must be off).",
          {"campaign_id": {"type": "integer"}, "template_id": {"type": "integer"},
           "action": {"type": "string", "enum": ["queue", "draft", "schedule", "send"]},
           "at": {"type": "string", "description": "Local time 'YYYY-MM-DD HH:MM', required for schedule"},
           "sheet_id": {"type": "integer", "description": "Optional: limit to one contact list"},
           "serial_from": {"type": "integer"}, "serial_to": {"type": "integer"}},
          ["campaign_id", "action"], mutating=True),
    _spec("cancel_emails", "Stop queued or scheduled emails so they will not be sent. Use when the user changes their mind about a send.",
          {"status": {"type": "string", "enum": ["scheduled", "pending", "drafted"]},
           "campaign_id": {"type": "integer", "description": "Optional: limit to one campaign"}},
          ["status"], mutating=True),
    _spec("ask_user", "Ask the user a question and wait for their answer. Provide up to 4 short options when the answer is one of a few choices; the user can also always type their own answer.",
          {"question": {"type": "string"},
           "options": {"type": "array", "items": {"type": "string"}}}, ["question"]),
]

_FIELD_ALIASES = {
    "recruiter_name": ("hr_name", "recruiter", "name", "contact", "person"),
    "company_name": ("company", "organisation", "organization", "org", "firm"),
    "first_name": ("first_name", "hr_name", "name"),
    "organisation_name": ("company", "organisation", "organization", "org"),
    "serial": ("serial", "no", "number"),
}


def _norm(s):
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def _auto_mapping(fields, columns):
    """Map template fields to contact columns: exact/alias/substring matches."""
    cols = [c for c in (columns or []) if c]
    mapping = {}
    for f in fields:
        nf = _norm(f)
        hit = None
        for c in cols:
            if _norm(c) == nf:
                hit = c
                break
        if not hit:
            aliases = tuple(_norm(x) for x in _FIELD_ALIASES.get(f, ()))
            for c in cols:
                if _norm(c) in aliases or (len(nf) > 3 and nf in _norm(c)):
                    hit = c
                    break
        if hit:
            mapping[f] = "column:" + hit
    return mapping


def _fields_of(tpl):
    try:
        return json.loads(tpl.get("fields") or "[]")
    except Exception:
        return []


def _sheet_columns(c, sheet_id):
    if not sheet_id:
        return []
    r = c.execute("SELECT headers FROM sheets WHERE id=?", (sheet_id,)).fetchone()
    if not r:
        return []
    try:
        return json.loads(r["headers"] or "[]")
    except Exception:
        return []


# ------------------------------------------------------- read handlers

def _h_overview(a, conv):
    c = db.conn()
    q = lambda s: c.execute(s).fetchone()[0]
    out = {
        "campaigns": q("SELECT COUNT(*) FROM campaigns"),
        "contacts": q("SELECT COUNT(*) FROM contacts"),
        "emails_total": q("SELECT COUNT(*) FROM emails WHERE status != 'deleted'"),
        "sent": q("SELECT COUNT(*) FROM emails WHERE status='sent'"),
        "replied": q("SELECT COUNT(*) FROM emails WHERE replied_at != '' AND replied_at IS NOT NULL"),
        "bounced": q("SELECT COUNT(*) FROM emails WHERE bounced_at != '' AND bounced_at IS NOT NULL"),
        "scheduled": q("SELECT COUNT(*) FROM emails WHERE status='scheduled'"),
        "queued": q("SELECT COUNT(*) FROM emails WHERE status IN ('pending','drafted')"),
        "templates": q("SELECT COUNT(*) FROM email_templates"),
    }
    c.close()
    return out


def _h_list_campaigns(a, conv):
    c = db.conn()
    rows = []
    for r in c.execute("SELECT * FROM campaigns ORDER BY id"):
        cam = dict(r)
        cam["emails"] = c.execute(
            "SELECT COUNT(*) FROM emails WHERE batch_id IN (SELECT id FROM batches WHERE campaign_id=?) AND status!='deleted'",
            (cam["id"],)).fetchone()[0]
        cam["sent"] = c.execute(
            "SELECT COUNT(*) FROM emails WHERE batch_id IN (SELECT id FROM batches WHERE campaign_id=?) AND status='sent'",
            (cam["id"],)).fetchone()[0]
        rows.append({k: cam.get(k) for k in ("id", "name", "description", "emails", "sent")})
    c.close()
    return {"campaigns": rows}


def _h_get_campaign(a, conv):
    cid = int(a["campaign_id"])
    c = db.conn()
    r = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not r:
        c.close()
        return {"ok": False, "error": "Campaign not found"}
    cam = dict(r)
    out = {"id": cid, "name": cam["name"], "description": cam.get("description") or ""}
    for st in ("sent", "replied", "scheduled", "pending", "drafted", "error"):
        if st == "replied":
            out[st] = c.execute("SELECT COUNT(*) FROM emails WHERE replied_at!='' AND replied_at IS NOT NULL AND batch_id IN (SELECT id FROM batches WHERE campaign_id=?)", (cid,)).fetchone()[0]
        else:
            out[st] = c.execute("SELECT COUNT(*) FROM emails WHERE status=? AND batch_id IN (SELECT id FROM batches WHERE campaign_id=?)", (st, cid)).fetchone()[0]
    out["bounced"] = c.execute("SELECT COUNT(*) FROM emails WHERE bounced_at!='' AND bounced_at IS NOT NULL AND batch_id IN (SELECT id FROM batches WHERE campaign_id=?)", (cid,)).fetchone()[0]
    out["sheets"] = [dict(x) for x in c.execute("SELECT id, name, n_rows FROM sheets WHERE campaign_id=?", (cid,))]
    for s in out["sheets"]:
        s.pop("headers", None)
    out["batches"] = [dict(x) for x in c.execute("SELECT id, name, template_id, created_at FROM batches WHERE campaign_id=?", (cid,))]
    for b in out["batches"]:
        b.pop("mapping", None)
    c.close()
    return out


def _h_list_templates(a, conv):
    c = db.conn()
    rows = []
    for r in c.execute("SELECT * FROM email_templates ORDER BY id"):
        t = dict(r)
        try:
            fields = json.loads(t.get("fields") or "[]")
        except Exception:
            fields = []
        rows.append({"id": t["id"], "name": t["name"], "subject": t["subject"],
                     "active": bool(t["is_active"]), "kind": t.get("kind") or t.get("type"),
                     "fields": fields, "body_chars": len(t.get("body_html") or "")})
    c.close()
    return {"templates": rows}


def _h_get_template(a, conv):
    tid = int(a["template_id"])
    c = db.conn()
    t = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    c.close()
    if not t:
        return {"ok": False, "error": "Template not found"}
    t = dict(t)
    body = t.get("body_html") or ""
    return {"id": tid, "name": t["name"], "subject": t["subject"],
            "active": bool(t["is_active"]), "fields": _fields_of(t),
            "body_html": body[:5000] + ("… (truncated)" if len(body) > 5000 else "")}


def _h_list_contacts(a, conv):
    lim = max(1, min(50, int(a.get("limit") or 20)))
    search = (a.get("search") or "").strip()
    c = db.conn()
    total = c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    if search:
        like = "%" + search + "%"
        rows = c.execute("SELECT id, serial, company, hr_name, hr_email, status FROM contacts "
                         "WHERE company LIKE ? OR hr_name LIKE ? OR hr_email LIKE ? ORDER BY id LIMIT ?",
                         (like, like, like, lim)).fetchall()
    else:
        rows = c.execute("SELECT id, serial, company, hr_name, hr_email, status FROM contacts ORDER BY id LIMIT ?",
                         (lim,)).fetchall()
    c.close()
    return {"total": total, "shown": len(rows), "contacts": [dict(r) for r in rows]}


def _h_get_replies(a, conv):
    lim = max(1, min(50, int(a.get("limit") or 20)))
    c = db.conn()
    total = c.execute("SELECT COUNT(*) FROM emails WHERE replied_at!='' AND replied_at IS NOT NULL").fetchone()[0]
    rows = c.execute("SELECT e.serial, e.to_addr, e.subject, e.replied_at, c.company "
                     "FROM emails e LEFT JOIN contacts c ON c.id=e.contact_id "
                     "WHERE e.replied_at!='' AND e.replied_at IS NOT NULL "
                     "ORDER BY e.replied_at DESC LIMIT ?", (lim,)).fetchall()
    c.close()
    return {"total_replies": total, "replies": [dict(r) for r in rows]}


def _h_list_team(a, conv):
    c = db.conn()
    rows = []
    for r in c.execute("SELECT id, email, display_name, role FROM users WHERE status IS NULL OR status='active' OR status='' ORDER BY id"):
        rows.append({"id": r["id"], "name": r["display_name"] or r["email"], "email": r["email"], "role": r["role"]})
    c.close()
    return {"members": rows}


def _h_recent_activity(a, conv):
    lim = max(1, min(50, int(a.get("limit") or 10)))
    c = db.conn()
    rows = c.execute("SELECT ts, actor, action, detail FROM activity ORDER BY id DESC LIMIT ?", (lim,)).fetchall()
    c.close()
    return {"activity": [dict(r) for r in rows]}


# -------------------------------------------------- mutating handlers

def _h_create_campaign(a, conv):
    name = (a.get("name") or "").strip()
    if not name:
        raise ValueError("Campaign needs a name")
    import app as _app
    c = db.conn()
    c.execute("INSERT INTO campaigns(name, description, created_by, created_at) VALUES(?,?,?,?)",
              (name, (a.get("description") or "").strip(), _app.request.user["email"], db.now_iso()))
    cid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    db.log_activity("campaign_create", name + " (via Ask AI)")
    c.close()
    return {"ok": True, "id": cid, "name": name}


def _h_update_campaign(a, conv):
    cid = int(a["campaign_id"])
    c = db.conn()
    cam = c.execute("SELECT id, name FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not cam:
        c.close()
        raise ValueError("Campaign not found")
    sets, args = [], []
    if a.get("name"):
        sets.append("name=?"); args.append(a["name"].strip())
    if "description" in a:
        sets.append("description=?"); args.append((a.get("description") or "").strip())
    if sets:
        args.append(cid)
        c.execute("UPDATE campaigns SET " + ", ".join(sets) + " WHERE id=?", args)
        c.commit()
        db.log_activity("campaign_update", cam["name"] + " (via Ask AI)")
    c.close()
    return {"ok": True}


def _h_delete_campaign(a, conv):
    cid = int(a["campaign_id"])
    import app as _app
    import sync_relay
    c = db.conn()
    cam = c.execute("SELECT id, name FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not cam:
        c.close()
        raise ValueError("Campaign not found")
    n = c.execute("SELECT COUNT(*) FROM emails WHERE batch_id IN (SELECT id FROM batches WHERE campaign_id=?) AND status!='deleted'", (cid,)).fetchone()[0]
    batch_ids = [r["id"] for r in c.execute("SELECT id FROM batches WHERE campaign_id=?", (cid,))]
    email_ids = [r["id"] for r in c.execute(
        "SELECT id FROM emails WHERE batch_id IN (SELECT id FROM batches WHERE campaign_id=?)", (cid,))]
    c.execute("DELETE FROM emails WHERE batch_id IN (SELECT id FROM batches WHERE campaign_id=?)", (cid,))
    c.execute("DELETE FROM batches WHERE campaign_id=?", (cid,))
    c.execute("UPDATE sheets SET campaign_id=0 WHERE campaign_id=?", (cid,))
    c.execute("DELETE FROM campaigns WHERE id=?", (cid,))
    c.commit()
    db.log_activity("campaign_delete", cam["name"] + " (via Ask AI)")
    sync_relay.record_delete("campaigns", cid)
    for bid in batch_ids:
        sync_relay.record_delete("batches", bid)
    for eid in email_ids:
        sync_relay.record_delete("emails", eid)
    c.close()
    return {"ok": True, "removed_emails": n}


def _h_create_template(a, conv):
    import app as _app
    name = (a.get("name") or "").strip()
    subject = (a.get("subject") or "").strip()
    body = a.get("body_html") or ""
    if not body and conv.get("file", {}).get("kind") == "template":
        body = conv["file"]["html"]
    if not name or not subject:
        raise ValueError("Template needs a name and a subject")
    if not body:
        raise ValueError("No template body - write one or attach a .docx/.html file")
    c = db.conn()
    c.execute("INSERT INTO email_templates(name, type, subject, body_html, fields, is_active, kind, created_at) "
              "VALUES(?,?,?,?,?,0,?,?)",
              (name, "html", subject, body,
               json.dumps(_app.all_fields(subject, body)),
               a.get("kind") or "outreach", db.now_iso()))
    tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    db.log_activity("template_add", name + " (via Ask AI)")
    c.close()
    return {"ok": True, "id": tid, "name": name}


def _h_update_template(a, conv):
    import app as _app
    tid = int(a["template_id"])
    c = db.conn()
    cur = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    if not cur:
        c.close()
        raise ValueError("Template not found")
    sets, args = [], []
    if a.get("name"):
        sets.append("name=?"); args.append(a["name"].strip())
    if a.get("subject"):
        sets.append("subject=?"); args.append(a["subject"].strip())
    if a.get("body_html"):
        sets.append("body_html=?"); args.append(a["body_html"])
    if "subject" in a or "body_html" in a:
        sets.append("fields=?")
        args.append(json.dumps(_app.all_fields(a.get("subject", cur["subject"]),
                                              a.get("body_html", cur["body_html"]))))
    if sets:
        args.append(tid)
        c.execute("UPDATE email_templates SET " + ", ".join(sets) + " WHERE id=?", args)
        c.commit()
        db.log_activity("template_update", cur["name"] + " (via Ask AI)")
    c.close()
    return {"ok": True}


def _h_delete_template(a, conv):
    import sync_relay
    tid = int(a["template_id"])
    c = db.conn()
    t = c.execute("SELECT id, name FROM email_templates WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        raise ValueError("Template not found")
    c.execute("DELETE FROM email_templates WHERE id=?", (tid,))
    c.commit()
    db.log_activity("template_delete", t["name"] + " (via Ask AI)")
    sync_relay.record_delete("email_templates", tid)
    c.close()
    return {"ok": True}


def _h_activate_template(a, conv):
    tid = int(a["template_id"])
    c = db.conn()
    t = c.execute("SELECT id, name FROM email_templates WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        raise ValueError("Template not found")
    c.execute("UPDATE email_templates SET is_active=0")
    c.execute("UPDATE email_templates SET is_active=1 WHERE id=?", (tid,))
    c.commit()
    db.log_activity("template_activate", t["name"] + " (via Ask AI)")
    c.close()
    return {"ok": True}


def _h_import_sheet(a, conv):
    f = conv.get("file") or {}
    if f.get("kind") != "sheet":
        raise ValueError("No contact file is attached to this conversation. Ask the user to attach a .csv or .xlsx.")
    import app as _app
    import re
    headers, rows = f["headers"], f["rows"]
    if not headers or not rows:
        raise ValueError("The attached file has no usable rows.")
    email_col = next((h for h in headers if re.search(r"email|mail", h, re.I)), "")
    comp_col = next((h for h in headers if re.search(r"company|organi[sz]ation|firm", h, re.I)), "")
    name_col = next((h for h in headers if h != comp_col and re.search(r"recruiter|hr|name|person|contact", h, re.I)), "")
    name = (a.get("name") or "").strip() or f.get("name") or "List " + time.strftime("%Y-%m-%d %H:%M")
    campaign_id = int(a.get("campaign_id") or 0)
    c = db.conn()
    if campaign_id and not c.execute("SELECT id FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
        c.close()
        raise ValueError("Campaign %s not found" % campaign_id)
    c.execute("INSERT INTO sheets(name, campaign_id, filename, headers, rows, n_rows, created_by, created_at) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (name, campaign_id, f.get("filename", ""), json.dumps(headers), json.dumps(rows),
               len(rows), _app.request.user["email"], db.now_iso()))
    sid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    no_email = 0
    for i, r in enumerate(rows):
        em = (r.get(email_col) or "").strip()
        if not em:
            no_email += 1
        c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, sheet_id, extra, created_at) "
                  "VALUES(?,?,?,?, 'new', ?, ?, ?)",
                  (str(i + 1), (r.get(comp_col) or "").strip(), (r.get(name_col) or "").strip(), em,
                   sid, json.dumps({"sheet": name}), db.now_iso()))
    c.commit()
    db.log_activity("sheet_upload", name + " - " + str(len(rows)) + " rows (via Ask AI)")
    c.close()
    return {"ok": True, "sheet_id": sid, "rows": len(rows), "no_email": no_email, "name": name}


def _h_send_test(a, conv):
    import app as _app
    import gmail_client
    uid = conv["uid"]
    account_id = int(a.get("account_id") or 0) or last_account(uid)
    _app._require_account(account_id, _app.request.user)
    res = gmail_client.send_with_retry(account_id, a["to"].strip(), a["subject"].strip(), a["body_html"], None, "")
    db.log_activity("test_send", "Test mail to " + a["to"].strip() + " (via Ask AI)")
    return {"ok": True, "message_id": (res or {}).get("message_id", "")}


def _h_prepare_send(a, conv):
    import app as _app
    cid = int(a["campaign_id"])
    c = db.conn()
    cam = c.execute("SELECT id, name FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not cam:
        c.close()
        raise ValueError("Campaign not found")
    tid = int(a.get("template_id") or 0)
    if not tid:
        t = c.execute("SELECT id FROM email_templates WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()
        tid = t["id"] if t else 0
    if not tid:
        c.close()
        raise ValueError("No template found - create or activate one first.")
    tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    sheet_id = int(a.get("sheet_id") or 0)
    if not sheet_id:
        s = c.execute("SELECT id FROM sheets WHERE campaign_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        if s:
            sheet_id = s["id"]
    fields = _fields_of(dict(tpl))
    mapping = _auto_mapping(fields, _sheet_columns(c, sheet_id))
    account_id = last_account(conv["uid"])
    c.close()
    action = a.get("action") or "queue"
    if action not in ("queue", "draft", "schedule", "send"):
        raise ValueError("action must be queue, draft, schedule or send")
    d = {"template_id": tid, "mapping": mapping, "campaign_id": cid,
         "account_id": account_id, "action": "create" if action == "queue" else action,
         "name": cam["name"] + " send " + time.strftime("%Y-%m-%d %H:%M")}
    if a.get("at"):
        d["at"] = a["at"]
    if sheet_id:
        d["sheet_id"] = sheet_id
    if a.get("serial_from"):
        d["serial_from"] = a["serial_from"]
    if a.get("serial_to"):
        d["serial_to"] = a["serial_to"]
    res = _app._run_merge(d)
    data = res.get_json(silent=True) or {}
    if res.status_code != 200:
        raise ValueError(data.get("error") or ("HTTP " + str(res.status_code)))
    return {"ok": True, **{k: data.get(k) for k in ("batch_id", "created", "created_drafts", "failed", "scheduled", "sent", "action") if data.get(k) is not None}}


def _h_cancel_emails(a, conv):
    status = a.get("status")
    if status not in ("scheduled", "pending", "drafted"):
        raise ValueError("status must be scheduled, pending or drafted")
    cid = int(a.get("campaign_id") or 0)
    c = db.conn()
    if cid and not c.execute("SELECT id FROM campaigns WHERE id=?", (cid,)).fetchone():
        c.close()
        raise ValueError("Campaign not found")
    if cid:
        cur = c.execute("UPDATE emails SET status='cancelled' WHERE status=? "
                        "AND batch_id IN (SELECT id FROM batches WHERE campaign_id=?)", (status, cid))
    else:
        cur = c.execute("UPDATE emails SET status='cancelled' WHERE status=?", (status,))
    n = cur.rowcount
    c.commit()
    db.log_activity("emails_cancel", "Cancelled %d %s email(s) (via Ask AI)" % (n, status))
    c.close()
    return {"ok": True, "cancelled": n}


def last_account(uid):
    import app as _app
    try:
        return int(_app.last_account_for(uid) or 0)
    except Exception:
        return 0


HANDLERS = {
    "get_overview": _h_overview, "list_campaigns": _h_list_campaigns,
    "get_campaign": _h_get_campaign, "list_templates": _h_list_templates,
    "get_template": _h_get_template, "list_contacts": _h_list_contacts,
    "get_replies": _h_get_replies, "list_team": _h_list_team,
    "recent_activity": _h_recent_activity,
    "create_campaign": _h_create_campaign, "update_campaign": _h_update_campaign,
    "delete_campaign": _h_delete_campaign, "create_template": _h_create_template,
    "update_template": _h_update_template, "delete_template": _h_delete_template,
    "activate_template": _h_activate_template, "import_contacts_sheet": _h_import_sheet,
    "send_test_email": _h_send_test, "prepare_send": _h_prepare_send,
    "cancel_emails": _h_cancel_emails,
    # ask_user is handled directly in _drive (it becomes a question card)
}

MUTATING = {"create_campaign", "update_campaign", "delete_campaign", "create_template",
            "update_template", "delete_template", "activate_template",
            "import_contacts_sheet", "send_test_email", "prepare_send", "cancel_emails"}

# human-readable confirmations ---------------------------------------

def _describe(tool, args, conv):
    if tool == "create_campaign":
        return "Create campaign \u201c%s\u201d" % args.get("name") + (
            ". " + args["description"] if args.get("description") else "")
    if tool == "update_campaign":
        return "Update campaign #%s%s%s" % (
            args.get("campaign_id"),
            " name \u2192 \u201c%s\u201d" % args["name"] if args.get("name") else "",
            " description \u2192 \u201c%s\u201d" % args["description"] if "description" in args else "")
    if tool == "delete_campaign":
        c = db.conn()
        r = c.execute("SELECT name FROM campaigns WHERE id=?", (int(args.get("campaign_id") or 0),)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM emails WHERE batch_id IN (SELECT id FROM batches WHERE campaign_id=?) AND status!='deleted'",
                      (int(args.get("campaign_id") or 0),)).fetchone()[0]
        c.close()
        return ("Delete campaign \u201c%s\u201d and its %d email record(s) from the app. "
                "Copies already in Gmail are not touched." % ((r["name"] if r else "?"), n))
    if tool == "create_template":
        src = " (from the attached file)" if (not args.get("body_html") and conv.get("file", {}).get("kind") == "template") else ""
        return "Save new template \u201c%s\u201d with subject \u201c%s\u201d%s" % (
            args.get("name"), args.get("subject"), src)
    if tool == "update_template":
        what = [k for k in ("name", "subject", "body_html") if args.get(k) or k == "body_html"]
        return "Update template #%s: %s" % (args.get("template_id"), ", ".join(what))
    if tool == "delete_template":
        c = db.conn()
        r = c.execute("SELECT name FROM email_templates WHERE id=?", (int(args.get("template_id") or 0),)).fetchone()
        c.close()
        return "Delete template \u201c%s\u201d" % (r["name"] if r else "?")
    if tool == "activate_template":
        c = db.conn()
        r = c.execute("SELECT name FROM email_templates WHERE id=?", (int(args.get("template_id") or 0),)).fetchone()
        c.close()
        return "Make \u201c%s\u201d the active template (new sends will use it)" % (r["name"] if r else "?")
    if tool == "import_contacts_sheet":
        f = conv.get("file") or {}
        extra = " and link it to campaign #%s" % args["campaign_id"] if args.get("campaign_id") else ""
        return "Import attached file \u201c%s\u201d as a contact list (%d rows)%s" % (
            f.get("name", "?"), len(f.get("rows") or []), extra)
    if tool == "send_test_email":
        return "Send a real test email to %s with subject \u201c%s\u201d from your connected Gmail account" % (
            args.get("to"), args.get("subject"))
    if tool == "cancel_emails":
        scope = " in campaign #%s" % args["campaign_id"] if args.get("campaign_id") else " (all campaigns)"
        return "Cancel all %s email(s)%s - they will not be sent. Already-sent emails are unaffected." % (
            args.get("status"), scope)
    if tool == "prepare_send":
        act = args.get("action") or "draft"
        c = db.conn()
        cam = c.execute("SELECT name FROM campaigns WHERE id=?", (int(args.get("campaign_id") or 0),)).fetchone()
        sheet_id = int(args.get("sheet_id") or 0)
        if not sheet_id:
            s = c.execute("SELECT id FROM sheets WHERE campaign_id=? ORDER BY id DESC LIMIT 1",
                          (int(args.get("campaign_id") or 0),)).fetchone()
            sheet_id = s["id"] if s else 0
        if sheet_id:
            n = c.execute("SELECT COUNT(*) FROM contacts WHERE sheet_id=?", (sheet_id,)).fetchone()[0]
        else:
            n = c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        c.close()
        verb = {"draft": "Create %d Gmail draft(s) for campaign \u201c%s\u201d (nothing is sent)" % (n, cam["name"] if cam else "?"),
                "schedule": "Schedule %d email(s) for campaign \u201c%s\u201d to send at %s" % (n, cam["name"] if cam else "?", args.get("at") or "the given time"),
                "send": "Send %d email(s) for campaign \u201c%s\u201d IMMEDIATELY" % (n, cam["name"] if cam else "?")}
        return verb.get(act, "Prepare send for campaign")
    return "Run %s with %s" % (tool, json.dumps(args)[:160])


def _summarize(tool, args, result):
    if isinstance(result, dict):
        if result.get("ok") is False:
            return tool + " failed: " + str(result.get("error"))[:120]
        return tool + " \u2713"
    return tool + " \u2713"


# ------------------------------------------------------------- driver

def _new_conv(uid):
    _gc()
    cid = uuid.uuid4().hex[:16]
    STATE[cid] = {"uid": uid, "created": time.time(),
                  "messages": [{"role": "system", "content": SYSTEM}],
                  "pending": None, "prompt": None, "file": None}
    return cid


def _gc():
    now = time.time()
    for k in [k for k, v in STATE.items() if now - v["created"] > CONV_TTL]:
        STATE.pop(k, None)


def _get(conv_id, uid):
    conv = STATE.get(conv_id)
    if not conv or conv["uid"] != uid:
        raise KeyError("conversation")
    return conv


def _append_tool_result(conv, call_id, result):
    conv["messages"].append({"role": "tool", "tool_call_id": call_id,
                             "content": json.dumps(result, default=str)[:4000]})


def _drive(conv, user_text=None, confirm=None, answer=None):
    """Run the tool loop until there is a reply or a pending user decision."""
    events = []
    if confirm is not None:
        pend = conv["pending"]
        conv["pending"] = None
        if pend:
            if confirm:
                try:
                    result = HANDLERS[pend["tool"]](pend["args"], conv)
                    result = {"ok": True, **result}
                except Exception as e:
                    result = {"ok": False, "error": str(e)[:300]}
                _append_tool_result(conv, pend["call_id"], result)
                events.append({"tool": pend["tool"], "summary": _summarize(pend["tool"], pend["args"], result)})
            else:
                _append_tool_result(conv, pend["call_id"],
                                    {"ok": False, "error": "The user declined this action. Do not retry it; ask what they would like instead."})
                events.append({"tool": pend["tool"], "summary": pend["tool"] + " - declined by user"})
    elif answer is not None:
        pr = conv["prompt"]
        conv["prompt"] = None
        if pr:
            _append_tool_result(conv, pr["call_id"], {"answer": answer})
            events.append({"tool": "ask_user", "summary": "user answered: " + str(answer)[:120]})
    elif user_text is not None:
        conv["messages"].append({"role": "user", "content": user_text})

    for _ in range(MAX_ROUNDS):
        msg = ai_client.chat_raw(conv["messages"], max_tokens=1500, temperature=0.3, tools=TOOLS_SPEC)
        conv["messages"].append(msg)
        tcs = msg.get("tool_calls") or []
        if not tcs:
            return {"reply": (msg.get("content") or "").strip(), "events": events}
        stop = None
        responded = set()
        for tc in tcs:
            cid_ = tc.get("id")
            fname = (tc.get("function") or {}).get("name") or ""
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except Exception:
                args = {}
            if fname == "ask_user":
                conv["prompt"] = {"call_id": cid_,
                                  "question": (args.get("question") or "Could you tell me more?").strip(),
                                  "options": [str(o) for o in (args.get("options") or [])][:4]}
                stop = {"prompt": conv["prompt"]}
                break
            if not fname or fname not in HANDLERS:
                _append_tool_result(conv, cid_, {"ok": False, "error": "Unknown tool"})
                responded.add(cid_)
                continue
            if fname in MUTATING:
                conv["pending"] = {"call_id": cid_, "tool": fname, "args": args,
                                   "description": _describe(fname, args, conv)}
                stop = {"pending": conv["pending"]}
                break
            try:
                result = HANDLERS[fname](args, conv)
                result = {"ok": True, **result}
            except Exception as e:
                result = {"ok": False, "error": str(e)[:300]}
            _append_tool_result(conv, cid_, result)
            responded.add(cid_)
            events.append({"tool": fname, "summary": _summarize(fname, args, result)})
        if stop:
            for tc in tcs:
                if tc.get("id") not in responded and tc.get("id") != (stop.get("pending") or stop.get("prompt") or {}).get("call_id"):
                    _append_tool_result(conv, tc.get("id"),
                                        {"ok": False, "error": "Skipped - waiting for user confirmation."})
            out = {"events": events}
            out.update(stop)
            return out
    conv["messages"].append({"role": "assistant",
                             "content": "I hit my step limit on that one. Could you break it into smaller steps?"})
    return {"reply": "I hit my step limit on that one. Could you break it into smaller steps?", "events": events}


def turn(uid, conv_id=None, text=None, file=None, confirm=None, answer=None):
    """One user turn. Returns dict: conv_id, reply?, events?, pending?, prompt?, error?"""
    if not ai_client.ai_configured():
        return {"error": "AI is not configured. Add an API key in Settings \u2192 AI features first."}
    if conv_id:
        try:
            conv = _get(conv_id, uid)
        except KeyError:
            conv = None
    else:
        conv = None
    if conv is None:
        if confirm is not None or answer is not None:
            return {"error": "That conversation expired - please ask again."}
        conv_id = _new_conv(uid)
        conv = STATE[conv_id]
    if text is not None and (conv.get("pending") or conv.get("prompt")):
        return {"conv_id": conv_id,
                "error": "I am still waiting on the question/confirmation shown above - answer that first."}
    if file:
        conv["file"] = file
        extra = ""
        if file.get("kind") == "sheet":
            extra = ("[Attached file %s - contact list with %d rows, columns: %s. "
                     "Tell the user what you found; if they want it imported, call import_contacts_sheet.]"
                     % (file.get("name"), len(file.get("rows") or []), ", ".join(file.get("headers") or [])[:300]))
        elif file.get("kind") == "template":
            extra = ("[Attached file %s - converted to HTML, %d chars. It starts: %s"
                     " If the user wants to save it, call create_template and leave body_html empty to use this file.]"
                     % (file.get("name"), len(file.get("html") or ""),
                        (file.get("html") or "")[:300].replace("\n", " ")))
        if text:
            text = text + "\n\n" + extra
        else:
            text = "I attached a file. " + extra
    out = _drive(conv, user_text=text, confirm=confirm, answer=answer)
    out["conv_id"] = conv_id
    return out
