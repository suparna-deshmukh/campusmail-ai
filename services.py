"""Shared service layer for CampusMail AI.

Every state change - from the UI routes and from Ask AI actions - goes
through these functions, so sync side effects, activity logging, safe mode,
daily caps, schedule windows and role checks stay consistent.

Pure helpers (field filling, rendering, CC parsing) live here too; app.py
imports them back so the two layers can never drift.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta

import db
import docx_convert

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class ServiceError(Exception):
    """Validation / business-rule failure. .code maps to an HTTP status."""

    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


def actor_email(actor):
    return actor["email"] if actor else "system"


# ------------------------------------------------------------------ fields

def fill_fields(text, mapping, row_values):
    def _repl(m):
        name = next(g for g in m.groups() if g).strip()
        val = mapping.get(name, "")
        if val.startswith("column:"):
            return str(row_values.get(val[len("column:"):], ""))
        if val.startswith("literal:"):
            return val[len("literal:"):]
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


def row_vals(r):
    """Contact row + any extra sheet columns, so template fields can map to them."""
    vals = dict(r)
    try:
        ex = json.loads(r["extra"] or "{}")
    except Exception:
        ex = {}
    for k, v in (ex.get("row") or {}).items():
        vals.setdefault(k, v)
    return vals


def render_email(tpl, row, mapping):
    """THE single renderer: subject + body for one contact. Used by previews,
    final review, test emails and the send/draft/schedule executors."""
    subject = fill_fields(tpl["subject"], mapping, dict(row))
    body = fill_fields(tpl["body_html"], mapping, dict(row))
    return subject, body


def open_tracking_body(body_html, email_id, ns=""):
    """Inject a 1x1 open-tracking pixel for a campaign send. No-op when
    tracking is off or no public base URL is configured (pixel must be
    reachable from the recipient's mail client, so it always points at the
    public/web instance)."""
    if (db.get_setting("track_opens", "1") or "") == "0":
        return body_html
    import gmail_client
    url = gmail_client.open_pixel_url(db.get_setting("public_base", ""), email_id,
                                      ns or db.open_tenant_ns())
    if not url:
        return body_html
    return gmail_client.inject_open_pixel(body_html, url)


def signature_html():
    return db.get_setting("signature_html", "")


def append_signature(body):
    sig = signature_html()
    if sig:
        body += "<p style='margin-top:14px'>" + sig + "</p>"
    return body


def render_hash(to_addr, subject, body_html, cc, attachment_names, account_id):
    """Stable hash of one reviewed recipient (spec 7.6)."""
    h = hashlib.sha256()
    for part in (to_addr or "", subject or "", body_html or "",
                 ",".join(sorted(cc or [])) or "",
                 ",".join(sorted(attachment_names or [])) or "",
                 str(account_id or 0)):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()


def template_content_hash(tpl):
    h = hashlib.sha256()
    h.update((tpl.get("subject") or "").encode("utf-8", "replace"))
    h.update(b"\x1f")
    h.update((tpl.get("body_html") or "").encode("utf-8", "replace"))
    return h.hexdigest()


def parse_cc(raw):
    out = []
    for part in (raw or "").replace(";", ",").split(","):
        a = part.strip()
        if not a:
            continue
        if "{" in a or "}" in a:
            # per-row merge field in CC - validated later per row
            if a.lower() not in [x.lower() for x in out]:
                out.append(a)
            continue
        if not EMAIL_RE.match(a):
            raise ServiceError("Invalid CC address: " + a)
        if a.lower() not in [x.lower() for x in out]:
            out.append(a)
    return out


def cc_of(batch):
    if batch and batch.get("cc"):
        try:
            return parse_cc(batch["cc"]) or None
        except ServiceError:
            return None
    return None


def load_attachments(batch):
    if not batch:
        return []
    import mimetypes
    import filestore
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


def sent_at_utc_sql(col="e.sent_at"):
    """SQL expression normalizing mixed sent_at formats to UTC:
    legacy rows are UTC ISO with 'T'/'Z'; current rows are local
    'YYYY-MM-DD HH:MM:SS'. Compare the result against UTC values only."""
    from datetime import datetime
    off = int(datetime.now().astimezone().utcoffset().total_seconds() // 60)
    return ("CASE WHEN INSTR(%s, 'T') > 0 THEN datetime(%s) "
            "ELSE datetime(%s, '%d minutes') END" % (col, col, col, -off))


def require_account(account_id, user):
    acc = db.account_for(user["id"], account_id)
    if not acc:
        raise ServiceError("Send account not found or not available to you.", 409)
    if not db.account_token_json(account_id):
        raise ServiceError("That Gmail account is not connected yet (Settings, Gmail accounts).", 409)
    return acc


def schedule_window_error(when, now=None):
    """None if OK, else ServiceError. now injected for tests."""
    now = now or datetime.now().astimezone()
    if when < now + timedelta(minutes=5):
        raise ServiceError("Schedule time must be at least 5 minutes in the future.")
    if when > now + timedelta(days=45):
        raise ServiceError("Schedule up to 45 days ahead only.")
    return None


def parse_when(when_str, min_lead_minutes=5, max_days=45, now=None):
    now = now or datetime.now().astimezone()
    when_str = (when_str or "").strip()
    try:
        when = datetime.strptime(when_str, "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
    except ValueError:
        raise ServiceError("Time format must be YYYY-MM-DD HH:MM (local time).")
    if min_lead_minutes:
        schedule_window_error(when, now)
    elif when > now + timedelta(days=max_days or 45):
        raise ServiceError("Schedule up to 45 days ahead only.")
    return when


def cloud_push_email(email_id, status=None):
    import app as _app
    try:
        _app._cloud_push_email(email_id, status)
    except Exception:
        pass


# ---------------------------------------------------------------- campaigns

def campaign_create(name, description="", actor=None):
    name = (name or "").strip()
    if not name:
        raise ServiceError("Give the campaign a name.")
    c = db.conn()
    c.execute("INSERT INTO campaigns(name, description, created_by, created_at, updated_at) "
              "VALUES(?,?,?,?,?)",
              (name, (description or "").strip(), actor_email(actor), db.now_iso(), db.now_iso()))
    cid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    db.log_activity("campaign_create", name, actor_email(actor))
    c.close()
    return {"id": cid, "name": name}


def campaign_update(cid, name=None, description=None, actor=None):
    cid = int(cid)
    c = db.conn()
    cam = c.execute("SELECT id, name FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not cam:
        c.close()
        raise ServiceError("Campaign not found", 404)
    sets, args = [], []
    if name is not None:
        name = name.strip()
        if not name:
            c.close()
            raise ServiceError("Give the campaign a name.")
        sets.append("name=?"); args.append(name)
    if description is not None:
        sets.append("description=?"); args.append(description.strip())
    if sets:
        # bump the sync rev so the change propagates to the other instance
        sets.append("updated_at=?"); args.append(db.now_iso())
        sets.append("updated_at_ms=?"); args.append(int(time.time() * 1000))
        args.append(cid)
        c.execute("UPDATE campaigns SET " + ", ".join(sets) + " WHERE id=?", args)
        c.commit()
        db.log_activity("campaign_update", cam["name"], actor_email(actor))
    c.close()
    return {"ok": True}


def campaign_impact(cid):
    """Counts for confirm cards: batches + email records removed."""
    cid = int(cid)
    c = db.conn()
    cam = c.execute("SELECT id, name FROM campaigns WHERE id=?", (cid,)).fetchone()
    n = c.execute("SELECT COUNT(*) n FROM emails WHERE batch_id IN "
                  "(SELECT id FROM batches WHERE campaign_id=?) AND status != 'deleted'",
                  (cid,)).fetchone()["n"]
    nb = c.execute("SELECT COUNT(*) n FROM batches WHERE campaign_id=?", (cid,)).fetchone()["n"]
    c.close()
    if not cam:
        raise ServiceError("Campaign not found", 404)
    return {"name": cam["name"], "batches": nb, "emails": n}


def campaign_delete(cid, actor=None):
    cid = int(cid)
    import sync_relay
    c = db.conn()
    cam = c.execute("SELECT id, name FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not cam:
        c.close()
        raise ServiceError("Campaign not found", 404)
    batch_ids = [r["id"] for r in c.execute("SELECT id FROM batches WHERE campaign_id=?", (cid,))]
    email_ids = [r["id"] for r in c.execute(
        "SELECT id FROM emails WHERE batch_id IN (SELECT id FROM batches WHERE campaign_id=?)", (cid,))]
    c.execute("DELETE FROM emails WHERE batch_id IN (SELECT id FROM batches WHERE campaign_id=?)", (cid,))
    c.execute("DELETE FROM batches WHERE campaign_id=?", (cid,))
    c.execute("UPDATE sheets SET campaign_id=0 WHERE campaign_id=?", (cid,))
    c.execute("DELETE FROM campaigns WHERE id=?", (cid,))
    c.commit()
    sync_relay.record_delete("campaigns", cid)
    for bid in batch_ids:
        sync_relay.record_delete("batches", bid)
    for eid in email_ids:
        sync_relay.record_delete("emails", eid)
    db.log_activity("campaign_delete", cam["name"], actor_email(actor))
    c.close()
    return {"ok": True, "removed_emails": len(email_ids), "removed_batches": len(batch_ids)}


# ---------------------------------------------------------------- templates

def template_create(name, subject, body_html, kind="outreach", actor=None, active=False):
    name = (name or "").strip()
    subject = (subject or "").strip()
    body = (body_html or "").strip()
    if not name or not subject:
        raise ServiceError("Template needs a name and a subject")
    if not body:
        raise ServiceError("No template body - write one or attach a file")
    c = db.conn()
    c.execute("INSERT INTO email_templates(name, type, subject, body_html, fields, is_active, kind, created_at, updated_at) "
              "VALUES(?,?,?,?,?,?,?,?,'')",
              (name, "html", subject, body, json.dumps(all_fields(subject, body)),
               1 if active else 0, kind or "outreach", db.now_iso()))
    if active:
        c.execute("UPDATE email_templates SET is_active=0")
        c.execute("UPDATE email_templates SET is_active=1 WHERE id=?", (name and 0 or 0,))
    tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    db.log_activity("template_add", name, actor_email(actor))
    c.close()
    return {"id": tid, "name": name, "subject": subject, "fields": all_fields(subject, body)}


def template_update(tid, name=None, subject=None, body_html=None, actor=None):
    tid = int(tid)
    c = db.conn()
    cur = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    if not cur:
        c.close()
        raise ServiceError("Template not found", 404)
    cur = dict(cur)
    sets, args = [], []
    if name is not None:
        if not name.strip():
            c.close()
            raise ServiceError("Template needs a name")
        sets.append("name=?"); args.append(name.strip())
    if subject is not None:
        sets.append("subject=?"); args.append(subject.strip())
    if body_html is not None:
        sets.append("body_html=?"); args.append(body_html)
    if subject is not None or body_html is not None:
        sets.append("fields=?")
        args.append(json.dumps(all_fields(
            subject.strip() if subject is not None else cur["subject"],
            body_html if body_html is not None else cur["body_html"])))
    if sets:
        sets.append("updated_at=?"); args.append(db.now_iso())
        sets.append("updated_at_ms=?"); args.append(int(time.time() * 1000))
        args.append(tid)
        c.execute("UPDATE email_templates SET " + ", ".join(sets) + " WHERE id=?", args)
        c.commit()
        db.log_activity("template_update", cur["name"], actor_email(actor))
    c.close()
    return {"ok": True}


def template_delete_impact(tid):
    tid = int(tid)
    c = db.conn()
    t = c.execute("SELECT id, name FROM email_templates WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        raise ServiceError("Template not found", 404)
    n_batches = c.execute("SELECT COUNT(*) n FROM batches WHERE template_id=? OR reminder_template_id=?",
                          (tid, tid)).fetchone()["n"]
    c.close()
    return {"name": t["name"], "used_by_batches": n_batches}


def template_delete(tid, actor=None):
    tid = int(tid)
    import sync_relay
    c = db.conn()
    t = c.execute("SELECT id, name FROM email_templates WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        raise ServiceError("Template not found", 404)
    c.execute("DELETE FROM email_templates WHERE id=?", (tid,))
    c.commit()
    sync_relay.record_delete("email_templates", tid)
    db.log_activity("template_delete", t["name"], actor_email(actor))
    c.close()
    return {"ok": True}


def template_activate(tid, actor=None):
    tid = int(tid)
    c = db.conn()
    t = c.execute("SELECT id, name FROM email_templates WHERE id=?", (tid,)).fetchone()
    if not t:
        c.close()
        raise ServiceError("Template not found", 404)
    prev = c.execute("SELECT id, name FROM email_templates WHERE is_active=1 AND id != ?", (tid,)).fetchone()
    c.execute("UPDATE email_templates SET is_active=0")
    c.execute("UPDATE email_templates SET is_active=1 WHERE id=?", (tid,))
    c.commit()
    db.log_activity("template_activate", t["name"] +
                    (" (was " + prev["name"] + ")" if prev else ""), actor_email(actor))
    c.close()
    return {"ok": True, "name": t["name"], "deactivated": prev["name"] if prev else None}


# ------------------------------------------------------------------- sheets

def sheet_upload(name, campaign_id, headers, rows, filename="", actor=None):
    name = (name or "").strip() or "List " + datetime.now().strftime("%Y-%m-%d %H:%M")
    campaign_id = int(campaign_id or 0)
    c = db.conn()
    if campaign_id and not c.execute("SELECT id FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
        c.close()
        raise ServiceError("Campaign not found", 404)
    c.execute("INSERT INTO sheets(name, campaign_id, filename, headers, rows, n_rows, created_by, created_at, updated_at) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (name, campaign_id, filename or "", json.dumps(headers), json.dumps(rows),
               len(rows), actor_email(actor), db.now_iso(), db.now_iso()))
    sid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    email_col = next((h for h in headers if re.search(r"email|mail", h, re.I)), "")
    comp_col = next((h for h in headers if re.search(r"company|organi[sz]ation|firm", h, re.I)), "")
    name_col = next((h for h in headers if h != comp_col and re.search(r"recruiter|hr|name|person|contact", h, re.I)), "")
    no_email = duplicates = 0
    seen = set()
    for i, r in enumerate(rows):
        em = (r.get(email_col) or "").strip()
        if not em:
            no_email += 1
        elif em.lower() in seen:
            duplicates += 1
        seen.add(em.lower())
        c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, sheet_id, extra, created_at, updated_at) "
                  "VALUES(?,?,?,?, 'new', ?, ?, ?, '')",
                  (str(i + 1), (r.get(comp_col) or "").strip(), (r.get(name_col) or "").strip(), em,
                   sid, json.dumps({"sheet": name, "row": r}), db.now_iso()))
    c.commit()
    db.log_activity("sheet_upload", "%s - %d rows" % (name, len(rows)), actor_email(actor))
    c.close()
    return {"sheet_id": sid, "rows": len(rows), "no_email": no_email,
            "duplicates": duplicates, "name": name, "email_column": email_col}


def sheet_delete_blocked_reason(sid):
    """If a live batch still uses this sheet, explain why it cannot be deleted."""
    sid = int(sid)
    c = db.conn()
    used = c.execute("SELECT COUNT(*) n FROM batches WHERE sheet_id=?", (sid,)).fetchone()["n"]
    n_contacts = c.execute("SELECT COUNT(*) n FROM contacts WHERE sheet_id=?", (sid,)).fetchone()["n"]
    c.close()
    if used:
        return {"blocked": True, "reason": "%d batch(es) still use this list" % used, "rows": n_contacts}
    return {"blocked": False, "rows": n_contacts}


def sheet_delete(sid, actor=None):
    sid = int(sid)
    import sync_relay
    c = db.conn()
    s = c.execute("SELECT id, name, n_rows FROM sheets WHERE id=?", (sid,)).fetchone()
    if not s:
        c.close()
        raise ServiceError("List not found", 404)
    used = c.execute("SELECT COUNT(*) n FROM batches WHERE sheet_id=?", (sid,)).fetchone()["n"]
    if used:
        c.close()
        raise ServiceError("This list is used by %d batch(es). Cancel or delete those batches first." % used, 409)
    c.execute("DELETE FROM contacts WHERE sheet_id=?", (sid,))
    c.execute("DELETE FROM sheets WHERE id=?", (sid,))
    c.commit()
    sync_relay.record_delete("sheets", sid)
    db.log_activity("sheet_delete", s["name"], actor_email(actor))
    c.close()
    return {"ok": True, "removed_rows": s["n_rows"]}


def sheet_set_campaign(sid, campaign_id, actor=None):
    sid, campaign_id = int(sid), int(campaign_id or 0)
    c = db.conn()
    if not c.execute("SELECT id FROM sheets WHERE id=?", (sid,)).fetchone():
        c.close()
        raise ServiceError("List not found", 404)
    if campaign_id and not c.execute("SELECT id FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
        c.close()
        raise ServiceError("Campaign not found", 404)
    c.execute("UPDATE sheets SET campaign_id=? WHERE id=?", (campaign_id, sid))
    c.commit()
    db.log_activity("sheet_campaign", "Sheet %d moved to campaign %d" % (sid, campaign_id), actor_email(actor))
    c.close()
    return {"ok": True}


def sheet_rename(sid, name, actor=None):
    sid = int(sid)
    c = db.conn()
    s = c.execute("SELECT id, name FROM sheets WHERE id=?", (sid,)).fetchone()
    if not s:
        c.close()
        raise ServiceError("List not found", 404)
    if not (name or "").strip():
        c.close()
        raise ServiceError("Give the list a name.")
    c.execute("UPDATE sheets SET name=? WHERE id=?", (name.strip(), sid))
    c.commit()
    db.log_activity("sheet_rename", "%s -> %s" % (s["name"], name.strip()), actor_email(actor))
    c.close()
    return {"ok": True}


def campaign_recipient_count(campaign_id):
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM contacts ct JOIN sheets s ON ct.sheet_id=s.id "
                  "WHERE s.campaign_id=?", (int(campaign_id),)).fetchone()["n"]
    c.close()
    return n


# ----------------------------------------------------------------- contacts

def contact_update(cid, company=None, hr_name=None, hr_email=None, status=None, notes=None, actor=None):
    cid = int(cid)
    c = db.conn()
    cur = c.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
    if not cur:
        c.close()
        raise ServiceError("Contact not found", 404)
    sets, args = [], []
    for col, val in (("company", company), ("hr_name", hr_name), ("hr_email", hr_email),
                     ("status", status), ("notes", notes)):
        if val is not None:
            if col == "hr_email" and val and not EMAIL_RE.match(val.strip()):
                c.close()
                raise ServiceError("Invalid email address: " + val)
            sets.append(col + "=?"); args.append(val.strip() if isinstance(val, str) else val)
    if sets:
        sets.append("updated_at=?"); args.append(db.now_iso())
        args.append(cid)
        c.execute("UPDATE contacts SET " + ", ".join(sets) + " WHERE id=?", args)
        c.commit()
        db.log_activity("contact_update", "contact %d" % cid, actor_email(actor))
    c.close()
    return {"ok": True}


def contacts_reset_status(ids, status, actor=None):
    """e.g. bounced -> 'new' after the address was fixed (ready to re-send)."""
    ids = [int(i) for i in ids]
    if not ids:
        return {"ok": True, "updated": 0}
    ph = ",".join("?" * len(ids))
    c = db.conn()
    cur = c.execute("UPDATE contacts SET status=?, updated_at=? WHERE id IN (%s) AND status != 'replied'" % ph,
                    [status, db.now_iso()] + ids)
    c.commit()
    n = cur.rowcount
    db.log_activity("contact_status", "%d contact(s) -> %s" % (n, status), actor_email(actor))
    c.close()
    return {"ok": True, "updated": n}


# ------------------------------------------------------------------- merge

def audience_rows(sheet_id, serial_from, serial_to, contact_ids=None):
    """The exact contacts a send will cover (same order as the send path)."""
    c = db.conn()
    w = " AND sheet_id=?" if sheet_id else ""
    args = [int(sheet_id)] if sheet_id else []
    if serial_from:
        w += " AND CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER) >= ?"
        args.append(int(serial_from))
    if serial_to:
        w += " AND CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER) <= ?"
        args.append(int(serial_to))
    if contact_ids:
        ph = ",".join("?" * len(contact_ids))
        w += " AND id IN (%s)" % ph
        args += [int(i) for i in contact_ids]
    rows = c.execute("SELECT * FROM contacts WHERE 1=1" + w +
                     " ORDER BY CAST(COALESCE(NULLIF(serial,''),'0') AS INTEGER), id", args).fetchall()
    c.close()
    return [dict(r) for r in rows]


def merge(d, actor, now=None):
    """The single batch-creation pipeline (was app._run_merge). d keys:
    template_id, mapping, name, account_id, campaign_id, sheet_id,
    serial_from, serial_to, contact_ids, cc, append_signature,
    action in queue|create|draft|schedule|send, at, stagger.
    `internal_hold=True` (never from client/tool) defers the start of a
    'send' batch by the undo window (spec 7.7)."""
    import app as _app
    tid = d.get("template_id")
    mapping = d.get("mapping") or {}
    name = (d.get("name") or "").strip() or "Batch " + datetime.now().strftime("%Y-%m-%d %H:%M")
    account_id = int(d.get("account_id") or 0)
    campaign_id = int(d.get("campaign_id") or 0)
    sheet_id = int(d.get("sheet_id") or 0)
    action = d.get("action") or "queue"
    if action not in ("queue", "create", "draft", "schedule", "send"):
        raise ServiceError("Unknown action.")
    try:
        cc_list = parse_cc(d.get("cc"))
    except ServiceError as e:
        raise
    if len(cc_list) > 20:
        raise ServiceError("Up to 20 CC addresses.")
    sfrom, sto = d.get("serial_from"), d.get("serial_to")
    if (sfrom or sto) and int(sfrom or 0) > int(sto or 999999):
        raise ServiceError("Serial range start is after its end.")
    when, stagger = None, 0
    hold_seconds = int(d.get("internal_hold_seconds") or 0)
    if action == "schedule":
        stagger = int(d.get("stagger") or 0)
        when = parse_when(d.get("at"))
    if action in ("draft", "schedule", "send"):
        require_account(account_id, actor)
        if action == "send" and db.get_setting("safe_mode") == "1" and not d.get("internal_hold"):
            raise ServiceError("Safe mode is ON - immediate sending is blocked. Pick Drafts or Schedule, or turn off Safe mode in Settings.", 409)
    c = db.conn()
    tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (tid,)).fetchone()
    if not tpl:
        c.close()
        raise ServiceError("Template not found", 404)
    rows = audience_rows(sheet_id, sfrom, sto, d.get("contact_ids"))
    if not rows:
        c.close()
        raise ServiceError("No contacts in that selection.", 404)
    append_sig = 1 if d.get("append_signature") else 0
    sig = signature_html() if append_sig else ""
    cc_str = ", ".join(cc_list)
    c.execute("INSERT INTO batches(name, template_id, mapping, account_id, created_at, append_signature, cc, campaign_id, sheet_id, created_by) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)",
              (name, tid, json.dumps(mapping), account_id, db.now_iso(), append_sig, cc_str, campaign_id, sheet_id,
               actor_email(actor)))
    bid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    eids = []
    for r in rows:
        subject, body = render_email(dict(tpl), row_vals(r), mapping)
        if sig:
            body = append_signature(body)
        c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, account_id, status) "
                  "VALUES(?,?,?,?,?,?,?,'pending')",
                  (bid, r["id"], r["serial"], r["hr_email"], subject, body, account_id))
        eid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        # embed the open-tracking pixel at creation so EVERY path (send now,
        # schedule, draft, later reminder) ships a trackable body
        tracked = open_tracking_body(body, eid)
        if tracked != body:
            c.execute("UPDATE emails SET body_html=? WHERE id=?", (tracked, eid))
        eids.append(eid)
    c.commit()
    if action == "create":
        db.log_activity("batch_create", "Batch '" + name + "' - " + str(len(rows)) + " emails queued (nothing sent)",
                        actor_email(actor))
        c.close()
        return {"ok": True, "batch_id": bid, "created": len(rows), "action": "create"}
    extra = {}
    if action == "send" and hold_seconds > 0:
        # Undo window (spec 7.7): hold as scheduled with release_at = now + hold.
        import math
        hold_seconds = max(0, min(600, hold_seconds))
        base = datetime.now().astimezone()
        for i, eid in enumerate(eids):
            t = base + timedelta(minutes=stagger * i, seconds=hold_seconds)
            c.execute("UPDATE emails SET status='scheduled', account_id=?, scheduled_at=?, error='' WHERE id=?",
                      (account_id, t.isoformat(), eid))
            c.execute("UPDATE contacts SET status='scheduled' WHERE id=(SELECT contact_id FROM emails WHERE id=?)", (eid,))
        c.commit()
        _record_undo_hold(c, bid, base + timedelta(seconds=hold_seconds), actor["id"])
        db.log_activity("batch_hold", "Batch '%s' - %d emails held for undo (%d s)" % (name, len(rows), hold_seconds),
                        actor_email(actor))
        extra.update(scheduled=len(eids), holding=True,
                     release_at=(base + timedelta(seconds=hold_seconds)).strftime("%Y-%m-%d %H:%M:%S"),
                     last=(base + timedelta(minutes=stagger * max(0, len(eids) - 1), seconds=hold_seconds)).isoformat())
        c.close()
        _schedule_release_timer(base + timedelta(seconds=hold_seconds))
        for eid in eids:
            cloud_push_email(eid, "scheduled")
        return {"ok": True, "batch_id": bid, "created": len(rows), **extra}
    if action == "schedule":
        for i, eid in enumerate(eids):
            t = when + timedelta(minutes=stagger * i)
            c.execute("UPDATE emails SET status='scheduled', account_id=?, scheduled_at=?, error='' WHERE id=?",
                      (account_id, t.isoformat(), eid))
            c.execute("UPDATE contacts SET status='scheduled' WHERE id=(SELECT contact_id FROM emails WHERE id=?)", (eid,))
        c.commit()
        for eid in eids:
            cloud_push_email(eid, "scheduled")
        extra.update(scheduled=len(eids),
                     last=(when + timedelta(minutes=stagger * max(0, len(eids) - 1))).isoformat())
    else:
        import gmail_client
        if action == "draft":
            made = failed = 0
            for eid in eids:
                r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
                try:
                    batch = c.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
                    attach = load_attachments(dict(batch)) if batch else []
                    gmail_client.create_draft(account_id, r["to_addr"], r["subject"],
                                              open_tracking_body(r["body_html"], eid),
                                              attach or None, cc_of(dict(batch)))
                    c.execute("UPDATE emails SET status='drafted', account_id=?, error='' WHERE id=?", (account_id, eid))
                    c.execute("UPDATE contacts SET status='drafted' WHERE id=?", (r["contact_id"],))
                    made += 1
                    cloud_push_email(eid, "drafted")
                except Exception as e:
                    c.execute("UPDATE emails SET status='error', error=? WHERE id=?", (str(e)[:300], eid))
                    failed += 1
            c.commit()
            extra.update(created_drafts=made, failed=failed)
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
                    attach = load_attachments(dict(batch)) if batch else []
                    res = gmail_client.send_with_retry(account_id, r["to_addr"], r["subject"],
                                                       open_tracking_body(r["body_html"], eid),
                                                       attach or None, cc_of(dict(batch)))
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
            extra.update(sent=ok, failed=fail, capped=ok < len(eids) and (sent_today + ok) >= cap)
    _app.save_last_account(actor["id"], account_id) if account_id else None
    db.log_activity("batch_create", "Batch '" + name + "' - " + str(len(rows)) + " emails from template " + str(tid)
                    + " (action " + action + ", cc " + str(len(cc_list)) + ")", actor_email(actor))
    c.close()
    return {"ok": True, "batch_id": bid, "created": len(rows), **extra}


def _record_undo_hold(c, bid, release_at, uid):
    c.execute("INSERT OR REPLACE INTO undo_holds(batch_id, release_at, user_id, created_at) VALUES(?,?,?,?)",
              (int(bid), release_at.strftime("%Y-%m-%d %H:%M:%S"), int(uid), db.now_iso()))
    c.commit()


_release_timers = set()


def _schedule_release_timer(release_at):
    """One-shot: nudge a scheduler tick the moment a held batch releases."""
    import threading
    delay = max(0.5, (release_at - datetime.now().astimezone()).total_seconds())
    t = threading.Timer(delay, _fire_release_tick)
    t.daemon = True
    t.start()
    _release_timers.add(t)
    try:
        t.add_done_callback(_release_timers.discard)
    except AttributeError:
        pass  # Python < 3.12: no add_done_callback; set stays small (timers are finite)


def _fire_release_tick():
    import app as _app
    try:
        with db.team_context():
            _app.scheduler_tick()
    except Exception:
        pass


def held_count():
    """Unreleased undo-hold emails count against today's cap (spec 7.7.4)."""
    c = db.conn()
    now = datetime.now().astimezone().isoformat()
    n = c.execute("SELECT COUNT(*) n FROM emails e JOIN undo_holds u ON u.batch_id = e.batch_id "
                  "WHERE e.status = 'scheduled' AND u.release_at > ? AND e.scheduled_at >= ?",
                  (now, now)).fetchone()["n"]
    # held rows whose scheduled_at is before now (staggered, not yet due) also reserve
    n2 = c.execute("SELECT COUNT(*) n FROM emails e JOIN undo_holds u ON u.batch_id = e.batch_id "
                   "WHERE e.status = 'scheduled' AND u.release_at > ? AND e.scheduled_at < ?",
                   (now, now)).fetchone()["n"]
    c.close()
    return n + n2


