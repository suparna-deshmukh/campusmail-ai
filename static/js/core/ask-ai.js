/* Ask AI - agentic assistant drawer. Reads real data, can create/edit
   campaigns, templates and contact lists and prepare sends. Every mutating
   step is shown as a confirmation card (Yes / No); questions arrive as a
   card with option buttons and/or a text box. Files can be attached. */
import { api, el, icon, toast, drawer } from "./ui.js";

const STORE = "cm_askai_history";
const MAX_HISTORY = 60;
let live = null; // {close, body, input, send, attach, fileChip, convId}

function loadHist() {
  try { return JSON.parse(localStorage.getItem(STORE) || "[]"); } catch { return []; }
}
function saveHist(h) {
  try { localStorage.setItem(STORE, JSON.stringify(h.slice(-MAX_HISTORY))); } catch { /* private */ }
}

const PAGE_LABEL = {
  "": "Home", home: "Home", campaigns: "Campaigns", send: "Sending emails",
  replies: "Replies", library: "Library", team: "Team", settings: "Settings",
};
const HINTS = {
  "": [
    "How are my campaigns doing?",
    "Create a campaign called Winter 2027",
    "Import my attached contact list",
  ],
  home: [
    "Summarise what needs my attention",
    "How is my reply rate looking?",
    "Show my campaigns and their results",
  ],
  campaigns: [
    "Which campaign has the best reply rate?",
    "Create a new campaign",
    "Prepare a send for my Diwali campaign",
  ],
  send: [
    "What should I say in a first outreach email?",
    "Suggest a subject line for a placement invite",
  ],
  replies: [
    "Draft a reply to a recruiter asking for details",
    "Summarise the open replies",
  ],
  library: [
    "Save my attached document as a template",
    "What fields do my templates use?",
  ],
  team: ["Who has sent the most emails?"],
  settings: ["How do I connect a second Gmail account?"],
};

function ctxLabel() {
  return PAGE_LABEL[document.body.dataset.page || ""] || "This page";
}

function thinkingNode() {
  return el("div", { class: "ask-ai ask-thinking" },
    icon("sparkles", "i"), el("span", {}, "Thinking…"));
}

function eventsNode(events) {
  if (!events || !events.length) return null;
  return el("div", { class: "ask-evs" }, events.map((ev) =>
    el("details", { class: "ask-ev" },
      el("summary", {}, icon("zap", "i ask-ev-ico"), el("span", {}, ev.summary || ev.tool || "step")),
      ev.result ? el("pre", { class: "ask-ev-pre" }, JSON.stringify(ev.result, null, 2)) : null)));
}

function confirmCard(m) {
  const card = el("div", { class: "ask-ai ask-confirm" },
    el("div", { class: "ask-confirm-head" }, icon("alert-circle", "i"), "Confirm action"));
  card.append(el("div", { class: "ask-confirm-desc" }, m.description || m.content || ""));
  if (m.done) {
    card.append(el("div", { class: "ask-confirm-done" + (m.done === "yes" ? " ok" : "") },
      m.done === "yes" ? "You confirmed - done" : "You cancelled"));
  } else {
    const row = el("div", { class: "ask-confirm-actions" });
    const yes = el("button", { class: "btn btn-primary btn-sm" }, "Yes, do it");
    const no = el("button", { class: "btn btn-secondary btn-sm" }, "No, cancel");
    yes.addEventListener("click", () => live && live.act(m, "confirm", true));
    no.addEventListener("click", () => live && live.act(m, "confirm", false));
    row.append(yes, no);
    card.append(row);
  }
  return card;
}

function promptCard(m) {
  const card = el("div", { class: "ask-ai ask-prompt" },
    el("div", { class: "ask-prompt-head" }, icon("message-square", "i"), "A quick question"));
  card.append(el("div", { class: "ask-prompt-q" }, m.question || m.content || ""));
  if (!m.done) {
    if (m.options && m.options.length) {
      card.append(el("div", { class: "ask-prompt-opts" }, m.options.map((o) =>
        el("button", { class: "chip-btn", onclick: () => live && live.act(m, "answer", o) }, o))));
    }
    const inp = el("input", { class: "input", placeholder: "Type your answer…", "aria-label": "Answer" });
    const go = el("button", { class: "btn btn-primary btn-sm", "aria-label": "Send answer" }, icon("send", "i i-16"));
    const submit = () => { const v = inp.value.trim(); if (v && live) live.act(m, "answer", v); };
    go.addEventListener("click", submit);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    card.append(el("div", { class: "ask-prompt-in" }, inp, go));
  } else {
    card.append(el("div", { class: "ask-confirm-done" }, "You answered: " + m.done));
  }
  return card;
}

function msgNode(m) {
  if (m.role === "user") {
    return el("div", { class: "ask-msg ask-user" },
      m.file ? el("div", { class: "ask-user-file" }, icon("paperclip", "i i-16"), m.file) : null,
      m.content ? el("div", {}, m.content) : null);
  }
  if (m.role === "confirm") return confirmCard(m);
  if (m.role === "prompt") return promptCard(m);
  // assistant
  const card = el("div", { class: "ask-ai" + (m.error ? " ask-err" : "") },
    eventsNode(m.events),
    el("div", { class: "ask-text" }, m.content || ""));
  return card;
}

function base64Of(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1] || "");
    r.onerror = rej;
    r.readAsDataURL(file);
  });
}

