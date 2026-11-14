/* Campaign hub — plan 7.2 (shell), 7.3 (overview), 7.6 (audience).
   The Emails tab lives in emails.js and is mounted here on ?tab=emails. */
import {
  api, el, icon, toast, modal, menu, confirmSimple, confirmTyped,
  badge, errorCard, emptyState, withLoading,
} from "/static/js/core/ui.js";
import { STATUS, CAMPAIGN_STATUS, plural, statusOf } from "/static/js/core/copy.js";
import { fmtInt, fmtPct, fmtDay, fmtDayTime, relTime, upcomingWhen, parseWhen } from "/static/js/core/format.js";
import { initEmailsTab, openEmailDrawer } from "./emails.js";
import { openTimePicker } from "/static/js/components/timepicker.js";

const cid = window.hub_campaign_id;
const root = document.getElementById("hub-root");
const initTab = new URLSearchParams(location.search).get("tab");
let tab = initTab === "emails" ? "emails" : initTab === "audience" ? "audience" : "overview";
let data = null;
let accounts = [];
let lastAccountId = 0;
let headerEl = null;
let tabsEl = null;

/* ---------------- derived ---------------- */
function counts() {
  const s = data.summary;
  return { all: s.n_emails, ready: s.n_ready, drafted: s.n_drafted, scheduled: s.n_scheduled, sent: s.n_sent, replied: s.n_replied, bounced: s.n_bounced, failed: s.n_failed, stopped: s.n_stopped };
}
function campaignStatus() {
  const s = data.summary;
  if (!s.n_emails) return "draft";
  const recent = s.last_sent_at && parseWhen(s.last_sent_at) && (Date.now() - parseWhen(s.last_sent_at).getTime()) < 14 * 86400e3;
  if (s.n_ready || s.n_drafted || s.n_scheduled || recent) return "active";
  return "finished";
}
function ctx() {
  return {
    campaignId: cid,
    campaignName: data ? data.campaign.name : "",
    batches: data ? data.batches : [],
    accounts,
    lastAccountId,
    counts,
    onChanged: refreshData,
  };
}

/* ---------------- load / render ---------------- */
async function load() {
  try {
    const [d, acc] = await Promise.all([
      api("/api/campaigns/" + cid),
      api("/api/accounts").catch(() => ({ accounts: [], last_account_id: 0 })),
    ]);
    data = d;
    accounts = acc.accounts || [];
    lastAccountId = acc.last_account_id || 0;
    document.title = (data.campaign.name || "Campaign") + " - CampusMail AI";
    render();
  } catch (e) {
    root.innerHTML = "";
    if (e.status === 404) {
      root.append(emptyState({
        icon: "wrong",
        title: "This campaign no longer exists",
        text: "It may have been deleted. Everything about it is gone.",
        primary: el("button", { class: "btn btn-primary", onclick: () => { location.href = "/campaigns"; } }, "Back to campaigns"),
      }));
      return;
    }
    root.append(errorCard("Could not load this campaign. Check your connection and try again.", { onRetry: load, detail: e.message }));
  }
}

function render() {
  const crumb = document.getElementById("crumb-name");
  if (crumb) crumb.textContent = data.campaign.name;
  root.innerHTML = "";
  headerEl = header();
  tabsEl = tabs();
  root.append(headerEl, tabsEl);
  const panel = el("div", { class: "tab-panel", style: "padding-top:var(--sp-5)" });
  root.append(panel);
  if (tab === "emails") mountEmails(panel);
  else if (tab === "audience") renderAudience(panel);
  else renderOverview(panel);
}