def undo_hold_for_batch(bid):
    c = db.conn()
    r = c.execute("SELECT * FROM undo_holds WHERE batch_id=?", (int(bid),)).fetchone()
    c.close()
    return dict(r) if r else None


def undo_send(bid, uid, actor=None):
    """Spec 7.7: one-click, idempotent, compare-and-set cancel of a held batch.
    Only succeeds while now < release_at and the emails are still held."""
    import app as _app
    c = db.conn()
    hold = c.execute("SELECT * FROM undo_holds WHERE batch_id=?", (int(bid),)).fetchone()
    if not hold:
        c.close()
        raise ServiceError("Already released - mail that has been sent can't be recalled from here.", 409)
    try:
        release_at = datetime.strptime(hold["release_at"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=datetime.now().astimezone().tzinfo)
    except ValueError:
        c.close()
        raise ServiceError("Already released - mail that has been sent can't be recalled from here.", 409)
    if datetime.now().astimezone() >= release_at:
        c.execute("DELETE FROM undo_holds WHERE batch_id=?", (int(bid),))
        c.commit()
        c.close()
        raise ServiceError("Already released - mail that has been sent can't be recalled from here.", 409)
    cur = c.execute("UPDATE emails SET status='cancelled', scheduled_at=NULL, error='' "
                    "WHERE batch_id=? AND status='scheduled'", (int(bid),))
    n = cur.rowcount
    c.execute("DELETE FROM undo_holds WHERE batch_id=?", (int(bid),))
    c.commit()
    db.log_activity("send_undone", "Batch %s - %d email(s) cancelled before release" % (bid, n),
                    actor_email(actor) or "user")
    c.close()
    for r in db.conn().execute("SELECT id FROM emails WHERE batch_id=? AND status='cancelled' ORDER BY id", (int(bid),)).fetchall():
        _app._cloud_push_email(r["id"], "cancelled")
    return {"ok": True, "cancelled": n, "undone": True}


def sends_today(include_held=True):
    c = db.conn()
    local_today = datetime.now().strftime("%Y-%m-%d")
    # test/self mails (is_test=1) never count against the daily cap
    n = c.execute("SELECT COUNT(*) n FROM emails WHERE COALESCE(is_test,0)=0 AND status='sent' AND sent_at >= ?",
                  (local_today + " 00:00:00",)).fetchone()["n"]
    c.close()
    if include_held:
        n += held_count()
    return n


def stop_remaining(bid, uid, actor=None):
    """Cancel unsent staggered/scheduled emails of a batch this user released.
    Risk-reducing: no confirm card (spec 7.7.6)."""
    import app as _app
    c = db.conn()
    mine = c.execute("SELECT COUNT(*) n FROM emails WHERE batch_id=? AND status='sent'", (int(bid),)).fetchone()["n"]
    cur = c.execute("UPDATE emails SET status='cancelled', scheduled_at=NULL, error='' "
                    "WHERE batch_id=? AND status IN ('scheduled','pending')", (int(bid),))
    n = cur.rowcount
    c.execute("DELETE FROM undo_holds WHERE batch_id=?", (int(bid),))
    c.commit()
    c.close()
    db.log_activity("emails_stopped", "Batch %s - %d remaining email(s) stopped (%d already sent)" % (bid, n, mine),
                    actor_email(actor) or "user")
    for r in db.conn().execute("SELECT id FROM emails WHERE batch_id=? AND status='cancelled' ORDER BY id", (int(bid),)).fetchall():
        _app._cloud_push_email(r["id"], "cancelled")
    return {"ok": True, "stopped": n, "sent": mine}


# ------------------------------------------------------- existing-row actions

def _ids_from(d, email_rows_func):
    ids = d.get("ids")
    if ids:
        return [int(i) for i in ids]
    rows = email_rows_func(d)
    return [r["id"] for r in rows if r["status"] not in ("sent", "deleted")]


def emails_draft(ids, account_id, actor):
    import gmail_client
    account_id = int(account_id)
    require_account(account_id, actor)
    c = db.conn()
    made = failed = 0
    for eid in ids:
        r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
        if not r or r["status"] == "deleted":
            continue
        try:
            batch = c.execute("SELECT * FROM batches WHERE id=?", (r["batch_id"],)).fetchone()
            attach = load_attachments(dict(batch)) if batch else []
            gmail_client.create_draft(account_id, r["to_addr"], r["subject"], r["body_html"],
                                      attach or None, cc_of(dict(batch)))
            c.execute("UPDATE emails SET status='drafted', account_id=?, error='' WHERE id=?", (account_id, eid))
            c.execute("UPDATE contacts SET status='drafted' WHERE id=?", (r["contact_id"],))
            made += 1
            cloud_push_email(eid, "drafted")
        except Exception as e:
            c.execute("UPDATE emails SET status='error', error=? WHERE id=?", (str(e)[:300], eid))
            failed += 1
    c.commit()
    db.log_activity("drafts_create", "%d drafts (%d failed) from account %d" % (made, failed, account_id),
                    actor_email(actor))
    c.close()
    return {"created": made, "failed": failed}


def emails_schedule(ids, at, stagger, account_id, actor):
    account_id = int(account_id)
    require_account(account_id, actor)
    when = parse_when(at)
    stagger = int(stagger or 0)
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
        cloud_push_email(eid, "scheduled")
    db.log_activity("schedule", "%d emails scheduled for %s (stagger %d min, account %d)" % (n, at, stagger, account_id),
                    actor_email(actor))
    c.close()
    return {"scheduled": n, "first": when.isoformat(),
            "last": (when + timedelta(minutes=stagger * max(0, len(ids) - 1))).isoformat()}


def emails_send_now(ids, account_id, actor):
    import gmail_client
    if db.get_setting("safe_mode") == "1":
        raise ServiceError("Safe mode is ON. Turn it off in Settings to allow immediate sending.", 409)
    account_id = int(account_id)
    require_account(account_id, actor)
    cap = int(db.get_setting("max_sends_per_day") or 100)
    sent_today = sends_today()
    if sent_today >= cap:
        raise ServiceError("Daily cap reached (%d)." % cap, 409)
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
            attach = load_attachments(dict(batch)) if batch else []
            res = gmail_client.send_with_retry(account_id, r["to_addr"], r["subject"], r["body_html"],
                                               attach or None, cc_of(dict(batch)))
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
    db.log_activity("send_now", "%d sent now (%d failed) from account %d" % (ok, fail, account_id),
                    actor_email(actor))
    c.close()
    return {"sent": ok, "failed": fail}


def emails_cancel(ids, actor=None):
    c = db.conn()
    n = 0
    for i in ids:
        r = c.execute("SELECT id, status FROM emails WHERE id=?", (int(i),)).fetchone()
        if r and r["status"] in ("scheduled", "pending"):
            c.execute("UPDATE emails SET status='cancelled', scheduled_at=NULL, error='' WHERE id=?", (i,))
            n += 1
    c.commit()
    db.log_activity("emails_cancel", "Stopped %d email(s)" % n, actor_email(actor))
    c.close()
    for i in ids:
        cloud_push_email(int(i), "cancelled")
    return {"cancelled": n}


def emails_delete(ids, actor=None):
    c = db.conn()
    c.executemany("UPDATE emails SET status='deleted', scheduled_at=NULL WHERE id=?", [(int(i),) for i in ids])
    c.commit()
    db.log_activity("emails_delete", "Removed %d email(s) from queue" % len(ids), actor_email(actor))
    c.close()
    for i in ids:
        cloud_push_email(int(i), "deleted")
    return {"removed": len(ids)}


def emails_revert(items, actor=None):
    c = db.conn()
    n = 0
    for it in (items or [])[:1000]:
        try:
            c.execute("UPDATE emails SET status=?, scheduled_at=?, error='' WHERE id=? "
                      "AND status IN ('pending','scheduled','cancelled')",
                      (it.get("status"), it.get("scheduled_at"), int(it.get("id") or 0)))
            n += 1
        except (TypeError, ValueError):
            continue
    c.commit()
    db.log_activity("emails_revert", "Reverted %d email(s)" % n, actor_email(actor))
    c.close()
    return {"reverted": n}


# ------------------------------------------------------------------- batches

def batch_reminders(bid, days, maxr, template_id, actor=None):
    days = int(days or 0)
    maxr = max(1, min(5, int(maxr or 1)))
    template_id = int(template_id or 0)
    if days and not (0 < days <= 90):
        raise ServiceError("Reminder delay must be 1-90 days.")
    c = db.conn()
    if template_id and not c.execute("SELECT id FROM email_templates WHERE id=?", (template_id,)).fetchone():
        c.close()
        raise ServiceError("Reminder template not found.", 404)
    b = c.execute("SELECT id FROM batches WHERE id=?", (int(bid),)).fetchone()
    if not b:
        c.close()
        raise ServiceError("Batch not found", 404)
    c.execute("UPDATE batches SET reminder_days=?, reminder_template_id=?, reminder_max=? WHERE id=?",
              (days, template_id, maxr, int(bid)))
    c.commit()
    db.log_activity("batch_reminders", "batch %d: every %d day(s), max %d" % (bid, days, maxr), actor_email(actor))
    c.close()
    return {"ok": True}


def batch_attachments_add(bid, files, actor=None):
    import filestore
    from app import ALLOWED_ATTACH_EXT, MAX_ATTACH_PER_BATCH, MAX_ATTACH_TOTAL
    c = db.conn()
    b = c.execute("SELECT * FROM batches WHERE id=?", (int(bid),)).fetchone()
    c.close()
    if not b:
        raise ServiceError("Batch not found", 404)
    b = dict(b)
    try:
        names = json.loads(b.get("attachments") or "[]")
    except Exception:
        names = []
    if len(names) + len(files) > MAX_ATTACH_PER_BATCH:
        raise ServiceError("Maximum %d attachments per batch." % MAX_ATTACH_PER_BATCH)
    existing = sum(filestore.size(n) for n in names if filestore.exists(n))
    added = 0
    for f in files:
        fname = f.get("name") or f.get("filename") or "file"
        data = f["data"]
        ext = os.path.splitext(fname)[1].lower()
        if ext not in ALLOWED_ATTACH_EXT:
            raise ServiceError("File type not allowed: " + fname)
        existing += len(data)
        if existing > MAX_ATTACH_TOTAL:
            raise ServiceError("Total attachments must stay under 15 MB per batch.")
        safe = "b" + str(bid) + "_" + re.sub(r"[^A-Za-z0-9._\-]", "_", os.path.basename(fname))
        filestore.put(safe, data)
        names.append(safe)
        added += 1
    c = db.conn()
    c.execute("UPDATE batches SET attachments=? WHERE id=?", (json.dumps(names), int(bid)))
    c.commit()
    c.close()
    db.log_activity("batch_attachments", "batch %d: +%d file(s)" % (bid, added), actor_email(actor))
    return {"count": len(names)}


def batch_attachments_remove(bid, name, actor=None):
    import filestore
    name = re.sub(r"[^A-Za-z0-9._\-]", "_", str(name or ""))
    c = db.conn()
    b = c.execute("SELECT * FROM batches WHERE id=?", (int(bid),)).fetchone()
    c.close()
    if not b:
        raise ServiceError("Batch not found", 404)
    try:
        names = json.loads(b["attachments"] or "[]")
    except Exception:
        names = []
    if name in names:
        names.remove(name)
        filestore.delete(name)
    c = db.conn()
    c.execute("UPDATE batches SET attachments=? WHERE id=?", (json.dumps(names), int(bid)))
    c.commit()
    c.close()
    db.log_activity("batch_attachments", "batch %d: removed " + name, actor_email(actor))
    return {"count": len(names)}


# -------------------------------------------------------------------- replies

def email_handled(eid, handled, actor=None):
    c = db.conn()
    r = c.execute("SELECT * FROM emails WHERE id=?", (int(eid),)).fetchone()
    if not r:
        c.close()
        raise ServiceError("Email not found", 404)
    if handled:
        c.execute("UPDATE emails SET handled_at=?, updated_at=? WHERE id=?", (db.now_iso(), db.now_iso(), int(eid)))
        verb = "Marked handled"
    else:
        c.execute("UPDATE emails SET handled_at=NULL, updated_at=? WHERE id=?", (db.now_iso(), int(eid)))
        verb = "Reopened"
    c.commit()
    db.log_activity("handled", verb + " email %d" % int(eid), actor_email(actor))
    c.close()
    return {"ok": True}


def email_snooze(eid, until, actor=None):
    """until: ISO string or 'YYYY-MM-DD HH:MM' local. Non-synced local table."""
    c = db.conn()
    r = c.execute("SELECT id FROM emails WHERE id=?", (int(eid),)).fetchone()
    if not r:
        c.close()
        raise ServiceError("Email not found", 404)
    c.execute("INSERT INTO email_snoozes(email_id, snoozed_until, snoozed_by, updated_at) VALUES(?,?,?,?) "
              "ON CONFLICT(email_id) DO UPDATE SET snoozed_until=excluded.snoozed_until,"
              " snoozed_by=excluded.snoozed_by, updated_at=excluded.updated_at",
              (int(eid), until, actor_email(actor), db.now_iso()))
    c.commit()
    db.log_activity("snoozed", "Email %d snoozed until %s" % (eid, until), actor_email(actor))
    c.close()
    return {"ok": True}


def email_unsnooze(eid, actor=None):
    c = db.conn()
    cur = c.execute("DELETE FROM email_snoozes WHERE email_id=?", (int(eid),))
    c.commit()
    n = cur.rowcount
    c.close()
    return {"ok": True, "unsnoozed": n}


def snoozed_ids():
    c = db.conn()
    rows = [r["email_id"] for r in c.execute("SELECT email_id FROM email_snoozes").fetchall()]
    c.close()
    return rows


def email_mark_replied(eid, on=True, actor=None):
    c = db.conn()
    r = c.execute("SELECT * FROM emails WHERE id=?", (int(eid),)).fetchone()
    if not r:
        c.close()
        raise ServiceError("Email not found", 404)
    if on:
        c.execute("UPDATE emails SET replied_at=?, updated_at=? WHERE id=?", (db.now_iso(), db.now_iso(), int(eid)))
        if r["contact_id"]:
            c.execute("UPDATE contacts SET status='replied' WHERE id=?", (r["contact_id"],))
    else:
        c.execute("UPDATE emails SET replied_at=NULL, updated_at=? WHERE id=?", (db.now_iso(), int(eid)))
    c.commit()
    db.log_activity("mark_replied" if on else "mark_not_replied",
                    "Email %d %s manually" % (int(eid), "marked replied" if on else "marked not replied"),
                    actor_email(actor))
    c.close()
    return {"ok": True}


def email_mark_opened(eid, on=True, actor=None):
    """Manually mark a mail as read (opened) or not. Mirrors email_mark_replied;
    lets a user record a read they saw outside the platform (e.g. on their
    phone) or correct a false positive. First open sets opened_at and counts 1;
    un-mark clears it."""
    c = db.conn()
    r = c.execute("SELECT * FROM emails WHERE id=?", (int(eid),)).fetchone()
    if not r:
        c.close()
        raise ServiceError("Email not found", 404)
    if on:
        c.execute("UPDATE emails SET opened_at=?, open_count=COALESCE(open_count,0)+1, updated_at=? WHERE id=?",
                  (db.now_iso(), db.now_iso(), int(eid)))
    else:
        c.execute("UPDATE emails SET opened_at=NULL, open_count=0, updated_at=? WHERE id=?",
                  (db.now_iso(), int(eid)))
    c.commit()
    db.log_activity("mark_opened" if on else "mark_not_opened",
                    "Email %d %s manually" % (int(eid), "marked read" if on else "marked not read"),
                    actor_email(actor))
    c.close()
    return {"ok": True}


def email_reply(eid, body, account_id, as_draft=False, actor=None):
    """Send (or draft, in safe mode) a reply to the recipient of email eid."""
    import gmail_client
    body = (body or "").strip()
    if not body:
        raise ServiceError("Write the reply first.")
    c = db.conn()
    r = c.execute("SELECT * FROM emails WHERE id=?", (int(eid),)).fetchone()
    if not r:
        c.close()
        raise ServiceError("Email not found", 404)
    r = dict(r)
    subject = ("Re: " if not r["subject"].lower().startswith("re:") else "") + r["subject"]
    # threaded reply: point at the original send's Message-Id (F-42 "threading preserved")
    mid = (r.get("message_id") or "").strip()
    require_account(int(account_id), actor)
    batch = c.execute("SELECT * FROM batches WHERE id=?", (r["batch_id"],)).fetchone()
    batch = dict(batch) if batch else None
    if as_draft or db.get_setting("safe_mode") == "1":
        gmail_client.create_draft(int(account_id), r["to_addr"], subject,
                                  "<p>" + body.replace("\n", "<br>") + "</p>", None, cc_of(batch),
                                  in_reply_to=mid or None, references=[mid] if mid else None)
        db.log_activity("reply_draft", "Reply draft for email %d" % int(eid), actor_email(actor))
        return {"drafted": True}
    gmail_client.send_with_retry(int(account_id), r["to_addr"], subject,
                                 "<p>" + body.replace("\n", "<br>") + "</p>", None, cc_of(batch),
                                 in_reply_to=mid or None, references=[mid] if mid else None)
    db.log_activity("reply_sent", "Manual reply sent for email %d" % int(eid), actor_email(actor))
    return {"sent": True}


# ------------------------------------------------------------------- test mail

def test_email_send(account_id, to, subject, body_html, actor, template_id=None, content_hash=None):
    """Send a rendered test email (spec 11.7). In safe mode it becomes a Gmail
    draft. Records a durable test-send when tied to a template version.

    Every test mail also becomes a real, trackable email row (is_test=1) with
    the open-tracking pixel injected, so "send me a test" exercises exactly
    the same read-receipt path as a real send. Test rows never count toward
    campaign/reports analytics or the daily cap."""
    import gmail_client
    require_account(int(account_id), actor)
    c = db.conn()
    c.execute("INSERT INTO batches(name, campaign_id, template_id, mapping, account_id, created_at) "
              "VALUES(?, 0, 0, '{}', ?, ?)", ("test mail", int(account_id), db.now_iso()))
    bid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, account_id, "
              "status, is_test, created_at) VALUES(?, 0, 'T', ?, ?, ?, ?, 'pending', 1, ?)",
              (bid, to, subject, body_html, int(account_id), db.now_iso()))
    eid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    tracked = open_tracking_body(body_html, eid)
    if tracked != body_html:
        c.execute("UPDATE emails SET body_html=? WHERE id=?", (tracked, eid))
    c.commit()
    c.close()
    if db.get_setting("safe_mode") == "1":
        gmail_client.create_draft(int(account_id), to, "[Test] " + subject, tracked)
        mode = "draft"
        c = db.conn()
        c.execute("UPDATE emails SET status='drafted', error='' WHERE id=?", (eid,))
        c.commit(); c.close()
    else:
        res = gmail_client.send_with_retry(int(account_id), to, "[Test] " + subject, tracked)
        mid = res.get("message_id", "") if isinstance(res, dict) else ""
        mode = "sent"
        c = db.conn()
        c.execute("UPDATE emails SET status='sent', sent_at=?, message_id=? WHERE id=?",
                  (db.now_iso(), mid, eid))
        c.commit(); c.close()
        cloud_push_email(eid, "sent")
    db.log_activity("test_email_%s" % mode, "[Test] %s -> %s (email %d)" % (subject[:60], to, eid), actor_email(actor))
    if template_id and content_hash:
        c = db.conn()
        c.execute("INSERT INTO template_test_sends(template_id, content_hash, user_id, sent_at) VALUES(?,?,?,?)",
                  (int(template_id), content_hash, int(actor["id"]), db.now_iso()))
        c.commit()
        c.close()
    return {"ok": True, "mode": mode, "email_id": eid}


