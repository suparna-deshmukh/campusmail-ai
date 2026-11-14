"""Ask AI entry point.

Routes user messages through the Intent Router (Tier 0 deterministic,
Tier 1 one LLM call) and walks flows. Card responses go through cards.py
endpoints directly. The LLM never executes mutating actions - only R0 reads
and start_flow. Every R2+ action runs through the Approval Gate.
"""
import json

import actions
import cards
import db
import router


def turn(uid, conv_id=None, text=None, file=None, **legacy):
    """One user message (text and/or file). Returns {conv_id, mode, outcomes}."""
    user = db.get_user(int(uid))
    if not user or (user.get("status") != "active" and not user.get("personal_workspace")):
        return {"ok": False, "error": "Session expired - sign in again.", "code": 401}
    conv = cards.get_conv(conv_id, uid) if conv_id else None
    if conv is None:
        cid = cards.create_conv(uid)
        conv = cards.get_conv(cid, uid)
    conv["user"] = user
    out = []
    # C9: typing while a card is pending
    if text and conv.get("pending_card"):
        out += cards.supersede_pending(conv, text)
    out += router.route(conv, text=text, file=file)
    # history (cards collapsed to one line for the model)
    for o in out:
        k = o.get("kind")
        if k == "message":
            conv.setdefault("messages", []).append({"role": "assistant", "content": o.get("text", "")[:500]})
        elif k == "card" and o.get("card"):
            q = (o["card"].get("payload") or {}).get("question") or (o["card"].get("payload") or {}).get("label") or o["card"]["type"]
            conv.setdefault("messages", []).append({"role": "assistant", "content": "[card %s: %s]" % (o["card"]["type"], str(q)[:120])})
    if not text and file:
        conv.pop("file", None)
    cards.save_conv(conv)
    return {"ok": True, "conv_id": conv["_conv_id"], "mode": conv.get("mode") or "guided",
            "outcomes": out}


def respond(card_id, uid, response, idem_key, conv_hint=None):
    card = cards.get_card(card_id)
    if not card:
        return {"ok": False, "error": "That card is no longer available.", "code": 404}
    conv = cards.get_conv(conv_hint or card["conv_id"], uid)
    if conv is None:
        return {"ok": False, "error": "This conversation expired - start a new one.", "code": 410}
    conv["user"] = db.get_user(int(uid))
    try:
        out = cards.respond(card_id, conv, response, idem_key)
    except cards.CardError as e:
        return {"ok": False, "error": e.message, "code": e.code}
    except actions.ApprovalRequired as e:
        return {"ok": False, "error": str(e), "code": 403}
    except Exception as e:
        return {"ok": False, "error": "Something went wrong: " + str(e)[:160], "code": 500}
    return {"ok": True, "conv_id": conv["_conv_id"], "outcomes": out}


def update(card_id, uid, fields, version, idem_key):
    card = cards.get_card(card_id)
    if not card:
        return {"ok": False, "error": "That card is no longer available.", "code": 404}
    conv = cards.get_conv(card["conv_id"], uid)
    if conv is None:
        return {"ok": False, "error": "This conversation expired - start a new one.", "code": 410}
    conv["user"] = db.get_user(int(uid))
    try:
        out = cards.update(card_id, conv, fields, version, idem_key)
    except cards.CardError as e:
        return {"ok": False, "error": e.message, "code": e.code}
    cards.save_conv(conv)
    return {"ok": True, "outcomes": out}


def cancel(card_id, uid):
    card = cards.get_card(card_id)
    if not card:
        return {"ok": False, "error": "That card is no longer available.", "code": 404}
    conv = cards.get_conv(card["conv_id"], uid)
    if conv is None:
        return {"ok": False, "error": "This conversation expired.", "code": 410}
    conv["user"] = db.get_user(int(uid))
    try:
        out = cards.cancel_card(card_id, conv)
    except cards.CardError as e:
        return {"ok": False, "error": e.message, "code": e.code}
    return {"ok": True, "conv_id": conv["_conv_id"], "outcomes": out}


def restore(conv_id, uid):
    conv = cards.get_conv(conv_id, uid)
    if conv is None:
        return {"ok": False, "error": "This conversation expired.", "code": 410}
    return {"ok": True, "conv_id": conv_id, "mode": conv.get("mode") or "guided",
            "cards": cards.restore(conv),
            "flow": (conv.get("flow") or {}).get("flow_id")}


def close(conv_id, uid):
    cards.close_conv(conv_id, uid)
    return {"ok": True}


def latest_conv(uid):
    c = db.conn()
    r = c.execute("SELECT conv_id FROM ask_ai_convs WHERE uid=? ORDER BY last_used DESC LIMIT 1",
                  (int(uid),)).fetchone()
    c.close()
    if not r:
        return None
    conv = cards.get_conv(r["conv_id"], uid)
    return r["conv_id"] if conv else None
