// CampusMail AI cloud relay - Supabase Edge Function edition.
// Sends scheduled emails (and auto follow-up reminders) from the cloud, 24/7.
// Secrets (set via `supabase secrets set`): DATABASE_URL, RELAY_SECRET,
// GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, GMAIL_SENDER_EMAIL.
import postgres from "npm:postgres@3.4.5";

const SECRET = Deno.env.get("RELAY_SECRET") || "";
const DB_URL = Deno.env.get("DATABASE_URL") || "";
const CLIENT_ID = Deno.env.get("GMAIL_CLIENT_ID") || "";
const CLIENT_SECRET = Deno.env.get("GMAIL_CLIENT_SECRET") || "";
const REFRESH_TOKEN = Deno.env.get("GMAIL_REFRESH_TOKEN") || "";
const SENDER = Deno.env.get("GMAIL_SENDER_EMAIL") || "";
const PUMP_LIMIT = 6; // sends per minute (function 60s limit)

const sql = postgres(DB_URL, { max: 4, idle_timeout: 15 });

let schemaReady = false;
async function ensureSchema() {
  await sql`ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS bounced_at TIMESTAMPTZ`;
  await sql`ALTER TABLE relay_emails ADD COLUMN IF NOT EXISTS bounce_reason TEXT DEFAULT ''`;
  await sql`CREATE TABLE IF NOT EXISTS relay_processed_dsn (
    message_id   TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now())`;
  await sql`CREATE TABLE IF NOT EXISTS sync_rows (
    table_name TEXT NOT NULL,
    row_id     INTEGER NOT NULL,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    instance   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (table_name, row_id))`;
  const dtype: any[] = await sql`SELECT data_type FROM information_schema.columns
    WHERE table_name = 'sync_rows' AND column_name = 'data'`;
  if (dtype.length && dtype[0].data_type && dtype[0].data_type.toLowerCase() !== "jsonb") {
    await sql`ALTER TABLE sync_rows ALTER COLUMN data TYPE JSONB USING data::jsonb`;
  }
  await sql`CREATE TABLE IF NOT EXISTS sync_deletes (
    table_name TEXT NOT NULL,
    row_id     INTEGER NOT NULL,
    deleted_at TIMESTAMPTZ NOT NULL,
    instance   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (table_name, row_id))`;
}

function json(obj: unknown, code = 200): Response {
  return new Response(JSON.stringify(obj), {
    status: code,
    headers: { "Content-Type": "application/json" },
  });
}

