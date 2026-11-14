/* Replies (plan 9.1) — inbox-style triage. List left, conversation right,
   keyboard friendly. Needs reply = replied without handled_at. Bounced gets
   a fix panel. Glossary: sent/list/No. Sentence case, no exclamation marks. */
import {
  api, el, icon, esc, toast, modal, menu, confirmSimple, badge,
  errorCard, emptyState, withLoading,
} from "/static/js/core/ui.js";
import { MSG, plural, statusOf, bounceReason } from "/static/js/core/copy.js";
import { fmtInt, fmtDayTime, relTime, initials, avatarColor } from "/static/js/core/format.js";

const st = {
  tab: new URLSearchParams(location.search).get("tab") || "needs",
  campaign: "all",
  member: "all",
  replied: [], bounced: [], all: [],
  selectedId: Number(new URLSearchParams(location.search).get("sel")) || null,
  loading: true,
  error: null,
};
const TABS = ["needs", "done", "bounced", "all"];

const $ = (id) => document.getElementById(id);
const layout = $("rp-layout");
const listScroll = $("rp-list-scroll");
const conv = $("rp-conv");

/* ================= data ================= */
async function load() {
  st.loading = true;
  renderListSkeleton();
  renderConvSkeleton();
  try {
    const [re, bo, al, cams] = await Promise.all([
      api("/api/emails?status=replied&sort=replied&dir=desc"),
      api("/api/emails?status=bounced"),
      api("/api/emails?status=sent&sort=time&dir=desc"),
      api("/api/campaigns"),
    ]);
    st.replied = re.rows;
    st.bounced = bo.rows;
    st.all = al.rows;
    st.error = null;
    fillCampaignFilter(cams.campaigns);
  } catch (e) {
    st.error = e;
  }
  st.loading = false;
  const inList = currentList().some((r) => r.id === st.selectedId);
  if (!inList) st.selectedId = currentList()[0]?.id ?? null;
  syncUrl();
  render();
}

function needs() { return st.replied.filter((r) => !r.handled_at); }
function done() { return st.replied.filter((r) => r.handled_at); }
function currentList() {
  let rows;
  if (st.tab === "needs") rows = needs();
  else if (st.tab === "done") rows = done();
  else if (st.tab === "bounced") rows = st.bounced;
  else rows = st.all;
  if (st.campaign !== "all") rows = rows.filter((r) => String(r.campaign_id || 0) === st.campaign);
  if (st.member !== "all") rows = rows.filter((r) => (r.member || "") === st.member);
  return rows;
}
function selectedRow() {
  const all = [...st.replied, ...st.bounced, ...st.all];
  return all.find((r) => r.id === st.selectedId) || null;
}
function syncUrl() {
  const p = new URLSearchParams();
  if (st.tab !== "needs") p.set("tab", st.tab);
  if (st.selectedId) p.set("sel", st.selectedId);
  history.replaceState(null, "", location.pathname + (p.toString() ? "?" + p : ""));
}

/* ================= list ================= */
function renderListSkeleton() {
  listScroll.innerHTML = "";
  for (let i = 0; i < 6; i++) {
    listScroll.append(el("div", { class: "rp-row" },
      el("span", { class: "skel", style: "width:32px;height:32px;border-radius:50%;flex:none" }),
      el("div", { style: "flex:1" }, el("div", { class: "skel", style: "width:60%;margin-bottom:6px" }),
        el("div", { class: "skel", style: "width:38%" })),
      el("span", { class: "skel", style: "width:36px" }),
    ));
  }
}

