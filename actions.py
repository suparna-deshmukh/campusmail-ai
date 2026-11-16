"""Action Registry (spec 5.1 / 9).

Every platform action the agent can take lives here as one record:
  name, risk (R0..R5), roles, JSON schema, preview(), execute(), verify().
R0 reads run immediately. R1 generate returns an ephemeral draft. R2-R5
MAY ONLY run through the Approval Gate (approval.consume) - a direct call
to a mutating action raises ApprovalRequired.
"""
import json
import re
import time
from datetime import datetime, timedelta

import db
import services
from services import ServiceError


class ApprovalRequired(Exception):
    pass


class ActionError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


R0, R1, R2, R3, R4, R5 = "R0", "R1", "R2", "R3", "R4", "R5"
ALL = ("user", "admin")
ADMIN = ("admin",)


def _actor_email(actor):
    return (actor or {}).get("email", "user")


class Action:
    def __init__(self, name, risk, roles, desc, schema=None,
                 read=None, generate=None, preview=None, execute=None,
                 verify=None, category="ops"):
        self.name = name
        self.risk = risk
        self.roles = tuple(roles)
        self.desc = desc
        self.schema = schema or {"type": "object", "properties": {}}
        self.read = read
        self.generate = generate
        self.preview = preview
        self.execute = execute
        self.verify = verify
        self.category = category

    def allowed(self, user):
        return (user or {}).get("role") in self.roles

    def spec(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.desc,
            "parameters": self.schema}}


ACTIONS = {}


def reg(action):
    ACTIONS[action.name] = action
    return action


def get(name):
    return ACTIONS.get(name)


def all_actions():
    return list(ACTIONS.values())


# --------------------------------------------------------------------- reads

def _q(sql, args=()):
    c = db.conn()
    rows = c.execute(sql, args).fetchall()
    c.close()
    return [dict(r) for r in rows]


def _one(sql, args=()):
    r = _q(sql, args)
    return r[0] if r else None


def _campaign_counts(c):
    out = []
    for cam in c.execute("SELECT * FROM campaigns ORDER BY updated_at DESC, id DESC").fetchall():
        st = {r["status"]: r["n"] for r in c.execute(
            "SELECT e.status, COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
            "WHERE b.campaign_id=? AND e.status!='deleted' GROUP BY e.status", (cam["id"],))}
        replied = c.execute("SELECT COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
                            "WHERE b.campaign_id=? AND e.status!='deleted' AND e.replied_at IS NOT NULL "
                            "AND e.replied_at!=''", (cam["id"],)).fetchone()["n"]
        sent = st.get("sent", 0)
        # delivery verification window (same rule as the campaign page):
        # "sent" = confidently delivered; in-flight mails show as "verifying"
        verify_h = int(db.get_setting("delivery_verify_hours") or 72)
        verifying = 0
        if verify_h > 0:
            vcut = (datetime.utcnow() - timedelta(hours=verify_h)).strftime("%Y-%m-%d %H:%M:%S")
            verifying = c.execute("SELECT COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
                                   "WHERE b.campaign_id=? AND e.status='sent' AND " + services.sent_at_utc_sql() + " > ? "
                                   "AND COALESCE(e.bounced_at,'')='' AND COALESCE(e.replied_at,'')=''",
                                   (cam["id"], vcut)).fetchone()["n"]
            sent = max(0, sent - verifying)
        contacts = c.execute(
            "SELECT COUNT(*) n FROM contacts ct JOIN sheets s ON ct.sheet_id=s.id WHERE s.campaign_id=?",
            (cam["id"],)).fetchone()["n"]
        if not contacts:
            contacts = c.execute(
                "SELECT COUNT(DISTINCT e.contact_id) n FROM emails e JOIN batches b ON e.batch_id=b.id "
                "WHERE b.campaign_id=? AND e.status!='deleted' AND e.contact_id>0", (cam["id"],)).fetchone()["n"]
        out.append({"id": cam["id"], "name": cam["name"], "description": cam["description"],
                    "n_emails": sum(st.values()), "n_contacts": contacts, "sent": sent,
                    "verifying": verifying,
                    "replied": replied, "rate": round(100.0 * replied / sent) if sent else 0,
                    "status_counts": st, "bounced": st.get("sent", 0) and
                    c.execute("SELECT COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id "
                              "WHERE b.campaign_id=? AND e.bounced_at IS NOT NULL AND e.bounced_at!=''",
                              (cam["id"],)).fetchone()["n"] or 0})
    return out


def read_overview(params, actor):
    c = db.conn()
    tiles = {
        "sent": c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent'").fetchone()["n"],
        "replies": c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent' "
                             "AND replied_at IS NOT NULL AND replied_at!=''").fetchone()["n"],
        "scheduled": c.execute("SELECT COUNT(*) n FROM emails WHERE status='scheduled'").fetchone()["n"],
        "bounced": c.execute("SELECT COUNT(*) n FROM emails WHERE is_reminder=0 AND status='sent' "
                             "AND bounced_at IS NOT NULL AND bounced_at!=''").fetchone()["n"],
        "open_replies": c.execute("SELECT COUNT(*) n FROM emails WHERE status='sent' AND replied_at IS NOT NULL "
                                  "AND replied_at!='' AND (handled_at IS NULL OR handled_at='')").fetchone()["n"],
        "campaigns": c.execute("SELECT COUNT(*) n FROM campaigns").fetchone()["n"],
        "contacts": c.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"],
        "templates": c.execute("SELECT COUNT(*) n FROM email_templates").fetchone()["n"],
    }
    cap = int(db.get_setting("max_sends_per_day") or 100)
    c.close()
    tiles["sent_today"] = services.sends_today(include_held=False)
    tiles["cap"] = cap
    tiles["reply_rate"] = round(100.0 * tiles["replies"] / tiles["sent"]) if tiles["sent"] else 0
    tiles["safe_mode"] = db.get_setting("safe_mode") == "1"
    return {"type": "stat", "data": tiles}


def read_campaigns(params, actor):
    c = db.conn()
    out = _campaign_counts(c)
    c.close()
    return {"type": "table", "data": out,
            "empty": {"text": "No campaigns yet.", "action": "Create a campaign", "flow": "create_campaign"}}


def read_campaign(params, actor):
    cid = int(params.get("campaign_id") or 0)
    c = db.conn()
    cam = c.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    if not cam:
        c.close()
        raise ActionError("Campaign not found.", 404)
    counts = _campaign_counts(c)
    sheets = [dict(r) for r in c.execute("SELECT * FROM sheets WHERE campaign_id=?", (cid,)).fetchall()]
    batches = [dict(r) for r in c.execute(
        "SELECT id, name, template_id, created_at, reminder_days, reminder_max FROM batches WHERE campaign_id=? "
        "ORDER BY id DESC", (cid,)).fetchall()]
    c.close()
    return {"type": "data", "data": {"campaign": dict(cam), "stats": next((x for x in counts if x["id"] == cid), {}),
                                     "sheets": sheets, "batches": batches}}


def read_templates(params, actor):
    c = db.conn()
    tpls = []
    for t in c.execute("SELECT * FROM email_templates ORDER BY is_active DESC, id").fetchall():
        t = dict(t)
        try:
            t["fields"] = json.loads(t.get("fields") or "[]")
        except Exception:
            t["fields"] = []
        t["body_preview"] = re.sub(r"<[^>]+>", " ", (t.get("body_html") or ""))[:120]
        tpls.append({"id": t["id"], "name": t["name"], "type": t.get("type"), "subject": t["subject"],
                     "active": bool(t.get("is_active")), "fields": t["fields"], "body_preview": t["body_preview"]})
    c.close()
    return {"type": "table", "data": tpls,
            "empty": {"text": "No templates yet.", "action": "Write a template", "flow": "write_template"}}


def read_template(params, actor):
    tid = int(params.get("template_id") or 0)
    t = _one("SELECT * FROM email_templates WHERE id=?", (tid,))
    if not t:
        raise ActionError("Template not found.", 404)
    try:
        t["fields"] = json.loads(t.get("fields") or "[]")
    except Exception:
        t["fields"] = []
    return {"type": "data", "data": t}


def read_contacts(params, actor):
    limit = min(int(params.get("limit") or 20), 200)
    search = (params.get("search") or "").strip()
    where, args = "1=1", []
    if search:
        where += " AND (company LIKE ? OR hr_name LIKE ? OR hr_email LIKE ?)"
        like = "%" + search + "%"
        args += [like, like, like]
    total = _one("SELECT COUNT(*) n FROM contacts WHERE " + where, args)["n"]
    rows = _q("SELECT id, serial, company, hr_name, hr_email, status, sheet_id FROM contacts WHERE " + where +
              " ORDER BY id LIMIT ?", args + [limit])
    return {"type": "table", "data": rows, "total": total, "limit": limit}


def read_replies(params, actor):
    limit = min(int(params.get("limit") or 20), 100)
    where, args = "e.status='sent' AND e.replied_at IS NOT NULL AND e.replied_at!=''", []
    if params.get("campaign_id"):
        where += " AND b.campaign_id=?"
        args.append(int(params["campaign_id"]))
    if params.get("member"):
        where += " AND b.created_by=?"
        args.append(str(params["member"]))
    if params.get("status") == "unhandled":
        where += " AND (e.handled_at IS NULL OR e.handled_at='')"
    elif params.get("status") == "handled":
        where += " AND e.handled_at IS NOT NULL AND e.handled_at!=''"
    if params.get("search"):
        where += " AND (e.to_addr LIKE ? OR ct.company LIKE ?)"
        like = "%" + str(params["search"]) + "%"
        args += [like, like]
    rows = _q("SELECT e.id, e.to_addr, e.subject, e.serial, e.replied_at, e.handled_at, e.batch_id, e.is_reminder, "
              "ct.company, b.name AS batch_name, c.name AS campaign_name "
              "FROM emails e LEFT JOIN batches b ON e.batch_id=b.id "
              "LEFT JOIN campaigns c ON c.id=b.campaign_id LEFT JOIN contacts ct ON ct.id=e.contact_id "
              "WHERE " + where + " ORDER BY e.replied_at DESC LIMIT ?", args + [limit])
    total = _one("SELECT COUNT(*) n FROM emails e LEFT JOIN batches b ON e.batch_id=b.id "
                 "LEFT JOIN campaigns c ON c.id=b.campaign_id LEFT JOIN contacts ct ON ct.id=e.contact_id "
                 "WHERE " + where, args)["n"]
    return {"type": "table", "data": rows, "total": total,
            "empty": {"text": "No replies" + (" match" if (params.get("search") or params.get("campaign_id")) else " yet.")}}


