"""Spec 5.3.5 + 17.1.5/17.1.16: the executor re-renders and compares the
render hash; a changed template or changed audience aborts with no write."""
import json

import db
import services
from conftest import post, find_card, find_outcome, walk_send_review, ADMIN


def confirm(client, card, key):
    d = post(client, "/api/ai/cards/%s/respond" % card["id"],
             {"response": {"action": "confirm"}, "idempotency_key": key})
    return d.json


def batches_count():
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM batches").fetchone()["n"]
    c.close()
    return n


def test_control_confirm_executes(client, tenant, mock_llm, mock_gmail):
    rev = walk_send_review(client, mock_llm, "queue", key="ri1")
    d = confirm(client, rev, "ri1c")
    assert d["ok"], d
    assert batches_count() == 1


def test_template_change_between_review_and_execute_aborts(client, tenant, mock_llm, mock_gmail):
    rev = walk_send_review(client, mock_llm, "queue", key="ri2")
    before = batches_count()
    admin = db.get_user(tenant["admin"])
    services.template_update(1, body_html="<p>CHANGED {company}</p>", actor=admin)
    d = confirm(client, rev, "ri2c")
    assert not d["ok"], d
    assert "preview" in d.get("error", "").lower() or "changed" in d.get("error", "").lower(), d
    assert batches_count() == before, "no batch may be created on a changed preview"
    assert mock_gmail.sends == []


def test_audience_count_change_aborts(client, tenant, mock_llm, mock_gmail):
    rev = walk_send_review(client, mock_llm, "queue", key="ri3")
    assert rev["payload"]["count"] == 12
    before = batches_count()
    c = db.conn()
    c.execute("DELETE FROM contacts WHERE serial IN ('10','11','12')")
    c.commit(); c.close()
    d = confirm(client, rev, "ri3c")
    assert not d["ok"], d
    assert "changed" in d.get("error", "").lower(), d
    assert batches_count() == before
    assert mock_gmail.sends == []
