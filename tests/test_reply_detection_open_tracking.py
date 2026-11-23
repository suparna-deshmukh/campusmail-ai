"""Reply detection must actually find replies (the old Gmail query used
in-reply-to:/references: operators that Gmail does not index, so the reply
counter was stuck at 0), and open tracking must mark + sync opens."""
import base64
import time
from datetime import datetime, timedelta

import db
import gmail_client
from conftest import get, post


class _MsgMeta:
    def __init__(self, mid, refs):
        self.mid = mid
        self.refs = refs


class _FakeSvc:
    """Minimal stand-in: list() returns the msg whose From address matches the
    query's from: filter (like real Gmail); get() returns that message's
    headers. The reply is found by scanning headers, exactly like the fixed
    check_replies."""
    def __init__(self, mid, frm, label="INBOX"):
        self.meta_by_label = {label: [(mid, frm, "<" + mid + ">")], "SPAM": []}

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
                q = (kw.get("q") or "").lower()
                frm = q.split("from:", 1)[1].strip() if "from:" in q else ""
                msgs = [m for m in outer.meta_by_label.get(label, []) if not frm or frm in m[1]]
                return _R({"messages": [{"id": m[0]} for m in msgs]})

            def get(self, **kw):
                mid = kw["id"]
                for label, msgs in outer.meta_by_label.items():
                    for (m_id, frm, refs) in msgs:
                        if m_id == mid:
                            return _R({"payload": {"headers": [
                                {"name": "From", "value": frm},
                                {"name": "References", "value": refs},
                                {"name": "In-Reply-To", "value": refs},
                            ]}})
                return _R({"payload": {"headers": []}})

        return M()


def _svc_with_reply(mid):
    # the reply sits in the inbox; its References header carries our Message-Id
    return _FakeSvc(mid, "rec1@example.com")


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


def test_check_replies_finds_reply_by_header_scan(tenant, monkeypatch):
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


def test_check_replies_no_reply(tenant, monkeypatch):
    eid = _add_sent("none")[0]
    mid = "nomatch@mail.gmail.com"
    c = db.conn()
    c.execute("UPDATE emails SET message_id=? WHERE id=?", (mid, eid))
    c.commit()
    c.close()

    monkeypatch.setattr(gmail_client, "get_service",
                        lambda acct: _FakeSvc("g", "rec1@example.com"))
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


# ---------------- trackable self-test mails + manual mark-read ----------------

def test_test_email_is_trackable_with_pixel(client, tenant, mock_gmail):
    """'Send me a test' must exercise the same read-receipt path as a real
    send: a trackable emails row (is_test=1) with the open pixel in the body."""
    db.set_setting("public_base", "https://app.example.com")
    db.set_setting("track_opens", "1")
    r = post(client, "/api/emails/test", {"template_id": 1, "mapping": {}, "sheet_id": 1,
                                          "account_id": tenant["account"]})
    assert r.status_code == 200, r.data[:200]
    d = r.json
    assert d["ok"] and d["mode"] == "sent", d
    assert "email_id" in d
    c = db.conn()
    row = c.execute("SELECT * FROM emails WHERE id=?", (d["email_id"],)).fetchone()
    c.close()
    assert row["is_test"] == 1
    assert row["status"] == "sent" and row["sent_at"]
    assert row["to_addr"] == "admin@test.dev"
    body_sent = mock_gmail.sends[-1]["args"][3]
    assert "/t/px/" in body_sent, "test mail body must carry the open-tracking pixel"


def test_test_email_does_not_pollute_analytics(client, tenant, mock_gmail):
    db.set_setting("public_base", "https://app.example.com")
    post(client, "/api/emails/test", {"template_id": 1, "mapping": {}, "sheet_id": 1,
                                      "account_id": tenant["account"]})
    d = get(client, "/api/state").json
    assert d["ok"], d
    # the state reply block must not count the test mail as sent/opened
    assert d["reply"]["sent"] == 0
    assert d["reply"]["opened"] == 0