function renderList() {
  const rows = currentList();
  listScroll.innerHTML = "";
  // segment counts
  const counts = { needs: needs().length, done: done().length, bounced: st.bounced.length, all: st.all.length };
  $("rp-n-needs").textContent = fmtInt(counts.needs);
  $("rp-n-done").textContent = fmtInt(counts.done);
  $("rp-n-bounced").textContent = fmtInt(counts.bounced);
  $("rp-n-all").textContent = fmtInt(counts.all);
  document.querySelectorAll("#rp-seg .seg-item").forEach((b) =>
    b.classList.toggle("is-active", b.dataset.tab === st.tab));

  if (st.error) {
    listScroll.append(errorCard("Could not load replies.", { onRetry: load }));
    return;
  }
  if (!rows.length) {
    if (st.tab === "needs" && st.replied.length) {
      listScroll.append(emptyState({ icon: "success", title: "No replies waiting for you.",
        text: "When a recruiter writes back, the reply lands here.",
        secondary: el("a", { class: "empty-link", href: "/replies?tab=done" }, "See handled replies") }));
    } else if (st.tab === "done") {
      listScroll.append(emptyState({ icon: "check-circle-2", title: "Nothing here yet.",
        text: "Replies you mark as done are kept here." }));
    } else if (st.tab === "bounced") {
      listScroll.append(emptyState({ icon: "mail-x", title: "No bounced emails.",
        text: "When an address does not work, the email appears here for a quick fix." }));
    } else if (st.all.length === 0 && st.replied.length === 0 && st.bounced.length === 0) {
      listScroll.append(emptyState({ icon: "replies", title: "Replies show up here once you send emails.",
        text: "Send a campaign and the replies come back to this inbox.",
        primary: el("a", { class: "btn btn-primary", href: "/campaigns" }, "Send emails") }));
    } else {
      listScroll.append(emptyState({ icon: "search", title: "No emails match",
        text: "Try a different campaign or member.",
        primary: el("button", { class: "btn btn-secondary", onclick: clearFilters }, "Clear filters") }));
    }
    return;
  }
  $("rp-list-foot").style.display = "";
  for (const r of rows) {
    const isNeeds = r.replied_at && !r.handled_at;
    const row = el("div", {
      class: "rp-row" + (r.id === st.selectedId ? " is-selected" : ""),
      role: "button", tabindex: "0",
      "data-id": r.id,
      onclick: () => select(r.id),
      onkeydown: (e) => { if (e.key === "Enter") select(r.id); },
    },
      el("span", { class: "avatar", style: `background:${avatarColor(r.company || r.to_addr)}` },
        initials(r.company || r.hr_name || "?")),
      el("div", { class: "rp-row-main" },
        el("div", { class: "rp-row-co" }, r.company || r.to_addr || "Unknown"),
        el("div", { class: "rp-row-sub" }, [r.hr_name, r.campaign_name].filter(Boolean).join(" · "))),
      isNeeds ? el("span", { class: "rp-dot", "aria-label": "Needs reply" }) : null,
      el("span", { class: "rp-row-time" },
        relTime(st.tab === "bounced" ? (r.bounced_at || r.sent_at) : (r.replied_at || r.sent_at) || r.created_at)),
    );
    listScroll.append(row);
  }
}

function clearFilters() {
  st.campaign = "all"; st.member = "all";
  $("rp-campaign").value = "all"; $("rp-member").value = "all";
  load();
}

function fillCampaignFilter(campaigns) {
  const sel = $("rp-campaign");
  sel.innerHTML = "";
  sel.append(el("option", { value: "all" }, "All campaigns"));
  for (const c of campaigns) sel.append(el("option", { value: String(c.id) }, c.name));
  sel.value = st.campaign;
  sel.onchange = () => { st.campaign = sel.value; load(); };
  const ms = $("rp-member");
  if (window.me_role === "admin") {
    ms.style.display = "";
    const members = [...new Set([...st.replied, ...st.bounced, ...st.all].map((r) => r.member).filter(Boolean))];
    ms.innerHTML = "";
    ms.append(el("option", { value: "all" }, "All members"));
    for (const m of members) ms.append(el("option", { value: m }, m));
    ms.value = st.member;
    ms.onchange = () => { st.member = ms.value; load(); };
  }
}

/* ================= conversation ================= */
function renderConvSkeleton() {
  conv.innerHTML = "";
  conv.append(el("div", { class: "rp-conv-head" },
    el("span", { class: "skel", style: "width:38px;height:38px;border-radius:50%" }),
    el("div", { style: "flex:1" }, el("div", { class: "skel", style: "width:40%;margin-bottom:8px" }),
      el("div", { class: "skel", style: "width:60%" }))));
  conv.append(el("div", { style: "padding:var(--sp-5);display:flex;flex-direction:column;gap:var(--sp-3)" },
    el("div", { class: "skel", style: "height:64px" }), el("div", { class: "skel", style: "height:84px" })));
}

