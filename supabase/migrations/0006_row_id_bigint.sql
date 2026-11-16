-- Sync row ids can be epoch-ms based (activity: ~1.8e15), which overflow the
-- 32-bit INTEGER the relay tables were first created with. Widen to BIGINT.
ALTER TABLE sync_rows DROP CONSTRAINT IF EXISTS sync_rows_pkey;
ALTER TABLE sync_rows ALTER COLUMN row_id TYPE BIGINT;
ALTER TABLE sync_rows ADD PRIMARY KEY (table_name, row_id);

ALTER TABLE sync_deletes DROP CONSTRAINT IF EXISTS sync_deletes_pkey;
ALTER TABLE sync_deletes ALTER COLUMN row_id TYPE BIGINT;
ALTER TABLE sync_deletes ADD PRIMARY KEY (table_name, row_id);

ALTER TABLE sync_outbox ALTER COLUMN row_id TYPE BIGINT;
