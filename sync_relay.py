"""Two-way row-level sync, protocol v2 (outbox/rev) through the Supabase relay.

Every instance (localhost, hosted web) pushes the mutations it made and pulls the
changes the other instance made, exactly once, via a monotonic relay-assigned rev.
Result: all stats tables are identical on every instance.

Protocol (docs/SYNC_V2.md):
- Push:  POST /api/sync/push   {version, instance, rows, deletes}
         relay assigns a rev per accepted mutation, upserts state, records tombstones.
- Pull:  GET  /api/sync/changes?instance=X&since_rev=N
         returns only changes made by OTHER instances with rev > N, at most once
         (client keeps the rev watermark locally; the relay keeps 7 days of outbox).
- LWW:   the relay's rev is the ordering key between rows; per-row ties use epoch-ms
         updated_at_ms. Local timestamps are naive 'YYYY-MM-DD HH:MM:SS' and are
         interpreted as UTC on the sync boundary (display conversion stays in the app).
- Guardrails: version handshake (426 halts this instance), exponential backoff
         60s -> 30min on failures, skip the pull when nothing changed locally and the
         last pull is < 90s old, byte/row accounting in the activity log.
- Row identity: remote rows are applied with their remote id (id must be a positive
         integer); tombstoned rows are never resurrected by a stale copy.

`emails.body_html` is deliberately excluded from the sync payload (size).
"""
import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import db

VERSION = 2

# columns to sync per table (explicit lists - never clobber unlisted columns)
SYNC_COLUMNS = {
    "contacts": ["id", "serial", "company", "hr_name", "hr_email", "status",
                 "error", "extra", "notes", "created_at", "updated_at", "updated_at_ms"],
    "emails": ["id", "batch_id", "contact_id", "serial", "to_addr", "subject",
               "status", "scheduled_at", "sent_at", "error", "account_id",
               "message_id", "replied_at", "bounced_at", "bounce_reason",
               "reminder_count", "is_reminder", "created_at", "updated_at", "updated_at_ms"],
    "batches": ["id", "name", "template_id", "mapping", "account_id", "attachments",
                "reminder_days", "reminder_template_id", "reminder_max",
                "append_signature", "cc", "created_at", "created_by", "updated_at",
                "updated_at_ms", "campaign_id", "sheet_id"],
    "campaigns": ["id", "name", "description", "created_by", "created_at", "updated_at", "updated_at_ms"],
    "email_templates": ["id", "name", "type", "subject", "body_html", "fields",
                        "is_active", "created_at", "updated_at", "updated_at_ms"],
    "sheets": ["id", "name", "campaign_id", "filename", "headers", "rows", "n_rows",
               "created_by", "created_at", "updated_at", "updated_at_ms"],
}

# apply remote rows parents-first so re-inserts never orphan children
TABLE_ORDER = ["campaigns", "email_templates", "sheets", "batches", "contacts", "emails"]

SYNC_STATE = {"last_run": 0, "last_pull": 0, "last_attempt": 0, "halted": False}
PUSH_BATCH = 50
PULL_MIN_INTERVAL = 90


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def _ts_ms(s):
    """Local/relay timestamp -> epoch ms. Naive strings are UTC by convention;
    ISO strings are converted properly."""
    s = (s or "").strip()
    if not s:
        return 0
    if "T" in s or s.endswith("Z"):
        t = s[:-1] if s.endswith("Z") else s
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return int(datetime.datetime.strptime(t, fmt).replace(
                    tzinfo=datetime.timezone.utc).timestamp() * 1000)
            except ValueError:
                pass
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.datetime.strptime(s, fmt).replace(
                tzinfo=datetime.timezone.utc).timestamp() * 1000)
        except ValueError:
            pass
    return 0