def template_tested(template_id, content_hash):
    c = db.conn()
    r = c.execute("SELECT 1 FROM template_test_sends WHERE template_id=? AND content_hash=? LIMIT 1",
                  (int(template_id), content_hash)).fetchone()
    c.close()
    return bool(r)


# ------------------------------------------------------------------ settings

ADMIN_ONLY_KEYS = {"ai_api_key", "ai_api_key_2", "google_client_id", "google_client_secret", "client_secret_json",
                   "admin_email", "cloud_relay_secret", "brand_team_name", "brand_tagline",
                   "supabase_service_key",
                   "ai_daily_calls_per_user", "ai_daily_tokens_per_user", "ai_daily_calls_workspace"}
VALID_KEYS = {"safe_mode", "max_sends_per_day", "default_stagger_min", "catchup_policy",
              "ai_base_url", "ai_model", "ai_enabled", "cloud_relay_url",
              "storage_backend", "supabase_url", "supabase_bucket",
              "send_undo_seconds", "bulk_confirm_threshold",
              "ai_daily_calls_per_user", "ai_daily_tokens_per_user", "ai_daily_calls_workspace",
              "presend_test_suggest_min", "public_base", "track_opens"} | ADMIN_ONLY_KEYS


def send_rail_settings():
    """Sending rails with ranges (spec 7.7/7.8). Returns dict of current values."""
    return {
        "safe_mode": db.get_setting("safe_mode", "1"),
        "max_sends_per_day": int(db.get_setting("max_sends_per_day") or 100),
        "default_stagger_min": int(db.get_setting("default_stagger_min") or 0),
        "catchup_policy": db.get_setting("catchup_policy", "send_now"),
        "send_undo_seconds": int(db.get_setting("send_undo_seconds") or 20),
        "bulk_confirm_threshold": int(db.get_setting("bulk_confirm_threshold") or 50),
    }


