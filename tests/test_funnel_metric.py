"""Campaign funnel 'Emailed' stage = contacts that received a SENT email.
Pending/drafted rows must not count (they made the bar show >100% of
contacts and confused sent vs. queued, 2026-11-15)."""
import db

from datetime import datetime, timedelta

from conftest import get


def _old_iso():
    return (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")


def test_funnel_emailed_counts_sent_only(client, tenant):
    c = db.conn()
    c.execute("INSERT INTO batches(name, campaign_id, template_id, mapping, account_id, created_at) "
              "VALUES('B-sent', 1, 1, '{}', 1, '%s')" % db.now_iso())
    b_sent = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    for i in range(1, 6):
        # 3 days old: past the delivery-verification window, so "delivered"
        c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, "
                  "status, sent_at, account_id, message_id, created_at) "
                  "VALUES(?, ?, ?, ?, 's', '<p>x</p>', 'sent', ?, 1, ?, ?)",
                  (b_sent, i, str(i), "rec%d@example.com" % i, _old_iso(),
                   "mid%d" % i, _old_iso()))
    c.execute("INSERT INTO batches(name, campaign_id, template_id, mapping, account_id, created_at) "
              "VALUES('B-unsent', 1, 1, '{}', 1, '%s')" % db.now_iso())
    b_unsent = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    for i in range(6, 13):
        st = "drafted" if i < 9 else "pending"
        c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, "
                  "status, created_at) VALUES(?, ?, ?, ?, 's', '<p>x</p>', ?, ?)",
                  (b_unsent, i, str(i), "rec%d@example.com" % i, st, db.now_iso()))
    c.commit()
    c.close()

    d = get(client, "/api/campaigns/1").json
    assert d["ok"], d
    assert d["n_emailed"] == 5, "funnel 'Emailed' must count sent emails only"
    assert d["summary"]["n_sent"] == 5
    assert d["summary"]["n_emails"] == 12  # pending/drafted still visible in totals
