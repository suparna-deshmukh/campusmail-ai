/* Ask AI - the agentic control surface.
   Reads are free (no confirmation); every create/edit/delete/send goes
   through a confirm card carrying a server-issued approval token - the
   client never sends parameters for execution, only approval ids.
   Card types: options, text/number/datetime/secret/file/table inputs,
   confirm, final_review, compact_review, draft_editor, undo_countdown,
   plan, budget, handoff, data (stat/table/list/thread/analytics/...),
   result, events (step chips). */
import { api, el, icon, toast, drawer, modal, esc } from "./ui.js";
import { makeEditor } from "./ask-ai-editor.js";

const STORE = "cm_askai_history";
const MAX_HISTORY = 60;

const S = {
  open: false,
  convId: null,
  mode: "guided",
  busy: false,
  pendingCard: null,   // pending card envelope
  pendingNode: null,   // its DOM node
  undo: null,          // {env, releaseAtMs, timer, toastMsg, toastHandle}
  nodes: [],           // live card nodes for editor refresh
  body: null,
  input: null,
  histTimer: null,
};

/* ------------------------------------------------------------- utils */

function base64Of(file) {
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result).split(",")[1] || "");
    fr.onerror = () => rej(fr.error);
    fr.readAsDataURL(file);
  });
}

const RE_EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
function vEmail(v) { return RE_EMAIL.test((v || "").trim()); }
function vEmails(v) {
  const list = String(v || "").split(",").map((x) => x.trim()).filter(Boolean);
  if (!list.length) return "Enter at least one address.";
  for (const e of list) if (!RE_EMAIL.test(e)) return "'" + e + "' is not a valid address.";
  if (list.length > 20) return "Up to 20 addresses.";
  return null;
}
function vUrl(v) {
  try { const u = new URL((v || "").trim()); return u.protocol === "http:" || u.protocol === "https:" ? null : "Use an http(s) address."; }
  catch { return "Enter a valid URL (https://…)."; }
}
function vName(v) { return (v || "").trim() ? null : "Enter a name."; }

function histPush(entry) {
  const h = loadHist();
  h.push(entry);
  saveHist(h.slice(-MAX_HISTORY));
}
function loadHist() {
  try { return JSON.parse(localStorage.getItem(STORE) || "[]"); } catch { return []; }
}
function saveHist(h) {
  try { localStorage.setItem(STORE, JSON.stringify(h)); } catch { /* private mode */ }
}

function scrollBottom() {
  if (S.body) requestAnimationFrame(() => { S.body.scrollTop = S.body.scrollHeight; });
}

function thinkingNode() {
  return el("div", { class: "ask-ai ask-thinking" }, icon("sparkles", "i"), el("span", {}, "Thinking…"));
}

/* ------------------------------------------------------------- drawer */

async function openAskAI(initial) {
  if (S.open) return;
  S.open = true;

  const body = el("div", { class: "ask-body" });
  S.body = body;

  let attachment = null;
  const fileInp = el("input", { type: "file", accept: ".csv,.xlsx,.tsv,.docx,.html,.htm,.txt,.pdf,.png,.jpg,.jpeg",
    style: "display:none" });
  const fileChip = el("div", { class: "ask-filechip", style: "display:none" });
  const renderChip = () => {
    if (!attachment) { fileChip.style.display = "none"; return; }
    fileChip.style.display = "flex";
    fileChip.innerHTML = "";
    fileChip.append(icon("paperclip", "i i-16"),
      el("span", { class: "ask-filechip-name" }, attachment.name),
      el("span", { class: "ask-filechip-size" }, Math.round(attachment.size / 1024) + " KB"),
      el("button", { class: "ask-filechip-x", "aria-label": "Remove file",
        onclick: () => { attachment = null; renderChip(); } }, icon("x", "i")));
  };

  const attachBtn = el("button", { class: "btn btn-ghost ask-attach", "aria-label": "Attach a file", title: "Attach a file" },
    icon("paperclip", "i i-16"));
  const input = el("input", { class: "input ask-input", "aria-label": "Message the assistant",
    placeholder: "Ask anything about your campaigns, or ask it to do it…" });
  const sendBtn = el("button", { class: "btn btn-primary ask-send", "aria-label": "Send" }, icon("send", "i i-16"));
  S.input = input;

  const hint = el("div", { class: "ask-hint", style: "display:none" });
  const setHint = (txt) => {
    if (!txt) { hint.style.display = "none"; return; }
    hint.textContent = txt;
    hint.style.display = "block";
  };

  const setBusy = (b) => {
    S.busy = b;
    input.disabled = b;
    sendBtn.disabled = b;
    attachBtn.disabled = b;
  };

  /* ---- send a user message (text and/or file) ---- */
  const doSend = async () => {
    const text = input.value.trim();
    if ((!text && !attachment) || S.busy) return;
    setBusy(true);
    const att = attachment;
    input.value = "";
    attachment = null;
    renderChip();
    histPush({ role: "user", content: text || (att ? "📎 " + att.name : "") });
    appendNode(userNode(text, att ? att.name : null));
    const th = thinkingNode();
    appendNode(th);
    try {
      const payload = { text: text || undefined, conv_id: S.convId || undefined };
      if (att) payload.file = { name: att.name, data: await base64Of(att) };
      const d = await api("/api/ai/agent", { body: payload });
      S.convId = d.conv_id || S.convId;
      S.mode = d.mode || S.mode;
      th.remove();
      await renderOutcomes(d.outcomes || []);
    } catch (e) {
      th.remove();
      appendNode(errorNode(e.message || String(e)));
      histPush({ role: "assistant", content: e.message || String(e), error: true });
    }
    setBusy(false);
    updateHint();
  };

  const updateHint = () => {
    if (S.pendingCard && !S.busy) {
      setHint("Answer the card above, or cancel it. Typing a new message will cancel the card.");
    } else if (S.busy) {
      setHint("");
    } else setHint("");
  };

  const clearBtn = el("button", { class: "btn btn-ghost btn-sm", "aria-label": "Start a new conversation",
    onclick: () => {
      S.convId = null;
      S.pendingCard = null;
      S.pendingNode = null;
      S.nodes = [];
      body.innerHTML = "";
      saveHist([]);
      emptyState();
    } }, "New chat");

  /* ---- quick-start chips (role-aware) ---- */
  const isAdmin = (window.me_role || "") === "admin";
  const page = document.body.dataset.page || "";
  const chipSets = {
    "": ["Show my overview", "What needs my attention?", "Which emails bounced and why?",
         "Prepare a send for a campaign"],
    home: ["What needs my attention?", "How are my campaigns doing?", "What's scheduled for the next 24 hours?"],
    campaigns: ["Which campaign has the best reply rate?", "Create a campaign", "Prepare a send for a campaign"],
    send: ["Prepare a send for a campaign", "Draft a first outreach email", "What should I schedule it for?"],
    replies: ["Summarise the open replies", "Draft a reply to a recruiter", "Which emails are waiting on me?"],
    library: ["What fields do my templates use?", "Write a template for outreach", "Which template is active?"],
    team: ["Who has sent the most emails?", "Show the activity log", ...(isAdmin ? ["Who has pending join requests?"] : [])],
    settings: ["Is live sending on?", "What's my daily cap left?", ...(isAdmin ? ["Show my AI usage"] : [])],
  };
  const chips = el("div", { class: "ask-chips" },
    ...(chipSets[page] || chipSets[""]).slice(0, 4).map((h) =>
      el("button", { class: "chip-btn", onclick: () => { input.value = h; doSend(); } }, h)),
    el("span", { class: "push" }), clearBtn);

  /* ---- mode switch (Guided | Compact) ---- */
  const modeSeg = el("div", { class: "seg ask-mode-seg", role: "group", "aria-label": "Interaction mode" },
    el("button", { class: "seg-item", "data-mode": "guided", "aria-pressed": S.mode === "guided",
      onclick: () => switchMode("guided") }, "Guided"),
    el("button", { class: "seg-item", "data-mode": "compact", "aria-pressed": S.mode === "compact",
      onclick: () => switchMode("compact") }, "Compact"));
  const syncModeSeg = () => {
    modeSeg.querySelectorAll(".seg-item").forEach((b) => {
      const on = b.dataset.mode === S.mode;
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.classList.toggle("is-on", on);
    });
  };
  const switchMode = async (m) => {
    if (S.mode === m || S.busy) return;
    setBusy(true);
    try {
      const d = await api("/api/ai/agent", { body: { text: "switch to " + m, conv_id: S.convId || undefined } });
      S.convId = d.conv_id || S.convId;
      S.mode = d.mode || m;
      syncModeSeg();
      await renderOutcomes(d.outcomes || []);
    } catch (e) { toast(e.message, { type: "error" }); }
    setBusy(false);
  };
  syncModeSeg();

  const titleNode = el("div", { class: "ask-title-row" },
    el("div", { class: "t-card truncate" }, "Ask AI"),
    modeSeg);

  /* ---- restore previous conversation (pending cards survive close) ---- */
  const restore = async () => {
    try {
      const d = await api("/api/ai/restore");
      if (d && d.conv_id) {
        const c = await api("/api/ai/convs/" + d.conv_id);
        if (c && c.ok) {
          S.convId = c.conv_id;
          S.mode = c.mode || "guided";
          syncModeSeg();
          for (const env of c.cards || []) pushCard(env);
        }
      }
    } catch { /* new conversation */ }
  };

  const emptyState = () => {
    if (body.children.length) return;
    body.append(el("div", { class: "ask-empty" },
      el("div", { class: "ask-empty-ico" }, icon("sparkles")),
      el("div", { class: "ask-empty-title" }, "What would you like to do?"),
      el("p", { class: "ask-empty-sub" },
        "Ask in plain language, or pick a chip. I read data freely and confirm before I change anything.")));
  };

  const closeUndo = () => {
    if (!S.undo) return null;
    const u = S.undo;
    S.undo = null;
    clearInterval(u.timer);
    /* keep the window alive in a toast: same Undo, same countdown */
    const msg = el("div", {}, "Sending " + u.count + " emails — Undo (" + u.left + " s)");
    const h = toast(msg, { type: "info", actionLabel: "Undo", duration: 120000,
      action: () => undoClick(u.env) });
    u.toastMsg = msg;
    u.toastHandle = h;
    return u;
  };

  const d = drawer({
    title: titleNode,
    sub: "Confirms before it acts · one approval at a time",
    body: el("div", { class: "ask-wrap" }, chips, body, fileChip),
    foot: el("div", { class: "ask-foot" },
      attachBtn, input, sendBtn, hint),
    onClose: () => {
      S.open = false;
      window.dispatchEvent(new CustomEvent("cm:drawer-closed"));
      const u = closeUndo();
      api("/api/ai/convs/" + S.convId + "/close", { method: "POST" }).catch(() => {});
      if (u) {
        /* countdown keeps ticking in the toast */
        u.timer = setInterval(() => tickUndo(u), 500);
      }
    },
  });
  S.drawer = d;

  attachBtn.addEventListener("click", () => fileInp.click());
  fileInp.addEventListener("change", async () => {
    const f = fileInp.files && fileInp.files[0];
    fileInp.value = "";
    if (!f) return;
    if (f.size > 5 * 1024 * 1024) { toast("File too large (max 5 MB).", { type: "error" }); return; }
    try {
      attachment = { name: f.name, size: f.size, data: await base64Of(f) };
      renderChip();
    } catch { toast("Could not read that file.", { type: "error" }); }
  });
  sendBtn.addEventListener("click", doSend);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); } });

  window.addEventListener("cm:ask-ai-text", (e) => {
    if (!S.open || S.busy) return;
    input.value = e.detail || "";
    doSend();
  });

  emptyState();
  await restore();
  if (initial) { input.value = initial; doSend(); }
  S.updateHint = updateHint;
}