def read_team(params, actor):
    rows = []
    c = db.conn()
    for u in c.execute("SELECT * FROM users ORDER BY role, email").fetchall():
        u = dict(u)
        u["sent"] = c.execute(
            "SELECT COUNT(*) n FROM emails e JOIN batches b ON e.batch_id=b.id WHERE b.created_by=? AND e.status='sent'",
            (u["email"],)).fetchone()["n"]
        rows.append({"id": u["id"], "email": u["email"], "display_name": u["display_name"],
                     "role": u["role"], "status": u["status"], "sent": u["sent"]})
    c.close()
    return {"type": "table", "data": rows}


def read_activity(params, actor):
    limit = min(int(params.get("limit") or 20), 100)
    where, args = "1=1", []
    if params.get("type"):
        where += " AND action=?"
        args.append(str(params["type"]))
    if params.get("actor"):
        where += " AND actor LIKE ?"
        args.append("%" + str(params["actor"]) + "%")
    if params.get("date"):
        where += " AND ts LIKE ?"
        args.append(str(params["date"]) + "%")
    rows = _q("SELECT id, ts, actor, action, detail FROM activity WHERE " + where +
              " ORDER BY id DESC LIMIT ?", args + [limit])
    return {"type": "activity", "data": rows}


def read_attention(params, actor):
    """Must equal /api/home attention counts exactly (F-40)."""
    from datetime import datetime, timedelta
    c = db.conn()
    now = datetime.now().astimezone()
    rows = []
    q = c.execute("SELECT COUNT(*) n FROM emails WHERE status='sent' AND replied_at IS NOT NULL "
                  "AND replied_at!='' AND (handled_at IS NULL OR handled_at='')").fetchone()["n"]
    if q:
        rows.append({"type": "replies", "count": q, "flow": "replies"})
    q = c.execute("SELECT COUNT(*) n FROM emails WHERE status='sent' AND bounced_at IS NOT NULL "
                  "AND bounced_at!=''").fetchone()["n"]
    if q:
        rows.append({"type": "bounced", "count": q, "flow": "bounces"})
    q = c.execute("SELECT COUNT(*) n FROM emails WHERE status='error'").fetchone()["n"]
    if q:
        rows.append({"type": "failed", "count": q})
    in24 = (now + timedelta(hours=24)).astimezone().isoformat()
    q = c.execute("SELECT COUNT(*) n FROM emails WHERE status='scheduled' AND scheduled_at<=?", (in24,)).fetchone()["n"]
    if q:
        rows.append({"type": "soon", "count": q, "flow": "upcoming"})
    cutoff = (now - timedelta(days=2)).astimezone().isoformat()
    q = c.execute("SELECT COUNT(*) n FROM emails WHERE status='drafted' AND created_at<?", (cutoff,)).fetchone()["n"]
    if q:
        rows.append({"type": "stale_drafts", "count": q})
    if not any(db.account_token_json(a["id"]) for a in db.list_accounts(actor["id"], actor["id"])):
        rows.append({"type": "gmail", "count": 1, "flow": "gmail_connect"})
    if actor.get("role") == "admin":
        q = c.execute("SELECT COUNT(*) n FROM users WHERE status='pending'").fetchone()["n"]
        if q:
            rows.append({"type": "requests", "count": q, "flow": "join_requests"})
    c.close()
    return {"type": "list", "data": rows, "empty": {"text": "All clear - nothing needs your attention."}}


def read_upcoming(params, actor):
    rows = _q("SELECT e.scheduled_at AS [when], c.name AS campaign, c.id AS campaign_id, e.batch_id, COUNT(*) n "
              "FROM emails e JOIN batches b ON e.batch_id=b.id LEFT JOIN campaigns c ON c.id=b.campaign_id "
              "WHERE e.status='scheduled' GROUP BY e.scheduled_at, c.id ORDER BY e.scheduled_at LIMIT 10")
    return {"type": "table", "data": rows,
            "empty": {"text": "Nothing scheduled.", "action": "Schedule a send", "flow": "send_campaign"}}


def read_batches(params, actor):
    rows = _q("SELECT b.id, b.name, b.template_id, b.created_at, b.reminder_days, b.reminder_max, "
              "b.cc, b.append_signature, c.name AS campaign, "
              "SUM(CASE WHEN e.status='pending' THEN 1 ELSE 0 END) pending, "
              "SUM(CASE WHEN e.status='drafted' THEN 1 ELSE 0 END) drafted, "
              "SUM(CASE WHEN e.status='scheduled' THEN 1 ELSE 0 END) scheduled, "
              "SUM(CASE WHEN e.status='sent' THEN 1 ELSE 0 END) sent, "
              "SUM(CASE WHEN e.status='error' THEN 1 ELSE 0 END) errored, "
              "SUM(CASE WHEN e.status='cancelled' THEN 1 ELSE 0 END) cancelled "
              "FROM batches b LEFT JOIN campaigns c ON c.id=b.campaign_id "
              "LEFT JOIN emails e ON e.batch_id=b.id GROUP BY b.id ORDER BY b.id DESC LIMIT 20")
    for r in rows:
        r["total"] = sum(r[k] or 0 for k in ("pending", "drafted", "scheduled", "sent", "errored", "cancelled"))
    return {"type": "table", "data": rows}


def read_batch(params, actor):
    bid = int(params.get("batch_id") or 0)
    b = _one("SELECT * FROM batches WHERE id=?", (bid,))
    if not b:
        raise ActionError("Batch not found.", 404)
    st = {r["status"]: r["n"] for r in _q("SELECT status, COUNT(*) n FROM emails WHERE batch_id=? "
                                          "AND status!='deleted' GROUP BY status", (bid,))}
    emails = _q("SELECT id, serial, to_addr, subject, status, scheduled_at, sent_at, error FROM emails "
                "WHERE batch_id=? AND status!='deleted' ORDER BY serial LIMIT 20", (bid,))
    return {"type": "data", "data": {"batch": b, "status_counts": st, "emails": emails}}


def read_reply_thread(params, actor):
    eid = int(params.get("email_id") or 0)
    e = _one("SELECT * FROM emails WHERE id=?", (eid,))
    if not e:
        raise ActionError("Email not found.", 404)
    original = re.sub(r"<[^>]+>", " ", e.get("body_html") or "")
    snooze = _one("SELECT * FROM email_snoozes WHERE email_id=?", (eid,))
    return {"type": "thread", "data": {
        "id": e["id"], "to_addr": e["to_addr"], "subject": e["subject"],
        "sent_at": e["sent_at"], "original_preview": original[:400],
        "replied_at": e["replied_at"], "handled_at": e["handled_at"],
        "snoozed_until": snooze["snoozed_until"] if snooze else None,
        "note": "The reply body is read live from Gmail when you draft a reply; the app stores the original send."}}


def read_bounces(params, actor):
    rows = _q("SELECT e.id, e.to_addr, e.serial, e.bounced_at, e.bounce_reason, ct.company, ct.hr_name, e.batch_id "
              "FROM emails e LEFT JOIN contacts ct ON ct.id=e.contact_id "
              "WHERE e.status='sent' AND e.bounced_at IS NOT NULL AND e.bounced_at!='' "
              "ORDER BY e.bounced_at DESC LIMIT 50")
    for r in rows:
        if not (r.get("bounce_reason") or "").strip():
            r["bounce_reason"] = "Reason unavailable"
    return {"type": "table", "data": rows,
            "empty": {"text": "No bounced emails.", }}


def read_sheets(params, actor):
    c = db.conn()
    rows = []
    for s in c.execute("SELECT * FROM sheets ORDER BY id").fetchall():
        s = dict(s)
        try:
            headers = json.loads(s.get("headers") or "[]")
        except Exception:
            headers = []
        try:
            rowsdata = json.loads(s.get("rows") or "[]")
        except Exception:
            rowsdata = []
        s["headers"] = headers
        s["email_column"] = next((h for h in headers if re.search(r"email|mail", h, re.I)), "")
        s["preview"] = rowsdata[:5]
        rows.append(s)
    c.close()
    return {"type": "table", "data": rows}


def read_sheet(params, actor):
    sid = int(params.get("sheet_id") or 0)
    s = _one("SELECT * FROM sheets WHERE id=?", (sid,))
    if not s:
        raise ActionError("List not found.", 404)
    try:
        s["rows"] = json.loads(s.get("rows") or "[]")[:20]
    except Exception:
        s["rows"] = []
    try:
        s["headers"] = json.loads(s.get("headers") or "[]")
    except Exception:
        s["headers"] = []
    s["deletable"] = not services.sheet_delete_blocked_reason(sid)["blocked"]
    return {"type": "data", "data": s}


def read_attachments(params, actor):
    import filestore
    import os
    c = db.conn()
    names = []
    for b in c.execute("SELECT id, attachments FROM batches").fetchall():
        try:
            names += json.loads(b["attachments"] or "[]")
        except Exception:
            pass
    c.close()
    out = []
    for n in dict.fromkeys(names):
        try:
            if filestore.exists(n):
                out.append({"name": n, "size": filestore.size(n)})
        except Exception:
            pass
    return {"type": "list", "data": out, "empty": {"text": "No attachments in the library yet."}}


