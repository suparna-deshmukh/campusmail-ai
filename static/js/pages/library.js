/* Library (plan 9.2-9.4) — templates grid + template editor + contact lists.
   Glossary: list/send/fill-in/follow-up. Sentence case, no exclamation marks. */
import {
  api, el, icon, esc, toast, modal, drawer, menu, confirmSimple, withLoading,
  errorCard, emptyState,
} from "/static/js/core/ui.js";
import { MSG, plural } from "/static/js/core/copy.js";
import { fmtInt, fmtDay, relTime, initials, avatarColor, localToUrl } from "/static/js/core/format.js";

const $ = (id) => document.getElementById(id);
const view = $("lib-view");
const EDITOR_ID = Number(window.lib_edit_id || 0) || null;
const PLACEHOLDER_RE = /\{\{([A-Za-z0-9_ .\-]{1,40})\}\}|\{([A-Za-z0-9_ .\-]{1,40})\}|\[((?:[A-Za-z][A-Za-z0-9_ .\-]{0,39}))\]|\u00ab([A-Za-z][A-Za-z0-9_ .\-]{0,39})\u00bb/g;

const KNOWN_COLS = {
  serial: "No.", no: "No.", company: "Company", organization: "Company", organisation: "Company",
  hrname: "Recruiter name", recruiter: "Recruiter name", recruitername: "Recruiter name", name: "Recruiter name",
  hremail: "Email", email: "Email", mail: "Email",
};
function norm(s) { return String(s || "").toLowerCase().replace(/[^a-z0-9]/g, ""); }

/* ================= routing ================= */
function init() {
  const tabs = document.querySelectorAll("#lib-tabs .tab");
  const isEditor = !!EDITOR_ID;
  const tab = isEditor ? "templates" : (new URLSearchParams(location.search).get("tab") || "templates");
  tabs.forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === tab && !isEditor)));
  if (isEditor) initEditor(EDITOR_ID);
  else if (tab === "lists") initLists();
  else initTemplates();
}

/* ================= templates ================= */
function initTemplates() {
  view.innerHTML = "";
  view.append(
    el("div", { class: "lib-toolbar" },
      el("div", { class: "search-input" },
        icon("search", "i i-16"),
        el("input", { class: "input", id: "lib-q", placeholder: "Search templates", "aria-label": "Search templates" })),
      el("div", { class: "seg", id: "lib-kind", role: "group", "aria-label": "Kind" },
        el("button", { class: "seg-item is-active", "data-k": "all" }, "All"),
        el("button", { class: "seg-item", "data-k": "outreach" }, "Outreach"),
        el("button", { class: "seg-item", "data-k": "reminder" }, "Follow-up")),
      el("div", { class: "push" },
        el("button", { class: "btn btn-primary", id: "lib-new" }, icon("plus", "i i-16"), "New template"))),
    el("div", { id: "lib-grid-wrap" }),
  );
  const state = { q: "", kind: "all", templates: [], loading: true, error: null };

  async function load() {
    state.loading = true; state.error = null;
    renderSkeleton();
    try {
      const d = await api("/api/templates");
      state.templates = d.templates;
    } catch (e) { state.error = e; }
    state.loading = false;
    render();
  }
  function filtered() {
    return state.templates.filter((t) => {
      if (state.kind !== "all" && (t.kind || "outreach") !== state.kind) return false;
      if (state.q) {
        const q = state.q.toLowerCase();
        if (!(t.name || "").toLowerCase().includes(q) && !(t.subject || "").toLowerCase().includes(q)) return false;
      }
      return true;
    });
  }
  function renderSkeleton() {
    const w = $("lib-grid-wrap"); w.innerHTML = "";
    const g = el("div", { class: "lib-grid" });
    for (let i = 0; i < 4; i++) g.append(el("div", { class: "lib-card" },
      el("div", { class: "lib-thumb" }, el("div", { class: "lib-thumb-ph" }, el("span", { class: "skel", style: "width:60%" }))),
      el("div", { class: "lib-card-body" }, el("div", { class: "skel", style: "width:70%;margin-bottom:8px" }), el("div", { class: "skel", style: "width:90%" })),
    ));
    w.append(g);
  }
  function render() {
    const w = $("lib-grid-wrap"); w.innerHTML = "";
    if (state.error) { w.append(errorCard("Could not load templates.", { onRetry: load })); return; }
    if (!state.templates.length) {
      w.append(emptyState({ icon: "file-text", title: "No templates yet",
        text: "A template is the email you will reuse for a whole campaign.",
        primary: el("div", { style: "display:flex;gap:8px;flex-wrap:wrap" },
          el("button", { class: "btn btn-secondary", onclick: () => newTemplateModal("ai") }, "Write with AI"),
          el("button", { class: "btn btn-secondary", onclick: () => newTemplateModal("upload") }, "Upload a Word file"),
          el("button", { class: "btn btn-secondary", onclick: () => newTemplateModal("example") }, "Start from an example")) }));
      return;
    }
    const rows = filtered();
    if (!rows.length) {
      w.append(emptyState({ icon: "search", title: "No templates match",
        text: "Try a different search or kind.",
        primary: el("button", { class: "btn btn-secondary", onclick: () => { state.q = ""; state.kind = "all"; $("lib-q").value = ""; syncSeg(); load(); } }, "Clear filters") }));
      return;
    }
    const g = el("div", { class: "lib-grid" });
    for (const t of rows) g.append(card(t));
    w.append(g);
    observeThumbs(g);
  }
  function card(t) {
    const kind = t.kind || "outreach";
    const body = el("div", { class: "lib-card-body" },
      el("div", { class: "lib-card-name" },
        el("span", { class: "truncate" }, t.name),
        t.is_active ? el("span", { class: "badge neutral" }, "Default") : null,
        kind === "reminder" ? el("span", { class: "badge waiting" }, "Follow-up") : null),
      el("div", { class: "lib-card-subj" }, t.subject || "No subject"),
      (t.fields && t.fields.length) ? el("div", { class: "lib-card-chips" },
        ...t.fields.slice(0, 3).map((f) => el("span", { class: "chip" }, f)),
        t.fields.length > 3 ? el("span", { class: "chip" }, "+" + (t.fields.length - 3)) : null) : null,
    );
    const cardEl = el("div", { class: "lib-card", role: "link", tabindex: "0",
      onclick: () => location.href = "/library/templates/" + t.id,
      onkeydown: (e) => { if (e.key === "Enter") location.href = "/library/templates/" + t.id; } },
      el("div", { class: "lib-thumb", "data-tid": t.id },
        el("div", { class: "lib-thumb-ph" }, "Template preview")),
      body,
      el("div", { class: "lib-card-foot" },
        "Used in " + (t.n_campaigns || 0) + " " + plural(t.n_campaigns || 0, "campaign"),
        t.updated_at ? " · edited " + relTime(t.updated_at) : null,
        el("span", { class: "push" }),
        kebab(t)),
    );
    return cardEl;
  }
  function kebab(t) {
    const b = el("button", { class: "btn btn-icon", "aria-label": "Template actions" }, icon("more-vertical"));
    b.onclick = (e) => {
      e.stopPropagation();
      const items = [
        { icon: "eye", label: "Preview", action: () => previewModal(t) },
        { icon: "check-circle-2", label: "Set as default",
          action: () => api("/api/templates/" + t.id + "/activate", { body: {} }).then(() => { toast("Template set as default."); load(); }) },
      ];
      if (window.cm && window.cm.state && window.cm.state.ai_on) {
        items.push({ icon: "sparkles", label: "Polish with AI", action: async () => {
          try { await api("/api/templates/" + t.id + "/polish", { body: {} }); toast("Template polished."); load(); }
          catch (e2) { toast(e2.message, { type: "error" }); }
        } });
      }
      items.push("-", { icon: "trash-2", label: "Delete template", danger: true, action: async () => {
        const ok = await confirmSimple({
          title: "Delete this template?",
          body: "Emails already created keep their own copy.",
          actionLabel: "Delete template",
        });
        if (!ok) return;
        try { await api("/api/templates/" + t.id + "/delete", { body: {} }); toast("Template deleted."); load(); }
        catch (e2) { toast(e2.message, { type: "error" }); }
      } });
      menu(b, items);
    };
    return b;
  }
  async function previewModal(t) {
    const m = modal({ title: t.name, wide: true, body: el("div", { class: "t-caption" }, "Rendering…") });
    try {
      const d = await api("/api/templates/" + t.id + "/preview");
      m.body.innerHTML = "";
      m.body.append(el("div", { class: "t-card", style: "margin-bottom:10px;font-weight:600" }, d.subject || ""));
      m.body.append(el("div", { class: "ed-preview-box", html: d.body || "" }));
    } catch (e) { m.body.innerHTML = ""; m.body.append(errorCard("Could not load the preview.", { onRetry: () => { m.close(); previewModal(t); } })); }
  }
  function syncSeg() {
    document.querySelectorAll("#lib-kind .seg-item").forEach((b) => b.classList.toggle("is-active", b.dataset.k === state.kind));
  }
  let qTimer;
  $("lib-q").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(() => { state.q = $("lib-q").value.trim(); render(); }, 200); });
  document.querySelectorAll("#lib-kind .seg-item").forEach((b) => b.addEventListener("click", () => { state.kind = b.dataset.k; syncSeg(); render(); }));
  $("lib-new").onclick = () => newTemplateMenu($("lib-new"));
  load();
}

