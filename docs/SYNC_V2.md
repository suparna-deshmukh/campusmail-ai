# Sync protocol v2 — outbox/rev, guardrails, deployment

## Why
The v1 page-scan pull (2,000-row pages every 45 s) plus renumber-on-insert created a
sync feedback loop that burned 348 GB of egress in ~4 days (see
`docs/SYNC_INIDENT_FORENSICS.md` — typo: SYNC_INCIDENT_FORENSICS.md). v2 makes
burn-out structurally impossible:

- **Diff-only pull**: each instance pulls only changes made by the *other* instance
  with `rev > last_acked_rev`. Empty day = ~100-byte responses.
- **Monotonic rev = LWW key**: the relay assigns a global rev per accepted mutation;
  instance clocks never decide conflicts.
- **Explicit row ids**: remote rows are applied with their remote id; non-positive
  ids are rejected (v1's `-71` junk cannot recur).
- **Tombstones win**: a row is never resurrected by a copy older than its tombstone.
- **Guardrails enforced in the relay** (old clients cannot bypass them):
  - version handshake — clients below `min_client_version` get **426** and their
    pushes are rejected;
  - per-pull caps: ≤ 500 rows **and** ≤ 256 KB (`truncated: true` to page);
  - per-instance rolling 24 h: ≤ 5,000 pushed rows, ≤ 20 MB pushed, ≤ 20 MB pulled
    → **429** + client backoff;
  - per-table cap: ≤ 10,000 `sync_rows` per table → **507**;
  - project rolling 24 h: ≤ 100 MB total sync egress → **503** kill;
  - `sync_enabled` kill switch (config row) → **503** on all sync endpoints;
  - pg_cron daily purge of tombstoned `sync_rows` + 7-day tombstone/outbox retention.

## Endpoints (relay edge function)
| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/sync/push` | accept local mutations `{version, instance, rows:[{table,id,data(+updated_at_ms)}], deletes:[{table,row_id,deleted_at_ms}]}` → `{ok, accepted, rev}` |
| GET  | `/api/sync/changes?instance=X&since_rev=N&version=V` | outbox changes by others, rev > N → `{changes, next_rev, truncated}`. `version` is required — missing/old → **426** (so an idle outdated client also learns to stop) |
| GET  | `/api/sync/bootstrap?instance=X&table=T` | one-time full snapshot (migration/resync), logged, outside the daily egress budget |
| GET  | `/api/sync/status` | version, enabled, per-instance counters, table counts, outbox depth, caps, project egress |
| *    | `/api/sync/pull[,...]` (v1) | retired → **426** so old clients back off |

Email cloud-sending endpoints (`/api/emails`, `/api/status`, `/api/pump`) are
unchanged.

## Client (sync_relay.py)
- **Versioning**: every synced table carries `updated_at_ms` (epoch ms). Triggers
  stamp it on every insert/update; `_migrate` backfills existing rows by parsing
  `updated_at` as UTC, clamping anything that parses into the future to "just now"
  (wall-clock demo strings included) so real stamps always sort after legacy rows.
- **Push**: per-row watermarks in the local `sync_pushed` table
  (`table, row_id, kind=row|del, version_ms`). A row is pushed while its current
  version exceeds its last-pushed version — immune to version regressions that
  break max-tuple cursors (a new row may legally carry a slightly older ms than
  an older row). Batches of 200 rows. Payloads keep real values: `0`/`False`/`null`
  are never coerced to `""`.
- **Pull**: applies parents-first (DELETE+INSERT so the local `updated_at` trigger
  does not re-stamp and echo), acks the rev watermark, and marks applied changes
  in `sync_pushed` so they are never re-pushed back to their origin (no echo loop).
- Hard-delete propagation: every hard delete (contacts, templates, sheets, and a
  campaign's children — its batches **and** emails) records a tombstone via
  `sync_relay.record_delete`; tombstones win over stale copies (no resurrection).
- Skips the pull when nothing changed locally and the last pull is < 90 s old.
- Exponential backoff 60 s → 30 min on repeated failures; **426 (push or pull)
  halts** the instance (logged as `sync_version_rejected`) — old code can never
  pollute.
- Never raises: a failed tick logs `sync_error` and backs off; the host
  process/thread is never taken down by sync.
- Every non-trivial tick logs `sync_tick` (rows in/out, rev range) to activity.

## Deployment (run on the machine with Supabase CLI + project access)
```bash
cd campusmail-ai
supabase login                     # once, opens browser
supabase link --project-ref xlwcrywcbsqpsjhngegz   # old project (or the new one)
supabase db push                   # applies migrations/0004_sync_v2.sql
supabase functions deploy relay --no-verify-jwt
```
Then, per instance (laptop + PA):
**Deploy in this order** (clients first, relay second):
```bash
# 1. every instance: laptop → git pull + restart 5001; PA → git pull + Reload
# 2. then: supabase db push && supabase functions deploy relay --no-verify-jwt
```
The new relay rejects pulls without a `version` param with 426; the new clients
always send it. An old client against the new relay therefore halts (safe, no
junk) until its instance is updated.

First tick after deploy: each instance pushes its full current state once
(≈ 200 rows each — this is the one-time bootstrap), then converges; idle pulls
are ~90-byte responses.

## Migration to a new Supabase project (when the new key is available)
1. Deploy the same `functions/relay/index.ts` + `migrations/0004_sync_v2.sql` to
   the new project; create fresh `RELAY_SECRET`.
2. On the laptop (source of truth, clean DB): set new `cloud_relay_url` /
   `cloud_relay_secret` in Settings, then reset the sync state
   (`DELETE FROM settings WHERE key IN ('sync_v2_rev','sync_fail_count')`,
   `DELETE FROM sync_pushed`, `DELETE FROM sync_deletes`), restart 5001 →
   full state bootstrap (every row is un-pushed, so the first tick pushes all).
3. PA — order matters: `git pull` → with the app **stopped**, run
   `python scripts/purge_stale.py --yes` (deletes PA rows that are not in the
   laptop-seeded relay set, e.g. incident-era junk) → set the same relay
   settings → restart. Purging against an *empty* relay would delete everything,
   so the laptop bootstrap (step 2) must be done first.
4. Verify: both instances show 82 contacts / 102 live emails (50 sent Diwali) /
   4 campaigns / 4 batches / 3 sheets; delete a contact + campaign on one side →
   gone on the other within 2 ticks.
5. After 24 h of clean operation: delete the old project.

## Verification (mock-relay suite + app-level soak)
Mock-relay protocol suite (all pass, this commit):
- S1 bootstrap: 200 rows each way, zero re-push on idle ticks
- S2/S3: add/delete/edit on both sides propagate, incl. campaign delete with
  1 batch + 50 email children tombstoned
- S4: stale copy of a tombstoned row does NOT resurrect; stale downgrade of a
  live row is rejected; a genuinely newer copy wins on both sides
- S5: relay min_version+1 → 426 on push *and* pull → client halts, no-op ticks
- S6: instance 24h row budget → exactly 5,000 rows then **429**; per-table cap →
  exactly 10,000 rows then **507**
- S7: sha1 over all synced columns identical on both instances, all 6 tables
- S8: idle pull = 87-byte response; pull without `version` → 426
- App-level: API create → pushed + `sync_tick` logged; API delete → tombstone
  pushed, relay row removed; `/api/sync/status` clean

Soak (two full app instances, 5099/5100, mock relay, started 2026-11-14 09:00 IST,
log `/tmp/soak-v2.log`): first 15 min — hash parity EQUAL every 4 min, chaos
mutation converged < 100 s, fail counts 0, idle egress ≈ 420 B / 4 min
(≈ 7 KB/h, ~1/600 of the 100 MB/24 h budget; the v1 incident ran ~36 GB/h).
