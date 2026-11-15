/* Send flow - 4 steps, one decision per screen (D-28):
   1 Audience  Who are you emailing?
   2 Message   What will they read?
   3 Delivery  How should it go out?
   4 Review    Check before it goes.
   Glossary: send/list/spacing/follow-up. Sentence case, no exclamation marks. */
import {
  api, el, icon, toast, modal, holdToConfirm, confirmSimple, confirmTyped,
  emptyState, errorCard, withLoading, esc, appendSafe,
} from "/static/js/core/ui.js";
import { fmtInt, fmtLong, fmtDayTime, relTime, toBackend, parseWhen, localToUrl } from "/static/js/core/format.js";
import { openTimePicker } from "/static/js/components/timepicker.js";
import { planDelivery, sendCapSentence, scheduleCapSentence } from "/static/js/core/send-plan.js";
import { dedupeRows, keepFirstIds } from "/static/js/core/dedupe.js";
import { startSendCountdown } from "/static/js/components/send-countdown.js";

const START_CID = window.send_campaign_id || 0;
const root = document.getElementById("send-root");

/* ---------------- steps ---------------- */
const STEPS = ["audience", "message", "delivery", "review"];
const SEC = {
  audience: ["Who are you emailing?", "Pick the list, or choose specific people."],
  message: ["What will they read?", "The message, and where each fill-in comes from."],
  delivery: ["How should it go out?", "The sending account and what happens next."],
  review: ["Check before it goes", "This is exactly what each person will get."],
};
const OUTCOME_IDS = ["draft", "schedule", "send", "queue"];

/* ---------------- state ---------------- */
const S = {
  schemaVersion: 7,
  open: [],              // expanded section keys (D-39: single page, one decision per section)
  campaignId: START_CID,
  listId: 0,             // 0 = All contacts
  contactIds: [],        // non-empty = only these contacts from the chosen list
  templateId: 0,
  mapping: {},
  mappedFor: "",
  matchOpen: false,
  account: 0,
  append_signature: true,
  cc: "",
  reminder_days: 0,
  reminder_max: 1,
  reminder_template_id: 0,
  when: "",
  stagger: 0,
  outcome: "draft",      // draft | schedule | send | queue
  skipDupes: true,       // 8.2: skip duplicate emails, keep first
  fillHi: true,          // D-20: highlight filled values in the preview
  att_meta: [],          // 8.8: {name,size} of attachments (File objects die on refresh)
  touched: false,        // set by real user actions; plain visits save nothing
};
const K = () => "cm_send_" + (S.campaignId || "global");
function save() {
  if (!S.touched) return;  // merely visiting must not create an "unfinished send"
  try {
    localStorage.setItem(K(), JSON.stringify({ ...S, saved_at: Date.now() }));
  } catch { /* private mode */ }
}
function touch() { S.touched = true; save(); }
function clearDraftKey() { try { localStorage.removeItem(K()); } catch { /* private mode */ } }

/* Migrate any saved draft (schema 1/2/3 or corrupt) onto the 4-step shape.
   Old step numbers: 1 -> audience, 2 -> review. */
function migrateSaved(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const out = { ...raw };
  // D-39: the 4-step "step" value becomes the one expanded section.
  // Old numeric steps (2-step flow): 1 -> audience, 2 -> review.
  const stepMap = { 1: "audience", 2: "review", "1": "audience", "2": "review",
    audience: "audience", message: "message", delivery: "delivery", review: "review" };
  if (Array.isArray(out.open)) {
    out.open = out.open.filter((k) => STEPS.includes(k));
  } else {
    const step = stepMap[out.step];
    out.open = step ? [step] : STEPS.slice();
  }
  delete out.step; delete out.visited; delete out.after;
  out.schemaVersion = 7;
  if (!Array.isArray(out.contactIds)) out.contactIds = [];
  if (typeof out.mapping !== "object" || out.mapping === null || Array.isArray(out.mapping)) out.mapping = {};
  if (typeof out.outcome !== "string" || !OUTCOME_IDS.includes(out.outcome)) out.outcome = "draft";
  for (const k of ["listId", "templateId", "account", "reminder_days", "reminder_max", "reminder_template_id", "stagger"])
    if (typeof out[k] !== "number" || !isFinite(out[k])) out[k] = 0;
  for (const k of ["when", "cc"]) if (typeof out[k] !== "string") out[k] = "";
  if (typeof out.append_signature !== "boolean") out.append_signature = true;
  if (typeof out.skipDupes !== "boolean") out.skipDupes = true;
  if (typeof out.fillHi !== "boolean") out.fillHi = true;
  if (!Array.isArray(out.att_meta) || !out.att_meta.every((x) => x && typeof x.name === "string")) out.att_meta = [];
  return out;
}
function restoreRaw() {
  if (!START_CID) return null;
  try { return JSON.parse(localStorage.getItem(K()) || "null"); } catch { return null; }
}

const data = {
  campaigns: [], sheets: [], templates: [], accounts: [],
  safe: true, ai_on: false, n_contacts: 0, last_account_id: 0,
  previews: [], preview_total: 0, preview_idx: 0,
  fields: [], columns: [], listName: "", listCount: 0,
  templateName: "", campaignName: "",
  sheetRows: null,       // rows of the selected list (for checks)
  allRows: null,         // all contacts (id/serial/company/hr_name/hr_email)
  quality: null,         // {n, missing, dupes} for the current audience
  qualityFor: "",     // audience key the quality result belongs to
  dedupe: null,        // {dupes, crossCompany, groups, keepIds}
  reviewFocus: null,     // D-19: {kind:'search'|'issues', list, idx, previews, loaded}
  reviewIssues: [],     // D-19: rows with issues (blank fill / no email / skipped dupe)
  last_error: "",
  meEmail: "", defaultStagger: 0, maxCap: 0, sentToday: 0,
  restoreNotes: [],      // {step, text} one-line notices for stale saved ids
};
let listEditing = false; // transient: show the radio stack for list picking

const $q = (sel, r = root) => r.querySelector(sel);
const nPeople = () => (S.contactIds.length || data.listCount || 0);
/* Audience count after duplicate skipping (8.2): what will actually go out.
   Base is nPeople() (unfiltered); preview_total already reflects the
   filtered audience once previews have loaded, so never subtract twice. */
const effN = () => {
  const n = nPeople();
  return (S.skipDupes && data.dedupe && data.dedupe.dupes) ? Math.max(0, n - data.dedupe.dupes) : n;
};
/* Contact ids that will actually be sent (skip duplicates -> keep first). */
function effContactIds() {
  if (S.skipDupes && data.dedupe && data.dedupe.dupes) return [...data.dedupe.keepIds];
  return S.contactIds.length ? [...S.contactIds] : undefined;
}
const planNow = () => planDelivery({ outcome: S.outcome, n: effN(), cap: data.maxCap, sentToday: data.sentToday });
const unmatchedFields = () => data.fields.filter((f) => !S.mapping[f]);

/* ---------------- load ---------------- */
function readState(st) {
  if (!st) return;
  data.safe = st.settings.safe_mode === "1";
  data.ai_on = st.settings.ai_enabled === "1" && st.ai_has_key === "1";
  data.n_contacts = (st.onboard && st.onboard.contacts) || 0;
  data.meEmail = (st.me && st.me.email) || "";
  data.defaultStagger = Number((st.settings && st.settings.default_stagger_min) || 0);
  data.maxCap = Number((st.settings && st.settings.max_sends_per_day) || 0);
  data.sentToday = st.sent_today || 0;
}
async function load() {
  try {
    const [camps, sheets, tpls, accs, st] = await Promise.all([
      api("/api/campaigns"),
      api("/api/sheets"),
      api("/api/templates"),
      api("/api/accounts").catch(() => ({ accounts: [], last_account_id: 0 })),
      api("/api/state").catch(() => null),
    ]);
    data.campaigns = camps.campaigns || [];
    data.sheets = sheets.sheets || [];
    data.templates = tpls.templates || [];
    data.accounts = (accs.accounts || []).filter((a) => a.connected);
    data.last_account_id = accs.last_account_id || 0;
    if (st) {
      data.safe = st.settings.safe_mode === "1";
      data.ai_on = st.settings.ai_enabled === "1" && st.ai_has_key === "1";
      data.n_contacts = (st.onboard && st.onboard.contacts) || 0;
      data.meEmail = (st.me && st.me.email) || "";
      data.defaultStagger = Number((st.settings && st.settings.default_stagger_min) || 0);
      data.maxCap = Number((st.settings && st.settings.max_sends_per_day) || 0);
      data.sentToday = st.sent_today || 0;
    }
    data.last_error = "";
  } catch (e) {
    data.last_error = e.message;
    root.innerHTML = "";
    root.append(errorCard("Could not load the send flow. Check your connection and try again.", { onRetry: load, detail: e.message }));
    return;
  }
  const saved = migrateSaved(restoreRaw());
  if (saved) { Object.assign(S, saved); S.touched = true; }
  if (!saved) {
    const u = (new URLSearchParams(location.search).get("step") || "").toLowerCase();
    S.open = STEPS.includes(u) ? [u] : STEPS.slice();
  }
  if (S.campaignId) {
    const c = data.campaigns.find((x) => x.id === S.campaignId);
    if (!c) S.campaignId = START_CID ? 0 : S.campaignId;
  }
  if (!S.campaignId && data.campaigns.length === 1) S.campaignId = data.campaigns[0].id;
  // Validate restored ids against loaded data; fall back with a one-line notice.
  if (S.templateId && !data.templates.some((t) => t.id === S.templateId)) {
    const def = data.templates.find((t) => t.is_active) || data.templates[0] || null;
    data.restoreNotes.push({ step: "message", text: "Your earlier template was deleted, so we picked " + (def ? "'" + def.name + "'." : "nothing.") });
    S.templateId = def ? def.id : 0;
    S.mapping = {};
  }
  if (S.listId && !data.sheets.some((s) => s.id === S.listId)) {
    data.restoreNotes.push({ step: "audience", text: "Your earlier list was deleted, so we picked " + (data.n_contacts ? "All contacts." : "no list.") });
    S.listId = 0;
    S.contactIds = [];
  }
  if (S.account && !data.accounts.some((a) => a.id === S.account)) {
    data.restoreNotes.push({ step: "delivery", text: "Your earlier send account is no longer connected, so we picked " + ((data.accounts[0] && data.accounts[0].email) || "no account.") });
    S.account = 0;
  }
  if (!S.templateId && data.templates.length) {
    const def = data.templates.find((t) => t.is_active) || data.templates[0];
    S.templateId = def.id;
  }
  if (!S.account && data.accounts.length) S.account = data.last_account_id || data.accounts[0].id;
  await selectList(S.listId, { keep: true });
  // Always resolve field mapping on load (the "no fields mapped -> 2 mapped"
  // mismatch came from skipping this on first visit).
  await autoMatch();
  await refreshQuality(); // footer counts and section statuses need it up front
  if (!saved) S.open = allValid() ? [] : [firstBlocker().sec];
  const cu = new URL(location.href); cu.searchParams.delete("step");
  history.replaceState(null, "", cu.toString());
  render();
}

async function selectList(id, { keep = false } = {}) {
  const sheet = data.sheets.find((s) => s.id === id);
  if (id && !sheet) return;
  S.listId = id;
  S.contactIds = [];
  data.listName = id ? sheet.name : "All contacts";
  data.listCount = id ? sheet.n_rows : data.n_contacts;
  data.columns = id ? (sheet.headers || []) : ["company", "hr_name", "hr_email"];
  data.sheetRows = null;
  if (id) {
    try {
      const d = await api("/api/sheets/" + id);
      data.sheetRows = (d.sheet && d.sheet.rows) || [];
    } catch { data.sheetRows = []; }
  }
}

/* Rows of the current audience (dicts), for checks + picker + quality. */
async function audienceRows() {
  if (S.listId) {
    try {
      const d = await api("/api/contacts?sheet_id=" + S.listId);
      return d.rows || [];
    } catch { return data.sheetRows || []; }
  }
  if (!data.allRows) {
    try { const d = await api("/api/contacts"); data.allRows = d.rows || []; }
    catch { data.allRows = []; }
  }
  return data.allRows;
}
async function checkRows() {
  let rows = await audienceRows();
  if (S.contactIds.length) {
    const set = new Set(S.contactIds);
    rows = rows.filter((r) => set.has(r.id));
  }
  return rows;
}

/* Data-quality summary for the Audience step: no-email rows + duplicates. */
async function refreshQuality() {
  const key = S.listId + "|" + (S.contactIds.length ? S.contactIds.join(",") : "");
  if (data.qualityFor === key && data.quality) { paintQuality(); return; }
  const rows = await checkRows();
  const missing = rows.filter((r) => !(r.hr_email || "").trim()).length;
  const dd = dedupeRows(rows);
  data.quality = { n: rows.length, missing, dupes: dd.dupes };
  data.dedupe = { dupes: dd.dupes, crossCompany: dd.crossCompany, groups: dd.groups, keepIds: keepFirstIds(rows) };
  data.qualityFor = key;
  paintQuality();
}
function paintQuality() {
  const q = $q("#sec-aud-quality");
  if (q) { q.innerHTML = ""; q.append(qualityLine()); }
  const l = $q("#sec-aud-list");
  if (l && isOpen("audience")) { l.innerHTML = ""; l.append(S.campaignId ? listSection() : el("div")); }
}

