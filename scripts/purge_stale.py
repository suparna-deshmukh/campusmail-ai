#!/usr/bin/env python3
"""One-shot cleanup for an instance whose local DB predates a relay reset.

Deletes local rows in the synced tables whose id is NOT in the relay's
authoritative row set (fetched via /api/sync/bootstrap), skipping ids that a
kept row still references (FK protection). Children are processed before
their parents so stale children cannot protect stale parents.

Usage (on the instance to clean, e.g. PythonAnywhere right after migration):
  python scripts/purge_stale.py            # dry run: shows what would be deleted
  python scripts/purge_stale.py --yes      # actually delete

Reads cloud_relay_url / cloud_relay_secret from the app settings, so run it
with the app's data/ dir.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402

# children first, then parents
TABLE_ORDER = ["emails", "batches", "sheets", "email_templates", "campaigns", "contacts"]
# parent table -> [(child table, child column)]
PROTECT_REVERSE = {
    "batches": [("emails", "batch_id")],
    "contacts": [("emails", "contact_id")],
    "campaigns": [("batches", "campaign_id"), ("sheets", "campaign_id")],
    "sheets": [("batches", "sheet_id")],
    "email_templates": [("batches", "template_id"), ("batches", "reminder_template_id")],
}


def fetch_relay_set(url, secret, table):
    req = urllib.request.Request(
        url + "/api/sync/bootstrap?instance=purge-stale&table=" + table,
        headers={"x-relay-secret": secret})
    with urllib.request.urlopen(req, timeout=120) as r:
        return {row["id"] for row in json.loads(r.read().decode()).get("rows", [])}


def kept_references(c, table, relay_sets):
    """Ids of `table` still referenced by a row that will be kept."""
    keep = set()
    for child, col in PROTECT_REVERSE.get(table, []):
        if child not in relay_sets:
            continue
        for rid, val in c.execute(
                "SELECT id, %s FROM %s WHERE %s IS NOT NULL AND %s <> 0" % (col, child, col, col)):
            if val is not None and rid in relay_sets[child]:
                keep.add(val)
    return keep


def main():
    yes = "--yes" in sys.argv
    url = (db.get_setting("cloud_relay_url", "") or "").strip().rstrip("/")
    secret = (db.get_setting("cloud_relay_secret", "") or "").strip()
    if not url or not secret:
        print("No cloud relay configured (settings cloud_relay_url / cloud_relay_secret).")
        return 1
    c = db.conn()
    total = 0
    try:
        relay_sets = {t: fetch_relay_set(url, secret, t) for t in TABLE_ORDER}
        for table in TABLE_ORDER:
            stale = [r[0] for r in c.execute("SELECT id FROM %s" % table)
                     if r[0] not in relay_sets[table]]
            keep = kept_references(c, table, relay_sets)
            stale = [i for i in stale if i not in keep]
            if not stale:
                continue
            print("%-16s stale: %d   e.g. %s" % (table, len(stale), stale[:8]))
            total += len(stale)
            if yes:
                c.executemany("DELETE FROM %s WHERE id=?" % table, [(i,) for i in stale])
        if yes:
            c.commit()
        print("total stale rows: %d" % total)
        print("DRY RUN - re-run with --yes to delete." if not yes else "deleted.")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
