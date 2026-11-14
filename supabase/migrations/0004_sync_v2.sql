-- Sync protocol v2: outbox/rev change queue, epoch-ms LWW, budgets, caps, kill switch.
-- See docs/SYNC_V2.md. Safe to run against an existing project (idempotent).

-- current-state store: epoch-ms column for unambiguous per-row LWW tiebreaks
ALTER TABLE sync_rows ADD COLUMN IF NOT EXISTS updated_at_ms BIGINT NOT NULL DEFAULT 0;

-- tombstones: epoch-ms + assigning rev
ALTER TABLE sync_deletes ADD COLUMN IF NOT EXISTS deleted_at_ms BIGINT NOT NULL DEFAULT 0;
ALTER TABLE sync_deletes ADD COLUMN IF NOT EXISTS del_rev BIGINT NOT NULL DEFAULT 0;

-- per-instance change outbox (monotonic global rev = LWW key)
CREATE TABLE IF NOT EXISTS sync_outbox (
  rev          BIGSERIAL PRIMARY KEY,
  instance_id  TEXT NOT NULL,
  table_name   TEXT NOT NULL,
  row_id       INTEGER NOT NULL CHECK (row_id > 0),
  op           TEXT NOT NULL CHECK (op IN ('upsert','delete')),
  payload      JSONB NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outbox_pull_idx ON sync_outbox (instance_id, rev);

-- per-instance ack watermark
CREATE TABLE IF NOT EXISTS sync_acked (
  instance_id TEXT PRIMARY KEY,
  last_rev    BIGINT NOT NULL DEFAULT 0,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- per-instance + project rolling-24h counters
CREATE TABLE IF NOT EXISTS sync_metrics (
  instance_id       TEXT PRIMARY KEY,
  rows_pushed_24h   INTEGER NOT NULL DEFAULT 0,
  bytes_pushed_24h  BIGINT  NOT NULL DEFAULT 0,
  rows_pulled_24h   INTEGER NOT NULL DEFAULT 0,
  bytes_pulled_24h  BIGINT  NOT NULL DEFAULT 0,
  bootstrap_bytes   BIGINT  NOT NULL DEFAULT 0,
  window_start      TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error        TEXT NOT NULL DEFAULT '',
  last_error_at     TIMESTAMPTZ,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- relay-side configuration (kill switch, budgets, caps, min client version)
CREATE TABLE IF NOT EXISTS sync_config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT INTO sync_config (key, value) VALUES
  ('min_client_version',      '2'),
  ('sync_enabled',            '1'),
  ('project_egress_mb_24h',   '100'),
  ('instance_rows_24h',       '5000'),
  ('instance_bytes_push_24h', '20971520'),
  ('instance_bytes_pull_24h', '20971520'),
  ('pull_max_rows',           '500'),
  ('pull_max_bytes',          '262144'),
  ('rows_per_table_cap',      '10000'),
  ('tombstone_retention_days','7')
ON CONFLICT (key) DO NOTHING;

-- daily graveyard hygiene: tombstoned rows, old tombstones, old outbox entries
SELECT cron.schedule(
  'sync-v2-tombstone-purge',
  '17 3 * * *',
  $purge$
    DELETE FROM sync_rows r
    WHERE EXISTS (SELECT 1 FROM sync_deletes d
                  WHERE d.table_name = r.table_name AND d.row_id = r.row_id
                    AND r.updated_at_ms <= d.deleted_at_ms);
    DELETE FROM sync_deletes
    WHERE deleted_at < now() - interval '7 days';
    DELETE FROM sync_outbox WHERE created_at < now() - interval '7 days';
    DELETE FROM sync_metrics m
    WHERE m.instance_id <> 'project'
      AND NOT EXISTS (SELECT 1 FROM sync_acked a WHERE a.instance_id = m.instance_id)
      AND m.updated_at < now() - interval '7 days';
  $purge$
);
