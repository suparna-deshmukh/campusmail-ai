"""Bounce DSN handling (2026-11-15):
- permanent failures (RFC 3463 5xx: bad address, policy, whatever) mark the
  mail bounced - Gmail gave up, so it must never age into "delivered"
- temporary failures (4xx, "Gmail will retry for N more hours") do NOT bounce
  the mail; it stays verifying, but the problem is logged (delivery_retry)
- each DSN notice is processed exactly once (processed_dsn)
"""
import db
import app as appmod
import gmail_client

DSN_PERM_511 = """Delivery Status Notification (Failure)
Final-Recipient: rfc822;bad.addr@example.com
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 The email account that you tried to reach does not exist.
"""
DSN_PERM_541 = """Delivery Status Notification (Failure)
Final-Recipient: rfc822;policy.blocked@example.com
Status: 5.4.1
Diagnostic-Code: smtp; 554 5.4.1 Content rejected by policy.
"""
DSN_PERM_570 = """Delivery Status Notification (Failure)
Final-Recipient: rfc822;other.fail@example.com
Status: 5.7.0
Diagnostic-Code: smtp; 550 5.7.0 Delivery not permitted.
"""
DSN_SOFT_441 = """Delivery Status Notification (Delay)
Final-Recipient: rfc822;vijayasimha.sk@akuva.com
Status: 4.4.1
Diagnostic-Code: smtp; 451 4.4.1 Temporary problem with recipient's server. Gmail will retry.
"""


def _dsn(subject, text, mid="dsn1"):
    return {"message_id": mid, "subject": subject, "text": text}


def test_parse_permanent_failures_are_bounces():
    for text, addr in ((DSN_PERM_511, "bad.addr@example.com"),
                       (DSN_PERM_541, "policy.blocked@example.com"),
                       (DSN_PERM_570, "other.fail@example.com")):
        out = gmail_client.parse_bounce_notice(text)
        assert out and out[0][0] == addr, "%s must be a bounce" % addr


def test_parse_temporary_failure_is_not_a_bounce():
    assert gmail_client.parse_bounce_notice(DSN_SOFT_441) == []
    assert gmail_client.parse_retry_notice(DSN_SOFT_441) == [("vijayasimha.sk@akuva.com", "4.4.1")]
    assert gmail_client.parse_retry_notice(DSN_PERM_511) == []


def _seed_sent(addr):
    c = db.conn()
    c.execute("INSERT INTO batches(name, campaign_id, template_id, mapping, account_id, created_at) "
              "VALUES('B', 1, 1, '{}', 1, ?)", (db.now_iso(),))
    bid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.execute("INSERT INTO contacts(company, hr_name, hr_email, status, sheet_id, created_at) "
              "VALUES('X', 'R', ?, 'new', 1, ?)", (addr, db.now_iso()))
    cid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, "
              "status, sent_at, account_id, message_id, created_at) "
              "VALUES(?, ?, '1', ?, 's', '<p>x</p>', 'sent', ?, 1, 'mid1', ?)",
              (bid, cid, addr, db.now_iso(), db.now_iso()))
    eid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    c.close()
    return eid, cid


def _tick_with_notices(notices, monkeypatch):
    monkeypatch.setattr(gmail_client, "list_bounce_notices",
                        lambda acct, limit=30: notices)
    appmod.bounces_tick()


def test_bounces_tick_marks_permanent(tenant, mock_gmail, monkeypatch):
    eid, cid = _seed_sent("policy.blocked@example.com")
    _tick_with_notices([_dsn("Delivery Status Notification (Failure)", DSN_PERM_541)], monkeypatch)
    c = db.conn()
    row = c.execute("SELECT bounced_at, bounce_reason FROM emails WHERE id=?", (eid,)).fetchone()
    assert row["bounced_at"], "permanent 5.4.1 failure must mark the mail bounced"
    assert "5.4.1" in row["bounce_reason"]
    status = c.execute("SELECT status FROM contacts WHERE id=?", (cid,)).fetchone()["status"]
    assert status == "bounced"
    acts = [r["action"] for r in c.execute("SELECT action FROM activity").fetchall()]
    assert "bounce_detected" in acts
    c.close()


def test_bounces_tick_soft_logged_not_bounced_and_deduped(tenant, mock_gmail, monkeypatch):
    eid, _ = _seed_sent("vijayasimha.sk@akuva.com")
    notice = _dsn("Delivery Status Notification (Delay)", DSN_SOFT_441)
    _tick_with_notices([notice], monkeypatch)
    _tick_with_notices([notice], monkeypatch)  # same DSN again: must be deduped
    c = db.conn()
    row = c.execute("SELECT bounced_at FROM emails WHERE id=?", (eid,)).fetchone()
    assert not row["bounced_at"], "temporary 4.4.1 failure must NOT bounce the mail"
    retries = c.execute("SELECT detail FROM activity WHERE action='delivery_retry'").fetchall()
    assert len(retries) == 1, "soft-failure notice logged once (deduped), got %d" % len(retries)
    assert "akuva" in retries[0]["detail"] and "4.4.1" in retries[0]["detail"]
    c.close()