Deno.serve(async (req: Request) => {
  const url = new URL(req.url);
  let p = url.pathname.replace(/^\/functions\/v1\//, "").replace(/^\/relay(\/|$)/, "$1");
  p = "/" + p.replace(/^\/+/, "");
  if (p === "/" || p === "/health") {
    return json({ ok: true, relay: "supabase" });
  }
  if (req.headers.get("x-relay-secret") !== SECRET || !SECRET) {
    return json({ ok: false, error: "bad secret" }, 403);
  }
  try {
    if (req.method === "POST" && p === "/api/emails") return await upsertEmails(req);
    if (req.method === "GET" && p === "/api/status") return await statusPull();
    if (req.method === "POST" && p === "/api/pump") return await pump();
    if (req.method === "POST" && p === "/api/sync/push") return await syncPush(req);
    if (req.method === "GET" && p === "/api/sync/changes") return await syncChanges(new URL(req.url));
    if (req.method === "GET" && p === "/api/sync/bootstrap") return await syncBootstrap(new URL(req.url));
    if (req.method === "GET" && p === "/api/sync/status") return await syncStatus();
    if (p === "/api/sync/pull" || p === "/api/sync/pull/deletes") return await syncLegacyRejected();
    return json({ ok: false, error: "not found" }, 404);
  } catch (e) {
    return json({ ok: false, error: String((e as Error).message || e) }, 500);
  }
});

// ---------------- queue sync ----------------

async function upsertEmails(req: Request): Promise<Response> {
  const d = await req.json();
  const list: any[] = Array.isArray((d as any).emails) ? (d as any).emails : [(d as any)];
  let n = 0;
  for (const e of list) {
    if (!e || !e.local_id) continue;
    await sql`
      INSERT INTO relay_emails(local_id, account_id, to_addr, cc, subject, body_text,
        attachments, status, scheduled_at, batch_name, campaign_name, is_reminder, serial,
        error, reminder_days, reminder_max, reminder_subject, reminder_body, replied_at,
        bounced_at, bounce_reason, updated_at)
      VALUES (${e.local_id}, ${e.account_id || 0}, ${e.to_addr || ""}, ${e.cc || ""}, ${e.subject || ""},
        ${e.body_html || ""}, ${JSON.stringify(e.attachments || [])}, ${e.status || "scheduled"},
        ${e.scheduled_at ? new Date(e.scheduled_at) : null}, ${e.batch_name || ""},
        ${e.campaign_name || ""}, ${e.is_reminder ? 1 : 0}, ${String(e.serial || "")},
        ${e.error || ""}, ${e.reminder_days || 0}, ${e.reminder_max || 0},
        ${e.reminder_subject || ""}, ${e.reminder_body || ""},
        ${e.replied_at ? new Date(e.replied_at) : null},
        ${e.bounced_at ? new Date(e.bounced_at) : null}, ${e.bounce_reason || ""}, now())
      ON CONFLICT (local_id) DO UPDATE SET
        account_id = excluded.account_id, to_addr = excluded.to_addr, cc = excluded.cc,
        subject = excluded.subject, body_text = excluded.body_text,
        attachments = excluded.attachments, status = excluded.status,
        scheduled_at = excluded.scheduled_at, batch_name = excluded.batch_name,
        campaign_name = excluded.campaign_name, is_reminder = excluded.is_reminder,
        serial = excluded.serial, error = excluded.error,
        reminder_days = excluded.reminder_days, reminder_max = excluded.reminder_max,
        reminder_subject = excluded.reminder_subject, reminder_body = excluded.reminder_body,
        replied_at = excluded.replied_at,
        bounced_at = CASE WHEN excluded.bounced_at IS NOT NULL THEN excluded.bounced_at
                   ELSE relay_emails.bounced_at END,
        bounce_reason = CASE WHEN excluded.bounce_reason IS NOT NULL AND excluded.bounce_reason != ''
                   THEN excluded.bounce_reason ELSE relay_emails.bounce_reason END,
        sent_at = CASE WHEN excluded.status = 'sent' THEN now() ELSE relay_emails.sent_at END,
        message_id = CASE WHEN excluded.status = 'sent' AND excluded.message_id IS NOT NULL
                   AND excluded.message_id != '' THEN excluded.message_id ELSE relay_emails.message_id END,
        updated_at = now()
    `;
    n++;
  }
  return json({ ok: true, upserted: n });
}

async function statusPull(): Promise<Response> {
  const rows: any[] = await sql`
    SELECT local_id, status, sent_at, message_id, error, updated_at,
           is_reminder, parent_local_id, to_addr, subject, reminder_count, body_text,
           bounced_at, bounce_reason
    FROM relay_emails
    WHERE updated_at >= now() - interval '10 days'
    ORDER BY updated_at DESC LIMIT 500`;
  return json({ ok: true, emails: rows.map((r) => ({
    local_id: r.local_id, status: r.status,
    sent_at: r.sent_at ? new Date(r.sent_at).toISOString() : null,
    message_id: r.message_id || "", error: r.error || "",
    updated_at: new Date(r.updated_at).toISOString(),
    is_reminder: !!r.is_reminder, parent_local_id: r.parent_local_id || 0,
    to_addr: r.to_addr || "", subject: r.subject || "",
    reminder_count: r.reminder_count || 0,
    bounced_at: r.bounced_at ? new Date(r.bounced_at).toISOString() : null,
    bounce_reason: r.bounce_reason || "",
    body_html: r.is_reminder ? (r.body_text || "") : undefined,
  })) });
}

// ---------------- sending ----------------

async function pump(): Promise<Response> {
  if (!schemaReady) {
    try { await ensureSchema(); schemaReady = true; } catch (e) { /* non-fatal */ }
  }
  // reaper: a function killed mid-pump leaves rows stuck in 'sending'
  await sql`
    UPDATE relay_emails SET status = 'scheduled', updated_at = now()
    WHERE status = 'sending' AND updated_at < now() - interval '15 minutes'`;

  // 1) send scheduled mails that are due
  const due: any[] = await sql`
    UPDATE relay_emails SET status = 'sending', updated_at = now()
    WHERE local_id IN (
      SELECT local_id FROM relay_emails
      WHERE status = 'scheduled' AND scheduled_at IS NOT NULL AND scheduled_at <= now()
      ORDER BY scheduled_at LIMIT ${PUMP_LIMIT} FOR UPDATE SKIP LOCKED
    )
    RETURNING *`;
  let sent = 0, failed = 0;
  for (const e of due) {
    try {
      const raw = buildRaw(e.to_addr, e.subject, e.body_text || "", parseAtts(e.attachments), parseCc(e.cc));
      const res = await gmailSend(raw);
      const mid = await gmailMessageId(res.id).catch(() => "");
      await sql`
        UPDATE relay_emails SET status = 'sent', sent_at = now(), message_id = ${mid || ""},
          error = '', updated_at = now() WHERE local_id = ${e.local_id}`;
      sent++;
    } catch (err) {
      failed++;
      await sql`
        UPDATE relay_emails SET status = 'error', error = ${String((err as Error).message || err).slice(0, 300)},
          updated_at = now() WHERE local_id = ${e.local_id}`;
    }
  }

  // 2) auto follow-up reminders (rendered by the platform, pushed with each mail)
  const rem: any[] = await sql`
    SELECT * FROM relay_emails
    WHERE status = 'sent' AND is_reminder = 0 AND reminder_days > 0
      AND reminder_count < reminder_max AND replied_at IS NULL
      AND sent_at IS NOT NULL
      AND sent_at + make_interval(days => reminder_days * (reminder_count + 1)) <= now()
    ORDER BY sent_at LIMIT ${PUMP_LIMIT}`;
  let remSent = 0, remFailed = 0;
  for (const p of rem) {
    try {
      const n = (p.reminder_count || 0) + 1;
      const raw = buildRaw(p.to_addr, p.reminder_subject || "(no subject)", p.reminder_body || "",
        parseAtts(p.attachments), parseCc(p.cc));
      const res = await gmailSend(raw);
      const mid = await gmailMessageId(res.id).catch(() => "");
      const rid = -(p.local_id * 10 + n);
      await sql`
        INSERT INTO relay_emails(local_id, account_id, to_addr, cc, subject, body_text,
          attachments, status, scheduled_at, batch_name, campaign_name, is_reminder, serial,
          error, sent_at, message_id, parent_local_id, updated_at)
        VALUES (${rid}, ${p.account_id}, ${p.to_addr}, ${p.cc}, ${p.reminder_subject || ""},
          ${p.reminder_body || ""}, ${p.attachments}, 'sent', NULL, ${p.batch_name},
          ${p.campaign_name}, 1, ${p.serial}, '', now(), ${mid || ""}, ${p.local_id}, now())
        ON CONFLICT (local_id) DO NOTHING`;
      await sql`
        UPDATE relay_emails SET reminder_count = ${n}, error = '', updated_at = now()
        WHERE local_id = ${p.local_id}`;
      remSent++;
    } catch (err) {
      remFailed++;
      await sql`
        UPDATE relay_emails SET error = ${String((err as Error).message || err).slice(0, 300)},
          updated_at = now() WHERE local_id = ${p.local_id}`;
    }
  }
  // 3) detect bounces: delivery failures where the recipient address is wrong
  //    or the mailbox does not exist (hard bounces only)
  let bounces = 0;
  try { bounces = await scanBounces(); } catch (e) { /* non-fatal */ }
  return json({ ok: true, due: due.length, sent, failed, reminders_sent: remSent,
    reminders_failed: remFailed, bounces });
}

// ---------------- bounce detection ----------------

const BOUNCE_Q = '(in:inbox OR in:spam) newer_than:30d (subject:"Undelivered Mail Returned to Sender" OR ' +
  'subject:"Mail Delivery Subsystem" OR subject:"Delivery Status Notification (Failure)" OR ' +
  'subject:"Delivery Failure")';
const HARD_BOUNCE_RE = /does not exist|no such user|unknown user|user unknown|mailbox (not found|unavailable|unknown|does not exist)|address (rejected|does not exist|unrecognized)|not a (valid )?(deliverable )?(mailbox|recipient)|recipient (unknown|rejected|refused|not found)|no such (mailbox|host)/i;

function extractText(payload: any): string {
  const plain: string[] = [], html: string[] = [];
  const walk = (node: any) => {
    const mime: string = (node?.mimeType || "").toLowerCase();
    const data: string = (node?.body && node.body.data) || "";
    if (data && mime.startsWith("text/")) {
      try {
        const fixed = data.replace(/=+$/, "").replace(/-/g, "+").replace(/_/g, "/");
        (mime.includes("html") ? html : plain).push(Buffer.from(fixed, "base64").toString("utf-8"));
      } catch (e) { /* skip part */ }
    }
    for (const part of node?.parts || []) walk(part);
  };
  walk(payload);
  return plain.join("\n") || html.join("\n");
}

function parseDsn(text: string): [string, string][] {
  const recips = new Set<string>();
  const reFin = /Final-Recipient:\s*[^;\n]+;\s*<?([^<>\s;]+@[^<>\s;]+)>?/gi;
  let m: RegExpExecArray | null;
  while ((m = reFin.exec(text))) recips.add(m[1].replace(/['"]/g, "").toLowerCase());
  if (!recips.size) {
    const m2 = text.match(/(?:failed|couldn'?t deliver|unable to deliver|not delivered)[^\n:]*:\s*<?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)>?/i);
    if (m2) recips.add(m2[1].toLowerCase());
  }
  if (!recips.size) return [];
  const st = text.match(/Status:\s*([0-9]\.[0-9](?:\.[0-9])?)/i);
  const code = st ? st[1] : "";
  const dm = text.match(/Diagnostic-Code:[^\n]*(?:\n[^\n]*)?/i);
  let diag = dm ? dm[0].replace(/Diagnostic-Code:\s*/i, "").replace(/\s+/g, " ").trim() : "";
  diag = diag.replace(/^(SMTP|SMTPError)\s*;\s*/, "").slice(0, 240);
  const hard = code.startsWith("5.1") ||
    ((code.startsWith("5.7") || !code) && HARD_BOUNCE_RE.test(text + " " + diag));
  if (!hard) return [];
  const reason = ([code, diag].filter(Boolean).join(" - ").slice(0, 300)) || "Delivery failed";
  return Array.from(recips).sort().map((a): [string, string] => [a, reason]);
}

async function scanBounces(): Promise<number> {
  if (!SENDER) return 0;
  const token = await accessToken();
  const list = await fetch(
    GMAIL_API + "/users/" + encodeURIComponent(SENDER) + "/messages?maxResults=15&q=" + encodeURIComponent(BOUNCE_Q),
    { headers: { Authorization: "Bearer " + token } });
  if (!list.ok) return 0;
  const d: any = await list.json();
  let marked = 0;
  for (const m of (d.messages || []).slice(0, 10)) {
    const seen: any[] = await sql`SELECT 1 FROM relay_processed_dsn WHERE message_id = ${m.id}`;
    if (seen.length) continue;
    const r = await fetch(
      GMAIL_API + "/users/" + encodeURIComponent(SENDER) + "/messages/" + m.id + "?format=full",
      { headers: { Authorization: "Bearer " + token } });
    if (!r.ok) continue;
    const msg: any = await r.json();
    const hdrs: Record<string, string> = {};
    for (const h of (msg.payload && msg.payload.headers) || []) hdrs[(h.name || "").toLowerCase()] = h.value || "";
    const mid = (hdrs["message-id"] || m.id).replace(/[<>]/g, "");
    const text = extractText(msg.payload || {});
    for (const [addr, reason] of parseDsn(text)) {
      const upd: any[] = await sql`
        UPDATE relay_emails SET bounced_at = now(), bounce_reason = ${reason}, updated_at = now()
        WHERE local_id IN (
          SELECT local_id FROM relay_emails
          WHERE lower(to_addr) = lower(${addr}) AND status = 'sent' AND bounced_at IS NULL
            AND is_reminder = 0
          ORDER BY local_id DESC LIMIT 1)
        RETURNING local_id`;
      marked += upd.length;
    }
    await sql`INSERT INTO relay_processed_dsn(message_id) VALUES (${mid})
      ON CONFLICT (message_id) DO NOTHING`;
  }
  return marked;
}


// ---------------- two-way team DB sync (v2: outbox/rev, budgets, caps) ----------------
// Protocol: docs/SYNC_V2.md
// - Push: client sends local mutations -> relay assigns a monotonic rev per mutation,
//   upserts current state (LWW guard on updated_at_ms), records tombstones, and
//   appends each mutation to the outbox. Old instances cannot downgrade state.
// - Pull: client asks for outbox changes it has not acked (since_rev). Empty queue is
//   a ~100-byte response. Exactly-once delivery via the client's local rev watermark.
// - Guardrails (enforced HERE, not client-side): version handshake (426), kill switch
//   (503), per-instance rolling-24h row/byte budgets (429), per-table row caps (507),
//   project rolling-24h egress budget (503), per-pull row/byte caps (truncated flag).

const SYNC_TABLES = ["contacts", "emails", "batches", "campaigns", "email_templates", "sheets"];
const RELAY_VERSION = 2;

async function cfgVal(key: string, dflt: string): Promise<string> {
  try {
    const rows: any[] = await sql`SELECT value FROM sync_config WHERE key = ${key}`;
    if (rows.length) return String(rows[0].value);
  } catch (e) { /* table may not exist until migration 0004 */ }
  return dflt;
}
async function cfgInt(key: string, dflt: number): Promise<number> {
  const v = await cfgVal(key, String(dflt));
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : dflt;
}

async function syncEnabled(): Promise<boolean> {
  return (await cfgVal("sync_enabled", "1")) === "1";
}

function nowMs(): number {
  return Date.now();
}

// rolling 24h window: reset counters when the window rolls
async function windowRoll(instanceId: string): Promise<any> {
  const rows: any[] = await sql`SELECT window_start FROM sync_metrics WHERE instance_id = ${instanceId}`;
  if (!rows.length) {
    await sql`INSERT INTO sync_metrics(instance_id) VALUES (${instanceId})`;
    return { rows_pushed_24h: 0, bytes_pushed_24h: 0, rows_pulled_24h: 0, bytes_pulled_24h: 0, window_start: null };
  }
  const ws = rows[0].window_start;
  if (ws && (Date.now() - new Date(ws).getTime()) > 24 * 3600 * 1000) {
    await sql`UPDATE sync_metrics SET rows_pushed_24h = 0, bytes_pushed_24h = 0,
      rows_pulled_24h = 0, bytes_pulled_24h = 0, window_start = now(), updated_at = now()
      WHERE instance_id = ${instanceId}`;
    return { rows_pushed_24h: 0, bytes_pushed_24h: 0, rows_pulled_24h: 0, bytes_pulled_24h: 0, window_start: null };
  }
  const m: any[] = await sql`SELECT rows_pushed_24h, bytes_pushed_24h, rows_pulled_24h, bytes_pulled_24h FROM sync_metrics WHERE instance_id = ${instanceId}`;
  return m[0] || { rows_pushed_24h: 0, bytes_pushed_24h: 0, rows_pulled_24h: 0, bytes_pulled_24h: 0 };
}

async function noteError(instanceId: string, msg: string): Promise<void> {
  try {
    await sql`INSERT INTO sync_metrics(instance_id, last_error, last_error_at, updated_at)
      VALUES (${instanceId}, ${msg.slice(0, 300)}, now(), now())
      ON CONFLICT (instance_id) DO UPDATE SET last_error = excluded.last_error,
      last_error_at = now(), updated_at = now()`;
  } catch (e) { /* non-fatal */ }
}

async function projectEgressUsed(): Promise<number> {
  const m: any[] = await sql`SELECT bytes_pushed_24h, bytes_pulled_24h FROM sync_metrics WHERE instance_id = 'project'`;
  if (m.length && m[0].window_start && (Date.now() - new Date(m[0].window_start).getTime()) <= 24 * 3600 * 1000) {
    return Number(m[0].bytes_pushed_24h || 0) + Number(m[0].bytes_pulled_24h || 0);
  }
  return 0;
}
async function addProjectEgress(bytes: number): Promise<void> {
  try {
    await sql`INSERT INTO sync_metrics(instance_id, bytes_pulled_24h, window_start)
      VALUES ('project', ${bytes}, now())
      ON CONFLICT (instance_id) DO UPDATE SET
        window_start = CASE WHEN sync_metrics.window_start IS NULL
                             OR (now() - sync_metrics.window_start) > interval '24 hours'
                            THEN now() ELSE sync_metrics.window_start END,
        bytes_pulled_24h = CASE WHEN sync_metrics.window_start IS NULL
                                 OR (now() - sync_metrics.window_start) > interval '24 hours'
                                THEN ${bytes} ELSE sync_metrics.bytes_pulled_24h + ${bytes} END,
        updated_at = now()`;
  } catch (e) { /* non-fatal */ }
}

interface SyncRowIn { table: string; id: number; data: any; updated_at_ms: number; }
interface SyncDelIn { table: string; row_id: number; deleted_at_ms: number; }

async function syncPush(req: Request): Promise<Response> {
  if (!(await syncEnabled())) return json({ ok: false, error: "sync disabled (kill switch)" }, 503);
  const d: any = await req.json();
  const version: number = parseInt(String(d.version || "0"), 10);
  const minV: number = await cfgInt("min_client_version", 2);
  if (!Number.isFinite(version) || version < minV) {
    return json({ ok: false, error: "client version too old; upgrade required" }, 426);
  }
  const instance: string = String(d.instance || "").slice(0, 64);
  if (!instance) return json({ ok: false, error: "missing instance" }, 400);
  const rowsIn: any[] = Array.isArray(d.rows) ? d.rows : [];
  const delsIn: any[] = Array.isArray(d.deletes) ? d.deletes : [];

  // validate the whole batch up front (atomic accept/reject)
  for (const r of rowsIn) {
    if (!SYNC_TABLES.includes(r && r.table)) return json({ ok: false, error: "unknown table" }, 400);
    const id = Number(r && r.id);
    if (!Number.isInteger(id) || id <= 0) return json({ ok: false, error: "row id must be a positive integer" }, 400);
    if (typeof (r.data && r.data.updated_at_ms) === "undefined") r.data = { ...(r.data || {}), updated_at_ms: nowMs() };
  }
  for (const dl of delsIn) {
    if (!SYNC_TABLES.includes(dl && dl.table)) return json({ ok: false, error: "unknown table (delete)" }, 400);
    const id = Number(dl && dl.row_id);
    if (!Number.isInteger(id) || id <= 0) return json({ ok: false, error: "delete row id must be a positive integer" }, 400);
  }
  const nMut = rowsIn.length + delsIn.length;
  if (nMut > 2000) return json({ ok: false, error: "too many mutations" }, 413);

  // project egress kill switch
  const projBudgetBytes: number = (await cfgInt("project_egress_mb_24h", 100)) * 1024 * 1024;
  const projUsed = await projectEgressUsed();
  if (projUsed >= projBudgetBytes) {
    await noteError(instance, "project egress budget exhausted");
    return json({ ok: false, error: "project 24h egress budget exhausted; sync halted" }, 503);
  }

  // per-instance rolling-24h budgets
  const w = await windowRoll(instance);
  const rowBudget: number = await cfgInt("instance_rows_24h", 5000);
  const pushByteBudget: number = await cfgInt("instance_bytes_push_24h", 20971520);
  // conservative per-mutation estimate (payload already consumed by req.json())
  const estBytes = Math.max(rowsIn.length + delsIn.length, 0) * 320 + 64;
  if (Number(w.rows_pushed_24h || 0) + nMut > rowBudget) {
    await noteError(instance, "instance 24h row budget exhausted");
    return json({ ok: false, error: "instance 24h row budget exhausted", retry_after_s: 60 }, 429);
  }
  if (Number(w.bytes_pushed_24h || 0) + estBytes > pushByteBudget) {
    await noteError(instance, "instance 24h push byte budget exhausted");
    return json({ ok: false, error: "instance 24h push byte budget exhausted", retry_after_s: 60 }, 429);
  }

  // per-table caps
  const cap: number = await cfgInt("rows_per_table_cap", 10000);
  const tables = Array.from(new Set([...rowsIn.map((r) => r.table), ...delsIn.map((d2) => d2.table)]));
  for (const t of tables) {
    const c: any[] = await sql`SELECT COUNT(*)::int AS n FROM sync_rows WHERE table_name = ${t}`;
    const incoming = rowsIn.filter((r) => r.table === t).length;
    if (Number(c[0] && c[0].n || 0) + incoming > cap) {
      await noteError(instance, "table cap exceeded: " + t);
      return json({ ok: false, error: "table row cap exceeded for " + t }, 507);
    }
  }

  // accept: upserts
  let accepted = 0;
  let lastRev = 0;
  const ts = nowMs();
  for (const r of rowsIn) {
    const id = Number(r.id);
    const ms = Number(r.data.updated_at_ms) || ts;
    const tomb: any[] = await sql`SELECT deleted_at_ms FROM sync_deletes
      WHERE table_name = ${r.table} AND row_id = ${id}`;
    if (tomb.length && Number(tomb[0].deleted_at_ms || 0) >= ms) {
      continue; // newer tombstone wins: do not resurrect
    }
    await sql`
      INSERT INTO sync_rows(table_name, row_id, data, updated_at, updated_at_ms, instance)
      VALUES (${r.table}, ${id}, ${JSON.stringify(r.data)}, to_timestamp(${ms / 1000}), ${ms}, ${instance})
      ON CONFLICT (table_name, row_id) DO UPDATE SET
        data = excluded.data,
        updated_at = excluded.updated_at,
        updated_at_ms = excluded.updated_at_ms,
        instance = excluded.instance
      WHERE sync_rows.updated_at_ms < excluded.updated_at_ms`;
    const out: any[] = await sql`INSERT INTO sync_outbox(instance_id, table_name, row_id, op, payload)
      VALUES (${instance}, ${r.table}, ${id}, 'upsert', ${JSON.stringify({ data: r.data })})
      RETURNING rev`;
    lastRev = Number(out[0] && out[0].rev || 0);
    accepted++;
  }
  // accept: deletes
  for (const dl of delsIn) {
    const id = Number(dl.row_id);
    const ms = Number(dl.deleted_at_ms) || ts;
    const revOut: any[] = await sql`INSERT INTO sync_outbox(instance_id, table_name, row_id, op, payload)
      VALUES (${instance}, ${dl.table}, ${id}, 'delete', ${JSON.stringify({ deleted_at_ms: ms })})
      RETURNING rev`;
    const rev = Number(revOut[0] && revOut[0].rev || 0);
    await sql`
      INSERT INTO sync_deletes(table_name, row_id, deleted_at, deleted_at_ms, del_rev, instance)
      VALUES (${dl.table}, ${id}, to_timestamp(${ms / 1000}), ${ms}, ${rev}, ${instance})
      ON CONFLICT (table_name, row_id) DO UPDATE SET
        deleted_at = excluded.deleted_at,
        deleted_at_ms = excluded.deleted_at_ms,
        del_rev = excluded.del_rev,
        instance = excluded.instance
      WHERE excluded.deleted_at_ms >= sync_deletes.deleted_at_ms`;
    await sql`DELETE FROM sync_rows WHERE table_name = ${dl.table} AND row_id = ${id} AND updated_at_ms <= ${ms}`;
    lastRev = Math.max(lastRev, rev);
    accepted++;
  }

  // metrics
  await sql`INSERT INTO sync_metrics(instance_id, rows_pushed_24h, bytes_pushed_24h, window_start, updated_at)
    VALUES (${instance}, ${accepted}, ${estBytes}, now(), now())
    ON CONFLICT (instance_id) DO UPDATE SET
      window_start = CASE WHEN sync_metrics.window_start IS NULL
                           OR (now() - sync_metrics.window_start) > interval '24 hours'
                          THEN now() ELSE sync_metrics.window_start END,
      rows_pushed_24h = CASE WHEN sync_metrics.window_start IS NULL
                              OR (now() - sync_metrics.window_start) > interval '24 hours'
                             THEN ${accepted} ELSE sync_metrics.rows_pushed_24h + ${accepted} END,
      bytes_pushed_24h = CASE WHEN sync_metrics.window_start IS NULL
                               OR (now() - sync_metrics.window_start) > interval '24 hours'
                              THEN ${estBytes} ELSE sync_metrics.bytes_pushed_24h + ${estBytes} END,
      last_error = '', last_error_at = NULL, updated_at = now()`;
  await addProjectEgress(estBytes);
  return json({ ok: true, accepted, rev: lastRev, relay_version: RELAY_VERSION });
}

async function syncChanges(url: URL): Promise<Response> {
  if (!(await syncEnabled())) return json({ ok: false, error: "sync disabled (kill switch)" }, 503);
  const version: number = parseInt(url.searchParams.get("version") || "0", 10);
  const minV: number = await cfgInt("min_client_version", 2);
  if (!Number.isFinite(version) || version < minV) {
    return json({ ok: false, error: "client version too old; upgrade required" }, 426);
  }
  const instance: string = (url.searchParams.get("instance") || "").slice(0, 64);
  const sinceRev = parseInt(url.searchParams.get("since_rev") || "0", 10);
  if (!instance || !Number.isFinite(sinceRev) || sinceRev < 0) {
    return json({ ok: false, error: "instance and since_rev required" }, 400);
  }
  const projBudgetBytes: number = (await cfgInt("project_egress_mb_24h", 100)) * 1024 * 1024;
  const projUsed = await projectEgressUsed();
  if (projUsed >= projBudgetBytes) {
    await noteError(instance, "project egress budget exhausted");
    return json({ ok: false, error: "project 24h egress budget exhausted; sync halted" }, 503);
  }
  const w = await windowRoll(instance);
  const pullByteBudget: number = await cfgInt("instance_bytes_pull_24h", 20971520);
  if (Number(w.bytes_pulled_24h || 0) >= pullByteBudget) {
    await noteError(instance, "instance 24h pull byte budget exhausted");
    return json({ ok: false, error: "instance 24h pull byte budget exhausted", retry_after_s: 60 }, 429);
  }
  const maxRows: number = await cfgInt("pull_max_rows", 500);
  const maxBytes: number = await cfgInt("pull_max_bytes", 262144);
  const rows: any[] = await sql`SELECT rev, table_name, row_id, op, payload
    FROM sync_outbox WHERE rev > ${sinceRev} AND instance_id <> ${instance}
    ORDER BY rev ASC LIMIT ${maxRows}`;
  const changes = rows.map((r) => ({
    rev: Number(r.rev), table: r.table_name, row_id: Number(r.row_id), op: r.op, payload: r.payload,
  }));
  let size = JSON.stringify({ changes }).length;
  let truncated = false;
  while (size > maxBytes && changes.length > 1) {
    changes.pop();
    size = JSON.stringify({ changes }).length;
    truncated = true;
  }
  const body = { ok: true, changes, next_rev: changes.length ? Number(changes[changes.length - 1].rev) : sinceRev, truncated, relay_version: RELAY_VERSION };
  const respBytes = JSON.stringify(body).length;
  await sql`INSERT INTO sync_metrics(instance_id, rows_pulled_24h, bytes_pulled_24h, window_start, updated_at)
    VALUES (${instance}, ${changes.length}, ${respBytes}, now(), now())
    ON CONFLICT (instance_id) DO UPDATE SET
      window_start = CASE WHEN sync_metrics.window_start IS NULL
                           OR (now() - sync_metrics.window_start) > interval '24 hours'
                          THEN now() ELSE sync_metrics.window_start END,
      rows_pulled_24h = CASE WHEN sync_metrics.window_start IS NULL
                              OR (now() - sync_metrics.window_start) > interval '24 hours'
                             THEN ${changes.length} ELSE sync_metrics.rows_pulled_24h + ${changes.length} END,
      bytes_pulled_24h = CASE WHEN sync_metrics.window_start IS NULL
                               OR (now() - sync_metrics.window_start) > interval '24 hours'
                              THEN ${respBytes} ELSE sync_metrics.bytes_pulled_24h + ${respBytes} END,
      updated_at = now()`;
  await sql`INSERT INTO sync_acked(instance_id, last_rev, updated_at) VALUES (${instance}, ${body.next_rev}, now())
    ON CONFLICT (instance_id) DO UPDATE SET last_rev = GREATEST(sync_acked.last_rev, excluded.last_rev), updated_at = now()`;
  await addProjectEgress(respBytes);
  return json(body);
}

// one-time full snapshot for bootstrap/resync (logged, outside the daily egress budget)
async function syncBootstrap(url: URL): Promise<Response> {
  if (!(await syncEnabled())) return json({ ok: false, error: "sync disabled (kill switch)" }, 503);
  const instance: string = (url.searchParams.get("instance") || "").slice(0, 64);
  const table: string = url.searchParams.get("table") || "";
  if (!instance || !SYNC_TABLES.includes(table)) return json({ ok: false, error: "instance and table required" }, 400);
  const rows: any[] = await sql`
    SELECT row_id, data FROM sync_rows WHERE table_name = ${table}
    AND NOT EXISTS (SELECT 1 FROM sync_deletes d
                    WHERE d.table_name = sync_rows.table_name AND d.row_id = sync_rows.row_id
                      AND sync_rows.updated_at_ms <= d.deleted_at_ms)
    ORDER BY row_id LIMIT 10000`;
  const out = rows.map((r) => ({ id: Number(r.row_id), data: r.data }));
  const body = { ok: true, table, rows: out, relay_version: RELAY_VERSION };
  const bytes = JSON.stringify(body).length;
  await sql`INSERT INTO sync_metrics(instance_id, bootstrap_bytes, updated_at) VALUES (${instance}, ${bytes}, now())
    ON CONFLICT (instance_id) DO UPDATE SET bootstrap_bytes = sync_metrics.bootstrap_bytes + ${bytes}, updated_at = now()`;
  return json(body);
}

async function syncStatus(): Promise<Response> {
  const enabled = await syncEnabled();
  const minV = await cfgInt("min_client_version", 2);
  const insts: any[] = await sql`SELECT m.instance_id, m.rows_pushed_24h, m.bytes_pushed_24h,
      m.rows_pulled_24h, m.bytes_pulled_24h, m.bootstrap_bytes, m.last_error, m.last_error_at, m.updated_at,
      a.last_rev
    FROM sync_metrics m LEFT JOIN sync_acked a ON a.instance_id = m.instance_id
    WHERE m.instance_id <> 'project' ORDER BY m.updated_at DESC`;
  const tables: any[] = await sql`SELECT table_name, COUNT(*)::int AS n FROM sync_rows GROUP BY table_name`;
  const tomb: any[] = await sql`SELECT table_name, COUNT(*)::int AS n FROM sync_deletes GROUP BY table_name`;
  const outbox: any[] = await sql`SELECT COUNT(*)::int AS n FROM sync_outbox`;
  const proj: any[] = await sql`SELECT bytes_pulled_24h, window_start FROM sync_metrics WHERE instance_id = 'project'`;
  return json({
    ok: true, version: RELAY_VERSION, enabled, min_client_version: minV,
    project: {
      egress_used_bytes_24h: proj.length ? Number(proj[0].bytes_pulled_24h || 0) : 0,
      egress_budget_mb_24h: await cfgInt("project_egress_mb_24h", 100),
    },
    instances: insts.map((m) => ({
      instance: m.instance_id, rows_pushed_24h: Number(m.rows_pushed_24h || 0),
      bytes_pushed_24h: Number(m.bytes_pushed_24h || 0), rows_pulled_24h: Number(m.rows_pulled_24h || 0),
      bytes_pulled_24h: Number(m.bytes_pulled_24h || 0), bootstrap_bytes: Number(m.bootstrap_bytes || 0),
      last_error: m.last_error || "", last_error_at: m.last_error_at || null,
      last_seen: m.updated_at || null, last_ack_rev: m.last_rev != null ? Number(m.last_rev) : 0,
    })),
    tables: Object.fromEntries(tables.map((t) => [t.table_name, Number(t.n)])),
    tombstones: Object.fromEntries(tomb.map((t) => [t.table_name, Number(t.n)])),
    outbox_depth: Number(outbox[0] && outbox[0].n || 0),
    caps: {
      rows_per_table: await cfgInt("rows_per_table_cap", 10000),
      instance_rows_24h: await cfgInt("instance_rows_24h", 5000),
      instance_bytes_push_24h: await cfgInt("instance_bytes_push_24h", 20971520),
      instance_bytes_pull_24h: await cfgInt("instance_bytes_pull_24h", 20971520),
      pull_max_rows: await cfgInt("pull_max_rows", 500),
      pull_max_bytes: await cfgInt("pull_max_bytes", 262144),
      project_egress_mb_24h: await cfgInt("project_egress_mb_24h", 100),
    },
  });
}

// legacy v1 endpoints: refuse politely so old clients back off instead of looping on data
async function syncLegacyRejected(): Promise<Response> {
  return json({ ok: false, error: "sync v1 endpoints retired; upgrade client to version >= 2" }, 426);
}
