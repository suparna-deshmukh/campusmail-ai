/* Ask AI draft editor - the existing rich-text editing pattern (toolbar,
   white paper, fill-in pills) reused inside cards. Exposes makeEditor().
   Registered in base_new.html with a cache-busting version. */
import { el, icon, esc } from "./ui.js";

const FIELD_LABELS = {
  serial: "No.", company: "Company", hr_name: "Recruiter name", hr_email: "Email",
  first_subject: "Original subject",
};
const FIELD_RE = /\{([a-zA-Z0-9_]{1,40})\}/g;

function pillHtml(name, label) {
  return '<span class="fill-pill ask-pill" data-f="' + esc(name) + '" contenteditable="false">' + esc(label) + "</span>";
}

function wrapPills(html) {
  return String(html || "").replace(FIELD_RE, (raw, name) => {
    const clean = name.trim();
    return pillHtml(clean, FIELD_LABELS[clean] || clean);
  });
}

function unwrapPills(html) {
  let h = String(html || "");
  h = h.replace(/<span class="fill-pill ask-pill" data-f="([^"]*)"[^>]*>[\s\S]*?<\/span>/g, (_, f) => "{" + f + "}");
  return h;
}

/* Build a contenteditable body with the standard toolbar.
   Returns {node, html(), text(), set(html), focus()}. */
export function makeEditor(initialHtml, { fields = [], paper = true, toolbar = true } = {}) {
  const body = el("div", {
    class: paper ? "ed-body ask-paper-body" : "ed-body",
    contenteditable: "true", "aria-label": "Email body", spellcheck: "true",
  });
  body.innerHTML = wrapPills(initialHtml || "");

  const out = {
    body,
    html: () => unwrapPills(body.innerHTML),
    text: () => (body.innerText || "").trim(),
    set(html) { body.innerHTML = wrapPills(html || ""); },
    focus() { body.focus(); },
    dirty: false,
    _markDirty() { out.dirty = true; },
  };

  if (toolbar) {
    const btn = (ico, label, fn) => {
      const b = el("button", { class: "btn btn-icon", "aria-label": label, title: label,
        mousedown: (e) => e.preventDefault() }, icon(ico));
      b.addEventListener("click", () => { body.focus(); fn(); out._markDirty(); });
      return b;
    };
    const undo = el("button", { class: "btn btn-icon", "aria-label": "Undo", title: "Undo",
      mousedown: (e) => e.preventDefault() }, icon("undo-2"));
    undo.addEventListener("click", () => { body.focus(); document.execCommand("undo"); });
    const redo = el("button", { class: "btn btn-icon", "aria-label": "Redo", title: "Redo",
      mousedown: (e) => e.preventDefault() }, icon("redo-2"));
    redo.addEventListener("click", () => { body.focus(); document.execCommand("redo"); });

    const fillBtn = el("button", { class: "btn btn-ghost btn-sm", "aria-label": "Insert fill-in field",
      title: "Insert fill-in field" }, icon("tag", "i i-14"), "Field");
    fillBtn.addEventListener("click", () => {
      const list = (fields && fields.length ? fields : ["serial", "company", "hr_name", "hr_email"])
        .map((f) => ({ label: FIELD_LABELS[f] || f, action: () => insertField(f) }));
      openFieldMenu(fillBtn, list);
    });

    const bar = el("div", { class: "ed-toolbar ask-ed-toolbar", role: "toolbar" },
      btn("bold", "Bold", () => document.execCommand("bold")),
      btn("italic", "Italic", () => document.execCommand("italic")),
      btn("underline", "Underline", () => document.execCommand("underline")),
      el("span", { class: "ed-toolbar-sep" }),
      btn("list", "Bulleted list", () => document.execCommand("insertUnorderedList")),
      btn("list-ordered", "Numbered list", () => document.execCommand("insertOrderedList")),
      el("span", { class: "ed-toolbar-sep" }),
      undo, redo,
      el("span", { class: "ed-toolbar-sep" }),
      fillBtn);

    function insertField(f) {
      body.focus();
      document.execCommand("insertHTML", false, pillHtml(f, FIELD_LABELS[f] || f) + "&nbsp;");
      out._markDirty();
    }
    return { node: wrapInBar(bar, body, out), body, html: out.html, text: out.text,
             set: out.set, focus: out.focus, markDirty: out._markDirty };
  }
  return { node: body, body, html: out.html, text: out.text, set: out.set,
           focus: out.focus, markDirty: out._markDirty };
}

function openFieldMenu(anchor, items) {
  const m = el("div", { class: "menu ask-field-menu", role: "menu",
    style: "position:absolute; z-index:60; min-width:180px" });
  const rect = anchor.getBoundingClientRect();
  m.style.top = (rect.bottom + window.scrollY + 4) + "px";
  m.style.left = Math.min(rect.left + window.scrollX, window.scrollX + document.documentElement.clientWidth - 200) + "px";
  for (const it of items) {
    m.append(el("button", { class: "menu-item", role: "menuitem",
      onclick: () => { m.remove(); document.removeEventListener("click", outside); it.action(); } }, it.label));
  }
  const outside = (e) => { if (!m.contains(e.target)) { m.remove(); document.removeEventListener("click", outside); } };
  document.body.append(m);
  setTimeout(() => document.addEventListener("click", outside), 0);
}

function wrapInBar(bar, body, out) {
  return el("div", { class: "ask-ed" }, bar, body);
}

window.CMAiEditor = { makeEditor, FIELD_LABELS, wrapPills, unwrapPills };
