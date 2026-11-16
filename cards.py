"""Card Protocol (spec 6) + Ask AI conversation store.

Cards are the only way the user interacts with flows: options, inputs,
tables, confirm, final_review, compact_review, undo_countdown, plan,
result, data, handoff. Every response is idempotent (card_id + key).
State changes only happen here or in flows; nothing executes on a stale
or superseded card.
"""
import json
import time
import uuid

import ai_client
import db
import actions
import approval
import services

CONV_TTL = 2 * 3600


class CardError(Exception):
    def __init__(self, message, code=409):
        super().__init__(message)
        self.message = message
        self.code = code


# ------------------------------------------------------------- conversations

def create_conv(uid):
    cid = "conv_" + uuid.uuid4().hex[:20]
    c = db.conn()
    c.execute("INSERT INTO ask_ai_convs(conv_id, uid, state_json, created_at, last_used) VALUES(?,?,?,?,?)",
              (cid, int(uid), json.dumps({
                  "uid": int(uid), "mode": db.get_user_setting(int(uid), "ask_ai_mode") or "guided",
                  "messages": [], "flow": None, "pending": None, "declined": [],
                  "draft": None, "events": [], "llm_calls": 0, "read_cache": {}, "budget_notified": False,
                  "version": 1}), db.now_iso(), time.time()))
    c.commit()
    c.close()
    return cid


def get_conv(conv_id, uid):
    """Re-resolve the actor from the CURRENT request, never trust stored uid."""
    if not conv_id:
        return None
    c = db.conn()
    r = c.execute("SELECT * FROM ask_ai_convs WHERE conv_id=?", (conv_id,)).fetchone()
    c.close()
    if not r:
        return None
    if r["uid"] != int(uid):
        return None
    if time.time() - (r["last_used"] or 0) > CONV_TTL:
        expire_conv(r["conv_id"])
        return None
    try:
        state = json.loads(r["state_json"] or "{}")
    except Exception:
        state = {}
    state["_conv_id"] = r["conv_id"]
    return state


def save_conv(state):
    cid = state["_conv_id"]
    c = db.conn()
    c.execute("UPDATE ask_ai_convs SET state_json=?, last_used=? WHERE conv_id=?",
              (json.dumps({k: v for k, v in state.items() if not k.startswith("_")}), time.time(), cid))
    c.commit()
    c.close()


def expire_conv(conv_id):
    c = db.conn()
    c.execute("UPDATE ask_ai_cards SET status='expired' WHERE conv_id=? AND status='pending'", (conv_id,))
    c.execute("DELETE FROM ask_ai_convs WHERE conv_id=?", (conv_id,))
    c.commit()
    c.close()
    approval.void_for_conv(conv_id)


def close_conv(conv_id, uid):
    """C13: closing the drawer cancels every pending interaction - except the
    undo countdown (the undo window keeps running in a toast, spec 12.14)."""
    c = db.conn()
    c.execute("UPDATE ask_ai_cards SET status='cancelled' WHERE conv_id=? AND status='pending' AND type!='undo_countdown'",
              (conv_id,))
    c.commit()
    c.close()
    approval.void_for_conv(conv_id)
    st = get_conv(conv_id, uid)
    if st:
        st["flow"] = st.get("flow")  # flow state survives for resume; pending cleared
        st["pending"] = None
        save_conv(st)


# --------------------------------------------------------------------- cards

def new_card(conv, ctype, payload, step_id=None, flow_id=None, ttl=600):
    cid = "card_" + uuid.uuid4().hex[:16]
    c = db.conn()
    c.execute("INSERT INTO ask_ai_cards(card_id, conv_id, type, status, payload_json, step_id, flow_id, created_at, expires_at) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (cid, conv["_conv_id"], ctype, "pending", json.dumps(payload, default=str), step_id, flow_id,
               time.time(), time.time() + (ttl or 600)))
    c.commit()
    c.close()
    return envelope(cid, conv["_conv_id"], ctype, payload, step_id, flow_id)


def get_card(card_id, conv=None):
    c = db.conn()
    r = c.execute("SELECT * FROM ask_ai_cards WHERE card_id=?", (card_id,)).fetchone()
    c.close()
    if not r:
        return None
    d = dict(r)
    d["payload"] = json.loads(d.get("payload_json") or "{}")
    d["result"] = json.loads(d["result_json"]) if d.get("result_json") else None
    return d


def mark_card(card_id, status, result=None):
    c = db.conn()
    c.execute("UPDATE ask_ai_cards SET status=?, result_json=? WHERE card_id=?",
              (status, json.dumps(result, default=str) if result is not None else None, card_id))
    c.commit()
    c.close()


