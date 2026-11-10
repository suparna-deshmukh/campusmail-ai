// MailForge cloud relay - Supabase Edge Function edition.
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
        error, reminder_days, reminder_max, reminder_subject, reminder_body, replied_at, updated_at)
      VALUES (${e.local_id}, ${e.account_id || 0}, ${e.to_addr || ""}, ${e.cc || ""}, ${e.subject || ""},
        ${e.body_html || ""}, ${JSON.stringify(e.attachments || [])}, ${e.status || "scheduled"},
        ${e.scheduled_at ? new Date(e.scheduled_at) : null}, ${e.batch_name || ""},
        ${e.campaign_name || ""}, ${e.is_reminder ? 1 : 0}, ${String(e.serial || "")},
        ${e.error || ""}, ${e.reminder_days || 0}, ${e.reminder_max || 0},
        ${e.reminder_subject || ""}, ${e.reminder_body || ""},
        ${e.replied_at ? new Date(e.replied_at) : null}, now())
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
           is_reminder, parent_local_id, to_addr, subject, reminder_count, body_text
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
    body_html: r.is_reminder ? (r.body_text || "") : undefined,
  })) });
}

// ---------------- sending ----------------

async function pump(): Promise<Response> {
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
  return json({ ok: true, due: due.length, sent, failed, reminders_sent: remSent, reminders_failed: remFailed });
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