function appendNode(n) {
  if (!S.body) return;
  S.body.append(n);
  scrollBottom();
  return n;
}

function userNode(text, file) {
  return el("div", { class: "ask-msg ask-user" },
    file ? el("div", { class: "ask-user-file" }, icon("paperclip", "i i-16"), file) : null,
    text ? el("div", {}, text) : null);
}

function errorNode(message, retry) {
  const n = el("div", { class: "ask-ai ask-err" }, el("div", { class: "ask-text" }, message));
  if (retry) {
    n.append(el("button", { class: "btn btn-ghost btn-sm", onclick: retry }, "Retry"));
  }
  return n;
}

/* ------------------------------------------------------------- outcomes */

async function renderOutcomes(outcomes) {
  for (const o of outcomes || []) {
    switch (o.kind) {
      case "message":
        appendNode(el("div", { class: "ask-ai" }, el("div", { class: "ask-text" }, o.text)));
        histPush({ role: "assistant", content: (o.text || "").slice(0, 300) });
        break;
      case "data":
        appendNode(dataNode(o.data));
        histPush({ role: "assistant", content: "[data " + (o.data && o.data.type) + "]" });
        break;
      case "result":
        appendNode(resultNode(o.data));
        histPush({ role: "assistant", content: "[result] " + (o.data && o.data.title || "done") });
        break;
      case "card":
        pushCard(o.card);
        break;
      case "error":
        appendNode(errorNode(o.message));
        histPush({ role: "assistant", content: o.message, error: true });
        break;
      case "events":
        appendNode(eventsNode(o.steps));
        break;
      case "plan":
        appendNode(planNode(o.data));
        break;
      case "subjects":
        applySubjects(o.data);
        break;
      case "editor_refresh":
        applyEditorRefresh(o.data);
        break;
      case "compact_update":
        applyCompactUpdate(o.data);
        break;
      default: break;
    }
  }
}

function applySubjects(d) {
  const ed = S.nodes.find((n) => n.type === "draft_editor" && n.status === "live");
  if (!ed || !ed.subjects) return;
  ed.subjects(d.subjects || []);
}
function applyEditorRefresh(d) {
  const ed = S.nodes.find((n) => n.type === "draft_editor" && n.status === "live");
  if (!ed || !ed.refresh) return;
  ed.refresh(d);
}
function applyCompactUpdate(d) {
  const c = S.nodes.find((n) => n.type === "compact_review" && n.status === "live");
  if (!c || !c.update) return;
  c.update(d);
}

function eventsNode(events) {
  if (!events || !events.length) return null;
  return el("div", { class: "ask-evs" }, events.map((ev) =>
    el("details", { class: "ask-ev" },
      el("summary", {}, icon("zap", "i ask-ev-ico"),
        el("span", {}, ev.tool || "step")),
      ev.result ? el("pre", { class: "ask-ev-pre" }, JSON.stringify(ev.result, null, 2)) : null)));
}

function planNode(d) {
  return el("div", { class: "ask-ai ask-plan" },
    el("div", { class: "ask-card-head" }, icon("list-checks", "i"), "Plan"),
    el("ol", { class: "ask-plan-steps" }, (d.steps || []).map((s) =>
      el("li", {}, el("span", {}, s.label || s.flow),
        el("span", { class: "ask-risk", title: "each step asks for its own approval" }, "gated")))));
}

function resultNode(d) {
  d = d || {};
  const n = el("div", { class: "ask-ai ask-result" },
    el("div", { class: "ask-result-head" }, icon("check-circle-2", "i"), d.title || "Done"));
  const data = d.data || {};
  const kv = Object.entries(data).filter(([, v]) => v !== null && v !== undefined && typeof v !== "object");
  if (kv.length) {
    n.append(el("div", { class: "ask-kv" }, kv.map(([k, v]) =>
      el("div", { class: "ask-kv-row" }, el("span", { class: "ask-kv-k" }, k.replace(/_/g, " ")),
        el("span", { class: "ask-kv-v" }, String(v))))));
  }
  if (d.links && d.links.length) {
    n.append(el("div", { class: "ask-links" }, d.links.map((l) =>
      el("a", { class: "chip-btn", href: l.page, "data-ask-link": "1" }, l.label))));
  }
  if (d.note) n.append(el("div", { class: "ask-note" }, d.note));
  return n;
}

/* ------------------------------------------------------------- data cards */

function dataNode(d) {
  d = d || {};
  const t = d.type || "data";
  if (t === "stat") return statNode(d.data);
  if (t === "table") return tableNode(d);
  if (t === "list") return listNode(d);
  if (t === "thread") return threadNode(d.data);
  if (t === "analytics") return analyticsNode(d.data);
  if (t === "activity") return activityNode(d.data);
  if (t === "preview") return paperNode(d.data.subject, d.data.body_html);
  if (t === "empty") return el("div", { class: "ask-empty" }, el("div", {}, d.text || ""));
  return genericNode(d.data);
}

function statNode(tiles) {
  tiles = tiles || {};
  const fmt = (k, v) => {
    if (k === "safe_mode") return v ? "Safe mode ON" : "Live sending ON";
    if (k === "reply_rate") return v + "%";
    return String(v);
  };
  return el("div", { class: "ask-ai ask-stat" },
    el("div", { class: "ask-stat-tiles" }, Object.entries(tiles).map(([k, v]) =>
      el("div", { class: "ask-tile" },
        el("div", { class: "ask-tile-v" }, fmt(k, v)),
        el("div", { class: "ask-tile-k" }, k.replace(/_/g, " "))))));
}

function tableNode(d) {
  const rows = d.data || [];
  if (!rows.length) {
    return el("div", { class: "ask-ai ask-empty" },
      el("div", {}, d.empty ? d.empty.text : "No rows."),
      d.empty && d.empty.action ? el("button", { class: "chip-btn", onclick: () => chipAsText(d.empty.action) }, d.empty.action) : null);
  }
  const cols = Object.keys(rows[0]).filter((c) => c !== "id");
  const LIMIT = 20;
  const wrap = el("div", { class: "ask-ai ask-table-wrap" });
  let shown = Math.min(LIMIT, rows.length);
  const tableHost = el("div", { class: "ask-table-scroll" });
  const moreBtn = el("button", { class: "btn btn-ghost btn-sm" });
  const render = () => {
    tableHost.innerHTML = "";
    const slice = rows.slice(0, shown);
    tableHost.append(el("table", { class: "ask-table" },
      el("thead", {}, el("tr", {}, cols.map((c) => el("th", {}, c.replace(/_/g, " ")))),
      el("tbody", {}, slice.map((r) => el("tr", {}, cols.map((c) => {
        let v = r[c];
        if (v === null || v === undefined || v === "") v = "\u2014";
        if (typeof v === "object") v = JSON.stringify(v);
        const t = String(v);
        return el("td", {}, t.length > 60 ? t.slice(0, 60) + "\u2026" : t);
      })))))))
    if (rows.length > LIMIT) {
      moreBtn.textContent = shown < rows.length ? "Show all " + rows.length : "Show less";
      moreBtn.style.display = "";
      moreBtn.onclick = () => { shown = shown < rows.length ? rows.length : LIMIT; render(); };
    } else {
      moreBtn.style.display = "none";
    }
  };
  render();
  wrap.append(tableHost, moreBtn);
  if (d.note) wrap.append(el("div", { class: "ask-note" }, d.note));
  return wrap;
}