def validate_send_rail(key, value):
    v = str(value).strip()

    def _int():
        try:
            return int(v)
        except ValueError:
            raise ServiceError("A whole number is required for this setting.")

    if key == "safe_mode":
        if v not in ("0", "1"):
            raise ServiceError("safe_mode must be 0 or 1.")
    elif key == "max_sends_per_day":
        n = _int()
        if not (1 <= n <= 100000):
            raise ServiceError("Daily cap must be 1-100000.")
    elif key == "default_stagger_min":
        n = _int()
        if not (0 <= n <= 600):
            raise ServiceError("Default stagger must be 0-600 minutes.")
    elif key == "catchup_policy":
        if v not in ("send_now", "mark_missed"):
            raise ServiceError("Catch-up policy must be send_now or mark_missed.")
    elif key == "send_undo_seconds":
        n = _int()
        if not (10 <= n <= 30):
            raise ServiceError("Undo window must be 10-30 seconds.")
    elif key == "bulk_confirm_threshold":
        n = _int()
        if not (10 <= n <= 500):
            raise ServiceError("Bulk threshold must be 10-500.")
    elif key == "ai_daily_calls_per_user" or key == "ai_daily_calls_workspace":
        n = _int()
        if n < 0:
            raise ServiceError("Budget must be 0 (unlimited) or more.")
    elif key == "ai_daily_tokens_per_user":
        n = _int()
        if n < 0:
            raise ServiceError("Token budget must be 0 (unlimited) or more.")
    elif key == "delivery_verify_hours":
        n = _int()
        if not (0 <= n <= 168):
            raise ServiceError("Delivery verification window must be 0-168 hours.")
    return v