def _ms_str(ms):
    return datetime.datetime.fromtimestamp((ms or 0) / 1000, datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")


def _cfg():
    url = (db.get_setting("cloud_relay_url", "") or "").strip().rstrip("/")
    secret = (db.get_setting("cloud_relay_secret", "") or "").strip()
    return url, secret


def _instance_id():
    iid = db.get_setting("sync_instance_id", "")
    if not iid:
        import secrets
        iid = "inst-" + secrets.token_hex(4)
        db.set_setting("sync_instance_id", iid)
    return iid


def _http(method, url, payload=None, tries=2):
    """One logical request. At most one fast retry; failures back off at tick level."""
    last = None
    for n in range(tries):
        try:
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            req = urllib.request.Request(url, data=data,
                                          headers={"Content-Type": "application/json",
                                                   "x-relay-secret": _cfg()[1]},
                                          method=method)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            return e.code, body
        except Exception as e:
            last = e
            if n + 1 < tries:
                time.sleep(2)
    raise last


def _bump_fail(note=""):
    try:
        n = int(db.get_setting("sync_fail_count", "0") or 0) + 1
        db.set_setting("sync_fail_count", str(n))
        db.set_setting("sync_last_attempt", str(int(time.time())))
        if note:
            db.log_activity("sync_error", note[:200], "sync")
    except Exception:
        pass


def _clear_fail():
    try:
        db.set_setting("sync_fail_count", "0")
        db.set_setting("sync_last_attempt", str(int(time.time())))
    except Exception:
        pass


def record_delete(table, row_id):
    """Remember a local hard delete so it propagates to every other instance."""
    if table not in SYNC_COLUMNS:
        return
    try:
        rid = int(row_id)
    except (TypeError, ValueError):
        return
    if rid <= 0:
        return
    c = db.conn()
    c.execute("INSERT OR REPLACE INTO sync_deletes(table_name, row_id, deleted_at) VALUES(?,?,?)",
              (table, rid, _now_utc().strftime("%Y-%m-%d %H:%M:%S.%f")))
    c.commit()
    c.close()


def _split_cursor(cur):
    """Parse a 'ms|id' cursor into (ms, id); empty becomes (0, -1)."""
    cur = (cur or "").strip()
    if not cur:
        return 0, -1
    if "|" in cur:
        ms, _, rid = cur.rpartition("|")
        try:
            return int(ms), int(rid)
        except ValueError:
            return 0, -1
    try:
        return int(cur), -1
    except ValueError:
        return 0, -1


def _local_tombstone_ms(c, table, rid):
    r = c.execute("SELECT deleted_at FROM sync_deletes WHERE table_name=? AND row_id=?",
                  (table, rid)).fetchone()
    return _ts_ms(r["deleted_at"] if r else None) or 0


def _push_table(table, rows, dels):
    url, _ = _cfg()
    payload = {"version": VERSION, "instance": _instance_id(),
               "rows": rows, "deletes": dels}
    status, body = _http("POST", url + "/api/sync/push", payload)
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = {}
    if status == 200:
        return True, 0, 0
    if status == 426:
        db.log_activity("sync_version_rejected",
                        "relay requires a newer client; this instance stops syncing. " + str(parsed.get("error", ""))[:150], "sync")
        SYNC_STATE["halted"] = True
        return False, 0, 0
    if status in (429, 503, 507):
        _bump_fail("push " + table + " http " + str(status) + " " + str(parsed.get("error", ""))[:120])
        return False, 0, 0
    _bump_fail("push " + table + " http " + str(status))
    return False, 0, 0


def _row_ms(row):
    """Row version: epoch-ms column first, legacy string parse as fallback."""
    if row is None:
        return 0
    try:
        ms = int(row["updated_at_ms"] or 0) if "updated_at_ms" in row.keys() else 0
    except (IndexError, KeyError, TypeError, ValueError):
        ms = 0
    return ms or _ts_ms(row["updated_at"] if "updated_at" in row.keys() else None)


def _apply_change(c, table, rid, op, payload):
    """Apply one outbox change locally. Returns True if applied (or idempotently skipped)."""
    if table not in SYNC_COLUMNS or not isinstance(rid, int) or rid <= 0:
        return False
    if op == "delete":
        ms = int((payload or {}).get("deleted_at_ms") or 0)
        local = c.execute("SELECT updated_at, updated_at_ms FROM %s WHERE id=?" % table, (rid,)).fetchone()
        if local and _row_ms(local) > ms:
            return False  # local copy was modified after the remote delete - keep it
        if local:
            c.execute("DELETE FROM %s WHERE id=?" % table, (rid,))
        c.execute("INSERT OR REPLACE INTO sync_deletes(table_name, row_id, deleted_at) VALUES(?,?,?)",
                  (table, rid, _ms_str(ms)))
        return True
    # upsert
    data = (payload or {}).get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return False
    ms = int(data.get("updated_at_ms") or 0) or _ts_ms(data.get("updated_at"))
    if _local_tombstone_ms(c, table, rid) >= ms:
        return False  # local tombstone is newer - never resurrect
    cols = SYNC_COLUMNS[table]
    local = c.execute("SELECT updated_at, updated_at_ms FROM %s WHERE id=?" % table, (rid,)).fetchone()
    if local and _row_ms(local) > ms:
        return False  # local copy is newer
    vals = [rid] + [data.get(col) for col in cols if col != "id"]
    if local:
        # DELETE+INSERT: the INSERT trigger does not re-stamp updated_at when one is
        # supplied, so the remote timestamp stays authoritative (no echo, no LWW flip).
        c.execute("DELETE FROM %s WHERE id=?" % table, (rid,))
    c.execute("INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(cols), ",".join("?" * len(cols))),
              vals)
    return True


def sync_tick(force=False):
    """One push-then-pull cycle over all synced tables (best effort, guarded)."""
    try:
        _sync_tick_inner(force)
    except Exception as e:
        # never propagate: a sync failure must not kill the host process/thread
        try:
            _bump_fail("sync_tick exception: %r" % (e,))
            db.log_activity("sync_error", "tick failed: %r" % (e,), "sync")
        except Exception:
            pass
        SYNC_STATE["last_run"] = time.time()


def _sync_tick_inner(force=False):
    now = time.time()
    if SYNC_STATE["halted"]:
        return
    if not force and now - SYNC_STATE["last_run"] < 45:
        return
    url, secret = _cfg()
    if not url or not secret:
        return
    # exponential backoff after repeated failures: 60s, 2m, 4m, ... 30m
    try:
        fails = int(db.get_setting("sync_fail_count", "0") or 0)
    except ValueError:
        fails = 0
    if fails:
        interval = min(1800, 60 * (2 ** (fails - 1)))
        if not force and now - SYNC_STATE["last_attempt"] < interval:
            return
    SYNC_STATE["last_run"] = now
    SYNC_STATE["last_attempt"] = now
    instance = _instance_id()
    pushed_rows = pushed_dels = 0

    # ---- push local mutations (per table, per-row push watermarks) ----
    c = db.conn()
    try:
        for table in TABLE_ORDER:
            cols = SYNC_COLUMNS[table]
            pushed = dict(c.execute("SELECT row_id, version_ms FROM sync_pushed "
                                     "WHERE table_name=? AND kind='row'", (table,)).fetchall())
            rows = []
            for r in c.execute("SELECT %s FROM %s ORDER BY id" % (",".join(cols), table)).fetchall():
                r = dict(r)
                ms = int(r.get("updated_at_ms") or 0) or _ts_ms(r.get("updated_at"))
                if ms > int(pushed.get(r["id"], 0) or 0):
                    # keep real values (0, False, None) - no falsy coercion
                    r["data"] = {k: r.get(k) for k in cols if k != "id"}
                    r["data"]["updated_at_ms"] = ms
                    rows.append({"table": table, "id": r["id"], "data": r["data"]})
            pushed_del = dict(c.execute("SELECT row_id, version_ms FROM sync_pushed "
                                         "WHERE table_name=? AND kind='del'", (table,)).fetchall())
            dels = []
            for r in c.execute("SELECT row_id, deleted_at FROM sync_deletes WHERE table_name=? "
                                "ORDER BY row_id", (table,)).fetchall():
                ms = _ts_ms(r["deleted_at"])
                if ms > int(pushed_del.get(r["row_id"], 0) or 0):
                    dels.append({"table": table, "row_id": r["row_id"], "deleted_at_ms": ms})
            if not rows and not dels:
                continue
            rows, dels = rows[:PUSH_BATCH], dels[:PUSH_BATCH]
            ok, _, _ = _push_table(table, rows, dels)
            if not ok:
                c.close()
                return
            for r in rows:
                c.execute("INSERT OR REPLACE INTO sync_pushed(table_name, row_id, kind, version_ms) "
                          "VALUES(?,?, 'row', ?)", (table, r["id"], r["data"]["updated_at_ms"]))
            for d in dels:
                c.execute("INSERT OR REPLACE INTO sync_pushed(table_name, row_id, kind, version_ms) "
                          "VALUES(?,?, 'del', ?)", (table, d["row_id"], d["deleted_at_ms"]))
            c.commit()
            pushed_rows += len(rows)
            pushed_dels += len(dels)
    finally:
        c.close()
    if pushed_rows or pushed_dels or fails == 0:
        _clear_fail()

    # ---- pull the other instance's changes (skip when idle and recent) ----
    idle = pushed_rows == 0 and pushed_dels == 0
    if not force and idle and now - SYNC_STATE["last_pull"] < PULL_MIN_INTERVAL:
        return
    rev, _ = _split_cursor(db.get_setting("sync_v2_rev", ""))
    q = ("instance=" + urllib.parse.quote(instance) + "&since_rev=" + str(rev)
        + "&version=" + str(VERSION))
    status, body = _http("GET", url + "/api/sync/changes?" + q)
    if status == 426:
        SYNC_STATE["halted"] = True
        db.log_activity("sync_version_rejected", "changes: " + body[:150], "sync")
        return
    if status in (429, 503, 507):
        _bump_fail("changes http " + str(status) + " " + body[:120])
        return
    if status != 200:
        _bump_fail("changes http " + str(status))
        return
    try:
        parsed = json.loads(body)
    except ValueError:
        _bump_fail("changes bad json")
        return
    changes = parsed.get("changes") or []
    if not changes:
        SYNC_STATE["last_pull"] = now
        if pushed_rows or pushed_dels:
            db.log_activity("sync_tick",
                            "out %d rows/%d dels, in 0 changes, rev %d" %
                            (pushed_rows, pushed_dels, rev), "sync")
        return
    c = db.conn()
    try:
        # apply upserts parents-first, deletes in arrival order
        upserts = [ch for ch in changes if ch.get("op") != "delete"]
        upserts.sort(key=lambda ch: (TABLE_ORDER.index(ch["table"])
                                     if ch.get("table") in TABLE_ORDER else 99, ch.get("rev", 0)))
        # mark applied remote changes as pushed so they are never re-pushed
        # back to their origin (echo loop)
        for ch in upserts:
            if _apply_change(c, ch.get("table"), ch.get("row_id"), "upsert", ch.get("payload") or {}):
                data = (ch.get("payload") or {}).get("data") or {}
                ms = int(data.get("updated_at_ms") or 0) or _ts_ms(data.get("updated_at"))
                c.execute("INSERT OR REPLACE INTO sync_pushed(table_name, row_id, kind, version_ms) "
                          "VALUES(?,?, 'row', ?)", (ch.get("table"), ch.get("row_id"), ms))
        for ch in changes:
            if ch.get("op") == "delete":
                if _apply_change(c, ch.get("table"), ch.get("row_id"), "delete", ch.get("payload") or {}):
                    ms = int((ch.get("payload") or {}).get("deleted_at_ms") or 0)
                    c.execute("INSERT OR REPLACE INTO sync_pushed(table_name, row_id, kind, version_ms) "
                              "VALUES(?,?, 'del', ?)", (ch.get("table"), ch.get("row_id"), ms))
        c.commit()
    finally:
        c.close()
    next_rev = int(parsed.get("next_rev") or rev)
    db.set_setting("sync_v2_rev", str(next_rev))
    SYNC_STATE["last_pull"] = now
    _clear_fail()
    if pushed_rows or pushed_dels or changes:
        db.log_activity("sync_tick",
                        "out %d rows/%d dels, in %d changes, rev %d->%d" %
                        (pushed_rows, pushed_dels, len(changes), rev, next_rev), "sync")
