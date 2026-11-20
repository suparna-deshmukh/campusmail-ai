"""Flow Engine (spec 5.2) + all Ask AI flows.

Flows are declarative state machines walked by deterministic code. The LLM
never walks a flow; it only routes into one (start_flow) with slots the user
already gave. Interactive steps pause on a card; gates mint approval tokens.
Compact mode collapses the send_params step group into one compact_review card.

Outcome protocol (returned to the client):
  {"kind":"message","text":...}  narration (number-guarded)
  {"kind":"data","data":{...}}   rendered data card (no pending state)
  {"kind":"card","card":env}     a new pending card to render
  {"kind":"result","data":{...}} deterministic result card
  {"kind":"error","message":...} inline error (card stays pending)
"""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta

import actions
import ai_client
import approval
import cards
import db
import docx_convert
import presend
import services
import usage
from cards import CardError
from services import ServiceError

NOW = lambda: datetime.now().astimezone()


def actor(conv):
    return db.get_user(int(conv["uid"]))


def _mode(conv):
    return (conv.get("mode") or "guided").lower()


def _uid(conv):
    return int(conv["uid"])


# ---------------------------------------------------------------- helpers

def card_outcome(conv, ctype, payload, step=None, ttl=600):
    env = cards.new_card(conv, ctype, payload, step_id=step,
                         flow_id=(conv.get("flow") or {}).get("flow_id"), ttl=ttl)
    conv["flow"]["st"]["pending_card"] = env["id"]
    conv["pending_card"] = env["id"]
    return {"kind": "card", "card": env}


def _account_default(conv):
    last = db.get_user_setting(_uid(conv), "last_account_id")
    if last:
        for a in services.account_list(actor(conv)):
            if str(a["id"]) == str(last) and db.account_token_json(a["id"]):
                return int(last)
    for a in services.account_list(actor(conv)):
        if db.account_token_json(a["id"]):
            return int(a["id"])
    return 0


def _remember(conv, key, value):
    if value is None:
        return
    try:
        db.set_user_setting(_uid(conv), key, json.dumps(value))
    except Exception:
        pass


def _remembered(conv, key, default=None):
    v = db.get_user_setting(_uid(conv), key)
    if v is None:
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


def _campaigns():
    c = db.conn()
    rows = [dict(r) for r in c.execute("SELECT id, name FROM campaigns ORDER BY name").fetchall()]
    c.close()
    return rows


def _sheets_of(campaign_id):
    c = db.conn()
    rows = [dict(r) for r in c.execute("SELECT id, name, n_rows FROM sheets WHERE campaign_id=? ORDER BY id",
                                       (campaign_id,)).fetchall()]
    c.close()
    return rows


def _templates():
    c = db.conn()
    rows = [dict(r) for r in c.execute(
        "SELECT id, name, is_active FROM email_templates ORDER BY is_active DESC, id").fetchall()]
    c.close()
    return rows


def _campaign_template_pref(campaign_id):
    c = db.conn()
    r = c.execute("SELECT template_id FROM campaign_meta WHERE campaign_id=?", (campaign_id,)).fetchone()
    c.close()
    return int(r["template_id"]) if r and r["template_id"] else 0


def _one_row(sql, args):
    c = db.conn()
    r = c.execute(sql, args).fetchone()
    c.close()
    return dict(r) if r else None


def _gen(conv, fn, *args):
    """One R1 LLM call with budget gate. Raises usage.BudgetExhausted."""
    return fn(*args, user_id=_uid(conv))


def _budget_outcomes(conv, e):
    st = usage.status(_uid(conv))
    return [card_outcome(conv, "budget", {
        "reason": str(e.reason), "reset_at": st.get("reset_at"),
        "stays_working": ["Reading data", "Importing lists", "Sending (already reviewed)",
                          "Settings and team actions", "Everything not needing AI writing"]},
        step="budget", ttl=3600)]


def send_params(st):
    """Canonical prepare_send params from flow slots."""
    s = st["slots"]
    p = {
        "campaign_id": int(s.get("campaign_id") or 0),
        "template_id": int(s.get("template_id") or 0),
        "sheet_id": int(s.get("sheet_id") or 0),
        "mapping": s.get("mapping") or {},
        "action": s.get("action") or "queue",
        "account_id": int(s.get("account_id") or 0),
        "append_signature": bool(s.get("append_signature")),
    }
    if s.get("serial_from"):
        p["serial_from"] = int(s["serial_from"])
    if s.get("serial_to"):
        p["serial_to"] = int(s["serial_to"])
    if s.get("cc"):
        p["cc"] = s["cc"]
    if s.get("attachments"):
        p["attachments"] = s["attachments"]
    if s.get("at"):
        p["at"] = s["at"]
    if s.get("stagger"):
        p["stagger"] = int(s["stagger"])
    if p["action"] == "send":
        p["internal_hold_seconds"] = int(db.get_setting("send_undo_seconds") or 0)
    if s.get("cap_ack"):
        p["cap_ack"] = True
    rows = services.audience_rows(int(s.get("sheet_id") or 0),
                                  p.get("serial_from"), p.get("serial_to"), None)
    p["_rows"] = rows
    p["count"] = len(rows)
    cam = _one_row("SELECT name FROM campaigns WHERE id=?", (p["campaign_id"],))
    tpl = _one_row("SELECT name FROM email_templates WHERE id=?", (p["template_id"],))
    p["campaign_name"] = cam["name"] if cam else ""
    p["template_name"] = tpl["name"] if tpl else ""
    if s.get("reminders_set"):
        p["reminders"] = "%d day(s) x %d round(s)" % (s.get("rem_days") or 0, s.get("rem_rounds") or 1)
    return p


def _clean(params):
    return {k: v for k, v in (params or {}).items() if not k.startswith("_")}


def _render_samples(st, params, n=12):
    c = db.conn()
    tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (int(params["template_id"]),)).fetchone()
    c.close()
    if not tpl:
        return []
    tpl = dict(tpl)
    sig = services.signature_html() if params.get("append_signature") else ""
    out = []
    for r in (params.get("_rows") or [])[:n]:
        s, b = services.render_email(tpl, services.row_vals(r), params.get("mapping") or {})
        if sig:
            b = services.append_signature(b)
        out.append({"serial": r["serial"], "to": r["hr_email"], "subject": s, "body_html": b})
    return out


def _bulk_state(count, threshold, action="send"):
    threshold = int(threshold or 50)
    if action in ("queue", "draft"):
        return {"needed": False}  # nothing leaves the building
    if count < threshold:
        return {"needed": False}
    if count < 4 * threshold:
        return {"needed": True, "checkbox": True, "typed": False}
    return {"needed": True, "checkbox": True, "typed": True}


# ------------------------------------------------------------- flow base

class Flow:
    id = ""
    title = ""

    def start(self, conv, slots):
        conv["flow"] = {"flow_id": self.id, "st": {
            "slots": dict(slots or {}), "data": {}, "version": 1,
            "pending_card": None, "gate": None, "last_res": None}}
        conv["pending_card"] = None
        return self.run(conv)

    def run(self, conv):
        raise NotImplementedError

    def respond(self, conv, card, response):
        raise NotImplementedError

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": self.title + " - done",
                                            "data": res}}]

    def resume_after_subflow(self, conv):
        # Default: the parent already completed its own work (gate executed)
        # before starting the subflow, so it simply ends when the subflow ends.
        conv["flow"] = None
        conv["pending_card"] = None
        return []


FLOWS = {}


def reg_flow(cls):
    FLOWS[cls.id] = cls
    return cls


def start_flow(conv, flow_id, slots=None):
    cls = FLOWS.get(flow_id)
    if not cls:
        raise CardError("Unknown flow: " + str(flow_id), 404)
    if conv.get("flow"):
        conv.setdefault("flow_stack", []).append(conv["flow"])
    return cls().start(conv, slots)


def on_card_response(conv, card, response):
    flow = conv.get("flow")
    if not flow:
        raise CardError("No flow is active for that card.", 409)
    return FLOWS[flow["flow_id"]]().respond(conv, card, response)


def after_gate(conv, action, res):
    flow = conv.get("flow")
    if not flow:
        return [{"kind": "result", "data": {"kind": "result", "title": "Done", "data": res}}]
    st = flow["st"]
    # non-secret fields only: flow state persists to storage and must never
    # carry gate params verbatim (secrets such as API keys are R5-critical)
    st["last_gate_template_id"] = (st.get("gate") or {}).get("params", {}).get("template_id")
    st["gate"] = None
    st["last_res"] = res
    st["last_gate_action"] = action
    return FLOWS[flow["flow_id"]]().post_gate(conv, res)


def finish_flow(conv):
    """Called when a flow ends with no pending card (done/cancelled)."""
    conv["flow"] = None
    conv["pending_card"] = None
    if conv.get("flow_stack"):
        parent = conv["flow_stack"].pop()
        conv["flow"] = parent
        return FLOWS[parent["flow_id"]]().resume_after_subflow(conv)
    if conv.get("plan"):
        return _plan_next_options(conv, completed=True)
    return []


def cancel_flow(conv, reason="cancelled"):
    flow = conv.get("flow")
    if flow:
        st = flow["st"]
        if st.get("gate") and st["gate"].get("approval_id"):
            approval.void(st["gate"]["approval_id"])
    out = []
    if conv.get("flow_stack"):
        conv["flow"] = None
        parent = conv["flow_stack"].pop()
        conv["flow"] = parent
        out.append({"kind": "message", "text": "Back to " + parent["flow_id"] + "."})
        return out + FLOWS[parent["flow_id"]]().resume_after_subflow(conv)
    conv["flow"] = None
    approval.void_for_conv(conv["_conv_id"])
    label = {"cancelled": "Cancelled - nothing was done.",
             "declined": "Declined - nothing was done.",
             "done": "Done."}.get(reason, reason)
    out.append({"kind": "message", "text": label})
    if conv.get("plan"):
        out += _plan_next_options(conv, completed=False)
    else:
        env = cards.new_card(conv, "options", {"question": "What would you like to do?",
                                               "options": [{"id": "new", "label": "Start something else"},
                                                           {"id": "done", "label": "Done"}],
                                               "allow_other": True}, step_id="post_cancel")
        conv["pending_card"] = env["id"]
        out.append({"kind": "card", "card": env})
    return out


def on_compact_update(conv, card, fields, version):
    flow = conv.get("flow")
    if not flow or flow["flow_id"] != "send_campaign":
        raise CardError("Compact review not active.", 409)
    st = flow["st"]
    if int(version or 0) != int(st["version"]):
        return [{"kind": "error", "message": "Stale response - this card was updated elsewhere."}]
    for k, v in (fields or {}).items():
        st["slots"][k] = v
    st["version"] += 1
    f = SendCampaign()
    p = f._compact_payload(conv)
    if st.get("gate") and st["gate"].get("approval_id"):
        approval.void(st["gate"]["approval_id"])
    params = f._gate_params(conv)
    ap = approval.mint("prepare_send", _clean(params), conv["_conv_id"], _uid(conv),
                       render_hash=p["render_hash"])
    st["gate"] = {"action": "prepare_send", "approval_id": ap["approval_id"],
                  "params": _clean(params), "count": p["count"]}
    p["approval_id"] = ap["approval_id"]
    p["version"] = st["version"]
    return [{"kind": "compact_update", "data": p}]


# ---------------------------------------------------------------- plan flow

def _plan_next_options(conv, completed):
    plan = conv.get("plan") or {}
    steps = plan.get("steps") or []
    idx = plan.get("idx", 0)
    if completed and idx >= len(steps):
        conv["plan"] = None
        return [{"kind": "result", "data": {"kind": "result", "title": "Plan complete",
                                            "data": {"steps_done": idx}}}]
    next_label = "Continue: " + (steps[idx].get("label") if idx < len(steps) else "step %d" % (idx + 1))
    opts = [{"id": "continue", "label": next_label}, {"id": "stop", "label": "Stop plan"}]
    if not completed:
        opts.insert(1, {"id": "skip", "label": "Skip this step"})
    return [card_outcome(conv, "options", {"question": "Plan step %d of %d." % (min(idx + 1, len(steps)), len(steps)),
                                           "options": opts, "allow_other": False, "plan": True}, step="plan_next")]


def start_plan(conv, steps):
    steps = [s for s in (steps or []) if isinstance(s, dict) and s.get("flow_id") in FLOWS][:6]
    if not steps:
        raise CardError("No valid steps in that plan.", 400)
    conv["plan"] = {"steps": steps, "idx": 0}
    out = [{"kind": "plan", "data": {"steps": [
        {"label": s.get("label") or s["flow_id"], "flow": s["flow_id"]} for s in steps]}}]
    conv["flow"] = None
    return out + _start_plan_step(conv)


def _start_plan_step(conv):
    plan = conv["plan"]
    step = plan["steps"][plan["idx"]]
    plan["idx"] += 1
    out = [{"kind": "message", "text": "Step %d: %s" % (plan["idx"], step.get("label") or step["flow_id"])}]
    out += start_flow(conv, step["flow_id"], step.get("slots") or {})
    return out


# ================================================================ send_campaign

