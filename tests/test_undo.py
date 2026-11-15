"""Spec 17.1.15 + 7.7: the undo window for Send now. Zero Gmail calls during
the hold; one-click idempotent undo; release semantics; safe-mode block."""
import json
from datetime import datetime, timedelta

import actions
import app as appmod
import db
import services
from conftest import ADMIN, post, find_card


def pj(client, url, body, email=ADMIN):
    return post(client, url, body, email=email).json


def card(outcomes, ctype=None):
    for o in outcomes or []:
        if o.get("kind") == "card" and (ctype is None or o["card"]["type"] == ctype):
            return o["card"]
    return None


def walk_to_send_review(client, mock_llm):
    mock_llm.queue.append({"content": "", "tool_calls": [{
        "id": "c1", "function": {"name": "start_flow",
                                 "arguments": json.dumps({"flow_id": "send_campaign"})}}]})
    d = pj(client, "/api/ai/agent", {"text": "send emails"})
    assert d["ok"]
    for resp, key in (({"id": "1"}, "a"), ({"id": "1"}, "b"), ({"id": "all"}, "c"), ({"id": "1"}, "d")):
        d = pj(client, "/api/ai/cards/%s/respond" % card(d["outcomes"], "options")["id"],
               {"response": resp, "idempotency_key": key})
    m = card(d["outcomes"], "table_edit")
    cols = m["payload"]["columns"]
    d = pj(client, "/api/ai/cards/%s/respond" % m["id"],
           {"response": {"fields": [{"field": f["field"],
                                     "value": f["field"] if f["field"] in cols else cols[1]}
                                    for f in m["payload"]["fields"]]},
            "idempotency_key": "e"})
    d = pj(client, "/api/ai/cards/%s/respond" % card(d["outcomes"], "text_input")["id"],
           {"response": {"text": ""}, "idempotency_key": "f"})
    d = pj(client, "/api/ai/cards/%s/respond" % card(d["outcomes"], "options")["id"],
           {"response": {"id": "0"}, "idempotency_key": "g"})
    d = pj(client, "/api/ai/cards/%s/respond" % card(d["outcomes"], "options")["id"],
           {"response": {"id": "none"}, "idempotency_key": "h"})
    d = pj(client, "/api/ai/cards/%s/respond" % card(d["outcomes"], "options")["id"],
           {"response": {"id": "send"}, "idempotency_key": "i"})
    d = pj(client, "/api/ai/cards/%s/respond" % card(d["outcomes"], "number_input")["id"],
           {"response": {"value": "0"}, "idempotency_key": "j"})
    d = pj(client, "/api/ai/cards/%s/respond" % card(d["outcomes"], "options")["id"],
           {"response": {"id": "none"}, "idempotency_key": "k"})
    review = card(d["outcomes"], "final_review")
    assert review, [o for o in d["outcomes"] if o.get("kind") == "card"]
    return review


def test_send_now_hold_and_undo(client, tenant, mock_llm, mock_gmail):
    db.set_setting("send_undo_seconds", "10")
    review = walk_to_send_review(client, mock_llm)
    d = pj(client, "/api/ai/cards/%s/respond" % review["id"],
           {"response": {"action": "confirm"}, "idempotency_key": "u0"})
    assert d["ok"], d
    ucard = card(d["outcomes"], "undo_countdown")
    assert ucard, "a visible undo countdown card must appear"
    assert ucard["payload"]["count"] == 12
    assert mock_gmail.sends == [], "zero Gmail calls during the hold"
    c = db.conn()
    n_sched = c.execute("SELECT COUNT(*) n FROM emails WHERE status='scheduled'").fetchone()["n"]
    hold = c.execute("SELECT * FROM undo_holds").fetchone()
    c.close()
    assert n_sched == 12 and hold
    # undo: one click, no confirmation
    d = pj(client, "/api/ai/cards/%s/respond" % ucard["id"],
           {"response": {"action": "undo"}, "idempotency_key": "u1"})
    assert d["ok"], d
    c = db.conn()
    st = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status")}
    holds = c.execute("SELECT COUNT(*) n FROM undo_holds").fetchone()["n"]
    c.close()
    assert st.get("cancelled") == 12 and holds == 0
    assert services.sends_today(include_held=True) == 0, "cap reservation released"
    acts = [dict(r) for r in db.conn().execute("SELECT * FROM activity WHERE action='send_undone'")]
    assert acts
    # double undo with the SAME idempotency key returns the original result
    d2 = pj(client, "/api/ai/cards/%s/respond" % ucard["id"],
            {"response": {"action": "undo"}, "idempotency_key": "u1"})
    assert d2["outcomes"] == d["outcomes"]
    # a fresh undo once the batch is gone is rejected at the service level, no state change
    bid = ucard["payload"]["batch_id"]
    try:
        services.undo_send(bid, tenant["admin"])
        raise AssertionError("undo after release must be rejected")
    except services.ServiceError as e:
        assert "recall" in str(e).lower() or "released" in str(e).lower()
    c = db.conn()
    st = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status")}
    c.close()
    assert st.get("cancelled") == 12


