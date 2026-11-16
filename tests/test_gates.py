"""Spec 17.1.1, 5.3: the Approval Gate. R2+ never runs without a valid token;
stored params execute (client/LLM never send params); edits void tokens."""
import time

import pytest

import actions
import approval
import cards
import db
import flows
from conftest import ADMIN, MEMBER, post, get, find_card, find_outcome, walk_until_gate


def _make_conv(client):
    d = post(client, "/api/ai/agent", {"text": "/overview"})
    assert d.json["ok"]
    return d.json["conv_id"]


def test_direct_mutation_call_raises(client, tenant):
    """5.3.6: calling a mutating tool directly (no approval) MUST raise."""
    user = db.get_user(tenant["admin"])
    with pytest.raises(actions.ApprovalRequired):
        actions.read_direct("create_campaign", {"name": "Nope"}, user)
    # and nothing was created
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM campaigns").fetchone()["n"]
    c.close()
    assert n == 1


def test_gate_requires_valid_approval(client, tenant):
    conv_id = _make_conv(client)
    a = actions.get("create_campaign")
    pv = a.preview({"name": "X"}, db.get_user(tenant["admin"]))
    # no approval at all -> error
    with pytest.raises(approval.ApprovalError):
        actions.execute_gated("create_campaign", "appr_nonexistent", conv_id, tenant["admin"])


def test_approval_single_use(client, tenant):
    ap = approval.mint("create_campaign", {"name": "Once"}, "conv1", tenant["admin"])
    res, _ = actions.execute_gated("create_campaign", ap["approval_id"], "conv1", tenant["admin"])
    assert res.get("campaign_id")
    # second use of the same token
    with pytest.raises(approval.ApprovalError):
        actions.execute_gated("create_campaign", ap["approval_id"], "conv1", tenant["admin"])


def test_approval_wrong_user_and_conv(client, tenant):
    ap = approval.mint("create_campaign", {"name": "Y"}, "convA", tenant["admin"])
    with pytest.raises(approval.ApprovalError):
        actions.execute_gated("create_campaign", ap["approval_id"], "convA", tenant["member"])
    with pytest.raises(approval.ApprovalError):
        actions.execute_gated("create_campaign", ap["approval_id"], "convB", tenant["admin"])


def test_approval_expired(client, tenant):
    ap = approval.mint("create_campaign", {"name": "Old"}, "conv1", tenant["admin"])
    c = db.conn()
    c.execute("UPDATE approvals SET expires_at=? WHERE approval_id=?", (time.time() - 10, ap["approval_id"]))
    c.commit()
    c.close()
    with pytest.raises(approval.ApprovalError):
        actions.execute_gated("create_campaign", ap["approval_id"], "conv1", tenant["admin"])


def test_edit_voids_previous_approval(client, tenant):
    """5.3.4: after an edit creates a new approval, the old one is void."""
    ap = approval.mint("create_campaign", {"name": "A"}, "conv1", tenant["admin"])
    approval.void(ap["approval_id"])
    with pytest.raises(approval.ApprovalError):
        actions.execute_gated("create_campaign", ap["approval_id"], "conv1", tenant["admin"])


def test_confirm_card_execute_stored_params(client, tenant):
    """Flow: create campaign via cards. The confirm click sends only
    {action: confirm} - the server executes the stored params."""
    d = post(client, "/api/ai/agent", {"text": "create campaign Winter 2027"})
    assert d.json["ok"]
    card, _ = walk_until_gate(client, d.json["outcomes"], d.json["conv_id"])
    assert card, "expected a confirm card"
    assert "Winter 2027" in json_title(card)
    d2 = post(client, "/api/ai/cards/%s/respond" % card["id"],
              {"response": {"action": "confirm"}, "idempotency_key": "k1"})
    assert d2.json["ok"]
    c = db.conn()
    row = c.execute("SELECT name FROM campaigns WHERE name=?", ("Winter 2027",)).fetchone()
    c.close()
    assert row, "stored params must have been executed"


def json_title(card):
    return card["payload"].get("title", "")


def test_double_submit_idempotent(client, tenant):
    """C14: same card + idempotency key executes once."""
    d = post(client, "/api/ai/agent", {"text": "create campaign Dup"})
    card, _ = walk_until_gate(client, d.json["outcomes"], d.json["conv_id"])
    d2 = post(client, "/api/ai/cards/%s/respond" % card["id"],
              {"response": {"action": "confirm"}, "idempotency_key": "dupk"})
    d3 = post(client, "/api/ai/cards/%s/respond" % card["id"],
              {"response": {"action": "confirm"}, "idempotency_key": "dupk"})
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM campaigns WHERE name='Dup'").fetchone()["n"]
    c.close()
    assert n == 1
    assert d2.json["outcomes"] == d3.json["outcomes"]


def test_cancel_does_not_execute(client, tenant):
    d = post(client, "/api/ai/agent", {"text": "create campaign Cancelled"})
    card, _ = walk_until_gate(client, d.json["outcomes"], d.json["conv_id"])
    post(client, "/api/ai/cards/%s/respond" % card["id"],
         {"response": {"action": "cancel"}, "idempotency_key": "ck"})
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM campaigns WHERE name='Cancelled'").fetchone()["n"]
    c.close()
    assert n == 0


def test_typing_supersedes_pending_card(client, tenant):
    """C9: a new message while a card is pending supersedes it; nothing executes."""
    d = post(client, "/api/ai/agent", {"text": "create campaign Sup"})
    card, _ = walk_until_gate(client, d.json["outcomes"], d.json["conv_id"])
    assert card
    d2 = post(client, "/api/ai/agent", {"text": "/overview", "conv_id": d.json["conv_id"]})
    assert d2.json["ok"]
    c = db.conn()
    row = c.execute("SELECT status FROM ask_ai_cards WHERE card_id=?", (card["id"],)).fetchone()
    n = c.execute("SELECT COUNT(*) n FROM campaigns WHERE name='Sup'").fetchone()["n"]
    c.close()
    assert row["status"] == "superseded"
    assert n == 0
    # and the superseded card can no longer be answered
    d3 = post(client, "/api/ai/cards/%s/respond" % card["id"],
              {"response": {"action": "confirm"}, "idempotency_key": "x"})
    assert not d3.json["ok"]


def test_cancel_word_cancels_pending(client, tenant):
    d = post(client, "/api/ai/agent", {"text": "create campaign Cw"})
    card, _ = walk_until_gate(client, d.json["outcomes"], d.json["conv_id"])
    d2 = post(client, "/api/ai/agent", {"text": "cancel", "conv_id": d.json["conv_id"]})
    assert d2.json["ok"]
    c = db.conn()
    row = c.execute("SELECT status FROM ask_ai_cards WHERE card_id=?", (card["id"],)).fetchone()
    n = c.execute("SELECT COUNT(*) n FROM campaigns WHERE name='Cw'").fetchone()["n"]
    c.close()
    assert row["status"] == "cancelled"
    assert n == 0
