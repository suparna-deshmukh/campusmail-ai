/* Team (plan 10.2) — members, results, activity. Admins only.
   Glossary: send/list/spacing/follow-up. Sentence case, no exclamation marks. */
import {
  api, el, icon, esc, toast, modal, menu, confirmSimple, withLoading,
  badge, statusBadge, errorCard, emptyState,
} from "/static/js/core/ui.js";
import { MSG, plural, statusOf } from "/static/js/core/copy.js";
import { fmtInt, fmtPct, fmtDay, fmtTime, fmtDayTime, initials, avatarColor, relTime } from "/static/js/core/format.js";

const $ = (id) => document.getElementById(id);
const view = $("tm-view");
const state = {
  tab: new URLSearchParams(location.search).get("tab") || "members",
  admin: null,
};

/* ================= init ================= */
async function init() {
  document.querySelectorAll("#tm-tabs .tab").forEach((t) =>
    t.setAttribute("aria-selected", String(t.dataset.tab === state.tab)));
  if (window.me_role !== "admin") {
    view.append(emptyState({ icon: "shield-check", title: "Admins only",
      text: "You need admin access to see the team page. Ask your admin for access." }));
    return;
  }
  try {
    state.admin = await api("/api/admin/data");
  } catch (e) {
    view.append(errorCard("Could not load the team.", { onRetry: init }));
    return;
  }
  const pend = state.admin.requests.length;
  const b = $("tm-req-badge");
  if (b) { b.textContent = String(pend); b.style.display = pend ? "" : "none"; }
  if (state.tab === "members") renderMembers();
  else if (state.tab === "results") renderResults();
  else renderActivity();
}

