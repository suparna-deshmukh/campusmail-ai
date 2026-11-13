/* Emails tab (old Queue) — plan 7.4 — plus the email drawer (7.5).
   Search, status segments with counts, send/member/sort filters in the
   URL, table with per-status actions, 50-row paging, floating selection
   bar, engine-overdue warning, four states. Glossary: send/list/space them
   out/sending engine/No. */
import {
  api, el, icon, toast, modal, drawer, menu, confirmSimple, undoable,
  holdToConfirm, statusBadge, badge, errorCard, emptyState, withLoading,
} from "/static/js/core/ui.js";
import { STATUS, MSG, plural, statusOf, bounceReason } from "/static/js/core/copy.js";
import { fmtInt, fmtDayTime, relTime, upcomingWhen, fmtLong, toBackend } from "/static/js/core/format.js";
import { openTimePicker } from "/static/js/components/timepicker.js";
import { openModeCard } from "/static/js/core/shell.js";

const PAGE = 50;
const STATUS_PARAM = { all: "", ready: "pending", drafted: "drafted", scheduled: "scheduled", sent: "sent", replied: "replied", bounced: "bounced", failed: "error", stopped: "cancelled" };
const FILTER_ORDER = ["ready", "drafted", "scheduled", "sent", "replied", "bounced", "failed", "stopped"];
const SORT_PARAM = { recipient: "company", status: "status", time: "time" };
const SORT_DIR = { recipient: "asc", status: "asc", time: "desc" };

// valid row actions per effective status (7.4)
const ACTIONS = {
  ready: ["create-draft", "schedule", "stop", "remove"],
  drafted: ["schedule", "stop", "remove"],
  scheduled: ["reschedule", "stop"],
  sent: ["reply", "mark-replied"],
  replied: ["reply", "mark-not-replied"],
  bounced: ["remove"],
  failed: ["remove"],
  stopped: ["remove"],
};
const ACTION_LABEL = {
  "create-draft": "Create draft", schedule: "Schedule", reschedule: "Reschedule",
  "send-now": "Send now", stop: "Stop", remove: "Remove", reply: "Reply",
  "mark-replied": "Mark as replied", "mark-not-replied": "Mark as not replied",
};
const ACTION_ICON = {
  "create-draft": "file-pen", schedule: "clock", reschedule: "clock", "send-now": "send",
  stop: "pause-circle", remove: "trash-2", reply: "reply",
  "mark-replied": "check-circle-2", "mark-not-replied": "undo-2",
};

function safeMode() { return !window.cm || !window.cm.state ? true : window.cm.state.safe_mode; }
function defaultStagger() {
  const fromSettings = window.cm && window.cm.state && window.cm.state.settings
    ? Number(window.cm.state.settings.default_stagger_min || 0) : 0;
  if (fromSettings > 0) return fromSettings;
  try { return Number(localStorage.getItem("cm_stagger") || 0) || 0; } catch { return 0; }
}

/* ================= selection helpers ================= */
function targetRows(st) {
  if (st.allMatches) return st.rows;
  return [...st.selected.values()].filter((r) => st.rows.some((x) => x.id === r.id));
}
function allMatch(st, action) {
  const rows = targetRows(st);
  if (!rows.length) return false;
  return rows.every((r) => ACTIONS[statusOf(r)].includes(action));
}