def read_results(params, actor):
    """Analytics (F-50). Members can see everyone's numbers."""
    where, args = "e.status='sent' AND e.is_reminder=0", []
    if params.get("campaign_id"):
        where += " AND b.campaign_id=?"
        args.append(int(params["campaign_id"]))
    if params.get("person"):
        where += " AND b.created_by=?"
        args.append(str(params["person"]))
    if params.get("date_from"):
        where += " AND e.sent_at>=?"
        args.append(str(params["date_from"]))
    if params.get("date_to"):
        where += " AND e.sent_at<=?"
        args.append(str(params["date_to"]) + " 23:59:59")
    base = ("FROM emails e JOIN batches b ON e.batch_id=b.id LEFT JOIN campaigns c ON c.id=b.campaign_id")
    sent = _one("SELECT COUNT(*) n " + base + " WHERE " + where, args)["n"]
    replied = _one("SELECT COUNT(*) n " + base + " WHERE " + where +
                   " AND e.replied_at IS NOT NULL AND e.replied_at!=''", args)["n"]
    bounced = _one("SELECT COUNT(*) n " + base + " WHERE " + where +
                   " AND e.bounced_at IS NOT NULL AND e.bounced_at!=''", args)["n"]
    rows = _q("SELECT c.name AS campaign, c.id AS campaign_id, COUNT(*) sent, "
              "SUM(CASE WHEN e.replied_at IS NOT NULL AND e.replied_at!='' THEN 1 ELSE 0 END) replied, "
              "SUM(CASE WHEN e.bounced_at IS NOT NULL AND e.bounced_at!='' THEN 1 ELSE 0 END) bounced "
              + base + " WHERE " + where + " GROUP BY c.id ORDER BY sent DESC LIMIT 20", args)
    return {"type": "analytics", "data": {
        "sent": sent, "replied": replied, "bounced": bounced,
        "reply_rate": round(100.0 * replied / sent) if sent else 0, "by_campaign": rows}}


def read_gmail_accounts(params, actor):
    out = []
    for a in db.list_accounts(actor["id"], actor["id"]):
        a = dict(a)
        a["connected"] = bool(db.account_token_json(a["id"]))
        out.append({"id": a["id"], "email": a["email"], "label": a.get("label"),
                    "visibility": a.get("visibility"), "connected": a["connected"]})
    return {"type": "list", "data": out,
            "empty": {"text": "No Gmail accounts connected.", "action": "Connect Gmail", "flow": "gmail_connect"}}


def read_settings(params, actor):
    s = services.settings_public()
    s["signature_present"] = bool(db.get_setting("signature_html", "").strip())
    s["safe_mode_on"] = s.get("safe_mode") == "1"
    return {"type": "data", "data": s}


def read_send_status(params, actor):
    cap = int(db.get_setting("max_sends_per_day") or 100)
    used = services.sends_today(include_held=True)
    return {"type": "stat", "data": {
        "safe_mode_on": db.get_setting("safe_mode") == "1",
        "sent_today": used, "cap": cap, "remaining": max(0, cap - used),
        "held": services.held_count(),
        "undo_seconds": int(db.get_setting("send_undo_seconds") or 20),
        "bulk_threshold": int(db.get_setting("bulk_confirm_threshold") or 50)}}


def read_preview(params, actor):
    tid = int(params.get("template_id") or 0)
    tpl = _one("SELECT * FROM email_templates WHERE id=?", (tid,))
    if not tpl:
        raise ActionError("Template not found.", 404)
    mapping = params.get("mapping") or {}
    row = params.get("row") or {}
    subject, body = services.render_email(dict(tpl), row, mapping)
    if params.get("append_signature"):
        body = services.append_signature(body)
    return {"type": "preview", "data": {"subject": subject, "body_html": body}}


def read_sync_status(params, actor):
    import sync_relay
    st = getattr(sync_relay, "status", lambda: {})()
    return {"type": "data", "data": st or {"relay_enabled": bool(db.get_setting("cloud_relay_url"))}}


def read_join_requests(params, actor):
    if actor.get("role") != "admin":
        raise ActionError("Admin only.", 403)
    rows = _q("SELECT id, email, display_name, request_note, created_at FROM users WHERE status='pending' "
              "ORDER BY created_at")
    return {"type": "list", "data": rows, "empty": {"text": "No pending join requests."}}


HELP_TOPICS = {
    "safe mode": "Safe mode blocks immediate sending for the whole team. Turn it off in Settings (or ask an admin) to allow 'Send now'. Scheduling and drafts always work.",
    "cap": "The daily cap limits how many emails can be sent per day per workspace. Held (undo-window) emails count against it until released or undone.",
    "undo": "When you choose Send now, the emails are held for the undo window (10-30 s, default 20). Click Undo during the window and nothing is sent. After release, sent mail can't be recalled from the app.",
    "schedule": "Schedule a send between 5 minutes and 45 days out. Stagger spreads emails over time so Gmail doesn't flag you. Reminders can follow up with non-repliers.",
    "bounce": "Bounced emails show the SMTP reason when available, otherwise 'Reason unavailable'. You can fix the address and re-send to just the fixed contacts.",
    "reply": "Replies appear in the Replies inbox. You can draft a reply (sent or as a Gmail draft), mark handled, snooze, or mark replied manually.",
    "budget": "Each user has a daily AI budget (calls + tokens). When it's exhausted, AI writing pauses until the next day, but every deterministic action (reads, imports, sends, settings) still works.",
    "compact": "Compact mode collects the send parameters (timing, stagger, CC, signature, attachments, reminders) into one editable review card. It never removes a gate or pre-selects the send action.",
    "bulk": "Sends at or above the bulk threshold (default 50) need an extra acknowledgement: tick the review box, and at 4x the threshold type the exact count. This is verified by the server.",
    "pre-send check": "Before every send the app runs a pre-send check: blocking problems (cap, safe mode, missing account, unresolved merge fields, CC over 20) disable the confirm button; warnings and suggestions never block. Link checking is best-effort and can never delay a send.",
    "one-off": "You can draft and send a one-off email to any address from the chat: it asks for recipients, writes the email, and offers Send / Schedule / Queue / Gmail drafts / Send test to me - with the same confirmations as a campaign send. It counts toward the daily cap.",
    "test email": "Send a test email to yourself from the chat to check how a draft, template, or a campaign's first recipient renders (signature, images, attachments). Test sends are recorded so 'never tested' suggestions disappear.",
    "reminders": "Attach follow-up reminders to a scheduled or sent batch: 1-5 rounds, N days apart. Each round goes to the people who haven't replied yet (evaluated at each round).",
    "oauth": "Connecting a Gmail account needs the OAuth consent screen, which the assistant can't complete itself. Use the handoff to open Settings and confirm when done.",
    "export": "Export gives you a CSV of your contacts. Danger-zone resets (emptying all emails) live in Settings and require typed confirmation, so the assistant hands off to that page.",
}


def read_help(params, actor):
    topic = (params.get("topic") or "").lower()
    hits = []
    for k, v in HELP_TOPICS.items():
        if k in topic or topic in k or any(w in k for w in topic.split()):
            hits.append({"topic": k, "text": v})
    if not hits and topic:
        hits = [{"topic": "topics", "text": "I can help with: " + ", ".join(HELP_TOPICS.keys())}]
    elif not hits:
        hits = [{"topic": "topics", "text": "Ask me about: " + ", ".join(HELP_TOPICS.keys())}]
    return {"type": "list", "data": hits}


def read_ai_usage(params, actor):
    import usage
    if actor.get("role") == "admin" and not params.get("own"):
        return {"type": "table", "data": usage.usage_report()["users"]}
    r = usage.usage_report(actor["id"])
    return {"type": "data", "data": r.get("status", {})}


def read_presend(params, actor):
    import presend
    return presend.run(params)


# --------------------------------------------------------------------- writes
# Each: preview(params, actor) -> confirm payload; execute(params, actor) -> result dict.

def _p_create_campaign(p, a):
    return {"title": 'Create campaign "%s"' % p["name"], "risk": "R2",
            "details": [{"k": "Name", "v": p["name"]}, {"k": "Description", "v": p.get("description") or "—"}],
            "impact": ["Creates 1 campaign (empty: no list, no template yet)"], "irreversible": False,
            "notes": ["Gmail is not touched."], "primary": {"label": "Create campaign"}}


def _e_create_campaign(p, a):
    out = services.campaign_create(p["name"], p.get("description") or "", a)
    return {"campaign_id": out.get("id") or out.get("campaign_id"), "name": p["name"]}


def _p_update_campaign(p, a):
    cur = _one("SELECT name, description FROM campaigns WHERE id=?", (int(p["campaign_id"]),))
    if not cur:
        raise ActionError("Campaign not found.", 404)
    return {"title": "Edit campaign", "risk": "R2",
            "details": [{"k": "Name", "v": p.get("name") or cur["name"]},
                        {"k": "Description", "v": p.get("description", cur["description"]) or "—"}],
            "impact": ["Updates the campaign's name/description"], "irreversible": False,
            "primary": {"label": "Save changes"}}


def _e_update_campaign(p, a):
    services.campaign_update(int(p["campaign_id"]), p.get("name"), p.get("description"), a)
    return {"campaign_id": int(p["campaign_id"])}


def _p_delete_campaign(p, a):
    imp = services.campaign_impact(int(p["campaign_id"]))
    return {"title": 'Delete campaign "%s"' % imp.get("name", ""), "risk": "R3",
            "details": [{"k": "Campaign", "v": imp.get("name", "")}],
            "impact": ["Removes %s batch(es) and %s email record(s)" % (imp.get("batches", 0), imp.get("emails", 0)),
                       "Unlinks any contact lists",
                       "Removed on both instances via sync"],
            "irreversible": True, "notes": ["Gmail copies are not touched."],
            "primary": {"label": "Delete campaign and %s email records" % imp.get("emails", 0)},
            "danger": True}


