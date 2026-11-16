"""Spec 6.7 + 7.9 + 17.1.18: Compact mode - one editable review card, no
pre-selected action, new approval per change, stale versions rejected,
mid-flow switch keeps values, and Guided/Compact produce identical params
and rendered emails."""
import json

import approval
import actions
import db
from conftest import post, find_card, find_outcome


def start_send_flow(client, mock_llm, key="cmp"):
    mock_llm.queue.append({"content": "", "tool_calls": [{
        "id": key + "1", "function": {"name": "start_flow",
                                      "arguments": json.dumps({"flow_id": "send_campaign"})}}]})
    d = post(client, "/api/ai/agent", {"text": "send emails"})
    assert d.json["ok"], d.json
    return d.json


def to_review_card(client, d, key):
    """Answer campaign/audience/range/template/mapping; returns the next card."""
    def go(ctype, response, k):
        c = find_card(d["outcomes"], ctype)
        assert c, "expected %s, got %s" % (ctype, [o["card"]["type"] for o in d["outcomes"] if o.get("kind") == "card"])
        r = post(client, "/api/ai/cards/%s/respond" % c["id"],
                 {"response": response, "idempotency_key": k})
        assert r.json["ok"], r.json
        return r.json

    d = go("options", {"id": "1"}, key + "a")
    d = go("options", {"id": "1"}, key + "b")
    d = go("options", {"id": "all"}, key + "c")
    d = go("options", {"id": "1"}, key + "d")
    m = find_card(d["outcomes"], "table_edit")
    cols = [x for x in (m["payload"].get("columns") or []) if x]
    r = post(client, "/api/ai/cards/%s/respond" % m["id"],
             {"response": {"fields": [{"field": f["field"],
                                       "value": f["field"] if f["field"] in cols else cols[1]}
                                      for f in m["payload"]["fields"]]},
              "idempotency_key": key + "e"})
    assert r.json["ok"], r.json
    return r.json


def test_mode_switch_persists(client, tenant, mock_llm):
    d = post(client, "/api/ai/agent", {"text": "switch to compact"})
    assert d.json["ok"] and d.json["mode"] == "compact"
    assert db.get_user_setting(tenant["admin"], "ask_ai_mode") == "compact"
    d = post(client, "/api/ai/agent", {"text": "switch to guided"})
    assert d.json["mode"] == "guided"
    assert db.get_user_setting(tenant["admin"], "ask_ai_mode") == "guided"


def test_compact_review_shape_no_preselected_action(client, tenant, mock_llm):
    post(client, "/api/ai/agent", {"text": "switch to compact"})
    d = start_send_flow(client, mock_llm, key="c1")
    d = to_review_card(client, d, "c1")
    card = find_card(d["outcomes"], "compact_review")
    assert card, [o["card"]["type"] for o in d["outcomes"] if o.get("kind") == "card"]
    p = card["payload"]
    assert p["action_selected"] is None, "the send action must never be pre-selected"
    assert "stagger_default" in p and "safety" in p
    assert p["presend"]["lines"] is not None and p["presend"]["blocked"] is False
    assert p["email"]["audience"] == "12 recipient(s)"
    assert p["count"] == 12


def test_update_mints_new_approval_and_voids_old(client, tenant, mock_llm):
    post(client, "/api/ai/agent", {"text": "switch to compact"})
    d = start_send_flow(client, mock_llm, key="c2")
    d = to_review_card(client, d, "c2")
    card = find_card(d["outcomes"], "compact_review")
    p = card["payload"]
    old_appr = p["approval_id"]
    version = p["version"]

    r = post(client, "/api/ai/cards/%s/update" % card["id"],
             {"fields": {"action": "schedule", "at": "2026-12-20 10:00"},
              "version": version, "idempotency_key": "c2u1"})
    assert r.json["ok"], r.json
    upd = [o for o in r.json["outcomes"] if o["kind"] == "compact_update"][0]["data"]
    assert upd["version"] == version + 1
    assert upd["approval_id"] != old_appr
    assert approval.get(old_appr)["voided"] == 1, "the previous approval must be voided"
    try:
        actions.execute_gated("prepare_send", old_appr, d["conv_id"], tenant["admin"])
        raise AssertionError("old approval must not execute")
    except approval.ApprovalError:
        pass
    # stale version is rejected, no state change
    r2 = post(client, "/api/ai/cards/%s/update" % card["id"],
              {"fields": {"stagger": "5"}, "version": version, "idempotency_key": "c2u2"})
    assert r2.json["ok"]
    assert any(o["kind"] == "error" and "Stale" in o["message"] for o in r2.json["outcomes"])
    # confirm with the fresh approval executes the SCHEDULE (nothing sent)
    r3 = post(client, "/api/ai/cards/%s/respond" % card["id"],
              {"response": {"action": "confirm"}, "idempotency_key": "c2c"})
    assert r3.json["ok"], r3.json
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM emails WHERE status='scheduled'").fetchone()["n"]
    c.close()
    assert n == 12