/* ================= main tab ================= */
export function initEmailsTab(host, ctx) {
  const params = new URLSearchParams(location.search);
  const tab = {
    ctx,
    state: {
      q: params.get("q") || "",
      status: FILTER_ORDER.includes(params.get("status")) ? params.get("status") : "all",
      batch: params.get("batch") || "all",
      by: params.get("by") || "all",
      sort: SORT_PARAM[params.get("sort")] ? params.get("sort") : "time",
      shown: PAGE,
      rows: [],
      selected: new Map(),
      allMatches: false,
      error: null,
      loading: true,
    },
  };
  host.append(
    el("div", { id: "emails-engine" }),
    el("div", { class: "emails-toolbar" },
      el("div", { class: "row" },
        el("div", { class: "search-input", style: "max-width:300px;flex:1;min-width:180px" },
          icon("search", "i i-16"),
          el("input", { class: "input", id: "em-search", placeholder: "Search company, name, email or subject", "aria-label": "Search emails" })),
        el("div", { class: "seg", id: "em-status", role: "group", "aria-label": "Status" })),
      el("div", { class: "row" },
        el("select", { class: "select", id: "em-batch", style: "width:180px", "aria-label": "Send" }),
        el("select", { class: "select", id: "em-by", style: "width:160px", "aria-label": "Sent by" }),
        el("select", { class: "select", id: "em-sort", style: "width:150px", "aria-label": "Sort" },
          el("option", { value: "time" }, "Sort: Time"),
          el("option", { value: "recipient" }, "Sort: Recipient"),
          el("option", { value: "status" }, "Sort: Status")),
        el("span", { class: "next-send-note push", id: "em-next" })),
    ),
    el("div", { class: "table-wrap", id: "em-wrap" },
      el("table", { class: "table", "aria-label": "Emails" },
        el("thead", {}, el("tr", {},
          el("th", { style: "width:36px" }, el("input", { type: "checkbox", class: "row-check", id: "em-check-all", "aria-label": "Select all shown" })),
          el("th", {}, "Recipient"), el("th", {}, "Subject"), el("th", {}, "Status"),
          el("th", {}, "When"), el("th", { style: "width:110px" }, ""))),
        el("tbody", { id: "em-rows" })),
      el("div", { id: "em-empty" }),
      el("div", { class: "show-more-wrap", id: "em-more-wrap", style: "display:none" },
        el("button", { class: "btn btn-secondary btn-sm", id: "em-more" }, "Show 50 more"),
        el("span", { class: "t-caption t-faint", id: "em-shown-of" })),
    ),
    el("div", { id: "em-selbar" }),
  );

  const $ = (id) => host.querySelector("#" + id);
  const s = tab.state;

  /* ---------- URL sync ---------- */
  function syncUrl() {
    const p = new URLSearchParams();
    p.set("tab", "emails");
    if (s.status !== "all") p.set("status", s.status);
    if (s.q) p.set("q", s.q);
    if (s.batch !== "all") p.set("batch", s.batch);
    if (s.by !== "all") p.set("by", s.by);
    if (s.sort !== "time") p.set("sort", s.sort);
    history.replaceState(null, "", "/campaigns/" + ctx.campaignId + "?" + p.toString());
  }

  /* ---------- data ---------- */
  async function load() {
    s.loading = true;
    s.error = null;
    renderStatic();
    renderBody();
    try {
      const p = new URLSearchParams({ campaign_id: String(ctx.campaignId), sort: SORT_PARAM[s.sort], dir: SORT_DIR[s.sort] });
      if (s.status !== "all") p.set("status", STATUS_PARAM[s.status]);
      if (s.batch !== "all") p.set("batch_id", s.batch);
      if (s.by !== "all") p.set("by", s.by);
      if (s.q) p.set("q", s.q);
      const d = await api("/api/emails?" + p.toString());
      let rows = d.rows || [];
      if (s.status === "sent") rows = rows.filter((r) => !r.replied_at && !r.bounced_at);
      s.rows = rows;
      // drop selections that no longer match
      for (const id of [...s.selected.keys()]) if (!rows.some((r) => r.id === id)) s.selected.delete(id);
    } catch (e) {
      s.error = e;
    }
    s.loading = false;
    renderStatic();
    renderBody();
    renderSelbar();
    const doFlag = params.get("do");
    if (doFlag && !s.loading && !s.error) {
      params.delete("do");
      history.replaceState(null, "", "/campaigns/" + ctx.campaignId + "?" + params.toString());
      const rows = s.rows;
      if (rows.length) {
        if (doFlag === "drafts") doDrafts(tab, rows);
        else if (doFlag === "schedule") doSchedule(tab, rows);
      }
    }
  }

  /* ---------- toolbar (counts, selects, next send) ---------- */
  function renderStatic() {
    const seg = $("em-status");
    seg.innerHTML = "";
    const counts = ctx.counts();
    seg.append(el("button", { class: "seg-item" + (s.status === "all" ? " is-active" : ""), onclick: () => setStatus("all") },
      "All", el("span", { class: "seg-badge" }, fmtInt(counts.all))));
    for (const k of FILTER_ORDER) {
      if (counts[k] === 0 && s.status !== k) continue;
      seg.append(el("button", { class: "seg-item" + (s.status === k ? " is-active" : ""), onclick: () => setStatus(k) },
        STATUS[k].short + " ", el("span", { class: "seg-badge" }, fmtInt(counts[k]))));
    }
    const batchSel = $("em-batch");
    const curBatch = s.batch;
    batchSel.innerHTML = "";
    batchSel.append(el("option", { value: "all" }, "All sends"));
    for (const b of ctx.batches) batchSel.append(el("option", { value: String(b.id) }, b.name || ("Send " + b.id)));
    batchSel.value = [...batchSel.options].some((o) => o.value === curBatch) ? curBatch : "all";
    s.batch = batchSel.value;

    const bySel = $("em-by");
    const members = [...new Set(ctx.batches.map((b) => b.created_by).filter(Boolean))];
    const curBy = s.by;
    bySel.innerHTML = "";
    bySel.append(el("option", { value: "all" }, "Anyone"));
    for (const m of members) bySel.append(el("option", { value: m }, m));
    bySel.value = [...bySel.options].some((o) => o.value === curBy) ? curBy : "all";
    s.by = bySel.value;

    const sortSel = $("em-sort");
    sortSel.value = s.sort;

    // next send caption
    const note = $("em-next");
    const sched = s.rows.filter((r) => statusOf(r) === "scheduled" && r.scheduled_at);
    note.textContent = "";
    if (sched.length) {
      const first = sched.reduce((a, b) => (a.scheduled_at <= b.scheduled_at ? a : b));
      const nAt = sched.filter((r) => r.scheduled_at === first.scheduled_at).length;
      note.append("Next send: " + upcomingWhen(first.scheduled_at) + ", " + nAt + " " + plural(nAt, "email"));
    }
    renderEngine();
  }

  function renderEngine() {
    const box = $("emails-engine");
    box.innerHTML = "";
    const sc = window.cm && window.cm.state && window.cm.state.scheduler;
    if (sc && !sc.running && sc.due_now > 0) {
      box.append(el("div", { class: "banner banner-warn" },
        icon("alert-triangle"),
        el("div", { style: "flex:1" }, "Some scheduled emails are overdue because the sending engine is not running."),
        el("button", { class: "btn btn-sm push", id: "em-run-now" }, "Run now")));
      const b = box.querySelector("#em-run-now");
      b.addEventListener("click", () => withLoading(b, async () => {
        await api("/api/scheduler/trigger");
        toast("The sending engine is running.", { type: "success" });
        setTimeout(renderEngine, 1200);
      }));
    }
  }

  function setStatus(k) {
    if (s.status === k) return;
    s.status = k;
    s.shown = PAGE;
    s.selected.clear();
    s.allMatches = false;
    syncUrl();
    load();
  }

  /* ---------- table body ---------- */
  function renderBody() {
    const tb = $("em-rows");
    const empty = $("em-empty");
    const moreWrap = $("em-more-wrap");
    tb.innerHTML = "";
    empty.innerHTML = "";
    moreWrap.style.display = "none";

    if (s.error) {
      empty.append(errorCard("Could not load emails. Check your connection and try again.", { onRetry: load, detail: s.error.message }));
      return;
    }
    if (s.loading && !s.rows.length) {
      for (let i = 0; i < 6; i++) {
        tb.append(el("tr", {}, ...[0, 1, 2, 3, 4].map(() => el("td", {}, el("div", { class: "skel", style: "height:20px;width:70%" })))))
      }
      return;
    }
    const hasFilters = s.status !== "all" || s.q || s.batch !== "all" || s.by !== "all";
    if (!s.rows.length) {
      if (hasFilters) {
        empty.append(emptyState({
          icon: "search", title: "No emails match", text: "Try a different search or status.",
          primary: el("button", { class: "btn btn-primary", onclick: clearFilters }, "Clear filters"),
        }));
      } else {
        empty.append(emptyState({
          icon: "send", title: "No emails in this campaign yet.",
          text: "When you send emails, each one shows up here with its status.",
          primary: el("button", { class: "btn btn-primary", onclick: () => { location.href = "/campaigns/" + ctx.campaignId + "/send"; } }, "Send emails"),
        }));
      }
      return;
    }

    const visible = s.rows.slice(0, s.shown);
    for (const r of visible) tb.append(rowEl(r));

    // header checkbox state
    const all = $("em-check-all");
    all.checked = visible.length > 0 && visible.every((r) => s.selected.has(r.id) || (s.allMatches && true));
    all.indeterminate = !all.checked && visible.some((r) => s.selected.has(r.id));

    if (s.rows.length > s.shown) {
      moreWrap.style.display = "";
      $("em-shown-of").textContent = "Showing " + fmtInt(s.shown) + " of " + fmtInt(s.rows.length);
      $("em-more").textContent = "Show " + Math.min(PAGE, s.rows.length - s.shown) + " more";
    }
    renderSelbar();
  }

  function clearFilters() {
    s.q = ""; s.status = "all"; s.batch = "all"; s.by = "all"; s.sort = "time";
    s.shown = PAGE;
    s.selected.clear();
    s.allMatches = false;
    $("em-search").value = "";
    syncUrl();
    load();
  }

  function rowEl(r) {
    const st = statusOf(r);
    const checked = s.selected.has(r.id);
    const tr = el("tr", { class: checked ? "is-selected" : "", dataset: { id: String(r.id) } });

    tr.append(el("td", {}, el("input", {
      type: "checkbox", class: "row-check", "aria-label": "Select " + (r.company || r.to_addr),
      onchange: (e) => {
        if (e.target.checked) s.selected.set(r.id, r); else s.selected.delete(r.id);
        s.allMatches = false;
        tr.classList.toggle("is-selected", e.target.checked);
        renderStatic();
        renderSelbar();
      },
    })));

    tr.append(el("td", {},
      el("div", { class: "cell-main" }, r.company || r.to_addr || "—"),
      el("div", { class: "cell-sub" }, [r.hr_name, r.to_addr, r.serial ? "No. " + r.serial : ""].filter(Boolean).join(" · "))));
    tr.append(el("td", {}, el("div", { class: "truncate", style: "max-width:260px", title: r.subject || "" }, r.subject || "—")));

    const stTd = el("td", {}, statusBadge(r));
    if (r.is_reminder) stTd.append(el("span", { class: "tag tag-primary", style: "margin-left:6px" }, "Follow-up " + (r.reminder_count || 1)));
    tr.append(stTd);

    const when = el("td", { class: "cell-sub", style: "max-width:220px" });
    if (st === "scheduled") when.append("Scheduled for " + upcomingWhen(r.scheduled_at));
    else if (st === "replied") when.append("Replied " + relTime(r.replied_at));
    else if (st === "bounced") when.append("Bounced " + relTime(r.bounced_at));
    else if (st === "failed") when.append(el("span", { class: "t-danger truncate-cell", title: r.error || "" }, (r.error || "A problem with sending").slice(0, 60)));
    else if (st === "sent") when.append("Sent " + relTime(r.sent_at));
    else if (st === "stopped") when.append("Stopped " + relTime(r.updated_at || r.created_at));
    else if (st === "drafted") when.append("Saved " + relTime(r.updated_at || r.created_at));
    else when.append("—");
    tr.append(when);

    // kebab + hover primary action
    const valid = ACTIONS[st] || [];
    const kebabWrap = el("div", { style: "display:flex;align-items:center;gap:4px;justify-content:flex-end" });
    if (valid[0]) {
      const primary = el("button", { class: "btn btn-ghost btn-sm row-action" }, ACTION_LABEL[valid[0]]);
      primary.addEventListener("click", (e) => { e.stopPropagation(); doAction(tab, valid[0], [r]); });
      kebabWrap.append(primary);
    }
    const kebab = el("button", { class: "btn btn-icon row-kebab", "aria-label": "More actions", style: "width:28px;height:28px" }, icon("more-vertical", "i i-16"));
    kebab.addEventListener("click", (e) => {
      e.stopPropagation();
      menu(kebab, [
        ...valid.map((a) => ({ label: ACTION_LABEL[a], icon: ACTION_ICON[a], action: () => doAction(tab, a, [r]) })),
        "-",
        { label: "Copy address", icon: "copy", action: () => copyText(r.to_addr || "") },
      ]);
    });
    kebabWrap.append(kebab);
    tr.append(kebabWrap);

    tr.addEventListener("click", () => openEmailDrawer(r, tab.ctx));
    return tr;
  }

  /* ---------- selection bar ---------- */
  function renderSelbar() {
    const box = $("em-selbar");
    box.innerHTML = "";
    const n = s.allMatches ? s.rows.length : s.selected.size;
    if (!n) return;
    const rows = targetRows(s);
    const validForAll = (a) => rows.length && rows.every((r) => ACTIONS[statusOf(r)].includes(a));
    const btn = (a, label, cls = "btn-secondary") => el("button", { class: "btn btn-sm " + cls, "data-a": a }, label);
    const items = [];
    if (validForAll("create-draft")) items.push(btn("create-draft", "Create drafts"));
    if (validForAll("schedule") || validForAll("reschedule")) items.push(btn("schedule", "Schedule"));
    if (validForAll("send-now") || rows.every((r) => ["ready", "drafted"].includes(statusOf(r)))) {
      const b = btn("send-now", "Send now", "btn-primary");
      if (safeMode()) {
        b.disabled = true;
        b.title = "Turn on live sending first";
        b.addEventListener("click", () => {});
        items.push(b, el("button", { class: "btn btn-sm btn-ghost selbar-link", onclick: () => openModeCard() }, "Turn on live sending"));
      } else items.push(b);
    }
    if (validForAll("stop")) items.push(btn("stop", "Stop"));
    if (rows.every((r) => (ACTIONS[statusOf(r)] || []).includes("remove"))) items.push(btn("remove", "Remove", "btn-danger-ghost"));

    if (s.allMatches) {
      box.append(el("span", { class: "selbar-count" }, fmtInt(n) + " matching selected"));
    } else {
      box.append(el("span", { class: "selbar-count" }, fmtInt(n) + " selected"));
      if (s.rows.length > s.shown) {
        box.append(el("button", { class: "btn btn-sm btn-ghost selbar-link", onclick: () => {
          s.allMatches = true;
          s.selected.clear();
          syncUrl();
          load();
        } }, "Select all " + fmtInt(s.rows.length) + " that match"));
      }
    }
    for (const b of items) {
      b.addEventListener("click", () => {
        const rowsNow = targetRows(s);
        if (!rowsNow.length) return;
        const a = b.dataset.a;
        if (a === "create-draft") doDrafts(tab, rowsNow);
        else if (a === "schedule") doSchedule(tab, rowsNow);
        else if (a === "send-now") doSendNow(tab, rowsNow);
        else if (a === "stop") doStop(tab, rowsNow);
        else if (a === "remove") doRemove(tab, rowsNow);
      });
    }
    box.append(...items);
  }

  /* ---------- actions ---------- */
  tab.actions = {
    drafts: (rows) => doDrafts(tab, rows),
    schedule: (rows) => doSchedule(tab, rows),
    sendNow: (rows) => doSendNow(tab, rows),
    stop: (rows) => doStop(tab, rows),
    remove: (rows) => doRemove(tab, rows),
  };

  function accountSelect(selectedId) {
    const sel = el("select", { class: "select", style: "width:100%", "aria-label": "Send from" });
    const accts = (ctx.accounts || []).filter((a) => a.connected);
    if (!accts.length) return { sel, error: true, value: () => 0 };
    const last = ctx.lastAccountId || selectedId;
    for (const a of accts) {
      sel.append(el("option", { value: String(a.id), selected: String(a.id) === String(last) || null }, a.label + (a.mine ? " (yours)" : "")));
    }
    return { sel, error: false, value: () => Number(sel.value) };
  }

  function recipientPreview(rows, max = 3) {
    const wrap = el("div", { class: "review-people" });
    rows.slice(0, max).forEach((r) => wrap.append(el("div", { class: "review-person" },
      el("span", { class: "rp-co" }, r.company || "—"),
      el("span", { class: "rp-mail" }, r.to_addr || ""))));
    if (rows.length > max) wrap.append(el("div", { class: "review-person" }, el("span", { class: "rp-mail" }, "and " + (rows.length - max) + " more")));
    return wrap;
  }

  /* Create drafts — tier 2 (review modal, no typed word) */
  async function doDrafts(tab, rows) {
    const n = rows.length;
    const ac = accountSelect();
    if (ac.error) {
      toast("Connect a Gmail account first.", { type: "error", action: () => { location.href = "/settings"; }, actionLabel: "Open Settings" });
      return;
    }
    const acctLabel = () => {
      const a = (ctx.accounts || []).find((x) => String(x.id) === String(ac.value()));
      return a ? (a.label || a.email) : "your account";
    };
    const sub = el("div", {}, `Save ${n} ${plural(n, "email")} as drafts in ${acctLabel()}? Nothing is sent.`);
    const refreshSub = () => { sub.textContent = `Save ${n} ${plural(n, "email")} as drafts in ${acctLabel()}? Nothing is sent.`; };
    ac.sel.addEventListener("change", refreshSub);
    const go = el("button", { class: "btn btn-primary" }, "Create " + n + " drafts");
    const m = modal({
      title: "Create " + n + " drafts",
      sub,
      body: el("div", {},
        el("div", { class: "field" }, el("label", { class: "field-label" }, "Send from"), ac.sel),
        recipientPreview(rows)),
      foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), go],
    });
    m.foot.querySelector(".btn-ghost").addEventListener("click", () => m.close());
    go.addEventListener("click", () => withLoading(go, async () => {
      m.close();
      try {
        const d = await api("/api/emails/draft", { body: { account_id: ac.value(), ids: rows.map((r) => r.id) } });
        toast(MSG.draftsCreated(d.created, acctLabel()), {
          action: () => window.open("https://mail.google.com/mail/?view=drafts", "_blank"), actionLabel: "Open Gmail",
        });
        if (d.failed) toast(d.failed + " " + plural(d.failed, "email") + " could not be saved. View them in the Failed filter.", { type: "error", action: () => setStatus("failed"), actionLabel: "View" });
        afterChange();
      } catch (e) {
        toast(e.message, { type: "error" });
      }
    }));
  }

  /* Schedule / reschedule — time picker, review sheet, undo */
  async function doSchedule(tab, rows) {
    const n = rows.length;
    const prev = rows.map((r) => ({ id: r.id, status: r.status, scheduled_at: r.scheduled_at || null }));
    const initial = rows.find((r) => r.scheduled_at);
    let picked;
    try {
      picked = await openTimePicker({ count: n, defaultStaggerMin: defaultStagger(), initial: initial ? new Date(initial.scheduled_at) : null, title: rows.some((r) => statusOf(r) === "scheduled") ? "Reschedule these emails" : "Schedule these emails" });
    } catch { return; }
    const ac = accountSelect();
    if (ac.error) {
      toast("Connect a Gmail account first.", { type: "error", action: () => { location.href = "/settings"; }, actionLabel: "Open Settings" });
      return;
    }
    const whenLong = fmtLong(picked.when);
    const check = el("label", { style: "display:flex;gap:8px;align-items:center;font-size:13px;font-weight:550;margin:10px 0" },
      el("input", { type: "checkbox", style: "width:16px;height:16px;accent-color:var(--primary-500)" }), "I checked the preview and the recipients");
    const go = el("button", { class: "btn btn-primary", disabled: true }, "Schedule " + n + " emails");
    check.querySelector("input").addEventListener("change", (e) => { go.disabled = !e.target.checked; });
    const m = modal({
      title: rows.some((r) => statusOf(r) === "scheduled") ? "Reschedule " + n + " emails" : "Schedule " + n + " emails",
      sub: `They go out ${whenLong}, ${picked.staggerMin ? "one every " + picked.staggerMin + (picked.staggerMin === 1 ? " minute" : " minutes") : "all at once"}.`,
      body: el("div", {},
        el("div", { class: "field" }, el("label", { class: "field-label" }, "Send from"), ac.sel),
        recipientPreview(rows), check),
      foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), go],
    });
    m.foot.querySelector(".btn-ghost").addEventListener("click", () => m.close());
    go.addEventListener("click", () => withLoading(go, async () => {
      m.close();
      try {
        await api("/api/emails/schedule", { body: { account_id: ac.value(), at: picked.when, stagger: picked.staggerMin, ids: rows.map((r) => r.id) } });
        toast(MSG.scheduled(n, whenLong), {
          action: async () => {
            try { await api("/api/emails/revert", { body: { items: prev.map((p) => ({ id: p.id, status: p.status === "scheduled" ? "scheduled" : "pending", scheduled_at: p.scheduled_at }))}});rChange(); toast("Back to how it was.", { type: "success" }); } catch { load(); }
          },
          actionLabel: "Undo",
        });
        afterChange();
      } catch (e) {
        toast(e.message, { type: "error" });
      }
    }));
  }

  /* Send now — tier 3 (review + hold + 10 s cancel) */
  function doSendNow(tab, rows) {
    const n = rows.length;
    if (safeMode()) { openModeCard(); return; }
    const ac = accountSelect();
    if (ac.error) {
      toast("Connect a Gmail account first.", { type: "error", action: () => { location.href = "/settings"; }, actionLabel: "Open Settings" });
      return;
    }
    const acctLabel = () => {
      const a = (ctx.accounts || []).find((x) => String(x.id) === String(ac.value()));
      return a ? (a.label || a.email) : "your account";
    };
    const check = el("label", { style: "display:flex;gap:8px;align-items:center;font-size:13px;font-weight:550;margin:10px 0" },
      el("input", { type: "checkbox", style: "width:16px;height:16px;accent-color:var(--primary-500)" }), "I checked the preview and the recipients");
    const holdBtn = el("button", { class: "btn btn-danger", disabled: true }, "Hold to send " + n + " emails");
    check.querySelector("input").addEventListener("change", (e) => { holdBtn.disabled = !e.target.checked; });
    const m = modal({
      title: "Send " + n + " emails now?",
      sub: `They go out right away from ${acctLabel()}. This cannot be undone.`,
      body: el("div", {}, recipientPreview(rows), check),
      foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), holdBtn],
    });
    m.foot.querySelector(".btn-ghost").addEventListener("click", () => m.close());
    holdToConfirm(holdBtn, {
      label: "Hold to send " + n + " emails",
      onConfirm: () => startCancelCountdown(m, rows, ac.value()),
    });
  }

  function startCancelCountdown(m, rows, accountId) {
    const n = rows.length;
    let left = 10;
    m.body.innerHTML = "";
    const count = el("div", { style: "font-size:26px;font-weight:650;text-align:center;padding:18px 0 6px" }, "Sending " + n + " " + plural(n, "email") + " in " + left);
    const sub = el("div", { style: "text-align:center;font-size:13px;color:var(--text-2)" }, "You can still change your mind.");
    const cancel = el("button", { class: "btn btn-secondary", style: "display:block;margin:14px auto 0" }, "Cancel");
    m.body.append(count, sub, cancel);
    const timer = setInterval(() => {
      left -= 1;
      if (left <= 0) {
        clearInterval(timer);
        m.close();
        fireSendNow(rows, accountId);
        return;
      }
      count.textContent = "Sending " + n + " " + plural(n, "email") + " in " + left;
    }, 1000);
    cancel.addEventListener("click", () => {
      clearInterval(timer);
      m.close();
      toast("Send cancelled. Nothing went out.", { type: "info" });
    });
  }

  async function fireSendNow(rows, accountId) {
    const n = rows.length;
    try {
      const d = await api("/api/emails/send-now", { body: { account_id: accountId, ids: rows.map((r) => r.id) } });
      if (d.failed) toast(MSG.partial(d.sent, n, d.failed), { type: "error", action: () => setStatus("failed"), actionLabel: "View the " + d.failed });
      else toast(MSG.sent(d.sent));
      afterChange();
    } catch (e) {
      toast(e.message, { type: "error" });
    }
  }

  /* Stop — tier 0 with Undo */
  function doStop(tab, rows) {
    const n = rows.length;
    const prev = rows.map((r) => ({ id: r.id, status: r.status, scheduled_at: r.scheduled_at || null }));
    s.rows.forEach((r) => { if (prev.some((p) => p.id === r.id)) { r.status = "cancelled"; r.scheduled_at = null; } });
    renderStatic();
    renderBody();
    undoable(MSG.stopped(n), async () => {
      await api("/api/emails/cancel", { body: { ids: rows.map((r) => r.id) } });
      ctx.onChanged();
    }, () => {
      prev.forEach((p) => {
        const r = s.rows.find((x) => x.id === p.id);
        if (r) { r.status = p.status; r.scheduled_at = p.scheduled_at; }
      });
      renderStatic();
      renderBody();
    });
  }

  /* Remove — tier 0 (1-10) with Undo, tier 1 above */
  async function doRemove(tab, rows) {
    const n = rows.length;
    if (n <= 10) {
      const removed = rows.slice();
      const prev = removed.map((r) => ({ id: r.id, status: r.status, scheduled_at: r.scheduled_at || null }));
      s.rows = s.rows.filter((r) => !removed.some((x) => x.id === r.id));
      removed.forEach((r) => s.selected.delete(r.id));
      renderStatic();
      renderBody();
      undoable("Removed " + n + " " + plural(n, "email") + ".", async () => {
        await api("/api/emails/delete", { body: { ids: removed.map((r) => r.id) } });
        ctx.onChanged();
      }, async () => {
        try {
          await api("/api/emails/revert", { body: { items: prev } });
          await load();
        } catch { load(); }
      });
      return;
    }
    const ok = await confirmSimple({
      title: "Remove " + n + " " + plural(n, "email") + "?",
      body: "They are taken out of this campaign. Gmail drafts are not touched.",
      actionLabel: "Remove " + n + " " + plural(n, "email"),
      onAction: async () => {
        try {
          await api("/api/emails/delete", { body: { ids: rows.map((r) => r.id) } });
          toast("Removed " + n + " " + plural(n, "email") + ".");
          afterChange();
        } catch (e) { toast(e.message, { type: "error" }); }
      },
    });
  }

  /* Mark replied / not replied — tier 0 with Undo */
  function doMarkReplied(tab, r, on) {
    const prev = { replied_at: r.replied_at || null, status: r.status };
    r.replied_at = on ? new Date().toISOString() : null;
    if (on) r.status = "sent";
    renderBody();
    undoable(on ? MSG.markedReplied() : "Marked as not replied.", async () => {
      await api("/api/emails/" + r.id + "/mark-replied", { body: { replied: on } });
      ctx.onChanged();
    }, () => {
      r.replied_at = prev.replied_at;
      r.status = prev.status;
      renderBody();
    });
  }

  tab.markReplied = doMarkReplied;

  function afterChange() {
    ctx.onChanged();
    load();
  }

  /* ---------- wiring ---------- */
  let qTimer = null;
  $("em-search").value = s.q;
  $("em-search").addEventListener("input", (e) => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => {
      s.q = e.target.value.trim();
      s.shown = PAGE;
      s.selected.clear();
      s.allMatches = false;
      syncUrl();
      load();
    }, 250);
  });
  $("em-batch").addEventListener("change", (e) => { s.batch = e.target.value; s.shown = PAGE; s.selected.clear(); s.allMatches = false; syncUrl(); load(); });
  $("em-by").addEventListener("change", (e) => { s.by = e.target.value; s.shown = PAGE; s.selected.clear(); s.allMatches = false; syncUrl(); load(); });
  $("em-sort").addEventListener("change", (e) => { s.sort = e.target.value; s.shown = PAGE; syncUrl(); load(); });
  $("em-check-all").addEventListener("change", (e) => {
    const visible = s.rows.slice(0, s.shown);
    if (e.target.checked) visible.forEach((r) => s.selected.set(r.id, r));
    else visible.forEach((r) => s.selected.delete(r.id));
    s.allMatches = false;
    renderBody();
  });
  $("em-more").addEventListener("click", () => { s.shown += PAGE; renderBody(); });
  document.addEventListener("cm:state", renderEngine);
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/input|textarea|select/i.test(document.activeElement.tagName)) {
      e.preventDefault();
      $("em-search").focus();
    }
  });

  load();
  return tab;
}

