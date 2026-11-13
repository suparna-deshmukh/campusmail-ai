-- bounce tracking: recipient address wrong or mailbox does not exist
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS bounced_at TIMESTAMPTZ;
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS bounce_reason TEXT DEFAULT '';

-- delivery-failure notifications already processed by the relay
CREATE TABLE IF NOT EXISTS relay_processed_dsn (
  message_id   TEXT PRIMARY KEY,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- two-way team DB sync store (row-level last-write-wins across instances)
CREATE TABLE IF NOT EXISTS sync_rows (
  table_name TEXT NOT NULL,
  row_id     INTEGER NOT NULL,
  data       JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  instance   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (table_name, row_id)
);

-- delete propagation (tombstones) for the two-way sync
CREATE TABLE IF NOT EXISTS sync_deletes (
  table_name TEXT NOT NULL,
  row_id     INTEGER NOT NULL,
  deleted_at TIMESTAMPTZ NOT NULL,
  instance   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (table_name, row_id)
);
