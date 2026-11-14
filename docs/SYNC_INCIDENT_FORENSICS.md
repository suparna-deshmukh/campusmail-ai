# Supabase egress incident — forensics & findings (old project xlwcrywcbsqpsjhngegz)

Date: 2026-11-14. Scope: verify all known issues, record evidence, cleanup performed.

## Egress
- 348.11 GB over ~4 days vs 5 GB Free Plan (343.11 GB overage). Daily: Nov 10 ~0,
  Nov 11 ~25 GB, **Nov 12 166.6 GB (peak, ~1.9 MB/s sustained 24/7)**, Nov 13 ~120 GB,
  Nov 14 ~20 GB (partial).
- Timeline: relay deployed Nov 11 (b693aba); peak days predate/straddle the two
  cursor/tombstone client fixes (dbc6ed3 11-13 14:33, 537a4dd 11-13 15:15). After
  those landed, daily egress dropped sharply.

## Verified root cause: sync feedback loop (renumber + page-scan)
1. **Renumber-on-insert (confirmed)**: old `_apply_rows` inserted relay rows with
   NULL id -> SQLite autoincrement -> pushed back as NEW rows. Evidence: laptop local
   contacts were a contiguous autoincrement block (ids 2319-17720, 15,320 rows) of
   relay junk; PA renumbered campaigns across rounds (19-25 -> 26-32 -> 33-39).
2. **Page-scan pull (confirmed in code)**: `ORDER BY updated_at, row_id LIMIT 2000`,
   cursor to max returned; continuous new-row inflow (from #1) meant both instances
   pulled full ~0.5-0.6 MB pages every 45 s for ~3 days. Committed-code ceiling
   ~2-3 GB/day; observed 166 GB/day -> deployed relay/client loop was worse
   (see open items).
3. **Stuck push (confirmed in activity log)**: `sync_push_error contacts The read
   operation timed out` repeated for days (30 s timeout, 500-row batches).
   `sync_push_contacts` cursor was NEVER set: the 82 real contacts and 1,182 contact
   tombstones never reached the relay (relay `sync_deletes` contacts was 0).
4. **Graveyard row store (confirmed)**: relay `sync_rows` kept tombstoned rows
   forever (2,450 contact rows + 139 deleted email rows observed post-cleanup).
5. **Naive-timestamp LWW (confirmed live)**: DB stores naive strings (seed = IST,
   SQLite trigger = UTC); relay re-stamps via `new Date(...).toISOString()` (UTC);
   `_norm_ts` compared strings with no timezone conversion. Observed: 50 batch-15
   "Warm Diwali wishes" emails repeatedly resurrected deleted -> sent because the
   stale cloud copy (06:59:54 IST interpreted as UTC) "looked newer" than fresh
   local deletes. Fixed with relay-queue purge + terminal-deleted guard + winning
   tombstone timestamps; stable across multiple ticks.
6. **Stale junk seed (confirmed)**: PA ran the pre-wipe DB (2,314 contacts, old
   campaigns 1-7/12-18, old batches, old emails); laptop even carried malformed
   email id = -71 (evidence the new protocol must reject non-positive ids).
7. **No budgets/caps/alerts (confirmed in code)**: none in client or relay.
8. **No version handshake (confirmed)**: old-code instances could (and did) pollute
   indefinitely.
9. **Deploy drift (confirmed)**: laptop server ran stale in-memory code for days
   (its pull cursor `2026-11-13T09:54:25.000Z` is a format no committed version
   writes); PA never git-pulled; no version/health endpoint existed.
10. **Assets (audited)**: `data/attachments/` empty; all `batches.attachments`
    empty; no base64 images in any `emails.body_html` or template; app has no
    upload path to the `campusmail-files` bucket (bucket created 2026-11-11,
    private, no size cap). So the egress was sync pages, not assets. The cloud
    send path base64-encodes batch attachments into the /api/emails queue payload
    (bounded by the 15 MB/batch app limit, only when attachments exist).

## Cleanup performed on the old project (2026-11-14, service key, physical)
- `sync_rows`: purged 2,450 junk contact rows (kept exactly the 82 real);
  campaigns/batches/sheets clean (3/4/3 rows); emails converged (102 live).
- `sync_deletes`: 17,634 contact tombstones + 35 campaigns + 18 batches + 2 sheets.
- `relay_emails` send queue: purged 64 stale rows (50 sent + 14 deleted + probe)
  that were causing the batch-15 resurrection.
- Laptop DB: 15,320 junk contact rows + email id -71 removed; batch-15 kept
  soft-deleted with a winning timestamp; verified stable across multiple ticks.

## Open manual items (dashboard only, before decommissioning — ~10-30 min)
The machine's supabase CLI is unauthenticated (whoami 403), so these need the
dashboard:
1. Edge Functions -> relay: request counts per endpoint, avg response size,
   active deploy timestamp vs committed code (closes the magnitude gap, issue #10).
2. Database -> Cron: pg_cron `sync`/pump job run history.
3. Storage -> campusmail-files: object inventory (service-key list API returned
   400/404; expected near-empty per code audit).

## Resolution
Guardrailed relay v2 (outbox/rev, version handshake, budgets, caps, kill switch,
tombstone purge) + rebuilt client + migration to a NEW Supabase project.