function chipAsText(text) {
  if (S.input) { S.input.value = text; }
  window.dispatchEvent(new CustomEvent("cm:ask-ai-text", { detail: text }));
}

function listNode(d) {
  const items = d.data || [];
  const n = el("div", { class: "ask-ai ask-list" });
  if (!items.length) {
    n.append(el("div", { class: "ask-empty" }, el("div", {}, d.empty ? d.empty.text : "Nothing here.")));
    return n;
  }
  for (const it of items) {
    if (typeof it === "object" && it !== null) {
      const main = it.label || it.title || it.name || it.text || JSON.stringify(it);
      const sub = [it.count !== undefined ? it.count + (it.count_label || "") : null,
                   it.note, it.serial ? "#" + it.serial : null].filter(Boolean).join(" · ");
      n.append(el("div", { class: "ask-list-row" },
        el("div", { class: "ask-list-main" }, main),
        sub ? el("div", { class: "ask-list-sub" }, sub) : null));
    } else n.append(el("div", { class: "ask-list-row" }, String(it)));
  }
  return n;
}

function threadNode(d) {
  d = d || {};
  const n = el("div", { class: "ask-ai ask-thread" },
    el("div", { class: "ask-thread-meta" },
      el("div", {}, "To " + (d.to_addr || "")),
      el("div", { class: "t-faint" }, d.sent_at || "")),
    el("div", { class: "ask-thread-subj" }, d.subject || ""),
    el("div", { class: "ask-paper" }, el("div", { class: "ask-paper-body" }, (d.original_preview || "").replace(/<[^>]+>/g, " ").slice(0, 500))));
  if (d.note) n.append(el("div", { class: "ask-note" }, d.note));
  return n;
}

function analyticsNode(d) {
  d = d || {};
  const n = el("div", { class: "ask-ai ask-analytics" },
    el("div", { class: "ask-stat-tiles" },
      el("div", { class: "ask-tile" }, el("div", { class: "ask-tile-v" }, d.sent ?? 0), el("div", { class: "ask-tile-k" }, "sent")),
      el("div", { class: "ask-tile" }, el("div", { class: "ask-tile-v" }, d.replied ?? 0), el("div", { class: "ask-tile-k" }, "replied")),
      el("div", { class: "ask-tile" }, el("div", { class: "ask-tile-v" }, d.bounced ?? 0), el("div", { class: "ask-tile-k" }, "bounced")),
      el("div", { class: "ask-tile" }, el("div", { class: "ask-tile-v" }, (d.reply_rate ?? 0) + "%"), el("div", { class: "ask-tile-k" }, "reply rate"))));
  const rows = d.by_campaign || [];
  if (rows.length) n.append(tableNode({ data: rows }).childNodes[0] && el("div", {}, tableNode({ data: rows })));
  return n;
}

function activityNode(rows) {
  const n = el("div", { class: "ask-ai ask-activity" });
  if (!rows || !rows.length) { n.append(el("div", { class: "ask-empty" }, "No activity.")); return n; }
  for (const r of rows) {
    n.append(el("div", { class: "ask-act-row" },
      el("div", { class: "ask-act-main" }, (r.detail || r.action || "") + (r.actor ? " · " + r.actor : "")),
      el("div", { class: "t-faint" }, r.at || r.created_at || "")));
  }
  return n;
}

function genericNode(d) {
  if (d === null || d === undefined) return el("div", { class: "ask-ai" }, "—");
  if (typeof d !== "object") return el("div", { class: "ask-ai" }, el("pre", { class: "ask-ev-pre" }, String(d)));
  const n = el("div", { class: "ask-ai" });
  for (const [k, v] of Object.entries(d)) {
    if (v === null || v === undefined) continue;
    if (typeof v === "object") {
      n.append(el("div", { class: "ask-kv-row" },
        el("span", { class: "ask-kv-k" }, k.replace(/_/g, " ")),
        el("pre", { class: "ask-ev-pre", style: "max-height:120px" }, JSON.stringify(v, null, 2))));
    } else {
      n.append(el("div", { class: "ask-kv-row" },
        el("span", { class: "ask-kv-k" }, k.replace(/_/g, " ")),
        el("span", { class: "ask-kv-v" }, String(v))));
    }
  }
  return n;
}

/* ------------------------------------------------------------- card lifecycle */

function pushCard(env) {
  const node = el("div", { class: "ask-ai ask-card-live", "data-card": env.id });
  const body = el("div", { class: "ask-card-body" });
  node.append(body);
  appendNode(node);
  const ctx = {
    env, node, body, type: env.type, status: "live",
    payload: env.payload || {},
    summary: "",
  };
  S.nodes.push(ctx);
  const renderer = CARD_RENDER[env.type] || renderGenericCard;
  try {
    renderer(ctx);
  } catch (e) {
    body.append(errorNode("Couldn't render that card: " + (e.message || e)));
    ctx.status = "error";
  }
  if (ctx.status === "live") {
    S.pendingCard = env;
    S.pendingNode = node;
    if (S.updateHint) S.updateHint();
  }
  return ctx;
}

function finishCard(ctx, summary) {
  if (ctx.status !== "live") return;
  ctx.status = "answered";
  ctx.summary = summary || "Done";
  S.nodes = S.nodes.filter((n) => n !== ctx);
  if (S.pendingCard && S.pendingCard.id === ctx.env.id) {
    S.pendingCard = null;
    S.pendingNode = null;
    if (S.updateHint) S.updateHint();
  }
  const live = ctx.body;
  const done = el("div", { class: "ask-card-done", role: "button", tabindex: "0",
    "aria-expanded": "false", onclick: () => toggleDone(ctx, done, live) },
    icon("check-circle-2", "i i-14"), el("span", {}, ctx.summary));
  live.classList.add("ask-card-collapsed");
  ctx.node.append(done);
  done.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleDone(ctx, done, live); }
  });
  histPush({ role: "assistant", content: "[card " + ctx.type + "] " + ctx.summary.slice(0, 160) });
  scrollBottom();
}

function toggleDone(ctx, done, live) {
  const open = live.style.display !== "none";
  live.style.display = open ? "none" : "";
  live.querySelectorAll("button, input, select, textarea, a").forEach((n) => { n.disabled = true; n.classList.add("inert"); });
  done.setAttribute("aria-expanded", open ? "false" : "true");
}

function inertCard(ctx, label) {
  if (ctx.status !== "live") return;
  ctx.status = "cancelled";
  S.nodes = S.nodes.filter((n) => n !== ctx);
  if (S.pendingCard && S.pendingCard.id === ctx.env.id) {
    S.pendingCard = null;
    S.pendingNode = null;
    if (S.updateHint) S.updateHint();
  }
  const live = ctx.body;
  live.querySelectorAll("button, input, select, textarea").forEach((n) => { n.disabled = true; });
  live.classList.add("ask-card-collapsed");
  ctx.node.append(el("div", { class: "ask-card-done ask-card-inert" },
    icon("x", "i i-14"), el("span", {}, label || "Cancelled")));
}