def test_release_path_and_post_release_undo_rejected(client, tenant, mock_llm, mock_gmail):
    db.set_setting("send_undo_seconds", "10")
    review = walk_to_send_review(client, mock_llm)
    d = pj(client, "/api/ai/cards/%s/respond" % review["id"],
           {"response": {"action": "confirm"}, "idempotency_key": "r0"})
    assert d["ok"]
    ucard = card(d["outcomes"], "undo_countdown")
    # force the window to be over
    c = db.conn()
    c.execute("UPDATE undo_holds SET release_at=datetime('now','-1 second')")
    c.commit(); c.close()
    appmod._release_due_holds()
    c = db.conn()
    holds = c.execute("SELECT COUNT(*) n FROM undo_holds").fetchone()["n"]
    n_sched = c.execute("SELECT COUNT(*) n FROM emails WHERE status='scheduled'").fetchone()["n"]
    c.close()
    assert holds == 0 and n_sched == 12, "release keeps the rows scheduled (cloud sends them)"
    d = pj(client, "/api/ai/cards/%s/respond" % ucard["id"],
           {"response": {"action": "undo"}, "idempotency_key": "r1"})
    assert not d["ok"], "undo after release must be rejected"
    assert mock_gmail.sends == []


def test_safe_mode_on_during_window_blocks_release(client, tenant, mock_llm, mock_gmail):
    review = walk_to_send_review(client, mock_llm)
    d = pj(client, "/api/ai/cards/%s/respond" % review["id"],
           {"response": {"action": "confirm"}, "idempotency_key": "s0"})
    assert d["ok"]
    db.set_setting("safe_mode", "1")
    c = db.conn()
    c.execute("UPDATE undo_holds SET release_at=datetime('now','-1 second')")
    c.commit(); c.close()
    appmod._release_due_holds()
    c = db.conn()
    st = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status")}
    holds = c.execute("SELECT COUNT(*) n FROM undo_holds").fetchone()["n"]
    acts = [dict(r) for r in c.execute("SELECT * FROM activity WHERE action='hold_release_blocked'")]
    c.close()
    assert holds == 0 and st.get("pending") == 12, "emails stay queued when safe mode is ON"
    assert acts
    assert mock_gmail.sends == []


def test_undo_seconds_range_enforced(client, tenant):
    admin = db.get_user(tenant["admin"])
    before = db.get_setting("send_undo_seconds")
    for bad in ("9", "31", "0", "abc", "-1"):
        try:
            services.settings_update(admin, {"send_undo_seconds": bad})
            raise AssertionError("send_undo_seconds %r accepted" % bad)
        except services.ServiceError:
            pass
    assert db.get_setting("send_undo_seconds") == before, "rejected values must not persist"
    services.settings_update(admin, {"send_undo_seconds": "25"})
    assert db.get_setting("send_undo_seconds") == "25"