def _e_delete_campaign(p, a):
    out = services.campaign_delete(int(p["campaign_id"]), a)
    return out


def _p_attach_sheet(p, a):
    s = _one("SELECT name, n_rows FROM sheets WHERE id=?", (int(p["sheet_id"]),))
    before = services.campaign_recipient_count(int(p["campaign_id"]))
    return {"title": "Attach list to campaign", "risk": "R2",
            "details": [{"k": "List", "v": s["name"] if s else str(p["sheet_id"])},
                        {"k": "Rows", "v": s["n_rows"] if s else "?"}],
            "impact": ["Recipient count: %s -> %s" % (before, before + (s["n_rows"] if s else 0))],
            "irreversible": False, "primary": {"label": "Attach list"}}


def _e_attach_sheet(p, a):
    services.sheet_set_campaign(int(p["sheet_id"]), int(p["campaign_id"]), a)
    return {"sheet_id": int(p["sheet_id"]), "campaign_id": int(p["campaign_id"]),
            "recipients": services.campaign_recipient_count(int(p["campaign_id"]))}


def _p_detach_sheet(p, a):
    s = _one("SELECT name, n_rows FROM sheets WHERE id=?", (int(p["sheet_id"]),))
    before = services.campaign_recipient_count(int(p["campaign_id"]))
    return {"title": "Detach list from campaign", "risk": "R2",
            "details": [{"k": "List", "v": s["name"] if s else str(p["sheet_id"])}],
            "impact": ["Recipient count: %s -> %s" % (before, max(0, before - (s["n_rows"] if s else 0)))],
            "irreversible": False, "primary": {"label": "Detach list"}}


def _e_detach_sheet(p, a):
    services.sheet_set_campaign(int(p["sheet_id"]), 0, a)
    return {"sheet_id": int(p["sheet_id"]),
            "recipients": services.campaign_recipient_count(int(p.get("campaign_id") or 0))}


def _p_set_campaign_template(p, a):
    t = _one("SELECT name FROM email_templates WHERE id=?", (int(p["template_id"]),))
    return {"title": "Set campaign template", "risk": "R2",
            "details": [{"k": "Template", "v": t["name"] if t else str(p["template_id"])}],
            "impact": ["New sends for this campaign will offer this template first"],
            "irreversible": False, "primary": {"label": "Set template"}}


def _e_set_campaign_template(p, a):
    c = db.conn()
    c.execute("INSERT INTO campaign_meta(campaign_id, template_id) VALUES(?,?) "
              "ON CONFLICT(campaign_id) DO UPDATE SET template_id=excluded.template_id",
              (int(p["campaign_id"]), int(p["template_id"])))
    c.commit()
    c.close()
    db.log_activity("campaign_template", "campaign %s -> template %s" % (p["campaign_id"], p["template_id"]),
                    _actor_email(a))
    return {"campaign_id": int(p["campaign_id"]), "template_id": int(p["template_id"])}


def _p_create_template(p, a):
    return {"title": "Save template '%s'" % p["name"], "risk": "R2",
            "details": [{"k": "Name", "v": p["name"]}, {"k": "Subject", "v": p.get("subject") or "—"},
                        {"k": "Fields", "v": ", ".join(p.get("fields") or []) or "—"}],
            "impact": ["Creates a new email template"], "irreversible": False,
            "primary": {"label": "Save template"}}


def _e_create_template(p, a):
    out = services.template_create(p["name"], p.get("subject") or "", p.get("body_html") or "",
                                   p.get("kind") or "outreach", a, active=bool(p.get("active")))
    return {"template_id": out.get("id") or out.get("template_id"), "name": p["name"],
            "active": bool(p.get("active"))}


def _p_update_template(p, a):
    t = _one("SELECT name, subject FROM email_templates WHERE id=?", (int(p["template_id"]),))
    if not t:
        raise ActionError("Template not found.", 404)
    return {"title": "Update template '%s'" % t["name"], "risk": "R2",
            "details": [{"k": "Subject", "v": p.get("subject") or t["subject"] or "—"}],
            "impact": ["Affects future sends only (already-sent emails are unchanged)"],
            "irreversible": False, "primary": {"label": "Save template"}}


def _e_update_template(p, a):
    services.template_update(int(p["template_id"]), p.get("name"), p.get("subject"),
                             p.get("body_html"), a)
    return {"template_id": int(p["template_id"])}


def _p_rename_template(p, a):
    t = _one("SELECT name FROM email_templates WHERE id=?", (int(p["template_id"]),))
    return {"title": "Rename template", "risk": "R2",
            "details": [{"k": "From", "v": t["name"] if t else "?"}, {"k": "To", "v": p["name"]}],
            "impact": ["Renames the template"], "irreversible": False, "primary": {"label": "Rename"}}


def _e_rename_template(p, a):
    services.template_update(int(p["template_id"]), p["name"], None, None, a)
    return {"template_id": int(p["template_id"]), "name": p["name"]}


def _p_activate_template(p, a):
    t = _one("SELECT name FROM email_templates WHERE id=?", (int(p["template_id"]),))
    cur = _one("SELECT name FROM email_templates WHERE is_active=1")
    return {"title": "Make '%s' the active template" % (t["name"] if t else ""), "risk": "R2",
            "details": [{"k": "Currently active", "v": cur["name"] if cur else "none"}],
            "impact": ["'%(new)s' becomes the default for new sends" % {"new": t["name"] if t else "?"},
                       ("'%s' gets deactivated" % cur["name"]) if cur and cur["id"] != p["template_id"] else ""],
            "irreversible": False, "primary": {"label": "Activate template"}}


def _e_activate_template(p, a):
    services.template_activate(int(p["template_id"]), a)
    return {"template_id": int(p["template_id"])}


def _p_delete_template(p, a):
    imp = services.template_delete_impact(int(p["template_id"]))
    t = _one("SELECT name FROM email_templates WHERE id=?", (int(p["template_id"]),))
    lines = ["Removes template '%s'" % (t["name"] if t else str(p["template_id"]))]
    if imp.get("used_by_campaigns"):
        lines.append("Referenced by %s campaign(s) - those sends will need a new template" % imp["used_by_campaigns"])
    lines.append("Removed on both instances via sync tombstone")
    return {"title": "Delete template", "risk": "R3", "details": [{"k": "Template",
            "v": t["name"] if t else str(p["template_id"])}], "impact": lines, "irreversible": True,
            "notes": ["Gmail copies are not touched."],
            "primary": {"label": "Delete template"}, "danger": True}


def _e_delete_template(p, a):
    return services.template_delete(int(p["template_id"]), a)


def _p_import_sheet(p, a):
    return {"title": "Import '%s' as a contact list" % (p.get("name") or p.get("filename") or "file"),
            "risk": "R2",
            "details": [{"k": "Rows", "v": p.get("rows")}, {"k": "Columns", "v": ", ".join(p.get("headers") or [])}],
            "impact": ["Adds %s contact row(s)" % p.get("rows")], "irreversible": False,
            "primary": {"label": "Import list"}}


def _e_import_sheet(p, a):
    return services.sheet_upload(p.get("name"), int(p.get("campaign_id") or 0),
                                 p.get("headers") or [], p.get("rows") or [], p.get("filename") or "", a)


def _p_delete_sheet(p, a):
    s = _one("SELECT name, n_rows FROM sheets WHERE id=?", (int(p["sheet_id"]),))
    blocked = services.sheet_delete_blocked_reason(int(p["sheet_id"]))
    if blocked["blocked"]:
        raise ActionError(blocked["reason"], 409)
    return {"title": "Delete list '%s'" % (s["name"] if s else ""), "risk": "R3",
            "details": [{"k": "Rows removed", "v": s["n_rows"] if s else "?"}],
            "impact": ["Deletes the list and its %s contact row(s)" % (s["n_rows"] if s else "?")],
            "irreversible": True, "primary": {"label": "Delete list and %s rows" % (s["n_rows"] if s else "?")},
            "danger": True}


def _e_delete_sheet(p, a):
    return services.sheet_delete(int(p["sheet_id"]), a)


def _p_update_contacts(p, a):
    return {"title": "Update %s contact(s)" % len(p.get("ids") or []), "risk": "R2",
            "details": [{"k": "Change", "v": "email -> %s" % json.dumps(p.get("updates", {}))[:80]}],
            "impact": ["Corrected contacts are marked ready to re-send"], "irreversible": False,
            "primary": {"label": "Update %d contacts" % len(p.get("ids") or [])}}


def _e_update_contacts(p, a):
    n = 0
    for cid, upd in (p.get("updates") or {}).items():
        services.contact_update(int(cid), None, None, upd.get("hr_email"), "ready_to_resend" if upd.get("hr_email") else None, None, a)
        n += 1
    return {"updated": n}


def _p_prepare_send(p, a):
    action = p.get("action") or "queue"
    n = p.get("count") or 0
    labels = {"queue": "Queue %s emails (nothing is sent)" % n, "draft": "Create %s Gmail drafts" % n,
              "schedule": "Schedule %s emails for %s" % (n, p.get("at", "")), "send": "Send %s emails now" % n}
    details = [{"k": "Campaign", "v": p.get("campaign_name") or ""}, {"k": "Template", "v": p.get("template_name") or ""},
               {"k": "Recipients", "v": n}, {"k": "Action", "v": labels.get(action, action)}]
    if p.get("at"):
        details.append({"k": "Time", "v": p["at"]})
    if p.get("stagger"):
        details.append({"k": "Stagger", "v": "%s min" % p["stagger"]})
    if p.get("cc"):
        details.append({"k": "CC", "v": p["cc"]})
    if p.get("reminders"):
        details.append({"k": "Reminders", "v": p["reminders"]})
    return {"title": labels.get(action, "Prepare send"), "risk": "R4", "details": details,
            "impact": ["%s" % labels.get(action, action)], "irreversible": action in ("send",),
            "notes": ["Gmail is touched only for drafts/send."],
            "primary": {"label": labels.get(action, "Continue")}}


