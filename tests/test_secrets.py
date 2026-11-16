"""Spec 6.2 + 13 + 17.1.9: the API key travels only through secret_input and
never enters LLM context, conversation state, cards, activity or responses."""
import json

import db
from conftest import post, find_card, find_outcome


SECRET = "sk-SUPERSECRET123"


def test_ai_key_secret_hygiene(client, tenant, mock_llm):
    # one routing call starts the settings flow on the AI section
    mock_llm.queue.append({"content": "", "tool_calls": [{
        "id": "s1", "function": {"name": "start_flow",
                                 "arguments": json.dumps({"flow_id": "settings",
                                                          "slots": {"which": "ai"}})}}]})
    seen = []

    def hit(url, body):
        d = post(client, url, body)
        seen.append(d.get_data(as_text=True))
        return d

    d = hit("/api/ai/agent", {"text": "change the AI settings"})
    assert d.json["ok"], d.json
    edit = find_card(d.json["outcomes"], "table_edit")
    assert edit and edit["step_id"] == "ai_meta"
    d = hit("/api/ai/cards/%s/respond" % edit["id"],
            {"response": {"fields": [
                {"field": "Base URL", "value": "http://mock"},
                {"field": "Model", "value": "mock-model"},
                {"field": "Enabled", "value": "1"}]},
             "idempotency_key": "sk1"})
    assert d.json["ok"], d.json
    sec = find_card(d.json["outcomes"], "secret_input")
    assert sec and sec["step_id"] == "ai_key"
    d = hit("/api/ai/cards/%s/respond" % sec["id"],
            {"response": {"value": SECRET}, "idempotency_key": "sk2"})
    assert d.json["ok"], d.json
    gate = find_card(d.json["outcomes"], "confirm")
    assert gate and gate["payload"]["action"] == "set_ai_settings"
    details = json.dumps(gate["payload"].get("details") or [])
    assert SECRET not in details, "the key must never be echoed in the preview"
    assert "Key set" in details
    d = hit("/api/ai/cards/%s/respond" % gate["id"],
            {"response": {"action": "confirm"}, "idempotency_key": "sk3"})
    assert d.json["ok"], d.json

    assert db.get_setting("ai_api_key") == SECRET, "the key must be stored server-side"

    c = db.conn()
    convs = [r["state_json"] or "" for r in c.execute("SELECT state_json FROM ask_ai_convs")]
    cards = [(r["payload_json"] or "") + (r["result_json"] or "") for r in c.execute("SELECT * FROM ask_ai_cards")]
    acts = [json.dumps(dict(r), default=str) for r in c.execute("SELECT * FROM activity")]
    appr = [(r["params_json"] or "") + (r["preview_hash"] or "") for r in c.execute("SELECT * FROM approvals")]
    c.close()
    assert all(SECRET not in x for x in convs), "secret must not persist in conversation state"
    assert all(SECRET not in x for x in cards), "secret must not persist in card payloads/results"
    assert all(SECRET not in x for x in acts), "secret must not enter the activity log"
    for body in seen:
        assert SECRET not in body, "secret must never be echoed back in a response"
    # the one LLM call (routing) happened before the secret was typed
    assert mock_llm.calls == 1
    assert SECRET not in json.dumps(mock_llm.last_messages, default=str)
    # the stored key is the only copy of it (approvals may hold it transiently by design of 5.3)
    assert any(SECRET in x for x in appr)