def envelope(card_id, conv_id, ctype, payload, step_id=None, flow_id=None):
    return {"id": card_id, "conversation_id": conv_id, "flow_id": flow_id or "",
            "step_id": step_id or "", "type": ctype, "status": "pending",
            "created_at": time.time(), "payload": payload}


# ------------------------------------------------------------- idempotency

def stored_result(card_id, key):
    if not key:
        return None
    c = db.conn()
    r = c.execute("SELECT result_json FROM card_responses WHERE card_id=? AND idempotency_key=?",
                  (card_id, key)).fetchone()
    c.close()
    if r:
        try:
            return json.loads(r["result_json"])
        except Exception:
            return None
    return None


def store_result(card_id, key, result):
    if not key:
        return
    c = db.conn()
    c.execute("INSERT OR REPLACE INTO card_responses(card_id, idempotency_key, result_json) VALUES(?,?,?)",
              (card_id, key, json.dumps(result, default=str)))
    c.commit()
    c.close()


# ------------------------------------------------------------- C9 supersede

CANCEL_WORDS = ("cancel", "stop", "never mind", "nevermind", "abort", "back off", "drop it",
                "no thanks", "no, cancel", "quit")


def supersede_pending(state, text):
    """C9: typing while a card is pending. Cancel words cancel the card;
    otherwise the card becomes inert (superseded) and nothing executes."""
    import flows
    if not state.get("pending_card"):
        return []
    old_pending = state["pending_card"]
    text_l = (text or "").strip().lower()
    is_cancel = any(w in text_l for w in CANCEL_WORDS)
    outcomes = []
    if is_cancel:
        outcomes.extend(flows.cancel_flow(state, "cancelled"))
        _mark(old_pending, "cancelled", {"reason": "cancelled by user"})
    else:
        _mark(old_pending, "superseded", {"reason": "replaced by a new request"})
        state["pending"] = None
        state["pending_card"] = None
        # keep flow state so the user can come back; current card inert
        outcomes.append({"kind": "message", "text": "I cancelled the card above - here's what I understood instead."})
    save_conv(state)
    return outcomes


# ------------------------------------------------------------- respond

def _mark(card_id, status, result=None):
    mark_card(card_id, status, result)


def respond(card_id, conv, response, idem_key):
    """Handle a card response. Returns a list of outcomes for the client.
    The actor is conv["uid"] re-validated by the route (request.user)."""
    uid = int(conv["uid"])
    stored = stored_result(card_id, idem_key)
    if stored is not None:
        return stored  # C14: double-click / two tabs / retry -> original result
    card = get_card(card_id)
    if not card or card["conv_id"] != conv["_conv_id"]:
        raise CardError("That card is no longer available.", 404)
    if card["status"] == "answered":
        return card["result"] or {"ok": True, "note": "Already answered."}
    if card["status"] != "pending":
        raise CardError("That card is no longer active (" + card["status"] + ").", 409)
    if card.get("expires_at") and time.time() > card["expires_at"] and card["type"] in ("confirm", "final_review", "compact_review"):
        approval.void(card["payload"].get("approval_id"))
        mark_card(card_id, "expired")
        raise CardError("That approval expired for safety - the card was regenerated.", 409)

    import flows
    ctype = card["type"]
    payload = card["payload"]
    if ctype in ("confirm", "final_review", "compact_review"):
        outcomes = _respond_gate(card, conv, response)
    elif ctype == "undo_countdown":
        outcomes = _respond_undo(card, conv, response)
    elif not conv.get("flow"):
        outcomes = _respond_loose(card, conv, response)
    else:
        outcomes = flows.on_card_response(conv, card, response)
    # draft-editor actions that keep the editor open (revise / subjects)
    keep_pending = (ctype == "draft_editor"
                    and (response or {}).get("action") in ("revise", "subjects"))
    if keep_pending:
        store_result(card_id, idem_key, outcomes)
        save_conv(conv)
        return outcomes
    conv["pending"] = None
    if conv.get("pending_card") == card_id:
        conv["pending_card"] = None
    stored_resp = response
    if ctype == "secret_input" and isinstance(response, dict) and "value" in response:
        stored_resp = dict(response)
        stored_resp["value"] = "***"  # secrets are never echoed back or persisted
    mark_card(card_id, "answered", {"response": stored_resp})
    store_result(card_id, idem_key, outcomes)
    save_conv(conv)
    return outcomes


