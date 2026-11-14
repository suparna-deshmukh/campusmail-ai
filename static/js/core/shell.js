/* App shell: theme, state polling (15 s, visibility-aware), mode card,
   system status popover, user menu, mobile drawer, command palette. */
import { api, el, icon, toast, modal, menu, closeAllMenus, badge, esc } from "./ui.js";
import { fmtInt, upcomingWhen, initials, avatarColor } from "./format.js";
import { onSeq, onKey, initKeys, shortcutList } from "./keys.js";

const state = { data: null, ai_on: false, me: null, safe_mode: true, offline: false };
window.cm = { state };

/* ---------------- Theme ---------------- */
function applyTheme(t) {
  const dark = t === "dark" || (t === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}
function themeNow() { return localStorage.getItem("cm_theme") || "auto"; }
document.documentElement.dataset.theme = themeNow() === "light" ? "light" : themeNow() === "dark" ? "dark" : (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
if (themeNow() === "auto") matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme("auto"));

/* ---------------- State polling ---------------- */
let _pollTimer = null;
let _backoff = 15000;

async function pollState() {
  try {
    const d = await api("/api/state");
    state.data = d;
    state.ai_on = d.settings.ai_enabled === "1" && d.ai_has_key === "1";
    state.me = d.me;
    state.safe_mode = d.settings.safe_mode === "1";
    state.offline = false;
    _backoff = 15000;
    renderShell(d);
  } catch (e) {
    state.offline = true;
    document.body.classList.add("offline");
    _backoff = Math.min(60000, _backoff * 2);
  }
  document.body.classList.remove("offline");
  _pollTimer = setTimeout(pollState, document.hidden ? 60000 : _backoff);
}

function renderShell(d) {
  // Replies sidebar badge
  const needs = (d.reply && (d.reply.needs_reply != null ? d.reply.needs_reply : d.reply.replied)) || 0;
  const b = document.getElementById("nav-replies-badge");
  if (b) { b.textContent = needs; b.classList.toggle("hot", needs > 0); b.style.display = needs ? "" : "none"; }
  // mode card
  renderModeCard();
  // system status
  renderSystemStatus(d);
  // Ask AI visibility
  const aiBtn = document.getElementById("topbar-ask-ai");
  if (aiBtn) aiBtn.style.display = state.ai_on ? "" : "none";
  document.dispatchEvent(new CustomEvent("cm:state", { detail: d }));
}

/* ---------------- Mode card (5.3) ---------------- */
function renderModeCard() {
  const card = document.getElementById("mode-card");
  if (!card) return;
  const live = !state.safe_mode;
  card.className = "mode-card " + (live ? "mode-live" : "mode-safe");
  card.innerHTML = "";
  card.append(
    icon(live ? "zap" : "shield-check", "i mode-ico"),
    el("div", { style: "min-width:0;flex:1" },
      el("div", { class: "mode-title" }, live ? "Live sending ON" : "Drafts only"),
      el("div", { class: "mode-sub" }, live
        ? "Scheduled and immediate sends will go out."
        : "Nothing is sent. Emails are saved in Gmail for you to review."),
    ),
  );
  document.body.classList.toggle("is-live", live);
}

window.addEventListener("cm:settings-changed", (e) => {
  const d = (e && e.detail) || {};
  if (d.key === "safe_mode") {
    state.safe_mode = d.value === "1";
    renderModeCard();
    document.dispatchEvent(new CustomEvent("cm:state"));
  }
});

export function setSafeMode(on) {
  return api("/api/settings", { body: { safe_mode: on ? "1" : "0" } }).then(() => {
    state.safe_mode = on;
    renderModeCard();
    document.dispatchEvent(new CustomEvent("cm:state"));
    toast(on ? "Drafts only. Nothing will be sent." : "Live sending is on. Scheduled emails will go out.",
      { type: on ? "success" : "info" });
  });
}

export function openModeCard() {
  const card = document.getElementById("mode-card");
  closeAllMenus();
  const m = el("div", { class: "menu", style: "min-width:250px" });
  const row = (live, title, sub, onClick) => {
    const wrap = el("button", { class: "menu-item", style: "height:auto;padding:9px 10px;align-items:flex-start;display:flex", onclick: () => { m.remove(); onClick(); } },
      icon(live ? "zap" : "shield-check", "i i-16"),
      el("div", { style: "flex:1" },
        el("div", { style: "font-weight:650" }, title),
        el("div", { style: "font-size:12px;color:var(--text-3);font-weight:500" }, sub)));
    m.append(wrap);
  };
  row(false, "Drafts only", "Nothing is sent; emails wait in Gmail", () => {
    if (!state.safe_mode) setSafeMode(true);
  });
  row(true, "Live sending", "Scheduled and immediate sends go out", () => {
    if (state.safe_mode) confirmLiveMode().then((on) => { if (on) setSafeMode(false); });
  });
  document.body.append(m);
  const r = card.getBoundingClientRect();
  m.style.top = (r.bottom + 8 + window.scrollY) + "px";
  m.style.left = (r.left + window.scrollX) + "px";
  const kill = (e) => { if (!m.contains(e.target) && e.target !== card) { m.remove(); document.removeEventListener("mousedown", kill, true); } };
  setTimeout(() => document.addEventListener("mousedown", kill, true), 0);
}

function confirmLiveMode() {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (v) => { if (!settled) { settled = true; resolve(v); } };
    const check = el("input", { type: "checkbox", style: "width:16px;height:16px;accent-color:var(--primary-500);cursor:pointer" });
    const btn = el("button", { class: "btn btn-primary", disabled: true }, "Turn on live sending");
    check.addEventListener("change", () => { btn.disabled = !check.checked; });
    const m = modal({
      title: "Turn on live sending?",
      body: el("div", {},
        el("ul", { style: "display:flex;flex-direction:column;gap:8px;font-size:13.5px;color:var(--text-2);padding-left:18px;margin-bottom:14px" },
          el("li", {}, "Scheduled emails will send on time."),
          el("li", {}, "Send now becomes available."),
          el("li", {}, "You can switch back to Drafts only any time.")),
        el("label", { class: "row", style: "cursor:pointer;font-size:13.5px;font-weight:550" },
          check, "I understand emails will reach real recruiters"),
      ),
      foot: [
        el("button", { class: "btn btn-ghost", onclick: () => { m.close(); settle(false); } }, "Cancel"),
        btn,
      ],
      onClose: () => settle(false),
    });
    btn.addEventListener("click", () => { m.close(); settle(true); });
  });
}