/* Suggest fill-in mapping. Non-destructive: keeps choices the user made. */
async function autoMatch(force = false) {
  const tpl = data.templates.find((t) => t.id === S.templateId);
  if (!tpl) return;
  data.fields = Array.isArray(tpl.fields) ? tpl.fields : [];
  const key = S.templateId + ":" + S.listId;
  if (!data.fields.length) { S.mapping = {}; S.matchOpen = false; S.mappedFor = key; return; }
  if (!force && S.mappedFor === key && Object.keys(S.mapping).length) return;
  const prev = (S.mappedFor === key && !force) ? S.mapping : {};
  try {
    const d = await api("/api/merge/suggest?template_id=" + S.templateId + "&columns=" + encodeURIComponent(data.columns.join(",")));
    S.mapping = {};
    for (const f of data.fields) {
      if (prev[f]) { S.mapping[f] = prev[f]; continue; }
      const col = (d.suggestion || {})[f];
      if (col) S.mapping[f] = "column:" + col;
    }
  } catch {
    S.mapping = {};
  }
  S.matchOpen = data.fields.some((f) => !S.mapping[f]);
  S.mappedFor = key;
}

/* ---------------- shell ---------------- */
function render() {
  root.innerHTML = "";
  root.append(el("div", { class: "send-layout" },
    el("div", { class: "send-col" },
      pageHead(),
      el("div", { class: "send-wrap" },
        section("audience"), section("message"), section("delivery"), section("review")),
      el("div", { class: "send-sticky-zone" }, footerBar()))));
  for (const k of STEPS) paintSection(k);
  syncWrapWide();
  save();
}
function pageHead() {
  return el("div", { class: "page-head send-head" },
    el("div", { class: "ph-main" },
      el("div", { class: "send-step-title" }, "Send"),
      el("p", { class: "send-step-sub" }, "Set up your send, then check it before it goes.")),
    el("div", { class: "push", style: "display:flex;gap:12px;align-items:center;flex:none" },
      el("button", { class: "btn btn-icon", "aria-label": "Close", onclick: closeFlow }, icon("x", "i i-16"))));
}
function closeFlow() {
  history.length > 2 ? history.back() : (location.href = S.campaignId ? "/campaigns/" + S.campaignId : "/campaigns");
}

/* One collapsible section per decision (D-39). */
function isOpen(key) { return S.open.includes(key); }
function section(key) {
  const [title] = SEC[key];
  const head = el("button", {
    class: "acc-head", type: "button", "aria-expanded": String(isOpen(key)),
    "aria-controls": "acc-body-" + key,
    onclick: () => toggleSec(key),
  },
    el("span", { class: "acc-num" }, String(STEPS.indexOf(key) + 1)),
    el("span", { class: "acc-head-txt" },
      el("span", { class: "acc-title" }, title),
      el("span", { class: "acc-status", id: "acc-status-" + key })),
    el("span", { class: "acc-chev" }, icon("chevron-down", "i i-16")));
  const body = el("div", { class: "acc-body", id: "acc-body-" + key, hidden: !isOpen(key) });
  return el("section", { class: "send-sec", id: "sec-wrap-" + key, "aria-label": title }, head, body);
}
function toggleSec(key) {
  const i = S.open.indexOf(key);
  if (i >= 0) S.open.splice(i, 1); else S.open.push(key);
  touch();
  paintSection(key);
  syncWrapWide();
  const head = $q("#sec-wrap-" + key + " .acc-head");
  if (head) head.scrollIntoView({ block: "nearest" });
}
function syncWrapWide() {
  const w = $q(".send-wrap");
  if (w) w.classList.toggle("review-open", isOpen("review"));
}
/* (Re)build an open section's body, refresh its status line and the footer. */
function paintSection(key) {
  const wrap = $q("#sec-wrap-" + key);
  if (!wrap) return;
  const head = wrap.querySelector(".acc-head");
  const body = $q("#acc-body-" + key);
  head.setAttribute("aria-expanded", String(isOpen(key)));
  body.hidden = !isOpen(key);
  if (isOpen(key)) {
    body.innerHTML = "";
    const note = restoreNote(key);
    if (note) body.append(note);
    if (key === "audience") renderStepAudience(body);
    else if (key === "message") renderStepMessage(body);
    else if (key === "delivery") renderStepDelivery(body);
    else renderStepReview(body);
  }
  const st = statusFor(key);
  const s = $q("#acc-status-" + key);
  if (s) { s.textContent = st.text; s.className = "acc-status " + st.cls; }
  updateFooterChrome();
}
function updateStatuses() {
  for (const k of STEPS) {
    const st = statusFor(k);
    const s = $q("#acc-status-" + k);
    if (s) { s.textContent = st.text; s.className = "acc-status " + st.cls; }
  }
  updateFooterChrome();
}
/* Targeted repaint: only the named sections (if open), statuses and footer. */
function repaint(...keys) {
  for (const k of new Set(keys)) if (STEPS.includes(k) && isOpen(k)) paintSection(k);
  updateStatuses();
}
/* One-line status per section; block = the primary action cannot fire yet. */
function statusFor(key) {
  if (key === "audience") {
    const n = nPeople();
    if (!S.campaignId) return { text: "Pick a campaign to continue", cls: "block" };
    if (!n) return { text: "Pick a list to continue", cls: "block" };
    const q = data.quality;
    let extra = "";
    if (q) {
      const bits = [];
      if (q.missing) bits.push(fmtInt(q.missing) + " without an email");
      if (q.dupes) bits.push(fmtInt(q.dupes) + " duplicate" + (q.dupes === 1 ? "" : "s"));
      if (bits.length) extra = " · " + bits.join(" · ");
    }
    return { text: fmtInt(n) + (n === 1 ? " contact" : " contacts") + " · " + data.listName + extra, cls: (q && (q.missing || q.dupes)) ? "warn" : "ok" };
  }
  if (key === "message") {
    const t = data.templates.find((x) => x.id === S.templateId);
    if (!t) return { text: "Pick a template to continue", cls: "block" };
    const bad = unmatchedFields();
    if (bad.length) return { text: "Match \u2018" + bad[0] + "\u2019 to continue", cls: "block" };
    return { text: t.name + (data.fields.length ? " · all " + data.fields.length + " fields matched" : ""), cls: "ok" };
  }
  if (key === "delivery") {
    if (!S.account) return { text: "Pick a send account to continue", cls: "block" };
    if (S.outcome === "schedule") {
      if (!S.when) return { text: "Pick a time to schedule", cls: "block" };
      return { text: "Scheduled " + fmtLong(S.when) + (S.stagger ? " · " + S.stagger + " min apart" : ""), cls: "ok" };
    }
    if (S.outcome === "send") {
      if (data.safe) return { text: "Safe mode is on - live sending is blocked", cls: "block" };
      const pl = planNow();
      return { text: "Send now" + (pl.capped ? " · " + fmtInt(pl.later) + " kept for later" : ""), cls: "ok" };
    }
    return { text: S.outcome === "draft" ? "Save as drafts (safest)" : "Keep in queue", cls: "ok" };
  }
  return { text: data.preview_total ? "Ready to review" : "Checking\u2026", cls: "ok" };
}
function firstBlocker() {
  for (const k of ["audience", "message", "delivery"]) {
    const st = statusFor(k);
    if (st.cls === "block") return { sec: k, text: st.text };
  }
  return null;
}

/* Footer: one-line summary + one primary. The primary is always actionable:
   it either names the first missing thing (and jumps there) or performs the
   outcome (D-39 / D-12). */
function footerBar() {
  const blocker = firstBlocker();
  const skip = !blocker
    ? el("button", { class: "linklike", type: "button",
        onclick: () => { if (!isOpen("review")) toggleSec("review"); $q("#sec-wrap-review").scrollIntoView({ block: "nearest" }); } },
      "Skip to review")
    : el("span");
  const primary = blocker
    ? el("button", { class: "btn btn-primary", onclick: jumpToBlocker }, blocker.text)
    : primary3();
  const sum = el("div", { class: "summary" }, el("span", {}, footerSummaryAll()));
  return el("div", { class: "send-sticky-foot" }, sum, skip, primary);
}
function footerSummaryAll() {
  const n = effN();
  if (!n) return "Set up your send";
  const t = (data.templates.find((x) => x.id === S.templateId) || {}).name || "no template";
  const pl = planNow();
  const outcome = S.outcome === "send" && pl.capped
    ? "send now (" + fmtInt(pl.today) + " today)"
    : { draft: "as drafts", schedule: "scheduled", send: "sent now", queue: "in queue" }[S.outcome];
  return fmtInt(n) + " " + (n === 1 ? "email" : "emails") + " · " + t + " · " + outcome;
}
function jumpToBlocker() {
  const b = firstBlocker();
  if (!b) return;
  if (!isOpen(b.sec)) { S.open.push(b.sec); touch(); paintSection(b.sec); }
  syncWrapWide();
  const w = $q("#sec-wrap-" + b.sec);
  if (w) w.scrollIntoView({ block: "start", behavior: "smooth" });
  const first = w && w.querySelector(".acc-body select, .acc-body input, .acc-body textarea, .acc-body .opt-card");
  if (first && first.focus) first.focus({ preventScroll: true });
}
function changeLink(step) {
  if (!STEPS.includes(step)) return;
  if (!isOpen(step)) { S.open.push(step); touch(); paintSection(step); }
  syncWrapWide();
  const w = $q("#sec-wrap-" + step);
  if (w) w.scrollIntoView({ block: "start", behavior: "smooth" });
}
function updateFooterChrome() {
  const zone = $q(".send-sticky-zone");
  if (!zone) return;
  zone.innerHTML = "";
  zone.append(footerBar());
}
function allValid() {
  if (!nPeople()) return false;
  if (!S.templateId) return false;
  if (unmatchedFields().length) return false;
  if (!S.account) return false;
  if (S.outcome === "schedule" && !S.when) return false;
  if (S.outcome === "send" && data.safe) return false;
  return true;
}

document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.tagName === "SELECT" || t.isContentEditable)) return;
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
    const btn = $q(".send-sticky-foot .btn-primary");
    if (btn && !btn.disabled) { e.preventDefault(); btn.click(); }
  }
});

function sec(id, ...nodes) {
  const d = el("div", { class: "step-sec", id: "sec-" + id });
  appendSafe(d, ...nodes);
  return d;
}
function restoreNote(step) {
  const notes = data.restoreNotes.filter((n) => n.step === step);
  if (!notes.length) return null;
  return el("div", { class: "banner banner-info", role: "status" }, icon("info", "i i-16"),
    ...notes.map((n) => el("span", {}, n.text)));
}

/* ================= Step 1: Audience ================= */
function renderStepAudience(panel) {
  const c = data.campaigns.find((x) => x.id === S.campaignId);
  if (S.campaignId && !c) {
    panel.append(el("div", { class: "banner banner-danger", role: "alert" },
      icon("alert-triangle", "i i-16"),
      el("span", {}, "This campaign no longer exists. Pick another one."),
      el("button", { class: "btn btn-ghost btn-sm push", onclick: () => { S.campaignId = 0; render(); } }, "Choose")));
    return;
  }
  if (!S.campaignId) {
    panel.append(campaignPick(),
      el("div", { class: "field-help", style: "margin-top:8px" }, "Pick a campaign to choose who to email."));
    return;
  }
  appendSafe(panel,
    el("div", { class: "send-cap" }, "Campaign: ", el("b", {}, c.name)),
    restoreNote("audience"),
    sec("aud-list", listSection()),
    sec("aud-tools", contactTools()),
    sec("aud-quality", el("div", { class: "match-note" }, icon("loader", "i i-16 spin"), el("span", {}, "Checking list quality\u2026"))));
  refreshQuality();
}

function campaignPick() {
  const sel = el("select", { class: "select", style: "max-width:380px", "aria-label": "Campaign" },
    el("option", { value: "" }, "Choose a campaign"),
    ...data.campaigns.map((c) => el("option", { value: String(c.id) }, c.name)),
    el("option", { value: "new" }, "+ New campaign"));
  sel.value = S.campaignId ? String(S.campaignId) : "";
  sel.addEventListener("change", async () => {
    if (sel.value === "new") {
      await newCampaignModal().then((id) => { if (id) { if (S.touched) clearDraftKey(); S.campaignId = id; touch(); render(); } });
    } else { if (S.touched) clearDraftKey(); S.campaignId = +sel.value; S.contactIds = []; touch(); render(); }
  });
  return el("div", { class: "field" }, el("label", { class: "field-label" }, "Campaign"), sel);
}
async function newCampaignModal() {
  return new Promise((resolve) => {
    const input = el("input", { class: "input", placeholder: "e.g. Summer 2027 recruiter outreach" });
    const btn = el("button", { class: "btn btn-primary" }, "Create & use");
    const m = modal({
      title: "New campaign", sub: "A campaign groups one outreach effort.",
      body: el("div", { class: "field" }, el("label", { class: "field-label" }, "Name"), input),
      foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), btn],
      onClose: () => resolve(null),
    });
    m.foot.querySelector(".btn-ghost").addEventListener("click", () => { m.close(); resolve(null); });
    btn.addEventListener("click", () => withLoading(btn, async () => {
      const name = input.value.trim();
      if (!name) return;
      try {
        const d = await api("/api/campaigns", { body: { name } });
        m.close();
        data.campaigns.unshift({ id: d.id, name });
        resolve(d.id);
      } catch (e) { toast(e.message, { type: "error" }); }
    }));
    input.focus();
  });
}

