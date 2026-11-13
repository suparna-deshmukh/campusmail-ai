"""Two-way row-level sync of the team database through the Supabase relay.

Every instance (localhost, hosted web) pushes and pulls the rows it knows,
merged with last-write-wins on `updated_at`. Result: all stats tables are
identical on every instance, so analytics shown on the laptop and on the
phone are always the same numbers.

Synced tables carry an `updated_at` column maintained by SQLite triggers
(see db._migrate). `emails.body_html` is deliberately excluded from the
sync payload (size); the rest of an email row is synced.
"""
import json
import urllib.parse
import urllib.request

import db

# columns to sync per table (explicit lists - never clobber unlisted columns)
SYNC_COLUMNS = {
    "contacts": ["id", "serial", "company", "hr_name", "hr_email", "status",
                 "error", "extra", "notes", "created_at", "updated_at"],
    "emails": ["id", "batch_id", "contact_id", "serial", "to_addr", "subject",
               "status", "scheduled_at", "sent_at", "error", "account_id",
               "message_id", "replied_at", "bounced_at", "bounce_reason",
               "reminder_count", "is_reminder", "created_at", "updated_at"],
    "batches": ["id", "name", "template_id", "mapping", "account_id", "attachments",
                "reminder_days", "reminder_template_id", "reminder_max",
                "append_signature", "cc", "created_at", "created_by", "updated_at",
                "campaign_id", "sheet_id"],
    "campaigns": ["id", "name", "description", "created_by", "created_at", "updated_at"],
    "email_templates": ["id", "name", "type", "subject", "body_html", "fields",
                        "is_active", "created_at", "updated_at"],
    "sheets": ["id", "name", "campaign_id", "filename", "headers", "rows", "n_rows",
               "created_by", "created_at", "updated_at"],
}

SYNC_STATE = {"last_run": 0}


def _split_cursor(cur):
    """Parse a 'ts|id' cursor into (ts, id); bare timestamps become (ts, -1)."""
    cur = (cur or "").strip()
    if "|" in cur:
        ts, _, rid = cur.rpartition("|")
        try:
            return ts, int(rid)
        except ValueError:
            pass
    return cur, -1


def _norm_ts(s):
    """Normalize relay ISO and SQLite timestamps to 'YYYY-MM-DD HH:MM:SS' UTC."""
    s = (s or "").strip()
    if not s:
        return ""
    if "T" not in s and not s.endswith("Z"):
        return s
    import datetime
    t = s[:-1] if s.endswith("Z") else s
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(t, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return s


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


def _http(method, url, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json",
                                          "x-relay-secret": _cfg()[1]},
                                 method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _apply_rows(table, rows):
    """Upsert remote rows locally; incoming wins only when newer (or row missing)."""
    cols = SYNC_COLUMNS[table]
    c = db.conn()
    applied = 0
    for row in rows:
        rid, data = row.get("id"), row.get("data") or {}
        if not rid:
            continue
        try:
            rid = int(rid)
        except Exception:
            continue
        local = c.execute("SELECT updated_at FROM %s WHERE id=?" % table, (rid,)).fetchone()
        inc_ts = _norm_ts(data.get("updated_at"))
        if local and _norm_ts(local["updated_at"]) > inc_ts:
            continue  # local copy is newer
        if local:
            sets = ", ".join("%s=?" % col for col in cols if col != "id")
            vals = [data.get(col, "" if col != "id" else rid) for col in cols if col != "id"]
            c.execute("UPDATE %s SET %s WHERE id=?" % (table, sets), vals + [rid])
        else:
            cols2 = cols[:]
            vals = [data.get(col) for col in cols2]
            c.execute("INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(cols2),
                        ",".join("?" * len(cols2))), vals)
            applied += 1
    c.commit()
    c.close()
    return applied


def record_delete(table, row_id):
    """Remember a local hard delete so it propagates to every other instance."""
    if table not in SYNC_COLUMNS:
        return
    c = db.conn()
    c.execute("INSERT OR REPLACE INTO sync_deletes(table_name, row_id, deleted_at) VALUES(?,?,?)",
              (table, int(row_id), _now()))
    c.commit()
    c.close()


def _now():
    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _fetch_remote(table, since):
    url, _ = _cfg()
    path = "/api/sync/pull?table=" + table
    if since:
        path += "&since=" + urllib.parse.quote(since)
    return _http("GET", url + path)


