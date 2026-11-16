"""Shared fixtures: isolated temp tenant, Flask client, mocked LLM + Gmail."""
import base64
import contextvars
import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import db  # noqa: E402
import app as appmod  # noqa: E402
import ai_client  # noqa: E402
import gmail_client  # noqa: E402

ADMIN = "admin@test.dev"
MEMBER = "member@test.dev"


class MockGmail:
    def __init__(self):
        self.sends = []
        self.drafts = []
        self.reply_checks = 0
        self.reply_pairs = []

    def send_with_retry(self, *a, **k):
        self.sends.append({"args": a, "kwargs": k})
        return {"message_id": "mockmid%d" % len(self.sends), "id": "m%d" % len(self.sends)}

    def create_draft(self, *a, **k):
        self.drafts.append({"args": a, "kwargs": k})
        return {"id": "mockdraft%d" % len(self.drafts)}

    def check_replies(self, *a, **k):
        self.reply_checks += 1
        pairs = a[1] if len(a) > 1 else k.get("pairs") or []
        self.reply_pairs.append(list(pairs))
        return []


class MockLLM:
    """Scripted LLM with a call counter. chat_raw returns queued responses."""
    def __init__(self):
        self.calls = 0
        self.last_messages = None
        self.queue = []

    def chat_raw(self, messages, **kw):
        self.calls += 1
        self.last_messages = messages
        if self.queue:
            return self.queue.pop(0)
        return {"content": "ok", "tool_calls": []}

    def chat_json(self, *a, **k):
        return self.chat_raw(*a, **k)


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    dbpath = tmp_path / "team.db"
    cv = contextvars.ContextVar("campusmail_tenant_db", default=dbpath)
    monkeypatch.setattr(db, "_TENANT_DB", cv)
    monkeypatch.setattr(db, "DB_PATH", dbpath)
    db.init_db(seed=False)
    db.set_setting("safe_mode", "0")
    db.set_setting("ai_enabled", "1")
    db.set_setting("ai_base_url", "http://mock")
    db.set_setting("ai_api_key", "mock-key")
    # users
    c = db.conn()
    c.execute("INSERT INTO users(email, display_name, role, status, request_note, created_at) "
              "VALUES(?,?, 'admin','active','','%s')" % db.now_iso(), (ADMIN, "Admin"))
    c.execute("INSERT INTO users(email, display_name, role, status, request_note, created_at) "
              "VALUES(?,?, 'user','active','','%s')" % db.now_iso(), (MEMBER, "Member"))
    admin_id = c.execute("SELECT id FROM users WHERE email=?", (ADMIN,)).fetchone()["id"]
    member_id = c.execute("SELECT id FROM users WHERE email=?", (MEMBER,)).fetchone()["id"]
    # shared gmail account with a token
    c.execute("INSERT INTO gmail_accounts(label, email, owner_id, visibility, token_json, created_at) "
              "VALUES('Team mailbox','teambox@gmail.com',?, 'shared','{\"access_token\":\"mock\"}','%s')" % db.now_iso(),
              (admin_id,))
    # template + campaign + sheet with 12 contacts
    c.execute("INSERT INTO email_templates(name, subject, body_html, kind, is_active, fields, created_at) "
              "VALUES('Outreach', 'Hi {company}', '<p>Dear {hr_name},</p><p>Opening at {company}.</p>',"
              " 'outreach', 1, '[\"company\", \"hr_name\"]', '%s')" % db.now_iso())
    c.execute("INSERT INTO campaigns(name, description, created_at, created_by) "
              "VALUES('Diwali', 'test', '%s', ?)" % db.now_iso(), (ADMIN,))
    c.execute("INSERT INTO sheets(name, campaign_id, headers, n_rows, created_at, created_by) "
              "VALUES('List 1', 1, '[\"serial\",\"company\",\"hr_name\",\"hr_email\"]', 12, '%s', ?)" % db.now_iso(),
              (ADMIN,))
    for i in range(1, 13):
        c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, sheet_id, created_at) "
                  "VALUES(?, ?, ?, ?, 'new', 1, '%s')" % db.now_iso(),
                  (str(i), "Company%d" % i, "Recruiter%d" % i, "rec%d@example.com" % i))
    c.execute("UPDATE sheets SET n_rows=(SELECT COUNT(*) FROM contacts) WHERE id=1")
    c.commit()
    c.close()
    yield {
        "db": dbpath,
        "admin": admin_id,
        "member": member_id,
        "account": 1,
        "template": 1,
        "campaign": 1,
        "sheet": 1,
    }


@pytest.fixture()
def mock_gmail(monkeypatch):
    g = MockGmail()
    monkeypatch.setattr(gmail_client, "send_with_retry", g.send_with_retry)
    monkeypatch.setattr(gmail_client, "create_draft", g.create_draft)
    monkeypatch.setattr(gmail_client, "check_replies", g.check_replies)
    return g


@pytest.fixture()
def mock_llm(monkeypatch):
    m = MockLLM()
    monkeypatch.setattr(ai_client, "chat_raw", m.chat_raw)
    monkeypatch.setattr(ai_client, "chat", m.chat_raw)
    monkeypatch.setattr(ai_client, "chat_json", m.chat_json)
    monkeypatch.setattr(ai_client, "write_template", lambda brief, tone="professional": ("Subject line", "<p>Body %s</p>" % brief, "AI Template"))
    monkeypatch.setattr(ai_client, "write_email", lambda brief, kind="one_off": ("One-off subject", "<p>%s</p>" % brief))
    monkeypatch.setattr(ai_client, "polish", lambda text, instruction: ("Polished", "<p>polished %s</p>" % instruction))
    monkeypatch.setattr(ai_client, "suggest_subjects", lambda text: ["Sub one", "Sub two", "Sub three"])
    monkeypatch.setattr(ai_client, "suggest_reply", lambda orig, reply: ("<p>Thanks for getting back to me.</p>"))
    monkeypatch.setattr(ai_client, "write_reminder", lambda ctx: ("Following up", "<p>Just following up.</p>"))
    monkeypatch.setattr(ai_client, "suggest_mapping_llm", lambda fields, columns: {})
    return m


