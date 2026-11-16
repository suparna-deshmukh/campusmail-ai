-- Undo window (spec 7.7): held "send now" batches must not be released by
-- the cloud pump before the local instance had a chance to release or undo.
-- Held rows are pushed with undo_hold = true; the pump skips them until
-- scheduled_at is at least 60s in the past.
ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS undo_hold BOOLEAN NOT NULL DEFAULT FALSE;