function newIdem(cardId) {
  return cardId + ":" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

async function respondCard(ctx, response, summary) {
  if (ctx.status !== "live") return;
  ctx.status = "sending";
  ctx.body.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  const idem = newIdem(ctx.env.id);
  try {
    const d = await api("/api/ai/cards/" + ctx.env.id + "/respond",
      { body: { response, idempotency_key: idem } });
    finishCard(ctx, summary || "Done");
    S.convId = d.conv_id || S.convId;
    await renderOutcomes(d.outcomes || []);
  } catch (e) {
    ctx.status = "live";
    ctx.body.querySelectorAll("button").forEach((b) => { if (!b.dataset.keep) b.disabled = false; });
    toast(e.message || String(e), { type: "error" });
  }
}

async function cancelCardAction(ctx, summary) {
  if (ctx.status !== "live") return;
  ctx.status = "sending";
  try {
    const d = await api("/api/ai/cards/" + ctx.env.id + "/cancel", { method: "POST" });
    inertCard(ctx, summary || "Cancelled - nothing was done");
    S.convId = d.conv_id || S.convId;
    await renderOutcomes(d.outcomes || []);
  } catch (e) {
    ctx.status = "live";
    ctx.body.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    toast(e.message || String(e), { type: "error" });
  }
}

/* ------------------------------------------------------------- options card
   (with the overflow "More…" disclosure: an in-card, searchable list -
   never a native <select>, never an unbounded button spill) */

function renderOptionsCard(ctx) {
  const p = ctx.payload;
  ctx.body.append(el("div", { class: "ask-card-head ask-q-head" },
    icon("message-square", "i"), el("span", {}, p.question || "Choose one")));
  const opts = (p.options || []).filter((o) => o && o.label);
  const more = p.more && p.more.options ? p.more.options.filter((o) => o && o.label) : [];
  const total = opts.length + more.length;
  const MAX = 4;
  const visible = total > MAX ? opts.slice(0, Math.min(MAX, opts.length)) : opts;
  const hidden = total > MAX ? (opts.slice(Math.min(MAX, opts.length))).concat(more) : [];

  const listHost = el("div", { class: "ask-opts" });
  const mkOpt = (o) => el("button", {
    class: "ask-opt" + (o.style === "danger" ? " danger" : ""),
    "data-id": o.id,
    onclick: (e) => {
      if (e.currentTarget.disabled) return;
      respondCard(ctx, { id: o.id }, o.label);
    },
  }, o.label);
  visible.forEach((o) => listHost.append(mkOpt(o)));

  if (hidden.length) {
    const panel = el("div", { class: "ask-more-panel", style: "display:none" });
    const search = el("input", { class: "input ask-more-search", placeholder: "Search…",
      "aria-label": "Search options", style: hidden.length >= 6 ? "" : "display:none" });
    const host = el("div", { class: "ask-more-list" });
    const renderHidden = (q) => {
      host.innerHTML = "";
      const list = hidden.filter((o) => !q || (o.label || "").toLowerCase().includes(q));
      if (!list.length) host.append(el("div", { class: "t-faint", style: "padding:6px" }, "No matches."));
      list.forEach((o) => host.append(mkOpt(o)));
    };
    renderHidden("");
    search.addEventListener("input", () => renderHidden(search.value.trim().toLowerCase()));
    panel.append(search, host);
    const moreBtn = el("button", { class: "ask-opt ask-more-btn", onclick: () => {
      const open = panel.style.display !== "none";
      panel.style.display = open ? "none" : "";
      moreBtn.textContent = open ? "More…" : "Show less";
      if (!open) search.focus();
    } }, "More… (" + hidden.length + ")");
    listHost.append(moreBtn, panel);
  }
  ctx.body.append(listHost);

  if (p.allow_other) {
    const otherBox = el("div", { class: "ask-other", style: "display:none" },
      el("input", { class: "input", placeholder: "Type your answer…", "aria-label": "Other answer" }),
      el("button", { class: "btn btn-primary btn-sm", "aria-label": "Send answer" }, icon("send", "i i-14")));
    const otherBtn = el("button", { class: "ask-opt ask-other-btn", onclick: () => {
      otherBox.style.display = "flex";
      otherBox.querySelector("input").focus();
    } }, "Something else…");
    const doOther = () => {
      const v = otherBox.querySelector("input").value.trim();
      if (!v) return;
      respondCard(ctx, { text: v }, v.length > 40 ? v.slice(0, 40) + "…" : v);
    };
    otherBox.querySelector("button").addEventListener("click", doOther);
    otherBox.querySelector("input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); doOther(); }
    });
    ctx.body.append(otherBtn, otherBox);
  }

  /* Esc cancels the card (spec 12.7) */
  const onKey = (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      document.removeEventListener("keydown", onKey, true);
      cancelCardAction(ctx, "Cancelled");
    }
  };
  setTimeout(() => document.addEventListener("keydown", onKey, true), 0);
}

/* ------------------------------------------------------------- input cards */

function inputCard(ctx, cfg) {
  const p = cfg.payload;
  const label = p.label || "Value";
  ctx.body.append(el("div", { class: "ask-card-head ask-q-head" }, icon("edit-3", "i"), el("span", {}, label)));
  const errEl = el("div", { class: "ask-field-err", style: "display:none" });
  const validate = (v) => {
    switch (p.validate || "none") {
      case "email": return vEmail(v) ? null : "Enter a valid email address.";
      case "emails": return vEmails(v);
      case "url": return vUrl(v);
      case "name": return vName(v);
      default: return v.trim() ? null : "Enter a value.";
    }
  };
  let input;
  if (cfg.type === "textarea") {
    input = el("textarea", { class: "textarea", rows: 4, placeholder: p.placeholder || "",
      "aria-label": label });
    if (p.default) input.value = p.default;
  } else {
    input = el("input", { class: "input", placeholder: p.placeholder || "",
      "aria-label": label });
    if (p.default !== undefined && p.default !== null) input.value = p.default;
  }
  const submit = el("button", { class: "btn btn-primary btn-sm", "aria-label": "Submit " + label },
    icon("check", "i i-14"), "OK");
  const doSubmit = () => {
    const v = input.value;
    const err = validate(v);
    if (err) { errEl.textContent = err; errEl.style.display = "block"; return; }
    errEl.style.display = "none";
    respondCard(ctx, cfg.response(v), (v || "").length > 40 ? String(v).slice(0, 40) + "…" : String(v || "OK"));
  };
  submit.addEventListener("click", doSubmit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && cfg.type !== "textarea") { e.preventDefault(); doSubmit(); }
  });
  const onKey = (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      document.removeEventListener("keydown", onKey, true);
      cancelCardAction(ctx, "Cancelled");
    }
  };
  setTimeout(() => document.addEventListener("keydown", onKey, true), 0);
  requestAnimationFrame(() => input.focus());
  ctx.body.append(input, errEl, el("div", { class: "ask-card-actions" }, submit));
}

function renderTextCard(ctx) {
  inputCard(ctx, { payload: ctx.payload, type: ctx.payload.multiline ? "textarea" : "text",
    response: (v) => ({ text: v.trim() }) });
}
function renderNumberCard(ctx) {
  const p = ctx.payload;
  const payload = { ...p, validate: "none" };
  const wrap = el("div", {});
  inputCard({ ...ctx, payload, body: wrap }, { payload, type: "text",
    response: (v) => ({ value: v.trim() }) });
  ctx.body.append(wrap);
  /* client-side range check on the wrapper's input */
  const input = wrap.querySelector("input");
  if (input) {
    const origFocus = input;
    input.addEventListener("input", () => {
      const n = Number(input.value);
      if (input.value === "") return;
      const bad = Number.isNaN(n) || (p.min !== undefined && n < Number(p.min)) || (p.max !== undefined && n > Number(p.max));
      input.classList.toggle("invalid", bad);
    });
  }
}
function renderDatetimeCard(ctx) {
  const p = ctx.payload;
  const toLocal = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const pad = (x) => String(x).padStart(2, "0");
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  };
  const input = el("input", { class: "input", type: "datetime-local", "aria-label": p.label || "When" });
  if (p.min_iso) input.min = toLocal(p.min_iso);
  if (p.max_iso) input.max = toLocal(p.max_iso);
  if (p.default) input.value = toLocal(p.default);
  ctx.body.append(el("div", { class: "ask-card-head ask-q-head" }, icon("calendar", "i"), el("span", {}, p.label || "When")));
  if (p.timezone_label) ctx.body.append(el("div", { class: "t-faint" }, p.timezone_label));
  const errEl = el("div", { class: "ask-field-err", style: "display:none" });
  const submit = el("button", { class: "btn btn-primary btn-sm", "aria-label": "Submit time" }, icon("check", "i i-14"), "OK");
  const doSubmit = () => {
    const v = input.value;
    if (!v) { errEl.textContent = "Pick a time."; errEl.style.display = "block"; return; }
    respondCard(ctx, { value: v }, v.replace("T", " "));
  };
  submit.addEventListener("click", doSubmit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doSubmit(); } });
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); document.removeEventListener("keydown", onKey, true); cancelCardAction(ctx, "Cancelled"); } };
  setTimeout(() => document.addEventListener("keydown", onKey, true), 0);
  requestAnimationFrame(() => input.focus());
  ctx.body.append(input, errEl, el("div", { class: "ask-card-actions" }, submit));
}
function renderSecretCard(ctx) {
  const p = ctx.payload;
  ctx.body.append(el("div", { class: "ask-card-head ask-q-head" }, icon("key", "i"), el("span", {}, p.label || "Secret")));
  ctx.body.append(el("div", { class: "ask-secret-note" },
    "Stored securely on the server. Never shown again, never logged, never sent to the model."));
  const input = el("input", { class: "input", type: "password", "aria-label": p.label || "Secret",
    placeholder: "Paste the key / secret" });
  const submit = el("button", { class: "btn btn-primary btn-sm", "aria-label": "Save secret" }, icon("check", "i i-14"), "Save");
  const doSubmit = () => {
    const v = input.value.trim();
    if (!v) return;
    input.value = "";
    respondCard(ctx, { value: v }, "Key saved (never stored in history)");
  };
  submit.addEventListener("click", doSubmit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doSubmit(); } });
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); document.removeEventListener("keydown", onKey, true); cancelCardAction(ctx, "Cancelled"); } };
  setTimeout(() => document.addEventListener("keydown", onKey, true), 0);
  requestAnimationFrame(() => input.focus());
  ctx.body.append(input, el("div", { class: "ask-card-actions" }, submit));
}

/* ------------------------------------------------------------- file request */

