/* Ask AI - assistant drawer (plan 5.10). Reads real data, can take small
   actions; every tool call is shown as a chip so nothing is hidden. */
import { api, el, icon, toast, drawer } from "./ui.js";

const STORE = "cm_askai_history";
const MAX_HISTORY = 30;
let live = null; // {close, body, input, send}

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
    "How many emails did I send this month?",
    "Who replied and hasn't been handled yet?",
    "Which campaigns have bounced emails?",
  ],
  home: [
    "Summarise what needs my attention",
    "How is my reply rate looking?",
    "Show my campaigns and their results",
  ],
  campaigns: [
    "Which campaign has the best reply rate?",
    "How many emails bounced in total?",
    "Help me plan a follow-up for unanswered emails",
  ],
  send: [
    "What should I say in a first outreach email?",
    "Suggest a subject line for a placement invite",
  ],
  replies: [
    "Draft a reply to a recruiter asking for details",
    "Summarise the open replies",
    "Which replies are waiting longest?",
  ],
  library: [
    "Suggest a template for a Diwali greeting",
    "What fields do my templates use?",
  ],
  team: [
    "Who has sent the most emails?",
  ],
  settings: [
    "How do I connect a second Gmail account?",
    "What does safe mode do?",
  ],
};

function ctxLabel() {
  return PAGE_LABEL[document.body.dataset.page || ""] || "This page";
}

function eventChip(ev) {
  const pre = el("pre", { class: "ask-ev-pre" }, safeJson(ev.result));
  return el("details", { class: "ask-ev" },
    el("summary", {}, icon("zap", "i ask-ev-ico"), el("span", {}, ev.tool || "tool")),
    el("div", { class: "ask-ev-body" }, pre));
}

function safeJson(v) {
  try {
    const s = typeof v === "string" ? v : JSON.stringify(v, null, 1);
    return (s || "").slice(0, 600) + ((s || "").length > 600 ? " …" : "");
  } catch { return String(v); }
}

function msgNode(m) {
  if (m.role === "user") {
    return el("div", { class: "ask-msg ask-user" }, m.content);
  }
  const node = el("div", { class: "ask-msg ask-ai" + (m.error ? " ask-err" : "") },
    m.events && m.events.length
      ? el("div", { class: "ask-evs" }, ...m.events.map(eventChip))
      : null,
    el("div", { class: "ask-text" }, m.error ? "⚠ " : "", m.content || "…"));
  return node;
}

function thinkingNode() {
  return el("div", { class: "ask-msg ask-ai" },
    el("div", { class: "ask-text ask-thinking" }, icon("sparkles", "i i-14"), " Thinking…"));
}

export function openAskAI(initial = "") {
  if (live) {
    if (initial) { live.input.value = initial; live.send(); }
    return;
  }
  let hist = loadHist();
  const body = el("div", { class: "ask-body" });
  const render = (scroll = true) => {
    body.innerHTML = "";
    if (!hist.length) {
      body.append(el("div", { class: "ask-empty" },
        el("div", { class: "ask-empty-ico" }, icon("sparkles", "i ask-empty-i")),
        el("div", { class: "ask-empty-title" }, "Ask anything about your campaigns"),
        el("p", { class: "ask-empty-sub" }, "It reads your real data and can help you send. Every step it takes is shown here.")));
    }
    hist.forEach((m) => body.append(msgNode(m)));
    if (scroll) body.scrollTop = body.scrollHeight;
  };

  const input = el("input", { class: "input ask-input", placeholder: "Ask about your campaigns, contacts, replies…", "aria-label": "Message AI" });
  const sendBtn = el("button", { class: "btn btn-primary ask-send", "aria-label": "Send" }, icon("send", "i i-16"));
  let busy = false;
  const doSend = async () => {
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    hist.push({ role: "user", content: text });
    saveHist(hist);
    input.value = "";
    const th = thinkingNode();
    body.append(th);
    body.scrollTop = body.scrollHeight;
    try {
      const d = await api("/api/ai/chat", { body: { messages: hist.map((m) => ({ role: m.role, content: m.content })) } });
      hist.push({ role: "assistant", content: d.reply || "", events: d.events || [] });
    } catch (e) {
      hist.push({ role: "assistant", content: e.message, error: true });
    }
    saveHist(hist);
    th.remove();
    busy = false;
    render();
    sendBtn.disabled = false;
  };
  sendBtn.addEventListener("click", doSend);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doSend(); } });
  live = { close: null, body, input, send: doSend, sendBtn };

  const clearBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => {
    hist = [];
    saveHist(hist);
    render();
  } }, "Clear");

  const d = drawer({
    title: "Ask AI",
    sub: "On: " + ctxLabel(),
    body: el("div", { class: "ask-wrap" },
      el("div", { class: "ask-chips" },
        ...(HINTS[document.body.dataset.page || ""] || HINTS[""]).slice(0, 4).map((h) =>
          el("button", { class: "chip-btn", onclick: () => { input.value = h; doSend(); } }, h)),
        el("span", { class: "push" }), clearBtn),
      body),
    foot: el("div", { class: "ask-foot" }, input, sendBtn),
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
