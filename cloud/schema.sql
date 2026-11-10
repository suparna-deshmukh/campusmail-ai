-- D1 schema for the MailForge cloud relay
CREATE TABLE IF NOT EXISTS emails (
  local_id     INTEGER PRIMARY KEY,
  account_id   INTEGER DEFAULT 0,
  to_addr      TEXT,
  cc           TEXT DEFAULT '',
  subject      TEXT,
  body_r2_key  TEXT,
  body_text    TEXT,
  attachments  TEXT DEFAULT '[]',
  status       TEXT DEFAULT 'scheduled',
  scheduled_at TEXT,
  batch_name   TEXT DEFAULT '',
  campaign_name TEXT DEFAULT '',
  is_reminder  INTEGER DEFAULT 0,
  serial       TEXT DEFAULT '',
  error        TEXT DEFAULT '',
  sent_at      TEXT,
  message_id   TEXT DEFAULT '',
  updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_due   ON emails(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_updt  ON emails(updated_at);

-- cached OAuth access token per account (refreshed on demand)
CREATE TABLE IF NOT EXISTS token_cache (
  account_id   INTEGER PRIMARY KEY,
  access_token TEXT,
  expiry_epoch INTEGER DEFAULT 0
);