/* ---------------- System status popover (5.4) ---------------- */
function renderSystemStatus(d) {
  const btn = document.getElementById("topbar-status");
  if (!btn) return;
  const problems = [];
  if (!d.gmail.any_connected) problems.push("Gmail not connected");
  if (d.scheduler.due_now > 0 && !d.scheduler.running) problems.push("engine stopped, " + d.scheduler.due_now + " due");
  if (d.scheduler.last_error) problems.push("a problem with sending");
  btn.querySelector(".dot").style.display = problems.length ? "" : "none";
  btn._problems = problems;
  btn._data = d;
}

function openSystemStatus() {
  const btn = document.getElementById("topbar-status");
  const d = btn._data;
  if (!d) return;
  closeAllMenus();
  const m = el("div", { class: "menu", style: "min-width:280px" });
  const accs = d.gmail.accounts || [];
  if (accs.length === 0) {
    m.append(el("div", { class: "menu-item", style: "color:var(--danger)" }, icon("mail-x", "i i-16"), "No Gmail account connected — reconnect"));
  } else {
    for (const a of accs.slice(0, 3)) {
      m.append(el("div", { class: "menu-item" }, icon("check-circle-2", "i i-16"),
        el("span", {}, a.email || a.label), el("span", { style: "margin-left:auto;font-size:12px;color:var(--success);font-weight:600" }, "Connected")));
    }
  }
  m.append(el("div", { class: "menu-sep" }));
  m.append(el("div", { class: "menu-item" },
    icon(d.scheduler.running ? "zap" : "pause-circle", "i i-16"),
    el("span", {}, d.scheduler.running ? "Sending engine running" : "Sending engine stopped"),
    d.scheduler.due_now > 0 ? el("span", { style: "margin-left:auto;font-size:12px;font-weight:600;color:var(--warn)" }, d.scheduler.due_now + " due") : null));
  if (d.scheduler.next_fire) {
    m.append(el("div", { class: "menu-item" }, icon("clock", "i i-16"), "Next send " + upcomingWhen(d.scheduler.next_fire)));
  }
  if (d.scheduler.last_error) {
    m.append(el("div", { class: "menu-sep" }));
    m.append(el("div", { class: "menu-item danger" }, icon("alert-triangle", "i i-16"), "Last problem",
      el("a", { href: "/settings/advanced", style: "margin-left:auto;font-size:12px;font-weight:650" }, "Fix it")));
  }
  document.body.append(m);
  const r = btn.getBoundingClientRect();
  m.style.top = (r.bottom + 6 + window.scrollY) + "px";
  m.style.left = Math.max(8, r.right - m.offsetWidth + window.scrollX) + "px";
  const kill = (e) => { if (!m.contains(e.target) && e.target !== btn) { m.remove(); document.removeEventListener("mousedown", kill, true); } };
  setTimeout(() => document.addEventListener("mousedown", kill, true), 0);
}