/* silent refresh after in-tab actions: data + header + tab counts, panel untouched */
async function refreshData() {
  try {
    data = await api("/api/campaigns/" + cid);
    refreshShell();
  } catch { /* keep working on stale data */ }
}
function refreshShell() {
  const crumb = document.getElementById("crumb-name");
  if (crumb) crumb.textContent = data.campaign.name;
  const t = headerEl && headerEl.querySelector("#hub-title");
  if (t && !t.querySelector("input")) t.textContent = data.campaign.name;
  const b = headerEl && headerEl.querySelector("#hub-badge");
  if (b) b.replaceWith(Object.assign(badge(CAMPAIGN_STATUS[campaignStatus()].label, CAMPAIGN_STATUS[campaignStatus()].cls, CAMPAIGN_STATUS[campaignStatus()].icon), { id: "hub-badge" }));
  const note = headerEl && headerEl.querySelector("#hub-note");
  if (note) {
    note.innerHTML = "";
    if (data.campaign.description) note.textContent = data.campaign.description;
    else note.append(el("button", { class: "linklike", onclick: editNote }, "Add a note"));
  }
  if (tabsEl) {
    const c = counts();
    const e = tabsEl.querySelector("[data-count=emails]");
    if (e) e.textContent = fmtInt(c.all);
    const a = tabsEl.querySelector("[data-count=audience]");
    if (a) a.textContent = fmtInt(data.n_contacts);
  }
}

/* ---------------- header (7.2) ---------------- */
function header() {
  const c = data.campaign;
  const st = CAMPAIGN_STATUS[campaignStatus()];
  const title = el("div", { class: "page-title hub-title", id: "hub-title", title: "Click to rename" }, c.name);
  const pencil = el("span", { class: "hub-pencil" }, icon("pencil", "i i-16"));
  title.append(pencil);
  title.addEventListener("click", () => startRename(title));

  const kebab = el("button", { class: "btn btn-icon", "aria-label": "More actions" }, icon("more-vertical", "i"));
  kebab.addEventListener("click", () => menu(kebab, [
    { label: "Rename", icon: "pencil", action: () => startRename(title) },
    { label: "Edit note", icon: "file-text", action: editNote },
    { label: "Export report (CSV)", icon: "download", action: exportReport },
    "-",
    { label: "Delete campaign", icon: "trash-2", danger: true, action: deleteCampaign },
  ]));

  const note = el("div", { class: "t-caption t-faint", id: "hub-note", style: "margin-top:4px" });
  if (c.description) note.textContent = c.description;
  else note.append(el("button", { class: "linklike", onclick: editNote }, "Add a note"));

  return el("div", { class: "page-head" },
    el("div", { class: "ph-main" },
      el("div", { style: "display:flex;align-items:center;gap:10px" }, title, badge(st.label, st.cls, st.icon)),
      note),
    el("div", { class: "push", style: "display:flex;gap:10px;align-items:center;flex:none" },
      el("button", { class: "btn btn-primary", onclick: () => { location.href = "/campaigns/" + cid + "/send"; } }, "Send emails"),
      kebab));
}

function startRename(titleEl) {
  if (titleEl.querySelector("input")) return;
  const old = data.campaign.name;
  const input = el("input", { class: "input", value: old, style: "font-size:22px;font-weight:650;padding:2px 10px;width:min(420px,70vw);border-radius:10px" });
  titleEl.innerHTML = "";
  titleEl.append(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const v = input.value.trim();
    if (save && v && v !== old) {
      try {
        await api("/api/campaigns/" + cid, { body: { name: v } });
        data.campaign.name = v;
        toast("Renamed to " + v + ".");
      } catch (e) { toast(e.message, { type: "error" }); }
    }
    refreshShell();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    else if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

function editNote() {
  const ta = el("textarea", { class: "textarea", placeholder: "Who is this for?", value: data.campaign.description || "" });
  const go = el("button", { class: "btn btn-primary" }, "Save note");
  const m = modal({
    title: "Edit note",
    sub: "A short line so your team knows what this campaign is for.",
    body: el("div", { class: "field" }, el("label", { class: "field-label" }, "Note"), ta),
    foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), go],
  });
  m.foot.querySelector(".btn-ghost").addEventListener("click", () => m.close());
  go.addEventListener("click", () => withLoading(go, async () => {
    try {
      await api("/api/campaigns/" + cid, { body: { description: ta.value.trim() } });
      data.campaign.description = ta.value.trim();
      m.close();
      refreshShell();
    } catch (e) { toast(e.message, { type: "error" }); }
  }));
}