def _respond_gate(card, conv, response):
    """confirm / final_review / compact_review click (spec 5.3, 7.8)."""
    import flows
    uid = int(conv["uid"])
    p = card["payload"]
    resp = response or {}
    if resp.get("action") in ("edit", "back"):
        return flows.on_card_response(conv, card, resp)
    if resp.get("action") == "cancel":
        approval.void(p.get("approval_id"))
        return flows.cancel_flow(conv, "declined")
    if resp.get("action") != "confirm":
        raise CardError("Unknown response.")
    # server-side pre-send guard: a blocking line (cap, safe mode, account, ...)
    # can never be confirmed, in either mode
    if p.get("action") == "prepare_send" and p.get("count"):
        import presend
        import json as _json
        try:
            stored = _json.loads((approval.get(p.get("approval_id")) or {}).get("params_json") or "{}")
        except Exception:
            stored = {}
        if stored.get("action") == "send":
            import services
            stored = dict(stored)
            stored["_rows"] = services.audience_rows(int(stored.get("sheet_id") or 0),
                                                     stored.get("serial_from"), stored.get("serial_to"), None)
            pre = presend.run(stored)
            bad = [l for l in pre.get("lines", []) if l.get("severity") == "block"]
            if bad:
                raise CardError("Cannot confirm: " + bad[0]["text"], 409)
    ack = resp.get("ack") or {}
    # bulk ack: checkbox (reviewed) and typed count for 4x threshold
    if ack.get("reviewed") is False:
        ack.pop("reviewed", None)
    try:
        res, verify = actions.execute_gated(
            p.get("action"), p.get("approval_id"), conv["_conv_id"], uid,
            ack=ack if ack else None, expected_count=p.get("count"))
    except approval.ApprovalError as e:
        raise CardError(e.message, e.code)
    except services.ServiceError as e:
        raise CardError(e.message, e.code)
    except actions.ActionError as e:
        raise CardError(e.message, e.code)
    outcomes = [{"kind": "message", "text": "Done."}]
    outcomes.append({"kind": "result", "data": verify if isinstance(verify, dict) and verify.get("kind") == "result"
                     else {"kind": "result", "title": p.get("title") or "Done", "data": verify}})
    # undo window card for held sends
    if p.get("action") == "prepare_send" and res.get("holding"):
        outcomes.insert(1, {"kind": "card", "card": new_card(
            conv, "undo_countdown", {
                "batch_id": res.get("batch_id"), "count": res.get("created"),
                "release_at": _iso_to_ts(res.get("release_at")), "undo_seconds": int(db.get_setting("send_undo_seconds") or 20)},
            step_id="undo", flow_id="send_campaign", ttl=3600)})
    # next steps
    outcomes.extend(flows.after_gate(conv, p.get("action"), res))
    return outcomes


def _iso_to_ts(s):
    if not s:
        return time.time()
    try:
        from datetime import datetime
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return time.time()


def _respond_undo(card, conv, response):
    """Undo a held send - one click, no confirm, idempotent (spec 7.7)."""
    uid = int(conv["uid"])
    resp = response or {}
    p = card["payload"]
    if resp.get("action") == "stop":
        try:
            res = services.stop_remaining(int(p["batch_id"]), uid)
        except services.ServiceError as e:
            raise CardError(e.message, e.code)
        return [{"kind": "result", "data": {"kind": "result", "title": "Stopped",
                                            "data": {"stopped": res["stopped"], "sent": res["sent"]}}}]
    if resp.get("action") == "release":
        return _release_check(int(p["batch_id"]), conv)
    if resp.get("action") == "stop_choice":
        if (resp.get("id") or resp.get("action")) == "stop":
            try:
                res = services.stop_remaining(int(p["batch_id"]), uid)
            except services.ServiceError as e:
                raise CardError(e.message, e.code)
            return [{"kind": "result", "data": {"kind": "result", "title": "Stopped",
                                                "data": {"stopped": res["stopped"], "sent": res["sent"]}}}]
        return [{"kind": "message", "text": "Kept - the remaining emails will send as their time comes."}]
    if resp.get("action") != "undo":
        raise CardError("Unknown response.")
    try:
        res = services.undo_send(int(p["batch_id"]), uid)
    except services.ServiceError as e:
        raise CardError(e.message, e.code)
    return [{"kind": "result", "data": {"kind": "result", "title": "Send undone",
                                        "data": {"cancelled": res["cancelled"],
                                                 "note": "Nothing was sent. The daily cap reservation was released."}}}]


