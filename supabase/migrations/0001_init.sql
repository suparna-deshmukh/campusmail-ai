-- CampusMail AI cloud relay schema (Supabase / Postgres)
CREATE TABLE IF NOT EXISTS relay_emails (
  local_id      INTEGER PRIMARY KEY,
  account_id    INTEGER DEFAULT 0,
  to_addr       TEXT,
  cc            TEXT DEFAULT '',
  subject       TEXT,
  body_text     TEXT,
  attachments   TEXT DEFAULT '[]',
  status        TEXT DEFAULT 'scheduled',   -- scheduled | sending | sent | error | cancelled | drafted | deleted
  scheduled_at  TIMESTAMPTZ,
  batch_name    TEXT DEFAULT '',
  campaign_name TEXT DEFAULT '',
  is_reminder   INTEGER DEFAULT 0,
  serial        TEXT DEFAULT '',
  error         TEXT DEFAULT '',
  sent_at       TIMESTAMPTZ,
  message_id    TEXT DEFAULT '',
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_relay_due  ON relay_emails(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_relay_updt ON relay_emails(updated_at);

-- cached OAuth access token (refreshed on demand by the relay)
CREATE TABLE IF NOT EXISTS relay_token_cache (
  account_id   INTEGER PRIMARY KEY DEFAULT 0,
  access_token TEXT,
  expiry_epoch BIGINT DEFAULT 0
);

-- relay runtime config (function URL + shared secret for the cron ping)
CREATE TABLE IF NOT EXISTS relay_config (
  relay_url    TEXT NOT NULL,
  relay_secret TEXT NOT NULL
);

CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;

-- every minute: ping the relay function to pump due emails (fire-and-forget)
SELECT cron.unschedule('campusmail-ai-pump')
 WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'campusmail-ai-pump');
SELECT cron.schedule('campusmail-ai-pump', '* * * * *', $cron$
  DO $body$
  BEGIN
    PERFORM net.http_post(
      url                  := (SELECT relay_url || '/api/pump' FROM relay_config LIMIT 1),
      body                 := '{}'::jsonb,
      headers              := jsonb_build_object('x-relay-secret', (SELECT relay_secret FROM relay_config LIMIT 1)),
      timeout_milliseconds := 55000
    );
    DELETE FROM net._http_response;
  END;
  $body$;
$cron$);