def _fetch_remote_deletes(table, since):
    url, _ = _cfg()
    path = "/api/sync/pull/deletes?table=" + table
    if since:
        path += "&since=" + urllib.parse.quote(since)
    return _http("GET", url + path)


def _apply_deletes(table, dels):
    c = db.conn()
    for d in dels:
        rid, ts = d.get("id"), d.get("deleted_at") or ""
        if not rid:
            continue
        try:
            rid = int(rid)
        except Exception:
            continue
        local = c.execute("SELECT updated_at FROM %s WHERE id=?" % table, (rid,)).fetchone()
        if local and _norm_ts(local["updated_at"]) >= _norm_ts(ts):
            continue  # local copy was modified after the remote delete - keep it
        if local:
            c.execute("DELETE FROM %s WHERE id=?" % table, (rid,))
        c.execute("INSERT OR IGNORE INTO sync_deletes(table_name, row_id, deleted_at) VALUES(?,?,?)",
                  (table, rid, ts))
    c.commit()
    c.close()


def _push_remote(table, rows, deletes=None):
    url, _ = _cfg()
    return _http("POST", url + "/api/sync/push",
                 {"table": table, "instance": _instance_id(), "rows": rows,
                  "deletes": deletes or []})


def sync_tick(force=False):
    """One pull-then-push cycle over all synced tables (best effort)."""
    import time
    now = time.time()
    if not force and now - SYNC_STATE["last_run"] < 45:
        return
    SYNC_STATE["last_run"] = now
    url, secret = _cfg()
    if not url or not secret:
        return
    for table, cols in SYNC_COLUMNS.items():
        try:
            remote = _fetch_remote(table, db.get_setting("sync_pull_" + table, ""))
            rows = remote.get("rows", [])
            if rows:
                _apply_rows(table, rows)
            dels = _fetch_remote_deletes(table, db.get_setting("sync_pull_del_" + table, "")).get("deletes", [])
            if dels:
                _apply_deletes(table, dels)
            if rows:
                best = max((_norm_ts(r.get("updated_at")), int(r.get("id") or 0)) for r in rows)
                if best > _split_cursor(db.get_setting("sync_pull_" + table, "")):
                    db.set_setting("sync_pull_" + table, best[0] + "|" + str(best[1]))
            if dels:
                dbest = max((_norm_ts(d.get("deleted_at")), int(d.get("id") or 0)) for d in dels)
                if dbest > _split_cursor(db.get_setting("sync_pull_del_" + table, "")):
                    db.set_setting("sync_pull_del_" + table, dbest[0] + "|" + str(dbest[1]))
        except Exception as e:
            db.log_activity("sync_pull_error", table + " " + str(e)[:120], "sync")
            continue
        try:
            p_ts, p_id = _split_cursor(db.get_setting("sync_push_" + table, ""))
            c = db.conn()
            local = [dict(r) for r in c.execute(
                "SELECT %s FROM %s WHERE (COALESCE(updated_at,''), id) > (?, ?) "
                "ORDER BY COALESCE(updated_at,''), id LIMIT 500"
                % (",".join(cols), table), (p_ts, p_id)).fetchall()]
            d_ts, d_id = _split_cursor(db.get_setting("sync_push_del_" + table, ""))
            dels_local = [dict(r) for r in c.execute(
                "SELECT row_id, deleted_at FROM sync_deletes WHERE table_name=? "
                "AND (COALESCE(deleted_at,''), row_id) > (?, ?) "
                "ORDER BY COALESCE(deleted_at,''), row_id LIMIT 500", (table, d_ts, d_id)).fetchall()]
            c.close()
            batch = []
            for r in local:
                batch.append({"id": r["id"], "data": {k: (r.get(k) or "") for k in cols if k != "id"},
                              "updated_at": r.get("updated_at") or ""})
                if len(batch) >= 500:
                    _push_remote(table, batch, [])
                    batch = []
            if batch or dels_local:
                _push_remote(table, batch, [{"id": d["row_id"], "deleted_at": d["deleted_at"]}
                                            for d in dels_local])
            if local:
                last = local[-1]
                db.set_setting("sync_push_" + table,
                               (last.get("updated_at") or "") + "|" + str(last["id"]))
            if dels_local:
                last = dels_local[-1]
                db.set_setting("sync_push_del_" + table,
                               (last["deleted_at"] or "") + "|" + str(last["row_id"]))
        except Exception as e:
            db.log_activity("sync_push_error", table + " " + str(e)[:120], "sync")