function observeThumbs(root) {
  const io = new IntersectionObserver((entries) => {
    for (const en of entries) {
      if (!en.isIntersecting) continue;
      const th = en.target;
      io.unobserve(th);
      const tid = th.dataset.tid;
      api("/api/templates/" + tid + "/preview").then((d) => {
        const doc = "<!DOCTYPE html><html><head><style>body{margin:0;padding:28px 32px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#0F1629;line-height:1.55}img{max-width:100%}</style></head><body>" + localToUrl(d.body || "") + "</body></html>";
        const f = document.createElement("iframe");
        f.setAttribute("srcdoc", doc);
        f.setAttribute("title", "Template thumbnail");
        th.innerHTML = "";
        th.append(f);
      }).catch(() => { /* leave placeholder */ });
    }
  }, { rootMargin: "200px" });
  root.querySelectorAll(".lib-thumb[data-tid]").forEach((t) => io.observe(t));
}

/* ---------- new template menu + modals ---------- */
function newTemplateMenu(anchor) {
  menu(anchor, [
    { icon: "sparkles", label: "Write with AI", action: () => newTemplateModal("ai") },
    { icon: "upload", label: "Upload a Word file", action: () => newTemplateModal("upload") },
    { icon: "file-text", label: "Paste text", action: () => newTemplateModal("paste") },
    { icon: "book-open", label: "Start from an example", action: () => newTemplateModal("example") },
  ]);
}

function newTemplateModal(which) {
  if (which === "ai") return aiModal();
  if (which === "upload") return uploadModal();
  if (which === "paste") return pasteModal();
  return exampleModal();
}

