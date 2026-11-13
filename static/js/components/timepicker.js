/* Shared time picker (plan 8.4). Quick chips, date + 15-minute time,
   zone caption, inline validation, "space them out" with a live
   first/last line, daily-limit meter. Resolves {when, staggerMin}. */
import { el, icon, modal, api } from "../core/ui.js";
import { fmtLong, fmtTime, toBackend, tzLabel } from "../core/format.js";

const pad = (n) => String(n).padStart(2, "0");

function nextMonday(from) {
  const d = new Date(from.getFullYear(), from.getMonth(), from.getDate());
  d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
  return d;
}

export function openTimePicker({ count = 1, defaultStaggerMin = 0, initial = null, title = "When should it go?" } = {}) {
  const now = new Date();
  let d = initial ? new Date(initial) : new Date(now.getTime() + 3600e3);

  // quick chips — only the ones still in the future
  const chipDefs = [
    { label: "In 1 hour", at: () => new Date(now.getTime() + 3600e3) },
    { label: "Today 18:00", at: () => { const x = new Date(); x.setHours(18, 0, 0, 0); return x; } },
    { label: "Tomorrow 09:00", at: () => { const x = new Date(); x.setDate(x.getDate() + 1); x.setHours(9, 0, 0, 0); return x; } },
    { label: "Tomorrow 10:00", at: () => { const x = new Date(); x.setDate(x.getDate() + 1); x.setHours(10, 0, 0, 0); return x; } },
    { label: "Monday 09:00", at: () => { const x = nextMonday(now); x.setHours(9, 0, 0, 0); return x; } },
    { label: "Next week", at: () => { const x = nextMonday(nextMonday(now)); x.setHours(9, 0, 0, 0); return x; } },
  ].filter((c) => c.at() > now + 10 * 60e3);

  const dateIn = el("input", { type: "date", class: "input", "aria-label": "Date" });
  const timeSel = el("select", { class: "select", "aria-label": "Time" });
  for (let m = 0; m < 24 * 4; m++) {
    const v = pad(Math.floor(m / 4)) + ":" + pad((m % 4) * 15);
    const o = el("option", { value: v }, v);
    if (v === pad(d.getHours()) + ":" + pad(d.getMinutes())) o.selected = true;
    timeSel.append(o);
  }
  const syncFields = () => {
    dateIn.value = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    timeSel.value = pad(d.getHours()) + ":" + pad(d.getMinutes());
  };
  const readFields = () => {
    const p = (dateIn.value || "").split("-").map(Number);
    const t = (timeSel.value || "10:00").split(":").map(Number);
    if (p.length === 3 && p.every((x) => x > 0)) d = new Date(p[0], p[1] - 1, p[2], t[0] || 0, t[1] || 0, 0, 0);
  };

  // space them out
  const staggerOpts = [
    { v: 0, label: "All at once" }, { v: 1, label: "1 min apart" },
    { v: 2, label: "2 min apart" }, { v: 5, label: "5 min apart" }, { v: -1, label: "Custom" },
  ];
  let staggerMode = defaultStaggerMin >= 1 && [1, 2, 5].includes(defaultStaggerMin)
    ? defaultStaggerMin : (defaultStaggerMin > 0 ? -1 : 0);
  let customMin = Math.max(1, Math.min(120, Math.round(defaultStaggerMin) || 1));
  const customIn = el("input", {
    type: "number", class: "input", min: "1", max: "120", value: String(customMin),
    style: "width:84px;display:none", "aria-label": "Minutes apart",
  });
  customIn.addEventListener("input", () => { customMin = Math.max(1, +customIn.value || 1); refresh(); });
  const staggerSeg = el("div", { class: "seg", role: "group", "aria-label": "Space them out" },
    ...staggerOpts.map((o) => el("button", {
      class: "seg-item", onclick: () => {
        staggerMode = o.v;
        customIn.style.display = o.v === -1 ? "" : "none";
        if (o.v === -1) customIn.focus();
        refresh();
      },
    }, o.label)));
  const staggerMin = () => (staggerMode === -1 ? Math.max(1, Math.min(120, customMin)) : staggerMode);

  const zoneLine = el("div", { class: "field-help", style: "display:flex;align-items:center;gap:6px" });
  const errLine = el("div", { class: "field-error", style: "display:none" },
    icon("alert-triangle"), "Pick a time at least 5 minutes from now.");
  const liveLine = el("div", { class: "field-help" });
  const limitLine = el("div", { class: "field-error", style: "display:none" },
    icon("alert-triangle"), el("span", {}, "limit-text"));
  const goBtn = el("button", { class: "btn btn-primary" }, "Choose time");
  let dailyCap = 0;

  function isPast() { return d <= new Date(Date.now() + 5 * 60e3); }

  function refresh() {
    syncFields();
    staggerSeg.querySelectorAll(".seg-item").forEach((b, i) => b.classList.toggle("is-active", staggerOpts[i].v === staggerMode));
    zoneLine.innerHTML = "";
    zoneLine.append(icon("clock", "i i-14"), tzLabel() + " — " + fmtLong(toBackend(d)));
    const past = isPast();
    errLine.style.display = past ? "" : "none";
    dateIn.classList.toggle("invalid", past);
    timeSel.classList.toggle("invalid", past);
    goBtn.disabled = past;
    const n = Math.max(1, count);
    const gap = staggerMin();
    const last = new Date(d.getTime() + gap * (n - 1) * 60e3);
    liveLine.textContent = n === 1
      ? "1 email at " + fmtTime(toBackend(d)) + "."
      : gap === 0
        ? `${n} emails all at once at ${fmtTime(toBackend(d))}.`
        : `${n} emails, one every ${gap} ${gap === 1 ? "minute" : "minutes"}. First at ${fmtTime(toBackend(d))}, last at ${fmtTime(toBackend(last))}.`;
    if (dailyCap > 0 && n > dailyCap) {
      limitLine.style.display = "";
      limitLine.querySelector("span").textContent = `Your daily limit is ${dailyCap}. This send puts ${n} on one day. `;
      const a = el("a", { href: "/settings", style: "font-weight:650" }, "Change limit");
      limitLine.append(a);
    } else {
      limitLine.style.display = "none";
    }
  }

  const chips = el("div", { class: "chips" },
    ...chipDefs.map((c) => el("button", { class: "chip", onclick: () => { d = c.at(); refresh(); } }, c.label)));
  syncFields();
  refresh();

  let settled = false;
  let settle = null;
  const m = modal({
    title,
    sub: "Choose when the " + (count === 1 ? "email" : count + " emails") + " should go out.",
    body: el("div", {},
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Quick pick"), chips),
      el("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:var(--sp-3)" },
        el("div", { class: "field", style: "margin-bottom:0" }, el("label", { class: "field-label" }, "Date"), dateIn),
        el("div", { class: "field", style: "margin-bottom:0" }, el("label", { class: "field-label" }, "Time"), timeSel)),
      zoneLine, errLine,
      el("div", { class: "field", style: "margin-top:var(--sp-4)" },
        el("label", { class: "field-label" }, "Space them out"),
        el("div", { style: "display:flex;gap:var(--sp-3);align-items:center;flex-wrap:wrap" }, staggerSeg, customIn),
        liveLine, limitLine),
    ),
    foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), goBtn],
    onClose: () => { if (!settled) { settled = true; settle(new Error("cancelled")); } },
  });
  return new Promise((resolve, reject) => {
    settle = (v) => (v instanceof Error ? reject(v) : resolve(v));
    m.foot.querySelector(".btn-ghost").addEventListener("click", () => {
      if (settled) return;
      settled = true;
      m.close();
      settle(new Error("cancelled"));
    });
    goBtn.addEventListener("click", () => {
      readFields();
      if (isPast()) { refresh(); return; }
      if (settled) return;
      settled = true;
      try { localStorage.setItem("cm_stagger", String(staggerMin())); } catch { /* private mode */ }
      m.close();
      settle({ when: toBackend(d), staggerMin: staggerMin() });
    });
    api("/api/state").then((s2) => {
      dailyCap = Number((s2.settings && s2.settings.max_sends_per_day) || 0);
      if (!m.box.isConnected) return;
      refresh();
    }).catch(() => {});
  });
}