function renderConv() {
  conv.innerHTML = "";
  const r = selectedRow();
  if (st.error) { conv.append(el("div", { style: "padding:var(--sp-5)" }, errorCard("Could not load this email.", { onRetry: load }))); return; }
  if (!r) {
    conv.append(el("div", { style: "padding:var(--sp-8);margin:auto" },
      emptyState({ icon: "inbox", title: "Pick an email on the left",
        text: "Select a reply to read it and act on it." })));
    return;
  }
  const bounced = !!r.bounced_at;
  const handled = !!r.handled_at;

  /* header */
  conv.append(el("div", { class: "rp-conv-head" },
    el("button", { class: "btn btn-icon rp-back", "aria-label": "Back to list", onclick: () => layout.classList.remove("show-conv") }, icon("chevron-left")),
    el("span", { class: "avatar", style: `background:${avatarColor(r.company || r.to_addr)}` },
      initials(r.company || r.hr_name || "?")),
    el("div", { class: "rp-conv-id" },
      el("div", { class: "rp-conv-co" }, r.company || r.to_addr || "Unknown"),
      el("div", { class: "rp-conv-who" }, [r.hr_name, r.to_addr].filter(Boolean).join(" · ")),
      r.campaign_name ? el("div", { class: "rp-conv-cam" },
        el("a", { href: "/campaigns/" + r.campaign_id }, r.campaign_name),
        r.batch_name ? el("span", { class: "t-caption" }, " · " + r.batch_name) : null) : null,
    ),
    el("div", { class: "rp-conv-actions" },
      bounced ? badge("Bounced", "bounced", "mail-x") :
        (r.replied_at ? badge("Replied", "replied", "reply") : badge("Sent", "sent", "send")),
      el("button", {
        class: "btn btn-secondary btn-sm",
        onclick: () => setHandled(r, !handled),
      }, handled ? "Reopen" : "Mark done"),
      kebabBtn(r, bounced),
    ),
  ));

  const body = el("div", { class: "rp-conv-body" });

  /* your email card (collapsed) */
  const subj = el("div", { class: "rp-msg-subj", style: "display:none" }, r.subject || "");
  const mailBody = el("div", { class: "rp-mail-body", style: "display:none" });
  let expanded = false;
  const head = el("div", { class: "rp-msg-head", onclick: () => toggle() },
    icon("chevron-down", "i i-16"),
    el("span", { class: "t-card" }, "Your email"),
    el("span", { class: "t-caption" },
      "Sent " + (r.sent_at ? fmtDayTime(r.sent_at) : "not yet")),
  );
  function toggle() {
    expanded = !expanded;
    head.classList.toggle("is-open", expanded);
    subj.style.display = expanded ? "" : "none";
    mailBody.style.display = expanded ? "" : "none";
    if (expanded && !mailBody.dataset.loaded) {
      mailBody.dataset.loaded = "1";
      mailBody.innerHTML = "<span class='t-caption'>Loading…</span>";
      api("/api/emails/view?id=" + r.id).then((d) => {
        mailBody.innerHTML = d.email.body || "<p class='t-caption'>No body.</p>";
      }).catch(() => { mailBody.innerHTML = "<span class='t-caption'>Could not load the email.</span>"; });
    }
  }
  body.append(el("div", { class: "rp-msg-card" }, head, subj, mailBody));

  /* their reply card / fix panel */
  if (bounced) {
    body.append(fixPanel(r));
  } else if (r.replied_at) {
    body.append(el("div", { class: "rp-msg-card" },
      el("div", { class: "rp-msg-head", style: "cursor:default" },
        icon("reply", "i i-16"),
        el("span", { class: "t-card" }, "Their reply"),
        el("span", { class: "t-caption" }, relTime(r.replied_at))),
      el("div", { class: "rp-reply-note" },
        "Open in Gmail to read their reply and write back from there, or draft a reply here.",
        el("a", { class: "btn btn-ghost btn-sm", target: "_blank", rel: "noopener",
          href: "https://mail.google.com/mail/?q=" + encodeURIComponent("from:" + (r.to_addr || "")) },
          "Open in Gmail"),
      ),
    ));
    body.append(composer(r, handled));
  } else {
    body.append(el("div", { class: "rp-msg-card" },
      el("div", { class: "rp-msg-head", style: "cursor:default" },
        icon("hourglass", "i i-16"),
        el("span", { class: "t-card" }, "Waiting for a reply"),
        el("span", { class: "t-caption" }, "This email was sent and has not been answered yet."),
      ),
    ));
  }
  conv.append(body);
}

