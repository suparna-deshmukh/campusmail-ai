"""F-42 threading: app-sent replies carry In-Reply-To/References of the
original send, and build_raw renders them RFC-style (angle brackets)."""
import base64

import db
import gmail_client
import services


def _raw_text(b64url):
    b64url = b64url.replace("-", "+").replace("_", "/")
    return base64.urlsafe_b64decode(b64url + "=" * (-len(b64url) % 4)).decode("utf-8")


def test_build_raw_thread_headers():
    raw = gmail_client.build_raw("a@example.com", "Re: Hi", "<p>ok</p>",
                                 in_reply_to="abc123@mail.gmail.com",
                                 references=["abc123@mail.gmail.com"])
    txt = _raw_text(raw)
    assert "In-Reply-To: <abc123@mail.gmail.com>" in txt
    assert "References: <abc123@mail.gmail.com>" in txt


def test_build_raw_no_thread_headers_when_absent():
    raw = gmail_client.build_raw("a@example.com", "Hi", "<p>ok</p>")
    txt = _raw_text(raw)
    assert "In-Reply-To" not in txt and "References" not in txt


def test_email_reply_threads_on_original(client, tenant, mock_gmail):
    c = db.conn()
    c.execute("INSERT INTO batches(name, campaign_id, template_id, mapping, account_id, created_at) "
              "VALUES('B1', 1, 1, '{}', 1, '%s')" % db.now_iso())
    bid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, "
              "status, sent_at, account_id, message_id, created_at) "
              "VALUES(?, 1, '1', 'rec1@example.com', 'Hi Company1', '<p>hi</p>', 'sent', ?, 1, "
              "'origmid99@mail.gmail.com', ?)",
              (bid, db.now_iso(), db.now_iso()))
    eid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.commit()
    c.close()
    actor = dict(db.get_user(tenant["admin"]))
    r = services.email_reply(eid, "Thanks!", tenant["account"], actor=actor)
    assert r.get("sent") is True
    assert mock_gmail.sends, "reply must go out through the Gmail send path"
    k = mock_gmail.sends[-1]["kwargs"]
    assert k.get("in_reply_to") == "origmid99@mail.gmail.com"
    assert k.get("references") == ["origmid99@mail.gmail.com"]
