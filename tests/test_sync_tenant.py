"""Spec F-72 + 17.1.10/11: agent mutations produce the same rev/tombstone
records as the UI (same service layer), and tenants never leak into each
other (team vs personal workspace)."""
import json

import actions
import db
import services
import sync_relay
from conftest import post, find_card, find_outcome, ADMIN


def test_agent_mutation_sync_records(client, tenant, mock_llm):
    # create via the agent (tier-0, 0 LLM)
    d = post(client, "/api/ai/agent", {"text": "create campaign SyncTest"})
    assert d.json["ok"], d.json
    card = find_card(d.json["outcomes"], "options")
    d = post(client, "/api/ai/cards/%s/respond" % card["id"],
             {"response": {"id": "skip"}, "idempotency_key": "st1"})
    gate = find_card(d.json["outcomes"], "confirm")
    d = post(client, "/api/ai/cards/%s/respond" % gate["id"],
             {"response": {"action": "confirm"}, "idempotency_key": "st2"})
    assert d.json["ok"], d.json
    admin = db.get_user(tenant["admin"])
    c = db.conn()
    camp = dict(c.execute("SELECT * FROM campaigns WHERE name='SyncTest'").fetchone())
    c.close()
    assert camp and (camp["updated_at"] or camp["updated_at_ms"]), "created rows carry a rev"
    # update via the same service the UI uses: rev must bump
    import time
    time.sleep(0.01)
    services.campaign_update(camp["id"], description="edited via agent", actor=admin)
    c = db.conn()
    after = dict(c.execute("SELECT * FROM campaigns WHERE id=?", (camp["id"],)).fetchone())
    c.close()
    assert int(after["updated_at_ms"] or 0) >= int(camp["updated_at_ms"] or 0)
    assert after["updated_at"] != camp["updated_at"], "update must bump the sync rev"
    # delete via the agent: tombstone must be recorded (propagates to both instances)
    d = post(client, "/api/ai/agent", {"text": "delete campaign SyncTest"})
    gate = find_card(d.json["outcomes"], "confirm")
    assert gate and gate["payload"]["risk"] == "R3"
    d = post(client, "/api/ai/cards/%s/respond" % gate["id"],
             {"response": {"action": "confirm"}, "idempotency_key": "st3"})
    assert d.json["ok"], d.json
    c = db.conn()
    tomb = c.execute("SELECT * FROM sync_deletes WHERE table_name='campaigns' AND row_id=?",
                     (camp["id"],)).fetchone()
    n = c.execute("SELECT COUNT(*) n FROM campaigns WHERE name='SyncTest'").fetchone()["n"]
    c.close()
    assert tomb, "deletion must be recorded as a sync tombstone"
    assert n == 0
    assert mock_llm.calls == 0


def test_template_and_sheet_tombstones(client, tenant, mock_llm):
    admin = db.get_user(tenant["admin"])
    services.template_update(1, name="Outreach v2", actor=admin)
    c = db.conn()
    tpl = dict(c.execute("SELECT * FROM email_templates WHERE id=1").fetchone())
    c.close()
    assert tpl["updated_at"], "template update must bump the rev"
    services.template_delete(1, actor=admin)
    c = db.conn()
    t1 = c.execute("SELECT * FROM sync_deletes WHERE table_name='email_templates' AND row_id=1").fetchone()
    c.close()
    assert t1, "template deletion must be a tombstone"
    services.sheet_delete(1, actor=admin)
    c = db.conn()
    t2 = c.execute("SELECT * FROM sync_deletes WHERE table_name='sheets' AND row_id=1").fetchone()
    c.close()
    assert t2, "sheet deletion must be a tombstone"


def test_tenant_isolation(client, tenant):
    # team admin sees team data
    admin = db.get_user(tenant["admin"])
    res = actions.read_direct("list_campaigns", {}, admin)
    rows = res.get("data") if isinstance(res, dict) else res
    names = [r["name"] for r in (rows or [])]
    assert "Diwali" in names
    # personal workspace: a different DB, same email address
    personal = tenant["db"].parent / "personal.db"
    with db.tenant(personal):
        db.init_db(seed=False)
        c = db.conn()
        c.execute("INSERT INTO users(email, display_name, role, status, request_note, created_at) "
                  "VALUES(?,?, 'admin','active','','%s')" % db.now_iso(), (ADMIN, "P"))
        c.execute("INSERT INTO campaigns(name, description, created_at, created_by) "
                  "VALUES('Personal Only','', '%s', '%s')" % (db.now_iso(), ADMIN))
        c.commit(); c.close()
        puser = db.get_user(db.conn().execute("SELECT id FROM users WHERE email=?", (ADMIN,)).fetchone()["id"])
        res_p = actions.read_direct("list_campaigns", {}, puser)
        rows_p = res_p.get("data") if isinstance(res_p, dict) else res_p
        names_p = [r["name"] for r in (rows_p or [])]
        assert names_p == ["Personal Only"]
        assert "Diwali" not in names_p, "team data must not leak into the personal workspace"
        # a personal mutation stays in the personal DB
        c = db.conn()
        c.execute("INSERT INTO email_templates(name, subject, body_html, kind, is_active, fields, created_at) "
                  "VALUES('P Tpl','Hi','<p>x</p>','outreach',0,'[]','%s')" % db.now_iso())
        c.commit(); c.close()
    # back in team context: nothing personal leaked in
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM email_templates WHERE name='P Tpl'").fetchone()["n"]
    names = [r["name"] for r in c.execute("SELECT name FROM campaigns")]
    c.close()
    assert n == 0 and "Personal Only" not in names
