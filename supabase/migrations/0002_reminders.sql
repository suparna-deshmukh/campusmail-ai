-- reminder support: the cloud renders nothing; the platform pushes pre-rendered
-- reminder subject/body. The cloud sends reminder N when
--   sent_at + reminder_days * (reminder_count + 1) days <= now()
-- and the parent has not been replied to.
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS reminder_days    INTEGER DEFAULT 0;
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS reminder_max     INTEGER DEFAULT 0;
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS reminder_subject TEXT DEFAULT '';
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS reminder_body    TEXT DEFAULT '';
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS replied_at       TIMESTAMPTZ;
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS reminder_count   INTEGER DEFAULT 0;
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS parent_local_id  INTEGER DEFAULT 0;
