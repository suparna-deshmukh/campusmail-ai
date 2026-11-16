"""Spec 17.1.2 + 8.7: chip/card-driven flows use ZERO LLM calls (at most the
single structured routing call when the request arrives as free text).
Every flow walks real cards through the agent protocol."""
import json

import pytest

import db
from conftest import ADMIN, MEMBER, post, get, b64, find_card, find_outcome


def mock_start_flow(mock_llm, flow_id, slots=None):
    mock_llm.queue.append({
        "content": "",
        "tool_calls": [{"id": "call_1", "function": {
            "name": "start_flow",
            "arguments": json.dumps({"flow_id": flow_id, "slots": slots or {}})}}],
    })


def respond(client, card, response):
    d = post(client, "/api/ai/cards/%s/respond" % card["id"],
             {"response": response, "idempotency_key": "t-" + card["id"] + "-" + str(len(card["id"]))})
    assert d.json["ok"], d.json
    return d.json


def activity_rows():
    c = db.conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM activity").fetchall()]
    c.close()
    return rows


# ---------------------------------------------------------------- tier-0 reads
def test_tier0_reads_zero_llm(client, mock_llm):
    d = post(client, "/api/ai/agent", {"text": "/overview"})
    assert d.json["ok"]
    assert find_outcome(d.json["outcomes"], "data"), "overview must render a data card"
    d = post(client, "/api/ai/agent", {"text": "what's my daily cap left?"})
    assert d.json["ok"]
    assert find_outcome(d.json["outcomes"], "data")
    assert mock_llm.calls == 0, "tier-0 catalog reads must not call the LLM"


# ---------------------------------------------------------------- create campaign
def test_create_campaign_zero_llm(client, tenant, mock_llm):
    d = post(client, "/api/ai/agent", {"text": "create campaign Winter 2027"})
    assert d.json["ok"]
    conv = d.json["conv_id"]
    # desc choice (tier-0 start fills the name slot)
    card = find_card(d.json["outcomes"], "options")
    assert card and card["step_id"] == "desc_choice"
    d2 = respond(client, card, {"id": "skip"})
    gate = find_card(d2["outcomes"], "confirm")
    assert gate, "expected confirm gate"
    assert "Winter 2027" in gate["payload"]["title"]
    assert gate["payload"]["risk"] == "R2"
    d3 = respond(client, gate, {"action": "confirm"})
    res = find_outcome(d3["outcomes"], "result")
    assert res and "Winter 2027" in res["data"]["title"]
    c = db.conn()
    row = c.execute("SELECT * FROM campaigns WHERE name='Winter 2027'").fetchone()
    c.close()
    assert row
    assert mock_llm.calls == 0
    assert any("via Ask AI" in (a.get("actor") or "") for a in activity_rows())


# ---------------------------------------------------------------- delete campaign
def test_delete_campaign_zero_llm(client, tenant, mock_llm):
    d = post(client, "/api/ai/agent", {"text": "delete campaign Diwali"})
    assert d.json["ok"]
    card = find_card(d.json["outcomes"], "confirm")
    assert card, "delete must go straight to the impact confirm"
    p = card["payload"]
    assert p["risk"] == "R3"
    impact = " ".join(p.get("impact") or []) + " ".join(p.get("notes") or [])
    assert "gmail" in impact.lower(), "R3 delete must say Gmail copies are untouched"
    respond(client, card, {"action": "confirm"})
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM campaigns WHERE name='Diwali'").fetchone()["n"]
    c.close()
    assert n == 0
    assert mock_llm.calls == 0


# ---------------------------------------------------------------- import list (file in, 0 LLM)
CSV = b"serial,company,hr_name,hr_email\n1,Acme,Alice,alice@acme.com\n2,Beta,Bob,bob@beta.com\n3,Beta,Carol,\n4,Gamma,Alice,alice@acme.com\n"