def settings_update(user, values):
    """Role-aware settings write (same rules as the UI endpoint)."""
    for k, v in (values or {}).items():
        if k not in VALID_KEYS:
            continue  # unknown keys ignored, as today
        if k in ADMIN_ONLY_KEYS and user["role"] != "admin":
            raise ServiceError("Admin only.", 403)
        if k in ("send_undo_seconds", "bulk_confirm_threshold", "safe_mode", "max_sends_per_day",
                 "default_stagger_min", "catchup_policy", "ai_daily_calls_per_user",
                 "ai_daily_tokens_per_user", "ai_daily_calls_workspace", "delivery_verify_hours"):
            v = validate_send_rail(k, v)
        db.set_setting(k, v)
    db.log_activity("settings", "Settings updated", user["email"])
    return {"ok": True}


def settings_public():
    """Non-secret settings for the agent's get_settings tool."""
    s = db.all_settings()
    s.pop("ai_api_key", None)
    s.pop("ai_api_key_2", None)
    s.pop("app_secret", None)
    s.pop("supabase_service_key", None)
    s.pop("cloud_relay_secret", None)
    s["supabase_key_set"] = "1" if db.get_setting("supabase_service_key") else "0"
    s["ai_has_key"] = "1" if db.get_setting("ai_api_key") else "0"
    return s


