"""Spec 7.8 + 17.1.16: bulk-send guard. Server-verified acknowledgement,
exemptions for queue/draft, typed count at 4x threshold, range validation."""
import json

import db
import services
from conftest import post, find_card, find_outcome, walk_send_review


def confirm(client, card, ack=None, key="cfm"):
    resp = {"action": "confirm"}
    if ack:
        resp["ack"] = ack
    d = post(client, "/api/ai/cards/%s/respond" % card["id"],
             {"response": resp, "idempotency_key": key})
    return d.json


def emails_count():
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM emails").fetchone()["n"]
    c.close()
    return n


def test_below_threshold_no_ack(client, tenant, mock_llm, mock_gmail):
    db.set_setting("bulk_confirm_threshold", "13")  # 12 < 13
    rev = walk_send_review(client, mock_llm, "queue", key="bg1")
    assert rev["payload"]["bulk"] == {"needed": False}, rev["payload"]["bulk"]
    d = confirm(client, rev, key="bg1c")
    assert d["ok"], d
    assert emails_count() == 12
    assert mock_gmail.sends == []


def test_at_threshold_checkbox_required(client, tenant, mock_llm, mock_gmail):
    db.set_setting("bulk_confirm_threshold", "12")  # 12 >= 12, < 48
    db.set_setting("send_undo_seconds", "10")
    rev = walk_send_review(client, mock_llm, "send", key="bg2")
    bulk = rev["payload"]["bulk"]
    assert bulk["needed"] is True and bulk.get("typed") is False, bulk
    before = emails_count()
    d = confirm(client, rev, key="bg2a")
    assert not d["ok"] and d.get("code") == 422, d
    assert emails_count() == before, "nothing may execute without the acknowledgement"
    d = confirm(client, rev, ack={"reviewed": True}, key="bg2b")
    assert d["ok"], d
    assert emails_count() == before + 12
    assert mock_gmail.sends == [], "held sends must not hit Gmail"


def test_at_4x_threshold_typed_count_must_match(client, tenant, mock_llm, mock_gmail):
    db.set_setting("bulk_confirm_threshold", "3")  # 4 * 3 = 12
    db.set_setting("send_undo_seconds", "10")
    rev = walk_send_review(client, mock_llm, "send", key="bg3")
    assert rev["payload"]["bulk"]["typed"] is True, rev["payload"]["bulk"]
    before = emails_count()
    d = confirm(client, rev, ack={"reviewed": True}, key="bg3a")
    assert not d["ok"] and d.get("code") == 422, "typed count required at 4x threshold"
    d = confirm(client, rev, ack={"reviewed": True, "typed_count": "11"}, key="bg3b")
    assert not d["ok"] and d.get("code") == 422, "wrong typed count must be rejected"
    assert emails_count() == before
    d = confirm(client, rev, ack={"reviewed": True, "typed_count": "12"}, key="bg3c")
    assert d["ok"], d
    assert emails_count() == before + 12


def test_queue_and_draft_exempt(client, tenant, mock_llm, mock_gmail):
    db.set_setting("bulk_confirm_threshold", "10")  # 12 >= 10: would need an ack for sends
    rev = walk_send_review(client, mock_llm, "queue", key="bg4")
    assert rev["payload"]["bulk"] == {"needed": False}, "queue must be exempt"
    d = confirm(client, rev, key="bg4c")
    assert d["ok"], d
    rev2 = walk_send_review(client, mock_llm, "draft", key="bg5")
    assert rev2["payload"]["bulk"] == {"needed": False}, "drafts must be exempt"
    d = confirm(client, rev2, key="bg5c")
    assert d["ok"], d
    assert len(mock_gmail.drafts) == 12
    assert mock_gmail.sends == []


def test_threshold_out_of_range_rejected(client, tenant):
    admin = db.get_user(tenant["admin"])
    before = db.get_setting("bulk_confirm_threshold")
    for bad in ("5", "501", "0", "abc"):
        try:
            services.settings_update(admin, {"bulk_confirm_threshold": bad})
            raise AssertionError("bulk threshold %r accepted" % bad)
        except services.ServiceError:
            pass
    assert db.get_setting("bulk_confirm_threshold") == before