/* ---------------- User menu ---------------- */
function openUserMenu(anchor) {
  const me = state.me || { email: "you" };
  closeAllMenus();
  const m = el("div", { class: "menu", style: "min-width:230px" });
  m.append(el("button", { class: "menu-item", onclick: () => { m.remove(); location.href = "/settings"; } },
    icon("settings", "i i-16"), "Settings"));
  m.append(el("div", { class: "menu-sep" }));
  m.append(el("div", { class: "menu-label" }, "Theme"));
  for (const t of ["light", "dark", "auto"]) {
    const item = el("button", { class: "menu-item", onclick: () => {
      localStorage.setItem("cm_theme", t);
      applyTheme(t);
      m.remove();
    } },
      icon(t === "light" ? "sun" : t === "dark" ? "moon" : "globe", "i i-16"),
      t[0].toUpperCase() + t.slice(1),
      themeNow() === t ? el("span", { style: "margin-left:auto;color:var(--primary-600)" }, icon("check", "i i-16")) : null);
    m.append(item);
  }
  m.append(el("div", { class: "menu-sep" }));
  m.append(el("div", { class: "menu-item", style: "cursor:default;color:var(--text-3)" }, "Signed in as " + me.email));
  m.append(el("div", { class: "menu-sep" }));
  m.append(el("button", { class: "menu-item danger", onclick: () => {
    m.remove();
    fetch("/logout", { method: "POST" }).then(() => { location.href = "/login"; });
  } }, icon("log-out", "i i-16"), "Sign out"));
  document.body.append(m);
  const r = anchor.getBoundingClientRect();
  m.style.top = (r.bottom + 6 + window.scrollY) + "px";
  m.style.left = Math.max(8, r.right - m.offsetWidth + window.scrollX) + "px";
  const kill = (e) => { if (!m.contains(e.target) && e.target !== anchor) { m.remove(); document.removeEventListener("mousedown", kill, true); } };
  setTimeout(() => document.addEventListener("mousedown", kill, true), 0);
}