def set_signature(html, actor=None):
    db.set_setting("signature_html", (html or "").strip())
    db.log_activity("signature_save", "", actor_email(actor))
    return {"ok": True}


def set_branding(team_name, tagline, admin_email, actor=None):
    if team_name is not None:
        if not team_name.strip():
            raise ServiceError("Team name cannot be empty.")
        if len(team_name.strip()) > 60:
            raise ServiceError("Team name must be under 60 characters.")
        db.set_setting("brand_team_name", team_name.strip())
    if tagline is not None:
        if len(tagline.strip()) > 140:
            raise ServiceError("Login footer must be under 140 characters.")
        db.set_setting("brand_tagline", tagline.strip())
    if admin_email is not None:
        if admin_email.strip() and not EMAIL_RE.match(admin_email.strip()):
            raise ServiceError("Admin email is not a valid address.")
        db.set_setting("admin_email", admin_email.strip())
    db.log_activity("branding", "Branding updated", actor_email(actor))
    return {"ok": True}


# ------------------------------------------------------------------ accounts

def account_list(user):
    return db.list_accounts(user["id"], user["id"])


def account_remove_impact(aid):
    aid = int(aid)
    c = db.conn()
    acc = c.execute("SELECT * FROM gmail_accounts WHERE id=?", (aid,)).fetchone()
    if not acc:
        c.close()
        raise ServiceError("Account not found", 404)
    q = c.execute("SELECT COUNT(*) n FROM emails WHERE account_id=? AND status IN ('scheduled','pending')", (aid,)).fetchone()["n"]
    d = c.execute("SELECT COUNT(*) n FROM emails WHERE account_id=? AND status='drafted'", (aid,)).fetchone()["n"]
    c.close()
    return {"email": acc["email"], "label": acc["label"], "queued_or_scheduled": q, "drafts": d}