def _e_prepare_send(p, a):
    d = {k: p[k] for k in ("template_id", "mapping", "name", "account_id", "campaign_id", "sheet_id",
                           "serial_from", "serial_to", "contact_ids", "cc", "append_signature",
                           "at", "stagger") if k in p}
    d["action"] = {"queue": "create", "create": "create", "draft": "draft",
                   "schedule": "schedule", "send": "send"}.get(p.get("action"), "create")
    if p.get("action") == "send":
        # the undo window is server-configured; never trusted from params (spec 17.1.15)
        d["internal_hold_seconds"] = int(db.get_setting("send_undo_seconds") or 0)
    out = services.merge(d, a)
    return out


def _p_cancel_emails(p, a):
    where, args = "e.status IN ('pending','scheduled','drafted')", []
    if p.get("campaign_id"):
        where += " AND b.campaign_id=?"
        args.append(int(p["campaign_id"]))
    if p.get("batch_id"):
        where += " AND e.batch_id=?"
        args.append(int(p["batch_id"]))
    if p.get("ids"):
        where = "e.id IN (%s)" % ",".join("?" * len(p["ids"]))
        args = [int(i) for i in p["ids"]]
    rows = _q("SELECT status, COUNT(*) n FROM emails e LEFT JOIN batches b ON e.batch_id=b.id WHERE " +
              where + " GROUP BY status", args)
    n = sum(r["n"] for r in rows)
    return {"title": "Cancel %s email(s)" % n, "risk": "R3",
            "details": [{"k": "Will stop", "v": ", ".join("%s %s" % (r["n"], r["status"]) for r in rows) or "0"}],
            "impact": ["Queued and scheduled emails stop; already-sent emails are NOT affected",
                       "Gmail drafts are not deleted - Gmail copies are never touched"],
            "irreversible": False, "primary": {"label": "Cancel %s emails" % n}, "danger": True}


def _e_cancel_emails(p, a):
    where, args = "e.status IN ('pending','scheduled','drafted')", []
    if p.get("campaign_id"):
        where += " AND b.campaign_id=?"
        args.append(int(p["campaign_id"]))
    if p.get("batch_id"):
        where += " AND e.batch_id=?"
        args.append(int(p["batch_id"]))
    if p.get("ids"):
        where = "e.id IN (%s)" % ",".join("?" * len(p["ids"]))
        args = [int(i) for i in p["ids"]]
    c = db.conn()
    rows = c.execute("SELECT e.id FROM emails e LEFT JOIN batches b ON e.batch_id=b.id WHERE " + where, args).fetchall()
    ids = [r["id"] for r in rows]
    c.close()
    out = services.emails_cancel(ids, a)
    return out


def _p_save_gmail_drafts(p, a):
    n = p.get("count") or 0
    return {"title": "Create %s Gmail draft(s) - nothing is sent" % n, "risk": "R4",
            "details": [{"k": "Recipient scope", "v": p.get("scope") or str(n) + " email(s)"}],
            "impact": ["Creates Gmail drafts in the connected account; nothing is sent"],
            "irreversible": False, "primary": {"label": "Create %s drafts" % n}}


def _e_save_gmail_drafts(p, a):
    if p.get("batch_id"):
        return services.batch_send_as(int(p["batch_id"]), "draft", "", 0, int(p["account_id"]), a,
                                      only_ids=p.get("ids"))
    return services.emails_draft(p.get("ids") or [], int(p["account_id"]), a)


def _p_send_test(p, a):
    return {"title": "Send test to %s" % p.get("to"), "risk": "R4",
            "details": [{"k": "Recipient", "v": p.get("to")}, {"k": "Subject", "v": p.get("subject") or "—"}],
            "impact": ["Sends one real test email (a draft in safe mode)"], "irreversible": False,
            "primary": {"label": "Send test to " + str(p.get("to"))}}


def _e_send_test(p, a):
    return services.test_email_send(int(p["account_id"]), p["to"], p.get("subject") or "Test",
                                    p.get("body_html") or "<p>Test</p>", a,
                                    p.get("template_id"), p.get("content_hash"))


def _p_send_adhoc(p, a):
    n = len(p.get("recipients") or [])
    return {"title": "Send %s one-off email(s)" % n, "risk": "R4",
            "details": [{"k": "Recipients", "v": ", ".join(p.get("recipients") or [])},
                        {"k": "Subject", "v": p.get("subject") or "—"}],
            "impact": ["Counts toward the daily cap"], "irreversible": p.get("action") == "send",
            "primary": {"label": "Send %s one-off email(s)" % n}}


def _e_send_adhoc(p, a):
    cid = int(p.get("campaign_id") or 0)
    if not cid:
        out = services.campaign_create("One-off emails", "Ad-hoc single-recipient emails", a)
        cid = out.get("id") or out.get("campaign_id")
    sid = None
    c = db.conn()
    headers = ["company", "hr_name", "hr_email"]
    rows = []
    for i, r in enumerate(p.get("recipients") or []):
        em = r if isinstance(r, str) else r.get("hr_email")
        rows.append({"company": (r.get("company") if isinstance(r, dict) else "") or "",
                     "hr_name": (r.get("hr_name") if isinstance(r, dict) else "") or "", "hr_email": em})
    out = services.sheet_upload("One-off " + time.strftime("%Y-%m-%d %H:%M"), cid, headers, rows, "", a)
    sid = out["sheet_id"]
    # one-off: use the provided subject/body as a fresh template
    t = services.template_create("One-off " + time.strftime("%Y-%m-%d %H:%M"), p.get("subject") or "",
                                 p.get("body_html") or "", "outreach", a)
    tid = t.get("id") or t.get("template_id")
    c.close()
    d = {"template_id": tid, "campaign_id": cid, "sheet_id": sid, "account_id": int(p.get("account_id")),
         "action": {"queue": "create", "draft": "draft", "schedule": "schedule", "send": "send"}.get(p.get("action"), "create"),
         "cc": p.get("cc"), "append_signature": p.get("append_signature"),
         "at": p.get("at"), "stagger": p.get("stagger") or 0}
    if p.get("action") == "send":
        d["internal_hold_seconds"] = int(db.get_setting("send_undo_seconds") or 0)
    return services.merge(d, a)


def _p_send_reply(p, a):
    e = _one("SELECT to_addr, subject FROM emails WHERE id=?", (int(p["email_id"]),))
    return {"title": "Send reply to %s" % (e["to_addr"] if e else ""), "risk": "R4",
            "details": [{"k": "To", "v": e["to_addr"] if e else "?"}, {"k": "Subject", "v": "Re: " + (e["subject"] if e else "")}],
            "impact": ["Sends the reply from the connected account"], "irreversible": True,
            "primary": {"label": "Send reply"}}


def _e_send_reply(p, a):
    return services.email_reply(int(p["email_id"]), p.get("body"), int(p["account_id"]), False, a)


def _p_reply_draft(p, a):
    e = _one("SELECT to_addr, subject FROM emails WHERE id=?", (int(p["email_id"]),))
    return {"title": "Create Gmail reply draft for %s" % (e["to_addr"] if e else ""), "risk": "R4",
            "details": [{"k": "To", "v": e["to_addr"] if e else "?"}],
            "impact": ["Creates a Gmail draft; nothing is sent"], "irreversible": False,
            "primary": {"label": "Create reply draft"}}


def _e_reply_draft(p, a):
    return services.email_reply(int(p["email_id"]), p.get("body"), int(p["account_id"]), True, a)


def _p_set_reminders(p, a):
    return {"title": "Set reminders", "risk": "R4",
            "details": [{"k": "Every", "v": "%s day(s)" % p.get("days")},
                        {"k": "Max rounds", "v": p.get("rounds")},
                        {"k": "Text", "v": (p.get("subject") or "")[:60]}],
            "impact": ["Follows up with non-repliers at each round (evaluated per round)"],
            "irreversible": False, "primary": {"label": "Set reminders"}}


def _e_set_reminders(p, a):
    tid = None
    if p.get("body_html") or p.get("subject"):
        t = services.template_create("Reminder " + time.strftime("%Y-%m-%d"), p.get("subject") or "Follow-up",
                                     p.get("body_html") or "", "reminder", a)
        tid = t.get("id") or t.get("template_id")
    services.batch_reminders(int(p["batch_id"]), int(p.get("days") or 0), int(p.get("rounds") or 1), tid, a)
    return {"batch_id": int(p["batch_id"]), "reminder_template_id": tid}


def _p_reschedule(p, a):
    return {"title": "Reschedule %s email(s)" % (p.get("count") or "?"), "risk": "R4",
            "details": [{"k": "New time", "v": p.get("at")}, {"k": "Stagger", "v": "%s min" % (p.get("stagger") or 0)}],
            "impact": ["Moves scheduled emails to the new time"], "irreversible": False,
            "primary": {"label": "Reschedule for " + str(p.get("at"))}}


def _e_reschedule(p, a):
    c = db.conn()
    when = services.parse_when(p.get("at"))
    stagger = int(p.get("stagger") or 0)
    rows = c.execute("SELECT id FROM emails WHERE batch_id=? AND status='scheduled' ORDER BY serial",
                     (int(p["batch_id"]),)).fetchall()
    for i, r in enumerate(rows):
        t = when + __import__("datetime").timedelta(minutes=stagger * i)
        c.execute("UPDATE emails SET scheduled_at=?, error='' WHERE id=?", (t.isoformat(), r["id"]))
    c.commit()
    for r in rows:
        services.cloud_push_email(r["id"], "scheduled")
    c.close()
    return {"rescheduled": len(rows), "at": p.get("at")}