def test_import_list_zero_llm(client, tenant, mock_llm):
    d = post(client, "/api/ai/agent", {"file": b64("recruiters.csv", CSV)})
    assert d.json["ok"]
    conv = d.json["conv_id"]
    # summary data card + "Import this list?" options
    assert find_outcome(d.json["outcomes"], "data")
    card = find_card(d.json["outcomes"], "options")
    assert card and card["step_id"] == "confirm"
    d2 = respond(client, card, {"id": "import"})
    link = find_card(d2["outcomes"], "options")
    assert link and link["step_id"] == "link"
    d3 = respond(client, link, {"id": "1"})  # link to Diwali
    gate = find_card(d3["outcomes"], "confirm")
    assert gate
    assert gate["payload"]["action"] == "import_contacts_sheet"
    detail = {x["k"]: x["v"] for x in gate["payload"]["details"]}
    assert detail["Rows"] == 4 and detail["Without email"] == 1 and detail["Duplicates"] == 1
    d4 = respond(client, gate, {"action": "confirm"})
    res = find_outcome(d4["outcomes"], "result")
    assert res and res["data"]["data"]["rows"] == 4
    c = db.conn()
    row = c.execute("SELECT * FROM sheets WHERE campaign_id=1 AND name LIKE 'recruiters%'").fetchone()
    c.close()
    assert row, "imported sheet must be linked to the campaign"
    assert mock_llm.calls == 0


# ---------------------------------------------------------------- template from file
TPL_TXT = b"Hi {hr_name},\n\nWe are hiring at {company}.\nPlease reply if interested.\n"


def test_template_from_file_zero_llm(client, tenant, mock_llm):
    d = post(client, "/api/ai/agent", {"file": b64("invite.txt", TPL_TXT)})
    assert d.json["ok"]
    card = find_card(d.json["outcomes"], "draft_editor")
    assert card and card["payload"]["kind"] == "template"
    assert set(card["payload"]["fields"]) == {"hr_name", "company"}
    d2 = respond(client, card, {"action": "continue", "subject": "Campus drive at {company}"})
    name_card = find_card(d2["outcomes"], "text_input")
    assert name_card and name_card["step_id"] == "name"
    d3 = respond(client, name_card, {"text": "File Template"})
    act = find_card(d3["outcomes"], "options")
    assert act and act["step_id"] == "active_choice"
    d4 = respond(client, act, {"id": "no"})
    gate = find_card(d4["outcomes"], "confirm")
    assert gate and gate["payload"]["action"] == "create_template"
    respond(client, gate, {"action": "confirm"})
    c = db.conn()
    row = c.execute("SELECT * FROM email_templates WHERE name='File Template'").fetchone()
    c.close()
    assert row and not row["is_active"]
    assert mock_llm.calls == 0


# ---------------------------------------------------------------- send queue (routing call + 0 flow calls)
def _respond_card(client, outcomes, ctype, response):
    card = find_card(outcomes, ctype)
    assert card, "expected a %s card" % ctype
    return respond(client, card, response)


def _queue_batch(client, mock_llm):
    """Walk send_campaign to a queued batch. Returns (conv_id, llm_calls)."""
    mock_start_flow(mock_llm, "send_campaign")
    d = post(client, "/api/ai/agent", {"text": "send emails"})
    assert d.json["ok"]
    conv = d.json["conv_id"]
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "options")["id"],
             {"response": {"id": "1"}, "idempotency_key": "qc1"})
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "options")["id"],
             {"response": {"id": "1"}, "idempotency_key": "qc2"})
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "options")["id"],
             {"response": {"id": "all"}, "idempotency_key": "qc3"})
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "options")["id"],
             {"response": {"id": "1"}, "idempotency_key": "qc4"})  # template
    map_card = find_card(d.json["outcomes"], "table_edit")
    assert map_card and map_card["step_id"] == "mapping"
    cols = map_card["payload"].get("columns") or []
    fields = map_card["payload"]["fields"]
    resp = {"fields": [{"field": f["field"],
                        "value": f["field"] if f["field"] in cols else cols[1]}
                       for f in fields]}
    d = post(client, "/api/ai/cards/%s/respond" % map_card["id"],
             {"response": resp, "idempotency_key": "qc5"})
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "text_input")["id"],
             {"response": {"text": ""}, "idempotency_key": "qc6"})  # no CC
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "options")["id"],
             {"response": {"id": "0"}, "idempotency_key": "qc7"})  # no signature
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "options")["id"],
             {"response": {"id": "none"}, "idempotency_key": "qc8"})  # attachments
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "options")["id"],
             {"response": {"id": "queue"}, "idempotency_key": "qc9"})  # timing
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "options")["id"],
             {"response": {"id": "none"}, "idempotency_key": "qc11"})  # no reminders
    review = find_card(d.json["outcomes"], "final_review")
    assert review, "expected final review, got: %s" % [c["card"]["type"] for c in d.json["outcomes"] if c.get("kind") == "card"]
    assert "Queue 12 emails" in review["payload"]["title"]
    assert review["payload"]["presend"]["passed"] is True
    d = post(client, "/api/ai/cards/%s/respond" % review["id"],
             {"response": {"action": "confirm"}, "idempotency_key": "qc10"})
    assert d.json["ok"]
    res = find_outcome(d.json["outcomes"], "result")
    assert res
    return conv, mock_llm.calls