def account_remove(aid, actor=None):
    aid = int(aid)
    c = db.conn()
    acc = c.execute("SELECT * FROM gmail_accounts WHERE id=?", (aid,)).fetchone()
    if not acc:
        c.close()
        raise ServiceError("Account not found", 404)
    c.execute("DELETE FROM gmail_accounts WHERE id=?", (aid,))
    c.commit()
    db.log_activity("account_remove", acc["email"], actor_email(actor))
    c.close()
    return {"ok": True}


def account_set_visibility(aid, visibility, actor=None):
    if visibility not in ("private", "shared"):
        raise ServiceError("Visibility must be private or shared.")
    db.set_account_visibility(int(aid), visibility)
    db.log_activity("account_visibility", "account %d -> %s" % (aid, visibility), actor_email(actor))
    return {"ok": True}


# -------------------------------------------------------------------- members

def _guard_last_admin(c, demote_or_remove_uids):
    admins = [r["id"] for r in c.execute(
        "SELECT id FROM users WHERE role='admin' AND (status IS NULL OR status='active' OR status='')").fetchall()]
    for uid in demote_or_remove_uids:
        if uid in admins and len(admins) <= 1:
            raise ServiceError("This is the last admin - promote another admin first.", 409)


def member_invite(email, display_name="", actor=None):
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        raise ServiceError("Enter a valid email address.")
    if actor and email.lower() == actor["email"].lower():
        raise ServiceError("You are already a member of this team.")
    u, how = db.invite_member(email, display_name)
    db.log_activity("user_invite", "Invited %s (%s)" % (email, how), actor_email(actor))
    return {"ok": True, "email": email, "how": how}