/* ---------------- Command palette (quick actions now; API search in Phase 7) ---------------- */
export function openPalette(initial = "") {
  let scrim = document.getElementById("palette-scrim");
  if (scrim) { scrim.remove(); }
  scrim = el("div", { id: "palette-scrim", class: "palette-scrim" });
  const actions = [
    { icon: "send", label: "Send emails", go: () => location.href = "/send" },
    { icon: "plus", label: "New campaign", go: () => { location.href = "/campaigns?new=1"; } },
    { icon: "upload", label: "Import contact list", go: () => { location.href = "/library?tab=lists&import=1"; } },
    { icon: "file-pen", label: "New template", go: () => { location.href = "/library?tab=templates&new=1"; } },
    { icon: state.safe_mode ? "zap" : "shield-check", label: state.safe_mode ? "Turn live sending on" : "Turn live sending off", go: () => openModeCard() },
    { icon: "inbox", label: "Open Replies", go: () => location.href = "/replies" },
    { icon: "sparkles", label: "Ask AI", go: () => window.dispatchEvent(new CustomEvent("cm:ask-ai")) },
  ];
  const list = el("div", { class: "palette-list" });
  let items = [];
  let activeIdx = 0;
  const render = (q) => {
    list.innerHTML = "";
    items = [];
    const ql = q.trim().toLowerCase();
    const hits = ql ? actions.filter((a) => a.label.toLowerCase().includes(ql)) : actions;
    if (hits.length) {
      list.append(el("div", { class: "palette-group" }, "Quick actions"));
      for (const a of hits) {
        const it = el("button", { class: "palette-item", onclick: () => { close(); a.go(); } },
          icon(a.icon, "i i-16"), a.label);
        items.push(it);
        list.append(it);
      }
    } else {
      list.append(el("div", { class: "palette-empty" }, "No matches. Try “Send emails”."));
    }
    activeIdx = 0;
    highlight();
  };
  const highlight = () => {
    items.forEach((it, i) => it.classList.toggle("is-active", i === activeIdx));
    if (items[activeIdx]) items[activeIdx].scrollIntoView({ block: "nearest" });
  };
  const input = el("input", { placeholder: "Search or jump to...", value: initial, "aria-label": "Search" });
  input.addEventListener("input", () => render(input.value));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = Math.min(items.length - 1, activeIdx + 1); highlight(); }
    if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = Math.max(0, activeIdx - 1); highlight(); }
    if (e.key === "Enter" && items[activeIdx]) { e.preventDefault(); items[activeIdx].click(); }
  });
  const close = () => { scrim.classList.remove("open"); setTimeout(() => scrim.remove(), 200); document.removeEventListener("keydown", onEsc, true); };
  const onEsc = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  scrim.append(el("div", { class: "palette" },
    el("div", { class: "palette-input" }, icon("search", "i i-16"), input),
    list,
    el("div", { class: "palette-foot" },
      el("span", { class: "kbd-row" }, el("span", { class: "kbd" }, "↑↓"), "navigate"),
      el("span", { class: "kbd-row" }, el("span", { class: "kbd" }, "↵"), "open"),
      el("span", { class: "kbd-row" }, el("span", { class: "kbd" }, "esc"), "close"),
    ),
  ));
  document.body.append(scrim);
  requestAnimationFrame(() => { scrim.classList.add("open"); input.focus(); input.select(); });
  document.addEventListener("keydown", onEsc, true);
  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) close(); });
  render(initial);
}

/* ---------------- Init ---------------- */
export function shortcutsModal() {
  const rows = shortcutList([
    ["G then C", "Go to Campaigns"],
    ["G then R", "Go to Replies"],
    ["G then H", "Go to Home"],
    ["N", "New send"],
    ["/", "Search this page"],
  ]);
  return modal({
    title: "Keyboard shortcuts",
    sub: "A few keys that make this go faster.",
    body: el("div", { style: "display:flex;flex-direction:column;gap:10px" },
      ...rows.map(([keys, what]) => el("div", { style: "display:flex;align-items:center;gap:14px" },
        el("span", { class: "kbd" }, keys),
        el("span", { style: "font-size:13.5px" }, what)))),
  });
}

export function initShell() {
  // mode card
  const modeCard = document.getElementById("mode-card");
  if (modeCard) modeCard.addEventListener("click", openModeCard);
  // system status
  const stBtn = document.getElementById("topbar-status");
  if (stBtn) stBtn.addEventListener("click", openSystemStatus);
  // user menu
  const userRow = document.getElementById("side-user");
  if (userRow) userRow.addEventListener("click", () => openUserMenu(userRow));
  // topbar buttons
  const askAi = document.getElementById("topbar-ask-ai");
  if (askAi) askAi.addEventListener("click", () => window.dispatchEvent(new CustomEvent("cm:ask-ai")));
  document.addEventListener("cm:palette", (e) => {
    if (e.defaultPrevented) return;
    e.preventDefault();
    openPalette();
  });
  // mobile side drawer
  const burger = document.getElementById("topbar-burger");
  if (burger) burger.addEventListener("click", () => document.body.classList.toggle("side-open"));
  const scrim = document.getElementById("side-scrim");
  if (scrim) scrim.addEventListener("click", () => document.body.classList.remove("side-open"));
  document.querySelectorAll(".side a").forEach((a) => a.addEventListener("click", () => document.body.classList.remove("side-open")));

  // shortcuts
  initKeys();
  onKey("/", () => openPalette());
  onSeq("g c", () => location.href = "/campaigns");
  onSeq("g r", () => location.href = "/replies");
  onSeq("g h", () => location.href = "/");
  onKey("n", () => location.href = "/send");
  onKey("?", () => shortcutsModal());
  const helpBtn = document.getElementById("side-help");
  if (helpBtn) helpBtn.addEventListener("click", () => shortcutsModal());

  // visibility-aware polling
  document.addEventListener("visibilitychange", () => {
    clearTimeout(_pollTimer);
    pollState();
  });
  window.addEventListener("focus", () => pollState());
  pollState();
}