function renderFileCard(ctx) {
  const p = ctx.payload;
  const accept = (p.accept || []).join(",");
  const inp = el("input", { type: "file", accept, style: "display:none" });
  const zone = el("div", { class: "ask-filezone", role: "button", tabindex: "0",
    "aria-label": "Attach a file" },
    icon("paperclip", "i"),
    el("div", {}, p.label || "Attach a file"),
    el("div", { class: "t-faint" }, (p.accept || []).join(" ") + (p.max_mb ? " · up to " + p.max_mb + " MB" : "")));
  let file = null;
  const setFile = (f) => {
    if (!f) return;
    if (p.max_mb && f.size > p.max_mb * 1024 * 1024) {
      toast("File too large (max " + p.max_mb + " MB).", { type: "error" });
      return;
    }
    const ok = (p.accept || []).some((a) => f.name.toLowerCase().endsWith(a));
    if (p.accept && p.accept.length && !ok) {
      toast("Unsupported file type. Accepted: " + (p.accept || []).join(", "), { type: "error" });
      return;
    }
    file = f;
    zone.innerHTML = "";
    zone.append(icon("check-circle-2", "i"), el("span", {}, f.name),
      el("span", { class: "t-faint" }, Math.round(f.size / 1024) + " KB"));
  };
  zone.addEventListener("click", () => inp.click());
  zone.addEventListener("keydown", (e) => { if (e.key === "Enter") inp.click(); });
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag");
    setFile(e.dataTransfer.files && e.dataTransfer.files[0]);
  });
  inp.addEventListener("change", () => setFile(inp.files && inp.files[0]));
  const submit = el("button", { class: "btn btn-primary btn-sm", "aria-label": "Attach file" },
    icon("check", "i i-14"), "Attach");
  submit.addEventListener("click", async () => {
    if (!file) { toast("Pick a file first.", { type: "error" }); return; }
    submit.disabled = true;
    const data = await base64Of(file);
    respondCard(ctx, { file: { name: file.name, data } }, file.name);
  });
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); document.removeEventListener("keydown", onKey, true); cancelCardAction(ctx, "Cancelled"); } };
  setTimeout(() => document.addEventListener("keydown", onKey, true), 0);
  ctx.body.append(inp, zone, el("div", { class: "ask-card-actions" }, submit));
}

/* ------------------------------------------------------------- table edit */

function renderTableCard(ctx) {
  const p = ctx.payload;
  if (p.question) ctx.body.append(el("div", { class: "ask-card-head ask-q-head" },
    icon("table", "i"), el("span", {}, p.question)));
  const cols = p.columns || [];
  const hasColumns = Array.isArray(cols) && cols.length > 1;
  const rows = el("div", { class: "ask-tedit" });
  for (const f of p.fields || []) {
    const row = el("div", { class: "ask-tedit-row" });
    row.append(el("div", { class: "ask-tedit-label", title: f.field }, f.field));
    if (hasColumns) {
      const sel = el("select", { class: "select ask-tedit-sel", "aria-label": "Column for " + f.field });
      sel.append(el("option", { value: "" }, "— fixed value —"));
      cols.filter(Boolean).forEach((c) => sel.append(el("option", { value: c }, c)));
      const txt = el("input", { class: "input", placeholder: "Fixed value", "aria-label": "Fixed value for " + f.field,
        style: "display:none" });
      const sync = () => {
        txt.style.display = sel.value === "" ? "" : "none";
        txt.value = (String(f.value || "").replace(/^literal:/, ""));
      };
      if (String(f.value || "").startsWith("column:") || (f.value && cols.includes(f.value))) {
        sel.value = f.value;
      }
      sel.addEventListener("change", () => { sync(); });
      sync();
      row.append(sel, txt);
      f._get = () => sel.value ? sel.value : (txt.value.trim() ? "literal:" + txt.value.trim() : "");
    } else {
      const txt = el("input", { class: "input", type: f.field && f.field.includes("@") ? "email" : "text",
        value: f.value || "", "aria-label": "Value for " + f.field });
      row.append(txt);
      f._get = () => txt.value;
    }
    rows.append(row);
  }
  const submit = el("button", { class: "btn btn-primary btn-sm", "aria-label": "Apply" },
    icon("check", "i i-14"), "Apply");
  submit.addEventListener("click", () => {
    const fields = (p.fields || []).map((f) => {
      const o = { field: f.field, value: f._get() };
      if (f.contact_id) o.contact_id = f.contact_id;
      return o;
    });
    respondCard(ctx, { fields }, (p.fields || []).length + " rows applied");
  });
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); document.removeEventListener("keydown", onKey, true); cancelCardAction(ctx, "Cancelled"); } };
  setTimeout(() => document.addEventListener("keydown", onKey, true), 0);
  ctx.body.append(rows, el("div", { class: "ask-card-actions" }, submit));
}

/* ------------------------------------------------------------- confirm + final review */

function riskBadge(risk) {
  const lbl = { R2: "App change", R3: "Destructive", R4: "Outbound", R5: "Platform-critical" }[risk] || risk;
  return el("span", { class: "ask-risk r-" + String(risk || "").toLowerCase(), title: "Risk level " + risk }, lbl);
}

function bulkControls(ctx, p, host) {
  const b = p.bulk;
  if (!b || !b.needed) return null;
  const box = el("div", { class: "ask-bulk" });
  const count = p.count || 0;
  const cb = el("input", { type: "checkbox", "aria-label": "I have reviewed this bulk send" });
  const cbRow = el("label", { class: "ask-bulk-row" },
    cb, el("span", {}, "I have reviewed these " + count + " emails and the details above."));
  let typed = null;
  if (b.typed) {
    typed = el("input", { class: "input", placeholder: "Type " + count + " to confirm", "inputmode": "numeric",
      "aria-label": "Type the exact count" });
  }
  box.append(cbRow, typed);
  host.append(box);
  ctx._bulk = () => {
    if (!cb.checked) return "Tick the acknowledgement box to continue.";
    if (b.typed && Number(typed.value) !== count) return "Type the exact count (" + count + ") to confirm.";
    return null;
  };
  const ack = () => cb.checked ? { reviewed: true, typed_count: b.typed ? String(typed.value) : undefined } : {};
  ctx._ack = ack;
  return box;
}

function presendNode(lines, blocked) {
  const n = el("div", { class: "ask-presend" });
  const iconFor = (sev) => sev === "block" ? "x-circle" : sev === "warn" ? "alert-triangle" : "info";
  const cls = (sev) => "ps-" + (sev === "block" ? "block" : sev === "warn" ? "warn" : "info");
  const list = (lines || []).filter((l) => l && l.text);
  if (!list.length) {
    n.append(el("div", { class: "ask-presend-line ps-pass" }, icon("check-circle-2", "i i-14"),
      el("span", {}, "All checks passed")));
    return n;
  }
  if (!blocked && list.every((l) => l.severity !== "block")) {
    /* collapse non-blocking to a summary, expandable */
    const det = el("details", { class: "ask-presend-det" },
      el("summary", {}, icon("check-circle-2", "i i-14"), el("span", {}, "All required checks passed (" + list.length + " notes)")));
    det.append(el("div", { class: "ask-presend-list" }, list.map(lineEl)));
    n.append(det);
    return n;
  }
  n.append(el("div", { class: "ask-presend-list" }, list.map(lineEl)));
  function lineEl(l) {
    return el("div", { class: "ask-presend-line " + cls(l.severity) },
      icon(iconFor(l.severity), "i i-14"),
      el("span", { class: "ask-presend-text" }, l.text),
      l.fix ? el("span", { class: "t-faint", title: "The matching Fix step is offered with the alternatives" }, "fix available") : null);
  }
  return n;
}

function safetyNode(s) {
  s = s || {};
  const n = el("div", { class: "ask-safety" });
  const items = [];
  items.push(s.safe_mode_on ? "Safe mode: ON (live sending blocked)" : "Safe mode: off - live sending allowed");
  if (s.sent_today !== undefined) items.push("Today: " + s.sent_today + " / " + s.cap + " sent · " + s.remaining + " left");
  if (s.held) items.push(s.held + " email(s) held in an undo window");
  if (s.account) items.push("Account: " + s.account.email + (s.account.token_ok ? "" : " - token problem"));
  items.forEach((t) => n.append(el("div", { class: "ask-safety-line" }, t)));
  return n;
}

function paperPreview(recipient, p) {
  const from = (p.from && (p.from.email || p.from)) || "";
  const n = el("div", { class: "ask-paper" },
    el("div", { class: "ask-paper-meta" },
      from ? el("div", {}, el("span", { class: "t-faint" }, "From "), from) : null,
      el("div", {}, el("span", { class: "t-faint" }, "To "), recipient.to || "—"),
      (p.cc || (recipient.cc || "")) ? el("div", {}, el("span", { class: "t-faint" }, "Cc "), p.cc || recipient.cc) : null),
    el("div", { class: "ask-paper-subj" }, recipient.subject || ""),
    el("div", { class: "ask-paper-body", html: (recipient.body_html || "") + (p.signature_html ? "<div class='ask-sig'>" + p.signature_html + "</div>" : "") }),
    (p.attachments && p.attachments.length) ? el("div", { class: "ask-paper-atts" },
      el("span", { class: "t-faint" }, "Attachments: " + p.attachments.join(", "))) : null);
  return n;
}