def _p_mark_handled(p, a):
    return {"title": "Mark reply handled", "risk": "R2",
            "details": [{"k": "Email", "v": str(p["email_id"])}],
            "impact": ["Moves the reply out of the needs-attention list"], "irreversible": False,
            "primary": {"label": "Mark handled"}}


def _e_mark_handled(p, a):
    return services.email_handled(int(p["email_id"]), True, a)


def _p_snooze(p, a):
    return {"title": "Snooze reply", "risk": "R2",
            "details": [{"k": "Until", "v": p.get("until")}],
            "impact": ["Hides the reply until the snooze time"], "irreversible": False,
            "primary": {"label": "Snooze until " + str(p.get("until"))}}


def _e_snooze(p, a):
    return services.email_snooze(int(p["email_id"]), p.get("until"), a)


def _p_mark_replied(p, a):
    return {"title": "Mark replied manually", "risk": "R2",
            "details": [{"k": "Email", "v": str(p["email_id"])}],
            "impact": ["Marks the email as replied"], "irreversible": False, "primary": {"label": "Mark replied"}}


def _e_mark_replied(p, a):
    return services.email_mark_replied(int(p["email_id"]), a)


def _p_set_live(p, a):
    on = p.get("live") == "1"
    return {"title": "Turn live sending " + ("ON" if on else "OFF"), "risk": "R5",
            "details": [{"k": "Safe mode", "v": "OFF" if on else "ON"}],
            "impact": ["Immediate sends become possible for everyone on the team." if on
                       else "Immediate sends are blocked for everyone; scheduling/drafts still work."],
            "irreversible": False, "primary": {"label": "Turn live sending " + ("on" if on else "off")},
            "danger": not on}


def _e_set_live(p, a):
    services.settings_update(a, {"safe_mode": p.get("live")})
    return {"safe_mode": p.get("live")}


def _p_set_limits(p, a):
    cur = services.send_rail_settings()
    return {"title": "Update sending limits", "risk": "R2",
            "details": [{"k": k, "v": ("%s -> %s" % (cur[k], p[k])) if k in p else cur[k]}
                        for k in cur],
            "impact": ["Applies to all sends from now"], "irreversible": False, "primary": {"label": "Save limits"}}


def _e_set_limits(p, a):
    services.settings_update(a, {k: v for k, v in p.items() if k in
                                 ("max_sends_per_day", "default_stagger_min", "catchup_policy",
                                  "send_undo_seconds", "bulk_confirm_threshold")})
    return {"ok": True}


def _p_set_signature(p, a):
    return {"title": "Update signature", "risk": "R2",
            "details": [{"k": "Length", "v": str(len(p.get("html") or "")) + " chars"}],
            "impact": ["Appended to new sends when 'Append signature' is on"], "irreversible": False,
            "primary": {"label": "Save signature"}}


def _e_set_signature(p, a):
    return services.set_signature(p.get("html"), a)


def _p_set_branding(p, a):
    return {"title": "Update branding", "risk": "R2",
            "details": [{"k": "Team name", "v": p.get("team_name") or "—"},
                        {"k": "Login footer", "v": p.get("tagline") or "—"},
                        {"k": "Admin email", "v": p.get("admin_email") or "—"}],
            "impact": ["Changes the login page and team name"], "irreversible": False,
            "primary": {"label": "Save branding"}}


def _e_set_branding(p, a):
    return services.set_branding(p.get("team_name"), p.get("tagline"), p.get("admin_email"), a)


def _p_set_ai(p, a):
    changes = []
    for k in ("ai_base_url", "ai_model", "ai_enabled"):
        if k in p:
            changes.append({"k": k, "v": p[k]})
    if "ai_api_key" in p:
        changes.append({"k": "ai_api_key", "v": "Key set ✓" if p["ai_api_key"] else "(cleared)"})
    return {"title": "Update AI settings", "risk": "R5", "details": changes,
            "impact": ["Changes model/base URL affect this assistant immediately"], "irreversible": False,
            "primary": {"label": "Save AI settings"}}


def _e_set_ai(p, a):
    vals = {k: p[k] for k in ("ai_base_url", "ai_model", "ai_enabled") if k in p}
    if "ai_api_key" in p:
        vals["ai_api_key"] = p["ai_api_key"]
    services.settings_update(a, vals)
    return {"ok": True}


def _p_remove_account(p, a):
    imp = services.account_remove_impact(int(p["account_id"]))
    return {"title": "Remove Gmail account %s" % imp["email"], "risk": "R5",
            "details": [{"k": "Queued/scheduled on it", "v": imp["queued_or_scheduled"]},
                        {"k": "Drafts on it", "v": imp["drafts"]}],
            "impact": ["Queued/scheduled emails on it will error with 'send account lost its login'",
                       "Existing Gmail drafts remain in Gmail"],
            "irreversible": True, "primary": {"label": "Remove account"}, "danger": True}


def _e_remove_account(p, a):
    return services.account_remove(int(p["account_id"]), a)


def _p_invite(p, a):
    return {"title": "Invite %s" % p["email"], "risk": "R2",
            "details": [{"k": "Name", "v": p.get("name") or "—"}, {"k": "Role", "v": p.get("role") or "user"}],
            "impact": ["Sends an invite; they join as pending until they accept"], "irreversible": False,
            "primary": {"label": "Send invite"}}


def _e_invite(p, a):
    return services.member_invite(p["email"], p.get("name") or "", a)


def _p_change_role(p, a):
    u = db.get_user(int(p["user_id"]))
    return {"title": "Change %s's role to %s" % (u["email"] if u else "?", p["role"]), "risk": "R3",
            "details": [{"k": "From", "v": u["role"] if u else "?"}],
            "impact": ["Takes effect immediately"], "irreversible": False, "danger": True,
            "primary": {"label": "Change role"}}


def _e_change_role(p, a):
    services.member_set_role(int(p["user_id"]), p["role"], a)
    return {"user_id": int(p["user_id"]), "role": p["role"]}


def _p_remove_member(p, a):
    u = db.get_user(int(p["user_id"]))
    return {"title": "Remove %s from the team" % (u["email"] if u else "?"), "risk": "R3",
            "details": [{"k": "Email", "v": u["email"] if u else "?"}],
            "impact": ["They lose access immediately; their sent history stays in the app"],
            "irreversible": True, "danger": True, "primary": {"label": "Remove member"}}


def _e_remove_member(p, a):
    services.member_remove(int(p["user_id"]), a)
    return {"user_id": int(p["user_id"])}


def _p_approve_join(p, a):
    u = db.get_user(int(p["user_id"]))
    return {"title": "Approve %s" % (u["email"] if u else "?"), "risk": "R2",
            "details": [{"k": "Note", "v": (u["request_note"] if u else "") or "—"}],
            "impact": ["They become an active member"], "irreversible": False, "primary": {"label": "Approve"}}


def _e_approve_join(p, a):
    services.member_set_status(int(p["user_id"]), "active", a)
    return {"user_id": int(p["user_id"])}


def _p_deny_join(p, a):
    u = db.get_user(int(p["user_id"]))
    return {"title": "Deny %s" % (u["email"] if u else "?"), "risk": "R2",
            "details": [], "impact": ["They are marked denied"], "irreversible": False,
            "primary": {"label": "Deny request"}}


def _e_deny_join(p, a):
    services.member_set_status(int(p["user_id"]), "denied", a)
    return {"user_id": int(p["user_id"])}


def _p_export(p, a):
    return {"title": "Export contacts to CSV", "risk": "R5",
            "details": [{"k": "Rows", "v": _one("SELECT COUNT(*) n FROM contacts")["n"]}],
            "impact": ["Generates a CSV download of all contacts"], "irreversible": False,
            "primary": {"label": "Export CSV"}}


def _e_export(p, a):
    return {"hint": "open", "page": "/api/export/contacts.csv"}


def _p_set_budget(p, a):
    return {"title": "Update AI budget limits", "risk": "R2",
            "details": [{"k": k, "v": v} for k, v in p.items() if k.startswith("ai_")],
            "impact": ["Applies from the next AI call"], "irreversible": False,
            "primary": {"label": "Save budget"}}


def _e_set_budget(p, a):
    import usage
    return usage.set_budget({k: v for k, v in p.items() if k in
                             ("ai_daily_calls_per_user", "ai_daily_tokens_per_user", "ai_daily_calls_workspace")}, a)


# --------------------------------------------------------------------- register

def _S(props, required=None):
    s = {"type": "object", "properties": props}
    if required:
        s["required"] = required
    return s


reg(Action("get_overview", R0, ALL, "Platform totals: sent, replied, bounced, scheduled, open replies, cap.",
           read=read_overview, category="read"))
reg(Action("list_campaigns", R0, ALL, "List campaigns with counts.", read=read_campaigns, category="read"))
reg(Action("get_campaign", R0, ALL, "One campaign's stats, lists and batches.",
           _S({"campaign_id": {"type": "integer"}}, ["campaign_id"]), read=read_campaign, category="read"))
reg(Action("list_templates", R0, ALL, "List templates.", read=read_templates, category="read"))
reg(Action("get_template", R0, ALL, "One template's detail.",
           _S({"template_id": {"type": "integer"}}, ["template_id"]), read=read_template, category="read"))
reg(Action("list_contacts", R0, ALL, "Contacts with search/limit.",
           _S({"search": {"type": "string"}, "limit": {"type": "integer"}}), read=read_contacts, category="read"))
reg(Action("get_replies", R0, ALL, "Replies with filters (campaign, member, status, search).",
           _S({"campaign_id": {"type": "integer"}, "member": {"type": "string"}, "status": {"type": "string"},
               "search": {"type": "string"}, "limit": {"type": "integer"}}), read=read_replies, category="read"))
reg(Action("list_team", R0, ALL, "Team members and who sent what.", read=read_team, category="read"))
reg(Action("recent_activity", R0, ALL, "Activity log with filters.",
           _S({"type": {"type": "string"}, "actor": {"type": "string"}, "date": {"type": "string"},
               "limit": {"type": "integer"}}), read=read_activity, category="read"))