function aiModal() {
  const aiOn = window.cm && window.cm.state && window.cm.state.ai_on;
  if (!aiOn) {
    modal({ title: "Write a template with AI",
      body: emptyState({ icon: "sparkles", title: "AI is off",
        text: "Add an API key to turn it on.",
        primary: el("a", { class: "btn btn-primary", href: "/settings/ai" }, "Open Settings") }),
      foot: [el("button", { class: "btn btn-ghost" }, "Close")] });
    return;
  }
  let kind = "outreach", tone = "Professional";
  const name = el("input", { class: "input", placeholder: "Template name", "aria-label": "Template name" });
  const brief = el("textarea", { class: "input", style: "width:100%;min-height:110px", placeholder: "Invite recruiters to our campus drive in January. Mention 464 students and ask for job descriptions.", "aria-label": "What is this email about" });
  const toneChips = el("div", { class: "chips" }, ...["Professional", "Warm", "Concise", "Formal"].map((t) =>
    el("button", { class: "chip" + (t === tone ? " is-on" : ""), "data-tone": t,
      onclick: (e) => { tone = t; toneChips.querySelectorAll(".chip").forEach((c) => c.classList.toggle("is-on", c.dataset.tone === t)); } }, t)));
  const kindSeg = el("div", { class: "seg", style: "margin-bottom:12px", role: "group", "aria-label": "Kind" },
    el("button", { class: "seg-item is-active", "data-k": "outreach", onclick: (e) => setKind(e, "outreach") }, "Outreach email"),
    el("button", { class: "seg-item", "data-k": "reminder", onclick: (e) => setKind(e, "reminder") }, "Follow-up email"));
  function setKind(e, k) { kind = k; kindSeg.querySelectorAll(".seg-item").forEach((b) => b.classList.toggle("is-active", b.dataset.k === k)); }
  const btn = el("button", { class: "btn btn-primary" }, "Write it");
  const cap = el("div", { class: "t-caption", style: "margin-top:8px;display:none" }, "Writing your email…");
  const body = el("div", { style: "display:flex;flex-direction:column;gap:12px" },
    kindSeg,
    el("div", { class: "field" }, el("label", { class: "field-label" }, "Name"), name),
    el("div", { class: "field" }, el("label", { class: "field-label" }, "What is this email about?"), brief),
    el("div", { class: "field" }, el("label", { class: "field-label" }, "Tone"), toneChips),
  );
  const m = modal({ title: "Write a template with AI", body, foot: [
    el("button", { class: "btn btn-ghost" }, "Cancel"),
    btn,
  ] });
  btn.onclick = async () => {
    cap.style.display = "";
    await withLoading(btn, async () => {
      try {
        const d = await api("/api/templates/ai", { body: { name: name.value.trim(), brief: brief.value.trim(), tone: tone.toLowerCase(), kind } });
        m.close();
        toast("Draft ready. Change anything you like.");
        location.href = "/library/templates/" + d.id;
      } catch (e) { toast(e.message, { type: "error" }); }
      cap.style.display = "none";
    });
  };
}

function uploadModal() {
  const file = el("input", { type: "file", accept: ".docx,.html,.txt", style: "display:none" });
  const name = el("input", { class: "input", placeholder: "Template name", "aria-label": "Template name" });
  const subject = el("input", { class: "input", placeholder: "Subject", "aria-label": "Subject" });
  const drop = el("div", { class: "dropzone", style: "cursor:pointer", role: "button", tabindex: "0" },
    icon("upload", "i i-16"),
    el("div", { style: "margin-top:8px;font-size:13px" }, "Choose a Word (.docx) file"),
  );
  drop.onclick = () => file.click();
  drop.onkeydown = (e) => { if (e.key === "Enter") file.click(); };
  file.onchange = () => {
    if (file.files[0]) {
      const n = file.files[0].name.replace(/\.[^.]+$/, "").replace(/[_-]/g, " ").trim();
      if (n && !name.value) name.value = n;
      drop.innerHTML = ""; drop.append(icon("file-text", "i i-16"), el("div", { style: "margin-top:8px;font-size:13px" }, file.files[0].name));
    }
  };
  const btn = el("button", { class: "btn btn-primary" }, "Upload");
  const m = modal({ title: "Upload a Word file",
    body: el("div", { style: "display:flex;flex-direction:column;gap:12px" },
      drop,
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Name"), name),
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Subject"), subject)),
    foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), btn] });
  btn.onclick = async () => {
    if (!file.files[0]) { toast("Choose a file first.", { type: "error" }); return; }
    const fd = new FormData();
    fd.append("file", file.files[0]);
    if (name.value.trim()) fd.append("name", name.value.trim());
    if (subject.value.trim()) fd.append("subject", subject.value.trim());
    await withLoading(btn, async () => {
      try {
        const d = await fetch("/api/templates/upload", { method: "POST", body: fd }).then((r) => r.json());
        if (!d.ok) throw new Error(d.error || "Upload failed");
        m.close();
        toast("Template added.");
        location.href = "/library/templates/" + d.id;
      } catch (e) { toast(e.message, { type: "error" }); }
    });
  };
}

function pasteModal() {
  const name = el("input", { class: "input", placeholder: "Template name", "aria-label": "Template name" });
  const subject = el("input", { class: "input", placeholder: "Subject", "aria-label": "Subject" });
  const body = el("textarea", { class: "input", style: "width:100%;min-height:140px", placeholder: "Paste the email text. Use {company} or [Recruiter name] to fill in each person's details.", "aria-label": "Email body" });
  const btn = el("button", { class: "btn btn-primary" }, "Create template");
  const m = modal({ title: "Paste text", wide: true,
    body: el("div", { style: "display:flex;flex-direction:column;gap:12px" },
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Name"), name),
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Subject"), subject),
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Body"), body)),
    foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), btn] });
  btn.onclick = async () => {
    const html = "<p>" + esc(body.value).replace(/\n\n+/g, "</p><p>").replace(/\n/g, "<br>") + "</p>";
    await withLoading(btn, async () => {
      try {
        const d = await api("/api/templates", { body: { name: name.value.trim(), subject: subject.value.trim(), body_html: html, type: "html", kind: "outreach" } });
        m.close();
        toast("Template created.");
        location.href = "/library/templates/" + d.id;
      } catch (e) { toast(e.message, { type: "error" }); }
    });
  };
}

