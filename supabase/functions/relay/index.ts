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
    if (req.method === "GET" && p === "/api/sync/pull/deletes") {
      if (schemaReady) return await syncPullDeletes(new URL(req.url));
      try { await ensureSchema(); schemaReady = true; } catch (e) { /* retry next call */ }
      return json({ ok: false, error: "schema not ready" }, 503);
    }
    if (req.method === "GET" && p === "/api/sync/pull") {
      if (schemaReady) return await syncPull(new URL(req.url));
      try { await ensureSchema(); schemaReady = true; } catch (e) { /* retry next call */ }
      return json({ ok: false, error: "schema not ready" }, 503);
    }
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


// ---------------- two-way team DB sync ----------------

const SYNC_TABLES = ["contacts", "emails", "batches", "campaigns", "email_templates", "sheets"];

function parseSinceTs(since: string): Date {
  const ts = since.split("|")[0].trim();
  return new Date(ts + (ts.endsWith("Z") ? "" : "Z"));
}
function parseSinceId(since: string): number {
  const idPart = since.split("|")[1];
  const n = parseInt(idPart || "-1", 10);
  return Number.isFinite(n) ? n : -1;
}

async function syncPush(req: Request): Promise<Response> {
  const d = (await req.json()) as any;
  const table: string = d.table || "";
  if (!SYNC_TABLES.includes(table)) return json({ ok: false, error: "unknown table" }, 400);
  const rows: any[] = Array.isArray(d.rows) ? d.rows : [];
  const dels: any[] = Array.isArray(d.deletes) ? d.deletes : [];
  if (rows.length + dels.length > 2000) return json({ ok: false, error: "too many rows" }, 413);
  let n = 0;
  for (const r of rows) {
    if (!r || r.id == null) continue;
    const ts: Date = r.updated_at ? new Date(r.updated_at) : new Date();
    await sql`
      INSERT INTO sync_rows(table_name, row_id, data, updated_at, instance)
      VALUES (${table}, ${parseInt(String(r.id))}, ${JSON.stringify(r.data || {})}::jsonb, ${ts}, ${d.instance || ""})
      ON CONFLICT (table_name, row_id) DO UPDATE
        SET data = excluded.data, updated_at = excluded.updated_at, instance = excluded.instance
        WHERE excluded.updated_at >= sync_rows.updated_at`;
    n++;
  }
  let dn = 0;
  for (const dl of dels) {
    if (!dl || dl.id == null) continue;
    const ts: Date = dl.deleted_at ? new Date(dl.deleted_at + (String(dl.deleted_at).endsWith("Z") ? "" : "Z")) : new Date();
    await sql`
      INSERT INTO sync_deletes(table_name, row_id, deleted_at, instance)
      VALUES (${table}, ${parseInt(String(dl.id))}, ${ts}, ${d.instance || ""})
      ON CONFLICT (table_name, row_id) DO UPDATE
        SET deleted_at = excluded.deleted_at, instance = excluded.instance
        WHERE excluded.deleted_at >= sync_deletes.deleted_at`;
    dn++;
  }
  return json({ ok: true, upserted: n, deletes: dn });
}

async function syncPullDeletes(url: URL): Promise<Response> {
  const table: string = url.searchParams.get("table") || "";
  if (!SYNC_TABLES.includes(table)) return json({ ok: false, error: "unknown table" }, 400);
  const since: string | null = url.searchParams.get("since");
  const rows: any[] = since
    ? await sql`SELECT row_id, deleted_at, instance FROM sync_deletes
      WHERE table_name = ${table}
        AND (deleted_at > ${parseSinceTs(since)}
          OR (deleted_at = ${parseSinceTs(since)} AND row_id > ${parseSinceId(since)}))
      ORDER BY deleted_at, row_id LIMIT 2000`
    : await sql`SELECT row_id, deleted_at, instance FROM sync_deletes
      WHERE table_name = ${table}
      ORDER BY deleted_at, row_id LIMIT 2000`;
  return json({ ok: true, deletes: rows.map((r) => ({
    id: r.row_id, deleted_at: new Date(r.deleted_at).toISOString(), instance: r.instance || "",
  })) });
}

async function syncPull(url: URL): Promise<Response> {
  const table: string = url.searchParams.get("table") || "";
  if (!SYNC_TABLES.includes(table)) return json({ ok: false, error: "unknown table" }, 400);
  const since: string | null = url.searchParams.get("since");
  const rows: any[] = since
    ? await sql`SELECT row_id, data, updated_at, instance FROM sync_rows
      WHERE table_name = ${table}
        AND (updated_at > ${parseSinceTs(since)}
          OR (updated_at = ${parseSinceTs(since)} AND row_id > ${parseSinceId(since)}))
      ORDER BY updated_at, row_id LIMIT 2000`
    : await sql`SELECT row_id, data, updated_at, instance FROM sync_rows
      WHERE table_name = ${table}
      ORDER BY updated_at, row_id LIMIT 2000`;
  return json({ ok: true, rows: rows.map((r) => ({
    id: r.row_id, data: typeof r.data === "string" ? JSON.parse(r.data) : r.data,
    updated_at: new Date(r.updated_at).toISOString(), instance: r.instance || "",
  })) });
}