async function deleteCampaign() {
  const s = data.summary;
  const hasSent = (s.n_sent || 0) + (s.n_replied || 0) + (s.n_bounced || 0) > 0;
  let ok = false;
  if (hasSent) {
    ok = await confirmTyped({
      title: "Delete this campaign?",
      body: fmtInt(s.n_emails) + " " + plural(s.n_emails, "email") + ", including ones already sent, will be deleted from the app. Gmail drafts are not touched.",
      expect: data.campaign.name,
      actionLabel: "Delete campaign",
    });
  } else {
    ok = await confirmSimple({
      title: "Delete this campaign?",
      body: "Its sends and " + fmtInt(s.n_emails) + " " + plural(s.n_emails, "email") + " are removed from the app. Gmail drafts are not touched.",
      actionLabel: "Delete campaign",
      onAction: async () => {
        try { await api("/api/campaigns/" + cid + "/delete", { body: {} }); location.href = "/campaigns"; }
        catch (e) { toast(e.message, { type: "error" }); }
      },
    });
  }
  if (ok && hasSent) {
    try { await api("/api/campaigns/" + cid + "/delete", { body: {} }); location.href = "/campaigns"; }
    catch (e) { toast(e.message, { type: "error" }); }
  }
}

async function exportReport() {
  try {
    const d = await api("/api/emails?campaign_id=" + cid + "&sort=time&dir=asc");
    const rows = d.rows || [];
    const q = (v) => '"' + String(v ?? "").replace(/"/g, '""') + '"';
    const lines = [["No.", "Company", "Name", "Email", "Subject", "Status", "Status code", "Scheduled for", "Sent at", "Replied at", "Bounced at"].join(",")];
    for (const r of rows) {
      const code = r.status + (r.replied_at ? "+replied" : "") + (r.bounced_at ? "+bounced" : "");
      lines.push([r.serial, r.company, r.hr_name, r.to_addr, r.subject, STATUS[statusOf(r)].label, code, r.scheduled_at || "", r.sent_at || "", r.replied_at || "", r.bounced_at || ""].map(q).join(","));
    }
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const a = el("a", { href: URL.createObjectURL(blob), download: (data.campaign.name || "campaign") + "-report.csv" });
    document.body.append(a);
    a.click();
    a.remove();
    toast("Report downloaded (" + fmtInt(rows.length) + " rows).");
  } catch (e) { toast(e.message, { type: "error" }); }
}

/* ---------------- tabs (underline, in URL) ---------------- */
function tabs() {
  const c = counts();
  const t = (k, label, count) => el("a", {
    class: "tab", href: "/campaigns/" + cid + "?tab=" + k, "aria-selected": String(tab === k),
  }, label, count === null ? null : el("span", { class: "seg-badge", "data-count": k }, fmtInt(count)));
  return el("nav", { class: "tabs tabs-sticky", role: "tablist" },
    t("overview", "Overview", null),
    t("emails", "Emails", c.all),
    t("audience", "Audience", data.n_contacts));
}

/* ---------------- overview (7.3) ---------------- */
function renderOverview(panel) {
  const ns = nextStep();
  if (ns) panel.append(ns);
  panel.append(statTiles());
  panel.append(el("div", { class: "grid-12", style: "margin-top:var(--sp-4)" },
    el("div", { class: "span-6" }, funnelCard()),
    el("div", { class: "span-6" }, sendsPerDayCard())));
  panel.append(el("div", { class: "grid-12", style: "margin-top:var(--sp-4)" },
    el("div", { class: "span-7", style: "display:flex;flex-direction:column;gap:var(--sp-4)" },
      standsCard(), sendsCard()),
    el("div", { class: "span-5", style: "display:flex;flex-direction:column;gap:var(--sp-4)" },
      repliesCard(), activityCard())));
}

/* exactly one next step, first match wins (7.3) */
function nextStep() {
  const s = data.summary;
  const link = (label, href, cls = "btn-primary", extra = {}) =>
    el("a", { class: "btn " + cls, href, ...extra }, label);
  const card = (msg, buttons) => el("div", { class: "nextstep" },
    icon("lightbulb", "i i-18"),
    el("div", { style: "flex:1" }, msg),
    el("div", { class: "push" }, ...buttons));
  if (data.n_contacts === 0) return card("Add the companies you want to email.", [link("Add contacts", "?tab=audience")]);
  if (s.n_emails === 0) return card("Your list is ready. Send your first emails.", [link("Send emails", "/campaigns/" + cid + "/send")]);
  if (s.n_ready > 0) return card(fmtInt(s.n_ready) + " " + plural(s.n_ready, "email") + " ready. Save them as Gmail drafts to review.", [link("Create drafts", "?tab=emails&status=ready&do=drafts")]);
  if (s.n_drafted > 0) return card(fmtInt(s.n_drafted) + " " + plural(s.n_drafted, "draft") + " waiting in Gmail. Review them there, or schedule them from here.",
    [link("Open Gmail", "https://mail.google.com/mail/?view=drafts", "btn-secondary", { target: "_blank" }), link("Schedule them", "?tab=emails&status=drafted&do=schedule")]);
  if (s.n_scheduled > 0) return card(fmtInt(s.n_scheduled) + " " + plural(s.n_scheduled, "email") + " go out " + upcomingWhen(s.next_scheduled_at) + ".", [link("View emails", "?tab=emails&status=scheduled")]);
  if (s.n_bounced > 0) return card(fmtInt(s.n_bounced) + " " + plural(s.n_bounced, "email") + " bounced. Fix the addresses and resend.", [link("Fix addresses", "?tab=emails&status=bounced")]);
  if (s.n_failed > 0) return card(fmtInt(s.n_failed) + " " + plural(s.n_failed, "email") + " failed. Fix the problem and resend.", [link("View failed", "?tab=emails&status=failed")]);
  if (s.n_sent_unreplied > 0 && s.last_sent_at && parseWhen(s.last_sent_at) && Date.now() - parseWhen(s.last_sent_at).getTime() > 3 * 86400e3) {
    return card(fmtInt(s.n_sent_unreplied) + (s.n_sent_unreplied === 1 ? " person" : " people") + " have not replied. Send them a follow-up.", [link("Set up follow-up", "/campaigns/" + cid + "/send")]);
  }
  return null;
}

function countUp(node, to) {
  if (!window.matchMedia("(prefers-reduced-motion: no-preference)").matches) { node.textContent = fmtInt(to); return; }
  const start = performance.now();
  const step = (t) => {
    const p = Math.min(1, (t - start) / 600);
    node.textContent = fmtInt(Math.round(to * (1 - Math.pow(1 - p, 3))));
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function statTiles() {
  const s = data.summary;
  const rate = s.n_sent ? (100 * s.n_replied / s.n_sent) : 0;
  const tile = (label, value, sub, href, isPct = false) => {
    const v = el("div", { class: "stat-value" }, "0");
    const a = el("a", { class: "stat stat-link", href }, label, v, el("div", { class: "stat-sub" }, sub));
    requestAnimationFrame(() => {
      if (isPct) {
        const start = performance.now();
        const step = (t) => {
          const p = Math.min(1, (t - start) / 600);
          v.textContent = fmtPct(value * (1 - Math.pow(1 - p, 3)));
          if (p < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
      } else countUp(v, value);
    });
    return a;
  };
  return el("div", { class: "stat-row" },
    tile("Sent", s.n_sent, "delivered so far", "?tab=emails&status=sent"),
    tile("Replied", s.n_replied, "people wrote back", "?tab=emails&status=replied"),
    tile("Reply rate", rate, s.n_sent ? "of " + fmtInt(s.n_sent) + " sent" : "no sends yet", "?tab=emails&status=replied", true),
    tile("Bounced", s.n_bounced, "addresses that did not work", "?tab=emails&status=bounced"));
}

function funnelCard() {
  const s = data.summary;
  const items = [
    { label: "Contacts", n: data.n_contacts, op: 1 },
    { label: "Emailed", n: data.n_emailed || 0, op: 0.55 },
    { label: "Replied", n: s.n_replied, op: 0.3 },
  ];
  const max = Math.max(1, ...items.map((it) => it.n));
  const rows = items.map((it, i) => {
    const prevN = i === 0 ? null : items[i - 1].n;
    const pct = i === 0 ? "your list"
      : prevN === 0 ? "no " + items[i - 1].label.toLowerCase() + " yet"
      : Math.round(100 * it.n / prevN) + "% of " + items[i - 1].label.toLowerCase();
    return el("div", { class: "funnel-row" },
      el("div", { class: "funnel-label" }, it.label),
      el("div", { class: "funnel-bar", style: "width:" + Math.min(100, Math.max(2, 100 * it.n / max)) + "%;opacity:" + it.op }),
      el("div", {},
        el("div", { class: "funnel-val" }, fmtInt(it.n)),
        el("div", { class: "funnel-pct" }, pct)));
  });
  const extra = [];
  if (s.n_bounced) extra.push(fmtInt(s.n_bounced) + " bounced");
  if (s.n_failed) extra.push(fmtInt(s.n_failed) + " failed");
  return el("div", { class: "card" },
    el("div", { class: "card-head" }, el("div", { class: "t-card" }, "From contact to reply")),
    el("div", { class: "funnel" }, ...rows),
    extra.length ? el("div", { class: "funnel-caption" }, extra.join(" · ")) : null);
}

function dstr(d) {
  const p = (x) => String(x).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function sendsPerDayCard() {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = [];
  for (let i = 13; i >= 0; i--) days.push(new Date(today.getTime() - i * 86400e3));
  const key = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  const vals = days.map((d) => data.per_day[key(d)] || 0);
  const card = el("div", { class: "card" }, el("div", { class: "card-head" }, el("div", { class: "t-card" }, "Sends per day, last 14 days")));
  if (vals.filter((v) => v > 0).length < 3) {
    card.append(el("div", { class: "t-caption t-faint", style: "padding:10px 0" }, "The chart appears after a few days of sends."));
    return card;
  }
  const max = Math.max(...vals);
  const tallest = vals.indexOf(max);
  const bars = el("div", { class: "barchart" });
  const labels = el("div", { class: "barchart-labels" });
  days.forEach((d, i) => {
    const v = vals[i];
    const bar = el("div", { class: "bar" + (v ? "" : " zero"), style: "height:" + (v ? Math.max(6, 100 * v / max) + "%" : "2px") },
      v ? el("span", { class: "bar-tip" }, fmtInt(v) + " on " + fmtDay(dstr(d))) : null);
    if (i === tallest) bar.append(el("span", { class: "bar-top" }, fmtInt(v)));
    bars.append(bar);
    labels.append(el("span", {}, i % 2 === 1 ? fmtDayTime(d).split(" ")[0] + " " + d.getDate() : ""));
  });
  card.append(bars, labels);
  return card;
}

function standsCard() {
  const c = counts();
  const total = Math.max(1, c.all);
  const order = ["ready", "drafted", "scheduled", "sent", "replied", "bounced", "failed", "stopped"];
  const seg = el("div", { class: "segbar" },
    ...order.filter((k) => c[k]).map((k) => el("span", { style: "width:" + (100 * c[k] / total) + "%;background:var(--st-" + k + ")" }, STATUS[k].short)));
  const legend = el("div", { class: "legend" },
    ...order.filter((k) => c[k]).map((k) => el("a", { href: "?tab=emails&status=" + k },
      el("span", { class: "dot", style: "background:var(--st-" + k + ")" }), STATUS[k].short, el("b", {}, fmtInt(c[k])))));
  const card = el("div", { class: "card" },
    el("div", { class: "card-head" }, el("div", { class: "t-card" }, "Where every email stands")));
  if (!c.all) {
    card.append(emptyState({ icon: "send", title: "No emails yet", text: "When you send emails, each one shows up here.", primary: el("a", { class: "btn btn-primary", href: "/campaigns/" + cid + "/send" }, "Send emails") }));
  } else card.append(seg, legend);
  return card;
}

function sendsCard() {
  const card = el("div", { class: "card card-flush" },
    el("div", { class: "card-head" }, el("div", { class: "t-card" }, "Sends in this campaign")));
  const batches = data.batches;
  if (!batches.length) {
    card.append(el("div", { class: "t-caption t-faint", style: "padding:12px 18px" }, "Each time you send, the group of emails is kept here as a send."));
    return card;
  }
  const order = ["pending", "drafted", "scheduled", "sent", "error", "cancelled"];
  const color = { pending: "var(--st-ready)", drafted: "var(--st-drafted)", scheduled: "var(--st-scheduled)", sent: "var(--st-sent)", error: "var(--st-failed)", cancelled: "var(--st-stopped)" };
  for (const b of batches) {
    const n = Object.values(b.counts || {}).reduce((a, x) => a + x, 0);
    const mix = n
      ? el("div", { class: "mixbar" },
          ...order.filter((k) => b.counts[k]).map((k) => el("span", { style: "width:" + (100 * b.counts[k] / n) + "%;background:" + color[k] })))
      : el("span", { class: "t-caption t-faint" }, "—");
    card.append(el("div", { class: "sendrow" },
      el("div", { style: "min-width:0" },
        el("div", { class: "sr-name" }, b.name || ("Send " + b.id)),
        el("div", { class: "sr-sub" }, "Created " + fmtDay(b.created_at))),
      el("span", { class: "num", title: "Emails" }, fmtInt(n)),
      mix,
      el("span", { class: "sr-sub", style: "min-width:86px" }, b.reminder_days > 0 ? "On, after " + b.reminder_days + " days" : "Off"),
      el("a", { class: "btn btn-ghost btn-sm push", href: "?tab=emails&batch=" + b.id }, "View emails")));
  }
  return card;
}

function repliesCard() {
  const card = el("div", { class: "card" },
    el("div", { class: "card-head" }, el("div", { class: "t-card" }, "Recent replies"), el("a", { class: "t-caption push", href: "/replies?campaign=" + cid }, "View all replies")));
  const rows = data.recent_replies || [];
  if (!rows.length) {
    card.append(el("div", { class: "t-caption t-faint", style: "padding:10px 0" }, "Replies appear here as soon as they arrive."));
    return card;
  }
  for (const r of rows) {
    card.append(el("div", { class: "reply-mini" },
      el("div", { style: "min-width:0" },
        el("div", { class: "rm-name" }, [r.company, r.hr_name].filter(Boolean).join(", ") || r.to_addr),
        el("div", { class: "rm-sub" }, r.to_addr)),
      el("span", { class: "rm-time" }, relTime(r.replied_at)),
      el("button", { class: "btn btn-ghost btn-sm", onclick: () => openEmailDrawer({ id: r.id, company: r.company, hr_name: r.hr_name, to_addr: r.to_addr }, ctx()) }, "Open")));
  }
  return card;
}

const ACT_PHRASE = {
  drafts_create: (d) => "created " + ((d.match(/^(\d+) drafts?/) || [])[1] || "some") + " drafts",
  schedule: (d) => "scheduled " + ((d.match(/^(\d+) emails?/) || [])[1] || "some") + " emails",
  send_now: (d) => "sent " + ((d.match(/^(\d+) sent/) || [])[1] || "0") + " emails",
  sheet_upload: () => "added a contact list",
  campaign_create: () => "created a campaign",
  emails_delete: (d) => "removed " + ((d.match(/(\d+) email/) || [])[1] || "some") + " emails",
  emails_cancel: (d) => "stopped " + ((d.match(/(\d+) email/) || [])[1] || "some") + " emails",
  mark_replied: () => "marked a reply",
  reply_sent: () => "sent a reply",
  reply_draft: () => "saved a reply draft",
};
const ACT_ICON = { drafts_create: "file-pen", schedule: "clock", send_now: "send", sheet_upload: "file", campaign_create: "zap", emails_delete: "trash-2", emails_cancel: "pause-circle", mark_replied: "reply", reply_sent: "reply", reply_draft: "file-pen" };

function activityCard() {
  const card = el("div", { class: "card" },
    el("div", { class: "card-head" }, el("div", { class: "t-card" }, "Recent activity"), el("a", { class: "t-caption push", href: "/team" }, "View all")));
  api("/api/activity").then((d) => {
    const rows = (d.rows || []).slice(0, 5);
    if (!rows.length) { card.append(el("div", { class: "t-caption t-faint", style: "padding:10px 0" }, "Nothing yet — actions your team takes show up here.")); return; }
    for (const r of rows) {
      const verb = (ACT_PHRASE[r.action] || (() => r.detail || "made a change"))(r.detail || "");
      card.append(el("div", { class: "act-row" },
        icon(ACT_ICON[r.action] || "zap", "i i-16"),
        el("div", { style: "min-width:0" }, el("b", {}, r.actor || "Someone"), " " + verb),
        el("span", { class: "act-time" }, relTime(r.ts))));
    }
  }).catch(() => card.append(el("div", { class: "t-caption t-faint", style: "padding:10px 0" }, "Could not load activity.")));
  return card;
}

/* ---------------- emails tab ---------------- */
function mountEmails(panel) {
  panel.innerHTML = "";
  const t = initEmailsTab(panel, ctx());
  t.ctx.tab = {
    drafts: t.actions.drafts, schedule: t.actions.schedule, sendNow: t.actions.sendNow,
    stop: t.actions.stop, remove: t.actions.remove, markReplied: t.markReplied,
  };
}

/* ---------------- audience (7.6) ---------------- */
function renderAudience(panel) {
  const sheets = data.sheets;
  const fileIn = el("input", { type: "file", accept: ".csv,.txt,.xlsx", style: "display:none" });
  fileIn.addEventListener("change", () => { if (fileIn.files && fileIn.files[0]) uploadSheet(fileIn.files[0]); fileIn.value = ""; });
  panel.append(fileIn);

  const addBtn = el("button", { class: "btn btn-secondary" }, "Add a list");
  addBtn.addEventListener("click", () => menu(addBtn, [
    { label: "Upload a file", icon: "upload", action: () => fileIn.click() },
    { label: "Choose an existing list", icon: "library", action: chooseExisting },
  ]));

  const recipients = data.recipients || [];
  const showRecipients = !sheets.length && recipients.length > 0;
  const head = el("div", { class: "camp-toolbar" },
    el("div", { class: "camp-note" }, showRecipients
      ? "Everyone this campaign reached, with their current status."
      : "The contact lists in this campaign. Drop a CSV or Excel file anywhere here to upload."),
    el("div", { class: "push" }, addBtn));
  panel.append(head);

  if (showRecipients) {
    const table = el("table", { class: "table", "aria-label": "Recipients" },
      el("thead", {}, el("tr", {},
        el("th", {}, "Company"), el("th", {}, "HR name"), el("th", {}, "HR email"), el("th", {}, "Status"))),
      el("tbody", {}));
    for (const rc of recipients) {
      const badge = rc.replied
        ? el("span", { class: "badge replied" }, "Replied")
        : rc.bounced
          ? el("span", { class: "badge bounced" }, "Bounced")
          : el("span", { class: "badge sent" }, "Sent");
      table.tBodies[0].append(el("tr", {},
        el("td", {}, rc.company || "—"),
        el("td", {}, rc.hr_name || "—"),
        el("td", {}, rc.hr_email || "—"),
        el("td", {}, badge)));
    }
    panel.append(el("div", { class: "table-wrap" }, table));
  } else if (!sheets.length) {
    panel.append(emptyState({
      icon: "contacts",
      title: "No contact lists yet.",
      text: "Upload a spreadsheet with company, HR name and HR email.",
      primary: el("button", { class: "btn btn-primary", onclick: () => fileIn.click() }, "Upload a file"),
    }));
  } else {
    const table = el("table", { class: "table", "aria-label": "Contact lists" },
      el("thead", {}, el("tr", {},
        el("th", {}, "List"), el("th", { class: "num" }, "Contacts"), el("th", {}, "Added"), el("th", { style: "width:44px" }, ""))),
      el("tbody", {}));
    for (const sh of sheets) {
      const kebab = el("button", { class: "btn btn-icon row-kebab", "aria-label": "More actions" }, icon("more-vertical", "i i-16"));
      kebab.addEventListener("click", (e) => {
        e.stopPropagation();
        menu(kebab, [
          { label: "Open list", icon: "external-link", action: () => { location.href = "/library"; } },
          { label: "Remove from campaign", icon: "x", danger: true, action: () => removeFromCampaign(sh) },
        ]);
      });
      table.tBodies[0].append(el("tr", {},
        el("td", {},
          el("div", { class: "cell-main" }, sh.name),
          el("div", { style: "display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;max-width:320px" },
            ...(sh.headers || []).map((h) => el("span", { class: "tag" }, h)))),
        el("td", { class: "num" }, fmtInt(sh.n_rows)),
        el("td", { class: "cell-sub" }, fmtDay(sh.created_at)),
        el("td", { style: "text-align:right" }, kebab)));
    }
    panel.append(el("div", { class: "table-wrap" }, table));
  }

  // drag & drop upload
  let overlay = null;
  const showOverlay = (show) => {
    if (show && !overlay) {
      overlay = el("div", { style: "position:fixed;inset:0;z-index:60;background:color-mix(in srgb, var(--surface) 70%, transparent);display:flex;align-items:center;justify-content:center" },
        el("div", { class: "card", style: "padding:40px 60px;text-align:center" },
          icon("upload", "i i-24"),
          el("div", { style: "font-size:15px;font-weight:650;margin-top:10px" }, "Drop to upload your list")));
      document.body.append(overlay);
    } else if (!show && overlay) { overlay.remove(); overlay = null; }
  };
  panel.addEventListener("dragover", (e) => { e.preventDefault(); showOverlay(true); });
  panel.addEventListener("dragleave", (e) => { if (e.target === panel) showOverlay(false); });
  panel.addEventListener("drop", (e) => {
    e.preventDefault();
    showOverlay(false);
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) uploadSheet(f);
  });
}

async function uploadSheet(file) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("campaign_id", String(cid));
  try {
    const res = await api("/api/sheets/upload", { body: fd, method: "POST" });
    toast("Added " + res.name + " with " + fmtInt(res.n_rows) + " " + plural(res.n_rows, "contact") + ".", {
      action: () => load(), actionLabel: "Refresh",
    });
    if (res.no_email) toast(res.no_email + " rows had no email address.", { type: "error" });
  } catch (e) {
    toast(e.message, { type: "error" });
  }
}

async function chooseExisting() {
  let d;
  try { d = await api("/api/sheets"); } catch (e) { toast(e.message, { type: "error" }); return; }
  const others = (d.sheets || []).filter((x) => x.campaign_id !== cid);
  if (!others.length) { toast("No other lists to choose from.", { type: "info" }); return; }
  const m = modal({
    title: "Choose a list",
    sub: "Pick a list from your library to add to this campaign.",
    body: el("div", {}, ...others.map((sh) => el("button", {
      class: "menu-item", style: "width:100%;justify-content:flex-start;text-align:left;margin-bottom:2px",
      onclick: () => { m.close(); attachSheet(sh); },
    }, el("div", { style: "flex:1;min-width:0" },
      el("div", { style: "font-weight:650" }, sh.name),
      el("div", { style: "font-size:12px;color:var(--text-3)" }, fmtInt(sh.n_rows) + " " + plural(sh.n_rows, "contact")))))),
  });
}

async function attachSheet(sh) {
  try {
    await api("/api/sheets/" + sh.id + "/campaign", { body: { campaign_id: cid } });
    toast("Added " + sh.name + " to this campaign.");
    load();
  } catch (e) { toast(e.message, { type: "error" }); }
}

async function removeFromCampaign(sh) {
  const ok = await confirmSimple({
    title: "Remove from campaign?",
    body: sh.name + " stays in your library; it is just taken out of this campaign.",
    actionLabel: "Remove from campaign",
    onAction: async () => {
      try {
        await api("/api/sheets/" + sh.id + "/campaign", { body: { campaign_id: 0 } });
        toast("Removed " + sh.name + " from this campaign.");
        load();
      } catch (e) { toast(e.message, { type: "error" }); }
    },
  });
}

load();

/* dev hook: ?debug=drawer|picker — visual QA for the drawer and time picker */
const dbg = new URLSearchParams(location.search).get("debug");
if (dbg) {
  setTimeout(() => {
    if (!data) return;
    if (dbg === "drawer" && data.summary.n_emails) {
      api("/api/emails?campaign_id=" + cid + "&sort=time&dir=desc").then((d) => {
        const r = (d.rows || [])[0];
        if (r) openEmailDrawer(r, ctx());
      }).catch(() => {});
    } else if (dbg === "picker") {
      openTimePicker({ count: 8, defaultStaggerMin: 2 });
    }
  }, 900);
}