/* ================= members ================= */
function renderMembers() {
  view.innerHTML = "";
  const { requests, members } = state.admin;

  /* requests card */
  if (requests.length) {
    const card = el("div", { class: "tm-card" },
      el("div", { class: "tm-card-head" }, icon("user-plus", "i i-16"),
        "Requests to join (" + requests.length + ")"),
      el("div", { class: "tm-card-body" },
        ...requests.map((u) => {
          const approve = el("button", { class: "btn btn-primary btn-sm" }, "Approve");
          const decline = el("button", { class: "btn btn-ghost btn-sm" }, "Decline");
          approve.onclick = async () => {
            await withLoading(approve, async () => {
              try {
                await api("/api/admin/requests", { body: { user_id: u.id, action: "approve" } });
                toast((u.display_name || u.email) + " can now sign in.", {
                  action: () => api("/api/admin/requests", { body: { user_id: u.id, action: "revert" } }).then(refresh),
                });
                refresh();
              } catch (e) { toast(e.message, { type: "error" }); }
            });
          };
          decline.onclick = async () => {
            await withLoading(decline, async () => {
              try {
                await api("/api/admin/requests", { body: { user_id: u.id, action: "deny" } });
                toast((u.display_name || u.email) + "'s request was declined.", {
                  action: () => api("/api/admin/requests", { body: { user_id: u.id, action: "revert" } }).then(refresh),
                });
                refresh();
              } catch (e) { toast(e.message, { type: "error" }); }
            });
          };
          return el("div", { class: "tm-req-row" },
            el("span", { class: "avatar", style: `background:${avatarColor(u.display_name || u.email)}` },
              initials(u.display_name || u.email)),
            el("div", { class: "tm-req-main" },
              el("div", { class: "tm-req-name" }, u.display_name || u.email,
                el("span", { class: "t-caption" }, "  " + u.email)),
              u.request_note ? el("div", { class: "tm-req-note" }, "\"" + u.request_note + "\"") : null),
            el("div", { class: "tm-req-actions" }, decline, approve));
        })));
    view.append(card);
  }

  /* invite row */
  const invEmail = el("input", { class: "input first", placeholder: "name@company.com", "aria-label": "Invite email" });
  const invName = el("input", { class: "input", placeholder: "Name (optional)", "aria-label": "Invite name" });
  const invBtn = el("button", { class: "btn btn-primary" }, "Invite");
  invBtn.onclick = async () => {
    const email = invEmail.value.trim();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { toast("Enter a valid email address.", { type: "error" }); return; }
    await withLoading(invBtn, async () => {
      try {
        const d = await api("/api/admin/invite", { body: { email, name: invName.value.trim() } });
        toast(d.how === "updated" ? email + " is already a member. Access restored." : "Invited " + email + ". They can sign in straight away.");
        invEmail.value = ""; invName.value = "";
        refresh();
      } catch (e) { toast(e.message, { type: "error" }); }
    });
  };
  view.append(el("div", { class: "tm-card" },
    el("div", { class: "tm-card-head" }, icon("user-plus", "i i-16"), "Invite a teammate"),
    el("div", { class: "tm-card-body" },
      el("div", { class: "tm-invite" }, invEmail, invName, invBtn),
      el("div", { class: "tm-invite-cap" }, "They can sign in straight away with that Google account."),
    )));

  /* members table */
  const tbl = el("table", { class: "table", "aria-label": "Members" },
    el("thead", {}, el("tr", {},
      el("th", {}, "Member"), el("th", {}, "Role"), el("th", {}, "Status"),
      el("th", {}, "Joined"), el("th", { style: "width:48px" }, ""))),
    el("tbody", {}));
  const tb = tbl.tBodies[0];
  for (const m of members) {
    const name = m.display_name || m.email;
    const sel = el("select", { class: "select", style: "width:110px", "aria-label": "Role for " + name },
      el("option", { value: "user" }, "Member"),
      el("option", { value: "admin" }, "Admin"));
    sel.value = m.role;
    sel.disabled = m.email === (window.me_email || "") || m.status === "pending";
    sel.onchange = async () => {
      const to = sel.value;
      const from = m.role;
      if (to === from) return;
      if (to === "admin") {
        const ok = await confirmSimple({
          title: "Make " + name + " an admin?",
          body: "They can manage the team, settings and sending.",
          actionLabel: "Make admin", danger: false,
        });
        if (!ok) { sel.value = from; return; }
      }
      try {
        await api("/api/admin/members", { body: { user_id: m.id, action: "role", role: to } });
        if (to === "user") {
          toast(name + " is now a member.", {
            action: () => api("/api/admin/members", { body: { user_id: m.id, action: "role", role: from } }).then(refresh),
          });
        } else {
          toast(name + " is now an admin.");
        }
        refresh();
      } catch (e) { toast(e.message, { type: "error" }); }
    };
    const kb = el("button", { class: "btn btn-icon", "aria-label": "Member actions" }, icon("more-vertical"));
    kb.onclick = () => menu(kb, [
      { icon: "trash-2", label: "Remove from team", danger: true, action: async () => {
        const ok = await confirmSimple({
          title: "Remove " + name + " from the team?",
          body: "Their personal settings are removed. Team data and Gmail accounts stay.",
          actionLabel: "Remove member",
        });
        if (!ok) return;
        try {
          await api("/api/admin/members", { body: { user_id: m.id, action: "remove" } });
          toast(name + " was removed.");
          refresh();
        } catch (e) { toast(e.message, { type: "error" }); }
      } },
    ]);
    const stBadge = m.status === "active" ? badge("Active", "active", "check-circle-2")
      : m.status === "pending" ? badge("Waiting", "waiting", "clock")
      : badge("Removed", "neutral", "circle-dashed");
    tb.append(el("tr", {},
      el("td", {}, el("div", { style: "display:flex;gap:10px;align-items:center" },
        el("span", { class: "avatar", style: `background:${avatarColor(name)}` }, initials(name)),
        el("div", {},
          el("div", { style: "font-weight:600" }, name),
          el("div", { class: "t-caption" }, m.email)))),
      el("td", {}, sel),
      el("td", {}, stBadge),
      el("td", { class: "t-caption" }, m.created_at ? fmtDay(m.created_at) : "—"),
      el("td", { style: "text-align:right" }, kb)));
  }
  view.append(el("div", { class: "table-wrap" }, tbl));
  view.append(el("div", { class: "tm-caption" }, "The admin email in Settings is always an admin."));
}
function refresh() {
  api("/api/admin/data").then((d) => {
    state.admin = d;
    const b = $("tm-req-badge");
    if (b) { b.textContent = String(d.requests.length); b.style.display = d.requests.length ? "" : "none"; }
    if (state.tab === "members") { view.innerHTML = ""; renderMembers(); }
  }).catch(() => {});
}