@pytest.fixture()
def client(tenant, mock_llm, mock_gmail, monkeypatch):
    import services as svc
    appmod.app.config["TESTING"] = True
    appmod.SCHEDULER_STATE["running"] = False
    # no real release timers in tests: ticks/holds are driven explicitly
    monkeypatch.setattr(svc, "_schedule_release_timer", lambda *a, **k: None)
    with appmod.app.test_client() as c:
        yield c


def login(client, email=ADMIN):
    appmod.DEV_LOGIN_EMAIL = email.lower()


def post(client, url, body, email=ADMIN):
    appmod.DEV_LOGIN_EMAIL = email.lower()
    return client.post(url, json=body)


def get(client, url, email=ADMIN):
    appmod.DEV_LOGIN_EMAIL = email.lower()
    return client.get(url)


def b64(name, data):
    return {"name": name, "data": base64.b64encode(data).decode("ascii")}


def find_card(outcomes, ctype=None):
    for o in outcomes or []:
        if o.get("kind") == "card":
            if ctype is None or o["card"]["type"] == ctype:
                return o["card"]
    return None


def find_outcome(outcomes, kind):
    for o in outcomes or []:
        if o.get("kind") == kind:
            return o
    return None


def respond_first(client, conv_id, card, pick=None):
    """Auto-respond to a non-gate card with a sensible default."""
    import datetime as dt
    p = card["payload"] or {}
    t = card["type"]
    if t == "options":
        rid = pick
        if not rid:
            for o in p.get("options") or []:
                if o.get("id") not in ("cancel", "stop", "discard"):
                    rid = o.get("id")
                    break
            if not rid and p.get("options"):
                rid = p["options"][0].get("id")
        resp = {"id": rid}
    elif t == "text_input":
        resp = {"text": p.get("default") or "Test value"}
    elif t == "number_input":
        resp = {"value": str(p.get("default", 0))}
    elif t == "datetime_input":
        d = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
        resp = {"value": d.strftime("%Y-%m-%dT%H:%M")}
    elif t == "secret_input":
        resp = {"value": "secret123"}
    elif t == "table_edit":
        resp = {"fields": [{"field": f["field"], "value": f.get("value") or "fixed"}
                            for f in (p.get("fields") or [])]}
    else:
        resp = {}
    return post(client, "/api/ai/cards/%s/respond" % card["id"],
                 {"response": resp, "idempotency_key": "auto-" + card["id"]})


def walk_send_review(client, mock_llm, timing="queue", stagger="0", at=None, key="w",
                     mapping_value=None):
    """Walk send_campaign (all cards, no confirm) and return the final_review /
    compact_review card. timing: queue|draft|schedule|send."""
    import datetime as _dt
    mock_llm.queue.append({"content": "", "tool_calls": [{
        "id": "wc1", "function": {"name": "start_flow",
                                  "arguments": json.dumps({"flow_id": "send_campaign"})}}]})
    d = post(client, "/api/ai/agent", {"text": "send emails"})
    assert d.json["ok"], d.json

    def nxt(ctype=None):
        c = find_card(d.json["outcomes"], ctype)
        assert c, "expected %s card, got: %s" % (
            ctype, [o["card"]["type"] for o in d.json["outcomes"] if o.get("kind") == "card"])
        return c

    def go(ctype, response):
        nonlocal d
        c = nxt(ctype)
        r = post(client, "/api/ai/cards/%s/respond" % c["id"],
                 {"response": response, "idempotency_key": "%s-%s" % (key, ctype)})
        assert r.json["ok"], r.json
        d = r

    go("options", {"id": "1"})    # campaign
    go("options", {"id": "1"})    # audience sheet
    go("options", {"id": "all"})  # range
    go("options", {"id": "1"})    # template
    m = nxt("table_edit")
    cols = m["payload"].get("columns") or []
    cols = [x for x in cols if x]
    resp = {"fields": [{"field": f["field"],
                        "value": mapping_value or (f["field"] if f["field"] in cols else cols[1])}
                       for f in m["payload"]["fields"]]}
    r = post(client, "/api/ai/cards/%s/respond" % m["id"],
             {"response": resp, "idempotency_key": key + "-map"})
    assert r.json["ok"], r.json
    d = r
    go("text_input", {"text": ""})   # cc
    go("options", {"id": "0"})       # signature off
    go("options", {"id": "none"})    # attachments
    go("options", {"id": timing})    # timing
    if timing == "schedule":
        when = at or (_dt.datetime.now().astimezone() + _dt.timedelta(hours=2))
        go("datetime_input", {"value": when.strftime("%Y-%m-%dT%H:%M")})
        go("number_input", {"value": stagger})
    elif timing == "send":
        go("number_input", {"value": stagger})
    go("options", {"id": "none"})    # reminders
    return nxt("final_review")


def walk_until_gate(client, outcomes, conv_id, max_steps=10, pick=None):
    """Walk through cards until a gate card appears. Returns (gate_card, last_outcomes)."""
    for _ in range(max_steps):
        card = find_card(outcomes)
        if card is None:
            return None, outcomes
        if card["type"] in ("confirm", "final_review", "compact_review"):
            return card, outcomes
        d = respond_first(client, conv_id, card, pick)
        outcomes = d.json["outcomes"]
    return find_card(outcomes), outcomes