const EXAMPLES = [
  {
    name: "Campus recruitment invitation", kind: "outreach",
    desc: "Invite a company to the campus drive and ask for job descriptions.",
    subject: "Campus drive at BITS Pilani Goa — job descriptions for January",
    body: "<p>Dear [Recruiter name],</p><p>We are organising our campus recruitment drive at BITS Pilani Goa in January, and I would love for {company} to take part.</p><p>Around 464 students from our final two semesters will be available. Could you share the job descriptions you would like to post?</p><p>Thank you for considering us. I look forward to hearing from you.</p><p>Warm regards,<br>[Recruiter name]'s peer, Placements Team</p>",
  },
  {
    name: "Internship request", kind: "outreach",
    desc: "Ask for internship openings for final-year students.",
    subject: "Internship openings with {company} for our final-year students",
    body: "<p>Dear [Recruiter name],</p><p>I am writing on behalf of the final-year students at BITS Pilani Goa. Several of us would value the chance to intern with {company} this summer.</p><p>Would you be open to sharing any internship openings or referring us to the right team?</p><p>Thank you for your time.</p><p>Warm regards,<br>Placements Team, BITS Pilani Goa</p>",
  },
  {
    name: "Follow-up reminder", kind: "reminder",
    desc: "A short, polite nudge after the first email went unanswered.",
    subject: "Following up: campus drive in January",
    body: "<p>Dear [Recruiter name],</p><p>I wanted to follow up on my earlier email about the campus drive in January at BITS Pilani Goa.</p><p>If {company} is considering participating, I would be glad to share the dates and the student profile details.</p><p>Warm regards,<br>Placements Team, BITS Pilani Goa</p>",
  },
];

function exampleModal() {
  const m = modal({ title: "Start from an example", wide: true,
    body: el("div", { style: "display:flex;flex-direction:column;gap:10px" },
      ...EXAMPLES.map((ex) => el("button", {
        class: "btn btn-ghost", style: "justify-content:flex-start;white-space:normal;text-align:left",
        onclick: async () => {
          m.close();
          try {
            const d = await api("/api/templates", { body: { name: ex.name, subject: ex.subject, body_html: ex.body, type: "html", kind: ex.kind } });
            toast("Example added. Make it your own.");
            location.href = "/library/templates/" + d.id;
          } catch (e) { toast(e.message, { type: "error" }); }
        },
      },
        el("div", { style: "min-width:0" },
          el("div", { style: "font-weight:650" }, ex.name),
          el("div", { style: "font-size:12px;color:var(--text-3)" }, ex.desc))),
    )),
  });
}

/* ================= template editor (9.3) ================= */
const SAMPLE_ROW = { serial: "1", company: "Example Company", hr_name: "Aarav Mehta", hr_email: "aarav@example.com" };

function detectFieldsClient(subject, bodyText) {
  const text = String(subject || "") + "\n" + String(bodyText || "").replace(/<[^>]+>/g, "");
  const out = [];
  PLACEHOLDER_RE.lastIndex = 0;
  let m;
  while ((m = PLACEHOLDER_RE.exec(text))) {
    const name = (m[1] || m[2] || m[3] || m[4] || "").trim();
    if (name && !out.some((f) => f.toLowerCase() === name.toLowerCase())) out.push(name);
  }
  return out;
}
function fillBodyClient(html, row) {
  const map = {};
  for (const [k, v] of Object.entries(row || {})) map[norm(k)] = String(v ?? "");
  return String(html || "").replace(PLACEHOLDER_RE, (raw, a, b, cc, dd) => {
    const name = (a || b || cc || dd || "").trim();
    const v = map[norm(name)];
    return v != null && v !== "" ? esc(v) : esc(name);
  });
}