/* ================= results ================= */
function renderResults() {
  view.innerHTML = "";
  const r = { campaign: "all", member: "all", period: "all", q: "", shown: 50,
              summary: null, emails: [], members: [], campaigns: [], loading: true, error: null };

  const camSel = el("select", { class: "select", "aria-label": "Campaign" }, el("option", { value: "all" }, "All campaigns"));
  const memSel = el("select", { class: "select", "aria-label": "Member" }, el("option", { value: "all" }, "All members"));
  const periodSeg = el("div", { class: "seg", role: "group", "aria-label": "Period" },
    el("button", { class: "seg-item", "data-p": "7" }, "7 days"),
    el("button", { class: "seg-item", "data-p": "30" }, "30 days"),
    el("button", { class: "seg-item is-active", "data-p": "all" }, "All time"));
  periodSeg.querySelectorAll(".seg-item").forEach((b) => b.onclick = () => {
    r.period = b.dataset.p;
    periodSeg.querySelectorAll(".seg-item").forEach((x) => x.classList.toggle("is-active", x === b));
    load();
  });
  const q = el("div", { class: "search-input", style: "max-width:260px" },
    icon("search", "i i-16"),
    el("input", { class: "input", placeholder: "Search emails", "aria-label": "Search emails" }));
  let qt;
  q.querySelector("input").addEventListener("input", (e) => { clearTimeout(qt); qt = setTimeout(() => { r.q = e.target.value.trim(); load(); }, 250); });
  const expBtn = el("button", { class: "btn btn-ghost" }, icon("download", "i i-16"), "Export CSV");
  expBtn.onclick = () => exportEmailsCsv(r);

  view.append(
    el("div", { class: "tm-res-filters" }, camSel, memSel, periodSeg, q,
      el("span", { class: "push" }), expBtn),
    el("div", { id: "tm-res-body" }),
  );

  api("/api/campaigns").then((d) => {
    for (const c of d.campaigns) camSel.append(el("option", { value: String(c.id) }, c.name));
  }).catch(() => {});
  api("/api/admin/data").then((d) => {
    r.members = d.members.filter((m) => m.status !== "pending");
    for (const m of r.members) memSel.append(el("option", { value: m.email }, m.display_name || m.email));
  }).catch(() => {});
  camSel.onchange = () => { r.campaign = camSel.value; load(); };
  memSel.onchange = () => { r.member = memSel.value; load(); };

  async function load() {
    r.loading = true;
    const body = $("tm-res-body");
    if (body.children.length) body.append(el("div", { class: "t-caption t-faint", id: "tm-res-spin" }, "Updating…"));
    try {
      const params = new URLSearchParams();
      if (r.campaign !== "all") params.set("campaign_id", r.campaign);
      if (r.member !== "all") params.set("member", r.member);
      params.set("period", r.period);
      const params2 = new URLSearchParams();
      if (r.campaign !== "all") params2.set("campaign_id", r.campaign);
      if (r.member !== "all") params2.set("by", r.member);
      params2.set("sort", "time"); params2.set("dir", "desc");
      if (r.q) params2.set("q", r.q);
      const [sum, em] = await Promise.all([
        api("/api/reports/team-summary?" + params), api("/api/emails?" + params2),
      ]);
      r.summary = sum; r.emails = em.rows; r.error = null;
    } catch (e) { r.error = e; }
    r.loading = false;
    paint();
  }
  function paint() {
    const body = $("tm-res-body");
    body.innerHTML = "";
    const spin = body.querySelector("#tm-res-spin");
    if (spin) spin.remove();
    if (r.error) { body.append(errorCard("Could not load results.", { onRetry: load })); return; }
    const t = r.summary.totals;
    body.append(el("div", { class: "tm-tiles" },
      resTile("Emails sent", fmtInt(t.n_sent)),
      resTile("Replies", fmtInt(t.n_replied)),
      resTile("Reply rate", t.n_sent ? fmtPct(t.rate) : "—"),
      resTile("Bounced", fmtInt(t.n_bounced)),
    ));
    /* by member */
    if (r.summary.by_member.length) {
      const lead = Math.max(...r.summary.by_member.map((x) => x.rate));
      body.append(el("div", { class: "tm-section-title" }, "By team member"));
      const t2 = el("table", { class: "table", "aria-label": "Results by member" },
        el("thead", {}, el("tr", {},
          el("th", {}, "Member"), el("th", { class: "num" }, "Emails"), el("th", { class: "num" }, "Sent"),
          el("th", { class: "num" }, "Replied"), el("th", { class: "num" }, "Bounced"), el("th", { class: "num" }, "Reply rate"))),
        el("tbody", {}));
      const tb = t2.tBodies[0];
      for (const m of r.summary.by_member) {
        tb.append(el("tr", {},
          el("td", {}, m.member),
          el("td", { class: "tm-num" }, fmtInt(m.n_emails)),
          el("td", { class: "tm-num" }, fmtInt(m.n_sent)),
          el("td", { class: "tm-num" }, fmtInt(m.n_replied)),
          el("td", { class: "tm-num" }, fmtInt(m.n_bounced)),
          el("td", { class: "tm-num" + (m.rate === lead && lead > 0 ? " lead" : "") }, m.n_sent ? fmtPct(m.rate) : "—")));
      }
      body.append(el("div", { class: "table-wrap" }, t2));
    }
    /* top companies */
    if (r.summary.by_company.length) {
      body.append(el("div", { class: "tm-section-title" }, "Top companies"));
      const t3 = el("table", { class: "table", "aria-label": "Top companies" },
        el("thead", {}, el("tr", {},
          el("th", {}, "Company"), el("th", { class: "num" }, "Emails"), el("th", { class: "num" }, "Replied"), el("th", { class: "num" }, "Reply rate"))),
        el("tbody", {}));
      const tb = t3.tBodies[0];
      for (const c of r.summary.by_company) {
        tb.append(el("tr", {},
          el("td", {}, c.company),
          el("td", { class: "tm-num" }, fmtInt(c.n_emails)),
          el("td", { class: "tm-num" }, fmtInt(c.n_replied)),
          el("td", { class: "tm-num" }, c.n_sent ? fmtPct(c.rate) : "—")));
      }
      body.append(el("div", { class: "table-wrap" }, t3));
    }
    /* all emails */
    body.append(el("div", { class: "tm-section-title" }, "All emails"));
    const t4 = el("table", { class: "table", "aria-label": "All emails" },
      el("thead", {}, el("tr", {},
        el("th", {}, "Recipient"), el("th", {}, "Subject"), el("th", {}, "Status"),
        el("th", {}, "When"), el("th", {}, "By"))),
      el("tbody", { id: "tm-em-rows" }));
    const rows = t4.tBodies[0];
    const shown = r.emails.slice(0, r.shown);
    if (!shown.length) {
      rows.append(el("tr", {}, el("td", { colspan: 5 }, el("div", { class: "t-caption", style: "padding:8px" },
        r.q || r.campaign !== "all" || r.member !== "all" ? "No emails match the filters." : "No emails in this period."))));
    }
    for (const e of shown) {
      rows.append(el("tr", {},
        el("td", {}, el("div", { style: "font-weight:600" }, e.company || e.to_addr || "—"),
          el("div", { class: "t-caption" }, e.hr_name || "")),
        el("td", { class: "t-caption" }, e.subject || "—"),
        el("td", {}, statusBadge(e)),
        el("td", { class: "t-caption" }, e.sent_at ? fmtDayTime(e.sent_at) : (e.created_at ? fmtDay(e.created_at) : "—")),
        el("td", { class: "t-caption" }, e.member || "—")));
    }
    body.append(el("div", { class: "table-wrap" }, t4));
    if (r.emails.length > r.shown) {
      const more = el("button", { class: "btn btn-secondary btn-sm" }, "Show 50 more");
      more.onclick = () => { r.shown += 50; paint(); };
      body.append(el("div", { class: "show-more-wrap" }, more,
        el("span", { class: "t-caption t-faint" }, "Showing " + fmtInt(r.shown) + " of " + fmtInt(r.emails.length))));
    }
  }
  load();
}
function resTile(label, value) {
  return el("div", { class: "stat" },
    el("div", { class: "stat-sub" }, label),
    el("div", { class: "stat-value" }, value));
}
function exportEmailsCsv(r) {
  if (!r.emails.length) { toast("Nothing to export.", { type: "info" }); return; }
  const hdrs = ["No.", "Company", "HR name", "Email", "Subject", "Status", "Sent at", "By", "Campaign"];
  const lines = [hdrs];
  for (const e of r.emails) {
    lines.push([e.serial, e.company, e.hr_name, e.to_addr, e.subject,
      statusOf(e), e.sent_at || "", e.member || "", e.campaign_name || ""]);
  }
  const csv = lines.map((row) => row.map((v) => '"' + String(v ?? "").replace(/"/g, '""') + '"').join(",")).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = "campusmail_results.csv";
  a.click();
  URL.revokeObjectURL(a.href);
  toast("Results exported as CSV.");
}

/* ================= activity ================= */
const ACT_META = {
  campaign_create: ["send", "created a campaign"], campaign_update: ["pencil", "updated a campaign"],
  campaign_delete: ["trash-2", "deleted a campaign"], batch_create: ["send", "created a send"],
  batch_reminders: ["bell", "set follow-ups on a send"], batch_attachments: ["paperclip", "changed attachments on a send"],
  drafts_create: ["file-pen", "created drafts"], send_now: ["send", "sent emails"],
  schedule: ["clock", "scheduled emails"], emails_cancel: ["pause-circle", "stopped emails"],
  emails_revert: ["undo-2", "restored stopped emails"], email_update: ["pencil", "edited an email"],
  email_ai_polish: ["sparkles", "polished an email with AI"], reply_sent: ["reply", "sent a reply"],
  reply_draft: ["file-pen", "saved a reply draft"], mark_replied: ["check-circle-2", "marked a reply"],
  contact_add: ["user-plus", "added a contact"], contact_delete: ["trash-2", "removed contacts"],
  import: ["upload", "imported contacts"], sheet_upload: ["upload", "imported a list"],
  sheet_delete: ["trash-2", "deleted a list"], sheet_campaign: ["send", "linked a list to a campaign"],
  sheet_update: ["pencil", "renamed a list"], practice_list: ["file-spreadsheet", "opened the practice list"],
  template_add: ["file-text", "added a template"], template_ai: ["sparkles", "wrote a template with AI"],
  template_ai_reminder: ["sparkles", "wrote a follow-up with AI"], template_ai_polish: ["sparkles", "polished a template with AI"],
  template_activate: ["check-circle-2", "set a template as default"], template_delete: ["trash-2", "deleted a template"],
  signature_save: ["pencil", "updated the signature"], signature_image: ["image", "added a signature image"],
  settings: ["settings", "changed settings"], gmail_account_add: ["mail", "connected a Gmail account"],
  gmail_account_removed: ["trash-2", "removed a Gmail account"], relay: ["globe", "changed cloud scheduling"],
  storage_migrate: ["folder-open", "moved file storage"], deploy: ["zap", "updated the app"],
  account_visibility: ["shield-check", "changed an account's sharing"], invite: ["user-plus", "invited someone"],
  request_approved: ["check-circle-2", "approved a request"], request_denied: ["x-circle", "declined a request"],
  request_reverted: ["undo-2", "reverted a request"], role_changed: ["users", "changed a role"],
  member_removed: ["trash-2", "removed a member"], handled: ["check-circle-2", "handled a reply"],
  email_draft: ["file-pen", "saved drafts"], contact_import: ["upload", "imported contacts"],
  send: ["send", "sent emails"], email_reply: ["reply", "replied to a recruiter"],
  reply_detected: ["mail", "detected a reply"],
};

function renderActivity() {
  view.innerHTML = "";
  const a = { person: "all", type: "all", q: "", limit: 50, rows: [], loading: true, error: null };
  const personSel = el("select", { class: "select", "aria-label": "Person" }, el("option", { value: "all" }, "Everyone"));
  for (const m of state.admin.members) personSel.append(el("option", { value: m.email }, m.display_name || m.email));
  const typeSel = el("select", { class: "select", "aria-label": "Type" },
    el("option", { value: "all" }, "All types"),
    el("option", { value: "campaigns" }, "Campaigns"),
    el("option", { value: "sending" }, "Sending"),
    el("option", { value: "drafts" }, "Drafts"),
    el("option", { value: "contacts" }, "Contacts"),
    el("option", { value: "templates" }, "Templates"),
    el("option", { value: "settings" }, "Settings"),
    el("option", { value: "team" }, "Team"));
  const q = el("div", { class: "search-input", style: "max-width:240px" },
    icon("search", "i i-16"),
    el("input", { class: "input", placeholder: "Search activity", "aria-label": "Search activity" }));
  const body = el("div", { id: "tm-act-body" });
  const moreWrap = el("div", { class: "show-more-wrap", style: "display:none" },
    el("button", { class: "btn btn-secondary btn-sm", id: "tm-act-more" }, "Show 50 more"));
  view.append(
    el("div", { class: "tm-act-filters" }, personSel, typeSel, q),
    body, moreWrap,
  );
  personSel.onchange = () => { a.person = personSel.value; load(); };
  typeSel.onchange = () => { a.type = typeSel.value; load(); };
  let qt;
  q.querySelector("input").addEventListener("input", (e) => { clearTimeout(qt); qt = setTimeout(() => { a.q = e.target.value.trim(); load(); }, 250); });
  $("tm-act-more").onclick = () => { a.limit += 50; load(true); };

  async function load(append) {
    if (!append) { a.loading = true; body.innerHTML = ""; body.append(el("div", { class: "skel-row" }), el("div", { class: "skel-row" }), el("div", { class: "skel-row" })); }
    try {
      const p = new URLSearchParams();
      if (a.person !== "all") p.set("person", a.person);
      if (a.type !== "all") p.set("type", a.type);
      if (a.q) p.set("q", a.q);
      p.set("limit", String(a.limit));
      const d = await api("/api/activity?" + p);
      a.rows = d.rows;
      a.error = null;
    } catch (e) { a.error = e; }
    a.loading = false;
    paint();
  }
  function dayLabel(ts) {
    if (!ts) return "";
    const d = new Date();
    const t = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const that = new Date();
    const m = String(ts).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m) that.setFullYear(+m[1], +m[2] - 1, +m[3]);
    const days = Math.round((t - that) / 86400000);
    if (days === 0) return "Today";
    if (days === 1) return "Yesterday";
    const dd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][that.getDay()];
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return dd + " " + that.getDate() + " " + months[that.getMonth()];
  }
  function paint() {
    body.innerHTML = "";
    if (a.error) { body.append(errorCard("Could not load activity.", { onRetry: () => load() })); return; }
    if (!a.rows.length) {
      body.append(emptyState({ icon: "inbox", title: "Nothing here yet",
        text: "Activity shows up here as your team works: sends, drafts, contacts and settings." }));
      return;
    }
    let lastDay = null;
    for (const r of a.rows) {
      const dl = dayLabel(r.ts);
      if (dl !== lastDay) {
        lastDay = dl;
        body.append(el("div", { class: "tm-day" }, dl));
      }
      const [ico, verb] = ACT_META[r.action] || ["info", r.action.replace(/_/g, " ")];
      const actor = r.actor === "system" ? "The system" : (r.actor.includes("@") ? nameFromEmail(r.actor) : r.actor);
      const row = el("div", { class: "tm-act-row" },
        icon(ico, "i i-16"),
        el("div", { class: "tm-act-main" },
          el("b", {}, actor), " " + verb,
          (r.detail || "").trim() ? el("details", { class: "tm-act-detail" },
            el("summary", {}, "Details"), el("div", { class: "t-mono" }, r.detail)) : null),
        el("span", { class: "tm-act-time" }, r.ts ? fmtTime(r.ts) : ""),
      );
      body.append(row);
    }
    moreWrap.style.display = a.rows.length >= a.limit ? "" : "none";
  }
  function nameFromEmail(email) {
    const m = state.admin.members.find((x) => x.email === email);
    return m && m.display_name ? m.display_name : email.split("@")[0];
  }
  load();
}

/* ================= tab wiring ================= */
document.querySelectorAll("#tm-tabs .tab").forEach((t) => {
  t.addEventListener("click", (e) => {
    e.preventDefault();
    if (t.dataset.tab === state.tab) return;
    state.tab = t.dataset.tab;
    history.replaceState(null, "", "/team?tab=" + state.tab);
    document.querySelectorAll("#tm-tabs .tab").forEach((x) => x.setAttribute("aria-selected", String(x === t)));
    if (state.tab === "members") renderMembers();
    else if (state.tab === "results") renderResults();
    else renderActivity();
  });
});

init();
