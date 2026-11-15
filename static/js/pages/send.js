/* Send flow — one screen: who, what, from, when. Then a check step.
   Glossary: send/list/spacing/follow-up. Sentence case, no exclamation marks. */
import {
  api, el, icon, toast, modal, holdToConfirm, confirmSimple,
  emptyState, errorCard, withLoading, esc, appendSafe,
} from "/static/js/core/ui.js";
import { fmtInt, fmtLong, fmtDayTime, toBackend, parseWhen, localToUrl } from "/static/js/core/format.js";
import { openTimePicker } from "/static/js/components/timepicker.js";
import { planDelivery, sendCapSentence, scheduleCapSentence } from "/static/js/core/send-plan.js";

const START_CID = window.send_campaign_id || 0;
const root = document.getElementById("send-root");

/* ---------------- state ---------------- */
const S = {
  step: 1,
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
function restore() {
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
  last_error: "",
  meEmail: "", defaultStagger: 0, maxCap: 0, sentToday: 0,
};

const $q = (sel, r = root) => r.querySelector(sel);
const nPeople = () => (S.contactIds.length || data.listCount || 0);
const planNow = () => planDelivery({ outcome: S.outcome, n: data.preview_total || nPeople(), cap: data.maxCap, sentToday: data.sentToday });

/* ---------------- load ---------------- */
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
  const saved = restore();
  if (saved) { Object.assign(S, saved); S.touched = true; }
  if (S.campaignId) {
    const c = data.campaigns.find((x) => x.id === S.campaignId);
    if (!c) S.campaignId = START_CID ? 0 : S.campaignId;
  }
  if (!S.campaignId && data.campaigns.length === 1) S.campaignId = data.campaigns[0].id;
  if (!S.templateId && data.templates.length) {
    const def = data.templates.find((t) => t.is_active) || data.templates[0];
    S.templateId = def.id;
  }
  if (!S.account && data.accounts.length) S.account = data.last_account_id || data.accounts[0].id;
  await selectList(S.listId, { keep: true });
  // Always resolve field mapping on load (the "no fields mapped -> 2 mapped"
  // mismatch came from skipping this on first visit).
  await autoMatch();
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

/* Rows of the current audience (dicts), for checks + picker. */
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
  root.append(header(), el("div", { class: "send-wrap" + (S.step === 2 ? " two-col" : ""), id: "send-panel" }));
  if (S.step === 1) renderStep1();
  else renderStep2();
  syncUrl();
  save();
}
function header() {
  const stepNames = ["What", "Check"];
  const stepper = el("nav", { class: "send-stepper", "aria-label": "Send steps" },
    stepNames.map((n, i) => {
      const n1 = i + 1;
      const done = n1 < S.step, cur = n1 === S.step;
      const future = !done && !cur;
      const b = el("button", {
        class: "send-step" + (cur ? " is-current" : "") + (done ? " is-done" : ""),
        type: "button",
        "aria-current": cur ? "step" : null,
        "aria-disabled": future ? "true" : null,
        title: future ? "Finish this step first" : null,
        onclick: () => { if (done) { S.step = n1; render(); } },
      },
        el("span", { class: "send-step-n" }, done ? icon("check", "i i-14") : String(n1)), n);
      return b;
    }));
  return el("div", { class: "page-head send-head" },
    el("div", { class: "ph-main" },
      el("div", { class: "send-step-title" }, S.step === 1 ? "What are you sending?" : "Check the emails"),
      el("p", { class: "send-step-sub" },
        S.step === 1 ? "Pick who to email, what to say, and what happens next."
          : "This is exactly what each person will get.")),
    el("div", { class: "push", style: "display:flex;gap:12px;align-items:center;flex:none" }, stepper,
      el("button", { class: "btn btn-icon", "aria-label": "Close", onclick: closeFlow }, icon("x", "i i-16"))));
}
function closeFlow() {
  history.length > 2 ? history.back() : (location.href = S.campaignId ? "/campaigns/" + S.campaignId : "/campaigns");
}
function footer(left, summary, primary, primaryDisabled, reason) {
  const bar = el("div", { class: "send-sticky-foot" }, left,
    el("div", { class: "summary", title: reason || "" }, summary),
    primary);
  if (primaryDisabled) { primary.disabled = true; if (reason) primary.title = reason; }
  return bar;
}
function syncUrl() {
  const url = new URL(location.href);
  url.searchParams.set("step", String(S.step));
  history.replaceState(null, "", url);
}
window.addEventListener("popstate", () => {
  const p = Number(new URLSearchParams(location.search).get("step") || 1);
  if (p < S.step && p >= 1) { S.step = p; render(); }
});
document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.tagName === "SELECT" || t.isContentEditable)) return;
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
    const btn = $q(".send-sticky-foot .btn-primary");
    if (btn && !btn.disabled) { e.preventDefault(); btn.click(); }
  }
});

