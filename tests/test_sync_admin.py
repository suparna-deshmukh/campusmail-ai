"""Laptop<->cloud resync: admin sync endpoints + stale 'sending' recovery."""
import time

import db
from conftest import post, ADMIN, MEMBER

import app as appmod


def test_sync_admin_force_requires_admin(client, tenant):
    d = post(client, "/api/admin/sync/force", {}, email=MEMBER)
    assert d.status_code == 403, d.json


def test_sync_admin_force_ok(client, tenant):
    d = post(client, "/api/admin/sync/force", {}, email=ADMIN)
    assert d.status_code == 200, d.json
    assert d.json == {"ok": True, "started": True}


def test_sync_reset_identity(client, tenant):
    old = db.get_setting("sync_instance_id", "")
    db.set_setting("sync_fail_count", "7")
    c = db.conn()
    c.execute("INSERT INTO sync_pushed(table_name, row_id, kind, version_ms) "
              "VALUES('emails', 1, 'row', 5)")
    c.execute("INSERT INTO sync_deletes(table_name, row_id, deleted_at) "
              "VALUES('emails', 9, '2026-01-01 00:00:00')")
    c.commit()
    c.close()
    d = post(client, "/api/admin/sync/reset-identity", {"full_resync": True}, email=ADMIN)
    assert d.status_code == 200, d.json
    new = d.json["new_instance"]
    assert d.json["old_instance"] == old
    assert new != old and new.startswith("inst-")
    assert db.get_setting("sync_instance_id", "") == new
    assert db.get_setting("sync_fail_count", "") == "0"
    c = db.conn()
    assert c.execute("SELECT COUNT(*) FROM sync_pushed").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM sync_deletes").fetchone()[0] == 0
    c.close()


def test_stale_sending_requeued(client, tenant, mock_gmail):
    trigger_sql = db.conn().execute(
        "SELECT sql FROM sqlite_master WHERE name='trg_emails_upd'").fetchone()[0]
    c = db.conn()
    c.execute("DROP TRIGGER trg_emails_upd")  # it would re-stamp the backdated row
    c = db.conn()
    c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, "
              "status, scheduled_at, account_id, created_at) "
              "VALUES(1, 1, '900', 'stale@example.com', 'stale', '', 'sending', "
              "'2030-01-01 00:00:00', 1, '2026-01-01 00:00:00')")
    c.execute("UPDATE emails SET updated_at_ms=? WHERE serial='900'",
              (int((time.time() - 3600) * 1000),))
    c.commit()
    c.execute(trigger_sql)
    c.close()
    appmod.scheduler_tick()
    c = db.conn()
    row = c.execute("SELECT status FROM emails WHERE serial='900'").fetchone()
    c.close()
    assert row["status"] == "scheduled", "stuck 'sending' row must be requeued"


def test_personal_workspace_for_pending_user(client, tenant, monkeypatch, tmp_path):
    """Non-team (pending) sign-in lands in a clean personal workspace:
    own DB file, no team data visible, personal banner in the shell."""
    # other tests leak the dev-login shortcut via the conftest helpers; this
    # one exercises the real session-based sign-in path
    monkeypatch.setattr(appmod, "DEV_LOGIN_EMAIL", "")
    c = db.conn()
    c.execute("INSERT INTO users(email, display_name, role, status, request_note, created_at) "
              "VALUES('outsider@test.dev', 'Outsider', 'user', 'pending', '', '%s')" % db.now_iso())
    uid = c.execute("SELECT id FROM users WHERE email='outsider@test.dev'").fetchone()["id"]
    c.commit()
    c.close()
    monkeypatch.setattr(appmod, "WORKSPACES_DIR", tmp_path / "ws")
    with client.session_transaction() as s:
        s["uid"] = uid
    d = client.get("/")
    assert d.status_code == 200
    assert "own workspace" in d.get_data(as_text=True)
    d = client.get("/api/campaigns")
    assert d.status_code == 200 and d.json["ok"]
    assert d.json["campaigns"] == [], "personal workspace must be a clean slate"
    d = client.get("/api/templates")
    assert d.json["templates"] == []
    ws_db = tmp_path / "ws" / (appmod.workspace_key("outsider@test.dev") + ".db")
    assert ws_db.exists(), "personal workspace must have its own database file"


def test_healthz_get_and_post(client, tenant):
    d = client.get("/healthz")
    assert d.status_code == 200 and d.json["ok"] and d.json["uptime_s"] >= 0
    d = client.post("/healthz", json={"ping": "supabase-cron"})
    assert d.status_code == 200 and d.json["ok"]