def test_send_campaign_queue_flow(client, tenant, mock_llm, mock_gmail):
    conv, calls = _queue_batch(client, mock_llm)
    assert calls == 1, "only the single routing call may use the LLM"
    c = db.conn()
    rows = c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status").fetchall()
    c.close()
    assert {"status": "pending", "n": 12} in [dict(r) for r in rows]
    assert mock_gmail.sends == [] and mock_gmail.drafts == []
    acts = activity_rows()
    assert any(a["action"] == "ask_ai_prepare_send" and "via Ask AI" in (a.get("actor") or "") for a in acts)


def test_cancel_sends_flow(client, tenant, mock_llm, mock_gmail):
    _queue_batch(client, mock_llm)
    mock_start_flow(mock_llm, "cancel_sends")
    d = post(client, "/api/ai/agent", {"text": "cancel sends"})
    assert d.json["ok"]
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "options")["id"],
             {"response": {"id": "campaign"}, "idempotency_key": "cs1"})
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "options")["id"],
             {"response": {"id": "1"}, "idempotency_key": "cs2"})
    gate = find_card(d.json["outcomes"], "confirm")
    assert gate and gate["payload"]["risk"] == "R3"
    assert "sent" in " ".join(gate["payload"].get("impact") or []).lower() or \
           "not affected" in " ".join(gate["payload"].get("impact") or gate["payload"].get("notes") or []).lower()
    d = post(client, "/api/ai/cards/%s/respond" % gate["id"],
             {"response": {"action": "confirm"}, "idempotency_key": "cs3"})
    assert d.json["ok"]
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM emails WHERE status='cancelled'").fetchone()["n"]
    c.close()
    assert n == 12
    assert mock_llm.calls == 2  # two routing calls, zero flow calls


# ---------------------------------------------------------------- settings limits
def test_settings_limits_flow(client, tenant, mock_llm):
    mock_start_flow(mock_llm, "settings")
    d = post(client, "/api/ai/agent", {"text": "change sending limits"})
    assert d.json["ok"]
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"], "options")["id"],
             {"response": {"id": "limits"}, "idempotency_key": "sl1"})
    edit = find_card(d.json["outcomes"], "table_edit")
    assert edit and edit["step_id"] == "limits_edit"
    vals = {f["field"]: f["value"] for f in edit["payload"]["fields"]}
    vals["Daily cap (emails)"] = "500"
    d = post(client, "/api/ai/cards/%s/respond" % edit["id"],
             {"response": {"fields": [{"field": k, "value": v} for k, v in vals.items()]},
              "idempotency_key": "sl2"})
    gate = find_card(d.json["outcomes"], "confirm")
    assert gate and gate["payload"]["action"] == "set_send_limits"
    txt = json.dumps(gate["payload"].get("details") or [])
    assert "500" in txt, "confirm must show before -> after values"
    d = post(client, "/api/ai/cards/%s/respond" % gate["id"],
             {"response": {"action": "confirm"}, "idempotency_key": "sl3"})
    assert d.json["ok"]
    assert db.get_setting("max_sends_per_day") == "500"
    assert mock_llm.calls == 1