async function initEditor(tid) {
  view.innerHTML = "";
  const s = {
    id: tid, name: "", subject: "", kind: "outreach",
    dirty: false, template: null, contacts: [], previewAs: "sample",
  };

  /* sticky header */
  const nameInput = el("input", { class: "ed-name-input", "aria-label": "Template name" });
  const saveInd = el("span", { class: "ed-save-ind" }, el("span", { class: "dot" }), "All changes saved");
  const kindSeg = el("div", { class: "seg", role: "group", "aria-label": "Used as" },
    el("button", { class: "seg-item is-active", "data-k": "outreach" }, "Outreach"),
    el("button", { class: "seg-item", "data-k": "reminder" }, "Follow-up"));
  kindSeg.querySelectorAll(".seg-item").forEach((b) => b.addEventListener("click", () => {
    s.kind = b.dataset.k;
    kindSeg.querySelectorAll(".seg-item").forEach((x) => x.classList.toggle("is-active", x === b));
    markDirty();
  }));
  const saveBtn = el("button", { class: "btn btn-primary" }, "Save");
  const kebabB = el("button", { class: "btn btn-icon", "aria-label": "More actions" }, icon("more-vertical"));
  kebabB.onclick = () => menu(kebabB, [
    { icon: "check-circle-2", label: "Set as default", action: () =>
      api("/api/templates/" + s.id + "/activate", { body: {} }).then(() => toast("Template set as default.")) },
    "-",
    { icon: "trash-2", label: "Delete template", danger: true, action: async () => {
      const ok = await confirmSimple({ title: "Delete this template?",
        body: "Emails already created keep their own copy.", actionLabel: "Delete template" });
      if (!ok) return;
      try { await api("/api/templates/" + s.id + "/delete", { body: {} }); toast("Template deleted."); location.href = "/library"; }
      catch (e) { toast(e.message, { type: "error" }); }
    } },
  ]);
  const sticky = el("div", { class: "ed-sticky" },
    el("a", { class: "btn btn-icon", href: "/library?tab=templates", "aria-label": "Back to Library" }, icon("chevron-left")),
    nameInput, saveInd,
    el("span", { class: "t-caption" }, "Used as:"), kindSeg,
    el("span", { class: "push" }), saveBtn, kebabB,
  );

  /* left: editor */
  const subject = el("input", { class: "input", style: "width:100%", "aria-label": "Subject", placeholder: "Subject" });
  const editor = el("div", { class: "ed-body", contenteditable: "true", "aria-label": "Email body" });
  const fillinBtn = el("button", { class: "btn btn-ghost btn-sm", id: "ed-fillin-btn" }, icon("tag", "i i-14"), "Insert fill-in");
  const toolbar = el("div", { class: "ed-toolbar", role: "toolbar" },
    tbBtn("bold", "Bold", () => docCmd("bold")),
    tbBtn("italic", "Italic", () => docCmd("italic")),
    tbBtn("underline", "Underline", () => docCmd("underline")),
    el("span", { class: "ed-toolbar-sep" }),
    tbBtn("list", "Bulleted list", () => docCmd("insertUnorderedList")),
    tbBtn("list-ordered", "Numbered list", () => docCmd("insertOrderedList")),
    tbBtn("link", "Link", addLink),
    tbBtn("image", "Image", addImage),
    el("span", { class: "ed-toolbar-sep" }),
    fillinBtn,
  );
  function tbBtn(ico, label, fn) {
    const b = el("button", { class: "btn btn-icon", "aria-label": label, title: label, mousedown: (e) => e.preventDefault() }, icon(ico));
    b.onclick = fn;
    return b;
  }
  function docCmd(cmd) { document.execCommand(cmd); editor.focus(); markDirty(); }
  function addLink() {
    const url = prompt("Link address", "https://");
    if (url) { document.execCommand("createLink", false, url); markDirty(); }
  }
  let imgInput = el("input", { type: "file", accept: "image/*", style: "display:none" });
  function addImage() { imgInput.click(); }
  imgInput.onchange = async () => {
    const f = imgInput.files[0];
    if (!f) return;
    try {
      const fd = new FormData(); fd.append("file", f);
      const d = await fetch("/api/signature/upload", { method: "POST", body: fd }).then((r) => r.json());
      if (!d.ok) throw new Error(d.error || "Upload failed");
      document.execCommand("insertHTML", false, d.html);
      markDirty();
    } catch (e) { toast(e.message, { type: "error" }); }
    imgInput.value = "";
  };
  fillinBtn.onclick = (e) => {
    const cols = [["No.", "{serial}"], ["Company", "{company}"], ["Recruiter name", "{hr_name}"], ["Email", "{hr_email}"]];
    const btn = e.currentTarget;
    menu(btn, cols.map(([label, ph]) => ({
      label,
      action: () => {
        editor.focus();
        const span = document.createElement("span");
        span.className = "fill-pill";
        span.dataset.f = ph;
        span.contentEditable = "false";
        span.textContent = label;
        document.execCommand("insertHTML", false, span.outerHTML + " ");
        markDirty();
      },
    })));
  };

  /* right: preview + fill-ins */
  const prevAs = el("select", { class: "select", style: "width:100%", "aria-label": "Preview as" },
    el("option", { value: "sample" }, "Sample contact"));
  const preview = el("div", { class: "ed-preview-box" });
  const fillins = el("div", { class: "ed-fillins" });
  const right = el("div", { class: "ed-right" },
    el("div", { class: "row", style: "gap:8px" },
      el("span", { class: "t-caption" }, "Preview as"), prevAs),
    preview, fillins,
  );
  const left = el("div", { style: "min-width:0" },
    el("div", { class: "ed-field-row" }, subject),
    toolbar, editor, imgInput,
  );
  view.append(sticky, el("div", { class: "ed-wrap" }, left, right));

  function editorHtml() {
    let h = editor.innerHTML;
    h = h.replace(/<span class="fill-pill" data-f="([^"]*)"[^>]*>[\s\S]*?<\/span>/g, (m, f) => f);
    return h;
  }
  function wrapPills(html) {
    return String(html || "").replace(PLACEHOLDER_RE, (raw, a, b, cc, dd) => {
      const name = (a || b || cc || dd || "").trim();
      const label = KNOWN_COLS[norm(name)] || name;
      return '<span class="fill-pill" data-f="' + esc(raw) + '" contenteditable="false">' + esc(label) + "</span>";
    });
  }
  function markDirty() {
    s.dirty = true;
    saveInd.classList.add("is-dirty");
    saveInd.lastChild.textContent = "Unsaved changes";
    scheduleAutosave();
  }
  function markSaved() {
    s.dirty = false;
    saveInd.classList.remove("is-dirty");
    saveInd.lastChild.textContent = "All changes saved";
    localStorage.removeItem("cm_tpl_draft_" + s.id);
  }

  let autosaveTimer;
  function scheduleAutosave() {
    clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => {
      localStorage.setItem("cm_tpl_draft_" + s.id, JSON.stringify({
        name: nameInput.value, subject: subject.value,
        body: editorHtml(), kind: s.kind, at: Date.now(),
      }));
    }, 5000);
  }
  nameInput.addEventListener("input", markDirty);
  subject.addEventListener("input", markDirty);
  editor.addEventListener("input", () => { markDirty(); schedulePreview(); });

  let prevTimer;
  function schedulePreview() { clearTimeout(prevTimer); prevTimer = setTimeout(renderPreview, 400); }
  function renderPreview() {
    let row = SAMPLE_ROW;
    if (s.previewAs !== "sample") {
      const c = s.contacts.find((x) => String(x.id) === s.previewAs);
      if (c) row = { serial: c.serial || "1", company: c.company, hr_name: c.hr_name, hr_email: c.hr_email };
    }
    preview.innerHTML = fillBodyClient(editorHtml(), row);
    const fields = detectFieldsClient(subject.value, editor.innerHTML);
    fillins.innerHTML = "";
    if (fields.length) {
      fillins.append(el("div", {}, "Fill-ins in this email:"));
      fillins.append(el("div", { class: "chips" }, ...fields.map((f) => el("span", { class: "chip" }, f))));
      const unknown = fields.filter((f) => !KNOWN_COLS[norm(f)]);
      if (unknown.length) {
        fillins.append(el("div", { class: "warn" },
          unknown.join(", ") + " has no matching column. You can still match it when sending."));
      }
    } else {
      fillins.append(el("div", {}, "No fill-ins yet. Use Insert fill-in to add a person's name or company."));
    }
  }

  saveBtn.onclick = async () => {
    await withLoading(saveBtn, async () => {
      try {
        await api("/api/templates/" + s.id, {
          body: { name: nameInput.value.trim(), subject: subject.value.trim(), body_html: editorHtml(), kind: s.kind },
        });
        markSaved();
        toast("Template saved.");
      } catch (e) { toast(e.message, { type: "error" }); }
    });
  };

  window.addEventListener("beforeunload", (e) => {
    if (s.dirty) { e.preventDefault(); e.returnValue = ""; }
  });

  async function loadTpl() {
    try {
      const d = await api("/api/templates");
      s.template = d.templates.find((t) => t.id === s.id);
      if (!s.template) { view.innerHTML = ""; view.append(errorCard("Template not found.")); return; }
      nameInput.value = s.template.name;
      subject.value = s.template.subject || "";
      s.kind = s.template.kind || "outreach";
      kindSeg.querySelectorAll(".seg-item").forEach((x) => x.classList.toggle("is-active", x.dataset.k === s.kind));
      editor.innerHTML = wrapPills(s.template.body_html);
      const c = await api("/api/contacts").catch(() => ({ rows: [] }));
      s.contacts = (c.rows || []).slice(0, 5);
      for (const x of s.contacts) {
        prevAs.append(el("option", { value: String(x.id) }, (x.company || x.hr_name || ("Contact " + x.id)) + " — " + (x.hr_name || x.hr_email || "")));
      }
      prevAs.onchange = () => { s.previewAs = prevAs.value; renderPreview(); };
      renderPreview();
      /* local draft restore */
      try {
        const raw = localStorage.getItem("cm_tpl_draft_" + s.id);
        if (raw) {
          const draft = JSON.parse(raw);
          if (draft.body !== editorHtml() || draft.subject !== subject.value) {
            const bar = el("div", { class: "banner banner-info", style: "margin-bottom:var(--sp-3);align-items:center" },
              el("div", { style: "flex:1" }, "You have unsaved changes from " + relTime(new Date(draft.at).toISOString())),
              el("button", { class: "btn btn-ghost btn-sm", onclick: () => {
                nameInput.value = draft.name || nameInput.value;
                subject.value = draft.subject || "";
                s.kind = draft.kind || s.kind;
                kindSeg.querySelectorAll(".seg-item").forEach((x) => x.classList.toggle("is-active", x.dataset.k === s.kind));
                editor.innerHTML = wrapPills(draft.body || "");
                renderPreview(); markDirty(); bar.remove();
              } }, "Restore"),
              el("button", { class: "btn btn-ghost btn-sm", onclick: () => { localStorage.removeItem("cm_tpl_draft_" + s.id); bar.remove(); } }, "Discard"),
            );
            view.prepend(bar);
          }
        }
      } catch { /* ignore corrupt draft */ }
    } catch (e) {
      view.innerHTML = "";
      view.append(errorCard("Could not load the template.", { onRetry: loadTpl }));
    }
  }
  loadTpl();
}

