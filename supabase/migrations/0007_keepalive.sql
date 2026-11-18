-- PA keepalive: keep the PythonAnywhere worker warm so a human visit never
-- waits out a cold worker start (free tier: minutes on the first request).
-- Every 5 min: one tiny HTTP ping to /healthz (no auth, no DB, ~50-byte reply).
-- Cost: ~288 tiny HTTP calls/day; zero edge-function invocations, no egress
-- budget impact. Rows land in net._http_response and are swept by the
-- existing campusmail-ai-pump job within a minute.
-- Change interval:  SELECT cron.alter_job('pa-keepalive', 'schedule', '* * * * *');
-- Stop:             SELECT cron.unschedule('pa-keepalive');
-- Recent pings:     SELECT start_time, status, return_message, duration
--                    FROM cron.job_run_details
--                    WHERE jobid = (SELECT jobid FROM cron.job WHERE jobname='pa-keepalive')
--                    ORDER BY start_time DESC LIMIT 10;

ALTER TABLE relay_config ADD COLUMN IF NOT EXISTS pa_url TEXT
  NOT NULL DEFAULT 'https://aicampusmail.pythonanywhere.com';

SELECT cron.unschedule('pa-keepalive')
 WHERE EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'pa-keepalive');
SELECT cron.schedule('pa-keepalive', '*/5 * * * *', $cron$
  DO $body$
  BEGIN
    PERFORM net.http_post(
      url                  := (SELECT pa_url FROM relay_config LIMIT 1) || '/healthz',
      body                 := '{"ping": "supabase-cron"}'::jsonb,
      headers              := jsonb_build_object('Content-Type', 'application/json'),
      timeout_milliseconds := 15000
    );
  END;
  $body$;
$cron$);