/* ================= Step 1: What (one screen) ================= */
function renderStep1() {
  const panel = $q("#send-panel");
  const c = data.campaigns.find((x) => x.id === S.campaignId);
  if (S.campaignId && !c) {
    panel.append(el("div", { class: "banner banner-danger", role: "alert" },
      icon("alert-triangle", "i i-16"),
      el("span", {}, "This campaign no longer exists. Pick another one."),
      el("button", { class: "btn btn-ghost btn-sm push", onclick: () => { S.campaignId = 0; render(); } }, "Choose")));
    return;
  }
  appendSafe(panel,
    S.campaignId
      ? el("div", { class: "send-cap" }, "Campaign: ", el("b", {}, c.name))
      : campaignPick(),
    el("div", { class: "overline-label" }, "Who to email"),
    listCards(),
    contactTools(),
    el("div", { class: "overline-label" }, "What to say"),
    templateCards(),
    matchReassurance(),
    el("div", { class: "overline-label" }, "Send from"),
    accountSelect(),
    el("div", { class: "overline-label" }, "What happens next"),
    outcomeCards(),
    S.outcome === "schedule" ? whenBlock() : null,
    el("div", { class: "overline-label" }, "Extras"),
    addOns(),
    footer(
      el("span"),
      footerSummary1(),
      el("button", { class: "btn btn-primary", onclick: () => { S.step = 2; render(); } }, "Check the " + fmtInt(nPeople()) + " emails"),
      !nPeople() || !S.templateId || matchBlocking() || (S.outcome === "schedule" && !S.when),
      matchBlocking() ? "Resolve the highlighted fields first"
        : !S.templateId ? "Pick a template to continue"
        : (S.outcome === "schedule" && !S.when) ? "Pick a time to schedule"
        : !nPeople() ? "Pick some contacts to continue" : ""));
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
function footerSummary1() {
  const n = nPeople();
  const c = data.campaigns.find((x) => x.id === S.campaignId);
  const who = (S.contactIds.length ? fmtInt(S.contactIds.length) + " of " : "") +
    fmtInt(data.listCount) + " in " + data.listName;
  return n + " " + (n === 1 ? "person" : "people") + " · " + who +
    (c ? " · " + c.name : "") + (S.templateId ? " · " + (data.templates.find((t) => t.id === S.templateId) || {}).name : "");
}

/* ---- list (who) ---- */
function listCards() {
  const stack = el("div", { class: "opt-stack" });
  const mk = (id, name, count, headers, note) => {
    const b = el("button", {
      class: "opt-card" + (S.listId === id ? " is-selected" : ""), type: "button",
      onclick: () => { S.listId = id; S.contactIds = []; touch(); selectList(id).then(() => { autoMatch(); render(); }); },
    },
      el("span", { class: "opt-ico" }, icon("users", "i i-18")),
      el("div", { style: "min-width:0;flex:1" },
        el("div", { class: "opt-title" }, el("span", {}, name),
          el("span", { class: "opt-count" }, fmtInt(count) + " " + (count === 1 ? "contact" : "contacts"))),
        headers && headers.length
          ? el("div", { class: "opt-cols" }, ...headers.slice(0, 6).map((h) => el("code", { class: "mono-chip" }, h)))
          : el("div", { class: "opt-desc" }, note || "Every contact in your main list.")),
      el("span", { class: "opt-radio" }));
    return b;
  };
  for (const s of data.sheets) {
    const camp = data.campaigns.find((x) => x.id === s.campaign_id);
    stack.append(mk(s.id, s.name, s.n_rows, s.headers || [], camp ? "List for " + camp.name : "Uploaded list"));
  }
  stack.append(mk(0, "All contacts", data.n_contacts, ["company", "hr_name", "hr_email"]));
  return stack;
}

/* Choose contacts / paste a table */
function contactTools() {
  const wrap = el("div", { class: "ct-tools" });
  const pickBtn = el("button", { class: "btn btn-secondary btn-sm" }, icon("check-square", "i i-14"),
    S.contactIds.length ? "Chosen: " + fmtInt(S.contactIds.length) + " · change" : "Choose who to email");
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
        render();
      } catch (e) { toast(e.message, { type: "error" }); }
    });
  });
  wrap.append(pickBtn, pasteBtn, drop);
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
        countLbl.textContent = fmtInt(state.sel.size) + " of " + fmt(total) + " chosen";
      });
      list.append(el("label", { class: "pick-row" },
        cb,
        el("span", { class: "pick-no" }, r.serial || "—"),
        el("span", { class: "pick-co" }, r.company || ""),
        el("span", { class: "pick-who" }, [r.hr_name, r.hr_email].filter(Boolean).join(" · ")),
      ));
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
    sub: data.listName + " · " + fmtInt(total) + " contacts. Leave nothing selected to email everyone.",
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
    render();
  });
  paint();
}