function parseAtts(s: string | null): any[] {
  try { return JSON.parse(s || "[]"); } catch { return []; }
}
function parseCc(s: string | null): string[] {
  return (s || "").split(",").map((x) => x.trim()).filter(Boolean);
}

// ---------------- MIME ----------------

function b64url(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function enc(s: string): Uint8Array {
  return new TextEncoder().encode(s);
}

function buildRaw(to: string, subject: string, bodyHtml: string, atts: any[], cc: string[]): string {
  if (!atts.length) {
    const ccLine = cc.length ? "Cc: " + cc.join(", ") + "\r\n" : "";
    const raw =
      "To: " + to + "\r\nSubject: " + subject + "\r\n" + ccLine +
      "MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + bodyHtml;
    return b64url(enc(raw));
  }
  const boundary = "mf-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  const chunks: string[] = [];
  chunks.push("--" + boundary + "\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + bodyHtml);
  for (const a of atts) {
    chunks.push("--" + boundary +
      "\r\nContent-Type: " + (a.mime || "application/octet-stream") +
      "\r\nContent-Transfer-Encoding: base64" +
      "\r\nContent-Disposition: attachment; filename=\"" + a.name + "\"" +
      "\r\n\r\n" + (a.b64 || ""));
  }
  chunks.push("--" + boundary + "--");
  const head = "To: " + to + "\r\nSubject: " + subject +
    (cc.length ? "\r\nCc: " + cc.join(", ") + "\r\n" : "") +
    "\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"" + boundary + "\"\r\n\r\n";
  return b64url(enc(head + chunks.join("\r\n")));
}

// ---------------- Gmail (fetch, no SDK) ----------------

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const GMAIL_API = "https://gmail.googleapis.com/gmail/v1";

async function accessToken(): Promise<string> {
  const cached: any[] = await sql`
    SELECT access_token, expiry_epoch FROM relay_token_cache WHERE account_id = 0 LIMIT 1`;
  const nowSec = Math.floor(Date.now() / 1000);
  if (cached.length && cached[0].access_token && (cached[0].expiry_epoch || 0) > nowSec + 60) {
    return cached[0].access_token as string;
  }
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: CLIENT_ID,
    client_secret: CLIENT_SECRET,
    refresh_token: REFRESH_TOKEN,
  });
  const r = await fetch(TOKEN_URL, {
    method: "POST",
    body,
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  });
  if (!r.ok) throw new Error("token refresh failed: " + (await r.text()).slice(0, 200));
  const d: any = await r.json();
  const expiry = nowSec + (d.expires_in || 3600);
  await sql`
    INSERT INTO relay_token_cache(account_id, access_token, expiry_epoch) VALUES (0, ${d.access_token}, ${expiry})
    ON CONFLICT (account_id) DO UPDATE SET access_token = excluded.access_token, expiry_epoch = excluded.expiry_epoch`;
  return d.access_token;
}

async function gmailSend(raw: string): Promise<any> {
  const doSend = async (token: string) => {
    const r = await fetch(GMAIL_API + "/users/" + encodeURIComponent(SENDER) + "/messages/send", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      body: JSON.stringify({ raw }),
    });
    if (!r.ok) throw new Error("gmail send failed: " + (await r.text()).slice(0, 300) + " [" + r.status + "]");
    return await r.json();
  };
  try {
    return await doSend(await accessToken());
  } catch (e) {
    if (String(e).includes("[401]")) {
      await sql`DELETE FROM relay_token_cache WHERE account_id = 0`;
      return await doSend(await accessToken());
    }
    throw e;
  }
}

async function gmailMessageId(messageId: string): Promise<string> {
  if (!messageId) return "";
  const token = await accessToken();
  const r = await fetch(
    GMAIL_API + "/users/" + encodeURIComponent(SENDER) + "/messages/" + messageId +
    "?format=metadata&metadataHeaders=Message-Id",
    { headers: { Authorization: "Bearer " + token } },
  );
  if (!r.ok) return "";
  const d: any = await r.json();
  for (const h of (d.payload && d.payload.headers) || []) {
    if ((h.name || "").toLowerCase() === "message-id") return (h.value || "").replace(/[<>]/g, "");
  }
  return "";
}
