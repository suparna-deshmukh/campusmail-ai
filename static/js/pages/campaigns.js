/* Campaigns list (7.1). */
import { api, el, icon, toast, menu, badge, confirmSimple, confirmTyped, emptyState, errorCard, withLoading, esc } from "/static/js/core/ui.js";
import { fmtInt, fmtPct, relTime } from "/static/js/core/format.js";
import { CAMPAIGN_STATUS, plural } from "/static/js/core/copy.js";

const $rows = document.getElementById("camp-rows");
const $empty = document.getElementById("camp-empty");
const $wrap = document.getElementById("camp-table-wrap");
let campaigns = [];
let filter = "all", q = "", sort = "recent";

function statusOf(cm) {
  const st = cm.status_counts || {};
  const total = Object.values(st).reduce((a, b) => a + b, 0);
  if (total === 0) return "draft";
  const live = (st.ready || 0) + (st.drafted || 0) + (st.scheduled || 0) + (st.failed || 0);
  const last = cm.last ? new Date(String(cm.last).replace(" ", "T")) : null;
  const quiet14 = last && (Date.now() - last.getTime() > 14 * 86400000);
  if (live === 0 && quiet14) return "finished";
  return "active";
}

function filtered() {
  let list = [...campaigns];
  if (filter !== "all") list = list.filter((c) => statusOf(c) === filter);
  if (q) list = list.filter((c) => (c.name || "").toLowerCase().includes(q));
  if (sort === "name") list.sort((a, b) => a.name.localeCompare(b.name));
  else if (sort === "created") list.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  else list.sort((a, b) => String(b.last || b.created_at || "").localeCompare(String(a.last || a.created_at || "")));
  return list;
}

function render() {
  const list = filtered();
  $rows.innerHTML = "";
  const counts = { all: campaigns.length, active: 0, finished: 0, draft: 0 };
  for (const c of campaigns) counts[statusOf(c)]++;
  document.getElementById("cf-all").textContent = counts.all;
  document.getElementById("cf-active").textContent = counts.active;
  document.getElementById("cf-finished").textContent = counts.finished;

  if (!list.length) {
    $wrap.querySelector("table").style.display = "none";
    $empty.style.display = "";
    $empty.innerHTML = "";
    if (!campaigns.length) {
      $empty.append(el("div", { class: "empty" },
        el("div", { class: "empty-illust" }, `<svg viewBox="0 0 96 96" width="96" height="96" aria-hidden="true"><path d="M18 74 L78 26" stroke="var(--primary-200)" stroke-width="2.4" stroke-dasharray="1 8" stroke-linecap="round"/><path d="M14 78 L58 44 L52 66 L38 60 Z" fill="var(--primary-100)" stroke="none"/><path d="M58 44 L80 22 M58 44 l-4 16 M58 44 l16-6" fill="none" stroke="var(--primary-500)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`),
        el("div", { class: "empty-title" }, "No campaigns yet"),
        el("p", { class: "empty-text" }, "A campaign holds the companies you email, the emails you send and the replies you get."),
        el("button", { class: "btn btn-primary", onclick: openNewModal }, "Create your first campaign")));
    } else {
      $empty.append(el("div", { class: "empty" },
        el("div", { class: "empty-illust" }, `<svg viewBox="0 0 96 96" width="96" height="96" aria-hidden="true"><circle cx="42" cy="42" r="22" fill="none" stroke="var(--primary-500)" stroke-width="2.4"/><path d="M58 58 L78 78" stroke="var(--primary-500)" stroke-width="3" stroke-linecap="round"/></svg>`),
        el("div", { class: "empty-title" }, "No campaigns match"),
        el("button", { class: "btn btn-secondary", onclick: () => { q = ""; filter = "all"; syncToolbar(); render(); } }, "Clear filters")));
    }
    return;
  }
  $wrap.querySelector("table").style.display = "";
  $empty.style.display = "none";
  for (const cm of list) {
    const st = cm.status_counts || {};
    const sent = st.sent || 0, replied = st.replied || 0;
    const tr = el("tr", { onclick: (e) => { if (!e.target.closest("button")) location.href = "/campaigns/" + cm.id; } },
      el("td", {},
        el("a", { class: "cell-main", href: "/campaigns/" + cm.id, style: "color:inherit" }, cm.name),
        el("div", { class: "cell-sub" }, cm.description || "No note")),
      el("td", {}, badge(CAMPAIGN_STATUS[statusOf(cm)].label, CAMPAIGN_STATUS[statusOf(cm)].cls, CAMPAIGN_STATUS[statusOf(cm)].icon)),
      el("td", { class: "num camp-hide-m" }, fmtInt(cm.n_contacts)),
      el("td", { class: "num camp-hide-m" }, fmtInt(sent)),
      el("td", { class: "num camp-hide-m" }, fmtInt(replied)),
      el("td", { class: "num camp-hide-m" }, fmtPct(sent ? 100 * replied / sent : 0)),
      el("td", { class: "cell-sub camp-hide-m" }, cm.last ? relTime(cm.last) : "—"),
      el("td", { class: "row-kebab", style: "white-space:nowrap" },
        el("a", { class: "btn btn-sm btn-ghost row-action camp-row-send", href: "/campaigns/" + cm.id + "/send" }, "Send emails"),
        kebab(cm)));
    $rows.append(tr);
  }
}