/* ================= contact lists (9.4) ================= */
async function initLists() {
  view.innerHTML = "";
  view.append(
    el("div", { class: "lib-toolbar" },
      el("div", { class: "search-input" },
        icon("search", "i i-16"),
        el("input", { class: "input", id: "ls-q", placeholder: "Search lists", "aria-label": "Search lists" })),
      el("div", { class: "push" },
        el("button", { class: "btn btn-primary", id: "ls-import" }, icon("upload", "i i-16"), "Import a list"))),
    el("div", { class: "table-wrap", id: "ls-wrap" }),
  );
  const state = { q: "", sheets: [], campaigns: [], total: 0, loading: true, error: null };

  async function load() {
    state.loading = true;
    renderSkeleton();
    try {
      const [sh, ca, ct] = await Promise.all([api("/api/sheets"), api("/api/campaigns"), api("/api/contacts")]);
      state.sheets = sh.sheets;
      state.campaigns = ca.campaigns;
      state.total = ct.total;
    } catch (e) { state.error = e; }
    state.loading = false;
    render();
  }
  function renderSkeleton() {
    $("ls-wrap").innerHTML = "";
    $("ls-wrap").append(el("div", { style: "padding:var(--sp-5)" },
      ...[0, 1, 2].map(() => el("div", { class: "skel", style: "height:44px;margin-bottom:10px" }))));
  }
  function camName(id) { return state.campaigns.find((c) => c.id === Number(id))?.name || ""; }
  function render() {
    const w = $("ls-wrap"); w.innerHTML = "";
    if (state.error) { w.append(errorCard("Could not load lists.", { onRetry: load })); return; }
    const rows = state.sheets.filter((s) => !state.q || (s.name || "").toLowerCase().includes(state.q.toLowerCase()));
    if (!state.sheets.length && !state.q) {
      w.append(importDropzone(true));
      return;
    }
    const tbl = el("table", { class: "table", "aria-label": "Contact lists" },
      el("thead", {}, el("tr", {},
        el("th", {}, "List"), el("th", { class: "num" }, "Contacts"), el("th", {}, "Columns"),
        el("th", {}, "Used in"), el("th", {}, "Added"), el("th", { style: "width:48px" }, ""))),
      el("tbody", { id: "ls-rows" }));
    const tb = tbl.tBodies[0];
    /* pinned: All contacts */
    tb.append(listRow({
      pinned: true, name: "All contacts", n: state.total, headers: ["No.", "Company", "Recruiter name", "Email"],
      used: "Every list", added: null,
    }, true));
    for (const s of rows) {
      tb.append(listRow({
        name: s.name, n: s.n_rows, headers: s.headers,
        used: camName(s.campaign_id) || "—", added: s.created_at, id: s.id,
      }, false));
    }
    w.append(tbl);
    if (rows.length === 0 && state.q) {
      w.append(emptyState({ icon: "search", title: "No lists match",
        primary: el("button", { class: "btn btn-secondary", onclick: () => { state.q = ""; $("ls-q").value = ""; render(); } }, "Clear filters") }));
    }
  }
  function listRow(d, isAll) {
    const tr = el("tr", { class: d.pinned ? "lib-pinned" : "" });
    tr.append(el("td", {}, el("div", { style: "font-weight:600" }, d.name),
      d.pinned ? el("div", { class: "t-caption t-faint" }, "Everything you have imported") : null));
    tr.append(el("td", { class: "num" }, fmtInt(d.n)));
    tr.append(el("td", {}, el("span", { style: "display:flex;gap:4px;flex-wrap:wrap" },
      ...(d.headers || []).slice(0, 4).map((h) => el("span", { class: "lib-col-chip" }, h)),
      (d.headers || []).length > 4 ? el("span", { class: "lib-col-chip" }, "+" + (d.headers.length - 4)) : null)));
    tr.append(el("td", { class: "t-caption" }, d.used));
    tr.append(el("td", { class: "t-caption" }, d.added ? fmtDay(d.added) : "—"));
    const kb = el("button", { class: "btn btn-icon", "aria-label": "List actions" }, icon("more-vertical"));
    const items = [
      { icon: "folder-open", label: "Open", action: () => openListDrawer(d, isAll) },
    ];
    if (!isAll) {
      items.push(
        { icon: "pencil", label: "Rename", action: () => renameList(d.id, d.name) },
        { icon: "send", label: "Use in a campaign", action: () => useInCampaign(d.id) },
        { icon: "download", label: "Export CSV", action: () => exportListCsv(d.id, d.name) },
        "-",
        { icon: "trash-2", label: "Delete list", danger: true, action: async () => {
          const ok = await confirmSimple({
            title: "Delete this list?",
            body: "Its contacts stay in All contacts.",
            actionLabel: "Delete list",
          });
          if (!ok) return;
          try { await api("/api/sheets/" + d.id + "/delete", { body: {} }); toast("List deleted."); load(); }
          catch (e) { toast(e.message, { type: "error" }); }
        } },
      );
    } else {
      items.push({ icon: "download", label: "Export CSV", action: () => window.location.href = "/api/export/contacts.csv" });
    }
    kb.onclick = (e) => { e.stopPropagation(); menu(kb, items); };
    tr.append(el("td", { style: "text-align:right" }, kb));
    return tr;
  }
  $("ls-q").addEventListener("input", () => { state.q = $("ls-q").value.trim(); render(); });
  $("ls-import").onclick = () => importModal(load);
  load();
}