def test_nested_flow_end_no_crash(client, mock_llm):
    """A flow nested under another (import_list -> send_campaign) must end
    cleanly when the child completes: the parent ends without AttributeError
    and without leaving conv['flow'] set (F-69: never corrupt state)."""
    from conftest import post, b64, find_card
    import flows
    d = post(client, "/api/ai/agent", {"text": "create campaign NestedTest"})
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"])["id"],
             {"response": {"id": "skip"}, "idempotency_key": "nt-desc"})
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"])["id"],
             {"response": {"action": "confirm"}, "idempotency_key": "nt-gate"})
    # drop into import_list (child), then start send_campaign (grandchild) via its post option
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"])["id"],
             {"response": {"id": "import"}, "idempotency_key": "nt-imp"})
    fr = find_card(d.json["outcomes"], "file_request")
    assert fr
    csv = "serial,company,hr_name,hr_email\n1,Co,Rec,%s\n" % "t@t.co"
    d = post(client, "/api/ai/cards/%s/respond" % fr["id"],
             {"response": {"file": {"name": "n.csv", "data": b64("n.csv", csv.encode())["data"]}},
              "idempotency_key": "nt-file"})
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"])["id"],
             {"response": {"id": "import"}, "idempotency_key": "nt-imp2"})
    import db as dbmod
    c = dbmod.conn()
    cam_id = c.execute("SELECT id FROM campaigns WHERE name='NestedTest'").fetchone()["id"]
    c.close()
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"])["id"],
             {"response": {"id": str(cam_id)}, "idempotency_key": "nt-link"})
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"])["id"],
             {"response": {"action": "confirm"}, "idempotency_key": "nt-imp-gate"})
    # post_import -> send emails -> send_campaign flow starts (sheet+campaign pre-filled)
    d = post(client, "/api/ai/cards/%s/respond" % find_card(d.json["outcomes"])["id"],
             {"response": {"id": "send"}, "idempotency_key": "nt-send"})
    # range -> template
    rng = find_card(d.json["outcomes"], "options")
    assert rng and rng.get("step_id") == "range", (rng or {}).get("step_id")
    d = post(client, "/api/ai/cards/%s/respond" % rng["id"],
             {"response": {"id": "all"}, "idempotency_key": "nt-range"})
    assert d.json["ok"], d.json
    cam_opts = find_card(d.json["outcomes"], "options")
    assert cam_opts and cam_opts.get("step_id") == "pick_template", cam_opts
    tpl_opts = cam_opts["payload"]["options"]
    # cancel via the flow: no template option is valid, so use "cancel" on the
    # range/audience-less flow -> pick a template id then cancel at the mapping gate
    d = post(client, "/api/ai/cards/%s/respond" % cam_opts["id"],
             {"response": {"id": tpl_opts[0]["id"]}, "idempotency_key": "nt-tpl"})
    assert d.json["ok"], d.json
    # mapping table_edit -> answer it, then back out through a cancel card
    m = find_card(d.json["outcomes"], "table_edit")
    if m:
        d = post(client, "/api/ai/cards/%s/respond" % m["id"],
                 {"response": {"fields": [{"field": f["field"], "value": "literal:X"}
                                          for f in m["payload"]["fields"]]},
                  "idempotency_key": "nt-map"})
        assert d.json["ok"], d.json
    cc = find_card(d.json["outcomes"], "text_input")
    if cc:
        d = post(client, "/api/ai/cards/%s/respond" % cc["id"],
                 {"response": {"text": ""}, "idempotency_key": "nt-cc"})
        assert d.json["ok"], d.json
    sig = find_card(d.json["outcomes"], "options")
    if sig and sig.get("step_id") == "signature":
        d = post(client, "/api/ai/cards/%s/respond" % sig["id"],
                 {"response": {"id": "0"}, "idempotency_key": "nt-sig"})
        assert d.json["ok"], d.json
    att = find_card(d.json["outcomes"], "options")
    if att and att.get("step_id") == "attachments":
        d = post(client, "/api/ai/cards/%s/respond" % att["id"],
                 {"response": {"id": "none"}, "idempotency_key": "nt-att"})
        assert d.json["ok"], d.json
    tim = find_card(d.json["outcomes"], "options")
    assert tim and tim.get("step_id") == "timing", (tim or {}).get("step_id")
    d = post(client, "/api/ai/cards/%s/respond" % tim["id"],
             {"response": {"id": "queue"}, "idempotency_key": "nt-timing"})
    assert d.json["ok"], d.json
    rem = find_card(d.json["outcomes"], "options")
    assert rem and rem.get("step_id") == "reminders"
    d = post(client, "/api/ai/cards/%s/respond" % rem["id"],
             {"response": {"id": "none"}, "idempotency_key": "nt-rem"})
    assert d.json["ok"], d.json
    # final_review -> cancel (declined) -> send_campaign cancels, pops parent import_list
    g = find_card(d.json["outcomes"], "final_review")
    assert g
    d = post(client, "/api/ai/cards/%s/respond" % g["id"],
             {"response": {"action": "cancel"}, "idempotency_key": "nt-scancel"})
    assert d.json["ok"], d.json
    # the parent (import_list) must have ended without crashing:
    # the conversation is clean (no active flow)
    conv_id = d.json["conv_id"]
    d = get(client, "/api/ai/convs/%s" % conv_id)
    assert d.json["ok"], d.json
    assert d.json.get("flow") is None, d.json
