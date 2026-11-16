"""Spec 17.1.4/7 + 7.3-D: safety rails parity - safe mode, daily cap with
remainder options, schedule window, CC limit, role matrix."""
import json
from datetime import datetime, timedelta

import pytest

import actions
import app as appmod
import cards
import db
import presend
import services
from conftest import ADMIN, MEMBER, post, find_card


def pj(client, url, body, email=ADMIN):
    d = post(client, url, body, email=email)
    return d.json


def respondj(client, card, response, key="r", email=ADMIN):
    return pj(client, "/api/ai/cards/%s/respond" % card["id"],
              {"response": response, "idempotency_key": key + card["id"]}, email=email)


def card(outcomes, ctype=None):
    for o in outcomes or []:
        if o.get("kind") == "card" and (ctype is None or o["card"]["type"] == ctype):
            return o["card"]
    return None


def mock_start_flow(mock_llm, flow_id, slots=None):
    mock_llm.queue.append({"content": "", "tool_calls": [{
        "id": "call_1", "function": {"name": "start_flow",
                                     "arguments": json.dumps({"flow_id": flow_id, "slots": slots or {}})}}]})


def merge_params():
    return {"template_id": 1, "sheet_id": 1, "campaign_id": 1, "account_id": 1,
            "mapping": {"company": "column:company", "hr_name": "column:hr_name"}}


def walk_to_timing(client, mock_llm, email=ADMIN):
    """Walk send flow to the timing question. Returns the current outcomes dict."""
    mock_start_flow(mock_llm, "send_campaign")
    d = pj(client, "/api/ai/agent", {"text": "send emails"}, email=email)
    assert d["ok"], d
    for resp, key in (({"id": "1"}, "a"), ({"id": "1"}, "b"), ({"id": "all"}, "c"), ({"id": "1"}, "d")):
        d = respondj(client, card(d["outcomes"], "options"), resp, key, email=email)
    m = card(d["outcomes"], "table_edit")
    assert m and m["step_id"] == "mapping", [o for o in d["outcomes"] if o.get("kind") == "card"]
    cols = m["payload"]["columns"]
    d = respondj(client, m, {"fields": [{"field": f["field"],
                                         "value": f["field"] if f["field"] in cols else cols[1]}
                                        for f in m["payload"]["fields"]]}, "e", email=email)
    d = respondj(client, card(d["outcomes"], "text_input"), {"text": ""}, "f", email=email)
    d = respondj(client, card(d["outcomes"], "options"), {"id": "0"}, "g", email=email)
    d = respondj(client, card(d["outcomes"], "options"), {"id": "none"}, "h", email=email)
    return d


# ---------------------------------------------------------------- safe mode
def test_safe_mode_presend_and_service_block(client, tenant, mock_llm, mock_gmail):
    db.set_setting("safe_mode", "1")
    rows = services.audience_rows(1, None, None, None)
    out = presend.run(dict(merge_params(), action="send", _rows=rows))
    assert any(l["id"] == "safe_mode" and l["severity"] == "block" for l in out["lines"])
    with pytest.raises(services.ServiceError):
        services.merge(dict(merge_params(), action="send"), db.get_user(tenant["admin"]))
    assert mock_gmail.sends == []


def test_safe_mode_flow_offers_alternatives_admin(client, tenant, mock_llm, mock_gmail):
    db.set_setting("safe_mode", "1")
    d = walk_to_timing(client, mock_llm)
    d = respondj(client, card(d["outcomes"], "options"), {"id": "send"}, "t")
    d = respondj(client, card(d["outcomes"], "number_input"), {"value": "0"}, "u")
    d = respondj(client, card(d["outcomes"], "options"), {"id": "none"}, "v")
    c = card(d["outcomes"], "options")
    assert c and c["step_id"] == "safe_mode_choice", [o for o in d["outcomes"] if o.get("kind") == "card"]
    labels = [o["label"] for o in c["payload"]["options"]]
    assert any("Schedule" in l for l in labels)
    assert any("draft" in l.lower() for l in labels)
    assert any("Queue" in l for l in labels)
    assert any("live sending" in l for l in labels), "admin gets the R5 live-sending option"
    d = respondj(client, c, {"id": "schedule"}, "w")
    assert card(d["outcomes"], "datetime_input"), "switching to schedule asks for a time"
    assert mock_gmail.sends == []