def member_set_role(uid, role, actor=None):
    if role not in ("admin", "user"):
        raise ServiceError("Role must be admin or user.")
    uid = int(uid)
    c = db.conn()
    u = c.execute("SELECT id, email, role FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        c.close()
        raise ServiceError("Member not found", 404)
    if actor and u["email"].lower() == actor["email"].lower() and role != "admin":
        raise ServiceError("You cannot demote yourself.", 409)
    if role == "user":
        _guard_last_admin(c, [uid])
    c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    c.commit()
    db.log_activity("user_role", "%s -> %s" % (u["email"], role), actor_email(actor))
    c.close()
    return {"ok": True}


def member_remove(uid, actor=None):
    uid = int(uid)
    c = db.conn()
    u = c.execute("SELECT id, email, role FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        c.close()
        raise ServiceError("Member not found", 404)
    if actor and u["email"].lower() == actor["email"].lower():
        raise ServiceError("You cannot remove yourself. Ask another admin.", 409)
    _guard_last_admin(c, [uid])
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.commit()
    c.close()
    try:
        import sync_relay
        sync_relay.record_delete("users", uid)
    except Exception:
        pass
    db.log_activity("user_remove", u["email"], actor_email(actor))
    return {"ok": True}


def member_requests():
    c = db.conn()
    rows = [dict(r) for r in c.execute(
        "SELECT id, email, display_name, request_note, created_at FROM users WHERE status='pending' ORDER BY id").fetchall()]
    c.close()
    return rows


def member_set_status(uid, status, actor=None):
    uid = int(uid)
    c = db.conn()
    u = c.execute("SELECT id, email FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        c.close()
        raise ServiceError("Request not found", 404)
    if status not in ("active", "denied"):
        raise ServiceError("Invalid status.")
    c.execute("UPDATE users SET status=? WHERE id=?", (status, uid))
    c.commit()
    db.log_activity("user_%s" % ("approved" if status == "active" else "denied"), u["email"], actor_email(actor))
    c.close()
    return {"ok": True}


def members_list(user=None):
    rows = db.list_users()
    return [r for r in rows if r.get("status") in (None, "active", "")] or rows


def batch_send_as(bid, action, at="", stagger=0, account_id=0, actor=None, internal_hold_seconds=0, only_ids=None):
    """Finalize a created (queued) batch: draft / schedule / send now.
    Used by the UI after attachments are added, and by the agent flow.
    only_ids: optional list of email row ids to restrict to (subset of the batch)."""
    import gmail_client
    if action not in ("draft", "schedule", "send"):
        raise ServiceError("Unknown action.")
    when = None
    if action == "schedule":
        when = parse_when(at)
    require_account(int(account_id), actor)
    if action == "send" and db.get_setting("safe_mode") == "1" and not internal_hold_seconds:
        raise ServiceError("Safe mode is ON - immediate sending is blocked. Pick Drafts or Schedule, or turn off Safe mode in Settings.", 409)
    c = db.conn()
    b = c.execute("SELECT id FROM batches WHERE id=?", (int(bid),)).fetchone()
    if not b:
        c.close()
        raise ServiceError("Batch not found", 404)
    if only_ids:
        ph = ",".join("?" * len(only_ids))
        eids = [r["id"] for r in c.execute(
            "SELECT id FROM emails WHERE batch_id=? AND status='pending' AND id IN (%s) ORDER BY id" % ph,
            [int(bid)] + [int(i) for i in only_ids])]
    else:
        eids = [r["id"] for r in c.execute(
            "SELECT id FROM emails WHERE batch_id=? AND status='pending' ORDER BY id", (int(bid),))]
    if not eids:
        c.close()
        raise ServiceError("No queued emails left in this batch.", 409)
    extra = {"action": action}
    if action == "draft":
        made = failed = 0
        for eid in eids:
            r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
            try:
                batch = c.execute("SELECT * FROM batches WHERE id=?", (int(bid),)).fetchone()
                attach = load_attachments(dict(batch)) if batch else []
                gmail_client.create_draft(int(account_id), r["to_addr"], r["subject"],
                                          open_tracking_body(r["body_html"], eid),
                                          attach or None, cc_of(dict(batch)))
                c.execute("UPDATE emails SET status='drafted', account_id=?, error='' WHERE id=?", (int(account_id), eid))
                c.execute("UPDATE contacts SET status='drafted' WHERE id=?", (r["contact_id"],))
                made += 1
                cloud_push_email(eid, "drafted")
            except Exception as e:
                c.execute("UPDATE emails SET status='error', error=? WHERE id=?", (str(e)[:300], eid))
                failed += 1
        c.commit()
        extra.update(created_drafts=made, failed=failed)
    elif action == "schedule":
        stagger = int(stagger or 0)
        for i, eid in enumerate(eids):
            t = when + timedelta(minutes=stagger * i)
            c.execute("UPDATE emails SET status='scheduled', account_id=?, scheduled_at=?, error='' WHERE id=?",
                      (int(account_id), t.isoformat(), eid))
            c.execute("UPDATE contacts SET status='scheduled' WHERE id=(SELECT contact_id FROM emails WHERE id=?)", (eid,))
        c.commit()
        for eid in eids:
            cloud_push_email(eid, "scheduled")
        extra.update(scheduled=len(eids),
                     last=(when + timedelta(minutes=stagger * max(0, len(eids) - 1))).isoformat())
    elif action == "send":
        if internal_hold_seconds > 0:
            base = datetime.now().astimezone()
            stagger = int(stagger or 0)
            for i, eid in enumerate(eids):
                t = base + timedelta(minutes=stagger * i, seconds=internal_hold_seconds)
                c.execute("UPDATE emails SET status='scheduled', account_id=?, scheduled_at=?, error='' WHERE id=?",
                          (int(account_id), t.isoformat(), eid))
                c.execute("UPDATE contacts SET status='scheduled' WHERE id=(SELECT contact_id FROM emails WHERE id=?)", (eid,))
            c.commit()
            _record_undo_hold(c, int(bid), base + timedelta(seconds=internal_hold_seconds), actor["id"])
            db.log_activity("batch_hold", "Batch %d - %d emails held for undo (%d s)" % (bid, len(eids), internal_hold_seconds),
                            actor_email(actor))
            _schedule_release_timer(base + timedelta(seconds=internal_hold_seconds))
            for eid in eids:
                cloud_push_email(eid, "scheduled")
            extra.update(scheduled=len(eids), holding=True,
                         release_at=(base + timedelta(seconds=internal_hold_seconds)).strftime("%Y-%m-%d %H:%M:%S"))
        else:
            cap = int(db.get_setting("max_sends_per_day") or 100)
            sent_today = sends_today()
            ok = fail = 0
            for eid in eids:
                if sent_today + ok >= cap:
                    break
                r = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
                try:
                    batch = c.execute("SELECT * FROM batches WHERE id=?", (int(bid),)).fetchone()
                    attach = load_attachments(dict(batch)) if batch else []
                    res = gmail_client.send_with_retry(int(account_id), r["to_addr"], r["subject"],
                                                       open_tracking_body(r["body_html"], eid),
                                                       attach or None, cc_of(dict(batch)))
                    mid = res.get("message_id", "") if isinstance(res, dict) else ""
                    c.execute("UPDATE emails SET status='sent', account_id=?, sent_at=?, error='', message_id=? WHERE id=?",
                              (int(account_id), db.now_iso(), mid, eid))
                    c.execute("UPDATE contacts SET status='sent' WHERE id=?", (r["contact_id"],))
                    ok += 1
                except Exception as e:
                    c.execute("UPDATE emails SET status='error', error=? WHERE id=?", (str(e)[:300], eid))
                    fail += 1
                time.sleep(0.4)
            c.commit()
            extra.update(sent=ok, failed=fail)
    import app as _app
    _app.save_last_account(actor["id"], int(account_id)) if account_id else None
    c.close()
    return {"ok": True, "batch_id": int(bid), "created": len(eids), **extra}


def send_now_held(ids, account_id, actor, internal_hold_seconds=20):
    """Send-now with the undo window (spec 7.7): rows are held as 'scheduled'
    with release_at = now + hold, across one or more batches. Nothing reaches
    Gmail during the hold; the one-shot release timer dispatches after."""
    import db as _db
    if db.get_setting("safe_mode") == "1":
        raise ServiceError("Safe mode is ON. Turn it off in Settings to allow immediate sending.", 409)
    internal_hold_seconds = max(0, min(300, int(internal_hold_seconds or 0)))
    c = db.conn()
    by_batch = {}
    for eid in [int(i) for i in ids]:
        r = c.execute("SELECT batch_id, status FROM emails WHERE id=?", (eid,)).fetchone()
        if r and r["status"] == "pending":
            by_batch.setdefault(r["batch_id"], []).append(eid)
    c.close()
    if not by_batch:
        raise ServiceError("Nothing selected.")
    results = []
    for bid, eids in by_batch.items():
        out = batch_send_as(bid, "send", "", 0, account_id, actor,
                            internal_hold_seconds=internal_hold_seconds)
        out["batch_id"] = bid
        results.append(out)
    return {"holding": True,
            "release_at": results[0].get("release_at"),
            "batches": [r["batch_id"] for r in results],
            "created": sum(r.get("created", 0) for r in results)}