reg(Action("get_attention_items", R0, ALL, "Home 'needs attention' items.", read=read_attention, category="read"))
reg(Action("get_upcoming_sends", R0, ALL, "Scheduled sends next.", read=read_upcoming, category="read"))
reg(Action("list_batches", R0, ALL, "Batches with status counts.", read=read_batches, category="read"))
reg(Action("get_batch", R0, ALL, "One batch detail.",
           _S({"batch_id": {"type": "integer"}}, ["batch_id"]), read=read_batch, category="read"))
reg(Action("get_reply_thread", R0, ALL, "A reply thread (original send + reply meta).",
           _S({"email_id": {"type": "integer"}}, ["email_id"]), read=read_reply_thread, category="read"))
reg(Action("list_bounces", R0, ALL, "Bounced emails with reasons.", read=read_bounces, category="read"))
reg(Action("list_sheets", R0, ALL, "Contact lists.", read=read_sheets, category="read"))
reg(Action("get_sheet", R0, ALL, "One list's columns/rows.",
           _S({"sheet_id": {"type": "integer"}}, ["sheet_id"]), read=read_sheet, category="read"))
reg(Action("list_attachments", R0, ALL, "Attachment library.", read=read_attachments, category="read"))
reg(Action("get_results", R0, ALL, "Analytics with person/campaign/date filters.",
           _S({"person": {"type": "string"}, "campaign_id": {"type": "integer"},
               "date_from": {"type": "string"}, "date_to": {"type": "string"}}), read=read_results, category="read"))
reg(Action("list_gmail_accounts", R0, ALL, "Gmail accounts + status.", read=read_gmail_accounts, category="read"))
reg(Action("get_settings", R0, ALL, "Non-secret settings.", read=read_settings, category="read"))
reg(Action("get_send_status", R0, ALL, "Safe mode + daily cap state.", read=read_send_status, category="read"))
reg(Action("render_preview", R0, ALL, "Render one email for a contact.",
           _S({"template_id": {"type": "integer"}, "mapping": {"type": "object"}, "row": {"type": "object"},
               "append_signature": {"type": "boolean"}}, ["template_id"]), read=read_preview, category="read"))
reg(Action("get_sync_status", R0, ALL, "Read-only sync status.", read=read_sync_status, category="read"))
reg(Action("list_join_requests", R0, ADMIN, "Pending join requests.", read=read_join_requests, category="read"))
reg(Action("get_help", R0, ALL, "Answer a how-to from in-app help.",
           _S({"topic": {"type": "string"}}, ["topic"]), read=read_help, category="read"))
reg(Action("get_ai_usage", R0, ALL, "AI usage and limits (admin: per user).", read=read_ai_usage, category="read"))
reg(Action("run_presend_check", R0, ALL, "Deterministic pre-send check.", read=read_presend, category="read"))

reg(Action("create_campaign", R2, ALL, "Create a campaign.",
           _S({"name": {"type": "string"}, "description": {"type": "string"}}, ["name"]),
           preview=_p_create_campaign, execute=_e_create_campaign, category="campaign"))
reg(Action("update_campaign", R2, ALL, "Edit a campaign.",
           _S({"campaign_id": {"type": "integer"}, "name": {"type": "string"}, "description": {"type": "string"}},
              ["campaign_id"]), preview=_p_update_campaign, execute=_e_update_campaign, category="campaign"))
reg(Action("delete_campaign", R3, ALL, "Delete a campaign (app records only).",
           _S({"campaign_id": {"type": "integer"}}, ["campaign_id"]),
           preview=_p_delete_campaign, execute=_e_delete_campaign, category="campaign"))
reg(Action("attach_sheet_to_campaign", R2, ALL, "Attach a list to a campaign.",
           _S({"sheet_id": {"type": "integer"}, "campaign_id": {"type": "integer"}},
              ["sheet_id", "campaign_id"]), preview=_p_attach_sheet, execute=_e_attach_sheet, category="campaign"))
reg(Action("detach_sheet_from_campaign", R2, ALL, "Detach a list from a campaign.",
           _S({"sheet_id": {"type": "integer"}, "campaign_id": {"type": "integer"}}, ["sheet_id"]),
           preview=_p_detach_sheet, execute=_e_detach_sheet, category="campaign"))
reg(Action("set_campaign_template", R2, ALL, "Set a campaign's default template.",
           _S({"campaign_id": {"type": "integer"}, "template_id": {"type": "integer"}},
              ["campaign_id", "template_id"]), preview=_p_set_campaign_template,
           execute=_e_set_campaign_template, category="campaign"))
reg(Action("create_template", R2, ALL, "Save a new template.",
           _S({"name": {"type": "string"}, "subject": {"type": "string"}, "body_html": {"type": "string"},
               "kind": {"type": "string"}, "active": {"type": "boolean"}}, ["name"]),
           preview=_p_create_template, execute=_e_create_template, category="template"))
reg(Action("update_template", R2, ALL, "Update a template.",
           _S({"template_id": {"type": "integer"}, "name": {"type": "string"}, "subject": {"type": "string"},
               "body_html": {"type": "string"}}, ["template_id"]),
           preview=_p_update_template, execute=_e_update_template, category="template"))
reg(Action("rename_template", R2, ALL, "Rename a template.",
           _S({"template_id": {"type": "integer"}, "name": {"type": "string"}}, ["template_id", "name"]),
           preview=_p_rename_template, execute=_e_rename_template, category="template"))
reg(Action("activate_template", R2, ALL, "Make a template active.",
           _S({"template_id": {"type": "integer"}}, ["template_id"]),
           preview=_p_activate_template, execute=_e_activate_template, category="template"))
reg(Action("delete_template", R3, ALL, "Delete a template.",
           _S({"template_id": {"type": "integer"}}, ["template_id"]),
           preview=_p_delete_template, execute=_e_delete_template, category="template"))
reg(Action("import_contacts_sheet", R2, ALL, "Import a contact sheet.",
           _S({"name": {"type": "string"}, "headers": {"type": "array"}, "rows": {"type": "array"},
               "campaign_id": {"type": "integer"}, "filename": {"type": "string"}}),
           preview=_p_import_sheet, execute=_e_import_sheet, category="import"))
reg(Action("delete_sheet", R3, ALL, "Delete a contact list.",
           _S({"sheet_id": {"type": "integer"}}, ["sheet_id"]),
           preview=_p_delete_sheet, execute=_e_delete_sheet, category="import"))
reg(Action("update_contact", R2, ALL, "Update contacts (bounce fix).",
           _S({"updates": {"type": "object"}}, ["updates"]),
           preview=_p_update_contacts, execute=_e_update_contacts, category="contacts"))
reg(Action("save_gmail_drafts", R4, ALL, "Create Gmail drafts (nothing sent).",
           _S({"ids": {"type": "array"}, "batch_id": {"type": "integer"}, "account_id": {"type": "integer"},
               "count": {"type": "integer"}}), preview=_p_save_gmail_drafts, execute=_e_save_gmail_drafts,
           category="send"))
reg(Action("prepare_send", R4, ALL, "Prepare a campaign send (queue/draft/schedule/send).",
           _S({"campaign_id": {"type": "integer"}, "template_id": {"type": "integer"},
               "sheet_id": {"type": "integer"}, "serial_from": {"type": "integer"}, "serial_to": {"type": "integer"},
               "contact_ids": {"type": "array"}, "mapping": {"type": "object"}, "cc": {"type": "string"},
               "append_signature": {"type": "boolean"},
               "action": {"type": "string", "enum": ["queue", "draft", "schedule", "send"]},
               "at": {"type": "string"}, "stagger": {"type": "integer"}, "account_id": {"type": "integer"},
               "count": {"type": "integer"}, "campaign_name": {"type": "string"},
               "template_name": {"type": "string"},
               "reminders": {"type": "string"}}, ["template_id", "action"]),
           preview=_p_prepare_send, execute=_e_prepare_send, category="send"))
reg(Action("send_test_email", R4, ALL, "Send a rendered test email.",
           _S({"account_id": {"type": "integer"}, "to": {"type": "string"}, "subject": {"type": "string"},
               "body_html": {"type": "string"}, "template_id": {"type": "integer"},
               "content_hash": {"type": "string"}}, ["account_id", "to"]),
           preview=_p_send_test, execute=_e_send_test, category="send"))
reg(Action("send_adhoc_email", R4, ALL, "One-off email to specific addresses (single-contact batch).",
           _S({"campaign_id": {"type": "integer"}, "recipients": {"type": "array"}, "subject": {"type": "string"},
               "body_html": {"type": "string"}, "account_id": {"type": "integer"}, "action": {"type": "string"},
               "at": {"type": "string"}, "stagger": {"type": "integer"}, "cc": {"type": "string"},
               "append_signature": {"type": "boolean"}},
              ["recipients"]), preview=_p_send_adhoc, execute=_e_send_adhoc, category="send"))
reg(Action("send_reply", R4, ALL, "Send a reply to a recipient.",
           _S({"email_id": {"type": "integer"}, "body": {"type": "string"}, "account_id": {"type": "integer"}},
              ["email_id", "body"]), preview=_p_send_reply, execute=_e_send_reply, category="reply"))
reg(Action("create_reply_draft", R4, ALL, "Create a Gmail reply draft.",
           _S({"email_id": {"type": "integer"}, "body": {"type": "string"}, "account_id": {"type": "integer"}},
              ["email_id", "body"]), preview=_p_reply_draft, execute=_e_reply_draft, category="reply"))
reg(Action("set_reminders", R4, ALL, "Set follow-up reminders on a batch.",
           _S({"batch_id": {"type": "integer"}, "days": {"type": "integer"}, "rounds": {"type": "integer"},
               "subject": {"type": "string"}, "body_html": {"type": "string"}}, ["batch_id"]),
           preview=_p_set_reminders, execute=_e_set_reminders, category="send"))
