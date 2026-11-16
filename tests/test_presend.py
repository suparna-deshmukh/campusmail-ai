"""Spec 7.5 + 17.1.17: the deterministic pre-send check - severities, blocking
vs non-blocking, link syntax / SSRF / offline behaviour, test-send suggestion,
and the no-new-columns-on-synced-tables rule."""
import json
import time
from datetime import datetime, timedelta

import db
import presend
import services
from conftest import ADMIN


def P(**over):
    p = {"template_id": 1, "sheet_id": 1,
         "mapping": {"company": "column:company", "hr_name": "column:hr_name"},
         "account_id": 1, "action": "queue",
         "_rows": services.audience_rows(1, None, None, None)}
    p.update(over)
    return p


def lines(res):
    out = []
    for l in res["lines"]:
        out.append(l)
    return out


def has(res, lid, severity=None):
    for l in res["lines"]:
        if l["id"] == lid and (severity is None or l["severity"] == severity):
            return l
    return None


def test_safe_mode_blocks_send_only(client, tenant):
    r = presend.run(P(action="send"))
    assert not has(r, "safe_mode"), "safe mode is off by default"
    db.set_setting("safe_mode", "1")
    r = presend.run(P(action="send"))
    assert has(r, "safe_mode", "block") and r["blocked"]
    r = presend.run(P(action="queue"))
    assert not has(r, "safe_mode"), "queue is not blocked by safe mode"


def test_cap_block_and_ack_warn(client, tenant):
    db.set_setting("max_sends_per_day", "5")
    r = presend.run(P(action="send"))
    assert has(r, "cap", "block") and r["blocked"]
    r = presend.run(P(action="send", cap_ack=True))
    assert has(r, "cap", "warn") and not has(r, "cap", "block")


def test_account_block(client, tenant):
    r = presend.run(P(action="draft", account_id=0))
    assert has(r, "account", "block")
    r = presend.run(P(action="draft"))
    assert not has(r, "account")


