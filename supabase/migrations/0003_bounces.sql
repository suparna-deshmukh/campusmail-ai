-- bounce tracking: recipient address wrong or mailbox does not exist
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS bounced_at TIMESTAMPTZ;
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS bounce_reason TEXT DEFAULT '';

-- delivery-failure notifications already processed by the relay
CREATE TABLE IF NOT EXISTS relay_processed_dsn (
  message_id   TEXT PRIMARY KEY,
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
