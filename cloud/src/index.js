// CampusMail AI cloud relay - sends scheduled emails without the laptop being on.
// Deploy: see cloud/README-CLOUD.md. Secrets: RELAY_SECRET, GMAIL_CLIENT_ID,
// GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, GMAIL_SENDER_EMAIL.

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const GMAIL_API = "https://gmail.googleapis.com/gmail/v1";
const PUMP_LIMIT = 10; // sends per minute (worker CPU limits)

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return json({ ok: true, relay: true });
    }
    // cron trigger -> pump
    if (request.headers.get("Cf-Worker-Trigger") === "crons") {
      return pump(env, ctx);
    }
    const secret = request.headers.get("x-relay-secret");
    if (!secret || secret !== env.RELAY_SECRET) {
      return json({ ok: false, error: "bad secret" }, 403);
    }
    try {
      if (request.method === "POST" && url.pathname === "/api/emails") {
        return await upsertEmails(request, env);
      }
      if (request.method === "GET" && url.pathname === "/api/status") {
        return await statusPull(request, env);
      }
      if (request.method === "POST" && url.pathname === "/api/pump") {
        return pump(env, ctx);
      }
      return json({ ok: false, error: "not found" }, 404);
    } catch (e) {
      return json({ ok: false, error: String(e && e.message || e) }, 500);
    }
  },
};

function json(obj, code = 200) {
  return new Response(JSON.stringify(obj), {
    status: code,
    headers: { "Content-Type": "application/json" },
  });
}

function nowIso() {
  return new Date().toISOString();
}

// ---------------- queue sync ----------------

async function upsertEmails(request, env) {
  const d = await request.json();
  const list = Array.isArray(d.emails) ? d.emails : [d.emails];
  let n = 0;
  for (const e of list) {
    if (!e || !e.local_id) continue;
    let body_text = e.body_html || "";
    let body_r2_key = null;
    if (body_text.length > 2048) {
      const key = "email-" + e.local_id + ".html";
      await env.BODIES.put(key, body_text, { httpMetadata: { contentType: "text/html" } });
      body_r2_key = key;
      body_text = null;
    }
    await env.DB.prepare(
      `INSERT INTO emails(local_id, account_id, to_addr, cc, subject, body_r2_key, body_text,
        attachments, status, scheduled_at, batch_name, campaign_name, is_reminder, serial, error, updated_at)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
       ON CONFLICT(local_id) DO UPDATE SET
        account_id=excluded.account_id, to_addr=excluded.to_addr, cc=excluded.cc,
        subject=excluded.subject, body_r2_key=excluded.body_r2_key, body_text=excluded.body_text,
        attachments=excluded.attachments, status=excluded.status,
        scheduled_at=excluded.scheduled_at, batch_name=excluded.batch_name,
        campaign_name=excluded.campaign_name, is_reminder=excluded.is_reminder,
        serial=excluded.serial, error=excluded.error,
        sent_at=CASE WHEN excluded.status='sent' THEN COALESCE(?, sent_at) ELSE sent_at END,
        message_id=CASE WHEN excluded.status='sent' AND excluded.message_id IS NOT NULL AND excluded.message_id != ''
                 THEN excluded.message_id ELSE message_id END,
        updated_at=excluded.updated_at`
    ).bind(
      e.local_id, e.account_id || 0, e.to_addr || "", e.cc || "", e.subject || "",
      body_r2_key, body_text,
      JSON.stringify(e.attachments || []),
      e.status || "scheduled", e.scheduled_at || null,
      e.batch_name || "", e.campaign_name || "", e.is_reminder ? 1 : 0,
      e.serial || "", e.error || "", nowIso(),
      e.sent_at || null, e.message_id || ""
    ).run();
    n++;
  }
  return json({ ok: true, upserted: n });
}

async function statusPull(request, env) {
  const url = new URL(request.url);
  const since = url.searchParams.get("updated_since") || "1970-01-01T00:00:00Z";
  const { results } = await env.DB.prepare(
    "SELECT * FROM emails WHERE updated_at >= ? ORDER BY updated_at LIMIT 500"
  ).bind(since).all();
  return json({ ok: true, emails: results.map((r) => ({
    local_id: r.local_id, status: r.status, sent_at: r.sent_at,
    message_id: r.message_id, error: r.error, updated_at: r.updated_at,
  })) });
}

// ---------------- sending ----------------