@reg_flow
class SendCampaign(Flow):
    id = "send_campaign"
    title = "Send emails"

    def resume_after_subflow(self, conv):
        st = conv["flow"]["st"]
        d = st.get("data", {})
        if d.pop("resume_after_import", None):
            if st.get("_imported_sheet_id"):
                st["slots"]["sheet_id"] = st["_imported_sheet_id"]
            return self.run(conv)
        if d.pop("resume_after_template", None):
            if st.get("_saved_template_id"):
                st["slots"]["template_id"] = st["_saved_template_id"]
            return self.run(conv)
        return self.run(conv)

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        # slots arrive from the model: validate the numeric ones (never trust)
        try:
            s["campaign_id"] = int(s["campaign_id"]) if s.get("campaign_id") else None
        except (TypeError, ValueError):
            s["campaign_id"] = None
        out = []
        if not s.get("campaign_id"):
            cams = _campaigns()
            if not cams:
                out.append({"kind": "data", "data": {"type": "empty", "text": "No campaigns yet."}})
                out.append(card_outcome(conv, "options", {
                    "question": "Create a campaign to send to?",
                    "options": [{"id": "create", "label": "Create a campaign"},
                                {"id": "cancel", "label": "Cancel"}]}, step="create_choice"))
                return out
            opts = [{"id": str(c["id"]), "label": c["name"]} for c in cams]
            payload = {"question": "Which campaign?", "options": opts[:4], "allow_other": False}
            if len(opts) > 4:
                payload["more"] = {"label": "More", "options": opts[4:]}
            out.append(card_outcome(conv, "options", payload, step="pick_campaign"))
            return out
        if not s.get("sheet_id"):
            sheets = _sheets_of(int(s["campaign_id"]))
            if not sheets:
                out.append(card_outcome(conv, "options", {
                    "question": "This campaign has no contact list yet.",
                    "options": [{"id": "import", "label": "Import a list"},
                                {"id": "cancel", "label": "Cancel"}]}, step="no_list"))
                return out
            opts = [{"id": str(x["id"]), "label": "%s (%s rows)" % (x["name"], x["n_rows"])} for x in sheets]
            payload = {"question": "Which contact list?", "options": opts[:4], "allow_other": False}
            if len(opts) > 4:
                payload["more"] = {"label": "More", "options": opts[4:]}
            out.append(card_outcome(conv, "options", payload, step="pick_audience"))
            return out
        if not s.get("range_done") and not s.get("serial_from"):
            out.append(card_outcome(conv, "options", {
                "question": "Send to which rows?",
                "options": [{"id": "all", "label": "All rows"},
                            {"id": "range", "label": "Choose a serial range"}]}, step="range"))
            return out
        if s.pop("_range_pending", None):
            raw = (s.get("_range_raw") or "").replace(" ", "")
            m = re.match(r"^(\d+)-(\d+)$", raw)
            if not m or int(m.group(1)) > int(m.group(2)):
                raise CardError("Enter the range like 10-50 (start before end).")
            s["serial_from"], s["serial_to"] = int(m.group(1)), int(m.group(2))
            s["range_done"] = True
        rows = services.audience_rows(int(s["sheet_id"]), s.get("serial_from"), s.get("serial_to"), None)
        st["data"]["audience"] = {"rows": rows[:12], "count": len(rows),
                                  "no_email": sum(1 for r in rows if not (r["hr_email"] or "").strip())}
        out.append({"kind": "data", "data": {"type": "table", "data": st["data"]["audience"]["rows"],
                                             "total": st["data"]["audience"]["count"],
                                             "note": "Audience preview - first 12 of %d" % len(rows)}})
        if not rows:
            out.append(card_outcome(conv, "options", {
                "question": "No one to send to in this selection.",
                "options": [{"id": "back_range", "label": "Change range"},
                            {"id": "import", "label": "Import a list"},
                            {"id": "cancel", "label": "Cancel"}]}, step="empty_audience"))
            return out
        if not s.get("template_id"):
            tpls = _templates()
            if not tpls:
                out.append(card_outcome(conv, "options", {
                    "question": "No templates yet - write one first.",
                    "options": [{"id": "write", "label": "Write a template"},
                                {"id": "cancel", "label": "Cancel"}]}, step="no_template"))
                return out
            pref = _campaign_template_pref(int(s["campaign_id"]))
            order = sorted(tpls, key=lambda t: (0 if t["id"] == pref else 1,
                                                0 if t["is_active"] else 1, t["id"]))
            opts = [{"id": str(t["id"]), "label": t["name"] + (" (active)" if t["is_active"] else "")}
                    for t in order]
            payload = {"question": "Which template?", "options": opts[:4], "allow_other": False}
            if len(opts) > 4:
                payload["more"] = {"label": "More", "options": opts[4:]}
            out.append(card_outcome(conv, "options", payload, step="pick_template"))
            return out
        if not s.get("mapping_done"):
            c = db.conn()
            tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (int(s["template_id"]),)).fetchone()
            headers_row = c.execute("SELECT headers FROM sheets WHERE id=?", (int(s["sheet_id"]),)).fetchone()
            c.close()
            tpl = dict(tpl)
            fields = json.loads(tpl.get("fields") or "[]")
            columns = json.loads(headers_row["headers"] or "[]") if headers_row else []
            if not fields:
                s["mapping"] = {}
                s["mapping_done"] = True
            else:
                mapping = {}
                for f in fields:
                    sug = docx_convert.suggest_mapping([f], columns).get(f, "")
                    if sug:
                        mapping[f] = "column:" + sug
                st["data"]["mapping_fields"] = fields
                st["data"]["mapping_columns"] = columns
                st["data"]["mapping"] = mapping
                out.append(card_outcome(conv, "table_edit", {
                    "question": "Match each field to a column (or type a fixed value).",
                    "fields": [{"field": f, "value": mapping.get(f, "")} for f in fields],
                    "columns": [""] + columns}, step="mapping"))
                return out
        if _mode(conv) == "compact" and not s.get("_group_done"):
            out.append(self._compact_card(conv))
            return out
        if s.get("_group_done"):
            out.extend(self._final_review(conv))
            return out
        if not s.get("cc_done"):
            last_cc = _remembered(conv, "last_cc_%s" % s["campaign_id"], "")
            out.append(card_outcome(conv, "text_input", {
                "label": "CC (comma separated, max 20 - merge fields allowed)",
                "placeholder": "e.g. team@campus.edu or {company_hr}", "validate": "emails",
                "default": last_cc or "", "hint": "Leave empty for no CC"}, step="cc"))
            return out
        if not s.get("signature_done"):
            has_sig = bool(db.get_setting("signature_html", "").strip())
            out.append(card_outcome(conv, "options", {
                "question": "Signature?",
                "options": [{"id": "1", "label": "Append signature"}, {"id": "0", "label": "No signature"}],
                **({} if has_sig else {"hint": "No signature is set yet (Settings -> Signature)."})},
                step="signature"))
            return out
        if not s.get("attachments_done"):
            import filestore
            lib = []
            c = db.conn()
            for b in c.execute("SELECT attachments FROM batches").fetchall():
                try:
                    lib += json.loads(b["attachments"] or "[]")
                except Exception:
                    pass
            c.close()
            lib = [n for n in dict.fromkeys(lib) if filestore.exists(n)]
            opts = [{"id": "none", "label": "No attachments"}]
            if lib:
                opts += [{"id": n, "label": n} for n in lib[:2]]
            opts.append({"id": "upload", "label": "Upload a file"})
            out.append(card_outcome(conv, "options", {"question": "Attachments?", "options": opts,
                                                      "allow_other": False}, step="attachments"))
            return out
        if not s.get("timing_done"):
            out.append(card_outcome(conv, "options", {
                "question": "What would you like to do with these emails?",
                "options": [{"id": "queue", "label": "Queue (nothing sent)"},
                            {"id": "draft", "label": "Gmail drafts (nothing sent)"},
                            {"id": "schedule", "label": "Schedule"},
                            {"id": "send", "label": "Send now"}], "allow_other": False}, step="timing"))
            return out
        if s.get("action") == "schedule" and not s.get("at"):
            now = NOW()
            out.append(card_outcome(conv, "datetime_input", {
                "label": "When to start?",
                "min_iso": (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
                "max_iso": (now + timedelta(days=45)).strftime("%Y-%m-%dT%H:%M"),
                "timezone_label": "local time"}, step="schedule_input"))
            return out
        if s.get("action") in ("schedule", "send") and s.get("stagger_done") is None:
            dflt = _remembered(conv, "last_stagger_min", db.get_setting("default_stagger_min") or 0)
            out.append(card_outcome(conv, "number_input", {
                "label": "Stagger between emails (minutes)", "min": 0, "max": 600, "step": 1,
                "default": dflt, "unit": "min",
                "hint": "Spreads sends out so Gmail doesn't flag you."}, step="stagger"))
            return out
        if s.get("reminders_done") is None:
            out.append(card_outcome(conv, "options", {
                "question": "Follow-up reminders?",
                "options": [{"id": "none", "label": "No reminders"},
                            {"id": "set", "label": "Set up reminders"}], "allow_other": False},
                step="reminders"))
            return out
        if s.get("reminders_set"):
            if not s.get("rem_days_done"):
                out.append(card_outcome(conv, "number_input", {
                    "label": "Every how many days?", "min": 1, "max": 90, "step": 1,
                    "default": _remembered(conv, "last_reminder_days", 7), "unit": "days"},
                    step="rem_days"))
                return out
            if not s.get("rem_rounds_done"):
                out.append(card_outcome(conv, "number_input", {
                    "label": "How many rounds (1-5)?", "min": 1, "max": 5, "step": 1,
                    "default": _remembered(conv, "last_reminder_rounds", 2), "unit": "rounds"},
                    step="rem_rounds"))
                return out
            if not s.get("rem_text_done"):
                if s.get("_budget_paused"):
                    st["data"]["reminder"] = {"subject": "Following up",
                                              "body_html": "<p>Hi {recruiter_name},<br>Just following up on my "
                                                           "earlier email - would love to hear if this is still of "
                                                           "interest.</p>"}
                    s["rem_text_done"] = True
                else:
                    try:
                        subj, body = _gen(conv, ai_client.write_reminder,
                                          "Reminder for the %s campaign" % (s.get("campaign_name") or "campaign"))
                    except usage.BudgetExhausted as e:
                        s["_budget_paused"] = True
                        st["data"]["reminder"] = {"subject": "Following up",
                                                  "body_html": "<p>Hi {recruiter_name},<br>Just following up on my "
                                                               "earlier email - would love to hear if this is still "
                                                               "of interest.</p>"}
                        s["rem_text_done"] = True
                        out.append(_budget_outcomes(conv, e)[0])
                        return out
                    st["data"]["reminder"] = {"subject": subj, "body_html": body}
                    out.append(card_outcome(conv, "draft_editor", {
                        "subject": subj, "body_html": body, "kind": "reminder",
                        "buttons": ["Continue", "Cancel"], "ai": False}, step="rem_text"))
                    return out
        if s.get("action") == "send" and db.get_setting("safe_mode") == "1":
            opts = [{"id": "schedule", "label": "Schedule instead"},
                    {"id": "draft", "label": "Save as Gmail drafts"},
                    {"id": "queue", "label": "Queue only"},
                    {"id": "cancel", "label": "Cancel"}]
            if actor(conv).get("role") == "admin":
                opts.insert(0, {"id": "live", "label": "Turn on live sending…", "style": "danger"})
            msg = "Safe mode is ON - immediate sending is blocked for everyone."
            if actor(conv).get("role") != "admin":
                msg += " Ask an admin to turn on live sending."
            out.append({"kind": "message", "text": msg})
            out.append(card_outcome(conv, "options", {"question": "How do you want to proceed?",
                                                     "options": opts, "allow_other": False}, step="safe_mode_choice"))
            return out
        if s.get("action") == "send" and not s.get("cap_ack"):
            rows = services.audience_rows(int(s.get("sheet_id") or 0), s.get("serial_from"), s.get("serial_to"), None)
            cap = int(db.get_setting("max_sends_per_day") or 100)
            used = services.sends_today(include_held=True)
            rem = max(0, cap - used)
            if len(rows) > rem:
                opts = []
                if rem > 0:
                    opts.append({"id": "partial", "label": "Send first %d now, queue the remaining %d" % (rem, len(rows) - rem)})
                opts.append({"id": "schedule", "label": "Schedule across days"})
                opts.append({"id": "cancel", "label": "Cancel"})
                out.append(card_outcome(conv, "options", {
                    "question": "Only %d of the %d emails fit under today's cap (%d used of %d). What now?"
                                % (rem, len(rows), used, cap),
                    "options": opts, "allow_other": False}, step="cap_choice"))
                return out
        s["_group_done"] = True
        if _mode(conv) == "compact" and not s.get("_compact_issued"):
            out.append(self._compact_card(conv))
            return out
        out.extend(self._final_review(conv))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        step = card["step_id"]
        r = response or {}
        if step == "safe_mode_choice":
            if r.get("id") in ("schedule", "draft", "queue"):
                s["action"] = r.get("id")
            elif r.get("id") == "live":
                return start_flow(conv, "settings", {"which": "safe_mode"})
            else:
                return cancel_flow(conv, "declined")
        if step == "cap_choice":
            if r.get("id") == "partial":
                s["cap_ack"] = True
            elif r.get("id") == "schedule":
                s["action"] = "schedule"
            else:
                return cancel_flow(conv, "declined")
        if step == "create_choice":
            if r.get("id") == "create":
                return start_flow(conv, "create_campaign", {"resume": "send_campaign"})
            return cancel_flow(conv, "declined")
        if step == "pick_campaign":
            if r.get("id"):
                s["campaign_id"] = int(r["id"])
            else:
                raise CardError("Pick a campaign from the list.")
        elif step == "no_list":
            if r.get("id") == "import":
                st["data"]["resume_after_import"] = "send_campaign"
                return start_flow(conv, "import_list", {})
            return cancel_flow(conv, "declined")
        elif step == "pick_audience":
            s["sheet_id"] = int(r.get("id") or 0)
        elif step == "range":
            if r.get("id") == "range":
                return [card_outcome(conv, "text_input", {
                    "label": "Serial range (e.g. 10-50)", "validate": "none",
                    "placeholder": "10-50"}, step="_range_input")]
            s["range_done"] = True
        elif step == "_range_input":
            s["_range_raw"] = (r.get("text") or "").strip()
            s["_range_pending"] = True
        elif step == "empty_audience":
            if r.get("id") == "back_range":
                s["serial_from"] = None
                s["range_done"] = None
            elif r.get("id") == "import":
                st["data"]["resume_after_import"] = "send_campaign"
                return start_flow(conv, "import_list", {})
            else:
                return cancel_flow(conv, "declined")
        elif step == "no_template":
            if r.get("id") == "write":
                st["data"]["resume_after_template"] = "send_campaign"
                return start_flow(conv, "write_template", {})
            return cancel_flow(conv, "declined")
        elif step == "pick_template":
            s["template_id"] = int(r.get("id") or 0)
        elif step == "mapping":
            mapping = {}
            cols = set(st["data"].get("mapping_columns") or [])
            for row in (r.get("fields") or []):
                f = row.get("field")
                v = (row.get("value") or "").strip()
                if not v:
                    raise CardError("Field %s is unmapped - choose a column or type a fixed value." % f)
                if v.startswith("literal:"):
                    mapping[f] = v
                elif v in cols:
                    mapping[f] = "column:" + v
                else:
                    mapping[f] = "literal:" + v
            s["mapping"] = mapping
            s["mapping_done"] = True
        elif step == "cc":
            raw = (r.get("text") or "").strip()
            if raw:
                try:
                    lst = services.parse_cc(raw)
                    if len(lst) > 20:
                        raise CardError("Up to 20 CC addresses.")
                except ServiceError as e:
                    raise CardError(e.message)
                s["cc"] = raw
                _remember(conv, "last_cc_%s" % s.get("campaign_id"), raw)
            else:
                s["cc"] = ""
            s["cc_done"] = True
        elif step == "signature":
            s["append_signature"] = r.get("id") == "1"
            _remember(conv, "last_signature", bool(s["append_signature"]))
            s["signature_done"] = True
        elif step == "attachments":
            if r.get("id") == "none":
                s["attachments"] = []
                s["attachments_done"] = True
            elif r.get("id") == "upload":
                return [card_outcome(conv, "file_request", {
                    "accept": [".pdf", ".png", ".jpg", ".docx", ".csv", ".xlsx"],
                    "max_mb": 5, "purpose": "attachment", "label": "Upload an attachment"},
                    step="_attach_upload")]
            else:
                s["attachments"] = [r["id"]]
                s["attachments_done"] = True
        elif step == "_attach_upload":
            f = r.get("file") or {}
            if not f.get("stored_name"):
                raise CardError("Attach a file first.")
            s["attachments"] = (s.get("attachments") or []) + [f["stored_name"]]
            s["attachments_done"] = True
        elif step == "final_review":
            if r.get("action") in ("edit", "back"):
                s["timing_done"] = False
                s["_group_done"] = False
                if _mode(conv) == "compact":
                    return [self._compact_card(conv)]
                return self.run(conv)
            return self.run(conv)
        elif step == "timing":
            s["action"] = r.get("id")
            if not s.get("account_id"):
                s["account_id"] = _account_default(conv)
            s["timing_done"] = True
        elif step == "schedule_input":
            at = (r.get("value") or "").replace("T", " ")
            try:
                services.parse_when(at)
            except ServiceError as e:
                raise CardError(e.message)
            s["at"] = at
        elif step == "stagger":
            try:
                n = int(r.get("value"))
            except (TypeError, ValueError):
                raise CardError("Enter a whole number of minutes.")
            if not (0 <= n <= 600):
                raise CardError("Stagger must be 0-600 minutes.")
            s["stagger"] = n
            _remember(conv, "last_stagger_min", n)
            s["stagger_done"] = True
        elif step == "reminders":
            s["reminders_set"] = r.get("id") == "set"
            s["reminders_done"] = True
        elif step == "rem_days":
            try:
                n = int(r.get("value"))
            except (TypeError, ValueError):
                raise CardError("Enter a number of days.")
            if not (1 <= n <= 90):
                raise CardError("Days must be 1-90.")
            s["rem_days"] = n
            _remember(conv, "last_reminder_days", n)
            s["rem_days_done"] = True
        elif step == "rem_rounds":
            try:
                n = int(r.get("value"))
            except (TypeError, ValueError):
                raise CardError("Enter 1-5.")
            if not (1 <= n <= 5):
                raise CardError("Rounds must be 1-5.")
            s["rem_rounds"] = n
            _remember(conv, "last_reminder_rounds", n)
            s["rem_rounds_done"] = True
        elif step == "rem_text":
            if r.get("action") == "cancel":
                s["reminders_set"] = False
            else:
                st["data"]["reminder"] = {"subject": r.get("subject") or st["data"]["reminder"]["subject"],
                                          "body_html": r.get("body_html") or st["data"]["reminder"]["body_html"]}
            s["rem_text_done"] = True
        elif step == "budget":
            s["_budget_paused"] = True
        elif step == "post_send":
            if r.get("id") == "reminders":
                return start_flow(conv, "reminders", {"batch_id": (st.get("last_res") or {}).get("batch_id")})
            if r.get("id") == "cancel":
                return start_flow(conv, "cancel_sends", {})
            return cancel_flow(conv, "done")
        return self.run(conv)

    def _final_review(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        params = self._gate_params(conv)
        q = _clean(params)
        q["_rows"] = params.get("_rows")
        pre = presend.run(q)
        samples = _render_samples(st, params)
        acc = int(s.get("account_id") or 0)
        safety = {"safe_mode_on": db.get_setting("safe_mode") == "1",
                  "sent_today": 0, "cap": 0, "remaining": 0, "held": services.held_count()}
        cap = pre.get("cap") or {}
        safety.update({"sent_today": cap.get("used", 0), "cap": cap.get("cap", 0),
                       "remaining": cap.get("remaining", 0)})
        for a in services.account_list(actor(conv)):
            if int(a["id"]) == acc:
                safety["account"] = {"email": a["email"], "token_ok": bool(db.account_token_json(a["id"]))}
        bulk = _bulk_state(pre.get("count", 0), int(db.get_setting("bulk_confirm_threshold") or 50),
                          s.get("action") or "send")
        labels = {"queue": "Queue %d emails (nothing is sent)" % pre.get("count", 0),
                  "draft": "Create %d Gmail drafts" % pre.get("count", 0),
                  "schedule": "Schedule %d emails for %s" % (pre.get("count", 0), s.get("at", "")),
                  "send": "Send %d emails now" % pre.get("count", 0)}
        payload = {
            "title": labels.get(s.get("action"), "Review send"),
            "risk": "R4", "action": "prepare_send", "count": pre.get("count", 0),
            "preview": {"from": safety.get("account"), "recipients": samples,
                        "total": pre.get("count", 0), "attachments": s.get("attachments") or [],
                        "cc": s.get("cc") or "",
                        "signature_html": services.signature_html() if s.get("append_signature") else ""},
            "facts": [{"k": "Action", "v": labels.get(s.get("action"), s.get("action"))},
                      {"k": "Stagger", "v": ("+ %s min" % s["stagger"]) if s.get("stagger") else "none"},
                      {"k": "Reminders", "v": ("%d day(s) x %d round(s)" % (s.get("rem_days") or 0, s.get("rem_rounds") or 1))
                       if s.get("reminders_set") else "none"}],
            "safety": safety,
            "presend": {"lines": pre["lines"], "blocked": pre["blocked"],
                        "passed": not pre["blocked"]},
            "bulk": bulk,
        }
        ap = approval.mint("prepare_send", _clean(params), conv["_conv_id"], _uid(conv),
                           render_hash=pre.get("render_hash"))
        st["gate"] = {"action": "prepare_send", "approval_id": ap["approval_id"],
                      "params": _clean(params), "count": pre.get("count", 0)}
        payload["approval_id"] = ap["approval_id"]
        payload["primary"] = {"label": labels.get(s.get("action"), "Continue")}
        return [card_outcome(conv, "final_review", payload, step="final_review", ttl=600)]

    def _gate_params(self, conv):
        st = conv["flow"]["st"]
        params = send_params(st)
        s = st["slots"]
        if s.get("reminders_set") and st["data"].get("reminder"):
            params["_reminders"] = {"days": s.get("rem_days"), "rounds": s.get("rem_rounds"),
                                    "subject": st["data"]["reminder"]["subject"],
                                    "body_html": st["data"]["reminder"]["body_html"]}
        return params

    def _compact_card(self, conv):
        st = conv["flow"]["st"]
        p = self._compact_payload(conv)
        params = self._gate_params(conv)
        ap = approval.mint("prepare_send", _clean(params), conv["_conv_id"], _uid(conv),
                           render_hash=p["render_hash"])
        st["gate"] = {"action": "prepare_send", "approval_id": ap["approval_id"],
                      "params": _clean(params), "count": p["count"]}
        p["approval_id"] = ap["approval_id"]
        p["version"] = st["version"]
        return card_outcome(conv, "compact_review", p, step="compact_review", ttl=600)

    def _compact_payload(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        if not s.get("account_id"):
            s["account_id"] = _account_default(conv)
        if s.get("append_signature") is None:
            s["append_signature"] = bool(_remembered(conv, "last_signature", False))
        if s.get("stagger") is None:
            s["stagger"] = int(_remembered(conv, "last_stagger_min", db.get_setting("default_stagger_min") or 0))
        params = self._gate_params(conv)
        q = _clean(params)
        q["_rows"] = params.get("_rows")
        pre = presend.run(q)
        bulk = _bulk_state(pre.get("count", 0), int(db.get_setting("bulk_confirm_threshold") or 50),
                          s.get("action") or "send")
        cap = pre.get("cap") or {}
        safety = {"safe_mode_on": db.get_setting("safe_mode") == "1",
                  "sent_today": cap.get("used", 0), "cap": cap.get("cap", 0),
                  "remaining": cap.get("remaining", 0), "held": services.held_count()}
        acc = int(s.get("account_id") or 0)
        for a in services.account_list(actor(conv)):
            if int(a["id"]) == acc:
                safety["account"] = {"email": a["email"], "token_ok": bool(db.account_token_json(a["id"]))}
        return {
            "title": "Review and send", "risk": "R4", "action": "prepare_send", "count": pre.get("count", 0),
            "email": {"account_id": acc,
                      "account_email": safety.get("account", {}).get("email") if safety.get("account") else None,
                      "campaign": params.get("campaign_name"),
                      "audience": "%s recipient(s)" % pre.get("count", 0),
                      "subject_preview": (pre.get("subject") or "")[:120],
                      "cc": s.get("cc") or "",
                      "signature_on": bool(s.get("append_signature")),
                      "signature_html": services.signature_html() if s.get("append_signature") else "",
                      "attachments": s.get("attachments") or []},
            "action_options": ["queue", "draft", "schedule", "send"],
            "action_selected": s.get("action") or None,
            "stagger_default": s["stagger"], "at": s.get("at") or "",
            "reminders": {"on": bool(s.get("reminders_set")),
                          "days": s.get("rem_days") or _remembered(conv, "last_reminder_days", 7),
                          "rounds": s.get("rem_rounds") or _remembered(conv, "last_reminder_rounds", 2),
                          "subject": (st["data"].get("reminder") or {}).get("subject", ""),
                          "body_html": (st["data"].get("reminder") or {}).get("body_html", "")},
            "preview": {"recipients": _render_samples(st, params), "total": pre.get("count", 0)},
            "safety": safety,
            "presend": {"lines": pre["lines"], "blocked": pre["blocked"],
                        "passed": not pre["blocked"]},
            "bulk": bulk, "render_hash": pre.get("render_hash", ""), "errors": [],
            "cap_choice": self._cap_choice_state(conv, pre),
        }

    def _cap_choice_state(self, conv, pre):
        st = conv["flow"]["st"]
        s = st["slots"]
        if s.get("action") != "send" or s.get("cap_ack"):
            return None
        cap = pre.get("cap") or {}
        rem = int(cap.get("remaining") or 0)
        n = int(pre.get("count") or 0)
        if n <= rem:
            return None
        return {"needed": True, "remaining": rem, "count": n,
                "options": (["partial"] if rem > 0 else []) + ["schedule"]}

    def post_gate(self, conv, res):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        extra = {}
        if s.get("action") == "send" and res.get("holding"):
            extra["note"] = "Held for the undo window - see the countdown card above."
        elif s.get("action") == "schedule":
            extra["note"] = "Sends from whichever instance is awake."
        elif s.get("action") == "draft":
            extra["note"] = "Nothing was sent - drafts are in Gmail."
        elif s.get("action") == "queue":
            extra["note"] = "Nothing was sent - the batch is queued."
        out.append({"kind": "result", "data": {
            "kind": "result", "title": self.title + " - done",
            "data": {"batch_id": res.get("batch_id"), "count": res.get("created"), **extra},
            "links": [{"label": "View queue", "page": "/queue"}]}})
        if s.get("reminders_set") and st["data"].get("reminder") and res.get("batch_id"):
            try:
                t = services.template_create("Reminder " + time.strftime("%Y-%m-%d %H:%M"),
                                             st["data"]["reminder"]["subject"],
                                             st["data"]["reminder"]["body_html"], "reminder", actor(conv))
                tid = t.get("id") or t.get("template_id")
                services.batch_reminders(int(res["batch_id"]), int(s.get("rem_days") or 7),
                                         int(s.get("rem_rounds") or 1), tid, actor(conv))
                out.append({"kind": "message", "text": "Reminders set: every %s day(s), max %s round(s)."
                                            % (s.get("rem_days"), s.get("rem_rounds"))})
            except ServiceError as e:
                out.append({"kind": "error", "message": "Reminders: " + e.message})
        out.append(card_outcome(conv, "options", {
            "question": "What next?",
            "options": [{"id": "reminders", "label": "Add reminders"},
                        {"id": "cancel", "label": "Cancel other sends"},
                        {"id": "done", "label": "Done"}],
            "allow_other": True}, step="post_send"))
        return out


# ================================================================ draft_email

@reg_flow
class DraftEmail(Flow):
    id = "draft_email"
    title = "Draft an email"

    def resume_after_subflow(self, conv):
        st = conv["flow"]["st"]
        if st.get("data", {}).pop("resume_after_template", None):
            st["slots"]["template_saved"] = st.get("_saved_template_id")
        return self.run(conv)

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("kind"):
            out.append(card_outcome(conv, "options", {
                "question": "What kind of email?",
                "options": [{"id": "one_off", "label": "One-off email"},
                            {"id": "followup", "label": "Follow-up reminder"},
                            {"id": "template", "label": "Outreach template for a campaign"}],
                "allow_other": True}, step="kind"))
            return out
        if s.get("kind") == "one_off" and not s.get("recipients"):
            out.append(card_outcome(conv, "text_input", {
                "label": "Recipient email addresses (comma separated)", "validate": "emails",
                "multiline": False, "max_len": 500, "placeholder": "name@company.com"},
                step="recipients"))
            return out
        if s.get("kind") == "followup" and not s.get("campaign_id"):
            cams = _campaigns()
            if not cams:
                out.append({"kind": "message", "text": "No campaigns found."})
                out.append(card_outcome(conv, "options", {"question": "What now?",
                                                          "options": [{"id": "done", "label": "Done"}]},
                                        step="post_draft"))
                return out
            opts = [{"id": str(c["id"]), "label": c["name"]} for c in cams[:4]]
            out.append(card_outcome(conv, "options", {"question": "Follow-up to which campaign?",
                                                      "options": opts, **({"more": {"label": "More", "options": opts[4:]}} if len(cams) > 4 else {})},
                                    step="pick_campaign"))
            return out
        if not s.get("brief_done"):
            out.append(card_outcome(conv, "text_input", {
                "label": "Tell me what to say (audience, the ask, anything personal)",
                "multiline": True, "validate": "none", "max_len": 2000,
                "hint": "Optional - I'll write a sensible default."}, step="brief"))
            return out
        if not s.get("generated"):
            try:
                if s.get("kind") == "template":
                    subj, body, name = _gen(conv, ai_client.write_template, s.get("brief") or "professional outreach", "professional")
                else:
                    subj, body = _gen(conv, ai_client.write_email, s.get("brief") or "", s["kind"])
                    name = (s.get("brief") or "Email").strip()[:40]
            except usage.BudgetExhausted as e:
                s["_budget_paused"] = True
                s["generated"] = True
                st["draft"] = {"subject": "", "body_html": "", "ai_paused": True}
                out.append(_budget_outcomes(conv, e)[0])
                out.append(card_outcome(conv, "draft_editor", self._editor_payload(conv), step="editor"))
                return out
            st["draft"] = {"subject": subj, "body_html": body, "name": name, "ai_paused": False}
            s["generated"] = True
        out.append(card_outcome(conv, "draft_editor", self._editor_payload(conv), step="editor"))
        return out

    def _editor_payload(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        d = st.get("draft") or {}
        acc = _account_default(conv)
        return {"subject": d.get("subject", ""), "body_html": d.get("body_html", ""),
                "kind": s.get("kind"), "ai": not s.get("_budget_paused"),
                "from": {"account_id": acc, "email": next((a["email"] for a in services.account_list(actor(conv))
                                                            if int(a["id"]) == acc), "")},
                "to": s.get("recipients") or [],
                "cc": s.get("cc") or "",
                "fields": d.get("fields") or [],
                "attachments": [], "signature_on": bool(_remembered(conv, "last_signature", False)),
                "buttons": ["Continue", "Cancel"]}

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        step = card["step_id"]
        r = response or {}
        if step == "kind":
            if r.get("id") in ("one_off", "followup", "template"):
                s["kind"] = r["id"]
            else:
                s["kind"] = "one_off"
                s["brief"] = (r.get("text") or "").strip()
                s["brief_done"] = True
        elif step == "recipients":
            raw = (r.get("text") or "").strip()
            try:
                lst = services.parse_cc(raw)
                if not lst:
                    raise ServiceError("Enter at least one valid email.")
            except ServiceError as e:
                raise CardError(e.message)
            s["recipients"] = lst
        elif step == "pick_campaign":
            s["campaign_id"] = int(r.get("id") or 0)
        elif step == "brief":
            s["brief"] = (r.get("text") or "").strip()
            s["brief_done"] = True
        elif step == "budget":
            s["_budget_paused"] = True
        elif step == "editor":
            if r.get("action") == "cancel":
                return cancel_flow(conv, "declined")
            if r.get("action") == "revise":
                try:
                    subj, body = _gen(conv, ai_client.polish,
                                      (r.get("body_html") or st["draft"]["body_html"]),
                                      r.get("instruction") or "")
                    st["draft"]["subject"] = subj or st["draft"]["subject"]
                    st["draft"]["body_html"] = body or st["draft"]["body_html"]
                except usage.BudgetExhausted as e:
                    s["_budget_paused"] = True
                    return _budget_outcomes(conv, e) + [card_outcome(conv, "draft_editor", self._editor_payload(conv), step="editor")]
                return [{"kind": "editor_refresh", "data": self._editor_payload(conv)}]
            if r.get("action") == "subjects":
                try:
                    subs = _gen(conv, ai_client.suggest_subjects,
                                (st["draft"].get("body_html") or "") + " " + (st["draft"].get("subject") or ""))
                except usage.BudgetExhausted as e:
                    return _budget_outcomes(conv, e)
                return [{"kind": "subjects", "data": {"subjects": subs}}]
            st["draft"]["subject"] = r.get("subject") if r.get("subject") is not None else st["draft"]["subject"]
            st["draft"]["body_html"] = r.get("body_html") if r.get("body_html") is not None else st["draft"]["body_html"]
            s["cc"] = r.get("cc") or s.get("cc") or ""
            if s.get("kind") == "template":
                out = [card_outcome(conv, "options", {
                    "question": "Save this as a template?",
                    "options": [{"id": "save", "label": "Save as template"},
                                {"id": "save_active", "label": "Save and make active"},
                                {"id": "discard", "label": "Discard"}],
                    "allow_other": False}, step="save_choice")]
                return out
            out = [card_outcome(conv, "options", {
                "question": "What would you like to do with this?",
                "options": [{"id": "send_now", "label": "Send"},
                            {"id": "schedule", "label": "Schedule"},
                            {"id": "queue", "label": "Queue"},
                            {"id": "drafts", "label": "Gmail drafts"}],
                "more": {"label": "More", "options": [
                    {"id": "save_template", "label": "Save as template"},
                    {"id": "test", "label": "Send test to me"},
                    {"id": "discard", "label": "Discard"}]},
                "allow_other": False}, step="what_next")]
            return out
        elif step == "save_choice":
            if r.get("id") in ("save", "save_active"):
                d = st["draft"]
                name = (d.get("name") or "Template").strip()
                out = [card_outcome(conv, "text_input", {"label": "Template name", "default": name,
                                                         "validate": "name", "max_len": 80}, step="tpl_name")]
                st["data"]["save_active"] = r["id"] == "save_active"
                return out
            return cancel_flow(conv, "declined")
        elif step == "tpl_name":
            s["tpl_name"] = (r.get("text") or "").strip() or st["draft"].get("name") or "Template"
            if st["data"].get("save_active"):
                return self._do_save_template(conv, make_active=True)
            return [card_outcome(conv, "options", {
                "question": "Make this the active template for new sends?",
                "options": [{"id": "active", "label": "Yes, make active"},
                            {"id": "no", "label": "No, just save"}]}, step="tpl_active")]
        elif step == "tpl_active":
            return self._do_save_template(conv, make_active=r.get("id") == "active")
        elif step == "what_next":
            i = r.get("id")
            d = st["draft"]
            if i in ("save_template",):
                return [card_outcome(conv, "text_input", {"label": "Template name",
                                                          "default": d.get("name") or "Template",
                                                          "validate": "name", "max_len": 80}, step="tpl_name")]
            if i == "test":
                return start_flow(conv, "test_email", {"subject": d.get("subject"),
                                                       "body_html": d.get("body_html")})
            if i == "discard":
                return cancel_flow(conv, "declined")
            if i in ("send_now", "schedule", "queue", "drafts"):
                action = {"send_now": "send", "schedule": "schedule", "queue": "queue", "drafts": "draft"}[i]
                # save as template (needed for the pipeline) then send via send_campaign? For one-off: adhoc.
                if s.get("kind") == "one_off":
                    slots = {"recipients": s.get("recipients"), "subject": d.get("subject"),
                             "body_html": d.get("body_html"), "action": action, "account_id": _account_default(conv)}
                    if i == "schedule":
                        out = [card_outcome(conv, "datetime_input", {
                            "label": "When to send?",
                            "min_iso": (NOW() + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
                            "max_iso": (NOW() + timedelta(days=45)).strftime("%Y-%m-%dT%H:%M")}, step="adhoc_at")]
                        st["data"]["adhoc_action"] = action
                        return out
                    out = self._adhoc_review(conv, action, d)
                    return out
                # followup/template -> save template, then send_campaign flow
                st["data"]["adhoc_action"] = action
                out = [card_outcome(conv, "text_input", {"label": "Template name",
                                                         "default": d.get("name") or "Template",
                                                         "validate": "name", "max_len": 80}, step="tpl_name")]
                return out
        elif step == "adhoc_review":
            return [card_outcome(conv, "draft_editor", self._editor_payload(conv), step="editor")]
        elif step == "adhoc_at":
            at = (r.get("value") or "").replace("T", " ")
            try:
                services.parse_when(at)
            except ServiceError as e:
                raise CardError(e.message)
            s["at"] = at
            d = st["draft"]
            return self._adhoc_review(conv, "schedule", d, at=at)
        elif step == "post_tpl":
            if r.get("id") == "send":
                return start_flow(conv, "send_campaign",
                                  {"campaign_id": s.get("campaign_id"),
                                   "template_id": st.get("_saved_template_id")})
            return cancel_flow(conv, "done")
        elif step == "post_draft":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def _do_save_template(self, conv, make_active):
        st = conv["flow"]["st"]
        s = st["slots"]
        d = st["draft"]
        params = {"name": s.get("tpl_name") or d.get("name") or "Template",
                  "subject": d.get("subject"), "body_html": d.get("body_html"),
                  "kind": "outreach", "active": bool(make_active)}
        pv = actions.get("create_template").preview(params, actor(conv))
        ap = approval.mint("create_template", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "create_template", "approval_id": ap["approval_id"], "params": params}
        return [card_outcome(conv, "confirm", {**pv, "approval_id": ap["approval_id"],
                                               "action": "create_template"}, step="tpl_confirm")]

    def _adhoc_review(self, conv, action, d, at=None):
        st = conv["flow"]["st"]
        s = st["slots"]
        acc = int(s.get("account_id") or _account_default(conv))
        recipients = s.get("recipients") or []
        n = len(recipients)
        params = {"campaign_id": int(s.get("campaign_id") or 0), "recipients": recipients,
                  "subject": d.get("subject"), "body_html": d.get("body_html"),
                  "account_id": acc, "action": action, "cc": s.get("cc") or "",
                  "append_signature": bool(_remembered(conv, "last_signature", False))}
        if at:
            params["at"] = at
        if action == "send":
            params["internal_hold_seconds"] = int(db.get_setting("send_undo_seconds") or 0)
        labels = {"send": "Send %d one-off email(s) now" % n, "schedule": "Schedule %d one-off email(s) for %s" % (n, at or ""),
                  "queue": "Queue %d one-off email(s) (nothing sent)" % n, "draft": "Create %d one-off Gmail draft(s)" % n}
        safety = {"safe_mode_on": db.get_setting("safe_mode") == "1",
                  "sent_today": services.sends_today(include_held=True),
                  "cap": int(db.get_setting("max_sends_per_day") or 100)}
        pv = actions.get("send_adhoc_email").preview(params, actor(conv))
        pv["preview"] = {"recipients": [{"serial": str(i + 1), "to": r, "subject": d.get("subject"),
                                         "body_html": d.get("body_html")} for i, r in enumerate(recipients)],
                         "total": n}
        pv["safety"] = safety
        ap = approval.mint("send_adhoc_email", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "send_adhoc_email", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "send_adhoc_email"
        pv["count"] = n
        pv["primary"] = {"label": labels.get(action)}
        pv["bulk"] = _bulk_state(n, int(db.get_setting("bulk_confirm_threshold") or 50), "send")
        return [card_outcome(conv, "final_review", pv, step="adhoc_review", ttl=600)]

    def post_gate(self, conv, res):
        st = conv["flow"]["st"]
        if st.get("last_gate_action") == "create_template":
            st["_saved_template_id"] = res.get("template_id")
            out = [{"kind": "result", "data": {"kind": "result", "title": "Template saved",
                                               "data": {"template_id": res.get("template_id"), "name": res.get("name")},
                                               "links": [{"label": "Open in Library", "page": "/library"}]}}]
            if st.get("data", {}).get("adhoc_action") in ("send", "schedule", "queue", "draft"):
                out.append(card_outcome(conv, "options", {
                    "question": "Send it to the campaign now?",
                    "options": [{"id": "send", "label": "Send to campaign"},
                                {"id": "done", "label": "Done"}]}, step="post_tpl"))
            return out
        note = "Held for the undo window." if res.get("holding") else ""
        out = [{"kind": "result", "data": {"kind": "result", "title": "One-off email - done",
                                           "data": {"batch_id": res.get("batch_id"),
                                                    "count": res.get("created"), "note": note}}}
              + ([{"kind": "card", "card": None}] if False else [])]
        out.append(card_outcome(conv, "options", {"question": "What next?",
                                                  "options": [{"id": "done", "label": "Done"}],
                                                  "allow_other": True}, step="post_draft"))
        return out



# ================================================================== reply (F-42)

@reg_flow
class Reply(Flow):
    id = "reply"
    title = "Handle a reply"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("email_id"):
            rows = _replies_unhandled(12)
            if not rows:
                out.append({"kind": "data", "data": {"type": "empty", "text": "No unhandled replies."}})
                out.append(card_outcome(conv, "options", {"question": "What now?",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(r["id"]), "label": "%s - %s" % (r["company"] or r["to_addr"], r["subject"][:40])}
                    for r in rows]
            out.append(card_outcome(conv, "options", {"question": "Which reply?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick"))
            return out
        e = _one_row("SELECT * FROM emails WHERE id=?", (int(s["email_id"]),))
        if not e:
            out.append(card_outcome(conv, "options", {"question": "Email not found.",
                                                      "options": [{"id": "done", "label": "Done"}]}, step="post"))
            return out
        snooze = _one_row("SELECT snoozed_until FROM email_snoozes WHERE email_id=?", (e["id"],))
        out.append({"kind": "data", "data": {"type": "thread",
                                             "data": {"id": e["id"], "to": e["to_addr"], "subject": e["subject"],
                                                      "sent_at": e["sent_at"], "replied_at": e["replied_at"],
                                                      "snoozed_until": snooze["snoozed_until"] if snooze else None,
                                                      "note": "Reply body is read live from Gmail when drafting."}}})
        out.append(card_outcome(conv, "options", {
            "question": "What do you want to do with this reply?",
            "options": [{"id": "draft", "label": "Draft a reply"},
                        {"id": "handled", "label": "Mark handled"},
                        {"id": "snooze", "label": "Snooze"},
                        {"id": "replied", "label": "Mark replied manually"}],
            "more": {"label": "More", "options": [{"id": "gmail", "label": "Open in Gmail"}]},
            "allow_other": False}, step="choose"))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        step = card["step_id"]
        r = response or {}
        if step == "pick":
            s["email_id"] = int(r.get("id") or 0)
        elif step == "choose":
            i = r.get("id")
            if i == "draft":
                try:
                    import gmail_client
                    reply_text = None
                    e = _one_row("SELECT * FROM emails WHERE id=?", (int(s["email_id"]),))
                    # (email_id, message_id, recipient) - check_replies only confirms
                    # whether a reply exists; the reply text is fetched at the draft step.
                    pairs = [(int(s["email_id"]), e["message_id"], e["to_addr"])] if e.get("message_id") else []
                    if pairs:
                        try:
                            res = gmail_client.check_replies(int(e["account_id"] or _account_default(conv)), pairs)
                            if isinstance(res, dict) and str(s["email_id"]) in res:
                                reply_text = None
                        except Exception:
                            reply_text = None
                    text = _gen(conv, ai_client.suggest_reply, e.get("body_html") or "", reply_text or "")
                except usage.BudgetExhausted as e2:
                    s["_budget_paused"] = True
                    text = "Hi,%20"
                    st["draft"] = {"subject": "Re: " + (e.get("subject") or ""), "body_html": "",
                                   "ai_paused": True, "email_id": s["email_id"],
                                   "to": e.get("to_addr"), "note": "AI budget exhausted - write the reply yourself."}
                    out = _budget_outcomes(conv, e2)
                    out.append(card_outcome(conv, "draft_editor",
                                            {"subject": st["draft"]["subject"], "body_html": "",
                                             "kind": "reply", "ai": False,
                                             "to_fixed": e.get("to_addr"),
                                             "buttons": ["Continue", "Cancel"]}, step="editor"))
                    return out
                st["draft"] = {"subject": ("Re: " if not (e.get("subject") or "").lower().startswith("re:") else "") + (e.get("subject") or ""),
                               "body_html": text, "email_id": s["email_id"], "to": e.get("to_addr"),
                               "ai_paused": False}
                out = [card_outcome(conv, "draft_editor",
                                    {"subject": st["draft"]["subject"], "body_html": text,
                                     "kind": "reply", "ai": not s.get("_budget_paused"),
                                     "to_fixed": e.get("to_addr"),
                                     "buttons": ["Continue", "Cancel"]}, step="editor")]
                return out
            if i == "handled":
                return self._gate(conv, "mark_reply_handled", {"email_id": int(s["email_id"])})
            if i == "replied":
                return self._gate(conv, "mark_replied_manual", {"email_id": int(s["email_id"])})
            if i == "snooze":
                return [card_outcome(conv, "options", {
                    "question": "Snooze until when?",
                    "options": [{"id": "1d", "label": "1 day"}, {"id": "3d", "label": "3 days"},
                                {"id": "1w", "label": "1 week"}, {"id": "custom", "label": "Custom"}]},
                    step="snooze_when")]
            if i == "gmail":
                out = [card_outcome(conv, "handoff", {
                    "title": "Open in Gmail",
                    "why": "Reading and sending replies in Gmail's own UI keeps full threading.",
                    "links": [{"label": "Open Gmail", "page": "https://mail.google.com"}],
                    "recheck": None}, step="handoff")]
                return out
        elif step == "snooze_when":
            i = r.get("id")
            if i == "custom":
                return [card_outcome(conv, "datetime_input", {
                    "label": "Snooze until",
                    "min_iso": (NOW() + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
                    "max_iso": (NOW() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M")}, step="snooze_custom")]
            days = {"1d": 1, "3d": 3, "1w": 7}.get(i, 1)
            until = (NOW() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
            return self._gate(conv, "snooze_reply", {"email_id": int(s["email_id"]), "until": until})
        elif step == "snooze_custom":
            until = (r.get("value") or "").replace("T", " ")
            try:
                services.parse_when(until)
            except ServiceError as e:
                raise CardError(e.message)
            return self._gate(conv, "snooze_reply", {"email_id": int(s["email_id"]), "until": until})
        elif step == "editor":
            if r.get("action") == "cancel":
                return cancel_flow(conv, "declined")
            st["draft"]["body_html"] = r.get("body_html") or st["draft"]["body_html"]
            out = [card_outcome(conv, "options", {
                "question": "Send the reply?",
                "options": [{"id": "send", "label": "Send reply"},
                            {"id": "draft", "label": "Save as Gmail draft"},
                            {"id": "cancel", "label": "Discard"}],
                "allow_other": False}, step="reply_action")]
            return out
        elif step == "reply_action":
            i = r.get("id")
            body = st["draft"]["body_html"]
            acc = int(_account_default(conv))
            if i == "send":
                if db.get_setting("safe_mode") == "1":
                    return [card_outcome(conv, "options", {
                        "question": "Safe mode is ON - replies go out as Gmail drafts.",
                        "options": [{"id": "draft", "label": "Save as Gmail draft"},
                                    {"id": "cancel", "label": "Cancel"}]}, step="reply_action2")]
                return self._gate(conv, "send_reply", {"email_id": int(s["email_id"]), "body": body, "account_id": acc})
            if i == "draft":
                return self._gate(conv, "create_reply_draft", {"email_id": int(s["email_id"]), "body": body, "account_id": acc})
            return cancel_flow(conv, "declined")
        elif step == "reply_action2":
            if r.get("id") == "draft":
                return self._gate(conv, "create_reply_draft",
                                  {"email_id": int(s["email_id"]), "body": st["draft"]["body_html"],
                                   "account_id": int(_account_default(conv))})
            return cancel_flow(conv, "declined")
        elif step == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def _gate(self, conv, action, params):
        a = actions.get(action)
        pv = a.preview(params, actor(conv))
        ap = approval.mint(action, params, conv["_conv_id"], _uid(conv))
        conv["flow"]["st"]["gate"] = {"action": action, "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = action
        return [card_outcome(conv, "confirm", pv, step="gate_" + action, ttl=600)]

    def post_gate(self, conv, res):
        st = conv["flow"]["st"]
        return [{"kind": "result", "data": {"kind": "result", "title": "Done", "data": res}},
                card_outcome(conv, "options", {"question": "What next?",
                                               "options": [{"id": "next", "label": "Next reply"},
                                                           {"id": "done", "label": "Done"}],
                                               "allow_other": False}, step="post")]



def _replies_unhandled(limit):
    c = db.conn()
    rows = c.execute("SELECT e.id, e.to_addr, e.subject, ct.company, e.replied_at FROM emails e "
                     "LEFT JOIN contacts ct ON ct.id=e.contact_id "
                     "WHERE e.status='sent' AND e.replied_at IS NOT NULL AND e.replied_at!='' "
                     "AND (e.handled_at IS NULL OR e.handled_at='') "
                     "AND e.id NOT IN (SELECT email_id FROM email_snoozes "
                     "WHERE snoozed_until > datetime('now')) "
                     "ORDER BY e.replied_at DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ================================================================== import_list (F-21/65)

@reg_flow
class ImportList(Flow):
    id = "import_list"
    title = "Import a contact list"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        f = s.get("file")
        if not f or f.get("kind") != "sheet":
            out.append(card_outcome(conv, "file_request", {
                "accept": [".csv", ".xlsx", ".tsv"], "max_mb": 5, "purpose": "contacts",
                "label": "Attach a contact list (.csv, .xlsx or .tsv)"}, step="file"))
            return out
        if not s.get("parsed"):
            st["data"]["sheet"] = f
            s["parsed"] = True
            headers = f.get("headers") or []
            rows = f.get("rows") or []
            email_col = next((h for h in headers if re.search(r"email|mail", h, re.I)), "")
            no_email = sum(1 for r in rows if not (r.get(email_col) or "").strip()) if email_col else len(rows)
            seen, dups = set(), 0
            for r in rows:
                em = (r.get(email_col) or "").strip().lower() if email_col else ""
                if em:
                    if em in seen:
                        dups += 1
                    seen.add(em)
            st["data"]["summary"] = {"filename": f.get("filename") or f.get("name"),
                                     "rows": len(rows), "headers": headers,
                                     "email_column": email_col, "no_email": no_email, "duplicates": dups,
                                     "preview": rows[:5]}
        sm = st["data"]["summary"]
        out.append({"kind": "data", "data": {"type": "table", "data": sm["preview"], "total": sm["rows"],
                                             "note": "First 5 of %s rows. Columns: %s. Email column: %s. Without email: %s. Duplicates: %s."
                                                     % (sm["rows"], ", ".join(sm["headers"]),
                                                        sm["email_column"] or "not detected",
                                                        sm["no_email"], sm["duplicates"])}})
        if not s.get("confirm_done"):
            out.append(card_outcome(conv, "options", {
                "question": "Import this list?",
                "options": [{"id": "import", "label": "Import list"},
                            {"id": "cancel", "label": "Cancel"}]}, step="confirm"))
            return out
        if not s.get("linked_done"):
            cams = _campaigns()
            if not cams:
                s["linked_done"] = True
                s["campaign_id"] = 0
            else:
                opts = [{"id": "0", "label": "Don't link"}] + \
                       [{"id": str(c["id"]), "label": c["name"]} for c in cams[:3]]
                out.append(card_outcome(conv, "options", {"question": "Link to a campaign?",
                                                          "options": opts,
                                                          **({"more": {"label": "More", "options": [
                                                              {"id": str(c["id"]), "label": c["name"]}
                                                              for c in cams[3:6]]}} if len(cams) > 3 else {})},
                                      step="link"))
                return out
        return self.run(conv)


    def _import_gate_card(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        sm = st["data"]["summary"]
        f = s["file"]
        name = (f.get("filename") or "").replace(".", " ").strip() or "Contact list"
        params = {"name": name, "headers": sm["headers"], "rows": f.get("rows") or [],
                  "campaign_id": int(s.get("campaign_id") or 0), "filename": sm["filename"]}
        pv = actions.get("import_contacts_sheet").preview(params, actor(conv))
        pv["details"] = [{"k": "File", "v": sm["filename"]}, {"k": "Rows", "v": sm["rows"]},
                         {"k": "Columns", "v": ", ".join(sm["headers"])},
                         {"k": "Without email", "v": sm["no_email"]}, {"k": "Duplicates", "v": sm["duplicates"]}]
        ap = approval.mint("import_contacts_sheet", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "import_contacts_sheet", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "import_contacts_sheet"
        return [card_outcome(conv, "confirm", pv, step="import_gate", ttl=600)]

    def post_gate(self, conv, res):
        st = conv["flow"]["st"]
        s = st["slots"]
        if res.get("sheet_id") is not None:
            st["_imported_sheet_id"] = res.get("sheet_id")
            st["_saved_template_id"] = None
            if s.get("campaign_id"):
                try:
                    services.sheet_set_campaign(int(res["sheet_id"]), int(s["campaign_id"]), actor(conv))
                except ServiceError:
                    pass
        out = [{"kind": "result", "data": {"kind": "result", "title": "List imported",
                                           "data": {"sheet_id": res.get("sheet_id"), "rows": res.get("rows"),
                                                    "no_email": res.get("no_email"),
                                                    "duplicates": res.get("duplicates")}}}
              ]
        out.append(card_outcome(conv, "options", {
            "question": "What next?",
            "options": [{"id": "send", "label": "Send emails"},
                        {"id": "template", "label": "Choose a template"},
                        {"id": "done", "label": "Done"}],
            "allow_other": False}, step="post_import"))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        step = card["step_id"]
        r = response or {}
        if step == "post_import":
            if r.get("id") == "send":
                slots = {"sheet_id": st.get("_imported_sheet_id")}
                cam = s.get("campaign_id")
                if cam:
                    slots["campaign_id"] = int(cam)
                return start_flow(conv, "send_campaign", slots)
            if r.get("id") == "template":
                return start_flow(conv, "write_template", {})
            return cancel_flow(conv, "done")
        if step == "file":
            parsed = r.get("parsed") or {}
            if not parsed or parsed.get("kind") != "sheet":
                raise CardError("That file is not a contact sheet (CSV/XLSX/TSV).")
            s["file"] = parsed
            s["parsed"] = False
        elif step == "confirm":
            if r.get("id") != "import":
                return cancel_flow(conv, "declined")
            s["confirm_done"] = True
            return self.run(conv)
        elif step == "link":
            s["campaign_id"] = int(r.get("id") or 0)
            return self._import_gate_card(conv)
        return self.run(conv)


# ============================================================ template flows

@reg_flow
class TemplateFromFile(Flow):
    id = "template_from_file"
    title = "Template from file"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        f = s.get("file")
        out = []
        if not f or f.get("kind") != "template":
            out.append(card_outcome(conv, "file_request", {
                "accept": [".docx", ".html", ".txt"], "max_mb": 5, "purpose": "template",
                "label": "Attach a template file (.docx, .html or .txt)"}, step="file"))
            return out
        if not s.get("parsed"):
            st["data"]["tpl"] = {"body_html": f.get("html") or "",
                                 "fields": docx_convert.detect_fields(f.get("html") or "")}
            s["parsed"] = True
        d = st["data"]["tpl"]
        out.append(card_outcome(conv, "draft_editor", {
            "subject": "", "body_html": d["body_html"], "kind": "template",
            "fields": d["fields"], "buttons": ["Continue", "Cancel"], "ai": False,
            "hint": "Fields detected: " + (", ".join(d["fields"]) or "none")}, step="preview"))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "file":
            parsed = r.get("parsed") or {}
            if not parsed or parsed.get("kind") != "template":
                raise CardError("That file is not a template (DOCX/HTML/TXT).")
            s["file"] = parsed
            s["parsed"] = False
        elif card["step_id"] == "preview":
            if r.get("action") == "cancel":
                return cancel_flow(conv, "declined")
            d = st["data"]["tpl"]
            d["subject"] = r.get("subject") or ""
            d["body_html"] = r.get("body_html") or d["body_html"]
            out = [card_outcome(conv, "text_input", {"label": "Template name",
                                                     "validate": "name", "max_len": 80,
                                                     "default": (s.get("file", {}).get("filename") or "").replace(".", " ").strip()},
                                step="name")]
            return out
        elif card["step_id"] == "name":
            s["tpl_name"] = (r.get("text") or "").strip() or "Template"
            out = [card_outcome(conv, "options", {
                "question": "Make this the active template for new sends?",
                "options": [{"id": "active", "label": "Yes, make active"},
                            {"id": "no", "label": "No, just save"}]}, step="active_choice")]
            return out
        elif card["step_id"] == "active_choice":
            make_active = r.get("id") == "active"
            d = st["data"]["tpl"]
            params = {"name": s.get("tpl_name") or "Template", "subject": d.get("subject"),
                      "body_html": d["body_html"], "kind": "outreach", "active": make_active}
            pv = actions.get("create_template").preview(params, actor(conv))
            ap = approval.mint("create_template", params, conv["_conv_id"], _uid(conv))
            st["gate"] = {"action": "create_template", "approval_id": ap["approval_id"], "params": params}
            pv["approval_id"] = ap["approval_id"]
            pv["action"] = "create_template"
            return [card_outcome(conv, "confirm", pv, step="save_gate", ttl=600)]
        elif card["step_id"] == "post":
            if r.get("id") == "send":
                return start_flow(conv, "send_campaign",
                                  {"template_id": st.get("_saved_template_id")})
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        st = conv["flow"]["st"]
        st["_saved_template_id"] = res.get("template_id")
        return [{"kind": "result", "data": {"kind": "result", "title": "Template saved",
                                            "data": {"template_id": res.get("template_id"), "name": res.get("name")},
                                            "links": [{"label": "Open in Library", "page": "/library"}]}},
                card_outcome(conv, "options", {"question": "What next?",
                                               "options": [{"id": "send", "label": "Send emails"},
                                                           {"id": "done", "label": "Done"}],
                                               "allow_other": False}, step="post")]



@reg_flow
class WriteTemplate(Flow):
    id = "write_template"
    title = "Write a template"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("brief_done"):
            out.append(card_outcome(conv, "text_input", {
                "label": "What should the template say? (audience, the ask, anything personal)",
                "multiline": True, "validate": "none", "max_len": 2000,
                "hint": "Optional - I'll write a sensible default."}, step="brief"))
            return out
        if not s.get("generated"):
            try:
                subj, body, name = _gen(conv, ai_client.write_template,
                                        s.get("brief") or "professional outreach email", "professional")
            except usage.BudgetExhausted as e:
                s["_budget_paused"] = True
                s["generated"] = True
                st["draft"] = {"subject": "", "body_html": "", "name": "Template", "ai_paused": True}
                out.append(_budget_outcomes(conv, e)[0])
            else:
                st["draft"] = {"subject": subj, "body_html": body, "name": name, "ai_paused": False}
            out.append(card_outcome(conv, "draft_editor", {
                "subject": st["draft"]["subject"], "body_html": st["draft"]["body_html"],
                "kind": "template", "ai": not s.get("_budget_paused"),
                "buttons": ["Continue", "Cancel"]}, step="editor"))
            return out
        out.append(card_outcome(conv, "draft_editor", {
            "subject": st["draft"]["subject"], "body_html": st["draft"]["body_html"],
            "kind": "template", "ai": not s.get("_budget_paused"),
            "buttons": ["Continue", "Cancel"]}, step="editor"))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "brief":
            s["brief"] = (r.get("text") or "").strip()
            s["brief_done"] = True
        elif card["step_id"] == "budget":
            s["_budget_paused"] = True
        elif card["step_id"] == "editor":
            if r.get("action") == "cancel":
                return cancel_flow(conv, "declined")
            if r.get("action") == "revise":
                try:
                    subj, body = _gen(conv, ai_client.polish, st["draft"].get("body_html") or "",
                                      r.get("instruction") or "")
                    st["draft"]["subject"] = subj or st["draft"]["subject"]
                    st["draft"]["body_html"] = body or st["draft"]["body_html"]
                except usage.BudgetExhausted as e:
                    s["_budget_paused"] = True
                    return _budget_outcomes(conv, e)
                return [{"kind": "editor_refresh", "data": {"subject": st["draft"]["subject"],
                                                            "body_html": st["draft"]["body_html"]}}]
            st["draft"]["subject"] = r.get("subject") if r.get("subject") is not None else st["draft"]["subject"]
            st["draft"]["body_html"] = r.get("body_html") if r.get("body_html") is not None else st["draft"]["body_html"]
            out = [card_outcome(conv, "text_input", {"label": "Template name",
                                                     "default": st["draft"].get("name") or "Template",
                                                     "validate": "name", "max_len": 80}, step="name")]
            return out
        elif card["step_id"] == "name":
            s["tpl_name"] = (r.get("text") or "").strip() or st["draft"].get("name") or "Template"
            out = [card_outcome(conv, "options", {
                "question": "Make this the active template for new sends?",
                "options": [{"id": "active", "label": "Yes, make active"},
                            {"id": "no", "label": "No, just save"}]}, step="active_choice")]
            return out
        elif card["step_id"] == "active_choice":
            d = st["draft"]
            params = {"name": s.get("tpl_name") or "Template", "subject": d.get("subject"),
                      "body_html": d.get("body_html"), "kind": "outreach",
                      "active": r.get("id") == "active"}
            pv = actions.get("create_template").preview(params, actor(conv))
            ap = approval.mint("create_template", params, conv["_conv_id"], _uid(conv))
            st["gate"] = {"action": "create_template", "approval_id": ap["approval_id"], "params": params}
            pv["approval_id"] = ap["approval_id"]
            pv["action"] = "create_template"
            return [card_outcome(conv, "confirm", pv, step="save_gate", ttl=600)]
        elif card["step_id"] == "post":
            if r.get("id") == "send":
                return start_flow(conv, "send_campaign",
                                  {"template_id": st.get("_saved_template_id")})
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        st = conv["flow"]["st"]
        st["_saved_template_id"] = res.get("template_id")
        return [{"kind": "result", "data": {"kind": "result", "title": "Template saved",
                                            "data": {"template_id": res.get("template_id"), "name": res.get("name")},
                                            "links": [{"label": "Open in Library", "page": "/library"}]}},
                card_outcome(conv, "options", {"question": "What next?",
                                               "options": [{"id": "send", "label": "Send emails"},
                                                           {"id": "done", "label": "Done"}],
                                               "allow_other": False}, step="post")]



@reg_flow
class EditTemplate(Flow):
    id = "edit_template"
    title = "Edit a template"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("template_id"):
            tpls = _templates()
            if not tpls:
                out.append({"kind": "data", "data": {"type": "empty", "text": "No templates to edit."}})
                out.append(card_outcome(conv, "options", {"question": "What now?",
                                                          "options": [{"id": "write", "label": "Write a new one"},
                                                                      {"id": "done", "label": "Done"}]}, step="choice0"))
                return out
            opts = [{"id": str(t["id"]), "label": t["name"] + (" (active)" if t["is_active"] else "")} for t in tpls]
            out.append(card_outcome(conv, "options", {"question": "Which template?",
                                                      "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick"))
            return out
        t = _one_row("SELECT * FROM email_templates WHERE id=?", (int(s["template_id"]),))
        if not t:
            out.append(card_outcome(conv, "options", {"question": "Template not found.",
                                                      "options": [{"id": "done", "label": "Done"}]}, step="choice0"))
            return out
        out.append(card_outcome(conv, "draft_editor", {
            "subject": t["subject"], "body_html": t["body_html"], "kind": "template",
            "fields": json.loads(t.get("fields") or "[]"), "buttons": ["Continue", "Cancel"], "ai": True},
            step="editor"))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick":
            s["template_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "choice0":
            if r.get("id") == "write":
                return start_flow(conv, "write_template", {})
            return cancel_flow(conv, "done")
        elif card["step_id"] == "editor":
            if r.get("action") == "cancel":
                return cancel_flow(conv, "declined")
            if r.get("action") == "revise":
                try:
                    subj, body = _gen(conv, ai_client.polish, r.get("body_html") or "",
                                      r.get("instruction") or "")
                    st["draft"] = {"subject": subj, "body_html": body}
                except usage.BudgetExhausted as e:
                    return _budget_outcomes(conv, e)
                return [{"kind": "editor_refresh", "data": st["draft"]}]
            st["draft"] = {"subject": r.get("subject") or "", "body_html": r.get("body_html") or ""}
            t = _one_row("SELECT * FROM email_templates WHERE id=?", (int(s["template_id"]),))
            params = {"template_id": int(s["template_id"]), "subject": st["draft"]["subject"],
                      "body_html": st["draft"]["body_html"]}
            pv = actions.get("update_template").preview(params, actor(conv))
            pv["title"] = "Update template '%s'" % (t["name"] if t else "")
            ap = approval.mint("update_template", params, conv["_conv_id"], _uid(conv))
            st["gate"] = {"action": "update_template", "approval_id": ap["approval_id"], "params": params}
            pv["approval_id"] = ap["approval_id"]
            pv["action"] = "update_template"
            return [card_outcome(conv, "confirm", pv, step="save_gate", ttl=600)]
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Template updated", "data": res,
                                            "links": [{"label": "Open in Library", "page": "/library"}]}}]


# ============================================================ campaign flows

@reg_flow
class CreateCampaign(Flow):
    id = "create_campaign"
    title = "Create a campaign"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("name"):
            out.append(card_outcome(conv, "text_input", {
                "label": "Campaign name", "validate": "name", "max_len": 80,
                "placeholder": "e.g. Winter Internship 2027"}, step="name"))
            return out
        if s.get("desc_done") is None:
            out.append(card_outcome(conv, "options", {
                "question": "Add a description?",
                "options": [{"id": "skip", "label": "Skip"}, {"id": "add", "label": "Add description"}]},
                step="desc_choice"))
            return out
        if s.get("description_pending"):
            out.append(card_outcome(conv, "text_input", {"label": "Description", "multiline": True,
                                                         "validate": "none", "max_len": 300}, step="desc"))
            return out
        params = {"name": s["name"], "description": s.get("description") or ""}
        pv = actions.get("create_campaign").preview(params, actor(conv))
        ap = approval.mint("create_campaign", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "create_campaign", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "create_campaign"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "name":
            s["name"] = (r.get("text") or "").strip()
            if not s["name"]:
                raise CardError("Give the campaign a name.")
        elif card["step_id"] == "desc_choice":
            if r.get("id") == "add":
                s["description_pending"] = True
            else:
                s["desc_done"] = True
        elif card["step_id"] == "desc":
            s["description"] = (r.get("text") or "").strip()
            s["description_pending"] = False
            s["desc_done"] = True
        elif card["step_id"] == "post":
            if r.get("id") == "import":
                return start_flow(conv, "import_list", {})
            if r.get("id") == "template":
                return start_flow(conv, "write_template", {})
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        st = conv["flow"]["st"]
        return [{"kind": "result", "data": {"kind": "result", "title": "Campaign created",
                                            "data": {"campaign_id": res.get("campaign_id"),
                                                      "note": "Empty campaign - no list, no template yet."}}},
                card_outcome(conv, "options", {
                    "question": "What next?",
                    "options": [{"id": "import", "label": "Import a contact list"},
                                {"id": "template", "label": "Choose a template"},
                                {"id": "done", "label": "Done"}], "allow_other": False}, step="post")]



@reg_flow
class EditCampaign(Flow):
    id = "edit_campaign"
    title = "Edit a campaign"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        # slots arrive from the model: validate the numeric ones (never trust)
        try:
            s["campaign_id"] = int(s["campaign_id"]) if s.get("campaign_id") else None
        except (TypeError, ValueError):
            s["campaign_id"] = None
        out = []
        if not s.get("campaign_id"):
            cams = _campaigns()
            if not cams:
                out.append(card_outcome(conv, "options", {"question": "No campaigns to edit.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(c["id"]), "label": c["name"]} for c in cams]
            out.append(card_outcome(conv, "options", {"question": "Which campaign?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick"))
            return out
        if not s.get("field"):
            out.append(card_outcome(conv, "options", {
                "question": "What do you want to change?",
                "options": [{"id": "name", "label": "Name"}, {"id": "description", "label": "Description"}]},
                step="field"))
            return out
        if not s.get("value"):
            cur = _one_row("SELECT name, description FROM campaigns WHERE id=?", (int(s["campaign_id"]),))
            key = "name" if s["field"] == "name" else "description"
            out.append(card_outcome(conv, "text_input", {
                "label": "New " + s["field"], "default": (cur.get(key) or ""),
                "validate": "name" if key == "name" else "none",
                "multiline": key == "description", "max_len": 300}, step="value"))
            return out
        params = {"campaign_id": int(s["campaign_id"]), s["field"]: s["value"]}
        pv = actions.get("update_campaign").preview(params, actor(conv))
        ap = approval.mint("update_campaign", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "update_campaign", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "update_campaign"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick":
            s["campaign_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "field":
            s["field"] = r.get("id")
        elif card["step_id"] == "value":
            s["value"] = (r.get("text") or "").strip()
            if not s["value"] and s["field"] == "name":
                raise CardError("The name cannot be empty.")
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Campaign updated", "data": res}},
                card_outcome(conv, "options", {"question": "Done?",
                                               "options": [{"id": "done", "label": "Done"}]}, step="post")]


@reg_flow
class DeleteCampaign(Flow):
    id = "delete_campaign"
    title = "Delete a campaign"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        # slots arrive from the model: validate the numeric ones (never trust)
        try:
            s["campaign_id"] = int(s["campaign_id"]) if s.get("campaign_id") else None
        except (TypeError, ValueError):
            s["campaign_id"] = None
        out = []
        if not s.get("campaign_id"):
            cams = _campaigns()
            if not cams:
                out.append(card_outcome(conv, "options", {"question": "No campaigns to delete.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(c["id"]), "label": c["name"]} for c in cams]
            out.append(card_outcome(conv, "options", {"question": "Which campaign?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick"))
            return out
        try:
            params = {"campaign_id": int(s["campaign_id"])}
            pv = actions.get("delete_campaign").preview(params, actor(conv))
        except ServiceError as e:
            out.append(card_outcome(conv, "options", {"question": e.message,
                                                      "options": [{"id": "done", "label": "Done"}]}, step="post"))
            return out
        ap = approval.mint("delete_campaign", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "delete_campaign", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "delete_campaign"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick":
            s["campaign_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Campaign deleted",
                                            "data": {"note": "Gmail copies were not touched."}}},
                card_outcome(conv, "options", {"question": "Done?",
                                               "options": [{"id": "done", "label": "Done"}]}, step="post")]


@reg_flow
class DeleteTemplate(Flow):
    id = "delete_template"
    title = "Delete a template"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("template_id"):
            tpls = _templates()
            if not tpls:
                out.append(card_outcome(conv, "options", {"question": "No templates to delete.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(t["id"]), "label": t["name"] + (" (active)" if t["is_active"] else "")}
                    for t in tpls]
            out.append(card_outcome(conv, "options", {"question": "Which template?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick"))
            return out
        try:
            params = {"template_id": int(s["template_id"])}
            pv = actions.get("delete_template").preview(params, actor(conv))
        except ServiceError as e:
            out.append(card_outcome(conv, "options", {"question": e.message,
                                                      "options": [{"id": "done", "label": "Done"}]}, step="post"))
            return out
        ap = approval.mint("delete_template", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "delete_template", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "delete_template"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick":
            s["template_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Template deleted",
                                            "data": {"note": "Gmail drafts and sent emails are not touched."}}}
]


@reg_flow
class DeleteSheet(Flow):
    id = "delete_sheet"
    title = "Delete a contact list"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("sheet_id"):
            c = db.conn()
            sheets = [dict(r) for r in c.execute(
                "SELECT s.id, s.name, s.n_rows, COALESCE(c.name,'') campaign FROM sheets s "
                "LEFT JOIN campaigns c ON c.id=s.campaign_id ORDER BY s.id DESC").fetchall()]
            c.close()
            if not sheets:
                out.append(card_outcome(conv, "options", {"question": "No lists to delete.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(x["id"]),
                     "label": "%s (%s rows)%s" % (x["name"], x["n_rows"],
                                                   " - " + x["campaign"] if x["campaign"] else "")}
                    for x in sheets]
            out.append(card_outcome(conv, "options", {"question": "Which list?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick"))
            return out
        try:
            params = {"sheet_id": int(s["sheet_id"])}
            pv = actions.get("delete_sheet").preview(params, actor(conv))
            if pv.get("blocked"):
                out.append(card_outcome(conv, "options", {"question": pv.get("reason") or "This list is in use.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
        except ServiceError as e:
            out.append(card_outcome(conv, "options", {"question": e.message,
                                                      "options": [{"id": "done", "label": "Done"}]}, step="post"))
            return out
        ap = approval.mint("delete_sheet", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "delete_sheet", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "delete_sheet"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick":
            s["sheet_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Contact list deleted",
                                            "data": {"note": "Gmail copies were not touched."}}}]


# ================================================================ cancel_sends

@reg_flow
class CancelSends(Flow):
    id = "cancel_sends"
    title = "Cancel sends"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("scope"):
            out.append(card_outcome(conv, "options", {
                "question": "What do you want to cancel?",
                "options": [{"id": "campaign", "label": "One campaign"},
                            {"id": "batch", "label": "A specific batch"},
                            {"id": "all", "label": "Everything unsent"}],
                "allow_other": False}, step="scope"))
            return out
        if s["scope"] == "campaign" and not s.get("campaign_id"):
            cams = _campaigns()
            opts = [{"id": str(c["id"]), "label": c["name"]} for c in cams]
            out.append(card_outcome(conv, "options", {"question": "Which campaign?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick_campaign"))
            return out
        if s["scope"] == "batch" and not s.get("batch_id"):
            rows = _q_rows("SELECT b.id, b.name, (SELECT COUNT(*) FROM emails e WHERE e.batch_id=b.id AND e.status IN ('pending','scheduled','drafted')) n "
                           "FROM batches b WHERE n > 0 ORDER BY b.id DESC LIMIT 8")
            if not rows:
                out.append(card_outcome(conv, "options", {"question": "No batches with unsent emails.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(r["id"]), "label": "%s (%s unsent)" % (r["name"], r["n"])} for r in rows]
            out.append(card_outcome(conv, "options", {"question": "Which batch?", "options": opts}, step="pick_batch"))
            return out
        # show what would be cancelled
        where, args = "e.status IN ('pending','scheduled','drafted')", []
        if s.get("campaign_id"):
            where += " AND b.campaign_id=?"
            args.append(int(s["campaign_id"]))
        if s.get("batch_id"):
            where += " AND e.batch_id=?"
            args.append(int(s["batch_id"]))
        counts = _q_rows("SELECT e.status, COUNT(*) n FROM emails e LEFT JOIN batches b ON e.batch_id=b.id "
                         "WHERE " + where + " GROUP BY e.status", args)
        n = sum(r["n"] for r in counts)
        out.append({"kind": "data", "data": {"type": "table", "data": counts, "total": n,
                                             "note": "Already-sent emails are not affected."}})
        if n == 0:
            out.append(card_outcome(conv, "options", {"question": "Nothing to cancel here.",
                                                      "options": [{"id": "done", "label": "Done"}]}, step="post"))
            return out
        params = {}
        if s.get("campaign_id"):
            params["campaign_id"] = int(s["campaign_id"])
        if s.get("batch_id"):
            params["batch_id"] = int(s["batch_id"])
        pv = actions.get("cancel_emails").preview(params, actor(conv))
        ap = approval.mint("cancel_emails", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "cancel_emails", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "cancel_emails"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "scope":
            s["scope"] = r.get("id")
        elif card["step_id"] == "pick_campaign":
            s["campaign_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "pick_batch":
            s["batch_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Sends cancelled",
                                            "data": {"cancelled": res.get("cancelled", 0),
                                                      "note": "Already-sent emails were not affected."}}},
                card_outcome(conv, "options", {"question": "What next?",
                                               "options": [{"id": "new", "label": "Start something else"},
                                                           {"id": "done", "label": "Done"}],
                                               "allow_other": True}, step="post")]


def _q_rows(sql, args=()):
    c = db.conn()
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    return rows


# ================================================================ bounces (F-43)

@reg_flow
class BouncesFix(Flow):
    id = "bounces_fix"
    title = "Fix bounces"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("listed"):
            rows = _q_rows("SELECT e.id, e.to_addr, e.serial, ct.company, ct.hr_name, ct.id AS contact_id, "
                           "COALESCE(NULLIF(e.bounce_reason,''),'Reason unavailable') AS reason "
                           "FROM emails e LEFT JOIN contacts ct ON ct.id=e.contact_id "
                           "WHERE e.status='sent' AND e.bounced_at IS NOT NULL AND e.bounced_at!='' "
                           "ORDER BY e.bounced_at DESC LIMIT 50")
            st["data"]["bounces"] = rows
            s["listed"] = True
            if not rows:
                out.append({"kind": "data", "data": {"type": "empty", "text": "No bounced emails."}})
                out.append(card_outcome(conv, "options", {"question": "What now?",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            out.append({"kind": "data", "data": {"type": "table", "data": rows,
                                                 "total": len(rows)}})
            out.append(card_outcome(conv, "options", {
                "question": "What would you like to do?",
                "options": [{"id": "fix", "label": "Fix addresses now"},
                            {"id": "export", "label": "Export the list"},
                            {"id": "ignore", "label": "Ignore"}],
                "allow_other": False}, step="choose"))
            return out
        if not s.get("fixed"):
            rows = st["data"]["bounces"]
            out.append(card_outcome(conv, "table_edit", {
                "question": "Correct the bounced addresses (leave unchanged rows as-is).",
                "fields": [{"field": r["to_addr"] + " (%s)" % (r["company"] or r["serial"]),
                            "value": r["to_addr"], "contact_id": r["contact_id"]} for r in rows],
                "columns": []}, step="fix_table"))
            return out
        updates = st["data"].get("updates") or {}
        if not updates:
            out.append(card_outcome(conv, "options", {
                "question": "No addresses were changed.",
                "options": [{"id": "retable", "label": "Try again"},
                            {"id": "done", "label": "Done"}]}, step="nochange"))
            return out
        params = {"updates": {str(cid): {"hr_email": em} for cid, em in updates.items()}}
        pv = actions.get("update_contact").preview(params, actor(conv))
        ap = approval.mint("update_contact", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "update_contact", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "update_contact"
        pv["title"] = "Update %d contact(s)" % len(updates)
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "choose":
            if r.get("id") == "fix":
                s["fixed"] = True
            elif r.get("id") == "export":
                return [card_outcome(conv, "handoff", {
                    "title": "Export contacts", "why": "Exports the full contacts CSV.",
                    "links": [{"label": "Download CSV", "page": "/api/export/contacts.csv"}]}, step="post")]
            else:
                return cancel_flow(conv, "done")
        elif card["step_id"] == "fix_table":
            updates = {}
            for row in (r.get("fields") or []):
                cid = row.get("contact_id")
                v = (row.get("value") or "").strip()
                old = (row.get("field") or "").split(" (")[0]
                if cid and v and v != old:
                    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
                        raise CardError("'%s' is not a valid email address." % v)
                    updates[cid] = v
            st["data"]["updates"] = updates
        elif card["step_id"] == "nochange":
            if r.get("id") == "retable":
                s["fixed"] = True
            else:
                return cancel_flow(conv, "done")
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        elif card["step_id"] == "resend_choice":
            if r.get("id") == "resend":
                return start_flow(conv, "send_campaign",
                                  {"contact_ids": st.get("just_fixed_ids")})
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        st = conv["flow"]["st"]
        n = len((st["data"].get("updates") or {}))
        st["just_fixed_ids"] = list((st["data"].get("updates") or {}).keys())
        return [{"kind": "result", "data": {"kind": "result", "title": "Contacts updated",
                                            "data": {"updated": res.get("updated", n),
                                                      "note": "Marked ready to re-send."}}},
                card_outcome(conv, "options", {
                    "question": "Re-send to the fixed contacts?",
                    "options": [{"id": "resend", "label": "Re-send to the %d fixed" % n},
                                {"id": "done", "label": "Done"}],
                    "allow_other": False}, step="resend_choice")]



# ================================================================ reminders (F-30)

@reg_flow
class Reminders(Flow):
    id = "reminders"
    title = "Set reminders"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("batch_id"):
            rows = _q_rows("SELECT b.id, b.name, (SELECT COUNT(*) FROM emails e WHERE e.batch_id=b.id AND e.status='sent') sent "
                           "FROM batches b WHERE sent > 0 ORDER BY b.id DESC LIMIT 8")
            if not rows:
                out.append(card_outcome(conv, "options", {"question": "No batches with sent emails yet.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(r["id"]), "label": "%s (%s sent)" % (r["name"], r["sent"])} for r in rows]
            out.append(card_outcome(conv, "options", {"question": "Which batch?", "options": opts}, step="pick"))
            return out
        if not s.get("days"):
            out.append(card_outcome(conv, "number_input", {
                "label": "Remind every how many days?", "min": 1, "max": 90, "step": 1,
                "default": _remembered(conv, "last_reminder_days", 7), "unit": "days"}, step="days"))
            return out
        if not s.get("rounds"):
            out.append(card_outcome(conv, "number_input", {
                "label": "How many rounds (1-5)?", "min": 1, "max": 5, "step": 1,
                "default": _remembered(conv, "last_reminder_rounds", 2), "unit": "rounds"}, step="rounds"))
            return out
        if not s.get("text_done"):
            if s.get("_budget_paused"):
                st["data"]["reminder"] = {"subject": "Following up",
                                          "body_html": "<p>Hi {recruiter_name},<br>Just following up on my "
                                                       "earlier email - would love to hear if this is still of "
                                                       "interest.</p>"}
                s["text_done"] = True
            else:
                try:
                    subj, body = _gen(conv, ai_client.write_reminder, "Reminder for batch " + str(s["batch_id"]))
                except usage.BudgetExhausted as e:
                    s["_budget_paused"] = True
                    st["data"]["reminder"] = {"subject": "Following up",
                                              "body_html": "<p>Hi {recruiter_name},<br>Just following up on my "
                                                           "earlier email.</p>"}
                    s["text_done"] = True
                    out.append(_budget_outcomes(conv, e)[0])
                else:
                    st["data"]["reminder"] = {"subject": subj, "body_html": body}
                out.append(card_outcome(conv, "draft_editor", {
                    "subject": st["data"]["reminder"]["subject"],
                    "body_html": st["data"]["reminder"]["body_html"],
                    "kind": "reminder", "buttons": ["Continue", "Cancel"], "ai": not s.get("_budget_paused")},
                    step="text"))
                return out
        d = st["data"]["reminder"]
        params = {"batch_id": int(s["batch_id"]), "days": int(s["days"]), "rounds": int(s["rounds"]),
                  "subject": d["subject"], "body_html": d["body_html"]}
        pv = actions.get("set_reminders").preview(params, actor(conv))
        ap = approval.mint("set_reminders", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "set_reminders", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "set_reminders"
        pv["details"].append({"k": "Who qualifies", "v": "Non-repliers, evaluated at each round"})
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick":
            s["batch_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "days":
            try:
                n = int(r.get("value"))
            except (TypeError, ValueError):
                raise CardError("Enter a number of days.")
            if not (1 <= n <= 90):
                raise CardError("Days must be 1-90.")
            s["days"] = n
            _remember(conv, "last_reminder_days", n)
        elif card["step_id"] == "rounds":
            try:
                n = int(r.get("value"))
            except (TypeError, ValueError):
                raise CardError("Enter 1-5.")
            if not (1 <= n <= 5):
                raise CardError("Rounds must be 1-5.")
            s["rounds"] = n
            _remember(conv, "last_reminder_rounds", n)
        elif card["step_id"] == "budget":
            s["_budget_paused"] = True
        elif card["step_id"] == "text":
            if r.get("action") == "cancel":
                return cancel_flow(conv, "declined")
            st["data"]["reminder"] = {"subject": r.get("subject") or st["data"]["reminder"]["subject"],
                                      "body_html": r.get("body_html") or st["data"]["reminder"]["body_html"]}
            s["text_done"] = True
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Reminders set", "data": res}}]


# ================================================================ test_email (F-68)

@reg_flow
class TestEmail(Flow):
    id = "test_email"
    title = "Send a test email"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        acc = _account_default(conv)
        if not s.get("to"):
            own = []
            for a in services.account_list(actor(conv)):
                if db.account_token_json(a["id"]):
                    own.append(a["email"])
            me = actor(conv).get("email")
            opts = [{"id": e, "label": e} for e in dict.fromkeys(own + ([me] if me else []))]
            out.append(card_outcome(conv, "options", {
                "question": "Who should receive the test?",
                "options": opts[:4], "allow_other": True, "other_type": "text",
                **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})}, step="to"))
            return out
        if not s.get("what"):
            out.append(card_outcome(conv, "options", {
                "question": "What should the test render?",
                "options": [{"id": "draft", "label": "The draft you were editing"},
                            {"id": "template", "label": "A template"},
                            {"id": "first", "label": "A campaign's first recipient"}],
                "allow_other": False}, step="what"))
            return out
        if s["what"] == "template" and not s.get("template_id"):
            tpls = _templates()
            if not tpls:
                out.append(card_outcome(conv, "options", {"question": "No templates exist.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(t["id"]), "label": t["name"]} for t in tpls]
            out.append(card_outcome(conv, "options", {"question": "Which template?",
                                                      "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick_tpl"))
            return out
        # build the render
        subject, body, tid, content_hash = "", "", None, None
        if s["what"] == "draft":
            subject, body = s.get("subject") or "Test", s.get("body_html") or "<p>Test</p>"
        elif s["what"] == "template":
            t = _one_row("SELECT * FROM email_templates WHERE id=?", (int(s["template_id"]),))
            if not t:
                raise CardError("Template not found.")
            subject, body = t["subject"], t["body_html"]
            tid = t["id"]
            content_hash = services.template_content_hash(t)
        else:
            c = db.conn()
            row = c.execute("SELECT * FROM contacts WHERE hr_email != '' AND hr_email IS NOT NULL LIMIT 1").fetchone()
            c.close()
            if not row:
                out.append(card_outcome(conv, "options", {
                    "question": "No contacts to preview.", "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            tpls = _templates()
            if not tpls:
                out.append(card_outcome(conv, "options", {"question": "No templates exist.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            t = _one_row("SELECT * FROM email_templates WHERE id=?", (int(tpls[0]["id"]),))
            subject, body = services.render_email(dict(t), services.row_vals(dict(row)), {})
            tid, content_hash = t["id"], services.template_content_hash(dict(t))
        if services.signature_html():
            body = services.append_signature(body)
        params = {"account_id": acc, "to": s["to"], "subject": subject, "body_html": body,
                  "template_id": tid, "content_hash": content_hash}
        pv = actions.get("send_test_email").preview(params, actor(conv))
        pv["preview"] = {"recipients": [{"serial": "test", "to": s["to"], "subject": subject,
                                         "body_html": body}], "total": 1}
        ap = approval.mint("send_test_email", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "send_test_email", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "send_test_email"
        pv["count"] = 1
        out.append({"kind": "data", "data": {"type": "preview", "data": {"subject": subject, "body_html": body}}})
        out.append(card_outcome(conv, "final_review", pv, step="review", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "to":
            to = (r.get("id") or r.get("text") or "").strip()
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to):
                raise CardError("Enter a valid email address.")
            s["to"] = to
        elif card["step_id"] == "what":
            s["what"] = r.get("id")
        elif card["step_id"] == "pick_tpl":
            s["template_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        note = "Sent as a draft (safe mode is ON)." if res.get("mode") == "draft" else "Sent."
        st = conv["flow"]["st"]
        return (
            [{"kind": "result", "data": {"kind": "result", "title": "Test email",
                                         "data": {"mode": res.get("mode"), "note": note}}}]
            + ([{"kind": "message", "text": "This template version is now marked as tested."}]
               if res.get("mode") != "draft" and st.get("last_gate_template_id") else [])
            + [card_outcome(conv, "options", {"question": "What next?",
                                              "options": [{"id": "done", "label": "Done"}],
                                              "allow_other": False}, step="post")])


# ================================================================ settings (F-12..16)

@reg_flow
class SettingsFlow(Flow):
    id = "settings"
    title = "Change a setting"
    SETTINGS = [
        ("safe_mode", "Live sending (safe mode)", "set_live_sending", "R5"),
        ("limits", "Sending limits & undo window", "set_send_limits", "R2"),
        ("signature", "Signature", "set_signature", "R2"),
        ("branding", "Team branding", "set_branding", "R2"),
        ("ai", "AI features (model / key)", "set_ai_settings", "R5"),
        ("budget", "AI budget limits", "set_ai_budget", "R2"),
    ]

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("which"):
            opts = [{"id": k, "label": label} for k, label, _, _ in self.SETTINGS]
            if actor(conv).get("role") != "admin":
                opts = [o for o in opts if o["id"] == "safe_mode"]  # members: only safe-mode info
                out.append({"kind": "message", "text": "Only an admin can change these settings."})
            out.append(card_outcome(conv, "options", {"question": "Which setting?", "options": opts,
                                                      "allow_other": False}, step="which"))
            return out
        if s["which"] == "safe_mode" and not s.get("val"):
            on = db.get_setting("safe_mode") == "1"
            out.append(card_outcome(conv, "options", {
                "question": "Live sending is currently %s." % ("ON" if not on else "OFF"),
                "options": [{"id": "0", "label": "Turn live sending ON (safe mode off)"},
                            {"id": "1", "label": "Turn live sending OFF (safe mode on)"}],
                "allow_other": False}, step="val"))
            return out
        if s["which"] == "limits" and not s.get("limits_done"):
            cur = services.send_rail_settings()
            out.append(card_outcome(conv, "table_edit", {
                "question": "Sending limits (edit what you want to change).",
                "fields": [
                    {"field": "Daily cap (emails)", "value": str(cur["max_sends_per_day"]), "min": 1, "max": 100000},
                    {"field": "Default stagger (min)", "value": str(cur["default_stagger_min"]), "min": 0, "max": 600},
                    {"field": "Catch-up policy", "value": cur["catchup_policy"], "choices": ["send_now", "mark_missed"]},
                    {"field": "Undo window (seconds)", "value": str(cur["send_undo_seconds"]), "min": 10, "max": 30},
                    {"field": "Bulk threshold (recipients)", "value": str(cur["bulk_confirm_threshold"]), "min": 10, "max": 500},
                ], "columns": []}, step="limits_edit"))
            return out
        if s["which"] == "signature" and not s.get("sig_done"):
            out.append(card_outcome(conv, "draft_editor", {
                "subject": "", "body_html": db.get_setting("signature_html", ""),
                "kind": "signature", "buttons": ["Continue", "Cancel"], "ai": False,
                "hint": "Shown at the end of emails when 'Append signature' is on."}, step="sig_edit"))
            return out
        if s["which"] == "branding" and not s.get("brand_done"):
            out.append(card_outcome(conv, "table_edit", {
                "question": "Branding (leave blank to keep).",
                "fields": [
                    {"field": "Team name", "value": db.get_setting("brand_team_name", "")},
                    {"field": "Login footer", "value": db.get_setting("brand_tagline", "")},
                    {"field": "Admin email", "value": db.get_setting("admin_email", "")},
                ], "columns": []}, step="brand_edit"))
            return out
        if s["which"] == "ai" and s.get("ai_meta_done") is None:
            out.append(card_outcome(conv, "table_edit", {
                "question": "AI settings (leave blank to keep). Changing model/base URL affects this assistant immediately.",
                "fields": [
                    {"field": "Base URL", "value": db.get_setting("ai_base_url", "")},
                    {"field": "Model", "value": db.get_setting("ai_model", "")},
                    {"field": "Enabled", "value": db.get_setting("ai_enabled", "0"), "choices": ["0", "1"]},
                ], "columns": []}, step="ai_meta"))
            return out
        if s["which"] == "ai" and s.get("ai_meta_done") is not None and not s.get("ai_done"):
            out.append(card_outcome(conv, "secret_input", {
                "label": "New API key (leave blank to keep the current one)",
                "hint": "The key is stored server-side and never shown again."}, step="ai_key"))
            return out
        if s["which"] == "budget" and not s.get("budget_done"):
            cur = {k: int(db.get_setting(k, d)) for k, d in
                   (("ai_daily_calls_per_user", 150), ("ai_daily_tokens_per_user", 400000),
                    ("ai_daily_calls_workspace", 0))}
            out.append(card_outcome(conv, "table_edit", {
                "question": "AI budget (0 = unlimited).",
                "fields": [
                    {"field": "Calls per user per day", "value": str(cur["ai_daily_calls_per_user"]), "min": 0},
                    {"field": "Tokens per user per day", "value": str(cur["ai_daily_tokens_per_user"]), "min": 0},
                    {"field": "Calls per workspace per day", "value": str(cur["ai_daily_calls_workspace"]), "min": 0},
                ], "columns": []}, step="budget_edit"))
            return out
        # build the gate
        if s["which"] == "safe_mode":
            params = {"live": s.get("val")}
            action = "set_live_sending"
        elif s["which"] == "limits":
            action = "set_send_limits"
            params = {k: v for k, v in (s.get("limits_vals") or {}).items() if v not in (None, "")}
        elif s["which"] == "signature":
            action = "set_signature"
            params = {"html": s.get("sig_html") or ""}
        elif s["which"] == "branding":
            action = "set_branding"
            params = {k: v for k, v in (s.get("brand_vals") or {}).items() if v not in (None, "")}
        elif s["which"] == "ai":
            action = "set_ai_settings"
            params = {k: v for k, v in (s.get("ai_vals") or {}).items() if v not in (None, "")}
            if s.get("ai_key"):
                params["ai_api_key"] = s["ai_key"]
            s.pop("ai_key", None)  # never persist the secret in conversation state
        else:
            action = "set_ai_budget"
            params = {k: v for k, v in (s.get("budget_vals") or {}).items() if v not in (None, "")}
        a = actions.get(action)
        pv = a.preview(params, actor(conv))
        cur_vals = pv.get("details")
        ap = approval.mint(action, params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": action, "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = action
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "which":
            s["which"] = r.get("id")
        elif card["step_id"] == "val":
            s["val"] = r.get("id")
        elif card["step_id"] == "limits_edit":
            vals = {}
            for row in (r.get("fields") or []):
                f = row.get("field", "")
                v = (row.get("value") or "").strip()
                if "Daily cap" in f:
                    vals["max_sends_per_day"] = v
                elif "stagger" in f.lower():
                    vals["default_stagger_min"] = v
                elif "Catch-up" in f:
                    vals["catchup_policy"] = v
                elif "Undo" in f:
                    vals["send_undo_seconds"] = v
                elif "Bulk" in f:
                    vals["bulk_confirm_threshold"] = v
            s["limits_vals"] = vals
            s["limits_done"] = True
        elif card["step_id"] == "sig_edit":
            if r.get("action") == "cancel":
                return cancel_flow(conv, "declined")
            s["sig_html"] = r.get("body_html") or ""
            s["sig_done"] = True
        elif card["step_id"] == "brand_edit":
            vals = {}
            for row in (r.get("fields") or []):
                f = row.get("field", "")
                v = (row.get("value") or "").strip()
                if "Team name" in f:
                    vals["team_name"] = v or None
                elif "footer" in f:
                    vals["tagline"] = v or None
                elif "Admin email" in f:
                    vals["admin_email"] = v or None
            s["brand_vals"] = vals
            s["brand_done"] = True
        elif card["step_id"] == "ai_meta":
            vals = {}
            for row in (r.get("fields") or []):
                f = row.get("field", "")
                v = (row.get("value") or "").strip()
                if "Base URL" in f:
                    vals["ai_base_url"] = v or None
                elif "Model" in f:
                    vals["ai_model"] = v or None
                elif "Enabled" in f:
                    vals["ai_enabled"] = v or None
            s["ai_vals"] = vals
            s["ai_meta_done"] = True
        elif card["step_id"] == "ai_key":
            s["ai_key"] = (r.get("value") or "").strip()
            s["ai_done"] = True
        elif card["step_id"] == "budget_edit":
            vals = {}
            for row in (r.get("fields") or []):
                f = row.get("field", "")
                v = (row.get("value") or "").strip()
                if "Calls per user" in f:
                    vals["ai_daily_calls_per_user"] = v
                elif "Tokens" in f:
                    vals["ai_daily_tokens_per_user"] = v
                elif "workspace" in f:
                    vals["ai_daily_calls_workspace"] = v
            s["budget_vals"] = vals
            s["budget_done"] = True
        elif card["step_id"] == "test_choice":
            if r.get("id") == "test":
                res = ai_client.test_connection()
                out = [{"kind": "result", "data": {"kind": "result",
                                                    "title": "Connection " + ("OK" if res.get("ok") else "failed"),
                                                    "data": res}}]
            else:
                out = []
            out.append(card_outcome(conv, "options", {"question": "Done?",
                                                      "options": [{"id": "done", "label": "Done"}]}, step="post"))
            return out
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        st = conv["flow"]["st"]
        which = st["slots"].get("which")
        out = [{"kind": "result", "data": {"kind": "result", "title": "Settings updated", "data": res}}]
        if which == "ai":
            out.append(card_outcome(conv, "options", {
                "question": "Test the connection?",
                "options": [{"id": "test", "label": "Test connection"},
                            {"id": "done", "label": "Done"}]}, step="test_choice"))
        return out



# ================================================================ team (F-17/F-52)

@reg_flow
class InviteMember(Flow):
    id = "invite_member"
    title = "Invite a member"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("email"):
            out.append(card_outcome(conv, "text_input", {"label": "Email address to invite",
                                                         "validate": "email", "max_len": 200}, step="email"))
            return out
        me = actor(conv).get("email", "").lower()
        if s["email"].lower() == me:
            out.append({"kind": "message", "text": "You are already on the team."})
            out.append(card_outcome(conv, "options", {"question": "Done?",
                                                      "options": [{"id": "done", "label": "Done"}]}, step="post"))
            return out
        if not s.get("name_done"):
            out.append(card_outcome(conv, "options", {
                "question": "Add a display name?",
                "options": [{"id": "skip", "label": "Skip"}, {"id": "add", "label": "Add a name"}]},
                step="name_choice"))
            return out
        if s.get("name_pending"):
            out.append(card_outcome(conv, "text_input", {"label": "Display name", "validate": "name",
                                                         "max_len": 80}, step="name"))
            return out
        if not s.get("role"):
            out.append(card_outcome(conv, "options", {
                "question": "What role?",
                "options": [{"id": "user", "label": "Member"}, {"id": "admin", "label": "Admin"}]},
                step="role"))
            return out
        params = {"email": s["email"], "name": s.get("name") or "", "role": s.get("role")}
        pv = actions.get("invite_member").preview(params, actor(conv))
        ap = approval.mint("invite_member", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "invite_member", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "invite_member"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "email":
            em = (r.get("text") or "").strip()
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em):
                raise CardError("Enter a valid email address.")
            s["email"] = em
        elif card["step_id"] == "name_choice":
            if r.get("id") == "add":
                s["name_pending"] = True
            else:
                s["name_done"] = True
        elif card["step_id"] == "name":
            s["name"] = (r.get("text") or "").strip()
            s["name_pending"] = False
            s["name_done"] = True
        elif card["step_id"] == "role":
            s["role"] = r.get("id")
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Invite sent", "data": res}}]


@reg_flow
class TeamMember(Flow):
    id = "change_role"
    title = "Change a member's role"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("user_id"):
            rows = _q_rows("SELECT id, email, display_name, role FROM users WHERE status='active' ORDER BY email")
            if not rows:
                out.append(card_outcome(conv, "options", {"question": "No team members.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(r["id"]), "label": "%s (%s)" % (r["email"], r["role"])} for r in rows]
            out.append(card_outcome(conv, "options", {"question": "Which member?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick"))
            return out
        if not s.get("role"):
            u = db.get_user(int(s["user_id"]))
            out.append(card_outcome(conv, "options", {
                "question": "Current role: %s. New role?" % (u["role"] if u else "?"),
                "options": [{"id": "admin", "label": "Admin"}, {"id": "user", "label": "Member"}]},
                step="role"))
            return out
        params = {"user_id": int(s["user_id"]), "role": s["role"]}
        pv = actions.get("change_role").preview(params, actor(conv))
        ap = approval.mint("change_role", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "change_role", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "change_role"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick":
            s["user_id"] = int(r.get("id") or 0)
            if int(s["user_id"]) == _uid(conv):
                raise CardError("You can't change your own role from here.")
        elif card["step_id"] == "role":
            s["role"] = r.get("id")
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Role updated", "data": res}}]


@reg_flow
class RemoveMember(Flow):
    id = "remove_member"
    title = "Remove a member"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("user_id"):
            rows = _q_rows("SELECT id, email, display_name, role FROM users WHERE status='active' ORDER BY email")
            if not rows:
                out.append(card_outcome(conv, "options", {"question": "No team members.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(r["id"]), "label": "%s (%s)" % (r["email"], r["role"])} for r in rows]
            out.append(card_outcome(conv, "options", {"question": "Which member?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick"))
            return out
        params = {"user_id": int(s["user_id"])}
        try:
            pv = actions.get("remove_member").preview(params, actor(conv))
        except ServiceError as e:
            out.append(card_outcome(conv, "options", {"question": e.message,
                                                      "options": [{"id": "done", "label": "Done"}]}, step="post"))
            return out
        ap = approval.mint("remove_member", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "remove_member", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "remove_member"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick":
            s["user_id"] = int(r.get("id") or 0)
            if int(s["user_id"]) == _uid(conv):
                raise CardError("You can't remove yourself.")
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Member removed", "data": res}}]


@reg_flow
class JoinRequests(Flow):
    id = "join_requests"
    title = "Join requests"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("user_id"):
            rows = _q_rows("SELECT id, email, display_name, request_note FROM users WHERE status='pending' ORDER BY created_at")
            if not rows:
                out.append({"kind": "data", "data": {"type": "empty", "text": "No pending join requests."}})
                out.append(card_outcome(conv, "options", {"question": "What now?",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(r["id"]), "label": "%s %s" % (r["email"], r.get("request_note") or "")} for r in rows]
            out.append(card_outcome(conv, "options", {"question": "Which request?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick"))
            return out
        u = db.get_user(int(s["user_id"]))
        if not u:
            out.append(card_outcome(conv, "options", {"question": "Request not found.",
                                                      "options": [{"id": "done", "label": "Done"}]}, step="post"))
            return out
        action = "approve_join" if s.get("decision", "approve") == "approve" else "deny_join"
        params = {"user_id": int(s["user_id"])}
        pv = actions.get(action).preview(params, actor(conv))
        ap = approval.mint(action, params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": action, "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = action
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick":
            s["user_id"] = int(r.get("id") or 0)
            s["decision"] = "approve"
        elif card["step_id"] == "post":
            s["user_id"] = None
            return self.run(conv)
        return self.run(conv)

    def post_gate(self, conv, res):
        st = conv["flow"]["st"]
        st["slots"]["user_id"] = None
        return [{"kind": "result", "data": {"kind": "result", "title": "Request handled", "data": res}}]


# ================================================================ accounts & export

@reg_flow
class RemoveAccount(Flow):
    id = "remove_gmail_account"
    title = "Remove a Gmail account"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("account_id"):
            rows = services.account_list(actor(conv))
            if not rows:
                out.append(card_outcome(conv, "options", {"question": "No accounts to remove.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(a["id"]), "label": a["email"]} for a in rows]
            out.append(card_outcome(conv, "options", {"question": "Which account?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick"))
            return out
        params = {"account_id": int(s["account_id"])}
        pv = actions.get("remove_gmail_account").preview(params, actor(conv))
        ap = approval.mint("remove_gmail_account", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "remove_gmail_account", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "remove_gmail_account"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick":
            s["account_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Account removed", "data": res,
                                            "note": "Queued/scheduled emails on it will show 'send account lost its login'."}}]


@reg_flow
class GmailConnect(Flow):
    id = "gmail_connect"
    title = "Connect Gmail"

    def run(self, conv):
        return [card_outcome(conv, "handoff", {
            "title": "Connect a Gmail account",
            "why": "OAuth consent needs to happen in the settings page - the assistant can't complete "
                   "the Google sign-in itself.",
            "links": [{"label": "Open Settings - Gmail", "page": "/settings#gmail"}],
            "recheck": "accounts"}, step="handoff")]

    def respond(self, conv, card, response):
        r = response or {}
        if card["step_id"] == "handoff":
            if r.get("action") == "recheck":
                rows = services.account_list(actor(conv))
                ok = [a["email"] for a in rows if db.account_token_json(a["id"])]
                return [{"kind": "result", "data": {"kind": "result",
                                                     "title": "Connected accounts",
                                                     "data": {"connected": ok or "none yet"}}}]
            return cancel_flow(conv, "done")
        return self.run(conv)


@reg_flow
class ExportData(Flow):
    id = "export_data"
    title = "Export data"

    def run(self, conv):
        st = conv["flow"]["st"]
        params = {}
        pv = actions.get("export_data").preview(params, actor(conv))
        ap = approval.mint("export_data", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "export_data", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "export_data"
        return [card_outcome(conv, "confirm", pv, step="gate", ttl=600)]

    def respond(self, conv, card, response):
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Export ready",
                                            "data": {"note": "Download the CSV."},
                                            "links": [{"label": "Download CSV", "page": "/api/export/contacts.csv"}]}}]


@reg_flow
class RescheduleBatch(Flow):
    id = "reschedule_batch"
    title = "Reschedule a batch"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("batch_id"):
            rows = _q_rows("SELECT b.id, b.name FROM batches b WHERE b.id IN "
                           "(SELECT batch_id FROM emails WHERE status='scheduled') ORDER BY b.id DESC")
            if not rows:
                out.append(card_outcome(conv, "options", {"question": "No scheduled batches.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(r["id"]), "label": r["name"]} for r in rows]
            out.append(card_outcome(conv, "options", {"question": "Which batch?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick"))
            return out
        if not s.get("at"):
            now = NOW()
            out.append(card_outcome(conv, "datetime_input", {
                "label": "New start time",
                "min_iso": (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"),
                "max_iso": (now + timedelta(days=45)).strftime("%Y-%m-%dT%H:%M")}, step="at"))
            return out
        if s.get("stagger_done") is None:
            out.append(card_outcome(conv, "number_input", {
                "label": "Stagger between emails (minutes)", "min": 0, "max": 600, "step": 1,
                "default": int(db.get_setting("default_stagger_min") or 0), "unit": "min"}, step="stagger"))
            return out
        params = {"batch_id": int(s["batch_id"]), "at": s["at"], "stagger": int(s.get("stagger") or 0)}
        pv = actions.get("reschedule_batch").preview(params, actor(conv))
        ap = approval.mint("reschedule_batch", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "reschedule_batch", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "reschedule_batch"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick":
            s["batch_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "at":
            at = (r.get("value") or "").replace("T", " ")
            try:
                services.parse_when(at)
            except ServiceError as e:
                raise CardError(e.message)
            s["at"] = at
        elif card["step_id"] == "stagger":
            try:
                n = int(r.get("value"))
            except (TypeError, ValueError):
                raise CardError("Enter a whole number.")
            if not (0 <= n <= 600):
                raise CardError("Stagger must be 0-600 minutes.")
            s["stagger"] = n
            s["stagger_done"] = True
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "Batch rescheduled", "data": res}}]


# ================================================================ attach/detach + sheet delete

@reg_flow
class AttachSheet(Flow):
    id = "attach_sheet"
    title = "Attach a list"

    def run(self, conv):
        st = conv["flow"]["st"]
        s = st["slots"]
        out = []
        if not s.get("sheet_id"):
            rows = _q_rows("SELECT id, name, n_rows, campaign_id FROM sheets ORDER BY id")
            if not rows:
                out.append(card_outcome(conv, "options", {"question": "No lists to attach.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(r["id"]), "label": "%s (%s rows%s)" % (
                r["name"], r["n_rows"], "" if not r["campaign_id"] else " - already in campaign %s" % r["campaign_id"])}
                for r in rows]
            out.append(card_outcome(conv, "options", {"question": "Which list?", "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick_sheet"))
            return out
        if not s.get("campaign_id"):
            cams = _campaigns()
            if not cams:
                out.append(card_outcome(conv, "options", {"question": "No campaigns to attach to.",
                                                          "options": [{"id": "done", "label": "Done"}]}, step="post"))
                return out
            opts = [{"id": str(c["id"]), "label": c["name"]} for c in cams]
            out.append(card_outcome(conv, "options", {"question": "Attach to which campaign?",
                                                      "options": opts[:4],
                                                      **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                    step="pick_campaign"))
            return out
        params = {"sheet_id": int(s["sheet_id"]), "campaign_id": int(s["campaign_id"])}
        pv = actions.get("attach_sheet_to_campaign").preview(params, actor(conv))
        ap = approval.mint("attach_sheet_to_campaign", params, conv["_conv_id"], _uid(conv))
        st["gate"] = {"action": "attach_sheet_to_campaign", "approval_id": ap["approval_id"], "params": params}
        pv["approval_id"] = ap["approval_id"]
        pv["action"] = "attach_sheet_to_campaign"
        out.append(card_outcome(conv, "confirm", pv, step="gate", ttl=600))
        return out

    def respond(self, conv, card, response):
        st = conv["flow"]["st"]
        s = st["slots"]
        r = response or {}
        if card["step_id"] == "pick_sheet":
            s["sheet_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "pick_campaign":
            s["campaign_id"] = int(r.get("id") or 0)
        elif card["step_id"] == "post":
            return cancel_flow(conv, "done")
        return self.run(conv)

    def post_gate(self, conv, res):
        return [{"kind": "result", "data": {"kind": "result", "title": "List attached",
                                            "data": {"recipients": res.get("recipients")}}}
                + [card_outcome(conv, "options", {"question": "Set the campaign's template?",
                                                  "options": [{"id": "template", "label": "Yes, set a template"},
                                                              {"id": "done", "label": "Done"}]}, step="next")]]

    def respond(self, conv, card, response):
        if card["step_id"] == "next":
            r = response or {}
            if r.get("id") == "template":
                st = conv["flow"]["st"]
                tpls = _templates()
                if not tpls:
                    return start_flow(conv, "write_template", {})
                opts = [{"id": str(t["id"]), "label": t["name"]} for t in tpls]
                params = {"sheet_id": None, "campaign_id": int(st["slots"]["campaign_id"])}
                return [card_outcome(conv, "options", {"question": "Which template?",
                                                       "options": opts[:4],
                                                       **({"more": {"label": "More", "options": opts[4:]}} if len(opts) > 4 else {})},
                                   step="pick_tpl")]
            return cancel_flow(conv, "done")
        if card["step_id"] == "pick_tpl":
            r = response or {}
            st = conv["flow"]["st"]
            params = {"campaign_id": int(st["slots"]["campaign_id"]), "template_id": int(r.get("id") or 0)}
            pv = actions.get("set_campaign_template").preview(params, actor(conv))
            ap = approval.mint("set_campaign_template", params, conv["_conv_id"], _uid(conv))
            st["gate"] = {"action": "set_campaign_template", "approval_id": ap["approval_id"], "params": params}
            pv["approval_id"] = ap["approval_id"]
            pv["action"] = "set_campaign_template"
            return [card_outcome(conv, "confirm", pv, step="gate", ttl=600)]
        return super().respond(conv, card, response)