function copyText(t) {
  if (!t) return;
  if (navigator.clipboard) navigator.clipboard.writeText(t).then(() => toast("Copied " + t, { type: "success" }));
}

/* ================= email drawer (7.5) ================= */
export async function openEmailDrawer(seedRow, ctx) {
  let full;
  try {
    full = (await api("/api/emails/view?id=" + seedRow.id)).email;
  } catch (e) {
    toast(e.message, { type: "error" });
    return;
  }
  const batch = (ctx.batches || []).find((b) => b.id === full.batch_id);
  const acct = (ctx.accounts || []).find((a) => a.id === full.account_id);
  const st = statusOf(full);
  const body = el("div", {});

  // timeline
  const tl = el("div", { class: "timeline" });
  const node = (label, when, sub, timeText) => el("div", { class: "tl-node" },
    el("span", {}, label),
    when ? el("span", { class: "tl-time", title: when }, timeText || relTime(when)) : null,
    sub ? el("div", { class: "t-caption t-faint" }, sub) : null);
  tl.append(node("Created", full.created_at));
  if (full.status === "drafted" || full.sent_at || full.scheduled_at) tl.append(node("Saved as draft", full.updated_at || full.created_at));
  if (full.status === "scheduled" && full.scheduled_at) tl.append(node("Scheduled for", full.scheduled_at, null, upcomingWhen(full.scheduled_at)));
  if (full.sent_at) tl.append(node(full.is_reminder ? "Follow-up " + (full.reminder_count || 1) + " sent" : "Sent", full.sent_at));
  if (full.bounced_at) tl.append(node("Bounced", full.bounced_at, bounceReason(full.bounce_reason)));
  else if (full.replied_at) tl.append(node("Replied", full.replied_at));
  if (full.status === "error") tl.append(node("A problem with sending", full.updated_at || full.created_at, full.error || ""));
  body.append(tl);

  // the email (always white)
  body.append(el("div", { class: "ema-panel" },
    el("div", { class: "ema-subject" }, full.subject || "(no subject)"),
    el("iframe", { class: "ema-frame", sandbox: "", title: "Email preview", srcdoc: "<!doctype html><meta charset=utf-8><body style='font:14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;color:#1f2430;margin:0;padding:16px'>" + (full.body || "") + "</body>"})));
  if (batch && batch.attachments) {
    try {
      const names = JSON.parse(batch.attachments || "[]");
      if (names.length) {
        body.append(el("div", { style: "margin-top:10px" }, ...names.map((nm) => el("span", { class: "ema-attach" }, icon("paperclip", "i i-14"), nm))));
      }
    } catch { /* ignore */ }
  }

  // facts
  body.append(el("dl", { class: "ema-facts", style: "margin-top:var(--sp-4);border-top:1px solid var(--border);padding-top:var(--sp-4)" },
    el("dt", {}, "Sent from"), el("dd", {}, acct ? acct.label || acct.email : "—"),
    el("dt", {}, "Send"), el("dd", {}, batch ? (batch.name || "Send " + batch.id) : "—"),
    el("dt", {}, "Campaign"), el("dd", {}, ctx.campaignName || "—"),
    el("dt", {}, "No."), el("dd", {}, full.serial ? "#" + full.serial : "—")));

  // failed banner
  if (st === "failed") {
    body.append(el("div", { class: "banner banner-danger", style: "margin-top:var(--sp-4)" },
      icon("alert-triangle"),
      el("div", { style: "flex:1" },
        el("div", {}, full.error || "A problem with sending."),
        el("div", { class: "t-caption t-faint", style: "margin-top:2px" }, "Fix the problem, then remove or re-create this email."))));
  }
  if (st === "bounced") {
    body.append(el("div", { class: "banner banner-danger", style: "margin-top:var(--sp-4)" },
      icon("alert-triangle"),
      el("div", { style: "flex:1" },
        el("div", {}, bounceReason(full.bounce_reason)),
        el("div", { class: "t-caption t-faint", style: "margin-top:2px" }, "Edit the contact's email, then resend."))));
  }

  const d = drawer({
    title: seedRow.company || full.company || full.to_addr || "Email",
    sub: [seedRow.hr_name || full.hr_name, full.to_addr, full.serial ? "No. " + full.serial : ""].filter(Boolean).join(" · "),
    body,
    foot: el("div", { id: "ema-foot" }),
    onClose: () => { if (replyBox) replyBox = null; },
  });

  function renderFoot() {
    const foot = d.foot;
    foot.innerHTML = "";
    const stNow = statusOf(full);
    const btn = (label, cls, fn) => { const b = el("button", { class: "btn " + cls }, label); b.addEventListener("click", fn); return b; };
    const doAct = (a) => {
      const r = s_row();
      if (a === "create-draft") tabActions.drafts([r]);
      else if (a === "schedule") tabActions.schedule([r]);
      else if (a === "reschedule") tabActions.schedule([r]);
      else if (a === "stop") tabActions.stop([r]);
      else if (a === "remove") { tabActions.remove([r]); d.close(); }
      else if (a === "mark-replied") { tabActions.markReplied(r, true); d.close(); }
      else if (a === "mark-not-replied") { tabActions.markReplied(r, false); d.close(); }
    };
    const s_row = () => ({ ...full, company: seedRow.company, hr_name: seedRow.hr_name });
    const map = {
      ready: [["create-draft", "Create draft", "btn-secondary"], ["schedule", "Schedule", "btn-primary"]],
      drafted: [["schedule", "Schedule", "btn-primary"], ["stop", "Stop", "btn-ghost"]],
      scheduled: [["reschedule", "Reschedule", "btn-primary"], ["stop", "Stop", "btn-ghost"]],
      sent: [["reply", "Reply", "btn-primary"], ["mark-replied", "Mark as replied", "btn-ghost"]],
      replied: [["reply", "Reply", "btn-primary"], ["mark-not-replied", "Mark as not replied", "btn-ghost"]],
      bounced: [["edit-contact", "Edit contact", "btn-secondary"], ["remove", "Remove", "btn-danger-ghost"]],
      failed: [["remove", "Remove", "btn-danger-ghost"]],
      stopped: [["remove", "Remove", "btn-danger-ghost"]],
    };
    const items = map[stNow] || [["remove", "Remove", "btn-danger-ghost"]];
    for (const [a, label, cls] of items) {
      foot.append(btn(label, cls, () => {
        if (a === "edit-contact") { location.href = "/contacts"; return; }
        if (a === "reply") { openReply(); return; }
        doAct(a);
      }));
    }
    if (safeMode() && ["ready", "drafted"].includes(stNow)) {
      foot.append(el("span", { class: "t-caption t-faint", style: "display:flex;align-items:center;gap:6px" },
        icon("shield-check", "i i-14"), "Drafts only — nothing is sent."));
    }
  }

  let replyBox = null;
  function openReply() {
    const foot = d.foot;
    foot.innerHTML = "";
    const ta = el("textarea", { class: "textarea", placeholder: "Write your reply…", style: "width:100%;min-height:74px" });
    const asDraft = safeMode();
    const go = el("button", { class: "btn " + (asDraft ? "btn-secondary" : "btn-primary") }, asDraft ? "Save as Gmail draft" : "Send reply");
    const back = el("button", { class: "btn btn-ghost", style: "margin-right:auto" }, "Back");
    replyBox = el("div", { style: "display:flex;flex-direction:column;gap:10px;width:100%" },
      el("div", { style: "font-size:13px;font-weight:650" }, "Reply to " + (full.to_addr || "")),
      ta,
      el("div", { style: "display:flex;gap:10px;align-items:center" }, back,
        asDraft ? el("span", { class: "t-caption t-faint" }, "Drafts only mode — the reply waits in Gmail.") : null,
        go));
    foot.append(replyBox);
    ta.focus();
    back.addEventListener("click", () => { replyBox = null; renderFoot(); });
    go.addEventListener("click", () => withLoading(go, async () => {
      try {
        const r = await api("/api/emails/" + full.id + "/reply", { body: { body: ta.value, account_id: acct ? acct.id : 0, as_draft: asDraft } });
        toast(r.drafted ? "Reply saved as a Gmail draft." : "Reply sent.", { type: "success" });
        d.close();
        ctx.onChanged();
      } catch (e) { toast(e.message, { type: "error" }); }
    }));
  }

  // kebab in drawer head: Copy address, Remove
  const headBtn = d.body.parentElement.querySelector("button[aria-label=Close]");
  const headEl = headBtn && headBtn.parentElement;
  const kebab = el("button", { class: "btn btn-icon", "aria-label": "More actions", style: "width:32px;height:32px" }, icon("more-vertical", "i i-16"));
  kebab.addEventListener("click", (e) => {
    e.stopPropagation();
    menu(kebab, [
      { label: "Copy address", icon: "copy", action: () => copyText(full.to_addr || "") },
      { label: "Remove", icon: "trash-2", danger: true, action: () => { tabActions.remove([s_row2()]); d.close(); } },
    ]);
  });
  const s_row2 = () => ({ ...full, company: seedRow.company, hr_name: seedRow.hr_name });
  if (headEl && headBtn) headEl.insertBefore(kebab, headBtn);

  const noop = () => {};
  const tabActions = ctx.tab || { drafts: noop, schedule: noop, sendNow: noop, stop: noop, remove: noop, markReplied: noop };
  renderFoot();
  return d;
}