/* ---- list (who) ---- */
function listInfo() {
  const sheet = data.sheets.find((s) => s.id === S.listId);
  if (S.listId && !sheet) return null;
  return {
    id: S.listId,
    name: sheet ? sheet.name : "All contacts",
    count: sheet ? sheet.n_rows : data.n_contacts,
    columns: sheet ? (sheet.headers || []) : ["company", "hr_name", "hr_email"],
  };
}
function listSection() {
  const wrap = el("div", {});
  const total = data.sheets.length + 1;
  if (!data.n_contacts && !data.sheets.length) {
    wrap.append(el("div", { class: "banner banner-info" }, icon("info", "i i-16"),
      el("span", {}, "No contact lists yet. Paste a table or upload a list below.")));
    return wrap;
  }
  if (listEditing && total <= 3) {
    wrap.append(listRadioCards());
  } else {
    wrap.append(listCompactCard());
  }
  return wrap;
}
function listCompactCard() {
  const li = listInfo();
  if (!li) return el("div", { class: "banner banner-info" }, icon("info", "i i-16"), el("span", {}, "Pick a list to get started."));
  return el("div", { class: "list-card" },
    el("span", { class: "opt-ico" }, icon("users", "i i-18")),
    el("div", { style: "min-width:0;flex:1" },
      el("div", { class: "opt-title" }, el("span", {}, li.name),
        el("span", { class: "opt-count" }, fmtInt(li.count) + " " + (li.count === 1 ? "contact" : "contacts"))),
      el("div", { class: "opt-cols" },
        ...li.columns.slice(0, 4).map((h) => el("code", { class: "mono-chip" }, h)),
        li.columns.length > 4 ? el("code", { class: "mono-chip" }, "+" + (li.columns.length - 4)) : null,
        data.quality && data.quality.missing ? el("span", { class: "list-warn" }, fmtInt(data.quality.missing) + " rows have no email") : null)),
    el("button", { class: "btn btn-secondary btn-sm", onclick: changeList }, "Change list"));
}
function changeList() {
  const total = data.sheets.length + 1;
  if (total <= 3) { listEditing = true; repaint("audience"); }
  else listPickModal();
}
function listRadioCards() {
  const stack = el("div", { class: "opt-stack" });
  const mk = (id, name, count, headers, note) => {
    const b = el("button", {
      class: "opt-card" + (S.listId === id ? " is-selected" : ""), type: "button",
      onclick: () => pickList(id),
    },
      el("span", { class: "opt-ico" }, icon("users", "i i-18")),
      el("div", { style: "min-width:0;flex:1" },
        el("div", { class: "opt-title" }, el("span", {}, name),
          el("span", { class: "opt-count" }, fmtInt(count) + " " + (count === 1 ? "contact" : "contacts"))),
        headers && headers.length
          ? el("div", { class: "opt-cols" },
              ...headers.slice(0, 4).map((h) => el("code", { class: "mono-chip" }, h)),
              headers.length > 4 ? el("code", { class: "mono-chip" }, "+" + (headers.length - 4)) : null)
          : el("div", { class: "opt-desc" }, note || "Every contact in your main list.")),
      el("span", { class: "opt-radio" }));
    return b;
  };
  for (const s of data.sheets) {
    const camp = data.campaigns.find((x) => x.id === s.campaign_id);
    stack.append(mk(s.id, s.name, s.n_rows, s.headers || [], camp ? "List for " + camp.name : "Uploaded list"));
  }
  stack.append(mk(0, "All contacts", data.n_contacts, ["company", "hr_name", "hr_email"], "Every contact in your main list."));
  stack.append(el("button", { class: "linklike", style: "align-self:flex-start", onclick: () => { listEditing = false; repaint("audience"); } }, "Done"));
  return stack;
}
function listPickModal() {
  const q = el("input", { class: "input", placeholder: "Search lists", "aria-label": "Search lists" });
  const sel = el("select", { class: "select", style: "width:100%", "aria-label": "Contact list" });
  const paint = () => {
    const t = q.value.trim().toLowerCase();
    sel.innerHTML = "";
    const opts = data.sheets.map((s) => ({ id: s.id, name: s.name, count: s.n_rows }))
      .concat([{ id: 0, name: "All contacts", count: data.n_contacts }]);
    for (const o of opts) {
      if (t && !o.name.toLowerCase().includes(t)) continue;
      sel.append(el("option", { value: String(o.id), selected: String(S.listId) === String(o.id) ? "" : null },
        o.name + " - " + fmtInt(o.count) + " " + (o.count === 1 ? "contact" : "contacts")));
    }
  };
  q.addEventListener("input", paint);
  const m = modal({
    title: "Choose a list",
    sub: "Or paste or upload a new list from the tools below.",
    body: el("div", { style: "display:flex;flex-direction:column;gap:10px" }, q, sel),
    foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), el("button", { class: "btn btn-primary" }, "Use this list")],
    onClose: () => {},
  });
  m.foot.querySelector(".btn-ghost").addEventListener("click", () => m.close());
  m.foot.querySelector(".btn-primary").addEventListener("click", () => {
    const id = sel.value === "" ? 0 : +sel.value;
    m.close();
    if (id !== S.listId) pickList(id);
  });
  paint();
}
function pickList(id) {
  S.listId = id;
  S.contactIds = [];
  listEditing = false;
  touch();
  selectList(id).then(() => { autoMatch().then(render); });
}

/* Choose contacts / paste a table / upload */
function contactTools() {
  const wrap = el("div", { class: "ct-tools" });
  const pickBtn = el("button", { class: "btn btn-secondary btn-sm" }, icon("check-square", "i i-14"),
    S.contactIds.length ? "Chosen: " + fmtInt(S.contactIds.length) + " of " + fmtInt(data.listCount) + " - change" : "Choose specific contacts");
  pickBtn.addEventListener("click", () => contactPicker());
  const pasteBtn = el("button", { class: "btn btn-ghost btn-sm" }, icon("clipboard", "i i-14"), "Paste a table");
  pasteBtn.addEventListener("click", () => pasteTableModal());
  const file = el("input", { type: "file", accept: ".csv,.xlsx", style: "display:none" });
  const drop = el("button", { class: "upload-drop", type: "button" },
    icon("upload", "i i-16"),
    el("span", {}, "Upload a list: CSV or Excel. First row = column names."),
    el("span", { class: "t-caption t-faint", style: "margin-left:auto" },
      "Adds to ", el("b", {}, S.campaignId ? (data.campaigns.find((x) => x.id === S.campaignId) || {}).name : "a campaign")));
  drop.addEventListener("click", () => file.click());
  file.addEventListener("change", () => {
    const f = file.files && file.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    if (S.campaignId) fd.append("campaign_id", String(S.campaignId));
    withLoading(drop, async () => {
      try {
        const d = await api("/api/sheets/upload", { body: fd });
        data.sheets.unshift(d.sheet);
        S.listId = d.sheet.id;
        S.contactIds = [];
        touch();
        await selectList(d.sheet.id);
        await autoMatch();
        toast("List added: " + d.sheet.name + " (" + fmtInt(d.sheet.n_rows) + " rows).");
        repaint("audience");
      } catch (e) { toast(e.message, { type: "error" }); }
    });
  });
  wrap.append(pickBtn, pasteBtn, drop);
  return wrap;
}

/* Data-quality line: counts + a way in to review the rows. */
function qualityLine() {
  const n = data.quality ? data.quality.n : nPeople();
  const wrap = el("div", { class: "match-note" + (data.quality && (data.quality.missing || data.quality.dupes) ? " warn" : " ok") });
  wrap.append(icon(data.quality && (data.quality.missing || data.quality.dupes) ? "alert-triangle" : "check-circle-2", "i i-16"));
  const parts = [fmtInt(n) + " " + (n === 1 ? "contact" : "contacts")];
  if (data.quality) {
    if (data.quality.missing) parts.push(fmtInt(data.quality.missing) + " with no email");
    if (data.quality.dupes) parts.push(fmtInt(data.quality.dupes) + " " + (data.quality.dupes === 1 ? "duplicate email" : "duplicate emails"));
  }
  wrap.append(el("span", {}, parts.join(", ")));
  if (data.quality) wrap.append(el("button", { class: "linklike", onclick: () => contactPicker() }, "Review"));
  if (data.quality && data.quality.dupes) {
    const cb = el("input", { type: "checkbox", "aria-label": "Skip duplicate emails",
      style: "width:15px;height:15px;accent-color:var(--primary-500);margin-left:10px" });
    cb.checked = S.skipDupes;
    cb.addEventListener("change", () => {
      S.skipDupes = cb.checked; touch();
      updateStatuses();
      const sc = $q("#summary-card");
      if (sc && isOpen("review")) sc.replaceWith(summaryCard());
      if (isOpen("review")) drawChecks();
    });
    wrap.append(el("label", { style: "display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:550;cursor:pointer" },
      cb, "Skip duplicates (keep first)"));
  }
  return wrap;
}

/* ---- contact picker ---- */
async function contactPicker() {
  const sourceRows = await audienceRows();
  const total = sourceRows.length;
  if (!total) { toast("There are no contacts in this list yet.", { type: "error" }); return; }
  const state = { q: "", sel: new Set(S.contactIds) };
  const list = el("div", { class: "pick-list" });
  const countLbl = el("span", { class: "pick-count" }, "");
  const selAll = el("button", { class: "btn btn-ghost btn-sm" }, "Select all");
  const clear = el("button", { class: "btn btn-ghost btn-sm" }, "Clear");
  const qInp = el("input", { class: "input", placeholder: "Search company, name, email or No.", "aria-label": "Search contacts" });
  function visible() {
    if (!state.q) return sourceRows;
    const q = state.q.toLowerCase();
    return sourceRows.filter((r) =>
      String(r.company || "").toLowerCase().includes(q) ||
      String(r.hr_name || "").toLowerCase().includes(q) ||
      String(r.hr_email || "").toLowerCase().includes(q) ||
      String(r.serial || "").includes(q));
  }
  function paint() {
    countLbl.textContent = fmtInt(state.sel.size) + " of " + fmtInt(total) + " chosen";
    list.innerHTML = "";
    const vis = visible();
    if (!vis.length) {
      list.append(el("div", { class: "t-caption", style: "padding:14px" }, "No contacts match."));
    }
    for (const r of vis.slice(0, 500)) {
      const cb = el("input", { type: "checkbox", "aria-label": "Choose " + (r.hr_name || r.company || r.hr_email) });
      cb.checked = state.sel.has(r.id);
      cb.addEventListener("change", () => {
        if (cb.checked) state.sel.add(r.id); else state.sel.delete(r.id);
        countLbl.textContent = fmtInt(state.sel.size) + " of " + fmtInt(total) + " chosen";
      });
      list.append(el("label", { class: "pick-row" },
        cb,
        el("span", { class: "pick-no" }, r.serial || "\u2014"),
        el("span", { class: "pick-co" }, r.company || ""),
        el("span", { class: "pick-who" }, [r.hr_name, r.hr_email].filter(Boolean).join(" \u00b7 "))));
    }
    if (vis.length > 500) list.append(el("div", { class: "t-caption", style: "padding:10px" },
      "Showing the first 500 matches - narrow the search to pick more."));
  }
  selAll.addEventListener("click", () => { visible().forEach((r) => state.sel.add(r.id)); paint(); });
  clear.addEventListener("click", () => { state.sel.clear(); paint(); });
  let qt;
  qInp.addEventListener("input", () => { clearTimeout(qt); qt = setTimeout(() => { state.q = qInp.value.trim(); paint(); }, 150); });

  const foot = el("div", { style: "display:flex;gap:8px;justify-content:flex-end;margin-top:12px" });
  const okBtn = el("button", { class: "btn btn-primary" }, "Use selection");
  const cancel = el("button", { class: "btn btn-ghost" }, "Cancel");
  const m = modal({
    title: "Choose who to email",
    sub: data.listName + " \u00b7 " + fmtInt(total) + " contacts. Leave nothing selected to email everyone.",
    wide: true,
    body: el("div", { class: "pick-wrap" },
      el("div", { class: "pick-tools" }, qInp, el("span", { class: "push" }), selAll, clear, countLbl),
      list,
      foot,
    ),
    foot: [cancel, okBtn],
    onClose: () => m.close(),
  });
  cancel.addEventListener("click", () => m.close());
  okBtn.addEventListener("click", () => {
    S.contactIds = [...state.sel].sort((a, b) => a - b);
    touch();
    m.close();
    repaint("audience");
  });
  paint();
}

/* ---- paste a table ---- */
function pasteTableModal() {
  const name = el("input", { class: "input", placeholder: "List name (optional)", "aria-label": "List name" });
  const ta = el("textarea", { class: "textarea", rows: "9", style: "width:100%", "aria-label": "Pasted table",
    placeholder: "Paste straight from Excel or Google Sheets - the first row is the column names.\n\nCompany\tRecruiter name\tHR email\nAccolite\tGenevieve Dsouza\tgen.dsouza@accoliteindia.com" });
  const btn = el("button", { class: "btn btn-primary" }, "Add this list");
  const camp = data.campaigns.find((c) => c.id === S.campaignId);
  const m = modal({
    title: "Paste a table",
    sub: (camp ? "It joins " + camp.name + " as a contact list. " : "") +
      "Columns can be company, recruiter/HR name, email - in any order.",
    wide: true,
    body: el("div", { style: "display:flex;flex-direction:column;gap:10px" },
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Name"), name), ta),
    foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), btn],
    onClose: () => m.close(),
  });
  m.foot.querySelector(".btn-ghost").addEventListener("click", () => m.close());
  btn.addEventListener("click", () => withLoading(btn, async () => {
    try {
      const d = await api("/api/sheets/paste", { body: { text: ta.value, name: name.value.trim(), campaign_id: S.campaignId } });
      data.sheets.unshift({ id: d.sheet_id, name: d.name, campaign_id: S.campaignId, headers: d.headers, n_rows: d.n_rows });
      S.listId = d.sheet_id;
      S.contactIds = [];
      touch();
      m.close();
      await selectList(d.sheet_id);
      await autoMatch();
      toast("List added: " + d.name + " (" + fmtInt(d.n_rows) + " rows)." + (d.duplicates ? " " + fmtInt(d.duplicates) + " were duplicates." : ""));
      render();
    } catch (e) { toast(e.message, { type: "error" }); }
  }));
  setTimeout(() => ta.focus(), 50);
}

/* ================= Step 2: Message ================= */
function renderStepMessage(panel) {
  appendSafe(panel,
    restoreNote("message"),
    sec("msg-template", templateSection()),
    sec("msg-match", matchSection()),
    sec("msg-options", messageOptions()),
  );
}