function importDropzone(big) {
  const file = el("input", { type: "file", accept: ".csv,.xlsx,.xls", style: "display:none" });
  const dz = el("div", { class: "dropzone" + (big ? " big" : ""), style: "max-width:520px;margin:var(--sp-8) auto;cursor:pointer", role: "button", tabindex: "0" },
    icon("upload"),
    el("div", { class: "empty-title", style: "margin-top:10px" }, "Drop a CSV or Excel file here"),
    el("p", { class: "empty-text" }, "First row = column names. Or click to choose a file."),
  );
  const pick = () => file.click();
  dz.onclick = pick;
  dz.onkeydown = (e) => { if (e.key === "Enter") pick(); };
  ["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) upload(f, null, null); });
  file.onchange = () => { if (file.files[0]) upload(file.files[0], null, null); };
  async function upload(f, name, campaignId) {
    dz.innerHTML = ""; dz.append(el("div", { class: "skel", style: "width:50%" }));
    const fd = new FormData();
    fd.append("file", f);
    if (name) fd.append("name", name);
    if (campaignId) fd.append("campaign_id", campaignId);
    try {
      const r = await fetch("/api/sheets/upload", { method: "POST", body: fd }).then((x) => x.json());
      if (!r.ok) throw new Error(r.error || "Import failed");
      toast("List " + r.name + " created with " + fmtInt(r.n_rows) + " " + plural(r.n_rows, "contact") + ".",
        { duration: 8000 });
      if (r.no_email) toast(r.no_email + " rows have no email address.", { type: "info" });
      if (typeof upload.reload === "function") upload.reload();
    } catch (e) {
      toast(e.message, { type: "error" });
      if (typeof upload.reload === "function") upload.reload();
    }
    file.value = "";
  }
  return dz;
}

function importModal(reload) {
  const file = el("input", { type: "file", accept: ".csv,.xlsx,.xls", style: "display:none" });
  const name = el("input", { class: "input", placeholder: "List name (defaults to the file name)", "aria-label": "List name" });
  const camSel = el("select", { class: "select", style: "width:100%", "aria-label": "Add to campaign" });
  camSel.append(el("option", { value: "0" }, "No campaign (yet)"));
  api("/api/campaigns").then((d) => {
    for (const c of d.campaigns) camSel.append(el("option", { value: String(c.id) }, c.name));
  }).catch(() => {});
  const dz = el("div", { class: "dropzone", style: "cursor:pointer", role: "button", tabindex: "0" },
    icon("upload"),
    el("div", { style: "margin-top:8px;font-size:13px" }, "Drop a CSV or Excel file here, or click to choose"),
  );
  let picked = null;
  const pick = () => file.click();
  dz.onclick = pick;
  dz.onkeydown = (e) => { if (e.key === "Enter") pick(); };
  ["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) setFile(f); });
  file.onchange = () => { if (file.files[0]) setFile(file.files[0]); };
  function setFile(f) {
    picked = f;
    dz.innerHTML = "";
    dz.append(icon("file-spreadsheet", "i i-16"), el("div", { style: "margin-top:8px;font-size:13px" }, f.name));
    if (!name.value) name.value = f.name.replace(/\.[^.]+$/, "").replace(/[_-]/g, " ").trim();
  }
  const btn = el("button", { class: "btn btn-primary" }, "Import contacts");
  const m = modal({ title: "Import a list", wide: true,
    body: el("div", { style: "display:flex;flex-direction:column;gap:12px" },
      dz,
      el("div", { class: "field" }, el("label", { class: "field-label" }, "List name"), name),
      el("div", { class: "field" }, el("label", { class: "field-label" }, "Add to campaign"), camSel),
      el("div", { class: "t-caption" }, "First row = column names. Rows without an email address are imported but flagged.")),
    foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), btn] });
  btn.onclick = async () => {
    if (!picked) { toast("Choose a file first.", { type: "error" }); return; }
    btn.disabled = true; btn.classList.add("is-loading");
    const fd = new FormData();
    fd.append("file", picked);
    if (name.value.trim()) fd.append("name", name.value.trim());
    if (camSel.value !== "0") fd.append("campaign_id", camSel.value);
    try {
      const r = await fetch("/api/sheets/upload", { method: "POST", body: fd }).then((x) => x.json());
      if (!r.ok) throw new Error(r.error || "Import failed");
      m.close();
      toast("List " + r.name + " created with " + fmtInt(r.n_rows) + " " + plural(r.n_rows, "contact") + ".");
      if (r.no_email) toast(r.no_email + " rows have no email address.", { type: "info" });
      if (reload) reload();
    } catch (e) { toast(e.message, { type: "error" }); }
    btn.disabled = false; btn.classList.remove("is-loading");
    file.value = ""; picked = null;
  };
}