export function openAskAI(initial, ctx) {
  if (live) {
    if (initial) { live.input.value = initial; live.send(); }
    return;
  }
  let hist = loadHist();
  let convId = null; // server conversation id (fresh on each open)
  let attachment = null; // {name, data(base64), size}
  let busy = false;

  const body = el("div", { class: "ask-body" });
  const render = (scroll = true) => {
    body.innerHTML = "";
    if (!hist.length) {
      body.append(el("div", { class: "ask-empty" },
        el("div", { class: "ask-empty-ico" }, icon("sparkles", "i")),
        el("div", { class: "ask-empty-title" }, "Ask anything about your campaigns"),
        el("p", { class: "ask-empty-sub" },
          "It reads your real data and can create campaigns, import contact lists, edit templates and prepare sends. It always asks before doing anything - and you can attach files.")));
    }
    hist.forEach((m) => body.append(msgNode(m)));
    if (scroll) body.scrollTop = body.scrollHeight;
  };

  const input = el("input", { class: "input ask-input", placeholder: "Ask, or attach a contact list / template file…", "aria-label": "Message AI" });
  const sendBtn = el("button", { class: "btn btn-primary ask-send", "aria-label": "Send" }, icon("send", "i i-16"));
  const attachBtn = el("button", { class: "btn btn-secondary ask-send", "aria-label": "Attach a file" }, icon("paperclip", "i i-16"));
  const fileInp = el("input", { type: "file", accept: ".csv,.xlsx,.tsv,.docx,.html,.htm,.txt", style: "display:none" });
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

  const setBusy = (b) => {
    busy = b;
    input.disabled = b;
    sendBtn.disabled = b;
    attachBtn.disabled = b;
  };

  const pushResponse = (d) => {
    if (!d || !d.conv_id) return false;
    convId = d.conv_id;
    if (d.error) { hist.push({ role: "assistant", content: d.error, error: true }); return true; }
    let changed = false;
    if (d.pending) { hist.push({ role: "confirm", description: d.pending.description, tool: d.pending.tool, events: d.events }); changed = true; }
    if (d.prompt) { hist.push({ role: "prompt", question: d.prompt.question, options: d.prompt.options, events: d.pending ? null : d.events }); changed = true; }
    if (d.reply) { hist.push({ role: "assistant", content: d.reply, events: d.pending || d.prompt ? null : d.events }); changed = true; }
    if (!changed && d.events && d.events.length) {
      hist.push({ role: "assistant", content: "Done - the steps I took are shown above.", events: d.events });
    }
    return changed;
  };

  const doSend = async () => {
    const text = input.value.trim();
    if ((!text && !attachment) || busy) return;
    setBusy(true);
    hist.push({ role: "user", content: text || "", file: attachment ? attachment.name : null });
    const att = attachment;
    input.value = "";
    attachment = null;
    renderChip();
    const th = thinkingNode();
    body.append(th);
    body.scrollTop = body.scrollHeight;
    try {
      const payload = { text: text, conv_id: convId };
      if (att) payload.file = { name: att.name, data: att.data };
      const d = await api("/api/ai/agent", { body: payload });
      pushResponse(d);
    } catch (e) {
      hist.push({ role: "assistant", content: e.message, error: true });
    }
    saveHist(hist);
    th.remove();
    setBusy(false);
    render();
  };

  const act = async (m, kind, value) => {
    if (busy) return;
    setBusy(true);
    const th = thinkingNode();
    body.append(th);
    body.scrollTop = body.scrollHeight;
    try {
      const payload = { conv_id: convId };
      if (kind === "confirm") { payload.confirm = !!value; m.done = value ? "yes" : "no"; }
      else { payload.answer = String(value); m.done = String(value); }
      const d = await api("/api/ai/agent", { body: payload });
      pushResponse(d);
    } catch (e) {
      m.done = m.done || "error";
      hist.push({ role: "assistant", content: e.message, error: true });
    }
    saveHist(hist);
    th.remove();
    setBusy(false);
    render();
  };

  attachBtn.addEventListener("click", () => fileInp.click());
  fileInp.addEventListener("change", async () => {
    const f = fileInp.files && fileInp.files[0];
    fileInp.value = "";
    if (!f) return;
    if (f.size > 5 * 1024 * 1024) { toast("File too large (max 5 MB).", { type: "error" }); return; }
    try {
      attachment = { name: f.name, size: f.size, data: await base64Of(f) };
      renderChip();
    } catch (e) {
      toast("Could not read that file.", { type: "error" });
    }
  });
  sendBtn.addEventListener("click", doSend);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doSend(); } });
  live = { close: null, body, input, send: doSend, act, convId: () => convId };

  const clearBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => {
    hist = [];
    convId = null;
    attachment = null;
    renderChip();
    saveHist(hist);
    render();
  } }, "Start new");

  const d = drawer({
    title: "Ask AI",
    sub: "On: " + ctxLabel() + " · asks before it acts",
    body: el("div", { class: "ask-wrap" },
      el("div", { class: "ask-chips" },
        ...(HINTS[document.body.dataset.page || ""] || HINTS[""]).slice(0, 4).map((h) =>
          el("button", { class: "chip-btn", onclick: () => { input.value = h; doSend(); } }, h)),
        el("span", { class: "push" }), clearBtn),
      body,
      fileChip,
    ),
    foot: el("div", { class: "ask-foot" }, attachBtn, input, sendBtn),
    onClose: () => { live = null; },
  });
  live.close = d.close;
  if (initial) { input.value = initial; doSend(); }
  else render();
}

/* wire the topbar button + deep link (shell.js dispatches cm:ask-ai) */
window.addEventListener("cm:ask-ai", () => openAskAI());
if (new URLSearchParams(location.search).get("ask") === "1") {
  window.addEventListener("load", () => openAskAI());
}