/* ---- template (what) ---- */
function templateSection() {
  const t = data.templates.find((x) => x.id === S.templateId);
  if (!data.templates.length) {
    return el("div", { class: "tpl-main-card" },
      el("div", { class: "banner banner-info" }, icon("info", "i i-16"),
        el("span", {}, "No templates yet. Write one with AI or upload an HTML file.")),
      uploadAiRow());
  }
  if (!t) {
    return el("div", { class: "tpl-main-card" },
      el("div", { class: "banner banner-info" }, icon("info", "i i-16"),
        el("span", {}, "No template selected.")),
      el("div", {}, el("button", { class: "btn btn-secondary", onclick: templateGallery }, "Choose a template")));
  }
  const fields = Array.isArray(t.fields) ? t.fields : [];
  return el("div", { class: "tpl-main-card" },
    el("div", { class: "tpl-main-head" },
      el("div", { class: "tpl-main-title" }, el("b", {}, t.name),
        t.is_active ? badge2("Default") : null,
        t.kind === "reminder" ? badge2("Follow-up") : null),
      el("span", { class: "push" }),
      el("button", { class: "btn btn-secondary btn-sm", onclick: templateGallery }, "Change template")),
    el("div", { class: "opt-desc" }, t.subject || "No subject"),
    fields.length
      ? el("div", { class: "opt-desc" }, fields.length + " " + (fields.length === 1 ? "field" : "fields") + " to fill in: " + fields.map(esc).join(", "))
      : el("div", { class: "opt-desc" }, "No fill-in fields - every email is identical"),
    tplMeta(t),
    sameSubjectWarn(t),
    lastUsedNotice(),
    el("div", { class: "tpl-main-preview" },
      el("iframe", { class: "tpl-main-frame", srcdoc: thumbDoc(t), "aria-label": "Template preview", scrolling: "no" })),
    el("div", { class: "tpl-main-foot" },
      el("a", { class: "linklike", href: "/library/templates/" + t.id }, "Edit in Library"),
      el("span", { class: "t-caption t-faint" }, "Previewed with sample data from " + data.listName)));
}
function templateGallery() {
  const m = modal({
    title: "Change template",
    sub: "Pick the message every person gets.",
    wide: true,
    body: el("div", {},
      data.templates.length
        ? el("div", { class: "opt-stack" }, ...data.templates.map((t) => tplCard(t, () => { pickTemplate(t.id); m.close(); })))
        : el("div", { class: "banner banner-info" }, icon("info", "i i-16"),
            el("span", {}, "No templates yet. Write one with AI or upload an HTML file.")),
      uploadAiRow()),
    foot: [el("button", { class: "btn btn-ghost" }, "Cancel")],
    onClose: () => {},
  });
  m.foot.querySelector(".btn-ghost").addEventListener("click", () => m.close());
}
function pickTemplate(id) {
  if (S.templateId === id) return;
  S.templateId = id;
  S.matchOpen = false;
  touch();
  autoMatch().then(() => repaint("message", "review"));
}
function tplMeta(t) {
  const bits = [];
  if (t.updated_at) bits.push("Last edited " + relTime(t.updated_at));
  if (t.n_campaigns) bits.push("Used in " + fmtInt(t.n_campaigns) + " campaign" + (t.n_campaigns === 1 ? "" : "s"));
  return bits.length ? el("div", { class: "opt-desc tpl-meta" }, bits.join(" \u00b7 ")) : null;
}
function sameSubjectOther(t) {
  const subj = (t.subject || "").trim().toLowerCase();
  if (!subj) return null;
  return data.templates.find((o) => o.id !== t.id && (o.subject || "").trim().toLowerCase() === subj) || null;
}
function sameSubjectWarn(t) {
  const o = sameSubjectOther(t);
  return o ? el("div", { class: "opt-desc tpl-warn" }, "Same subject as \u2018" + esc(o.name) + "\u2018") : null;
}
function previewTemplate(t) {
  const m = modal({
    title: "Preview: " + t.name,
    sub: "Previewed with sample data from " + data.listName + ".",
    wide: true,
    body: el("div", {}, el("div", { class: "tpl-full-preview" },
      el("iframe", { class: "tpl-full-frame", srcdoc: thumbDoc(t), scrolling: "no", "aria-label": "Full template preview" }))),
    foot: [el("button", { class: "btn btn-ghost" }, "Close")],
  });
  m.foot.querySelector(".btn-ghost").addEventListener("click", () => m.close());
}
function lastUsedNotice() {
  const camp = data.campaigns.find((x) => x.id === S.campaignId);
  if (!camp || !camp.last_template_id || camp.last_template_id === S.templateId) return null;
  const lt = data.templates.find((t) => t.id === camp.last_template_id);
  if (!lt) return null;
  return el("div", { class: "banner banner-info", role: "status", style: "margin-top:8px" },
    icon("info", "i i-16"), el("span", {}, "You used \u2018" + lt.name + "\u2019 last time."));
}
function tplCard(t, onPick) {
  const fields = Array.isArray(t.fields) ? t.fields : [];
  return el("button", {
    class: "opt-card tpl-card" + (S.templateId === t.id ? " is-selected" : ""), type: "button",
    onclick: () => onPick(t),
  },
    el("div", { class: "tpl-thumb", "data-tid": String(t.id) },
      el("iframe", { class: "tpl-thumb-frame", srcdoc: thumbDoc(t), "aria-hidden": "true", scrolling: "no" })),
    el("div", { style: "min-width:0;flex:1" },
      el("div", { class: "opt-title" }, el("span", {}, t.name),
        t.is_active ? badge2("Default") : null,
        t.kind === "reminder" ? badge2("Follow-up") : null),
      el("div", { class: "opt-desc" }, t.subject || "No subject"),
      fields.length ? el("div", { class: "opt-desc" }, fields.length + " " + (fields.length === 1 ? "field" : "fields") + " to fill in") : el("div", { class: "opt-desc" }, "No fill-in fields - every email is identical"),
      tplMeta(t),
      sameSubjectWarn(t)),
    el("a", { class: "btn btn-ghost btn-sm tpl-preview", href: "#", "aria-label": "Preview " + t.name,
      onclick: (e) => { e.preventDefault(); e.stopPropagation(); previewTemplate(t); } }, "Preview"),
    el("a", { class: "btn btn-ghost btn-sm tpl-edit", href: "/library/templates/" + t.id, "aria-label": "Edit template",
      onclick: (e) => e.stopPropagation() }, icon("pencil", "i i-14"), "Edit"),
    el("span", { class: "opt-radio" }));
}
function uploadAiRow() {
  const row = el("div", { style: "display:flex;gap:8px;margin-top:10px;flex-wrap:wrap" });
  const fileIn = el("input", { type: "file", accept: ".html,.htm,.txt", style: "display:none" });
  row.append(el("button", { class: "btn btn-ghost", onclick: () => fileIn.click() }, "Upload a template"));
  row.append(el("button", { class: "btn btn-ai", onclick: aiTemplateModal }, icon("sparkles", "i i-16"), "Write one with AI"));
  fileIn.addEventListener("change", () => {
    const f = fileIn.files && fileIn.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    withLoading(row.querySelector(".btn-ghost"), async () => {
      try {
        const d = await api("/api/templates/upload", { body: fd });
        data.templates.push({ id: d.id, name: f.name, fields: d.fields || [] });
        S.templateId = d.id;
        touch();
        toast("Template added.");
        autoMatch().then(render);
      } catch (e) { toast(e.message, { type: "error" }); }
    });
  });
  return row;
}
function badge2(label) { return el("span", { class: "badge neutral" }, label); }
function thumbDoc(t) {
  const body = localToUrl(String(t.body_html || ""));
  return "<!doctype html><html><head><meta charset='utf-8'><style>" +
    "body{margin:0;padding:10px;font:12px/1.45 -apple-system,'Segoe UI',Arial,sans-serif;color:#111;background:#fff;overflow:hidden}" +
    "img{max-width:100%} p{margin:0 0 8px} a{color:#4350C4}" +
    "</style></head><body>" + body + "</body></html>";
}
function aiTemplateModal() {
  const name = el("input", { class: "input", placeholder: "e.g. Summer 2027 outreach" });
  const subj = el("input", { class: "input", placeholder: "e.g. Internship drive at our campus" });
  const brief = el("textarea", { class: "textarea", rows: "3", placeholder: "What should this email say? Audience, the ask, anything personal." });
  const tone = el("select", { class: "select" },
    el("option", { value: "friendly" }, "Friendly"),
    el("option", { value: "professional" }, "Professional"),
    el("option", { value: "short" }, "Short and direct"));
  const btn = el("button", { class: "btn btn-primary" }, "Write template");
  const m = modal({
    title: "Write a template with AI", sub: "You review and edit it before anyone gets it.",
    body: el("div", { style: "display:flex;flex-direction:column;gap:12px" },
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Name"), name),
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Subject"), subj),
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Brief"), brief),
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Tone"), tone)),
    foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), btn],
    onClose: () => {},
  });
  m.foot.querySelector(".btn-ghost").addEventListener("click", () => m.close());
  btn.addEventListener("click", () => withLoading(btn, async () => {
    try {
      const d = await api("/api/templates/ai", { body: { name: name.value.trim(), subject: subj.value.trim(), brief: brief.value.trim(), tone: tone.value } });
      m.close();
      data.templates.push(d.template);
      S.templateId = d.template.id;
      touch();
      toast("Template written. Open it to edit.");
      location.href = "/library/templates/" + d.template.id;
    } catch (e) { toast(e.message, { type: "error" }); }
  }));
}


/* ---- field matching (Message step) ---- */
function rebuildWithFocus(host, build) {
  const f = document.activeElement;
  const key = f && host.contains(f) && f.dataset ? (f.dataset.focusKey || null) : null;
  let selStart = null, selEnd = null;
  if (f && f.setSelectionRange) { try { selStart = f.selectionStart; selEnd = f.selectionEnd; } catch { /* read-only */ } }
  host.innerHTML = "";
  appendSafe(host, build());
  if (key) {
    const nf = host.querySelector('[data-focus-key="' + key + '"]');
    if (nf) {
      nf.focus();
      if (selStart != null && nf.setSelectionRange) { try { nf.setSelectionRange(selStart, selEnd); } catch { /* range out of bounds */ } }
    }
  }
}
function paintMatchSection() {
  const host = $q("#sec-msg-match");
  if (host) rebuildWithFocus(host, matchSection());
  updateFooterChrome();
}
function matchSection() {
  const t = data.templates.find((x) => x.id === S.templateId);
  if (!t) return el("div", { class: "match-note" }, icon("info", "i i-16"),
    el("span", {}, "Pick a template to see field matching."));
  if (!data.fields.length) {
    return el("div", { class: "match-note ok" }, icon("check-circle-2", "i i-16"),
      el("span", {}, "No fill-in fields - every email is identical."));
  }
  const unmatched = data.fields.filter((f) => !S.mapping[f]);
  const wrap = el("div", {});
  if (!unmatched.length) {
    wrap.append(el("div", { class: "match-note ok" }, icon("check-circle-2", "i i-16"),
      el("span", {}, "All " + data.fields.length + " " + (data.fields.length === 1 ? "field" : "fields") + " matched")));
    const d = el("details", { class: "match-disc", style: "display:flex;margin-top:6px" },
      el("summary", {}, "Review matches"));
    const body = el("div", { class: "match-readonly" });
    d.append(body);
    d.addEventListener("toggle", () => { if (d.open && !body.children.length) buildMatchReadonly(body); });
    wrap.append(d);
  } else {
    wrap.append(el("div", { class: "match-note warn" }, icon("alert-triangle", "i i-16"),
      el("span", {}, unmatched.length + " " + (unmatched.length === 1 ? "field needs" : "fields need") + " your help: " + unmatched.map((f) => esc(f)).join(", ")),
      el("button", { class: "linklike", onclick: () => { S.matchOpen = !S.matchOpen; touch(); repaint("message"); } }, S.matchOpen ? "Hide" : "Fix now")));
    if (S.matchOpen) { const host = el("div", { id: "match-panel" }); buildMatchPanel(host); wrap.append(host); }
  }
  return wrap;
}
function buildMatchReadonly(host) {
  host.innerHTML = "";
  for (const f of data.fields) {
    const m = S.mapping[f] || "";
    const isCol = String(m).startsWith("column:");
    const from = isCol ? String(m).slice(7) : "your text";
    host.append(el("div", { class: "mr-row" },
      el("code", { class: "mono-chip" }, f),
      icon("arrow-right", "i i-14"),
      el("span", {}, "Filled from " + (isCol ? from : "'" + esc(m) + "'")),
      isCol ? (sampleFor(from, f) ? el("span", { class: "mr-sample" }, sampleFor(from, f)) : null) : null));
  }
  host.append(el("button", { class: "linklike", style: "align-self:flex-start;margin-top:4px", onclick: () => {
    host.innerHTML = "";
    const panel = el("div", { id: "match-panel" });
    buildMatchPanel(panel);
    host.append(panel);
  } }, "Edit matches"));
}

/* ================= Step 3: Delivery ================= */
function renderStepDelivery(panel) {
  appendSafe(panel,
    restoreNote("delivery"),
    sec("del-account", accountSelectSec()),
    sec("del-outcome", outcomeSection()),
    sec("del-followup", followUpWrap()),
  );
}
function accountSelectSec() {
  return el("div", { class: "field" }, el("label", { class: "field-label" }, "Send from"), accountSelect());
}
function outcomeSection() {
  const wrap = el("div", {});
  wrap.append(outcomeCards());
  if (S.outcome === "schedule") wrap.append(whenBlock());
  return wrap;
}