def test_hold_flag_never_in_tool_schemas(client, tenant):
    admin = db.get_user(tenant["admin"])
    dump = json.dumps(actions.llm_tools(admin))
    assert "internal_hold_seconds" not in dump
    assert "undo_hold" not in dump
    # and the prepare_send registry schema is never itself offered to the model
    assert all(t["function"]["name"] != "prepare_send" for t in actions.llm_tools(admin))


# ---------------- D-32 / B2: the UI merge endpoint ----------------

MERGE_BODY = {
    "name": "Diwali · List 1", "template_id": 1,
    "mapping": {"company": "column:company", "hr_name": "column:hr_name"},
    "campaign_id": 1, "sheet_id": 1, "account_id": 1,
    "action": "send", "at": "", "stagger": 0, "cc": "", "append_signature": 0,
}


def test_merge_strips_tampered_hold_keys(client, tenant):
    # D-32: a tampered client must not bypass safe mode with internal_hold.
    db.set_setting("safe_mode", "1")
    r = post(client, "/api/merge/generate", {**MERGE_BODY, "internal_hold": True,
                                             "internal_hold_seconds": 60})
    assert r.status_code == 409, r.get_json()
    c = db.conn()
    assert c.execute("SELECT COUNT(*) n FROM emails").fetchone()["n"] == 0
    assert c.execute("SELECT COUNT(*) n FROM undo_holds").fetchone()["n"] == 0
    c.close()


def test_merge_server_sanctioned_hold(client, tenant, mock_gmail):
    db.set_setting("send_undo_seconds", "25")
    r = post(client, "/api/merge/generate", {**MERGE_BODY, "hold": True})
    d = r.get_json()
    assert d["ok"], d
    assert d.get("holding") and d.get("scheduled") == 12
    assert mock_gmail.sends == [], "zero Gmail calls during the hold"
    c = db.conn()
    hold = c.execute("SELECT * FROM undo_holds").fetchone()
    c.close()
    assert hold, "an undo hold row must be recorded"
    rel = datetime.strptime(hold["release_at"], "%Y-%m-%d %H:%M:%S")
    delta = (rel - datetime.now()).total_seconds()
    assert 20 <= delta <= 30, delta
    # undo through the new batch route
    r2 = post(client, "/api/batches/%d/undo" % d["batch_id"], {})
    assert r2.status_code == 200 and r2.get_json()["cancelled"] == 12
    c = db.conn()
    st = {row["status"]: row["n"] for row in c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status")}
    holds = c.execute("SELECT COUNT(*) n FROM undo_holds").fetchone()["n"]
    c.close()
    assert st.get("cancelled") == 12 and holds == 0
    # a second undo is rejected, no state change
    r3 = post(client, "/api/batches/%d/undo" % d["batch_id"], {})
    assert r3.status_code == 409


def test_merge_send_without_hold_sends_immediately(client, tenant, mock_gmail):
    r = post(client, "/api/merge/generate", MERGE_BODY)
    d = r.get_json()
    assert d["ok"], d
    assert d.get("sent") == 12 and not d.get("holding")
    assert len(mock_gmail.sends) == 12
    c = db.conn()
    assert c.execute("SELECT COUNT(*) n FROM undo_holds").fetchone()["n"] == 0
    c.close()


def test_send_as_holds_only_when_sanctioned(client, tenant, mock_gmail):
    # create a queued batch first
    r = post(client, "/api/merge/generate", {**MERGE_BODY, "action": "create"})
    bid = r.get_json()["batch_id"]
    db.set_setting("send_undo_seconds", "15")
    # tampered hold keys on send-as must not create a hold (route ignores them)
    r2 = post(client, "/api/batches/%d/send-as" % bid,
              {"action": "send", "account_id": 1, "internal_hold_seconds": 60})
    d2 = r2.get_json()
    assert d2["ok"], d2
    assert not d2.get("holding"), d2
    assert len(mock_gmail.sends) == 12
    c = db.conn()
    assert c.execute("SELECT COUNT(*) n FROM undo_holds").fetchone()["n"] == 0
    c.close()