def test_stale_update_no_state_change(client, tenant, mock_llm):
    post(client, "/api/ai/agent", {"text": "switch to compact"})
    d = start_send_flow(client, mock_llm, key="c3")
    d = to_review_card(client, d, "c3")
    card = find_card(d["outcomes"], "compact_review")
    p = card["payload"]
    r = post(client, "/api/ai/cards/%s/update" % card["id"],
             {"fields": {"stagger": "7"}, "version": p["version"] - 5, "idempotency_key": "c3u1"})
    assert r.json["ok"]
    assert any(o["kind"] == "error" for o in r.json["outcomes"])
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM emails").fetchone()["n"]
    c.close()
    assert n == 0


def test_guided_compact_parity(client, tenant, mock_llm, mock_gmail):
    AT = "2026-12-20 10:00"
    # compact walk
    post(client, "/api/ai/agent", {"text": "switch to compact"})
    d = start_send_flow(client, mock_llm, key="p1")
    d = to_review_card(client, d, "p1")
    card = find_card(d["outcomes"], "compact_review")
    r = post(client, "/api/ai/cards/%s/update" % card["id"],
             {"fields": {"action": "schedule", "at": AT},
              "version": card["payload"]["version"], "idempotency_key": "p1u"})
    assert r.json["ok"], r.json
    upd = [o for o in r.json["outcomes"] if o["kind"] == "compact_update"][0]["data"]
    compact_appr = approval.get(upd["approval_id"])
    compact_render = upd["preview"]["recipients"][0]
    # guided walk, same inputs
    post(client, "/api/ai/agent", {"text": "switch to guided"})
    d = start_send_flow(client, mock_llm, key="p2")
    d = to_review_card(client, d, "p2")
    def gocard(ctype, response, k):
        nonlocal d
        c = find_card(d["outcomes"], ctype)
        r = post(client, "/api/ai/cards/%s/respond" % c["id"],
                 {"response": response, "idempotency_key": k})
        assert r.json["ok"], r.json
        d = r.json

    gocard("text_input", {"text": ""}, "p2cc")
    gocard("options", {"id": "0"}, "p2sig")
    gocard("options", {"id": "none"}, "p2att")
    gocard("options", {"id": "schedule"}, "p2timing")
    gocard("datetime_input", {"value": AT.replace(" ", "T")}, "p2at")
    gocard("number_input", {"value": "0"}, "p2stg")
    gocard("options", {"id": "none"}, "p2rem")
    review = find_card(d["outcomes"], "final_review")
    guided_appr = approval.get(review["payload"]["approval_id"])
    guided_render = review["payload"]["preview"]["recipients"][0]
    assert json.loads(compact_appr["params_json"]) == json.loads(guided_appr["params_json"]), \
        "both modes must produce identical approval params"
    assert compact_render["subject"] == guided_render["subject"]
    assert compact_render["body_html"] == guided_render["body_html"]
    assert compact_render["to"] == guided_render["to"]
    assert compact_appr["render_hash"] == guided_appr["render_hash"]


def test_midflow_switch_preserves_values(client, tenant, mock_llm):
    mock_llm.queue.append({"content": "", "tool_calls": [{
        "id": "mf1", "function": {"name": "start_flow",
                                  "arguments": json.dumps({"flow_id": "send_campaign"})}}]})
    d = post(client, "/api/ai/agent", {"text": "send emails"})
    assert d.json["ok"], d.json
    d = to_review_card(client, d.json, "mf")   # guided so far
    assert find_card(d["outcomes"], "text_input"), "guided mode shows the CC step"
    d = post(client, "/api/ai/agent", {"conv_id": d["conv_id"], "text": "switch to compact"})
    assert d.json["ok"], d.json
    card = find_card(d.json["outcomes"], "compact_review")
    assert card, "mid-flow switch must re-render the send params as one card"
    assert card["payload"]["email"]["audience"] == "12 recipient(s)", "slots must survive the switch"
    assert card["payload"]["presend"]["blocked"] is False, "mapping must survive the switch"