function kebabBtn(r, bounced) {
  const btn = el("button", { class: "btn btn-icon", "aria-label": "More actions" }, icon("more-vertical"));
  btn.onclick = (e) => {
    e.stopPropagation();
    menu(btn, [
      { icon: "external-link", label: "Open in Gmail", action: () =>
        window.open("https://mail.google.com/mail/?q=" + encodeURIComponent("from:" + (r.to_addr || "")), "_blank") },
      { icon: "copy", label: "Copy address", action: async () => {
          try { await navigator.clipboard.writeText(r.to_addr || ""); toast("Address copied."); }
          catch { toast("Could not copy the address.", { type: "error" }); }
        } },
      { icon: "undo-2", label: bounced ? "Remove from bounced" : "Mark as not replied",
        action: () => api("/api/emails/" + r.id + "/mark-replied", { body: { replied: false } })
          .then(() => { toast(bounced ? "Email moved back to sent." : "Marked as not replied."); load(); })
          .catch((e) => toast(e.message, { type: "error" })) },
    ]);
  };
  return btn;
}

/* ---------------- composer ---------------- */
let _accounts = null;
async function connectedAccounts() {
  if (!_accounts) {
    const d = await api("/api/accounts").catch(() => ({ accounts: [] }));
    _accounts = (d.accounts || []).filter((a) => a.connected);
  }
  return _accounts;
}

function composer(r, handled) {
  const wrap = el("div", { class: "rp-composer" });
  const ta = el("textarea", { id: "rp-reply-box", placeholder: "Write your reply…", "aria-label": "Write your reply" });
  const foot = el("div", { class: "rp-composer-foot" });
  const aiOn = window.cm && window.cm.state && window.cm.state.ai_on;
  if (aiOn) {
    foot.append(el("button", {
      class: "btn btn-ghost btn-sm",
      onclick: async () => {
        const b = foot.querySelector(".ai-btn");
        b.disabled = true; b.classList.add("is-loading");
        try {
          const d = await api("/api/emails/" + r.id + "/reply-suggest", { body: {} });
          ta.value = d.draft; ta.focus();
        } catch (e) { toast(e.message, { type: "error" }); }
        finally { b.disabled = false; b.classList.remove("is-loading"); }
      },
    }, icon("sparkles", "i i-14"), "Draft with AI"));
    foot.lastChild.classList.add("ai-btn");
  }
  const acctPromise = connectedAccounts();
  wrap.append(ta, foot, el("div", { class: "rp-composer-note" }, "Replies go to the recruiter from your connected account."));
  foot.append(el("span", { class: "push" }));
  acctPromise.then((accts) => {
    if (!accts.length) {
      foot.append(el("span", { class: "t-caption", style: "color:var(--text-3)" },
        "No send account is connected. Connect one in Settings to save replies to Gmail.",
        el("a", { style: "color:var(--link)", href: "/settings/gmail" }, " Open Settings")));
      return;
    }
    const acctId = accts[0].id;
    const safe = window.cm.state.safe_mode;
    const btn = el("button", {
      class: "btn btn-primary btn-sm",
      onclick: async () => {
        const body = ta.value.trim();
        if (!body) { toast("Write the reply first.", { type: "error" }); return; }
        await withLoading(btn, async () => {
          try {
            const d = await api("/api/emails/" + r.id + "/reply", {
              body: { body, account_id: acctId, as_draft: safe },
            });
            if (safe || d.drafted) {
              toast("Reply saved in Gmail drafts.", {
                action: async () => { try { await api("/api/emails/" + r.id + "/handled", { body: { handled: true } }); } catch { } },
              });
            } else {
              toast("Reply sent.");
            }
            ta.value = "";
            try { await api("/api/emails/" + r.id + "/handled", { body: { handled: true } }); } catch { /* best effort */ }
            _accounts = null;
            load();
          } catch (e) { toast(e.message, { type: "error" }); }
        });
      },
    }, safe ? "Save as draft" : "Send reply");
    foot.append(btn);
  }).catch(() => {
    foot.append(el("span", { class: "t-caption" }, "Could not check send accounts."));
  });
  return wrap;
}