/* ---- paste a table ---- */
function pasteTableModal() {
  const name = el("input", { class: "input", placeholder: "List name (optional)", "aria-label": "List name" });
  const ta = el("textarea", { class: "textarea", rows: "9", style: "width:100%", "aria-label": "Pasted table",
    placeholder: "Paste straight from Excel or Google Sheets - the first row is the column names.\n\nCompany\tRecruiter name\tHR email\nAccolite\tGenevieve Dsouza\tgen.dsouza@accoliteindia.com" });
  const btn = el("button", { class: "btn btn-primary" }, "Add this list");
  const camp = data.campaigns.find((x) => x.id === S.campaignId);
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

/* ---- template (what) ---- */
function templateCards() {
  const stack = el("div", { class: "opt-stack" });
  if (!data.templates.length) {
    stack.append(el("div", { class: "banner banner-info" }, icon("info", "i i-16"),
      el("span", {}, "No templates yet. Write one with AI or upload an HTML file.")));
  }
  for (const t of data.templates) {
    const fields = Array.isArray(t.fields) ? t.fields : [];
    const b = el("button", {
      class: "opt-card tpl-card" + (S.templateId === t.id ? " is-selected" : ""), type: "button",
      onclick: () => { S.templateId = t.id; touch(); autoMatch().then(render); },
    },
      el("div", { class: "tpl-thumb", "data-tid": String(t.id) },
        el("iframe", { class: "tpl-thumb-frame", srcdoc: thumbDoc(t), "aria-hidden": "true", scrolling: "no" })),
      el("div", { style: "min-width:0;flex:1" },
        el("div", { class: "opt-title" }, el("span", {}, t.name),
          t.is_active ? badge2("Default") : null,
          t.kind === "reminder" ? badge2("Follow-up") : null),
        el("div", { class: "opt-desc" }, t.subject || "No subject"),
        fields.length ? el("div", { class: "opt-desc" }, fields.length + " " + (fields.length === 1 ? "field" : "fields") + " to fill in") : el("div", { class: "opt-desc" }, "No fill-in fields - every email is identical")),
      el("a", { class: "btn btn-ghost btn-sm tpl-edit", href: "/library/templates/" + t.id, "aria-label": "Edit template",
        onclick: (e) => e.stopPropagation() }, icon("pencil", "i i-14"), "Edit"),
      el("span", { class: "opt-radio" }));
    stack.append(b);
  }
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
  return stack;
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

/* ---- matching ---- */
function matchBlocking() {
  if (!data.fields.length) return false;
  return data.fields.some((f) => !S.mapping[f]);
}
function matchReassurance() {
  if (!data.fields.length) {
    return el("div", { class: "match-note info" }, icon("info", "i i-16"), "This template has no fill-in fields, so every email is identical.");
  }
  const unmatched = data.fields.filter((f) => !S.mapping[f]);
  const wrap = el("div", { class: "match-note " + (unmatched.length ? "warn" : "ok") });
  if (unmatched.length === 0) {
    wrap.append(icon("check-circle-2", "i i-16"),
      el("span", {}, "We matched all " + data.fields.length + " fields automatically."));
    const d = el("details", { class: "match-disc" },
      el("summary", {}, "See how"), el("div", { id: "match-panel" }));
    d.addEventListener("toggle", () => { if (d.open) buildMatchPanel($q("#match-panel")); });
    wrap.append(d);
  } else {
    wrap.append(icon("alert-triangle", "i i-16"),
      el("span", {}, unmatched.length + " " + (unmatched.length === 1 ? "field needs" : "fields need") + " your help."),
      el("button", { class: "linklike", onclick: () => { S.matchOpen = !S.matchOpen; touch(); render(); } }, S.matchOpen ? "Hide" : "Fix now"));
    if (S.matchOpen) {
      const host = el("div", { id: "match-panel", style: "margin-top:10px" });
      buildMatchPanel(host);
      wrap.append(host);
    }
  }
  return wrap;
}
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
    const textIn = el("input", { class: "input", style: "max-width:260px;display:none", placeholder: "Fixed text, e.g. Campus Placement Cell" });
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
      setTimeout(() => render(), 350);
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
function sampleFor(col, field) {
  if (!data.sheetRows) return "";
  const r = data.sheetRows.find((r) => r[col]);
  return r ? ", e.g. " + String(r[col]).slice(0, 24) : "";
}

/* ---- send from ---- */
function accountSelect() {
  const sel = el("select", { class: "select", style: "max-width:440px", "aria-label": "Send from" },
    ...data.accounts.map((a) => el("option", { value: String(a.id) }, a.email || a.label)));
  if (!S.account && data.accounts.length) S.account = data.last_account_id || data.accounts[0].id;
  sel.value = String(S.account || "");
  sel.addEventListener("change", () => { S.account = +sel.value; touch(); render(); });
  return el("div", {}, sel,
    el("div", { class: "field-help" }, data.accounts.length
      ? "The Gmail account that sends. Add more in Settings."
      : "No connected account - connect one in Settings first."));
}

/* ================= Step 2: Check ================= */
const previewHtmlEsc = (x) => String(x ?? "").replace(/&/g, "&").replace(/</g, "<").replace(/>/g, ">");

function previewDoc(p, fills) {
  const fillsJson = JSON.stringify(fills).replace(/</g, "\\u003c");
  return "<!doctype html><html><head><meta charset='utf-8'>" +
    "<style>" +
    "body{margin:0;padding:18px 20px;font:14px/1.55 -apple-system,'Segoe UI',Arial,sans-serif;color:#1a1a1a;background:#fff}" +
    ".cm-to{font-size:12.5px;color:#667;margin-bottom:4px}.cm-to b{color:#111}" +
    ".cm-subj{font-size:14.5px;font-weight:650;margin:6px 0 12px;padding-bottom:10px;border-bottom:1px solid #ececf2}" +
    "img{max-width:100%} a{color:#4350C4}" +
    ".cm-fill{border-bottom:2px dotted #b3baff;background:#f2f3ff;padding:0 1px;border-radius:2px}" +
    "</style></head><body>" +
    "<div class='cm-to'>To: <b>" + previewHtmlEsc(p.hr_email) + "</b>" +
    (p.hr_name ? " &middot; " + previewHtmlEsc(p.hr_name) : "") +
    (p.company ? " &middot; " + previewHtmlEsc(p.company) : "") + "</div>" +
    "<div class='cm-subj'>" + previewHtmlEsc(p.subject) + "</div>" +
    localToUrl(p.body) +
    "<script>" +
    "var fills=" + fillsJson + ";" +
    "function esc(s){return String(s).replace(/&/g,'&').replace(/</g,'<').replace(/>/g,'>')}" +
    "function walk(n){" +
    "if(n.nodeType===3){var t=n.nodeValue,ch=false;" +
    "for(var i=0;i<fills.length;i++){var v=fills[i];if(v&&t.indexOf(v)>=0){t=t.split(v).join('<span class=cm-fill>'+esc(v)+'</span>');ch=true;}}" +
    "if(ch){var d=document.createElement('div');d.innerHTML=t;while(d.firstChild){n.parentNode.insertBefore(d.firstChild,n);}n.parentNode.removeChild(n);}}" +
    "else if(n.nodeType===1&&n.nodeName!=='SCRIPT'&&n.nodeName!=='STYLE'){Array.prototype.slice.call(n.childNodes).forEach(walk);}}" +
    "document.body.querySelectorAll('*').forEach(walk);" +
    "<\/script></body></html>";
}

function stepRecipient(dir) {
  const n = data.previews.length;
  if (!n) return;
  data.preview_idx = (data.preview_idx + dir + n) % n;
  const frame = $q(".preview-paper");
  if (frame) drawPreview(frame);
  const cap = $q(".recip-caption");
  if (cap) cap.textContent = "Person " + (data.preview_idx + 1) + " of " + n + " shown · " +
    fmtInt(data.preview_total) + " " + (data.preview_total === 1 ? "person" : "people") + " in total";
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
  const fills = [];
  for (const f of data.fields) {
    const m = S.mapping[f];
    if (!m) continue;
    const v = String(m).startsWith("column:") ? String(row[String(m).slice(7)] ?? "") : String(m);
    if (v && !fills.includes(v)) fills.push(v);
  }
  frame.srcdoc = previewDoc(p, fills);
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
  const total = data.preview_total || rows.length;
  const push = (cls, ic, html) => card.append(el("div", { class: "check-line " + cls }, icon(ic, "i i-16"), el("span", { html })));
  if (!total) {
    push("warn", "alert-triangle", "This selection has no recipients yet.");
    return;
  }
  push("ok", "check-circle-2", "<b>" + fmtInt(total) + "</b> " + (total === 1 ? "person" : "people") + " will get this");
  const missing = rows.filter((r) => !(r.hr_email || "").trim()).length;
  if (missing) push("warn", "alert-triangle", "<b>" + fmtInt(missing) + "</b> row" + (missing === 1 ? " has" : "s have") + " no email address - they'll be skipped");
  else push("ok", "check-circle-2", "Every row has an email address");
  const seen = new Set(); let dupes = 0;
  for (const r of rows) {
    const e = String(r.hr_email || "").trim().toLowerCase();
    if (!e) continue;
    if (seen.has(e)) dupes++;
    seen.add(e);
  }
  if (dupes) push("warn", "alert-triangle", "<b>" + fmtInt(dupes) + "</b> duplicate email" + (dupes === 1 ? "" : "s") + " - each one still gets an email");
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
  if (fix) fix.addEventListener("click", (e) => { e.preventDefault(); S.step = 1; S.matchOpen = true; touch(); render(); });
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

function renderStep2() {
  const panel = $q("#send-panel");
  const cols = el("div", { class: "send-cols" });
  const left = el("div", { style: "min-width:0" });
  left.append(
    el("div", { class: "recip-switch" },
      el("b", { style: "font-size:13.5px" }, "What they'll get"),
      el("span", { class: "push" }),
      el("button", { class: "btn btn-icon", "aria-label": "Previous person", onclick: () => stepRecipient(-1) }, icon("chevron-left", "i i-16")),
      el("button", { class: "btn btn-icon", "aria-label": "Next person", onclick: () => stepRecipient(1) }, icon("chevron-right", "i i-16"))),
    el("div", { class: "recip-caption", id: "recip-caption" }, "Loading previews…"),
    el("iframe", { class: "preview-paper", "aria-label": "Email preview", scrolling: "no" }));
  const right = el("div", { style: "min-width:0" }, checksCard(), summaryCard());
  cols.append(left, right);
  panel.append(cols,
    footer(
      el("button", { class: "btn btn-ghost", onclick: () => { S.step = 1; render(); } }, icon("chevron-left", "i i-14"), " Back"),
      "Checking…",
      primary3(),
      false, ""));
  (async () => {
    try {
      const d = await api("/api/merge/preview", { body: {
        template_id: S.templateId, mapping: S.mapping, sheet_id: S.listId,
        contact_ids: S.contactIds,
        limit: 8, append_signature: S.append_signature ? 1 : 0,
      } });
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
    const pl = planDelivery({ outcome: o.id, n: nPeople(), cap: data.maxCap, sentToday: data.sentToday });
    const capLine = o.id === "send" ? sendCapSentence(pl) : o.id === "schedule" ? scheduleCapSentence(pl) : "";
    const b = el("button", {
      class: "opt-card" + (S.outcome === o.id ? " is-selected" : "") + (disabled ? " is-disabled" : ""),
      type: "button", disabled: disabled ? "" : null,
      title: disabled ? "Safe mode is on. Turn it off to send immediately." : "",
      onclick: () => { if (!disabled) { S.outcome = o.id; touch(); render(); } },
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
        render();
        resolve(true);
      } catch (e) { toast(e.message, { type: "error" }); done = true; m.close(); resolve(false); }
    }));
  });
}

async function pickWhen() {
  const count = Math.max(1, data.preview_total || nPeople() || 1);
  try {
    const r = await openTimePicker({
      count,
      defaultStaggerMin: S.stagger || data.defaultStagger || 0,
      initial: S.when ? parseWhen(S.when) : null,
    });
    S.when = r.when;
    S.stagger = r.staggerMin || 0;
    touch(); render();
  } catch (e) {
    if (e && e.message !== "cancelled") toast(e.message, { type: "error" });
  }
}

function whenBlock() {
  const when = parseWhen(S.when);
  return el("div", { class: "card", style: "padding:14px 16px;margin-top:12px" },
    el("div", { style: "display:flex;align-items:center;gap:12px;flex-wrap:wrap" },
      icon("clock", "i i-16"),
      el("span", { style: "font-weight:600" }, when ? fmtLong(when.toISOString()) : "Pick a time"),
      S.stagger ? el("span", { class: "t-caption t-faint" }, "spaced " + S.stagger + " min apart") : null,
      el("span", { class: "push" }),
      el("button", { class: "btn btn-ghost btn-sm", onclick: pickWhen }, when ? "Change" : "Choose time")));
}

const files = [];

function addOn(ico, title, stateText, open, build) {
  const wrap = el("div", { class: "addon" + (open ? " open" : "") });
  const body = el("div", { class: "addon-body" }, build());
  wrap.append(el("button", { class: "addon-head", type: "button" },
    icon(ico, "i i-16"),
    el("span", {}, title),
    el("span", { class: "addon-state" }, stateText),
    el("span", { class: "chev" }, icon("chevron-down", "i i-14"))),
    body);
  wrap.querySelector(".addon-head").addEventListener("click", () => wrap.classList.toggle("open"));
  return wrap;
}

function addOns() {
  const wrap = el("div", { class: "card", style: "padding:4px 16px" });
  const rtpls = data.templates.filter((t) => (t.kind || "outreach") === "reminder");
  wrap.append(
    addOn("pencil", "Signature", S.append_signature ? "On" : "Off", S.append_signature, () =>
      el("label", { style: "display:flex;gap:10px;align-items:center;font-size:13.5px;cursor:pointer" },
        el("input", { type: "checkbox", checked: S.append_signature ? "" : null,
          onchange: (e) => { S.append_signature = e.target.checked; touch(); render(); } }),
        "Append your saved signature to every email")),
    addOn("mail", "CC", S.cc.trim() ? S.cc.trim() : "None", !!S.cc.trim(), () => {
      const inp = el("input", { class: "input", style: "width:100%", value: S.cc, placeholder: "name@company.com, another@company.com", "aria-label": "CC addresses" });
      inp.addEventListener("change", () => { S.cc = inp.value; touch(); render(); });
      return el("div", {}, inp, el("div", { class: "field-help" }, "Copied onto every email in this batch."));
    }),
    addOn("bell", "Follow-up reminder", S.reminder_days > 0 ? "Every " + S.reminder_days + "d" : "Off", S.reminder_days > 0, () => {
      const daysIn = el("input", { class: "input", type: "number", min: "1", max: "90", value: String(S.reminder_days || 3), style: "width:70px" });
      const maxSel = el("select", { class: "select", style: "max-width:130px" },
        [1, 2, 3].map((n) => el("option", { value: String(n), selected: S.reminder_max === n ? "" : null }, String(n) + "×")));
      maxSel.addEventListener("change", () => { S.reminder_max = +maxSel.value; touch(); render(); });
      const tplSel = el("select", { class: "select", style: "max-width:300px" },
        el("option", { value: "0" }, "No reminder template"),
        ...rtpls.map((t) => el("option", { value: String(t.id), selected: String(S.reminder_template_id) === String(t.id) ? "" : null }, t.name)));
      tplSel.addEventListener("change", () => { S.reminder_template_id = +tplSel.value; touch(); render(); });
      const on = S.reminder_days > 0;
      return el("div", { style: "display:flex;flex-direction:column;gap:10px" },
        el("label", { style: "display:flex;gap:10px;align-items:center;font-size:13.5px;cursor:pointer" },
          el("input", { type: "checkbox", checked: on ? "" : null,
            onchange: (e) => {
              S.reminder_days = e.target.checked ? (Math.max(1, Math.min(90, +daysIn.value || 3)) || 3) : 0;
              touch(); render();
            } }),
          "Follow up with people who don't reply"),
        el("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap" },
          el("span", { class: "t-caption t-faint" }, "Every"), daysIn,
          el("span", { class: "t-caption t-faint" }, "day(s),"), maxSel),
        el("div", { style: "display:flex;gap:10px;align-items:center;flex-wrap:wrap" },
          el("span", { class: "t-caption t-faint" }, "Using"), tplSel));
    }),
    addOn("paperclip", "Attachments",
      files.length ? fmtInt(files.length) + " file" + (files.length === 1 ? "" : "s") : "None",
      files.length > 0, () => {
        const fileIn = el("input", { type: "file", multiple: true, style: "display:none" });
        const add = el("button", { class: "btn btn-ghost btn-sm", onclick: () => fileIn.click() }, icon("upload", "i i-14"), " Add files");
        const list = el("div", { style: "display:flex;flex-direction:column;gap:6px;margin-top:10px" });
        files.forEach((f, i) => {
          list.append(el("div", { style: "display:flex;align-items:center;gap:10px;font-size:13px" },
            icon("paperclip", "i i-14"),
            el("span", { style: "flex:1;min-width:0" }, f.name),
            el("span", { class: "t-caption t-faint" }, Math.max(1, Math.round(f.size / 1024)) + " KB"),
            el("button", { class: "btn btn-icon", "aria-label": "Remove " + f.name,
              onclick: () => { files.splice(i, 1); touch(); render(); } }, icon("x", "i i-14"))));
        });
        fileIn.addEventListener("change", () => {
          const fs = [...(fileIn.files || [])];
          if (!fs.length) return;
          const total = files.reduce((a, f) => a + f.size, 0) + fs.reduce((a, f) => a + f.size, 0);
          if (total > 15 * 1024 * 1024) { toast("Attachments must stay under 15 MB in total.", { type: "error" }); return; }
          files.push(...fs);
          touch(); render();
        });
        return el("div", {}, add, list,
          el("div", { class: "field-help" }, "Sent with every email in this batch. Re-select the files if you refresh the page."));
      }));
  return wrap;
}

function summaryCard() {
  const camp = data.campaigns.find((x) => x.id === S.campaignId);
  const tpl = data.templates.find((t) => t.id === S.templateId);
  const n = data.preview_total || nPeople();
  const L = (ic, label, val) => el("div", { class: "summary-line" }, icon(ic, "i i-16"),
    el("span", { style: "flex:1;color:var(--text-2)" }, label), el("b", {}, val));
  const when = parseWhen(S.when);
  const outcomeLabel = {
    draft: "Saved as Gmail drafts - nothing is sent",
    schedule: S.when ? "Scheduled for " + fmtLong(when.toISOString()) + (S.stagger ? " · spaced " + S.stagger + " min apart" : "") : "Scheduled (pick a time first)",
    send: "Sent immediately, one after another",
    queue: "Kept in your email queue",
  }[S.outcome];
  return el("div", { class: "summary-card", style: "margin-top:14px" },
    el("h3", {}, "Summary"),
    L("users", "To", (S.contactIds.length ? fmtInt(S.contactIds.length) + " of " : "") + data.listName + (camp ? " · " + camp.name : "")),
    L("file-text", "Template", tpl ? tpl.name : "—"),
    L("send", "From", (data.accounts.find((a) => a.id === S.account) || {}).email || "No account picked"),
    S.append_signature ? L("pencil", "Signature", "appended") : null,
    S.cc.trim() ? L("mail", "CC", S.cc.trim()) : null,
    S.reminder_days > 0 ? L("bell", "Follow-up", "every " + S.reminder_days + " day(s), up to " + S.reminder_max + "×") : null,
    files.length ? L("paperclip", "Attachments", fmtInt(files.length) + " file" + (files.length === 1 ? "" : "s")) : null,
    el("div", { class: "summary-outcome" }, outcomeLabel));
}

/* ================= Final CTA + create ================= */
function accountEmail() {
  return (data.accounts.find((a) => a.id === S.account) || {}).email || "";
}

function footerSummary3() {
  const n = data.preview_total || nPeople();
  const pl = planNow();
  const base = fmtInt(n) + " " + (n === 1 ? "email" : "emails") + " · " +
    { draft: "as drafts", schedule: "scheduled", send: "sent now", queue: "queued" }[S.outcome];
  return S.outcome === "send" && pl.capped ? base + " · " + fmtInt(pl.later) + " kept for later" : base;
}

function primary3() {
  const n = data.preview_total || nPeople();
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

function confirmSendNow() {
  return new Promise((resolve) => {
    const n = data.preview_total || nPeople();
    const pl = planNow();
    const capNote = sendCapSentence(pl);
    let done = false;
    const m = modal({
      title: "Send " + fmtInt(pl.today) + " real emails now?",
      sub: "They go out from " + accountEmail() + " immediately, one after another, and can't be recalled.",
      body: el("div", {},
        el("div", { class: "banner banner-warn" }, icon("alert-triangle", "i i-16"),
          el("span", {}, "If this is the first time, 'Send me a test' is the safe move.")),
        capNote ? el("div", { class: "field-help", style: "margin-top:10px" }, capNote) : null,
        el("div", { class: "field-help", style: "margin-top:10px" }, "Hold the button for 2 seconds to confirm.")),
      foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), el("button", { class: "btn btn-danger" }, "Hold to send " + fmtInt(pl.today))],
      onClose: () => { if (!done) resolve(false); },
    });
    const hold = m.foot.querySelector(".btn-danger");
    m.foot.querySelector(".btn-ghost").addEventListener("click", () => { done = true; m.close(); resolve(false); });
    holdToConfirm(hold, {
      duration: 2000, label: "Hold to send " + fmtInt(pl.today),
      onConfirm: () => { done = true; m.close(); resolve(true); },
    });
  });
}