function recipientBrowser(ctx, p) {
  const recs = (p.preview && p.preview.recipients) || [];
  const total = (p.preview && p.preview.total) || recs.length;
  if (!recs.length) {
    return el("div", { class: "ask-recips" },
      el("div", { class: "t-faint" }, total ? total + " recipients (previews render for the first 12)" : "No recipients to preview."));
  }
  let i = 0;
  const host = el("div", {});
  const nav = el("div", { class: "ask-recip-nav" },
    el("button", { class: "btn btn-ghost btn-sm", "aria-label": "Previous recipient", onclick: () => { i = Math.max(0, i - 1); render(); } }, icon("chevron-left", "i i-14")),
    el("span", { class: "ask-recip-pos t-caption" }),
    el("input", { class: "input ask-recip-search", placeholder: "Search to…", "aria-label": "Search recipients" }),
    el("button", { class: "btn btn-ghost btn-sm", "aria-label": "Next recipient", onclick: () => { i = Math.min(recs.length - 1, i + 1); render(); } }, icon("chevron-right", "i i-14")));
  function render() {
    host.innerHTML = "";
    host.append(paperPreview(recs[i], p));
    nav.querySelector(".ask-recip-pos").textContent = "Recipient " + (i + 1) + " of " + total;
  }
  nav.querySelector(".ask-recip-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    if (!q) return;
    const j = recs.findIndex((r) => (r.to || "").toLowerCase().includes(q) || String(r.serial).includes(q));
    if (j >= 0) { i = j; render(); }
  });
  render();
  return el("div", { class: "ask-recips" }, nav, host);
}

function renderConfirmCard(ctx, isReview) {
  const p = ctx.payload;
  const danger = p.danger === true || (p.risk === "R3" || p.risk === "R5");
  ctx.node.classList.add(danger ? "ask-card-danger" : "ask-card-gate");
  ctx.body.append(el("div", { class: "ask-card-head ask-gate-head" },
    icon("alert-circle", "i"), el("span", {}, "CONFIRM ACTION"), riskBadge(p.risk)));
  if (p.title) ctx.body.append(el("div", { class: "ask-gate-title" }, p.title));
  if (p.details && p.details.length) {
    ctx.body.append(el("div", { class: "ask-kv" }, p.details.map((d) =>
      el("div", { class: "ask-kv-row" }, el("span", { class: "ask-kv-k" }, d.k),
        el("span", { class: "ask-kv-v" }, String(d.v === null || d.v === undefined ? "—" : d.v))))));
  }
  if (p.impact && p.impact.length) {
    ctx.body.append(el("div", { class: "ask-impact" }, p.impact.map((t) => el("div", { class: "ask-impact-line" }, icon("info", "i i-13"), t))));
  }
  if (p.notes && p.notes.length) {
    ctx.body.append(el("div", { class: "ask-note" }, p.notes.join(" ")));
  }
  if (isReview && p.preview) {
    ctx.body.append(recipientBrowser(ctx, p));
    if (p.facts && p.facts.length) {
      ctx.body.append(el("div", { class: "ask-kv" }, p.facts.map((d) =>
        el("div", { class: "ask-kv-row" }, el("span", { class: "ask-kv-k" }, d.k),
          el("span", { class: "ask-kv-v" }, String(d.v))))));
    }
    ctx.body.append(presendNode(p.presend && p.presend.lines, p.presend && p.presend.blocked));
    ctx.body.append(safetyNode(p.safety));
  }
  bulkControls(ctx, p, ctx.body);

  const actions = el("div", { class: "ask-card-actions ask-gate-actions" });
  const primary = el("button", { class: "btn btn-primary" + (danger ? " btn-danger" : ""),
    "aria-label": p.primary ? p.primary.label : "Confirm" }, p.primary ? p.primary.label : "Confirm");
  const blocked = isReview && p.presend && p.presend.blocked;
  const gate = async (action) => {
    if (blocked) { toast("A blocking check must be fixed first.", { type: "error" }); return; }
    const berr = ctx._bulk ? ctx._bulk() : null;
    if (berr) { toast(berr, { type: "error" }); return; }
    const resp = { action };
    if (action === "confirm" && ctx._ack) resp.ack = ctx._ack();
    const summary = action === "confirm" ? (p.primary ? p.primary.label : "Confirmed")
      : action === "cancel" ? "Cancelled" : "Went back";
    respondCard(ctx, resp, summary);
  };
  primary.addEventListener("click", () => gate("confirm"));
  const secondaries = [];
  if (isReview) {
    secondaries.push(el("button", { class: "btn btn-ghost", onclick: () => gate("edit") }, "Edit"),
      el("button", { class: "btn btn-ghost", onclick: () => gate("back") }, "Back"));
  }
  for (const sec of (p.secondary || [])) {
    if (sec.id === "cancel") continue;
    secondaries.push(el("button", { class: "btn btn-ghost", onclick: () => gate(sec.id === "edit" ? "edit" : "back") }, sec.label));
  }
  secondaries.push(el("button", { class: "btn btn-ghost", onclick: () => cancelCardAction(ctx, "Cancelled - nothing was done") }, "Cancel"));
  actions.append(primary, ...secondaries);
  if (blocked) {
    primary.disabled = true;
    ctx.body.append(el("div", { class: "ask-field-err", style: "display:block" },
      "A blocking pre-send check is failing - fix it before you can continue."));
  }
  ctx.body.append(actions);

  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); document.removeEventListener("keydown", onKey, true); cancelCardAction(ctx, "Cancelled - nothing was done"); } };
  setTimeout(() => document.addEventListener("keydown", onKey, true), 0);
}

/* ------------------------------------------------------------- handoff + budget */

function renderHandoffCard(ctx) {
  const p = ctx.payload;
  ctx.node.classList.add("ask-card-handoff");
  ctx.body.append(el("div", { class: "ask-card-head" }, icon("external-link", "i"), el("span", {}, p.title || "Open in the app")));
  if (p.why) ctx.body.append(el("div", { class: "ask-text" }, p.why));
  ctx.body.append(el("div", { class: "ask-links" }, (p.links || []).map((l) =>
    el("a", { class: "btn btn-secondary btn-sm", href: l.page, "data-ask-link": "1" }, l.label))));
  if (p.recheck) {
    const b = el("button", { class: "btn btn-ghost btn-sm", onclick: () =>
      respondCard(ctx, { id: "recheck" }, "Checked") }, "I've done it - check now");
    ctx.body.append(b);
  }
}

function renderBudgetCard(ctx) {
  const p = ctx.payload;
  ctx.node.classList.add("ask-card-budget");
  ctx.body.append(el("div", { class: "ask-card-head" }, icon("gauge", "i"),
    el("span", {}, "AI budget paused")));
  ctx.body.append(el("div", { class: "ask-text" },
    "You've used today's AI budget. Free text that needs the model is paused until " +
    (p.reset_at || "the next reset") + ". Reading data and deterministic flows still work."));
  if (p.stays_working && p.stays_working.length) {
    ctx.body.append(el("div", { class: "ask-opts" }, p.stays_working.map((o) =>
      el("button", { class: "ask-opt", onclick: (e) => respondCard(ctx, { id: o.id }, o.label) }, o.label))));
  }
  const dismiss = el("button", { class: "btn btn-ghost btn-sm", onclick: () => inertCard(ctx, "Budget notice dismissed") }, "Dismiss");
  ctx.body.append(dismiss);
}

function renderGenericCard(ctx) {
  ctx.body.append(el("div", { class: "ask-card-head" }, icon("info", "i"), el("span", {}, "Card")));
  ctx.body.append(el("pre", { class: "ask-ev-pre" }, JSON.stringify(ctx.payload, null, 2)));
}

/* ------------------------------------------------------------- draft editor */