/* ---------------- bounced fix panel ---------------- */
function fixPanel(r) {
  const input = el("input", { class: "input", value: r.to_addr || "", "aria-label": "New email address" });
  const save = el("button", { class: "btn btn-primary btn-sm" }, "Save new address");
  save.onclick = async () => {
    const addr = input.value.trim();
    if (!/^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/.test(addr)) {
      toast("Enter a valid email address.", { type: "error" }); return;
    }
    if (addr === r.to_addr) { toast("The address has not changed.", { type: "error" }); return; }
    await withLoading(save, async () => {
      try {
        await api("/api/emails/" + r.id, { body: { to_addr: addr } });
        toast("Address saved. The email is Ready - create its draft from the campaign's Emails tab.");
        load();
      } catch (e) { toast(e.message, { type: "error" }); }
    });
  };
  const goCam = r.campaign_id
    ? el("a", { class: "btn btn-ghost btn-sm", href: "/campaigns/" + r.campaign_id + "?tab=emails" }, "Go to campaign")
    : null;
  return el("div", { class: "rp-fix" },
    el("div", { class: "rp-fix-title" }, icon("mail-x", "i i-16"), "This address did not work"),
    el("div", { class: "rp-fix-reason" }, bounceReason(r.bounce_reason)),
    el("div", { class: "rp-fix-row" },
      input, save, goCam),
    el("div", { class: "t-caption", style: "color:var(--text-3)" },
      "Saving the new address makes the email Ready. Create its draft to send it again."),
  );
}

/* ================= actions ================= */
async function setHandled(r, on) {
  try {
    await api("/api/emails/" + r.id + "/handled", { body: { handled: on } });
    toast(on ? "Marked as done." : "Moved back to Needs reply.");
    load();
  } catch (e) { toast(e.message, { type: "error" }); }
}

function select(id) {
  st.selectedId = id;
  syncUrl();
  render();
  layout.classList.add("show-conv");
}

function render() {
  renderList();
  renderConv();
}

/* ================= keyboard ================= */
document.addEventListener("keydown", (e) => {
  const tag = (e.target.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable;
  if (e.key === "Escape") {
    if (layout.classList.contains("show-conv") && window.innerWidth <= 900) layout.classList.remove("show-conv");
    if (typing) e.target.blur();
    return;
  }
  if (typing) return;
  const rows = currentList();
  if (!rows.length) return;
  let idx = rows.findIndex((r) => r.id === st.selectedId);
  if (e.key === "j" || e.key === "J" || e.key === "ArrowDown") {
    e.preventDefault(); idx = Math.min(rows.length - 1, idx + 1);
  } else if (e.key === "k" || e.key === "K" || e.key === "ArrowUp") {
    e.preventDefault(); idx = Math.max(0, idx - 1);
  } else if (e.key === "Enter") {
    e.preventDefault(); select(rows[idx]?.id ?? rows[0].id); layout.classList.add("show-conv"); return;
  } else if (e.key === "e" || e.key === "E") {
    const r = rows[idx]; if (!r) return;
    if (r.replied_at && !r.handled_at) setHandled(r, true);
    return;
  } else if (e.key === "r" || e.key === "R") {
    const box = document.getElementById("rp-reply-box");
    if (box) { box.focus(); box.select(); }
    return;
  } else return;
  const r = rows[idx];
  if (r && r.id !== st.selectedId) select(r.id);
  const node = listScroll.querySelector('.rp-row[data-id="' + (r?.id ?? "") + '"]');
  if (node) node.scrollIntoView({ block: "nearest" });
});

/* ================= wire + init ================= */
document.querySelectorAll("#rp-seg .seg-item").forEach((b) => {
  b.addEventListener("click", () => {
    st.tab = b.dataset.tab;
    st.selectedId = null;
    load();
  });
});
load();