async function pump(env, ctx) {
  const { results: due } = await env.DB.prepare(
    "SELECT * FROM emails WHERE status='scheduled' AND scheduled_at IS NOT NULL AND scheduled_at <= ? ORDER BY scheduled_at LIMIT ?",
  ).bind(nowIso(), PUMP_LIMIT).all();
  let sent = 0, failed = 0;
  for (const e of due) {
    try {
      const body = e.body_text != null ? e.body_text : (await env.BODIES.get(e.body_r2_key)).text();
      const raw = buildRaw(e.to_addr, e.subject, body, parseAtts(e.attachments), parseCc(e.cc));
      const res = await gmailSend(env, raw);
      const mid = await gmailMessageId(env, res.id).catch(() => "");
      await env.DB.prepare(
        "UPDATE emails SET status='sent', sent_at=?, message_id=?, error='', updated_at=? WHERE local_id=?"
      ).bind(nowIso(), mid, nowIso(), e.local_id).run();
      sent++;
    } catch (err) {
      failed++;
      await env.DB.prepare(
        "UPDATE emails SET status='error', error=?, updated_at=? WHERE local_id=?"
      ).bind(String(err && err.message || err).slice(0, 300), nowIso(), e.local_id).run();
      await env.BODIES.delete(e.body_r2_key).catch(() => {});
    }
  }
  return json({ ok: true, due: due.length, sent, failed });
}

function parseAtts(s) { try { return JSON.parse(s || "[]"); } catch { return []; } }
function parseCc(s) { return (s || "").split(",").map((x) => x.trim()).filter(Boolean); }

// ---------------- MIME ----------------

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function b64(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)));
}
function enc(s) { return new TextEncoder().encode(s); }

function buildRaw(to, subject, bodyHtml, atts, cc) {
  if (!atts.length) {
    const ccLine = cc.length ? "Cc: " + cc.join(", ") + "\r\n" : "";
    const raw =
      "To: " + to + "\r\nSubject: " + subject + "\r\n" + ccLine +
      "MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + bodyHtml;
    return b64url(enc(raw));
  }
  const boundary = "mf-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  const chunks = [];
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
    (cc.length ? "\r\nCc: " + cc.join(", ") : "") +
    "\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"" + boundary + "\"\r\n\r\n";
  return b64url(enc(head + chunks.join("\r\n")));
}

// ---------------- Gmail (fetch, no SDK) ----------------

async function accessToken(env) {
  const cached = await env.DB.prepare("SELECT * FROM token_cache WHERE account_id=0").first();
  const nowSec = Math.floor(Date.now() / 1000);
  if (cached && cached.access_token && cached.expiry_epoch > nowSec + 60) {
    return cached.access_token;
  }
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: env.GMAIL_CLIENT_ID,
    client_secret: env.GMAIL_CLIENT_SECRET,
    refresh_token: env.GMAIL_REFRESH_TOKEN,
  });
  const r = await fetch(TOKEN_URL, { method: "POST", body, headers: { "Content-Type": "application/x-www-form-urlencoded" } });
  if (!r.ok) throw new Error("token refresh failed: " + (await r.text()).slice(0, 200));
  const d = await r.json();
  const expiry = nowSec + (d.expires_in || 3600);
  await env.DB.prepare(
    "INSERT INTO token_cache(account_id, access_token, expiry_epoch) VALUES(0,?,?) " +
    "ON CONFLICT(account_id) DO UPDATE SET access_token=excluded.access_token, expiry_epoch=excluded.expiry_epoch"
  ).bind(d.access_token, expiry).run();
  return d.access_token;
}

async function gmailSend(env, raw) {
  const token = await accessToken(env);
  const r = await fetch(GMAIL_API + "/users/" + encodeURIComponent(env.GMAIL_SENDER_EMAIL) + "/messages/send", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
    body: JSON.stringify({ raw }),
  });
  if (!r.ok) {
    const t = (await r.text()).slice(0, 300);
    if (r.status === 401) {
      await env.DB.prepare("DELETE FROM token_cache WHERE account_id=0").run();
      const t2 = await fetch(GMAIL_API + "/users/" + encodeURIComponent(env.GMAIL_SENDER_EMAIL) + "/messages/send", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + (await accessToken(env)) },
        body: JSON.stringify({ raw }),
      });
      if (!t2.ok) throw new Error("gmail send failed: " + (await t2.text()).slice(0, 300));
      return await t2.json();
    }
    throw new Error("gmail send failed: " + t);
  }
  return await r.json();
}

async function gmailMessageId(env, messageId) {
  if (!messageId) return "";
  const token = await accessToken(env);
  const r = await fetch(GMAIL_API + "/users/" + encodeURIComponent(env.GMAIL_SENDER_EMAIL) + "/messages/" + messageId + "?format=metadata&metadataHeaders=Message-Id", {
    headers: { Authorization: "Bearer " + token },
  });
  if (!r.ok) return "";
  const d = await r.json();
  for (const h of (d.payload && d.payload.headers) || []) {
    if ((h.name || "").toLowerCase() === "message-id") return (h.value || "").replace(/[<>]/g, "");
  }
  return "";
}