def test_safe_mode_member_has_no_live_option(client, tenant, mock_llm, mock_gmail):
    db.set_setting("safe_mode", "1")
    d = walk_to_timing(client, mock_llm, email=MEMBER)
    d = respondj(client, card(d["outcomes"], "options"), {"id": "send"}, "t", email=MEMBER)
    d = respondj(client, card(d["outcomes"], "number_input"), {"value": "0"}, "u", email=MEMBER)
    d = respondj(client, card(d["outcomes"], "options"), {"id": "none"}, "v", email=MEMBER)
    c = card(d["outcomes"], "options")
    assert c and c["step_id"] == "safe_mode_choice"
    labels = [o["label"] for o in c["payload"]["options"]]
    assert not any("live" in l.lower() for l in labels), "members must not be offered the R5 toggle"
    msgs = [o["text"] for o in d["outcomes"] if o.get("kind") == "message"]
    assert any("admin" in t.lower() for t in msgs)
    assert mock_gmail.sends == []


def test_safe_mode_gate_guard_server_side(client, tenant, mock_gmail):
    """Even a force-confirmed send gate with a blocking line never executes."""
    import approval
    db.set_setting("safe_mode", "1")
    conv = {"_conv_id": "convX", "uid": str(tenant["admin"]), "flow": None, "messages": []}
    params = dict(merge_params(), action="send", internal_hold_seconds=10)
    ap = approval.mint("prepare_send", params, "convX", tenant["admin"])
    payload = {"title": "Send 12 emails now", "risk": "R4", "action": "prepare_send",
               "count": 12, "approval_id": ap["approval_id"]}
    env = cards.new_card(conv, "final_review", payload, step_id="final_review", ttl=600)
    with pytest.raises(cards.CardError):
        cards._respond_gate({"id": env["id"], "payload": payload, "type": "final_review"},
                            conv, {"action": "confirm"})
    assert mock_gmail.sends == []


# ---------------------------------------------------------------- cap remainder
def test_cap_shortfall_partial_sends_only_remainder(client, tenant, mock_llm, mock_gmail):
    db.set_setting("max_sends_per_day", "5")
    db.set_setting("send_undo_seconds", "10")
    d = walk_to_timing(client, mock_llm)
    d = respondj(client, card(d["outcomes"], "options"), {"id": "send"}, "t")
    d = respondj(client, card(d["outcomes"], "number_input"), {"value": "0"}, "u")
    d = respondj(client, card(d["outcomes"], "options"), {"id": "none"}, "v")
    c = card(d["outcomes"], "options")
    assert c and c["step_id"] == "cap_choice", [o for o in d["outcomes"] if o.get("kind") == "card"]
    assert "5 of the 12" in c["payload"]["question"]
    labels = [o["label"] for o in c["payload"]["options"]]
    assert any(l.startswith("Send first 5 now") for l in labels)
    assert any("Schedule" in l for l in labels)
    d = respondj(client, c, {"id": "partial"}, "w")
    review = card(d["outcomes"], "final_review")
    assert review, "after the remainder choice the review is unblocked"
    assert review["payload"]["presend"]["blocked"] is False
    cap_lines = [l for l in review["payload"]["presend"]["lines"] if l["id"] == "cap"]
    assert cap_lines and cap_lines[0]["severity"] == "warn"
    d = respondj(client, review, {"action": "confirm"}, "x")
    assert d["ok"], d
    cdb = db.conn()
    held = cdb.execute("SELECT COUNT(*) n FROM emails WHERE status='scheduled'").fetchone()["n"]
    cdb.close()
    assert held == 12
    assert mock_gmail.sends == [], "nothing reaches Gmail during the undo hold"
    # release the hold, backdate the rows, run the tick
    cdb = db.conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cdb.execute("UPDATE undo_holds SET release_at=?", (now,))
    cdb.execute("UPDATE emails SET scheduled_at=datetime('now','-1 minute') WHERE status='scheduled'")
    cdb.commit(); cdb.close()
    appmod.scheduler_tick()
    assert len(mock_gmail.sends) == 5, "cap must stop the tick at the remainder"
    cdb = db.conn()
    left = cdb.execute("SELECT COUNT(*) n FROM emails WHERE status='scheduled'").fetchone()["n"]
    cdb.close()
    assert left == 7, "the rest stays queued for another day"


def test_cap_shortfall_switch_to_schedule(client, tenant, mock_llm):
    db.set_setting("max_sends_per_day", "5")
    d = walk_to_timing(client, mock_llm)
    d = respondj(client, card(d["outcomes"], "options"), {"id": "send"}, "t")
    d = respondj(client, card(d["outcomes"], "number_input"), {"value": "0"}, "u")
    d = respondj(client, card(d["outcomes"], "options"), {"id": "none"}, "v")
    c = card(d["outcomes"], "options")
    assert c and c["step_id"] == "cap_choice"
    d = respondj(client, c, {"id": "schedule"}, "w")
    assert card(d["outcomes"], "datetime_input")


