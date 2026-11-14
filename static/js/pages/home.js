/* Home: what needs you today + one next best action (6.4). */
import { api, el, icon, toast, statusBadge, badge, emptyState, errorCard, esc } from "/static/js/core/ui.js";
import { greeting, fmtInt, fmtPct, relTime, upcomingWhen, initials, avatarColor } from "/static/js/core/format.js";
import { CAMPAIGN_STATUS } from "/static/js/core/copy.js";

const root = document.getElementById("home-root");
let _firstPaint = true;

/* ---------- skeletons (loading state) ---------- */
function skeletons() {
  root.innerHTML = "";
  root.append(
    el("div", { class: "page-head" },
      el("div", { class: "skel", style: "width:240px;height:32px" }),
      el("div", { class: "skel", style: "width:340px;height:16px;margin-top:8px" }),
      el("div", { class: "push", style: "flex:none" }, el("div", { class: "skel", style: "width:120px;height:40px;border-radius:10px" }))),
    el("div", { class: "stat-row", style: "margin-bottom:24px" },
      ...[0, 1, 2, 3].map(() => el("div", { class: "skel", style: "height:86px;border-radius:14px" }))),
    el("div", { class: "grid-12" },
      el("div", { class: "span-7" }, el("div", { class: "skel", style: "height:220px;border-radius:14px" })),
      el("div", { class: "span-5" }, el("div", { class: "skel", style: "height:220px;border-radius:14px" }))),
    el("div", { style: "margin-top:32px" },
      el("div", { class: "skel", style: "height:24px;width:180px;margin-bottom:16px" }),
      el("div", { class: "grid-12" },
        ...[0, 1, 2, 3].map(() => el("div", { class: "span-6" }, el("div", { class: "skel", style: "height:120px;border-radius:14px" }))))),
  );
}

/* ---------- number count-up (600 ms, once) ---------- */
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