def _release_check(bid, conv=None):
    """After the undo window: report what happened (deterministic read)."""
    c = db.conn()
    rows = c.execute("SELECT status, COUNT(*) n FROM emails WHERE batch_id=? GROUP BY status", (bid,)).fetchall()
    c.close()
    st = {r["status"]: r["n"] for r in rows}
    data = {"sent": st.get("sent", 0), "failed": st.get("error", 0),
            "scheduled": st.get("scheduled", 0), "pending": st.get("pending", 0),
            "cancelled": st.get("cancelled", 0)}
    remaining = data["scheduled"] + data["pending"]
    if remaining and conv is not None:
        out = [{"kind": "result", "data": {"kind": "result", "title": "Undo window released",
                                           "data": data,
                                           "note": "%d sent, %d still to go - they will send as their time comes." % (data["sent"], remaining)}}]
        out.append({"kind": "card", "card": new_card(conv, "options", {
            "question": "Stop the remaining %d?" % remaining,
            "options": [{"id": "stop", "label": "Stop remaining %d" % remaining, "style": "danger"},
                        {"id": "keep", "label": "Keep sending"}],
            "allow_other": False}, step_id="stop_choice",
            flow_id="send_campaign", ttl=3600)})
        return out
    return [{"kind": "result", "data": {"kind": "result", "title": "Undo window released",
                                        "data": data,
                                        "note": "These emails have been released. Mail already sent cannot be recalled from this app."}}]


def _respond_loose(card, conv, response):
    """Option cards outside any flow (ai_off, budget, ask_user, handoff)."""
    import actions
    import router
    step = card.get("step_id") or ""
    p = card.get("payload") or {}
    r = response or {}
    uid = int(conv["uid"])
    if step == "ai_off":
        if r.get("id") == "settings":
            env = new_card(conv, "handoff", {
                "title": "AI features",
                "why": "The API key and model are configured in Settings.",
                "links": [{"label": "Open Settings", "page": "/settings"}],
                "recheck": "ai"}, step_id="ai_off_handoff")
            conv["pending_card"] = env["id"]
            return [{"kind": "card", "card": env}]
        user = db.get_user(uid)
        res = actions.read_direct("get_help", {"topic": "start"}, user)
        data = res if (isinstance(res, dict) and res.get("type")) else {"type": "data", "data": res}
        return [{"kind": "data", "data": data}]
    if step == "ai_off_handoff":
        if r.get("id") == "recheck" or r.get("action") == "recheck":
            res = ai_client.test_connection()
            return [{"kind": "result", "data": {"kind": "result",
                                                 "title": "Connection " + ("OK" if res.get("ok") else "failed"),
                                                 "data": res}}]
        return [{"kind": "message", "text": "OK."}]
    if step == "budget":
        return [{"kind": "message",
                 "text": "OK - data questions and deterministic flows still work until the budget resets."}]
    if step == "ask_user":
        label = ""
        for o in (p.get("options") or []):
            if isinstance(o, dict) and o.get("id") == r.get("id"):
                label = o.get("label") or ""
                break
        text = (r.get("text") or label or "ok").strip()
        return router.route(conv, text=text)
    return [{"kind": "message", "text": "Noted."}]


def update(card_id, conv, fields, version, idem_key):
    """Compact review field change (spec 6.7). New version + NEW approval each time.
    The fresh payload (with the new approval_id) is persisted onto the card row,
    so the confirm click always uses the latest stored params."""
    uid = int(conv["uid"])
    card = get_card(card_id)
    if not card or card["conv_id"] != conv["_conv_id"]:
        raise CardError("That card is no longer available.", 404)
    if card["status"] != "pending" or card["type"] != "compact_review":
        raise CardError("That card is no longer active.", 409)
    import flows
    out = flows.on_compact_update(conv, card, fields or {}, version)
    for o in out:
        if o.get("kind") == "compact_update":
            c = db.conn()
            c.execute("UPDATE ask_ai_cards SET payload_json=? WHERE card_id=?",
                      (json.dumps(o["data"], default=str), card_id))
            c.commit()
            c.close()
    return out


def cancel_card(card_id, conv):
    card = get_card(card_id)
    if not card or card["conv_id"] != conv["_conv_id"]:
        raise CardError("That card is no longer available.", 404)
    if card["status"] != "pending":
        return [{"kind": "message", "text": "Already handled."}]
    import flows
    approval.void(card["payload"].get("approval_id"))
    mark_card(card_id, "cancelled", {"reason": "cancelled"})
    conv["pending"] = None
    conv["pending_card"] = None
    outcomes = flows.cancel_flow(conv, "cancelled")
    save_conv(conv)
    return outcomes


def restore(conv):
    """Re-open: return pending cards so the client can re-render them (C13)."""
    c = db.conn()
    rows = c.execute("SELECT * FROM ask_ai_cards WHERE conv_id=? AND status='pending' ORDER BY created_at",
                     (conv["_conv_id"],)).fetchall()
    c.close()
    out = []
    for r in rows:
        r = dict(r)
        out.append(envelope(r["card_id"], conv["_conv_id"], r["type"],
                            json.loads(r.get("payload_json") or "{}"), r["step_id"], r["flow_id"]))
    return out
