"""Reply detection must actually find replies (the old Gmail query used
in-reply-to:/references: operators that Gmail does not index, so the reply
counter was stuck at 0), and open tracking must mark + sync opens."""
import base64
import time
from datetime import datetime, timedelta

import db
import gmail_client
from conftest import get


class _MsgMeta:
    def __init__(self, mid, refs):
        self.mid = mid
        self.refs = refs


class _FakeSvc:
    """Minimal stand-in: list() returns recent msgs per label; get() returns
    that message's References header. The reply is found by scanning headers,
    exactly like the fixed check_replies."""
    def __init__(self, meta_by_label):
        self.meta_by_label = meta_by_label

    def users(self):
        return self

    def messages(self):
        outer = self

        class _R:
            def __init__(self, data):
                self._d = data

            def execute(self):
                return self._d

        class M:
            def list(self, **kw):
                label = str(kw.get("labelIds") or "").upper()
                msgs = outer.meta_by_label.get(label, [])
                return _R({"messages": [{"id": m[0]} for m in msgs]})

            def get(self, **kw):
                mid = kw["id"]
                for label, msgs in outer.meta_by_label.items():
                    for (m_id, refs) in msgs:
                        if m_id == mid:
                            return _R({"payload": {"headers": [
                                {"name": "References", "value": refs},
                                {"name": "In-Reply-To", "value": refs},
                            ]}})
                return _R({"payload": {"headers": []}})

        return M()


def _svc_with_reply(mid):
    # the reply sits in the inbox; its References header carries our Message-Id
    return _FakeSvc({"INBOX": [("gmsg1", "<" + mid + ">")], "SPAM": []})


    # the reply sits in the inbox, its References header carries our Message-Id
    meta = [_MsgMeta("gmsg1", "<" + mid + ">")]
    return _FakeSvc({"INBOX": meta, "SPAM": []})


def _add_sent(tag, n=1, offset_hours=48):
    c = db.conn()
    iso = (datetime.now() - timedelta(hours=offset_hours)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO batches(name, campaign_id, template_id, mapping, account_id, created_at) "
              "VALUES(?, 1, 1, '{}', 1, ?)", ("B-" + tag, iso))
    bid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    ids = []
    for j in range(n):
        c.execute(
            "INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, "
            "status, sent_at, account_id, message_id, created_at) "
            "VALUES(?, ?, ?, ?, 's', '<p>x</p>', 'sent', ?, 1, ?, ?)",
            (bid, j + 1, str(j + 1), "rec%d@example.com" % (j + 1), iso,
             "mid-%s-%d" % (tag, j), iso))
        ids.append(c.execute("SELECT last_insert_rowid() id").fetchone()["id"])
    c.commit()
    c.close()
    return ids


def test_check_replies_finds_reply_by_header_scan(monkeypatch):
    """The real bug: a reply exists (References = our Message-Id) but the old
    Gmail query could never match it. The client-side scan must find it."""
    eid = _add_sent("found")[0]
    mid = "CAHwzOOyDeBgmek0xD2N-RX7-gQ6KSa=0e1FPAHpBvPZwNgCEBA@mail.gmail.com"
    c = db.conn()
    c.execute("UPDATE emails SET message_id=? WHERE id=?", (mid, eid))
    c.commit()
    c.close()

    monkeypatch.setattr(gmail_client, "get_service", lambda acct: _svc_with_reply(mid))
    # speed up: no sleeps
    monkeypatch.setattr(time, "sleep", lambda s: None)

    found = gmail_client.check_replies(1, [(eid, mid, "rec1@example.com")])
    assert str(eid) in found, "reply must be detected by scanning headers"


def test_check_replies_no_reply(monkeypatch):
    eid = _add_sent("none")[0]
    mid = "nomatch@mail.gmail.com"
    c = db.conn()
    c.execute("UPDATE emails SET message_id=? WHERE id=?", (mid, eid))
    c.commit()
    c.close()

    monkeypatch.setattr(gmail_client, "get_service",
                        lambda acct: _FakeSvc({"INBOX": [_MsgMeta("g", "<other@x>")], "SPAM": []}))
    monkeypatch.setattr(time, "sleep", lambda s: None)

    found = gmail_client.check_replies(1, [(eid, mid, "rec1@example.com")])
    assert str(eid) not in found


def test_open_token_roundtrip(tenant):
    tok = db.open_token_for(42)
    assert db.open_token_decode(tok) == ("", 42)
    assert db.open_token_decode(tok[:-1] + ("a" if tok[-1] != "a" else "b")) is None


def test_open_pixel_route_marks_opened(client, tenant):
    eid = _add_sent("open")[0]
    tok = db.open_token_for(eid)
    # unauthenticated GET (a mail client, not a signed-in user)
    r = client.get("/t/px/" + tok)
    assert r.status_code == 200 and r.mimetype == "image/png", r.data[:20]
    c = db.conn()
    row = c.execute("SELECT opened_at, open_count FROM emails WHERE id=?", (eid,)).fetchone()
    c.close()
    assert row["opened_at"] and row["open_count"] == 1
    # second open: first-open stays, count increments
    client.get("/t/px/" + tok)
    c = db.conn()
    row = c.execute("SELECT open_count FROM emails WHERE id=?", (eid,)).fetchone()
    first = row["open_count"]
    c.close()
    assert first == 2


def test_open_pixel_rejects_bad_token(client, tenant):
    r = client.get("/t/px/" + "garbage.token")
    assert r.status_code == 204  # silent no-op, mail client sees nothing