/* ---- matching ---- */
function buildMatchPanel(host) {
  if (!host) return;
  host.innerHTML = "";
  const table = el("table", { class: "match-table" });
  table.append(el("tr", {}, el("th", {}, "In your email"), el("th", {}, ""), el("th", {}, "Filled from")));
  for (const f of data.fields) {
    const sel = el("select", { class: "select", style: "max-width:260px" },
      el("option", { value: "" }, "Pick a column"),
      ...data.columns.map((c) => el("option", { value: c }, c + sampleFor(c, f))),
      el("option", { value: "__text__" }, "Type my own text..."),
      el("option", { value: "" }, "Leave blank"));
    if (S.mapping[f] && String(S.mapping[f]).startsWith("column:")) sel.value = S.mapping[f].slice(7);
    else if (S.mapping[f]) { sel.value = "__text__"; }
    const textIn = el("input", { class: "input", style: "max-width:260px;display:none", placeholder: "Fixed text, e.g. Campus Placement Cell", "data-focus-key": "match-" + f });
    if (S.mapping[f] === "__text__") textIn.style.display = "";
    const row = el("tr", { class: S.mapping[f] ? "" : "is-unmatched" },
      el("td", {}, el("code", { class: "mono-chip" }, f)),
      el("td", { class: "arrow" }, icon("arrow-right", "i i-14")),
      el("td", {}, sel, textIn));
    const commit = () => {
      if (sel.value === "__text__") {
        const v = textIn.value.trim();
        if (v) S.mapping[f] = v; else delete S.mapping[f];
      } else if (sel.value) S.mapping[f] = "column:" + sel.value;
      else delete S.mapping[f];
      S.mappedFor = S.templateId + ":" + S.listId;  // user's choice is authoritative
      touch();
      row.classList.toggle("is-unmatched", !S.mapping[f]);
      paintMatchSection();
    };
    sel.addEventListener("change", () => {
      textIn.style.display = sel.value === "__text__" ? "" : "none";
      commit();
    });
    textIn.addEventListener("change", commit);
    textIn.value = S.mapping[f] && !String(S.mapping[f]).startsWith("column:") ? S.mapping[f] : "";
    table.append(row);
  }
  if (data.ai_on) {
    const aib = el("button", { class: "btn btn-ai btn-sm", style: "margin-top:10px" }, icon("sparkles", "i i-16"), "Match with AI");
    aib.addEventListener("click", () => withLoading(aib, async () => {
      try {
        const d = await api("/api/ai/mapping", { body: { fields: data.fields, columns: data.columns } });
        S.mapping = {};
        for (const [f, col] of Object.entries(d.mapping || {})) if (col) S.mapping[f] = String(col).startsWith("column:") ? col : "column:" + col;
        S.mappedFor = S.templateId + ":" + S.listId;
        touch();
        toast("Matched with AI. Review the choices.");
        render();
      } catch (e) { toast(e.message, { type: "error" }); }
    }));
    table.append(el("tr", {}, el("td", { colspan: "3" }, aib)));
  }
  host.append(table);
}
function audRows() {
  return S.listId ? (data.sheetRows || []) : (data.allRows || []);
}
function sampleFor(col, field) {
  const rows = audRows();
  if (!rows.length) return "";
  const r = rows.find((r) => r[col]);
  return r ? ", e.g. " + String(r[col]).slice(0, 24) : "";
}

/* ---- send from ---- */
function accountSelect() {
  const sel = el("select", { class: "select", style: "max-width:440px", "aria-label": "Send from" },
    ...data.accounts.map((a) => el("option", { value: String(a.id) }, a.email || a.label)));
  if (!S.account && data.accounts.length) S.account = data.last_account_id || data.accounts[0].id;
  sel.value = String(S.account || "");
  sel.addEventListener("change", () => { S.account = +sel.value; touch(); repaint("delivery", "review"); });
  return el("div", {}, sel,
    el("div", { class: "field-help" }, data.accounts.length
      ? "The Gmail account that sends. Add more in Settings."
      : "No connected account - connect one in Settings first."));
}