function renderEditorCard(ctx) {
  const p = ctx.payload;
  ctx.node.classList.add("ask-card-editor");
  const top = el("div", { class: "ask-card-head" }, icon("pen-tool", "i"),
    el("span", {}, { reply: "Draft reply", template: "Template", reminder: "Reminder email",
                     one_off: "Email" }[p.kind] || "Draft email"));
  if (p.to_fixed) top.append(el("span", { class: "t-faint ask-to-fixed" }, "To " + p.to_fixed));
  ctx.body.append(top);

  const subj = el("input", { class: "input", "aria-label": "Subject", placeholder: "Subject", value: p.subject || "" });
  const editor = makeEditor(p.body_html || "", { fields: p.fields || [] });
  const status = el("div", { class: "ask-ed-status t-faint" });
  const countStatus = () => {
    const words = (editor.text().match(/\S+/g) || []).length;
    status.textContent = (editor.dirty ? "● edited · " : "") + words + " words";
  };
  editor.body.addEventListener("input", () => { editor.markDirty(); countStatus(); });
  countStatus();

  const bodyHtml = () => {
    let h = editor.html();
    /* convert pills back to {field} placeholders */
    return h;
  };

  /* subjects suggestions slot */
  const subjectsBox = el("div", { class: "ask-subjects", style: "display:none" },
    el("div", { class: "t-caption" }, "Suggested subjects"));
  ctx.subjects = (list) => {
    subjectsBox.querySelectorAll(".ask-opt").forEach((n) => n.remove());
    (list || []).slice(0, 3).forEach((s) => subjectsBox.append(
      el("button", { class: "ask-opt", onclick: () => {
        subj.value = s;
        subjectsBox.style.display = "none";
      } }, s)));
    if (!(list || []).length) subjectsBox.append(el("div", { class: "t-faint" }, "No suggestions."));
    subjectsBox.style.display = "";
  };

  /* AI controls */
  const aiBar = el("div", { class: "ask-ai-bar" });
  const orig = { subject: p.subject || "", body: p.body_html || "" };
  if (p.ai !== false) {
    const reviseBox = el("div", { class: "ask-revise", style: "display:none" },
      el("input", { class: "input", placeholder: "e.g. make it shorter, more formal…", "aria-label": "Revision instruction" }),
      el("button", { class: "btn btn-secondary btn-sm", "aria-label": "Apply revision" }, "Apply"));
    const reviseBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => {
      reviseBox.style.display = reviseBox.style.display === "none" ? "flex" : "none";
      if (reviseBox.style.display !== "none") reviseBox.querySelector("input").focus();
    } }, "Revise with AI…");
    reviseBox.querySelector("button").addEventListener("click", async () => {
      const instruction = reviseBox.querySelector("input").value.trim();
      if (!instruction) return;
      reviseBox.querySelectorAll("button, input").forEach((n) => { n.disabled = true; });
      const idem = newIdem(ctx.env.id);
      try {
        const d = await api("/api/ai/cards/" + ctx.env.id + "/respond",
          { body: { response: { action: "revise", body_html: bodyHtml(), instruction }, idempotency_key: idem } });
        /* card stays pending: refresh arrives via editor_refresh outcome */
        renderOutcomes(d.outcomes || []);
        reviseBox.querySelector("input").value = "";
      } catch (e) { toast(e.message, { type: "error" }); }
      reviseBox.querySelectorAll("button, input").forEach((n) => { n.disabled = false; });
    });
    reviseBox.querySelector("input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); reviseBox.querySelector("button").click(); }
    });
    const subjectsBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () =>
      respondCardKeep(ctx, { action: "subjects" }, null) }, "Suggest subjects");
    const resetBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => {
      subj.value = orig.subject;
      editor.set(orig.body);
      countStatus();
    } }, "Reset to AI version");
    aiBar.append(reviseBtn, subjectsBtn, resetBtn, reviseBox);
  }
  ctx.body.append(el("div", { class: "ask-ed-subj-row" }, subj), editor.node, status, subjectsBox, aiBar);
  ctx.refresh = (d) => {
    if (d.subject !== undefined) subj.value = d.subject;
    if (d.body_html !== undefined) editor.set(d.body_html);
    countStatus();
  };

  const actions = el("div", { class: "ask-card-actions" },
    el("button", { class: "btn btn-primary", "aria-label": "Continue",
      onclick: () => {
        if (!subj.value.trim()) { toast("Give it a subject first.", { type: "error" }); return; }
        respondCard(ctx, { action: "continue", subject: subj.value.trim(), body_html: bodyHtml() }, "Draft ready");
      } }, "Continue"),
    el("button", { class: "btn btn-ghost", onclick: () => cancelCardAction(ctx, "Cancelled - draft discarded") }, "Cancel"),
    el("button", { class: "btn btn-ghost", onclick: () => gateBack(ctx) }, "Back"));
  ctx.body.append(actions);

  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); document.removeEventListener("keydown", onKey, true); cancelCardAction(ctx, "Cancelled - draft discarded"); } };
  setTimeout(() => document.addEventListener("keydown", onKey, true), 0);
}

/* like respondCard but the card STAYS pending (editor keeps its state) */
async function respondCardKeep(ctx, response) {
  if (ctx.status !== "live") return;
  const idem = newIdem(ctx.env.id);
  try {
    const d = await api("/api/ai/cards/" + ctx.env.id + "/respond",
      { body: { response, idempotency_key: idem } });
    await renderOutcomes(d.outcomes || []);
  } catch (e) {
    toast(e.message || String(e), { type: "error" });
  }
}
function gateBack(ctx) {
  respondCard(ctx, { action: "back" }, "Went back");
}

/* ------------------------------------------------------------- undo countdown */

function renderUndoCard(ctx) {
  const p = ctx.payload;
  ctx.node.classList.add("ask-card-undo");
  const releaseAtMs = (p.release_at || 0) * 1000;
  const total = p.undo_seconds || Math.max(0, Math.round((releaseAtMs - Date.now()) / 1000));
  const row = el("div", { class: "ask-undo-row" });
  const text = el("div", { class: "ask-undo-text" }, "Sending " + p.count + " emails");
  const live = el("span", { class: "ask-undo-live", "aria-live": "polite" });
  const undoBtn = el("button", { class: "btn btn-secondary", "aria-label": "Undo send" }, "Undo");
  row.append(text, live, undoBtn);
  ctx.body.append(row, el("div", { class: "t-faint" }, "Nothing reaches Gmail until the countdown ends."));

  let announced10 = false, released = false;
  const tick = () => {
    const left = Math.max(0, Math.ceil((releaseAtMs - Date.now()) / 1000));
    if (S.open && ctx.status === "live") {
      live.textContent = "Undo (" + left + " s)";
      if (left <= 10 && !announced10) { announced10 = true; live.textContent = "Undo - about to send (" + left + " s)"; }
      if (left <= 0) {
        clearInterval(tickFn);
        released = true;
        live.textContent = "Releasing…";
        respondCard(ctx, { action: "release" }, "Undo window released");
      }
    }
  };
  const tickFn = setInterval(tick, 500);
  tick();
  ctx._undo = { env: ctx.env, releaseAtMs, timer: tickFn, count: p.count, left: total };

  undoBtn.addEventListener("click", () => {
    clearInterval(tickFn);
    respondCard(ctx, { action: "undo" }, "Send undone - nothing was sent");
  });

  /* if the drawer closes, the window keeps running via toast */
  const onClose = () => {
    if (ctx.status !== "live") return;
    S.undo = { env: ctx.env, releaseAtMs, timer: null, count: p.count, left: total, ctx };
  };
  document.addEventListener("cm:drawer-closed", onClose, { once: true });
  ctx._onClose = onClose;
}

function tickUndo(u) {
  const left = Math.max(0, Math.ceil((u.releaseAtMs - Date.now()) / 1000));
  if (u.toastMsg) u.toastMsg.textContent = "Sending " + u.count + " emails — Undo (" + left + " s)";
  if (left <= 0) {
    clearInterval(u.timer);
    u.timer = null;
    if (u.ctx) respondCard(u.ctx, { action: "release" }, "Undo window released");
    if (u.toastHandle) u.toastHandle.dismiss();
    S.undo = null;
    return;
  }
  S.undo = u;
}

function undoClick(env) {
  /* called from the toast action with the original card env */
  const ctx = S.nodes.find((n) => n.env && n.env.id === env.id);
  if (ctx && ctx.status === "live") {
    clearInterval(ctx._undo && ctx._undo.timer);
    respondCard(ctx, { action: "undo" }, "Send undone - nothing was sent");
  }
  S.undo = null;
}

/* ------------------------------------------------------------- compact review */

const ACTION_LABELS = {
  queue: "Queue", draft: "Gmail drafts", schedule: "Schedule", send: "Send now",
};
function compactPrimaryLabel(p, atText) {
  const n = p.count || 0;
  const a = p.action_selected;
  if (a === "queue") return "Queue " + n + " emails (nothing is sent)";
  if (a === "draft") return "Create " + n + " Gmail drafts";
  if (a === "schedule") return "Schedule " + n + " emails for " + (atText || "…");
  if (a === "send") return "Send " + n + " emails now";
  return "Choose what to do with these emails";
}