function kebab(cm) {
  const sent = (cm.status_counts || {}).sent || 0;
  const b = el("button", { class: "btn btn-icon", style: "width:32px;height:32px", "aria-label": "Campaign actions",
    onclick: (e) => {
      e.stopPropagation();
      menu(e.currentTarget, [
        { icon: "external-link", text: "Open", action: () => location.href = "/campaigns/" + cm.id },
        { icon: "pencil", text: "Rename", action: () => renameModal(cm) },
        "-",
        { icon: "trash-2", text: "Delete", danger: true, action: () => deleteCampaign(cm, sent) },
      ]);
    } }, icon("ellipsis", "i i-16"));
  return b;
}

async function renameModal(cm) {
  const input = el("input", { class: "input", value: cm.name });
  let saved = false;
  const m = modal2("Rename campaign", null,
    el("div", { class: "field" }, el("label", { class: "field-label" }, "Name"), input,
      el("div", { class: "field-help" }, "Emails and replies stay attached.")),
    [el("button", { class: "btn btn-ghost", id: "rncel" }, "Cancel"), el("button", { class: "btn btn-primary" }, "Save name")]);
  m.foot.querySelector("#rncel").addEventListener("click", () => m.close());
  m.foot.querySelector(".btn-primary").addEventListener("click", async () => {
    const name = input.value.trim();
    if (!name) return;
    const btn = m.foot.querySelector(".btn-primary");
    await withLoading(btn, async () => {
      await api("/api/campaigns/" + cm.id, { body: { name } });
      saved = true;
      m.close();
      toast("Campaign renamed.");
      load();
    });
  });
  input.focus(); input.select();
}
function modal2(title, sub, body, foot) {
  const scrim = el("div", { class: "modal-scrim" });
  const box = el("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title },
    el("div", { class: "modal-head" }, el("div", { class: "modal-title" }, title), sub ? el("div", { class: "modal-sub" }, sub) : null),
    el("div", { class: "modal-body" }, body),
    el("div", { class: "modal-foot" }, ...foot));
  scrim.append(box);
  document.body.append(scrim);
  requestAnimationFrame(() => scrim.classList.add("open"));
  const close = () => { scrim.classList.remove("open"); setTimeout(() => scrim.remove(), 200); };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  document.addEventListener("keydown", onKey, true);
  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) close(); });
  return { close, foot: box.querySelector(".modal-foot") };
}

async function deleteCampaign(cm, sent) {
  const ok = sent
    ? await confirmTyped({ title: "Delete " + cm.name + "?", body: sent + " email" + (sent === 1 ? "" : "s") + (sent === 1 ? " was sent" : " were sent") + " in this campaign. Type the campaign name to delete it. Gmail drafts stay untouched.", expect: cm.name, actionLabel: "Delete campaign" })
    : await confirmSimple({ title: "Delete this campaign?", body: "Its " + cm.n_emails + " email" + (cm.n_emails === 1 ? "" : "s") + " will be removed. Contact lists stay in your Library.", actionLabel: "Delete campaign" });
  if (!ok) return;
  try {
    await api("/api/campaigns/" + cm.id + "/delete", { body: {} });
    toast("Campaign deleted.");
    load();
  } catch (e) { toast(e.message, { type: "error" }); }
}

function openNewModal() {
  const name = el("input", { class: "input", placeholder: "Diwali wishes to recruiters" });
  const note = el("input", { class: "input", placeholder: "What is this campaign for?" });
  const m = modal2("New campaign", "A campaign holds your list, your emails and the replies you get.",
    el("div", {},
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Name"), name),
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Description (optional)"), note)),
    [el("button", { class: "btn btn-ghost" }, "Cancel"), el("button", { class: "btn btn-primary" }, "Create campaign")]);
  m.foot.querySelector(".btn-ghost").addEventListener("click", () => m.close());
  const btn = m.foot.querySelector(".btn-primary");
  btn.addEventListener("click", () => withLoading(btn, async () => {
    const d = await api("/api/campaigns", { body: { name: name.value.trim(), description: note.value.trim() } });
    m.close();
    location.href = "/campaigns/" + d.id;
  }));
  name.focus();
}

/* toolbar wiring */
function syncToolbar() {
  document.getElementById("camp-search").value = q;
  document.getElementById("camp-sort").value = sort;
  document.querySelectorAll("#camp-filter .seg-item").forEach((b) => b.classList.toggle("is-active", b.dataset.f === filter));
}
document.getElementById("btn-new-campaign").addEventListener("click", openNewModal);
let deb;
document.getElementById("camp-search").addEventListener("input", (e) => {
  clearTimeout(deb);
  deb = setTimeout(() => { q = e.target.value.trim().toLowerCase(); render(); }, 250);
});
document.getElementById("camp-filter").addEventListener("click", (e) => {
  const b = e.target.closest(".seg-item");
  if (b) { filter = b.dataset.f; render(); }
});
document.getElementById("camp-sort").addEventListener("change", (e) => { sort = e.target.value; render(); });
document.querySelectorAll("#camp-table-wrap th.sortable").forEach((th) => th.addEventListener("click", () => {
  sort = "name";
  document.getElementById("camp-sort").value = "name";
  render();
}));
if (new URLSearchParams(location.search).get("new") === "1") openNewModal();

async function load() {
  $rows.innerHTML = "";
  $rows.append(el("tr", {}, ...[0, 1, 2].map(() => el("td", {}, el("div", { class: "skel", style: "height:20px;width:70%" })))))
  try {
    const d = await api("/api/campaigns");
    campaigns = d.campaigns || [];
    render();
  } catch (e) {
    $rows.innerHTML = "";
    $rows.append(el("tr", {}, el("td", { colspan: 8 }, errorCard("We could not load campaigns.", { onRetry: load, detail: e.message }))));
  }
}
load();