/* ================= Step 2: Check ================= */
const previewHtmlEsc = (x) => String(x ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

function previewDoc(p, fills, chrome = {}) {
  const fillsJson = JSON.stringify(fills).replace(/</g, "\\u003c");
  const hi = chrome.hi !== false && fills.length > 0;
  const atts = (chrome.atts || []).map(previewHtmlEsc).join(", ");
  return "<!doctype html><html><head><meta charset='utf-8'>" +
    "<style>" +
    "body{margin:0;padding:18px 20px;font:14px/1.55 -apple-system,'Segoe UI',Arial,sans-serif;color:#1a1a1a;background:#fff}" +
    ".cm-to{font-size:12.5px;color:#667;margin-bottom:4px}.cm-to b{color:#111}" +
    ".cm-meta{font-size:12px;color:#889;margin-bottom:4px}" +
    ".cm-subj{font-size:14.5px;font-weight:650;margin:6px 0 12px;padding-bottom:10px;border-bottom:1px solid #ececf2}" +
    "img{max-width:100%} a{color:#4350C4}" +
    ".cm-fill{border-bottom:2px dotted #b3baff;background:#f2f3ff;padding:0 1px;border-radius:2px}" +
    "</style></head><body>" +
    "<div class='cm-to'>To: <b>" + previewHtmlEsc(p.hr_email) + "</b>" +
    (p.hr_name ? " &middot; " + previewHtmlEsc(p.hr_name) : "") +
    (p.company ? " &middot; " + previewHtmlEsc(p.company) : "") + "</div>" +
    (chrome.from ? "<div class='cm-meta'>From: " + previewHtmlEsc(chrome.from) + "</div>" : "") +
    (chrome.cc ? "<div class='cm-meta'>CC: " + previewHtmlEsc(chrome.cc) + "</div>" : "") +
    (atts ? "<div class='cm-meta'>Attachment" + (chrome.atts.length === 1 ? "" : "s") + ": " + atts + "</div>" : "") +
    "<div class='cm-subj'>" + previewHtmlEsc(p.subject) + "</div>" +
    localToUrl(p.body) +
    (hi
      ? "<script>" +
      "var fills=" + fillsJson + ";" +
      "function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}" +
      "function walk(n){" +
      "if(n.nodeType===3){var t=n.nodeValue,ch=false;" +
      "for(var i=0;i<fills.length;i++){var v=fills[i];if(v&&t.indexOf(v)>=0){t=t.split(v).join('<span class=cm-fill>'+esc(v)+'</span>');ch=true;}}" +
      "if(ch){var d=document.createElement('div');d.innerHTML=t;while(d.firstChild){n.parentNode.insertBefore(d.firstChild,n);}n.parentNode.removeChild(n);}}" +
      "else if(n.nodeType===1&&n.nodeName!=='SCRIPT'&&n.nodeName!=='STYLE'){Array.prototype.slice.call(n.childNodes).forEach(walk);}}" +
      "document.body.querySelectorAll('*').forEach(walk);" +
        "<\/script>"
      : "") +
    "</body></html>";
}

function stepRecipient(dir) {
  const f = data.reviewFocus;
  if (f) {
    const n = f.list.length;
    if (!n) return;
    f.idx = (f.idx + dir + n) % n;
    paintFocus();
    return;
  }
  const n = data.previews.length;
  if (!n) return;
  data.preview_idx = (data.preview_idx + dir + n) % n;
  const frame = $q(".preview-paper");
  if (frame) drawPreview(frame);
  const cap = $q(".recip-caption");
  if (cap) cap.textContent = "Person " + (data.preview_idx + 1) + " of " + n + " shown · " +
    fmtInt(data.preview_total) + " " + (data.preview_total === 1 ? "person" : "people") + " in total";
}

function fillsFor(row) {
  const fills = [];
  for (const f of data.fields) {
    const m = S.mapping[f];
    if (!m) continue;
    const v = String(m).startsWith("column:") ? String(row[String(m).slice(7)] ?? "") : String(m);
    if (v && !fills.includes(v)) fills.push(v);
  }
  return fills;
}
function chromeFor() {
  return { from: accountEmail(), cc: S.cc.trim(), atts: files.map((f) => f.name), hi: S.fillHi };
}
function drawPreview(frame) {
  const p = data.previews[data.preview_idx];
  if (!p) {
    frame.srcdoc = "<!doctype html><html><body style='margin:0;padding:40px;font:14px/1.5 -apple-system,'Segoe UI',Arial,sans-serif;color:#667;background:#fff'>" +
      "No one to preview - this list is empty.</body></html>";
    return;
  }
  const rows = (S.listId ? (data.sheetRows || []) : (data.allRows || []));
  const row = rows.find((r) => String(r.hr_email || "") === String(p.hr_email || "")) || {};
  frame.srcdoc = previewDoc(p, fillsFor(row), chromeFor());
}

/* D-19: preview any recipient on demand (search or issue rows) via a
   single-contact /api/merge/preview call. */
async function paintFocus() {
  const f = data.reviewFocus;
  const frame = $q(".preview-paper");
  if (!f || !frame) return;
  const cap = $q("#recip-caption");
  const row = f.list[f.idx];
  if (!f.loaded) {
    if (cap) cap.textContent = "Loading preview\u2026";
    if (row && String(row.hr_email || "").trim()) {
      try {
        const d = await api("/api/merge/preview", { body: {
          template_id: S.templateId, mapping: S.mapping, sheet_id: S.listId,
          contact_ids: [row.id], limit: 1, append_signature: S.append_signature ? 1 : 0,
        } });
        if (data.reviewFocus !== f) return;
        f.previews = d.previews || [];
        f.loaded = true;
      } catch (e) {
        if (data.reviewFocus !== f) return;
        f.loaded = true; f.previews = []; f.err = e.message;
        if (cap) cap.textContent = "Couldn\u2019t load this preview: " + e.message;
        return;
      }
    } else {
      f.loaded = true; f.previews = [];
    }
  }
  drawFocusFrame(frame, f);
  capFocus(f);
}
function drawFocusFrame(frame, f) {
  const row = f.list[f.idx];
  const p = (f.previews || [])[0];
  if (!row) {
    frame.srcdoc = "<!doctype html><html><body style='margin:0;padding:40px;font:14px/1.5 -apple-system,'Segoe UI',Arial,sans-serif;color:#667;background:#fff'>No recipients.</body></html>";
    return;
  }
  if (!p) {
    frame.srcdoc = "<!doctype html><html><body style='margin:0;padding:40px;font:14px/1.5 -apple-system,'Segoe UI',Arial,sans-serif;color:#667;background:#fff'>" +
      (String(row.hr_email || "").trim() ? "No preview for this row." : "This row has no email address - it will be skipped.") +
      "</body></html>";
    return;
  }
  frame.srcdoc = previewDoc(p, fillsFor(row), chromeFor());
}
function capFocus(f) {
  const cap = $q("#recip-caption");
  if (!cap) return;
  cap.innerHTML = "";
  const row = f.list[f.idx];
  const who = row ? (row.hr_name || row.hr_email || ("row " + (row.serial || "?"))) : "";
  const back = el("a", { class: "linklike", href: "#" }, "back to first 8");
  back.addEventListener("click", (e) => { e.preventDefault(); clearFocus(); });
  if (f.kind === "issues") {
    cap.append("Issue " + (f.idx + 1) + " of " + f.list.length + " \u00b7 " + esc(who) +
      (row && row.__issues && row.__issues.length ? " \u00b7 " + row.__issues.join(", ") : "") + " \u00b7 ", back);
  } else {
    cap.append("Previewing " + esc(who) + " \u00b7 match " + (f.idx + 1) + " of " + f.list.length + " \u00b7 ", back);
  }
}
function clearFocus() {
  data.reviewFocus = null;
  const frame = $q(".preview-paper");
  if (frame) drawPreview(frame);
  const cap = $q("#recip-caption");
  if (cap) cap.textContent = data.previews.length
    ? "Person " + (data.preview_idx + 1) + " of " + data.previews.length + " shown \u00b7 " +
      fmtInt(data.preview_total) + " " + (data.preview_total === 1 ? "person" : "people") + " in total"
    : "No recipients in this selection.";
}
/* D-19: rows needing a look - no email, a blank fill, or a skipped duplicate. */
function issueRows(rows) {
  const keep = (S.skipDupes && data.dedupe && data.dedupe.keepIds) ? new Set(data.dedupe.keepIds) : null;
  const out = [];
  for (const r of rows) {
    const rs = [];
    if (!(r.hr_email || "").trim()) rs.push("no email");
    for (const f of data.fields) {
      const m = S.mapping[f];
      if (m && String(m).startsWith("column:") && !(String(r[String(m).slice(7)] ?? "").trim())) { rs.push("blank \u2018" + f + "\u2019"); break; }
    }
    if (keep && String(r.hr_email || "").trim() && !keep.has(r.id)) rs.push("duplicate (skipped)");
    if (rs.length) out.push(Object.assign({}, r, { __issues: rs }));
  }
  return out;
}

function checksCard() {
  return el("div", { class: "summary-card", id: "checks-card" },
    el("h3", {}, "Quick checks"),
    el("div", { class: "check-line info" }, icon("info", "i i-16"), el("span", {}, "Running…")));
}

async function drawChecks() {
  const card = $q("#checks-card");
  if (!card) return;
  card.querySelectorAll(".check-line, .btn").forEach((n) => n.remove());
  const rows = await checkRows();
  const total = effN();
  const push = (cls, ic, html) => card.append(el("div", { class: "check-line " + cls }, icon(ic, "i i-16"), el("span", { html })));
  if (!total) {
    push("warn", "alert-triangle", "This selection has no recipients yet.");
    return;
  }
  push("ok", "check-circle-2", "<b>" + fmtInt(total) + "</b> " + (total === 1 ? "person" : "people") + " will get this");
  const missing = rows.filter((r) => !(r.hr_email || "").trim()).length;
  if (missing) push("warn", "alert-triangle", "<b>" + fmtInt(missing) + "</b> row" + (missing === 1 ? " has" : "s have") + " no email address - they'll be skipped");
  else push("ok", "check-circle-2", "Every row has an email address");
  const dd = data.dedupe || dedupeRows(rows);
  const dupNote = fmtInt(dd.dupes) + " duplicate email" + (dd.dupes === 1 ? "" : "s");
  if (dd.dupes) {
    if (S.skipDupes) {
      push("ok", "check-circle-2", dupNote + " will be skipped (keep first)" +
        (dd.crossCompany ? " - " + fmtInt(dd.crossCompany) + " shared by 2+ companies" : "") +
        " - <a class='linklike' id='view-dupes'>view</a>");
    } else {
      push("warn", "alert-triangle", dupNote + " - each one still gets an email" +
        (dd.crossCompany ? " (" + fmtInt(dd.crossCompany) + " shared by 2+ companies)" : "") +
        " - <a class='linklike' id='skip-dupes'>skip duplicates</a> · <a class='linklike' id='view-dupes'>view</a>");
    }
  }
  const blanks = [];
  for (const f of data.fields) {
    const m = S.mapping[f];
    if (!m) { blanks.push(f); continue; }
    if (String(m).startsWith("column:")) {
      const col = String(m).slice(7);
      const n = rows.filter((r) => !String(r[col] ?? "").trim()).length;
      if (n) blanks.push(f + " (" + n + " blank" + (n === 1 ? "" : "s") + ")");
    }
  }
  if (blanks.length) {
    push("warn", "alert-triangle", "Fills left empty: " + blanks.map(esc).join(", ") + " - <a class='linklike' id='fix-match'>fix</a>");
  } else if (data.fields.length) {
    push("ok", "check-circle-2", "All " + data.fields.length + " fill-in field" + (data.fields.length === 1 ? "" : "s") + " filled for everyone");
  } else {
    push("ok", "check-circle-2", "No fill-in fields - every email is identical");
  }
  const fix = card.querySelector("#fix-match");
  if (fix) fix.addEventListener("click", (e) => { e.preventDefault(); S.matchOpen = true; touch(); changeLink("message"); });
  const sd = card.querySelector("#skip-dupes");
  if (sd) sd.addEventListener("click", (e) => {
    e.preventDefault();
    S.skipDupes = true; touch();
    syncDupesToggle(); drawChecks(); updateStatuses();
    const sc = $q("#summary-card");
    if (sc) sc.replaceWith(summaryCard());
  });
  const vd = card.querySelector("#view-dupes");
  if (vd) vd.addEventListener("click", (e) => { e.preventDefault(); viewDuplicates(); });
  const tbtn = el("button", { class: "btn btn-secondary", style: "width:100%;justify-content:center;margin-top:12px" },
    icon("send", "i i-14"), " Send me a test");
  card.append(tbtn);
  tbtn.addEventListener("click", () => withLoading(tbtn, async () => {
    if (!S.account) { toast("Pick a send account first.", { type: "error" }); return; }
    try {
      const d = await api("/api/emails/test", { body: {
        template_id: S.templateId, mapping: S.mapping, sheet_id: S.listId,
        contact_ids: S.contactIds, account_id: S.account, append_signature: S.append_signature ? 1 : 0,
      } });
      toast(d.mode === "draft"
        ? "Test saved to your Gmail Drafts (Safe mode is on)."
        : "Test sent to " + data.meEmail + " - check your inbox.", { type: "success", duration: 8000 });
    } catch (e) { toast(e.message, { type: "error" }); }
  }));
}

function syncDupesToggle() {
  const cb = $q("#sec-aud-quality input[type=checkbox]");
  if (cb) cb.checked = S.skipDupes;
}
function viewDuplicates() {
  const dd = data.dedupe;
  if (!dd || !dd.dupes) { toast("No duplicate emails in this selection."); return; }
  const bodyRows = dd.groups.map((g) => {
    const comp = g.companies.length > 1 ? g.companies.map(esc).join(", ") + " (different companies!)"
      : (g.companies.length ? esc(g.companies[0]) : "—");
    return "<tr><td>" + esc(g.email) + "</td><td>" + g.count + "</td><td>" + comp + "</td><td>first in list</td></tr>";
  }).join("");
  const m = modal({
    title: fmtInt(dd.dupes) + " duplicate email" + (dd.dupes === 1 ? "" : "s") + (S.skipDupes ? " - will be skipped" : ""),
    sub: "Only the first row for each address goes out." + (dd.crossCompany ? " " + fmtInt(dd.crossCompany) + " address" + (dd.crossCompany === 1 ? " appears" : "es appear") + " under more than one company." : ""),
    body: el("div", { style: "max-height:340px;overflow:auto" },
      el("table", { class: "match-table" },
        el("tr", {}, el("th", {}, "Email"), el("th", {}, "Times"), el("th", {}, "Company"), el("th", {}, "Kept")),
        el("tbody", { html: bodyRows }))),
    foot: [el("button", { class: "btn btn-ghost" }, "Close")],
  });
  m.foot.querySelector(".btn-ghost").addEventListener("click", () => m.close());
}

function renderStepReview(panel) {
  const cols = el("div", { class: "send-cols" });
  const left = el("div", { style: "min-width:0" });
  const search = el("input", { class: "input recip-search", "aria-label": "Preview someone else", placeholder: "Preview someone else: name, company, email" });
  const hi = el("input", { type: "checkbox", "aria-label": "Highlight filled values" });
  hi.checked = S.fillHi;
  hi.addEventListener("change", () => {
    S.fillHi = hi.checked; touch();
    if (data.reviewFocus) paintFocus();
    else { const fr = $q(".preview-paper"); if (fr) drawPreview(fr); }
  });
  let sT;
  search.addEventListener("input", () => {
    clearTimeout(sT);
    sT = setTimeout(async () => {
      const q = search.value.trim().toLowerCase();
      if (!q) { clearFocus(); return; }
      try {
        const rows = await checkRows();
        const matches = rows.filter((r) => [r.company, r.hr_name, r.hr_email, r.serial]
          .some((v) => String(v || "").toLowerCase().includes(q)));
        if (!matches.length) {
          const cap = $q("#recip-caption");
          if (cap) cap.textContent = "No one matches \u2018" + search.value.trim() + "\u2019.";
          return;
        }
        data.reviewFocus = { kind: "search", list: matches, idx: 0, previews: [], loaded: false };
        paintFocus();
      } catch { /* keep the current preview */ }
    }, 250);
  });
  left.append(
    el("div", { class: "recip-switch" },
      el("b", { style: "font-size:13.5px" }, "What they'll get"),
      el("span", { class: "push" }),
      search,
      el("label", { class: "recip-hi" }, hi, "Highlight filled values"),
      el("button", { class: "btn btn-ghost btn-sm", id: "issue-chip", type: "button", style: "display:none" }, "Rows with issues"),
      el("button", { class: "btn btn-icon", "aria-label": "Previous person", onclick: () => stepRecipient(-1) }, icon("chevron-left", "i i-16")),
      el("button", { class: "btn btn-icon", "aria-label": "Next person", onclick: () => stepRecipient(1) }, icon("chevron-right", "i i-16"))),
    el("div", { class: "recip-caption", id: "recip-caption" }, "Loading previews…"),
    el("iframe", { class: "preview-paper", "aria-label": "Email preview", scrolling: "no" }));
  const tpl = data.templates.find((t) => t.id === S.templateId);
  const right = el("div", { style: "min-width:0" },
    el("div", { class: "review-tpl-chip" }, icon("file-text", "i i-14"),
      el("span", {}, "Template: "), el("b", {}, tpl ? tpl.name : "—"),
      el("button", { class: "linklike", type: "button", onclick: templateGallery }, "Change")),
    checksCard(), summaryCard());
  cols.append(left, right);
  panel.append(cols);
  (async () => {
    try {
      const [d] = await Promise.all([
        api("/api/merge/preview", { body: {
          template_id: S.templateId, mapping: S.mapping, sheet_id: S.listId,
          contact_ids: effContactIds(),
          limit: 8, append_signature: S.append_signature ? 1 : 0,
        } }),
        audienceRows(),
      ]);
      data.previews = d.previews || [];
      data.preview_total = d.total || 0;
      if (data.preview_idx >= data.previews.length) data.preview_idx = 0;
      const frame = $q(".preview-paper");
      if (frame) drawPreview(frame);
      const cap = $q("#recip-caption");
      if (cap) cap.textContent = data.previews.length
        ? "Person 1 of " + data.previews.length + " shown · " + fmtInt(data.preview_total) + " " + (data.preview_total === 1 ? "person" : "people") + " in total"
        : "No recipients in this selection.";
      await drawChecks();
      data.reviewIssues = issueRows(await checkRows());
      const chip = $q("#issue-chip");
      if (chip) {
        const n = data.reviewIssues.length;
        chip.style.display = n ? "" : "none";
        chip.textContent = "Rows with issues (" + fmtInt(n) + ")";
        chip.addEventListener("click", () => {
          if (!data.reviewIssues.length) return;
          const cur = data.reviewFocus && data.reviewFocus.kind === "issues" ? data.reviewFocus.idx : -1;
          data.reviewFocus = { kind: "issues", list: data.reviewIssues, idx: (cur + 1) % data.reviewIssues.length, previews: [], loaded: false };
          paintFocus();
        });
      }
      const sum = $q(".send-sticky-foot .summary");
      if (sum) sum.textContent = fmtInt(data.preview_total) + " " + (data.preview_total === 1 ? "person" : "people") +
        " · " + (data.templates.find((t) => t.id === S.templateId) || {}).name;
      const pb = $q(".send-sticky-foot .btn-primary");
      if (pb) {
        const n = data.preview_total;
        pb.disabled = !n;
        pb.title = n ? "" : "This selection has no recipients";
      }
    } catch (e) {
      const cap = $q("#recip-caption");
      if (cap) cap.textContent = e.message;
    }
  })();
}

/* ================= Outcomes + add-ons ================= */
const OUTCOMES = [
  { id: "draft", icon: "file-pen", title: "Save as drafts", tag: "Safest",
    desc: "Nothing is sent. Every email lands in your Gmail Drafts, so you can review or delete them first." },
  { id: "schedule", icon: "clock", title: "Schedule for later", tag: null,
    desc: "We send at the time you pick. You can reschedule or cancel any time before then." },
  { id: "send", icon: "send", title: "Send now", tag: null,
    desc: "Real emails go out immediately, one after another. This can't be recalled." },
  { id: "queue", icon: "inbox", title: "Keep in queue", tag: null,
    desc: "They wait in your email list. Send or schedule them whenever you're ready." },
];

function outcomeCards() {
  const stack = el("div", { class: "opt-stack" });
  if (data.safe) {
    stack.append(el("div", { class: "banner banner-warn", role: "status" },
      icon("shield", "i i-16"),
      el("span", {}, "Safe mode is on - immediate sending is blocked. "),
      el("button", { class: "linklike", onclick: () => liveSendingModal() }, "Turn on live sending")));
  }
  for (const o of OUTCOMES) {
    const disabled = o.id === "send" && data.safe;
    const pl = planDelivery({ outcome: o.id, n: effN(), cap: data.maxCap, sentToday: data.sentToday });
    const capLine = o.id === "send" ? sendCapSentence(pl) : o.id === "schedule" ? scheduleCapSentence(pl) : "";
    const b = el("button", {
      class: "opt-card" + (S.outcome === o.id ? " is-selected" : "") + (disabled ? " is-disabled" : ""),
      type: "button", disabled: disabled ? "" : null,
      title: disabled ? "Safe mode is on. Turn it off to send immediately." : "",
      onclick: () => { if (!disabled) { S.outcome = o.id; touch(); repaint("delivery", "review"); } },
    },
      el("span", { class: "opt-ico" }, icon(o.icon, "i i-18")),
      el("div", { style: "min-width:0;flex:1" },
        el("div", { class: "opt-title" }, el("span", {}, o.title), o.tag ? badge2(o.tag) : null),
        el("div", { class: "opt-desc" }, o.desc),
        capLine ? el("div", { class: "opt-desc cap-line" }, capLine) : null),
      el("span", { class: "opt-radio" }));
    stack.append(b);
  }
  return stack;
}

function liveSendingModal() {
  return new Promise((resolve) => {
    let done = false;
    const m = modal({
      title: "Turn on live sending?",
      sub: "You'll be able to send real emails immediately. Drafts and schedules work either way.",
      body: el("div", { class: "banner banner-warn" },
        icon("alert-triangle", "i i-16"),
        el("span", {}, "Live sending can't be recalled. Start with drafts until you trust the setup.")),
      foot: [el("button", { class: "btn btn-ghost" }, "Keep safe mode"), el("button", { class: "btn btn-danger" }, "Turn on live sending")],
      onClose: () => { if (!done) resolve(false); },
    });
    const yes = m.foot.querySelector(".btn-danger");
    m.foot.querySelector(".btn-ghost").addEventListener("click", () => { done = true; m.close(); resolve(false); });
    yes.addEventListener("click", () => withLoading(yes, async () => {
      try {
        await api("/api/settings", { body: { safe_mode: "0" } });
        data.safe = false;
        done = true; m.close();
        toast("Live sending is on.", { type: "success" });
        repaint("delivery", "review");
        resolve(true);
      } catch (e) { toast(e.message, { type: "error" }); done = true; m.close(); resolve(false); }
    }));
  });
}

async function pickWhen() {
  const count = Math.max(1, effN());
  try {
    const r = await openTimePicker({
      count,
      defaultStaggerMin: S.stagger || data.defaultStagger || 0,
      initial: S.when ? parseWhen(S.when) : null,
      dailyCap: data.maxCap,
    });
    S.when = r.when;
    S.stagger = r.staggerMin || 0;
    touch(); repaint("delivery", "review");
  } catch (e) {
    if (e && e.message !== "cancelled") toast(e.message, { type: "error" });
  }
}

function whenBlock() {
  return el("div", { class: "card", style: "padding:14px 16px;margin-top:12px" },
    el("div", { style: "display:flex;align-items:center;gap:12px;flex-wrap:wrap" },
      icon("clock", "i i-16"),
      el("span", { style: "font-weight:600" }, S.when ? fmtLong(S.when) : "Pick a time"),
      S.stagger ? el("span", { class: "t-caption t-faint" }, "spaced " + S.stagger + " min apart") : null,
      el("span", { class: "push" }),
      el("button", { class: "btn btn-ghost btn-sm", onclick: pickWhen }, S.when ? "Change" : "Choose time")));
}

const files = [];

function addOn(ico, title, stateFn, open, build) {
  const wrap = el("div", { class: "addon" + (open ? " open" : "") });
  const head = el("button", { class: "addon-head", type: "button" },
    icon(ico, "i i-16"),
    el("span", {}, title),
    el("span", { class: "addon-state" }, stateFn()),
    el("span", { class: "chev" }, icon("chevron-down", "i i-14")));
  head.addEventListener("click", () => wrap.classList.toggle("open"));
  const body = el("div", { class: "addon-body" });
  const commit = () => {
    touch();
    head.querySelector(".addon-state").textContent = stateFn();
    updateStatuses();
    const sc = $q("#summary-card");
    if (sc && isOpen("review")) sc.replaceWith(summaryCard());
    // keep the live preview truthful (D-18): signature/CC/attachments are
    // baked into the server-rendered preview, so refetch for open sections.
    if (isOpen("review")) {
      if (data.reviewFocus) { data.reviewFocus.loaded = false; paintFocus(); }
      else {
        api("/api/merge/preview", { body: {
          template_id: S.templateId, mapping: S.mapping, sheet_id: S.listId,
          contact_ids: effContactIds(), limit: 8, append_signature: S.append_signature ? 1 : 0,
        } }).then((d) => {
          data.previews = d.previews || [];
          data.preview_total = d.total || 0;
          if (data.preview_idx >= data.previews.length) data.preview_idx = 0;
          const fr = $q(".preview-paper");
          if (fr && !data.reviewFocus) drawPreview(fr);
        }).catch(() => {});
      }
    }
  };
  body.append(build(commit));
  wrap.append(head, body);
  return wrap;
}

function messageOptions() {
  const wrap = el("div", { class: "card", style: "padding:4px 16px" });
  wrap.append(
    addOn("pencil", "Signature", () => (S.append_signature ? "On" : "Off"), false, (commit) =>
      el("label", { style: "display:flex;gap:10px;align-items:center;font-size:13.5px;cursor:pointer" },
        el("input", { type: "checkbox", checked: S.append_signature ? "" : null,
          onchange: (e) => { S.append_signature = e.target.checked; commit(); } }),
        "Append your saved signature to every email")),
    addOn("mail", "CC", () => (S.cc.trim() ? S.cc.trim() : "None"), !!S.cc.trim(), (commit) => {
      const inp = el("input", { class: "input", style: "width:100%", value: S.cc, placeholder: "name@company.com, another@company.com", "aria-label": "CC addresses", "data-focus-key": "cc" });
      inp.addEventListener("change", () => { S.cc = inp.value; commit(); });
      return el("div", {}, inp, el("div", { class: "field-help" }, "Copied onto every email in this batch."));
    }),
    addOn("paperclip", "Attachments",
      () => (files.length ? fmtInt(files.length) + " file" + (files.length === 1 ? "" : "s")
            : S.att_meta.length ? fmtInt(S.att_meta.length) + " need re-selecting" : "None"),
      files.length > 0 || S.att_meta.length > 0, (commit) => {
        const fileIn = el("input", { type: "file", multiple: true, style: "display:none" });
        const add = el("button", { class: "btn btn-ghost btn-sm", onclick: () => fileIn.click() }, icon("upload", "i i-14"), " Add files");
        const list = el("div", { style: "display:flex;flex-direction:column;gap:6px;margin-top:10px" });
        const syncMeta = () => { S.att_meta = files.map((f) => ({ name: f.name, size: f.size })); };
        const kb = (n) => Math.max(1, Math.round((n || 0) / 1024)) + " KB";
        const paintFiles = () => {
          list.innerHTML = "";
          files.forEach((f, i) => {
            list.append(el("div", { style: "display:flex;align-items:center;gap:10px;font-size:13px" },
              icon("paperclip", "i i-14"),
              el("span", { style: "flex:1;min-width:0" }, f.name),
              el("span", { class: "t-caption t-faint" }, kb(f.size)),
              el("button", { class: "btn btn-icon", "aria-label": "Remove " + f.name,
                onclick: () => { files.splice(i, 1); syncMeta(); commit(); paintFiles(); paintNotice(); } }, icon("x", "i i-14"))));
          });
        };
        // 8.8: File objects do not survive a refresh - say what was picked and offer to re-pick.
        const notice = el("div", { style: "display:flex;gap:10px;align-items:center;margin-top:10px" });
        const paintNotice = () => {
          notice.innerHTML = "";
          if (files.length || !S.att_meta.length) { notice.style.display = "none"; return; }
          notice.style.display = "";
          notice.append(icon("alert-triangle", "i i-14"),
            el("span", { class: "t-caption", style: "flex:1" },
              fmtInt(S.att_meta.length) + " file" + (S.att_meta.length === 1 ? "" : "s") +
              " need re-selecting: " + S.att_meta.map((m) => m.name + " (" + kb(m.size) + ")").join(", ")),
            el("button", { class: "btn btn-ghost btn-sm", onclick: () => fileIn.click() }, "Re-select"),
            el("button", { class: "btn btn-ghost btn-sm", onclick: () => { S.att_meta = []; commit(); paintNotice(); } }, "Remove"));
        };
        paintFiles();
        paintNotice();
        fileIn.addEventListener("change", () => {
          const fs = [...(fileIn.files || [])];
          if (!fs.length) return;
          const total = files.reduce((a, f) => a + f.size, 0) + fs.reduce((a, f) => a + f.size, 0);
          if (total > 15 * 1024 * 1024) { toast("Attachments must stay under 15 MB in total.", { type: "error" }); return; }
          files.push(...fs);
          syncMeta();
          commit();
          paintFiles();
          paintNotice();
        });
        return el("div", {}, add, list, notice,
          el("div", { class: "field-help" }, "Sent with every email in this batch. Re-select the files if you refresh the page."));
      }));
  return wrap;
}

function followUpWrap() {
  const wrap = el("div", { class: "card", style: "padding:4px 16px" });
  const rtpls = data.templates.filter((t) => (t.kind || "outreach") === "reminder");
  wrap.append(addOn("bell", "Follow-up reminders", () => (S.reminder_days > 0 ? "Every " + S.reminder_days + "d" : "Off"), S.reminder_days > 0, (commit) => {
    const daysIn = el("input", { class: "input", type: "number", min: "1", max: "90", value: String(S.reminder_days || 3), style: "width:70px", "data-focus-key": "reminder-days" });
    const maxSel = el("select", { class: "select", style: "max-width:130px" },
      [1, 2, 3].map((n) => el("option", { value: String(n), selected: S.reminder_max === n ? "" : null }, String(n) + "\u00d7")));
    maxSel.addEventListener("change", () => { S.reminder_max = +maxSel.value; commit(); });
    const tplSel = el("select", { class: "select", style: "max-width:300px" },
      el("option", { value: "0" }, "No reminder template"),
      ...rtpls.map((t) => el("option", { value: String(t.id), selected: String(S.reminder_template_id) === String(t.id) ? "" : null }, t.name)));
    tplSel.addEventListener("change", () => { S.reminder_template_id = +tplSel.value; commit(); });
    const on = S.reminder_days > 0;
    return el("div", { style: "display:flex;flex-direction:column;gap:10px" },
      el("label", { style: "display:flex;gap:10px;align-items:center;font-size:13.5px;cursor:pointer" },
        el("input", { type: "checkbox", checked: on ? "" : null,
          onchange: (e) => {
            S.reminder_days = e.target.checked ? (Math.max(1, Math.min(90, +daysIn.value || 3)) || 3) : 0;
            commit();
          } }),
        "Follow up with people who don't reply"),
      el("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap" },
        el("span", { class: "t-caption t-faint" }, "Every"), daysIn,
        el("span", { class: "t-caption t-faint" }, "day(s),"), maxSel),
      el("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap" },
        el("span", { class: "t-caption t-faint" }, "Using"), tplSel));
  }));
  return wrap;
}