def test_schedule_window(client, tenant):
    past = (datetime.now().astimezone() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    r = presend.run(P(action="schedule", at=past))
    assert has(r, "when", "block") and r["blocked"]
    ok = (datetime.now().astimezone() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    r = presend.run(P(action="schedule", at=ok))
    assert not has(r, "when")


def test_audience_empty(client, tenant):
    r = presend.run(P(serial_from=99, serial_to=999))
    assert r["blocked"] and r["count"] == 0
    assert any(l["severity"] == "block" for l in r["lines"])


def test_cc_checks(client, tenant):
    many = ", ".join("u%d@x.com" % i for i in range(21))
    r = presend.run(P(cc=many))
    assert has(r, "cc_many", "block")
    r = presend.run(P(cc="not-an-email"))
    assert has(r, "cc_bad", "block")
    r = presend.run(P(cc="{recruiter_nam}"))
    assert has(r, "cc_field", "block")


def test_unmapped_field_blocks(client, tenant):
    r = presend.run(P(mapping={"company": "column:company"}))
    assert has(r, "unmapped", "block")
    assert "{hr_name}" in has(r, "unmapped", "block")["text"]


def test_blank_field_warns(client, tenant):
    c = db.conn()
    c.execute("UPDATE contacts SET hr_name='' WHERE serial IN ('1','2','3')")
    c.commit(); c.close()
    r = presend.run(P())
    l = has(r, "blank_field", "warn")
    assert l and "3 recipient" in l["text"]
    assert r["blocked"] is False


def test_subject_and_body_checks(client, tenant):
    r = presend.run(P(subject_override="S" * 90))
    assert has(r, "subject_long", "warn")
    services.template_update(1, subject="   ", actor={"id": 1, "email": "a@t.dev"})
    r = presend.run(P())
    assert has(r, "subject_empty", "block")
    services.template_update(1, subject="Hi {company}", actor={"id": 1, "email": "a@t.dev"})
    services.template_update(1, body_html="<p>&nbsp;&nbsp;</p>", actor={"id": 1, "email": "a@t.dev"})
    r = presend.run(P(mapping={"company": "column:company", "hr_name": "column:hr_name"}))
    assert has(r, "body_empty", "block")


def test_link_syntax_rules(client, tenant):
    body = ('<p><a href="www.site.com">a</a> <a href="plainhost.com/x">b</a> '
            '<a href="https://x.co/{unmapped_field_xyz}">c</a> <a href="https://example.com/y">d</a> '
            '<a href="">e</a></p>')
    services.template_update(1, body_html=body, actor={"id": 1, "email": "a@t.dev"})
    r = presend.run(P())
    assert has(r, "link_scheme", "warn")
    assert has(r, "link_dummy", "warn")
    assert has(r, "link_empty", "warn")
    assert has(r, "link_field", "block")
    assert r["blocked"] is True
    # a link containing a merge field must never be fetched
    assert all("{" in u or not u.startswith("http") for u in r["links_status"])


def test_ssrf_safe_rejects_private_ranges(client, tenant):
    assert presend._ssrf_safe("http://127.0.0.1/secret") is None
    assert presend._ssrf_safe("http://10.255.255.1/x") is None
    assert presend._ssrf_safe("http://192.168.1.1/x") is None
    assert presend._ssrf_safe("http://169.254.169.254/latest/meta-data/") is None
    assert presend._ssrf_safe("http://93.184.216.34/public") == "http://93.184.216.34/public"


def test_link_blocked_reported_as_info(monkeypatch, client, tenant):
    services.template_update(1, body_html='<p><a href="http://10.255.255.1/x">x</a></p>',
                             actor={"id": 1, "email": "a@t.dev"})
    monkeypatch.setattr(presend, "_probe", lambda u: "blocked")
    r = presend.run(P(async_links=False))
    assert has(r, "link_blocked", "info")
    assert r["blocked"] is False, "link reachability must never block"


def test_link_offline_reported_as_info(monkeypatch, client, tenant):
    services.template_update(1, body_html='<p><a href="http://93.184.216.34/uniq-offline-77">x</a></p>',
                             actor={"id": 1, "email": "a@t.dev"})

    def _boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    r = presend.run(P(async_links=False))
    l = has(r, "link_offline", "info")
    assert l and "Couldn't check" in l["text"]
    assert r["blocked"] is False


def test_test_suggestion_lifecycle(client, tenant):
    r = presend.run(P())
    assert has(r, "test_suggest", "info"), ">=10 recipients, untested content"
    c = db.conn()
    tpl = dict(c.execute("SELECT * FROM email_templates WHERE id=1").fetchone())
    c.close()
    h = services.template_content_hash(tpl)
    c = db.conn()
    c.execute("INSERT INTO template_test_sends(template_id, content_hash, user_id, sent_at) VALUES(?,?,?,?)",
              (1, h, 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    c.commit(); c.close()
    r = presend.run(P())
    assert not has(r, "test_suggest"), "a test send silences the suggestion"
    r = presend.run(P(test_skipped=True))
    assert not has(r, "test_suggest")


def test_warns_and_info_never_block(client, tenant):
    c = db.conn()
    c.execute("UPDATE contacts SET hr_name='' WHERE serial='1'")
    c.execute("UPDATE contacts SET hr_email='' WHERE serial='2'")
    c.commit(); c.close()
    services.template_update(1, body_html="<p><a href='www.missing.com'>x</a></p>", actor={"id": 1, "email": "a@t.dev"})
    r = presend.run(P())
    assert r["blocked"] is False, [l for l in r["lines"] if l["severity"] == "block"]
    assert has(r, "no_email", "warn") and has(r, "dup_email", "warn") or has(r, "no_email", "warn")


def test_no_new_columns_on_synced_tables(client, tenant):
    c = db.conn()
    for table in ("emails", "batches", "contacts", "sheets", "campaigns", "email_templates", "users"):
        cols = [r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)]
        bad = [x for x in cols if x in ("undo_hold", "ai_calls", "ai_tokens", "bulk_ack", "hold_release_blocked")]
        assert not bad, "%s gained new column(s) %s - synced tables must not change" % (table, bad)
    tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    for t in ("approvals", "ai_usage", "template_test_sends", "undo_holds", "link_check_cache",
              "ask_ai_convs", "ask_ai_cards", "card_responses"):
        assert t in tables, "local (non-synced) table %s missing" % t