async function openListDrawer(d, isAll) {
  const d0 = drawer({
    title: d.name,
    sub: fmtInt(d.n) + " " + plural(d.n, "contact"),
    body: el("div", { class: "t-caption" }, "Loading…"),
  });
  try {
    const c = await api("/api/contacts");
    const rows = (c.rows || []).filter((x) => isAll ? true : Number(x.sheet_id) === Number(d.id));
    const q = el("div", { class: "search-input", style: "margin-bottom:10px" },
      icon("search", "i i-16"),
      el("input", { class: "input", placeholder: "Search this list", "aria-label": "Search this list" }));
    const wrap = el("div", { class: "table-wrap" });
    d0.body.innerHTML = "";
    d0.body.append(q, wrap);
    function paint(filt) {
      const f = (filt || "").toLowerCase();
      const rs = rows.filter((x) => !f || [x.company, x.hr_name, x.hr_email].some((v) => (v || "").toLowerCase().includes(f)));
      wrap.innerHTML = "";
      if (!rs.length) { wrap.append(emptyState({ icon: "contacts", title: "This list has no contacts yet." })); return; }
      const t = el("table", { class: "table", "aria-label": "Contacts" },
        el("thead", {}, el("tr", {},
          el("th", {}, "No."), el("th", {}, "Company"), el("th", {}, "HR name"), el("th", {}, "HR email"), el("th", {}, "Status"))),
        el("tbody", {}));
      const tb = t.tBodies[0];
      for (const x of rs.slice(0, 200)) {
        const sCls = { new: "neutral", pending: "ready", drafted: "drafted", scheduled: "scheduled", sent: "sent", replied: "replied", bounced: "bounced", failed: "failed" }[x.status] || "neutral";
        const sLabel = { new: "Not contacted", pending: "Ready", drafted: "In Gmail drafts", scheduled: "Scheduled", sent: "Sent", replied: "Replied", bounced: "Bounced", failed: "Failed" }[x.status] || x.status;
        tb.append(el("tr", {},
          el("td", { class: "t-mono t-caption" }, x.serial || "—"),
          el("td", {}, x.company || "—"),
          el("td", {}, x.hr_name || "—"),
          el("td", { class: "t-mono t-caption" }, x.hr_email || "—"),
          el("td", {}, el("span", { class: "badge " + sCls }, sLabel))));
      }
      wrap.append(t);
      if (rs.length > 200) wrap.append(el("div", { class: "t-caption t-faint", style: "padding:10px" }, "Showing first 200 of " + fmtInt(rs.length) + "."));
    }
    let t2;
    q.querySelector("input").addEventListener("input", (e) => { clearTimeout(t2); t2 = setTimeout(() => paint(e.target.value.trim()), 200); });
    paint("");
  } catch (e) {
    d0.body.innerHTML = "";
    d0.body.append(errorCard("Could not load this list.", { onRetry: () => { d0.close(); openListDrawer(d, isAll); } }));
  }
}

function renameList(id, current) {
  const inp = el("input", { class: "input", value: current, "aria-label": "New name" });
  const btn = el("button", { class: "btn btn-primary" }, "Rename");
  const m = modal({ title: "Rename list",
    body: el("div", { class: "field" }, el("label", { class: "field-label" }, "List name"), inp),
    foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), btn] });
  btn.onclick = async () => {
    const v = inp.value.trim();
    if (!v) { toast("Give the list a name.", { type: "error" }); return; }
    try {
      await api("/api/sheets/" + id, { body: { name: v } });
      m.close();
      toast("List renamed.");
      location.reload();
    } catch (e) { toast(e.message, { type: "error" }); }
  };
}

function useInCampaign(id) {
  const sel = el("select", { class: "select", style: "width:100%", "aria-label": "Campaign" });
  const btn = el("button", { class: "btn btn-primary" }, "Use in campaign");
  const m = modal({ title: "Use in a campaign",
    body: el("div", { class: "field" }, el("label", { class: "field-label" }, "Campaign"), sel),
    foot: [el("button", { class: "btn btn-ghost" }, "Cancel"), btn] });
  api("/api/campaigns").then((d) => {
    sel.append(el("option", { value: "0" }, "No campaign"));
    for (const c of d.campaigns) sel.append(el("option", { value: String(c.id) }, c.name));
  }).catch(() => {});
  btn.onclick = async () => {
    if (!sel.value || sel.value === "0") { toast("Pick a campaign first.", { type: "error" }); return; }
    try {
      await api("/api/sheets/" + id + "/campaign", { body: { campaign_id: Number(sel.value) } });
      m.close();
      toast("List linked to the campaign.");
      location.reload();
    } catch (e) { toast(e.message, { type: "error" }); }
  };
}

async function exportListCsv(id, name) {
  try {
    const d = await api("/api/sheets/" + id);
    const sh = d.sheet;
    const hdrs = sh.headers || [];
    const lines = [hdrs];
    for (const r of (sh.rows || []).slice(0, 5000)) lines.push(hdrs.map((h) => r[h]));
    const csv = lines.map((row) => row.map((v) => '"' + String(v ?? "").replace(/"/g, '""') + '"').join(",")).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = (name || "list").replace(/[^\w\-]+/g, "_") + ".csv";
    a.click();
    URL.revokeObjectURL(a.href);
    toast("List exported as CSV.");
  } catch (e) { toast(e.message, { type: "error" }); }
}

init();