function summaryCard() {
  const camp = data.campaigns.find((c) => c.id === S.campaignId);
  const tpl = data.templates.find((t) => t.id === S.templateId);
  const n = data.preview_total || nPeople();
  const L = (ic, label, val, step) => el("div", { class: "summary-line" }, icon(ic, "i i-16"),
    el("span", { style: "flex:1;color:var(--text-2)" }, label),
    el("b", {}, val),
    el("button", { class: "linklike summary-change", onclick: () => changeLink(step) }, "Change"));
  const outcomeLabel = {
    draft: "Saved as Gmail drafts - nothing is sent",
    schedule: S.when ? "Scheduled for " + fmtLong(S.when) + (S.stagger ? " \u00b7 spaced " + S.stagger + " min apart" : "") : "Scheduled (pick a time first)",
    send: "Sent immediately, one after another",
    queue: "Kept in your email queue",
  }[S.outcome];
  return el("div", { class: "summary-card", id: "summary-card", style: "margin-top:14px" },
    el("h3", {}, "Summary"),
    L("users", "To", (S.contactIds.length ? fmtInt(S.contactIds.length) + " of " : "") + data.listName + (camp ? " \u00b7 " + camp.name : ""), "audience"),
    L("file-text", "Template", tpl ? tpl.name : "\u2014", "message"),
    L("send", "From", (data.accounts.find((a) => a.id === S.account) || {}).email || "No account picked", "delivery"),
    S.append_signature ? L("pencil", "Signature", "appended", "message") : null,
    S.cc.trim() ? L("mail", "CC", S.cc.trim(), "message") : null,
    (S.skipDupes && data.dedupe && data.dedupe.dupes) ? L("copy", "Skipped", fmtInt(data.dedupe.dupes) + " duplicates (keep first)", "audience") : null,
    S.reminder_days > 0 ? L("bell", "Follow-up", "every " + S.reminder_days + " day(s), up to " + S.reminder_max + "\u00d7", "delivery") : null,
    files.length ? L("paperclip", "Attachments", fmtInt(files.length) + " file" + (files.length === 1 ? "" : "s"), "message") : null,
    el("div", { class: "summary-outcome" }, outcomeLabel,
      el("button", { class: "linklike summary-change", style: "margin-left:8px", onclick: () => changeLink("delivery") }, "Change")));
}

/* ================= Final CTA + create ================= */
function accountEmail() {
  return (data.accounts.find((a) => a.id === S.account) || {}).email || "";
}

function primary3() {
  const n = effN();
  const pl = planNow();
  const label = {
    draft: "Create " + fmtInt(n) + " drafts",
    schedule: "Schedule " + fmtInt(n) + " " + (n === 1 ? "email" : "emails"),
    send: "Send " + fmtInt(pl.today) + " " + (pl.today === 1 ? "email" : "emails") + " now",
    queue: "Queue " + fmtInt(n) + " " + (n === 1 ? "email" : "emails"),
  }[S.outcome];
  const b = el("button", { class: "btn btn-primary" }, label);
  b.addEventListener("click", onPrimary3);
  return b;
}

/* Send-now confirmation (8.4): states the real numbers and only the
   warnings that apply. 8.9 (decision #9): 100+ gets a typed alternative,
   500+ must type the number. */
function confirmSendNow() {
  return new Promise((resolve) => {
    const n = effN();
    const pl = planNow();
    const today = Math.min(pl.today, n);
    const acct = accountEmail();
    const tpl = (data.templates.find((t) => t.id === S.templateId) || {}).name || "";
    const dd = data.dedupe;
    let done = false;
    const warns = [];
    if (pl.capped) warns.push(sendCapSentence(pl));
    if (dd && dd.dupes && !S.skipDupes) warns.push(fmtInt(dd.dupes) + " duplicate emails will each get an email.");
    if (n > (data.previews.length || 0)) warns.push("The preview covered " + (data.previews.length || 8) + " of " + fmtInt(n) + " - the rest follow the same template.");
    const finish = (v) => { if (done) return; done = true; m.close(); resolve(v); };
    const m = modal({
      title: "Send " + fmtInt(today) + " real emails now?",
      sub: "From " + (acct || "your account") + " to " +
        (S.contactIds.length ? fmtInt(S.contactIds.length) + " of " : "") + data.listName +
        (tpl ? " with \u2018" + tpl + "\u2019" : "") +
        ". They go out one after another and can't be recalled.",
      body: el("div", {},
        el("div", { class: "banner banner-warn" }, icon("alert-triangle", "i i-16"),
          el("span", {}, "If this is the first time, 'Send me a test' is the safe move.")),
        ...warns.map((w) => el("div", { class: "field-help", style: "margin-top:10px" }, w))),
      foot: [el("button", { class: "btn btn-ghost" }, "Cancel")],
      onClose: () => finish(false),
    });
    m.foot.querySelector(".btn-ghost").addEventListener("click", () => finish(false));
    if (today >= 500) {
      const input = el("input", { class: "input", type: "text", inputmode: "numeric",
        style: "margin-top:14px;max-width:220px", placeholder: fmtInt(today),
        "aria-label": "Type the number of emails to confirm" });
      const ok = el("button", { class: "btn btn-danger", style: "margin-top:10px", disabled: true },
        "Send " + fmtInt(today) + " emails");
      m.body.append(el("div", { class: "field-help", style: "margin-top:14px" },
        "This is a big send - type the number to confirm."), input, ok);
      input.addEventListener("input", () => { ok.disabled = input.value.replace(/[,\s]/g, "") !== String(today); });
      ok.addEventListener("click", () => finish(true));
      input.focus();
    } else {
      const hold = el("button", { class: "btn btn-danger" }, "Hold to send " + fmtInt(today));
      m.body.append(el("div", { class: "field-help", style: "margin-top:10px" }, "Hold the button for 2 seconds to confirm."));
      m.foot.append(hold);
      holdToConfirm(hold, { duration: 2000, label: "Hold to send " + fmtInt(today), onConfirm: () => finish(true) });
      if (today >= 100) {
        const alt = el("button", { class: "linklike", style: "display:block;margin-top:10px" }, "Type the number to confirm instead");
        m.body.append(alt);
        alt.addEventListener("click", () => confirmTyped({
          title: "Send " + fmtInt(today) + " real emails now?",
          body: "Type the number to confirm.",
          expect: today, actionLabel: "Send " + fmtInt(today) + " emails",
        }).then((v) => finish(v)));
      }
    }
  });
}