reg(Action("reschedule_batch", R4, ALL, "Reschedule a batch's scheduled emails.",
           _S({"batch_id": {"type": "integer"}, "at": {"type": "string"}, "stagger": {"type": "integer"}},
              ["batch_id", "at"]), preview=_p_reschedule, execute=_e_reschedule, category="send"))
reg(Action("cancel_emails", R3, ALL, "Cancel queued/scheduled/drafted emails.",
           _S({"campaign_id": {"type": "integer"}, "batch_id": {"type": "integer"}, "ids": {"type": "array"}}),
           preview=_p_cancel_emails, execute=_e_cancel_emails, category="send"))
reg(Action("mark_reply_handled", R2, ALL, "Mark a reply handled.",
           _S({"email_id": {"type": "integer"}}, ["email_id"]),
           preview=_p_mark_handled, execute=_e_mark_handled, category="reply"))
reg(Action("snooze_reply", R2, ALL, "Snooze a reply.",
           _S({"email_id": {"type": "integer"}, "until": {"type": "string"}}, ["email_id", "until"]),
           preview=_p_snooze, execute=_e_snooze, category="reply"))
reg(Action("mark_replied_manual", R2, ALL, "Mark an email replied manually.",
           _S({"email_id": {"type": "integer"}}, ["email_id"]),
           preview=_p_mark_replied, execute=_e_mark_replied, category="reply"))
reg(Action("set_live_sending", R5, ADMIN, "Toggle live sending (safe mode).",
           _S({"live": {"type": "string", "enum": ["0", "1"]}}, ["live"]),
           preview=_p_set_live, execute=_e_set_live, category="settings"))
reg(Action("set_send_limits", R2, ADMIN, "Set daily cap, stagger, catch-up, undo seconds, bulk threshold.",
           _S({"max_sends_per_day": {"type": "integer"}, "default_stagger_min": {"type": "integer"},
               "catchup_policy": {"type": "string"}, "send_undo_seconds": {"type": "integer"},
               "bulk_confirm_threshold": {"type": "integer"}}),
           preview=_p_set_limits, execute=_e_set_limits, category="settings"))
reg(Action("set_signature", R2, ADMIN, "Set the signature HTML.",
           _S({"html": {"type": "string"}}, ["html"]), preview=_p_set_signature, execute=_e_set_signature,
           category="settings"))
reg(Action("set_branding", R2, ADMIN, "Set team branding.",
           _S({"team_name": {"type": "string"}, "tagline": {"type": "string"},
               "admin_email": {"type": "string"}}),
           preview=_p_set_branding, execute=_e_set_branding, category="settings"))
reg(Action("set_ai_settings", R5, ADMIN, "Set AI base URL / model / enabled / key.",
           _S({"ai_base_url": {"type": "string"}, "ai_model": {"type": "string"},
               "ai_enabled": {"type": "string"}, "ai_api_key": {"type": "string"}}),
           preview=_p_set_ai, execute=_e_set_ai, category="settings"))
reg(Action("remove_gmail_account", R5, ADMIN, "Remove a Gmail account.",
           _S({"account_id": {"type": "integer"}}, ["account_id"]),
           preview=_p_remove_account, execute=_e_remove_account, category="settings"))
reg(Action("invite_member", R2, ADMIN, "Invite a member.",
           _S({"email": {"type": "string"}, "name": {"type": "string"}, "role": {"type": "string"}}, ["email"]),
           preview=_p_invite, execute=_e_invite, category="team"))
reg(Action("change_role", R3, ADMIN, "Change a member's role.",
           _S({"user_id": {"type": "integer"}, "role": {"type": "string"}}, ["user_id", "role"]),
           preview=_p_change_role, execute=_e_change_role, category="team"))
reg(Action("remove_member", R3, ADMIN, "Remove a member.",
           _S({"user_id": {"type": "integer"}}, ["user_id"]),
           preview=_p_remove_member, execute=_e_remove_member, category="team"))
reg(Action("approve_join", R2, ADMIN, "Approve a join request.",
           _S({"user_id": {"type": "integer"}}, ["user_id"]),
           preview=_p_approve_join, execute=_e_approve_join, category="team"))
reg(Action("deny_join", R2, ADMIN, "Deny a join request.",
           _S({"user_id": {"type": "integer"}}, ["user_id"]),
           preview=_p_deny_join, execute=_e_deny_join, category="team"))
reg(Action("export_data", R5, ADMIN, "Export contacts CSV.",
           preview=_p_export, execute=_e_export, category="settings"))
reg(Action("set_ai_budget", R2, ADMIN, "Set AI budget limits.",
           _S({"ai_daily_calls_per_user": {"type": "integer"}, "ai_daily_tokens_per_user": {"type": "integer"},
               "ai_daily_calls_workspace": {"type": "integer"}}),
           preview=_p_set_budget, execute=_e_set_budget, category="settings"))


# --------------------------------------------------------------------- gate

import approval
import time as _time


def llm_tools(user):
    """Tool list the model may call: R0 reads (role-filtered) + start_flow + ask_user."""
    out = [a.spec() for a in all_actions()
           if a.risk == R0 and a.allowed(user) and a.name != "run_presend_check"]
    out.append({"type": "function", "function": {
        "name": "start_flow",
        "description": ("Start a guided flow for anything that changes data or sends email "
                        "(create/edit/delete campaign, template, import list, send/draft/schedule "
                        "emails, reply, bounce fix, settings, team, export). Pass every value the "
                        "user already gave in slots. The app asks for the rest with buttons and "
                        "requests approval for each gated step."),
        "parameters": {"type": "object", "properties": {
            "flow_id": {"type": "string", "description": "one of: send_campaign, draft_email, reply, "
                        "import_list, template_from_file, write_template, edit_template, create_campaign, "
                        "edit_campaign, delete_campaign, cancel_sends, bounces_fix, reminders, test_email, "
                        "settings, invite_member, change_role, remove_member, join_requests, "
                        "remove_gmail_account, gmail_connect, export_data, reschedule_batch"},
            "slots": {"type": "object", "description": "values already known, e.g. {"
                        "\"campaign_id\": 3, \"name\": \"Winter 2027\"}"}},
            "required": ["flow_id"]}}})
    out.append({"type": "function", "function": {
        "name": "ask_user",
        "description": "Rarely: ask one clarifying question when you cannot tell which flow the user "
                        "wants. Provide up to 4 short options.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}}},
            "required": ["question"]}}})
    return out


def read_direct(name, params, actor):
    """Run an R0 read (or R1 generate) - no gate. Raises for R2+."""
    a = get(name)
    if not a:
        raise ActionError("Unknown tool: " + name, 404)
    if not a.allowed(actor):
        raise ActionError("You do not have permission for that.", 403)
    if a.risk == R0:
        if not a.read:
            raise ActionError("Not a read tool.", 400)
        return a.read(params or {}, actor)
    if a.risk == R1:
        if not a.generate:
            raise ActionError("Not a generate tool.", 400)
        return a.generate(params or {}, actor)
    raise ApprovalRequired(name + " requires a user-approved card (risk " + a.risk + ").")


def execute_gated(action_name, approval_id, conv_id, uid, ack=None, expected_count=None):
    """The ONLY way R2-R5 actions run: consume a valid approval, execute the
    STORED params, verify, log. Raises ApprovalError / ApprovalRequired."""
    a = get(action_name)
    if not a:
        raise ActionError("Unknown action: " + action_name, 404)
    if a.risk in (R0, R1):
        raise ApprovalRequired(action_name + " does not use the approval gate.")
    row = approval.get(approval_id)
    if not row or row["action"] != action_name:
        raise approval.ApprovalError("That approval is no longer valid.", 404)
    # render integrity for sends: re-hash what would be sent now
    if a.risk == R4 and action_name in ("prepare_send", "send_adhoc_email") and row.get("render_hash"):
        import presend
        try:
            stored = json.loads(row["params_json"])
            if a.name == "prepare_send" and stored.get("template_id"):
                cur = presend.render_hash_of(stored)
                if cur and row["render_hash"] and cur != row["render_hash"]:
                    raise approval.ApprovalError(
                        "The preview changed since you reviewed it (template, list, mapping, signature "
                        "or attachments). Review the updated preview.", 409)
            if a.name == "prepare_send" and stored.get("sheet_id") and expected_count is not None:
                import services
                live = len(services.audience_rows(int(stored["sheet_id"]), stored.get("serial_from"),
                                                  stored.get("serial_to"), None))
                if live != int(expected_count):
                    raise approval.ApprovalError(
                        "The recipient list changed since you reviewed it (%s -> %s). Review the "
                        "updated preview." % (expected_count, live), 409)
        except approval.ApprovalError:
            raise
        except Exception:
            pass
    # bulk-send acknowledgement is only a property of OUTBOUND sends; queue and
    # draft actions never leave the building and are exempt (spec 17.1.16)
    eff_count = expected_count
    if action_name == "prepare_send" and expected_count is not None:
        try:
            sp = json.loads(row["params_json"])
            if sp.get("action") in ("queue", "draft"):
                eff_count = None
        except Exception:
            pass
    params = approval.consume(approval_id, conv_id, uid, expected_count=eff_count, ack=ack)
    actor = db.get_user(int(uid))
    if not actor or actor.get("status") != "active":
        raise approval.ApprovalError("Session expired - sign in again.", 401)
    if not a.allowed(actor):
        raise approval.ApprovalError("You no longer have permission for this action.", 403)
    res = a.execute(params, actor) or {}
    detail = res.get("activity") or a.name
    db.log_activity("ask_ai_" + a.name, str(detail)[:200], _actor_email(actor) + " via Ask AI")
    verify = a.verify(params, res, actor) if a.verify else res
    return res, verify