function renderCompactCard(ctx) {
  const p = ctx.payload;
  ctx.node.classList.add("ask-card-compact");
  ctx._version = p.version || 0;
  ctx._apply = (np) => {
    if (!np) return;
    if (typeof np.version === "number" && np.version !== ctx._version + 0) {
      /* stale - but accept if equal (duplicate) */
      if (np.version <= ctx._version) return;
    }
    ctx._version = np.version;
    ctx.payload = np;
    rebuild();
  };
  ctx.update = (d) => ctx._apply(d);

  const dirty = {};   // local field overrides awaiting debounce
  let flushTimer = null;
  const scheduleFlush = () => {
    clearTimeout(flushTimer);
    flushTimer = setTimeout(flush, 400);
  };
  const flush = async () => {
    clearTimeout(flushTimer);
    const fields = {};
    for (const [k, v] of Object.entries(dirty)) fields[k] = v;
    for (const k of Object.keys(dirty)) delete dirty[k];
    if (!Object.keys(fields).length) return;
    const ver = ctx._version;
    try {
      const d = await api("/api/ai/cards/" + ctx.env.id + "/update",
        { body: { fields, version: ver, idempotency_key: newIdem(ctx.env.id) } });
      for (const o of d.outcomes || []) {
        if (o.kind === "compact_update") ctx._apply(o.data);
        else if (o.kind === "error") toast(o.message, { type: "error" });
      }
    } catch (e) {
      toast(e.message || String(e), { type: "error" });
    }
  };
  const setField = (k, v) => { dirty[k] = v; scheduleFlush(); };

  const sections = el("div", { class: "ask-compact" });
  let primaryBtn, blockedNote;

  const rebuild = () => {
    const p = ctx.payload;
    sections.innerHTML = "";
    sections.append(sec("Email", secEmail()));
    sections.append(sec("What to do", secAction()));
    sections.append(sec("Reminders", secReminders()));
    sections.append(sec("Preview", secPreview()));
    const capNode = secCapChoice();
    if (capNode) sections.append(sec("Daily cap reached", capNode));
    sections.append(sec("Checks & safety", secChecks()));

    function secCapChoice() {
      const cc = p.cap_choice;
      if (!cc || !cc.needed) return null;
      const n = el("div", { class: "ask-cap-choice" });
      n.append(el("div", { class: "t-faint" },
        "Only " + cc.remaining + " of " + cc.count + " emails fit under today's cap. Pick a remainder option:"));
      const mk = (label, fields) => el("button", { class: "ask-opt",
        onclick: () => { for (const [k, v] of Object.entries(fields)) setField(k, v); } }, label);
      if (cc.options.indexOf("partial") !== -1) {
        n.append(mk("Send first " + cc.remaining + " now, queue the remaining " + (cc.count - cc.remaining),
                    { cap_ack: true }));
      }
      if (cc.options.indexOf("schedule") !== -1) {
        n.append(mk("Schedule across days", { action: "schedule" }));
      }
      return n;
    }

    function sec(title, node, open = true) {
      const d = el("details", { class: "ask-cc-sec" }, el("summary", {}, title));
      if (open) d.open = true;
      d.append(node);
      return d;
    }

    function secEmail() {
      const e = p.email || {};
      const n = el("div", { class: "ask-cc-grid" });
      n.append(el("div", { class: "ask-kv-row" }, el("span", { class: "ask-kv-k" }, "From"),
        el("span", { class: "ask-kv-v" }, e.account_email || "—")));
      n.append(el("div", { class: "ask-kv-row" }, el("span", { class: "ask-kv-k" }, "Audience"),
        el("span", { class: "ask-kv-v" }, (e.campaign || "One-off") + " · " + (e.audience || ""))));
      const cc = el("input", { class: "input", value: e.cc || "", "aria-label": "CC addresses",
        placeholder: "CC (up to 20, merge fields ok)" });
      cc.addEventListener("change", () => setField("cc", cc.value));
      n.append(el("div", { class: "ask-cc-field" }, el("span", { class: "ask-kv-k" }, "CC"), cc));
      const sig = el("input", { type: "checkbox", "aria-label": "Append signature" });
      sig.checked = !!e.signature_on;
      sig.addEventListener("change", () => setField("append_signature", sig.checked));
      n.append(el("label", { class: "ask-bulk-row" }, sig, el("span", {}, "Append signature")));
      if (e.attachments && e.attachments.length) {
        n.append(el("div", { class: "ask-kv-row" }, el("span", { class: "ask-kv-k" }, "Attachments"),
          el("span", { class: "ask-kv-v" }, e.attachments.join(", "))));
      }
      if (e.subject_preview) {
        n.append(el("div", { class: "ask-cc-subj" }, "Subject: " + e.subject_preview));
      }
      return n;
    }

    function secAction() {
      const n = el("div", {});
      const seg = el("div", { class: "ask-seg", role: "group", "aria-label": "What to do with these emails" });
      (p.action_options || []).forEach((a) => {
        const b = el("button", { class: "seg-item" + (p.action_selected === a ? " is-on" : ""),
          "aria-pressed": p.action_selected === a ? "true" : "false",
          onclick: () => setField("action", a) }, ACTION_LABELS[a] || a);
        seg.append(b);
      });
      n.append(seg, el("div", { class: "t-faint ask-cc-hint" }, "Nothing is pre-selected - pick one."));
      const needsAt = (p.action_selected === "schedule");
      const needsStagger = ["schedule", "send"].includes(p.action_selected);
      if (needsAt) {
        const dt = el("input", { class: "input", type: "datetime-local", "aria-label": "When to start" });
        const min = new Date(Date.now() + 5 * 60000);
        const max = new Date(Date.now() + 45 * 86400000);
        const pad = (x) => String(x).padStart(2, "0");
        const fmt = (d) => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
        dt.min = fmt(min); dt.max = fmt(max);
        if (p.at) dt.value = String(p.at).replace(" ", "T").slice(0, 16);
        dt.addEventListener("change", () => setField("at", dt.value ? dt.value.replace("T", " ") : ""));
        n.append(dt);
      }
      if (needsStagger) {
        const num = el("input", { class: "input", type: "number", min: 0, max: 600, step: 1,
          value: p.stagger_default !== undefined ? p.stagger_default : 0, "aria-label": "Stagger minutes" });
        num.addEventListener("change", () => setField("stagger", Number(num.value) || 0));
        n.append(el("label", { class: "ask-bulk-row" }, el("span", {}, "Stagger between emails (minutes): "), num,
          el("span", { class: "t-faint" }, " - so Gmail doesn't flag you")));
      }
      return n;
    }

    function secReminders() {
      const r = p.reminders || {};
      const n = el("div", {});
      const tg = el("input", { type: "checkbox", "aria-label": "Set up reminders" });
      tg.checked = !!r.on;
      const body = el("div", { style: r.on ? "" : "display:none" });
      const days = el("input", { class: "input", type: "number", min: 1, max: 90, value: r.days || 7, "aria-label": "Every how many days" });
      const rounds = el("input", { class: "input", type: "number", min: 1, max: 5, value: r.rounds || 2, "aria-label": "Maximum rounds" });
      const rsub = el("input", { class: "input", value: r.subject || "", "aria-label": "Reminder subject" });
      const rbody = el("textarea", { class: "textarea", rows: 3, "aria-label": "Reminder text" }, (r.body_html || "").replace(/<[^>]+>/g, " "));
      days.addEventListener("change", () => setField("rem_days", Number(days.value) || 7));
      rounds.addEventListener("change", () => setField("rem_rounds", Number(rounds.value) || 1));
      rsub.addEventListener("change", () => setField("rem_subject", rsub.value));
      rbody.addEventListener("change", () => setField("rem_body", rbody.value));
      tg.addEventListener("change", () => setField("reminders_set", tg.checked));
      body.append(
        el("label", { class: "ask-bulk-row" }, el("span", {}, "Every "), days, el("span", {}, " days, up to "), rounds, el("span", {}, " rounds")));
      body.append(el("div", { class: "ask-cc-field" }, rsub));
      body.append(rbody);
      n.append(el("label", { class: "ask-bulk-row" }, tg, el("span", {}, "Follow-up reminders to non-repliers")), body);
      return n;
    }

    function secPreview() {
      const bp = { preview: p.preview, cc: (p.email || {}).cc || "", signature_html: (p.email || {}).signature_html || "", from: (p.safety || {}).account };
      return recipientBrowser(ctx, bp);
    }

    function secChecks() {
      const n = el("div", {});
      n.append(presendNode(p.presend && p.presend.lines, p.presend && p.presend.blocked));
      n.append(safetyNode(p.safety));
      bulkControls(ctx, p, n);
      return n;
    }

    /* primary button with dynamic exact label */
    primaryBtn = el("button", { class: "btn btn-primary" + (String(p.risk) === "R4" && p.action_selected === "send" ? " btn-danger" : "") },
      compactPrimaryLabel(p, String(p.at || "").replace("T", " ")));
    const hint = el("div", { class: "t-faint ask-cc-hint" });
    const gate = async () => {
      if (!p.action_selected) { hint.textContent = "Choose what to do with these emails first."; hint.style.display = ""; return; }
      if (p.presend && p.presend.blocked) { hint.textContent = "A blocking check is failing - fix it first."; hint.style.display = ""; return; }
      const berr = ctx._bulk ? ctx._bulk() : null;
      if (berr) { hint.textContent = berr; hint.style.display = ""; return; }
      hint.style.display = "none";
      const resp = { action: "confirm" };
      if (ctx._ack) resp.ack = ctx._ack();
      respondCard(ctx, resp, primaryBtn.textContent);
    };
    primaryBtn.addEventListener("click", gate);
    sections.append(el("div", { class: "ask-compact-bar" },
      el("button", { class: "btn btn-ghost", onclick: () => cancelCardAction(ctx, "Cancelled - nothing was done") }, "Cancel"),
      primaryBtn, hint));
  };

  rebuild();
  ctx.body.append(sections);

  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); document.removeEventListener("keydown", onKey, true); cancelCardAction(ctx, "Cancelled - nothing was done"); } };
  setTimeout(() => document.addEventListener("keydown", onKey, true), 0);
}

/* ------------------------------------------------------------- registry + entry */

const CARD_RENDER = {
  options: renderOptionsCard,
  text_input: renderTextCard,
  number_input: renderNumberCard,
  datetime_input: renderDatetimeCard,
  secret_input: renderSecretCard,
  file_request: renderFileCard,
  table_edit: renderTableCard,
  confirm: (ctx) => renderConfirmCard(ctx, false),
  final_review: (ctx) => renderConfirmCard(ctx, true),
  compact_review: renderCompactCard,
  draft_editor: renderEditorCard,
  undo_countdown: renderUndoCard,
  handoff: renderHandoffCard,
  budget: renderBudgetCard,
};

/* wire the topbar button + deep link + command palette */
window.addEventListener("cm:ask-ai", (e) => openAskAI(e && e.detail && e.detail.text));
if (new URLSearchParams(location.search).get("ask") === "1") {
  window.addEventListener("load", () => openAskAI());
}
document.addEventListener("DOMContentLoaded", () => {
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      openAskAI();
    }
  });
});