function confirmSchedule() {
  return new Promise((resolve) => {
    const n = data.preview_total || nPeople();
    const when = S.when ? fmtLong(S.when) : "";
    let done = false;
    const m = modal({
      title: "Schedule " + fmtInt(n) + " " + (n === 1 ? "email" : "emails") + "?",
      sub: when + (S.stagger ? " · spaced " + S.stagger + " minutes apart" : "") +
        ". You can reschedule or cancel until then.",
      foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), el("button", { class: "btn btn-primary" }, "Schedule " + fmtInt(n) + " " + (n === 1 ? "email" : "emails"))],
      onClose: () => { if (!done) resolve(false); },
    });
    const ok = m.foot.querySelector(".btn-primary");
    m.foot.querySelector(".btn-ghost").addEventListener("click", () => { done = true; m.close(); resolve(false); });
    ok.addEventListener("click", () => { done = true; m.close(); resolve(true); });
  });
}

async function onPrimary3() {
  if (!S.account) { toast("Pick a send account first.", { type: "error" }); return; }
  if (S.outcome === "schedule" && !S.when) { toast("Pick a time first.", { type: "error" }); return; }
  if (S.outcome === "send") { if (await confirmSendNow()) startCreate(); return; }
  if (S.outcome === "schedule") { if (await confirmSchedule()) startCreate(); return; }
  startCreate();
}

function genBody(action) {
  const camp = data.campaigns.find((x) => x.id === S.campaignId);
  const d = new Date();
  const day = d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  return {
    name: (camp ? camp.name + " · " : "") + data.listName +
      (S.contactIds.length ? " (" + fmtInt(S.contactIds.length) + " chosen)" : "") + " " + day,
    template_id: S.templateId,
    mapping: S.mapping,
    campaign_id: S.campaignId,
    sheet_id: S.listId,
    contact_ids: effContactIds(),
    account_id: S.account,
    action,
    at: S.when,
    stagger: S.stagger,
    cc: S.cc,
    append_signature: S.append_signature ? 1 : 0,
    hold: action === "send" ? true : undefined,
  };
}

async function startCreate() {
  try {
    const prog = progressPanel("Creating your send…");
    root.innerHTML = "";
    root.append(prog.host);
    let result, batchId;
    if (S.outcome === "queue" || !files.length) {
      result = await api("/api/merge/generate", { body: genBody(S.outcome) });
      batchId = result.batch_id;
      prog.done(prog.add("Creating " + fmtInt(effN()) + " emails…"),
        "Batch created - " + fmtInt(result.created) + " " + (result.created === 1 ? "email" : "emails") +
        (S.outcome === "queue" ? " queued" : " " + outcomeDoneText(result)));
    } else {
      result = await api("/api/merge/generate", { body: genBody("create") });
      batchId = result.batch_id;
      prog.done(prog.add("Creating " + fmtInt(nPeople()) + " emails…"), "Batch created");
      const it2 = prog.add("Attaching " + files.length + " file" + (files.length === 1 ? "" : "s") + "…");
      const fd = new FormData();
      files.forEach((f) => fd.append("files", f));
      try {
        await api("/api/batches/" + batchId + "/attachments", { method: "POST", body: fd });
        prog.done(it2, "Attachments added");
      } catch (e) { prog.fail(it2, "Attachment upload failed: " + e.message); }
      result = await api("/api/batches/" + batchId + "/send-as", { body: {
        action: S.outcome, at: S.when, stagger: S.stagger, account_id: S.account,
        hold: S.outcome === "send" ? true : undefined,
      } });
      prog.done(prog.add("Finishing…"), outcomeDoneText(result));
    }
    if (S.reminder_days > 0) {
      const it4 = prog.add("Setting up follow-up reminders…");
      try {
        await api("/api/batches/" + batchId + "/reminders", { body: {
          days: S.reminder_days, max: S.reminder_max, template_id: S.reminder_template_id,
        } });
        prog.done(it4, "Follow-ups on: every " + S.reminder_days + " day(s), up to " + S.reminder_max + "×");
      } catch (e) { prog.fail(it4, "Reminders not set: " + e.message); }
    }
    files.length = 0;
    if (S.outcome === "send" && result.holding) {
      await heldCountdown(batchId, result);
      return;
    }
    successScreen(result);
  } catch (e) {
    // D-37: never blind-retry a send - the request is one long synchronous
    // batch, so some emails may already be out.
    root.innerHTML = "";
    const canRetry = S.outcome !== "send";
    root.append(
      errorCard(canRetry ? "Couldn't finish. " + e.message : "Something went wrong while sending.",
        { onRetry: canRetry ? startCreate : null, detail: e.message }),
      canRetry ? null : el("div", { style: "margin-top:12px;font-size:13px;color:var(--text-2);max-width:560px" },
        el("span", {}, "Sending is one long request, so some emails may have gone out before the problem. Check the campaign to see exactly what happened before trying again.")),
      el("div", { style: "margin-top:12px" },
        el("button", { class: "btn btn-ghost", onclick: () => location.href = "/campaigns" }, "Back to campaigns")));
  }
}

/* 8.3: after a held send, show the shared undo countdown. Cancelling calls
   the idempotent batch undo endpoint (B2). */
async function heldCountdown(batchId, result) {
  const heldN = result.scheduled != null ? result.scheduled : effN();
  const cm = modal({
    title: "You can still change your mind",
    sub: "Sending " + fmtInt(heldN) + (heldN === 1 ? " email" : " emails") + " from " + (accountEmail() || "your account") + ".",
    body: el("div", {}),
  });
  const r = await startSendCountdown(cm, { n: heldN, releaseAt: result.release_at });
  cm.close();
  if (r.cancelled) {
    let undone = 0;
    try {
      const u = await api("/api/batches/" + batchId + "/undo", { body: {} });
      undone = u.cancelled || 0;
    } catch { /* window already over - report from the campaign */ }
    heldCancelledScreen(undone);
    return;
  }
  heldSuccessScreen(result);
}
function successIllustration() {
  return el("div", { class: "empty-illust" }, `<svg viewBox="0 0 96 96" width="96" height="96" aria-hidden="true"><circle cx="48" cy="48" r="26" fill="var(--success-bg)" stroke="none"/><path d="M36 49l9 9 17-19" fill="none" stroke="var(--success)" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`);
}
function heldSuccessScreen(result) {
  try { localStorage.removeItem(K()); } catch { /* private mode */ }
  const heldN = result.scheduled != null ? result.scheduled : effN();
  const whenTxt = result.release_at ? fmtLong(result.release_at) : "momentarily";
  const cid = S.campaignId || 0;
  root.innerHTML = "";
  root.append(el("div", { class: "send-success" },
    successIllustration(),
    el("div", { class: "ss-title" }, fmtInt(heldN) + (heldN === 1 ? " email will go out" : " emails will go out") + " " + whenTxt),
    el("p", { class: "ss-sub" }, "From " + (accountEmail() || "your account") + ", one after another. You can cancel from the Emails tab until then - once sent, they can't be recalled."),
    el("div", { class: "ss-actions" },
      el("a", { class: "btn btn-primary", href: cid ? "/campaigns/" + cid + "?tab=emails" : "/campaigns" }, "Watch them go (Emails tab)"),
      el("button", { class: "btn btn-ghost", onclick: () => location.href = "/campaigns" }, "Back to campaigns"))));
}
function heldCancelledScreen(undone) {
  try { localStorage.removeItem(K()); } catch { /* private mode */ }
  const n = effN();
  root.innerHTML = "";
  root.append(el("div", { class: "send-success" },
    successIllustration(),
    el("div", { class: "ss-title" }, undone ? "Send cancelled - nothing went out" : "The undo window was already over"),
    el("p", { class: "ss-sub" }, undone
      ? fmtInt(undone) + (undone === 1 ? " email went" : " emails went") + " back to your queue (Ready). Schedule or send them when you're ready."
      : "Check the campaign to see exactly what was sent."),
    el("div", { class: "ss-actions" },
      el("a", { class: "btn btn-primary", href: (S.campaignId || 0) ? "/campaigns/" + S.campaignId + "?tab=emails" : "/campaigns" }, "Open the Emails tab"),
      el("button", { class: "btn btn-ghost", onclick: () => location.href = "/campaigns" }, "Back to campaigns"))));
}

function progressPanel(title) {
  const host = el("div", { class: "send-success" },
    el("div", { class: "ss-title" }, title),
    el("div", { id: "prog-lines", style: "display:flex;flex-direction:column;gap:6px;margin-top:10px" }));
  const lines = $q("#prog-lines", host);
  return {
    host,
    add: (t) => {
      const l = el("div", { class: "check-line info" }, icon("loader", "i i-16 spin"), el("span", {}, t));
      lines.append(l);
      return l;
    },
    done: (l, t) => { l.innerHTML = ""; l.append(icon("check", "i i-16"), el("span", {}, t)); l.className = "check-line ok"; },
    fail: (l, t) => { l.innerHTML = ""; l.append(icon("alert-triangle", "i i-16"), el("span", {}, t)); l.className = "check-line warn"; },
  };
}

function outcomeDoneText(result) {
  if (result.holding) return fmtInt(result.scheduled) + " held for the undo window";
  if (result.created_drafts != null) return fmtInt(result.created_drafts) + " draft" + (result.created_drafts === 1 ? "" : "s") + " created";
  if (result.scheduled != null) return fmtInt(result.scheduled) + " scheduled";
  if (result.sent != null) return fmtInt(result.sent) + " sent";
  return "done";
}

function successScreen(res) {
  try { localStorage.removeItem(K()); } catch { /* private mode */ }
  const n = data.preview_total || nPeople();
  const failed = res.failed || 0;
  let title, sub;
  if (S.outcome === "draft") {
    title = (res.created_drafts != null ? fmtInt(res.created_drafts) : fmtInt(n)) + " draft" + ((res.created_drafts ?? n) === 1 ? "" : "s") + " in your Gmail";
    sub = "Open your Drafts folder to review them - delete what you don't want, edit, or send from there.";
  } else if (S.outcome === "schedule") {
    title = (res.scheduled != null ? fmtInt(res.scheduled) : fmtInt(n)) + " " + ((res.scheduled ?? n) === 1 ? "email is" : "emails are") + " scheduled";
    sub = "Goes out " + (S.when ? fmtLong(S.when) : "at the chosen time") +
      (S.stagger ? ", spaced " + S.stagger + " minutes apart" : "") + ". You can reschedule or cancel until then.";
  } else if (S.outcome === "send") {
    const sent = res.sent != null ? res.sent : n;
    title = fmtInt(sent) + " " + (sent === 1 ? "email is" : "emails are") + " on their way";
    sub = "Sent from " + accountEmail() + ". Replies will show up in your Replies tab." +
      (res.capped ? " The other " + fmtInt(Math.max(0, n - sent)) + " stayed in your queue (Ready) - schedule them or send them tomorrow." : "");
  } else {
    title = fmtInt(res.created != null ? res.created : n) + " " + ((res.created ?? n) === 1 ? "email is" : "emails are") + " in your queue";
    sub = "They're waiting in the campaign's email list. Send or schedule them whenever you're ready.";
  }
  if (S.skipDupes && data.dedupe && data.dedupe.dupes)
    sub += " Skipped " + fmtInt(data.dedupe.dupes) + " duplicate email" + (data.dedupe.dupes === 1 ? "" : "s") + " (keep first).";
  const cid = S.campaignId || 0;
  root.innerHTML = "";
  root.append(el("div", { class: "send-success" },
    successIllustration(),
    el("div", { class: "ss-title" }, title),
    el("p", { class: "ss-sub" }, sub),
    failed ? el("div", { class: "banner banner-warn", style: "text-align:left;max-width:560px" },
      icon("alert-triangle", "i i-16"),
      el("span", {}, failed + " " + (failed === 1 ? "email" : "emails") + " failed - open the campaign to see why and retry.")) : null,
    el("div", { class: "ss-actions" },
      el("a", { class: "btn btn-primary", href: cid ? "/campaigns/" + cid : "/campaigns" }, "View campaign"),
      el("button", { class: "btn btn-ghost", onclick: () => location.href = "/campaigns" }, "Back to campaigns"))));
}

/* ---------------- init ---------------- */
/* Spec 14.7: safe mode and the daily cap can change elsewhere (Settings,
   top bar). Re-evaluate the delivery cards instead of waiting for a refresh. */
document.addEventListener("cm:state", async () => {
  if (data.last_error || !root.querySelector(".send-sticky-foot")) return;
  try {
    const st = await api("/api/state");
    readState(st);
    if (isOpen("delivery")) paintSection("delivery");
    updateStatuses();
  } catch { /* keep last known state */ }
});

load();