/* ---------- onboarding card (6.3) ---------- */
function onboardCard(ob) {
  const rows = [
    { done: ob.gmail, label: "Connect a Gmail account to send from", doneLabel: "A send account is ready", btn: "Connect Gmail", href: "/settings" },
    { done: ob.contacts, label: "Add your contact list", btn: "Import a list", href: "/contacts" },
    { done: ob.templates, label: "Choose or write an email template", btn: "Open templates", href: "/templates" },
    { done: ob.first_send, label: "Save your first drafts", btn: "Send emails", href: "/send" },
  ];
  const done = rows.filter((r) => r.done).length;
  if (done === 4) {
    return el("div", { class: "banner banner-success", style: "margin-bottom:24px" },
      icon("check-circle-2"), el("div", { style: "color:var(--text)" }, "You are all set. Start a new send whenever you like."));
  }
  let nextShown = false;
  const card = el("div", { class: "card", style: "margin-bottom:24px" },
    el("div", { class: "onboard" },
      el("div", { class: "onboard-ring" },
        el("svg", { class: "progress-ring", viewBox: "0 0 56 56" },
          el("circle", { class: "ring-bg", cx: 28, cy: 28, r: 24 }),
          el("circle", { class: "ring-fg", cx: 28, cy: 28, r: 24, "stroke-dasharray": "150.8", "stroke-dashoffset": String(150.8 * (1 - done / 4)), transform: "rotate(-90 28 28)" }),
          el("text", { x: 28, y: 32, "text-anchor": "middle" }, done + " of 4"))),
      el("div", { style: "flex:1" },
        el("div", { class: "t-card mb-4" }, "Get ready to send your first emails"),
        el("div", { class: "onboard-rows" },
          ...rows.map((r) => {
            const isNext = !r.done && !nextShown;
            if (isNext) nextShown = true;
            return el("div", { class: "onboard-row" + (r.done ? " is-done" : "") },
              el("span", { class: "i-check" }, icon("check", "i")),
              el("span", { class: "or-text" }, r.done ? r.doneLabel || r.label : r.label),
              r.done ? null : isNext
                ? el("a", { class: "btn btn-sm btn-primary", href: r.href }, r.btn)
                : el("span", { class: "or-later" }, "Do this later"));
          }),
        el("div", { class: "mt-3" },
          el("a", { class: "t-small", style: "color:var(--primary-600);font-weight:550", href: "#practice", id: "practice-link"},
            "Not ready? Try it with a practice list."))),
    )),
  );
  card.querySelector("#practice-link").addEventListener("click", async (e) => {
    e.preventDefault();
    try {
      const d = await api("/api/sheets/practice", { body: {} });
      toast("Practice list created. Every email in it is addressed to you.", { type: "success", action: () => { location.href = "/send"; }, actionLabel: "Send to it" });
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  });
  return card;
}

/* ---------- attention card ---------- */
const ATTN = {
  replies: { cls: "reply", icon: "reply", text: (n) => `${n} ${n === 1 ? "reply" : "replies"} ${n === 1 ? "is" : "are"} waiting`, btn: "Open replies", href: "/replies" },
  bounced: { cls: "bounced", icon: "mail-x", text: (n) => `${n} ${n === 1 ? "email" : "emails"} ${n === 1 ? "bounced" : "bounced"}`, btn: "Fix addresses", href: "/replies?tab=bounced" },
  failed: { cls: "failed", icon: "alert-triangle", text: (n) => `${n} ${n === 1 ? "email" : "emails"} ${n === 1 ? "failed" : "failed"} to send`, btn: "See why", href: "/campaigns" },
  soon: { cls: "soon", icon: "clock", text: (n) => `${n} ${n === 1 ? "email" : "emails"} go out in the next 24 hours`, btn: "Review", href: "/campaigns" },
  stale_drafts: { cls: "drafts", icon: "file-pen", text: (n) => `${n} ${n === 1 ? "draft" : "drafts"} older than 2 days have not been sent`, btn: "Open Gmail", href: "https://mail.google.com" },
  gmail: { cls: "gmail", icon: "mail", text: () => "Connect a Gmail account to send from", btn: "Connect", href: "/settings" },
  requests: { cls: "requests", icon: "user-plus", text: (n) => `${n} ${n === 1 ? "person is" : "people are"} waiting to join your team`, btn: "Review requests", href: "/team" },
};

function attentionCard(items) {
  const body = el("div");
  const shown = items.slice(0, 5);
  if (shown.length === 0) {
    body.append(el("div", { class: "empty" },
      el("div", { class: "empty-illust" }, `<svg viewBox="0 0 96 96" width="96" height="96" aria-hidden="true"><circle cx="48" cy="48" r="26" fill="var(--primary-100)" stroke="none"/><path d="M36 49l9 9 17-19" fill="none" stroke="var(--primary-500)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`),
      el("div", { class: "empty-title" }, "You are all caught up"),
      el("a", { class: "btn btn-secondary", href: "/send" }, "Start a new send")));
  } else {
    for (const it of shown) {
      const cfg = ATTN[it.type] || ATTN.replies;
      body.append(el("div", { class: "attn-row" },
        el("div", { class: "attn-ico " + cfg.cls }, icon(cfg.icon)),
        el("div", { class: "attn-text" },
          el("div", {}, el("b", {}, String(it.count)), " " + cfg.text(it.count).replace(/^\d+ /, "")),
          it.campaign ? el("div", { class: "attn-sub" }, it.campaign) : null),
        el("a", { class: "btn btn-sm btn-secondary push", href: cfg.href }, cfg.btn)));
    }
    if (items.length > 5) {
      const more = el("button", { class: "btn btn-ghost btn-sm mt-2" }, `Show all ${items.length}`);
      more.addEventListener("click", () => {
        body.innerHTML = "";
        for (const it of items) body.append(attentionRow(it));
      });
      body.append(more);
    }
  }
  return el("div", { class: "card card-flush span-7", style: "padding:8px 20px" },
    el("div", { class: "t-card", style: "padding:12px 0 4px" }, "Needs your attention"),
    body);
}
function attentionRow(it) {
  const cfg = ATTN[it.type] || ATTN.replies;
  return el("div", { class: "attn-row" },
    el("div", { class: "attn-ico " + cfg.cls }, icon(cfg.icon)),
    el("div", { class: "attn-text" },
      el("div", {}, el("b", {}, String(it.count)), " " + cfg.text(it.count).replace(/^\d+ /, "")),
      it.campaign ? el("div", { class: "attn-sub" }, it.campaign) : null),
    el("a", { class: "btn btn-sm btn-secondary push", href: cfg.href }, cfg.btn));
}

/* ---------- upcoming sends ---------- */
function upcomingCard(items) {
  const body = el("div");
  if (items.length === 0) {
    body.append(el("div", { class: "t-small t-faint", style: "padding:12px 0" }, "Nothing scheduled."));
  } else {
    for (const u of items) {
      body.append(el("div", { class: "up-row" },
        el("div", { class: "up-when" }, upcomingWhen(u.when)),
        el("a", { class: "up-name", href: "/campaigns/" + (u.campaign_id || "") }, u.campaign),
        el("div", { class: "up-count" }, fmtInt(u.count) + " " + (u.count === 1 ? "email" : "emails")),
        el("button", { class: "btn btn-icon", style: "width:32px;height:32px", "aria-label": "Actions for this send" }, icon("ellipsis", "i i-16"))));
    }
  }
  return el("div", { class: "card card-flush span-5", style: "padding:8px 20px" },
    el("div", { class: "t-card", style: "padding:12px 0 4px" }, "Upcoming sends"),
    body);
}

/* ---------- campaign cards ---------- */
function campaignStatus(cam) {
  const st = cam.status_counts || {};
  const total = cam.n_emails || 0;
  const pending = st.pending || 0;
  if (total === 0) return "draft";
  const live = (st.ready || 0) + (st.drafted || 0) + (st.scheduled || 0) + (st.failed || 0);
  const last = cam.last ? new Date(cam.last.replace(" ", "T")) : null;
  const quiet14 = last && (Date.now() - last.getTime() > 14 * 86400000);
  if (live === 0 && quiet14) return "finished";
  return "active";
}

function campaignCards(cams) {
  if (cams.length === 0) {
    return el("div", { class: "card", style: "padding:8px 20px" },
      el("div", { class: "card-head" }, el("h2", { class: "t-card" }, "Your campaigns")),
      el("div", { class: "empty" },
        el("div", { class: "empty-illust" }, `<svg viewBox="0 0 96 96" width="96" height="96" aria-hidden="true"><path d="M18 74 L78 26" stroke="var(--primary-200)" stroke-width="2.4" stroke-dasharray="1 8" stroke-linecap="round"/><path d="M14 78 L58 44 L52 66 L38 60 Z" fill="var(--primary-100)" stroke="none"/><path d="M58 44 L80 22 M58 44 l-4 16 M58 44 l16-6" fill="none" stroke="var(--primary-500)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`),
        el("div", { class: "empty-title" }, "Your campaigns will appear here"),
        el("a", { class: "btn btn-primary", href: "/campaigns?new=1" }, "Create your first campaign")));
  }
  const wrap = el("div", { class: "grid-12 stagger" });
  for (const cam of cams) {
    const emailed = Math.max(0, cam.n_emails - (cam.status_counts?.pending || 0));
    const pct = cam.n_contacts ? Math.min(100, Math.round(100 * emailed / cam.n_contacts)) : (emailed ? 100 : 0);
    const status = campaignStatus(cam);
    const card = el("a", { class: "card card-flush card-clickable span-6", href: "/campaigns/" + cam.id, style: "padding:20px;display:block" },
      el("div", { class: "cc-head" },
        el("span", { class: "cc-name" }, cam.name),
        el("span", { class: "push" }, campaignBadge(status))),
      cam.n_contacts
        ? el("div", { class: "cc-line" }, `${fmtInt(emailed)} of ${fmtInt(cam.n_contacts)} contacts emailed`)
        : el("div", { class: "cc-line t-faint" }, "No contact list yet"),
      el("div", { class: "progress" }, el("span", { style: `width:${pct}%` })),
      el("div", { class: "cc-figs" },
        el("span", {}, "Sent ", el("b", { class: "cc-sent" }, fmtInt(cam.sent))),
        el("span", {}, "Replied ", el("b", { class: "cc-replied" }, fmtInt(cam.replied))),
        el("span", {}, "Reply rate ", el("b", {}, fmtPct(cam.rate)))),
      el("div", { class: "cc-foot" },
        el("span", { class: "t-caption t-faint" }, "Active " + relTime(cam.last)),
        el("span", { class: "btn btn-sm btn-secondary", id: "" }, "Send emails")));
    card.querySelector(".btn").addEventListener("click", (e) => { e.preventDefault(); location.href = "/campaigns/" + cam.id + "/send"; });
    wrap.append(card);
  }
  const head = el("div", { class: "row", style: "margin-bottom:16px" },
    el("h2", { class: "t-card" }, "Your campaigns"),
    el("a", { class: "btn btn-ghost btn-sm push", href: "/campaigns" }, "View all"));
  return el("div", { style: "margin-top:32px" }, head, wrap);
}
function campaignBadge(status) {
  const info = CAMPAIGN_STATUS[status];
  return badge(info.label, info.cls, info.icon);
}

/* ---------- main render ---------- */
async function load() {
  skeletons();
  let d;
  try {
    d = await api("/api/home");
  } catch (e) {
    root.innerHTML = "";
    root.append(errorCard("We could not load your dashboard.", {
      onRetry: load,
      detail: e.message,
    }));
    return;
  }
  root.innerHTML = "";
  root.append(
    el("div", { class: "page-head" },
      el("div", { class: "ph-main" },
        el("div", { class: "home-greet", id: "home-greet" }, "…"),
        el("p", { class: "home-greet-sub", id: "home-greet-sub" })),
      el("div", { class: "push", style: "flex:none" }, el("a", { class: "btn btn-primary", href: "/send" }, "Send emails"))),
    el("div", { id: "home-onboard" }),
    el("div", { class: "stat-row mb-6 hide", id: "home-tiles", style: "margin-bottom:24px" }),
    el("div", { class: "grid-12 mb-6", id: "home-cards", style: "margin-bottom:32px" }),
    el("div", { id: "home-campaigns" }),
  );

  // greeting
  const meName = (window.me_name || "").split(" ")[0];
  const tiles = d.tiles;
  const attention = d.attention || [];
  const replyN = attention.find((a) => a.type === "replies")?.count || 0;
  const failN = attention.find((a) => a.type === "failed")?.count || 0;
  const bouncedN = attention.find((a) => a.type === "bounced")?.count || 0;
  const soon = d.upcoming || [];
  let sub;
  if (replyN) sub = `<b>${replyN}</b> ${replyN === 1 ? "reply" : "replies"} ${replyN === 1 ? "needs" : "need"} an answer${failN ? `. ${failN} ${failN === 1 ? "email" : "emails"} failed to send` : ""}.`;
  else if (failN) sub = `<b>${failN}</b> ${failN === 1 ? "email" : "emails"} ${failN === 1 ? "failed" : "failed"} to send.`;
  else if (bouncedN) sub = `<b>${bouncedN}</b> ${bouncedN === 1 ? "email" : "emails"} ${bouncedN === 1 ? "bounced" : "bounced"}.`;
  else if (soon.length) sub = `<b>${fmtInt(soon[0].count)}</b> emails go out ${upcomingWhen(soon[0].when).toLowerCase()}.`;
  else sub = "You are all caught up.";
  root.querySelector("#home-greet").textContent = greeting() + (meName ? ", " + meName : "") + ".";
  root.querySelector("#home-greet-sub").innerHTML = sub;

  // onboarding (until all four rows done)
  const ob = d.onboard || {};
  const allDone = ob.gmail && ob.contacts && ob.templates && ob.first_send;
  if (!allDone) root.querySelector("#home-onboard").append(onboardCard(ob));

  // tiles (hidden until at least one email sent)
  if (tiles.sent > 0) {
    const tilesEl = root.querySelector("#home-tiles");
    tilesEl.classList.remove("hide");
    const defs = [
      ["Emails sent", tiles.sent, "30 days", "/replies", true],
      ["Replies", tiles.replies, "waiting: " + replyN, "/replies", true],
      ["Reply rate", null, "of " + fmtInt(tiles.sent) + " sent", null, false],
      ["Scheduled", tiles.scheduled, soon[0] ? "next " + upcomingWhen(soon[0].when).toLowerCase() : "none yet", "/campaigns", true],
      ["Bounced", tiles.bounced, tiles.bounced ? "need fixing" : "none", "/replies?tab=bounced", true],
    ];
    for (const [label, val, sub2, href, isLink] of defs) {
      const valueNode = el("span", { class: "stat-value" }, "0");
      const t = el(isLink ? "button" : "div", { class: "stat" + (isLink ? " stat-link" : "") },
        el("span", { class: "t-overline" }, label),
        valueNode,
        el("span", { class: "stat-sub" }, sub2));
      if (isLink && href) t.addEventListener("click", () => location.href = href);
      tilesEl.append(t);
      if (label === "Reply rate") {
        const pct = tiles.rate;
        if (window.matchMedia("(prefers-reduced-motion: no-preference)").matches && _firstPaint) {
          const start = performance.now();
          const step = (tt) => {
            const p = Math.min(1, (tt - start) / 600);
            valueNode.textContent = (pct * (1 - Math.pow(1 - p, 3))).toFixed(1).replace(/\.0$/, "") + "%";
            if (p < 1) requestAnimationFrame(step);
          };
          requestAnimationFrame(step);
        } else valueNode.textContent = fmtPct(pct);
      } else if (_firstPaint) {
        countUp(valueNode, val);
      } else valueNode.textContent = fmtInt(val);
    }
  }

  // attention + upcoming
  const cardsEl = root.querySelector("#home-cards");
  cardsEl.append(attentionCard(attention), upcomingCard(d.upcoming || []));

  // campaigns
  root.querySelector("#home-campaigns").append(campaignCards(d.campaigns || []));
  _firstPaint = false;
}

load();