def test_campaign_summary_counts_opened(client, tenant):
    eid = _add_sent("summ", 1, 200)[0]  # old enough to be delivered
    c = db.conn()
    c.execute("UPDATE emails SET opened_at=?, open_count=1 WHERE id=?", (db.now_iso(), eid))
    c.commit()
    c.close()
    d = get(client, "/api/campaigns/1").json
    assert d["ok"], d
    assert d["summary"]["n_opened"] == 1
    assert d["summary"]["n_sent_all"] >= 1


def test_send_injects_open_pixel_when_configured(client, tenant, mock_gmail):
    db.set_setting("public_base", "https://app.example.com")
    db.set_setting("track_opens", "1")
    # build a pending batch email through merge then send
    c = db.conn()
    c.execute("INSERT INTO batches(name, campaign_id, template_id, mapping, account_id, created_at) "
              "VALUES('B-px', 1, 1, '{}', 1, '%s')" % db.now_iso())
    bid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, "
              "status, account_id, created_at) VALUES(?, 1, '1', 'rec1@example.com', 'Hi', "
              "'<p>hello</p>', 'pending', 1, '%s')" % db.now_iso(), (bid,))
    c.commit()
    c.close()
    import services
    out = services.batch_send_as(bid, "send", "", 0, tenant["account"],
                                 actor=dict(db.get_user(tenant["admin"])))
    assert out["ok"], out
    assert mock_gmail.sends
    body_sent = mock_gmail.sends[-1]["args"][3]
    assert '/t/px/' in body_sent, "open-tracking pixel must be injected into the sent body"


def test_no_pixel_when_tracking_off(client, tenant, mock_gmail):
    db.set_setting("public_base", "https://app.example.com")
    db.set_setting("track_opens", "0")
    c = db.conn()
    c.execute("INSERT INTO batches(name, campaign_id, template_id, mapping, account_id, created_at) "
              "VALUES('B-nopx', 1, 1, '{}', 1, '%s')" % db.now_iso())
    bid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, "
              "status, account_id, created_at) VALUES(?, 1, '1', 'rec1@example.com', 'Hi', "
              "'<p>hello</p>', 'pending', 1, '%s')" % db.now_iso(), (bid,))
    c.commit()
    c.close()
    import services
    services.batch_send_as(bid, "send", "", 0, tenant["account"],
                           actor=dict(db.get_user(tenant["admin"])))
    body_sent = mock_gmail.sends[-1]["args"][3]
    assert '/t/px/' not in body_sent


class _FakeSvc2:
    """list() -> recent inbox msgs; get() -> headers (From/Subject)."""
    def __init__(self, msgs):
        self.msgs = msgs  # list of (mid, headers-dict-lowercase)

    def users(self):
        return self

    def messages(self):
        outer = self

        class _R:
            def __init__(self, d): self._d = d
            def execute(self): return self._d

        class M:
            def list(self, **kw):
                return _R({"messages": [{"id": m[0]} for m in outer.msgs]})
            def get(self, **kw):
                mid = kw["id"]
                for (m_id, headers) in outer.msgs:
                    if m_id == mid:
                        return _R({"payload": {"headers": [
                            {"name": k.title(), "value": v} for k, v in headers.items()]}})
                return _R({"payload": {"headers": []}})

        return M()


def test_reads_tick_reply_implies_read(client, tenant, mock_gmail, monkeypatch):
    import app as appmod
    eid = _add_sent("rd1", 1, 200)[0]
    c = db.conn()
    c.execute("UPDATE emails SET replied_at=? WHERE id=?", (db.now_iso(), eid))
    c.commit()
    c.close()
    monkeypatch.setattr(appmod, "_send_accounts", lambda: [])  # no live scan in test A
    appmod.reads_tick()
    c = db.conn()
    row = c.execute("SELECT opened_at, open_count FROM emails WHERE id=?", (eid,)).fetchone()
    c.close()
    assert row["opened_at"] and row["open_count"] == 1, "reply must backfill opened_at"


def test_reads_tick_recipient_wrote_back(client, tenant, mock_gmail, monkeypatch):
    import app as appmod
    last = _add_sent("rd3", 1, 200)[0]
    c = db.conn()
    c.execute("UPDATE emails SET to_addr='hr@corp.com' WHERE id=?", (last,))
    c.commit(); c.close()
    # recipient writes to us (any subject) -> proves they read our mail
    svc = _FakeSvc2([("g1", {"from": "Priya HR <hr@corp.com>", "subject": "question about the drive"})])
    monkeypatch.setattr(appmod, "_send_accounts", lambda: [1])
    monkeypatch.setattr(appmod, "_gmail_svc", lambda acct: svc)
    appmod.reads_tick()
    c = db.conn()
    row = c.execute("SELECT opened_at, open_count FROM emails WHERE id=?", (last,)).fetchone()
    c.close()
    assert row["opened_at"] and row["open_count"] == 1, "recipient activity must mark read"


def test_reads_tick_bounced_never_read(client, tenant, mock_gmail, monkeypatch):
    import app as appmod
    eid = _add_sent("rd2", 1, 200)[0]
    c = db.conn()
    c.execute("UPDATE emails SET to_addr='bounce@x.com', bounced_at=? WHERE id=?", (db.now_iso(), eid))
    c.commit(); c.close()
    monkeypatch.setattr(appmod, "_send_accounts", lambda: [])
    appmod.reads_tick()
    c = db.conn()
    row = c.execute("SELECT opened_at FROM emails WHERE id=?", (eid,)).fetchone()
    c.close()
    assert not row["opened_at"], "bounced mail must never be marked read"
