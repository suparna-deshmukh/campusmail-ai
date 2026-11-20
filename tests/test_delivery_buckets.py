"""Delivery verification buckets (2026-11-15):
- bounced mails stop being re-checked (no more reply polling)
- reply polling starts only after the delivery window (replies belong to
  confidently delivered mails)
- every sent mail gets re-checked over time (rotating cursor, no oldest-40 starvation)
- the campaign page splits sent into "delivered" (past the verification
  window) and "verifying" (in flight, Gmail may still retry/DSN pending)
"""
from datetime import datetime, timedelta

import db
import app as appmod
from conftest import get


def _iso(offset_hours):
    return (datetime.now() - timedelta(hours=offset_hours)).strftime("%Y-%m-%d %H:%M:%S")


def _add_sent(tag, n, offset_hours, bounce=False):
    c = db.conn()
    ids = []
    c.execute("INSERT INTO batches(name, campaign_id, template_id, mapping, account_id, created_at) "
              "VALUES(?, 1, 1, '{}', 1, ?)", ("B-" + tag, _iso(offset_hours)))
    bid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    for j in range(n):
        c.execute(
            "INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, "
            "status, sent_at, account_id, message_id, bounced_at, created_at) "
            "VALUES(?, ?, ?, ?, 's', '<p>x</p>', 'sent', ?, 1, ?, ?, ?)",
            (bid, j + 1, str(j + 1), "rec%d@example.com" % (j + 1), _iso(offset_hours),
             "mid-%s-%d" % (tag, j), _iso(offset_hours) if bounce else "", _iso(offset_hours)))
        ids.append(c.execute("SELECT last_insert_rowid() id").fetchone()["id"])
    c.commit()
    c.close()
    return ids


def test_replies_tick_skips_bounced(tenant, mock_gmail):
    good = _add_sent("a", 1, 5 * 24)        # past the 72h delivery window
    bad = _add_sent("b", 1, 5 * 24, bounce=True)
    appmod.replies_tick()
    polled = [eid for pairs in mock_gmail.reply_pairs for (eid, _, _) in pairs]
    assert good[0] in polled, "delivered mails must be re-checked for replies"
    assert bad[0] not in polled, "bounced mails are terminal: stop re-checking them"


def test_replies_tick_rotates_past_first_40(tenant, mock_gmail):
    ids = _add_sent("rot", 45, 5 * 24)
    appmod.replies_tick()
    first = {eid for pairs in mock_gmail.reply_pairs for (eid, _, _) in pairs}
    assert len(first) == 40
    appmod.replies_tick()
    second = {eid for pairs in mock_gmail.reply_pairs[1:] for (eid, _, _) in pairs}
    assert len(second) == 5
    assert first | second == set(ids), "every sent mail must be re-checked over time"
    appmod.replies_tick()
    third = {eid for pairs in mock_gmail.reply_pairs[2:] for (eid, _, _) in pairs}
    assert third and third.issubset(first), "cursor wraps around to keep re-checking"


def test_campaign_summary_delivery_buckets(client, tenant):
    _add_sent("old", 2, 5 * 24)            # 5 days ago: past the 72h window -> delivered
    _add_sent("fresh", 2, 1)               # 1 hour ago: in flight -> verifying
    _add_sent("oldb", 1, 5 * 24, bounce=True)
    d = get(client, "/api/campaigns/1").json
    assert d["ok"], d
    s = d["summary"]
    assert s["n_verifying"] == 2
    assert s["n_sent"] == 2, "'delivered so far' excludes in-flight mails"
    assert s["n_bounced"] == 1


def test_verify_window_can_be_disabled(client, tenant):
    db.set_setting("delivery_verify_hours", "0")
    _add_sent("fresh", 2, 1)
    d = get(client, "/api/campaigns/1").json
    s = d["summary"]
    assert s["n_verifying"] == 0
    assert s["n_sent"] == 2


def test_replies_tick_starts_shortly_after_send(tenant, mock_gmail):
    young = _add_sent("young", 1, 0.25)     # 15m old: inside the 1h grace
    mid = _add_sent("mid", 1, 2)            # 2h old: past the grace, check now
    old = _add_sent("old", 1, 5 * 24)       # confidently delivered
    appmod.replies_tick()
    polled = [eid for pairs in mock_gmail.reply_pairs for (eid, _, _) in pairs]
    assert young[0] not in polled, "mails within the 1h grace are not yet reply-checked"
    assert mid[0] in polled, "mails past the 1h grace get reply-checked promptly"
    assert old[0] in polled, "delivered mails get reply-checked"