function confirmSchedule() {
  return new Promise((resolve) => {
    const n = data.preview_total || nPeople();
    const when = S.when ? fmtLong(parseWhen(S.when).toISOString()) : "";
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
    contact_ids: S.contactIds.length ? S.contactIds : undefined,
    account_id: S.account,
    action,
    at: S.when,
    stagger: S.stagger,
    cc: S.cc,
    append_signature: S.append_signature ? 1 : 0,
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
      prog.done(prog.add("Creating " + fmtInt(nPeople()) + " emails…"),
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
    successScreen(result);
  } catch (e) {
    root.innerHTML = "";
    root.append(
      errorCard("Couldn't finish. " + e.message, { onRetry: startCreate }),
      el("div", { style: "margin-top:12px" },
        el("button", { class: "btn btn-ghost", onclick: () => location.href = "/campaigns" }, "Back to campaigns")));
  }
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
    sub = "Goes out " + (S.when ? fmtLong(parseWhen(S.when).toISOString()) : "at the chosen time") +
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
  const cid = S.campaignId || 0;
  root.innerHTML = "";
  root.append(el("div", { class: "send-success" },
    el("div", { class: "empty-illust" }, `<svg viewBox="0 0 96 96" width="96" height="96" aria-hidden="true"><circle cx="48" cy="48" r="26" fill="var(--success-bg)" stroke="none"/><path d="M36 49l9 9 17-19" fill="none" stroke="var(--success)" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`),
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
load();
