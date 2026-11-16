"""Regression: literal mapping values must render without the 'literal:'
prefix (send pipeline), and the local scheduler must claim rows atomically
so two instances on the same DB cannot double-send (F-29 / F-72)."""
import app as appmod
import db
import services


def test_fill_fields_strips_literal_prefix():
    for ff in (services.fill_fields, appmod.fill_fields):
        out = ff("Hi {hr_name} at {co}",
                 {"hr_name": "literal:Verify", "co": "column:company"},
                 {"company": "Acme"})
        assert out == "Hi Verify at Acme", out


def test_fill_fields_plain_and_column_unchanged():
    out = services.fill_fields("{a}{b}", {"a": "column:x", "b": "plain"}, {"x": "1"})
    assert out == "1plain"


def _seed_due_email(tenant, serial, to):
    c = db.conn()
    c.execute("INSERT INTO batches(name, template_id, mapping, account_id, campaign_id, sheet_id, "
              "created_by, created_at, updated_at) VALUES(?, 1, '{}', 1, 1, 1, ?, ?, ?)",
              ("B-%s" % serial, str(tenant["admin"]), db.now_iso(), db.now_iso()))
    bid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    cid = c.execute("SELECT id FROM contacts WHERE serial=?", (str(serial),)).fetchone()["id"]
    c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, "
              "status, scheduled_at, account_id, created_at) VALUES(?,?,?,?,'s','<p>b</p>','scheduled',?,1,?)",
              (bid, cid, serial, to, db.now_iso(), db.now_iso()))
    c.commit()
    c.close()


def test_claim_sql_is_exclusive(tenant):
    _seed_due_email(tenant, "9", "x@example.com")
    eid = db.conn().execute("SELECT id FROM emails WHERE serial='9'").fetchone()["id"]
    c1 = db.conn()
    r1 = c1.execute("UPDATE emails SET status='sending' WHERE id=? AND status='scheduled'", (eid,)).rowcount
    c1.commit()
    c2 = db.conn()
    r2 = c2.execute("UPDATE emails SET status='sending' WHERE id=? AND status='scheduled'", (eid,)).rowcount
    c2.close(); c1.close()
    assert (r1, r2) == (1, 0)


def test_scheduler_tick_skips_preclaimed_rows(tenant, mock_gmail):
    _seed_due_email(tenant, "1", "rec1@example.com")
    _seed_due_email(tenant, "2", "rec2@example.com")
    # another instance grabs serial 2 first (the race)
    c2 = db.conn()
    assert c2.execute("UPDATE emails SET status='sending' WHERE serial='2' AND status='scheduled'").rowcount == 1
    c2.commit()
    c2.close()

    appmod.scheduler_tick()

    sent = mock_gmail.sends
    assert len(sent) == 1, "preclaimed row must not be sent twice: %r" % (sent,)
    row = db.conn().execute("SELECT status FROM emails WHERE serial='1'").fetchone()
    assert row["status"] == "sent"