# ---------------------------------------------------------------- schedule window
def test_schedule_window_bounds(client, tenant, mock_llm):
    now = datetime.now()  # naive local - matches the card's iso strings
    services.parse_when((now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"))
    with pytest.raises(services.ServiceError):
        services.parse_when((now + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M"))
    with pytest.raises(services.ServiceError):
        services.parse_when((now + timedelta(days=46)).strftime("%Y-%m-%d %H:%M"))
    d = walk_to_timing(client, mock_llm)
    d = respondj(client, card(d["outcomes"], "options"), {"id": "schedule"}, "s")
    dt = card(d["outcomes"], "datetime_input")
    assert dt and dt["step_id"] == "schedule_input"
    lo = datetime.fromisoformat(dt["payload"]["min_iso"])
    hi = datetime.fromisoformat(dt["payload"]["max_iso"])
    assert timedelta(minutes=4) < (lo - now) < timedelta(minutes=7)
    assert timedelta(days=44, hours=23) < (hi - now) < timedelta(days=45, hours=1)
    d = respondj(client, dt, {"value": (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")}, "t")
    assert not d["ok"], "out-of-window times are rejected inline"


# ---------------------------------------------------------------- CC limit
def test_cc_over_20_rejected(client, tenant, mock_llm):
    cc21 = ",".join("u%d@example.com" % i for i in range(21))
    rows = services.audience_rows(1, None, None, None)
    with pytest.raises(services.ServiceError):
        services.merge(dict(merge_params(), action="queue", cc=cc21, _rows=rows),
                       db.get_user(tenant["admin"]))
    out = presend.run(dict(merge_params(), action="queue", cc=cc21, _rows=rows))
    assert any(l["id"] == "cc_many" and l["severity"] == "block" for l in out["lines"])
    d = walk_to_timing(client, mock_llm)
    # cc step happens before timing; re-walk to it is not possible, so drive directly:
    mock_start_flow(mock_llm, "send_campaign")
    d = pj(client, "/api/ai/agent", {"text": "send emails"})
    for resp, key in (({"id": "1"}, "a"), ({"id": "1"}, "b"), ({"id": "all"}, "c"), ({"id": "1"}, "d")):
        d = respondj(client, card(d["outcomes"], "options"), resp, key)
    m = card(d["outcomes"], "table_edit")
    cols = m["payload"]["columns"]
    d = respondj(client, m, {"fields": [{"field": f["field"],
                                         "value": f["field"] if f["field"] in cols else cols[1]}
                                        for f in m["payload"]["fields"]]}, "e")
    d = respondj(client, card(d["outcomes"], "text_input"), {"text": cc21}, "f")
    assert not d["ok"], "the flow's cc step rejects >20 inline"


# ---------------------------------------------------------------- role matrix
def test_llm_tools_filtered_by_role(client, tenant):
    admin = db.get_user(tenant["admin"])
    member = db.get_user(tenant["member"])
    admin_names = {t["function"]["name"] for t in actions.llm_tools(admin)}
    member_names = {t["function"]["name"] for t in actions.llm_tools(member)}
    assert "list_join_requests" in admin_names, "admin-only read offered to admins"
    assert "list_join_requests" not in member_names, "admin-only read hidden from members"
    assert "invite_member" not in admin_names, "mutating actions are flows, never raw tools"
    assert "set_ai_settings" not in member_names
    assert "start_flow" in admin_names and "start_flow" in member_names
    assert "get_overview" in member_names, "reads stay available"


def test_member_settings_flow_only_safe_mode(client, tenant, mock_llm):
    mock_start_flow(mock_llm, "settings")
    d = pj(client, "/api/ai/agent", {"text": "change settings"}, email=MEMBER)
    c = card(d["outcomes"], "options")
    assert c
    assert [o["id"] for o in c["payload"]["options"]] == ["safe_mode"]
    assert any("Only an admin" in o.get("text", "") for o in d["outcomes"] if o.get("kind") == "message")


def test_demoted_user_cannot_execute(client, tenant):
    import approval
    ap = approval.mint("set_live_sending", {"live": "0"}, "convZ", tenant["admin"])
    c = db.conn()
    c.execute("UPDATE users SET role='member' WHERE id=?", (tenant["admin"],))
    c.commit(); c.close()
    with pytest.raises(approval.ApprovalError):
        actions.execute_gated("set_live_sending", ap["approval_id"], "convZ", tenant["admin"])
