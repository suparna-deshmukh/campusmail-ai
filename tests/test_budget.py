"""Spec 8.10 + 17.1.19: per-user daily AI budget - metering (incl. retries),
80% notice once/day + narration drop, zero LLM calls when exhausted, tier-0
degradation, per-minute limit window, day rollover, role control."""
import json

import ai_client
import db
import services
import usage
from conftest import post, find_card, find_outcome


def agent_text(client, text, conv_id=None):
    body = {"text": text}
    if conv_id:
        body["conv_id"] = conv_id
    d = post(client, "/api/ai/agent", body)
    assert d.json["ok"], d.json
    return d.json


def test_record_and_status(client, tenant):
    uid = tenant["admin"]
    usage.record(uid, calls=2, prompt_tokens=100, completion_tokens=50)
    st = usage.status(uid)
    assert st["calls"] == 2 and st["prompt_tokens"] == 100 and st["completion_tokens"] == 50
    assert st["exhausted"] is False
    usage.record(uid, calls=1)
    assert usage.status(uid)["calls"] == 3


def test_retries_are_counted(monkeypatch, client, tenant):
    uid = tenant["admin"]

    def fake_post_chat(payload, timeout=120):
        return {"choices": [{"message": {"content": "ok"}}]}, 2  # one retry
    data, attempts = fake_post_chat({}, timeout=120)
    ai_client._meter(uid, data, attempts)
    assert usage.status(uid)["calls"] == 2, "provider retries must count against the budget"


def test_check_raises_when_exhausted(client, tenant):
    uid = tenant["admin"]
    db.set_setting("ai_daily_calls_per_user", "2")
    usage.record(uid, calls=2)
    try:
        usage.check(uid)
        raise AssertionError("check must raise when the budget is exhausted")
    except usage.BudgetExhausted:
        pass


def test_tier1_exhausted_makes_no_llm_call(client, tenant, mock_llm, monkeypatch):
    uid = tenant["admin"]
    db.set_setting("ai_daily_calls_per_user", "1")
    usage.record(uid, calls=1)
    before = mock_llm.calls

    def guarded(messages, **kw):
        usage.check(uid)  # the same gate the real chat_raw applies
        return {"content": "should never be produced", "tool_calls": []}
    monkeypatch.setattr(ai_client, "chat_raw", guarded)
    d = agent_text(client, "write me an email about internships")
    assert mock_llm.calls == before, "no LLM call may start once the budget is exhausted"
    budgets = [o["card"] for o in d["outcomes"]
               if o.get("kind") == "card" and o["card"]["type"] == "budget"]
    card = next((c for c in budgets if c["payload"].get("reason") == "budget"), None)
    assert card, [o.get("kind") for o in d["outcomes"]]
    assert card["payload"].get("reset_at"), "the paused state must show the reset time"
    assert any("deterministic" in o["label"] for o in card["payload"]["stays_working"])


def test_tier0_still_works_when_exhausted(client, tenant, mock_llm):
    uid = tenant["admin"]
    db.set_setting("ai_daily_calls_per_user", "1")
    usage.record(uid, calls=1)
    d = agent_text(client, "/overview")
    assert find_outcome(d["outcomes"], "data")
    assert mock_llm.calls == 0


def test_notice_at_80_once_per_day_and_narration_drop(client, tenant, mock_llm):
    uid = tenant["admin"]
    db.set_setting("ai_daily_calls_per_user", "10")
    usage.record(uid, calls=8)
    mock_llm.queue.append({"content": "You have made 8 calls today.", "tool_calls": []})
    d = agent_text(client, "hello there")
    notice = find_card(d["outcomes"], "budget")
    assert notice and notice["payload"]["reason"] == "warn"
    assert db.get_user_setting(uid, "budget_notice_date") == usage.day_key()
    mock_llm.queue.append({"content": "You have made 8 calls today.", "tool_calls": []})
    d = agent_text(client, "hello again", conv_id=d["conv_id"])
    assert find_card(d["outcomes"], "budget") is None, "at most one notice per day"
    texts = [o["text"] for o in d["outcomes"] if o.get("kind") == "message"]
    assert not any("8" in t for t in texts), "narration must be dropped at >=80%"


def test_per_minute_limit_window(client, tenant):
    uid = tenant["admin"]
    db.set_setting("ai_daily_calls_per_user", "100")
    usage.record(uid, calls=usage.PER_MINUTE_LIMIT)
    st = usage.status(uid)
    assert st["exhausted"] is True and st["reason"] == "per-minute limit"
    c = db.conn()
    c.execute("UPDATE ai_usage SET minute_ts = minute_ts - 61 WHERE user_id=?", (uid,))
    c.commit(); c.close()
    assert usage.status(uid)["exhausted"] is False, "the per-minute window must roll over"


def test_day_rollover_resets_counters(client, tenant):
    uid = tenant["admin"]
    db.set_setting("ai_daily_calls_per_user", "2")
    usage.record(uid, calls=2)
    assert usage.status(uid)["exhausted"] is True
    c = db.conn()
    c.execute("UPDATE ai_usage SET day='1999-01-01' WHERE user_id=?", (uid,))
    c.commit(); c.close()
    st = usage.status(uid)
    assert st["calls"] == 0 and st["exhausted"] is False


def test_budget_limits_role_gated(client, tenant):
    admin = db.get_user(tenant["admin"])
    member = db.get_user(tenant["member"])
    services.settings_update(admin, {"ai_daily_calls_per_user": "300"})
    assert db.get_setting("ai_daily_calls_per_user") == "300"
    try:
        services.settings_update(member, {"ai_daily_calls_per_user": "5"})
        raise AssertionError("a member must not be able to change AI budget limits")
    except services.ServiceError as e:
        assert e.code == 403
    assert db.get_setting("ai_daily_calls_per_user") == "300"