def test_mark_read_endpoint(client, tenant):
    eid = _add_sent("mr")[0]
    r = post(client, "/api/emails/%d/mark-read" % eid, {"read": True})
    assert r.status_code == 200, r.data[:200]
    c = db.conn()
    row = c.execute("SELECT opened_at, open_count FROM emails WHERE id=?", (eid,)).fetchone()
    c.close()
    assert row["opened_at"] and row["open_count"] == 1
    # un-mark clears it
    post(client, "/api/emails/%d/mark-read" % eid, {"read": False})
    c = db.conn()
    row = c.execute("SELECT opened_at, open_count FROM emails WHERE id=?", (eid,)).fetchone()
    c.close()
    assert not row["opened_at"] and row["open_count"] == 0


def test_mark_replied_toggle(client, tenant):
    """The mark-replied endpoint used to crash (3 args to a 2-arg service);
    toggling on and off must both work."""
    eid = _add_sent("mr2")[0]
    r = post(client, "/api/emails/%d/mark-replied" % eid, {"replied": True})
    assert r.status_code == 200, r.data[:200]
    c = db.conn()
    row = c.execute("SELECT replied_at FROM emails WHERE id=?", (eid,)).fetchone()
    c.close()
    assert row["replied_at"]
    r = post(client, "/api/emails/%d/mark-replied" % eid, {"replied": False})
    assert r.status_code == 200, r.data[:200]
    c = db.conn()
    row = c.execute("SELECT replied_at FROM emails WHERE id=?", (eid,)).fetchone()
    c.close()
    assert not row["replied_at"]


# ---------------- sync version-skew guard must not crash ----------------

def test_apply_change_remote_payload_missing_column(tenant):
    """Root-cause regression: _apply_change fetched the local row with only the
    version columns, so the version-skew guard crashed with IndexError when the
    remote payload omitted a non-version column (e.g. an older instance pushing
    a row that has is_test locally). Any such upsert must apply cleanly."""
    import sync_relay
    eid = _add_sent("skew")[0]
    c = db.conn()
    # newer remote update that omits is_test (older instance payload shape)
    payload = {"data": {"status": "sent", "updated_at": "2026-11-23 13:00:00",
                        "updated_at_ms": int(__import__("time").time() * 1000) + 999999}}
    ok = sync_relay._apply_change(c, "emails", eid, "upsert", payload)
    c.commit()
    row = c.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone()
    c.close()
    assert ok is True
    assert row["is_test"] == 0, "local is_test must survive a payload that omits it"


# ---------------- sync rate-limit regression ----------------

def test_sync_ticks_never_run_concurrently(client, tenant, monkeypatch):
    """Root-cause regression for the relay 403 rateLimitExceeded bursts: the
    scheduled tick and a force click used to run in two threads and push to
    the relay at the same time. Ticks must serialize (skip if busy)."""
    import threading
    import sync_relay

    ran = {"active": 0, "max_active": 0, "skipped": 0}
    def fake_inner(force=False):
        with _lock:
            if ran["active"] > 0:
                raise AssertionError("two sync ticks overlapped")
            ran["active"] += 1
            ran["max_active"] = max(ran["max_active"], ran["active"])
        try:
            import time as _t
            _t.sleep(0.05)
        finally:
            with _lock:
                ran["active"] -= 1
    _lock = threading.Lock()
    monkeypatch.setattr(sync_relay, "_sync_tick_inner", fake_inner)
    monkeypatch.setattr(sync_relay.db, "get_setting", lambda k, d="0": "0")
    monkeypatch.setattr(sync_relay.db, "set_setting", lambda *a: None)

    # clear state so both ticks attempt to run
    sync_relay.SYNC_STATE.update({"last_run": 0, "last_pull": 0, "last_attempt": 0, "halted": False})

    results = []
    def runner(force):
        sync_relay.sync_tick(force=force)
        results.append(force)
    t1 = threading.Thread(target=runner, args=(False,))
    t2 = threading.Thread(target=runner, args=(True,))
    t1.start(); t2.start(); t1.join(5); t2.join(5)
    assert ran["max_active"] == 1, "a tick must skip when one is running"
    assert not any(r is None for r in results)
