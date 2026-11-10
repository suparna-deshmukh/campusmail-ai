/* CampusMail AI frontend - vanilla JS, no build step. Python 3.9-era browsers all fine. */

// ---------- helpers ----------
function $(sel, root) { return (root || document).querySelector(sel); }
function $all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function fmtTime(iso) { if (!iso) return "-"; try { return new Date(iso).toLocaleString(); } catch (e) { return iso; } }
function embedLocal(body) {
  return String(body == null ? "" : body).replace(/(src=["'])local:([A-Za-z0-9._\-]+)\1/g, "$1/local_img/$2");
}
function pill(status) { return '<span class="pill ' + esc(status) + '">' + esc(status) + "</span>"; }

async function api(path, opts) {
  const o = opts || {};
  const init = { method: o.method || (o.body !== undefined ? "POST" : "GET"), headers: {} };
  if (o.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(o.body); }
  const res = await fetch(path, init);
  if (res.status === 401 && !path.startsWith("/api/login")) { location.href = "/login"; throw new Error("Not signed in"); }
  let data = {};
  try { data = await res.json(); } catch (e) { data = { ok: false, error: "HTTP " + res.status }; }
  if (!data.ok) throw new Error(data.error || "Request failed");
  return data;
}
function toast(msg, type) {
  const t = document.createElement("div");
  t.className = "toast " + (type || "");
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 4500);
}

const Modal = {
  open(title, bodyHtml, footHtml, wide) {
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = bodyHtml;
    $("#modal-foot").innerHTML = footHtml || "";
    $("#modal-box").classList.toggle("wide", !!wide);
    $("#modal-backdrop").classList.add("open");
  },
  close() { $("#modal-backdrop").classList.remove("open"); },
};
document.addEventListener("click", (e) => {
  if (e.target === $("#modal-backdrop") || e.target.hasAttribute("data-close")) Modal.close();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") Modal.close(); });

function typeToConfirm(title, bodyHtml, word, onConfirm, confirmLabel) {
  Modal.open(title, bodyHtml +
    '<div class="field" style="margin-top:16px"><label>Type ' + esc(word) + ' to confirm</label>' +
    '<input class="input" id="confirm-word" placeholder="' + esc(word) + '"></div>',
    '<button class="btn ghost" data-close>Cancel</button>' +
    '<button class="btn danger" id="confirm-go" disabled>' + esc(confirmLabel || word) + "</button>");
  const inp = $("#confirm-word");
  inp.focus();
  inp.addEventListener("input", () => { $("#confirm-go").disabled = inp.value.trim().toUpperCase() !== word; });
  $("#confirm-go").addEventListener("click", async () => {
    $("#confirm-go").disabled = true;
    Modal.close();
    try { await onConfirm(); } catch (err) { toast(err.message, "err"); }
  });
}

// ---------- account picker (shared) ----------
async function fillAccountSelect(sel) {
  if (!sel) return "";
  const keep = sel.value;
  let last = 0;
  try {
    const d = await api("/api/accounts");
    last = d.last_account_id || 0;
    const conn = d.accounts.filter((a) => a.connected);
    const preferred = conn.some((a) => String(a.id) === String(keep)) ? keep : String(last);
    sel.innerHTML = conn.length
      ? conn.map((a) => '<option value="' + a.id + '"' + (String(a.id) === preferred ? " selected" : "") + ">" + esc(a.email || a.label) + (a.mine ? " (mine)" : "") + "</option>").join("")
      : '<option value="">- no connected account -</option>';
    if (!conn.length) {
      const add = document.createElement("option");
      add.value = "add"; add.textContent = "+ connect one in Settings";
      sel.appendChild(add);
    }
  } catch (e) { /* not signed in */ }
  return sel.value || "";
}
function requireAccount(sel) {
  const v = sel ? sel.value : "";
  if (!v) { toast("No Gmail send account connected. Settings, Gmail send-accounts.", "err"); return 0; }
  if (v === "add") { location.href = "/settings"; return 0; }
  return +v;
}

// ---------- global state ----------
let STATE = null;
async function refreshState() {
  try { STATE = await api("/api/state"); } catch (e) { return; }
  const safe = STATE.settings.safe_mode === "1";
  $("#sf-safe").textContent = safe ? "Safe mode ON - drafts only" : "Safe mode OFF - live sends armed";
  $("#sf-dot").className = "dot " + (safe ? "" : "on");
  const g = STATE.gmail || {};
  $("#sf-gmail").textContent = g.connected_count ? g.accounts.map((a) => a.email).join(", ") : "No send account";
  if (STATE.me) $("#sf-user").textContent = STATE.me.name || STATE.me.email;
  const chips = [];
  chips.push(safe
    ? '<span class="chip warn"><span class="dot"></span>Safe mode: no live sends</span>'
    : '<span class="chip on"><span class="dot"></span>Live sending armed</span>');
  if (g.connected_count) chips.push('<span class="chip on"><span class="dot"></span>' + esc((g.accounts[0] || {}).email || "Gmail") + (g.connected_count > 1 ? " +" + (g.connected_count - 1) : "") + "</span>");
  const sch = STATE.scheduler || {};
  if (sch.next_fire) chips.push('<span class="chip"><span class="dot" style="background:var(--warn)"></span>Next send: ' + esc(fmtTime(sch.next_fire)) + "</span>");
  if (sch.last_error) chips.push('<span class="chip danger"><span class="dot"></span>' + esc(sch.last_error.slice(0, 28)) + "...</span>");
  $("#topbar-chips").innerHTML = chips.join("");
  window.__safeMode = safe;
  window.__state = STATE;
  if (window.onState) window.onState(STATE);
}
setInterval(refreshState, 15000);

document.addEventListener("DOMContentLoaded", () => {
  const lo = $("#btn-logout");
  if (lo) lo.addEventListener("click", async () => {
    try { await fetch("/logout", { method: "POST" }); } catch (e) {}
    location.href = "/login";
  });
});

// ---------- pages ----------
const Pages = {};

Pages.dashboard = async function () {
  const s = STATE;
  const c = s.stats.contacts || {}, e = s.stats.emails || {};
  const cards = [
    ["Contacts in list", c.new || 0, "ready to process", "accent"],
    ["Drafted", c.drafted || 0, "in Gmail drafts", ""],
    ["Scheduled", c.scheduled || 0, "waiting for worker", "amber"],
    ["Sent", c.sent || 0, "delivered", "green"],
    ["Replied", c.replied || 0, "recruiters responded", "accent"],
    ["Queue (pending)", e.pending || 0, "generated, not sent", ""],
    ["Scheduled emails", e.scheduled || 0, "in send queue", "amber"],
    ["Sent emails", e.sent || 0, "total sent so far", "green"],
    ["Errors", (c.error || 0) + (e.error || 0), "need attention", ((c.error || 0) + (e.error || 0)) > 0 ? "red" : ""],
  ];
  $("#stat-cards").innerHTML = cards.map((x) =>
    '<div class="stat ' + x[3] + '"><div class="label">' + esc(x[0]) + '</div><div class="value">' + x[1] + '</div><div class="hint">' + esc(x[2]) + "</div></div>"
  ).join("");

  // checklist + nudge
  const o = s.onboard || {};
  const me = s.me || {};
  const items = [
    ["Sign in", true, "/login"],
    [me.role === "admin" ? "Team sign-in set up" : "Team is set up", true, "/settings"],
    ["Gmail account connected", o.gmail_connected, "/settings"],
    ["Company list loaded", (o.contacts || 0) > 0, "/contacts"],
    ["Template ready", (o.templates || 0) > 0, "/templates"],
    ["First batch generated", (o.batches || 0) > 0, "/merge"],
    ["First drafts/sends done", (o.first_send || 0) > 0, "/queue"],
  ];
  $("#checklist").innerHTML = items.map((x) =>
    '<span class="check ' + (x[1] ? "done" : "") + '"><span class="box">' + (x[1] ? "&#10003;" : "") + "</span>" + esc(x[0]) + "</span>"
  ).join("");
  const first = items.find((x) => !x[1]);
  $("#checklist-banner").innerHTML = first
    ? '<a class="btn primary sm" href="' + first[2] + '">Next: ' + esc(first[0]) + " &#8594;</a>"
    : '<span class="chip on"><span class="dot"></span>All set - ready to send</span>';

  const act = s.active_template;
  $("#active-tpl").innerHTML = act
    ? '<span style="font-weight:700">' + esc(act.name) + "</span> <span class='muted'>(id " + act.id + ")</span>"
    : "<span class='muted'>No template yet - add one in Templates.</span>";
  const batches = await api("/api/batches");
  $("#batch-count").textContent = batches.batches.length + " total";
  $("#batch-rows").innerHTML = batches.batches.length ? batches.batches.map((b) => {
    const total = Object.values(b.counts).reduce((a, x) => a + x, 0);
    return "<tr><td class='num'>" + b.id + "</td><td>" + esc(b.name) + "</td><td class='muted'>" + fmtTime(b.created_at) +
      "</td><td class='num'>" + total + "</td><td>" + Object.entries(b.counts).map(([k, v]) => pill(k) + " " + v).join(" ") + "</td></tr>";
  }).join("") : "<tr><td colspan='5' class='muted'>No batches yet - run a Mail Merge.</td></tr>";
  const actRows = await api("/api/activity");
  $("#recent-activity").innerHTML = actRows.rows.slice(0, 6).map((r) =>
    "<tr><td class='muted'>" + esc(r.ts) + "</td><td>" + pill(r.action) + "</td><td class='muted'>" + esc(r.actor || "") + "</td><td>" + esc(r.detail) + "</td></tr>"
  ).join("") || "<tr><td colspan='4' class='muted'>No activity yet.</td></tr>";
};

Pages.contacts = async function () {
  const state = { q: "", status: "", page: 1, per: 50, sel: new Set() };
  const load = async () => {
    const u = new URL("/api/contacts", location.origin);
    if (state.q) u.searchParams.set("q", state.q);
    if (state.status) u.searchParams.set("status", state.status);
    const data = await api(u.pathname + u.search);
    const rows = data.rows;
    const start = (state.page - 1) * state.per;
    const slice = rows.slice(start, start + state.per);
    $("#contact-total").textContent = data.total + " contacts";
    $("#contact-count").textContent = slice.length + " shown, page " + state.page + " of " + Math.max(1, Math.ceil(rows.length / state.per));
    $("#contact-rows").innerHTML = slice.map((r) =>
      "<tr><td><input type='checkbox' data-id='" + r.id + "' class='c-sel' " + (state.sel.has(r.id) ? "checked" : "") + "></td>" +
      "<td class='num'>" + esc(r.serial || "-") + "</td><td>" + esc(r.company) + "</td><td>" + esc(r.hr_name) +
      "</td><td>" + esc(r.hr_email) + "</td><td>" + pill(r.status) + "</td>" +
      "<td><button class='btn sm ghost' data-edit='" + r.id + "'>Edit</button></td></tr>"
    ).join("") || "<tr><td colspan='7' class='muted'>No contacts match.</td></tr>";
    $all(".c-sel").forEach((cb) => cb.addEventListener("change", () => {
      const id = +cb.dataset.id;
      cb.checked ? state.sel.add(id) : state.sel.delete(id);
      selInfo();
    }));
    $all("[data-edit]").forEach((b) => b.addEventListener("click", () => editContact(+b.dataset.edit)));
    selInfo();
  };
  const selInfo = () => { $("#sel-count").textContent = state.sel.size; };
  let deb;
  $("#contact-search").addEventListener("input", (e) => {
    clearTimeout(deb);
    deb = setTimeout(() => { state.q = e.target.value.trim(); state.page = 1; load(); }, 300);
  });
  $("#contact-status").addEventListener("change", (e) => { state.status = e.target.value; state.page = 1; load(); });
  $all("#contact-pager .btn").forEach((b) => b.addEventListener("click", () => { state.page = Math.max(1, state.page + (+b.dataset.d)); load(); }));
  $("#btn-contact-import").addEventListener("click", () => $("#contact-file").click());
  $("#contact-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    const res = await fetch("/api/contacts/import", { method: "POST", body: fd });
    const d = await res.json();
    if (d.ok) { toast("Imported: " + d.added + " added, " + d.updated + " updated (" + d.skipped + " skipped)", "ok"); load(); refreshState(); }
    else toast(d.error, "err");
    e.target.value = "";
  });
  $("#btn-contact-export").addEventListener("click", () => { location.href = "/api/export/contacts.csv"; });
  $("#btn-contact-add").addEventListener("click", () => {
    Modal.open("Add contact",
      '<div class="field"><label>Company</label><input class="input" id="ac-company"></div>' +
      '<div class="field"><label>HR name</label><input class="input" id="ac-name"></div>' +
      '<div class="field"><label>HR email</label><input class="input" id="ac-email"></div>' +
      '<div class="field"><label>Serial (optional)</label><input class="input" id="ac-serial"></div>',
      '<button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="ac-save">Add</button>');
    $("#ac-save").addEventListener("click", async () => {
      try {
        await api("/api/contacts", { body: {
          company: $("#ac-company").value, hr_name: $("#ac-name").value,
          hr_email: $("#ac-email").value, serial: $("#ac-serial").value,
        }});
        Modal.close(); toast("Contact added", "ok"); load(); refreshState();
      } catch (err) { toast(err.message, "err"); }
    });
  });
  $("#btn-contact-delete").addEventListener("click", () => {
    if (!state.sel.size) return toast("Select contacts first", "err");
    typeToConfirm("Delete contacts", "This removes " + state.sel.size + " contact(s) from the list. Generated emails in the queue are not touched.", "DELETE",
      async () => {
        await api("/api/contacts/delete", { body: { ids: Array.from(state.sel) } });
        state.sel.clear(); toast("Deleted", "ok"); load(); refreshState();
      }, "Delete");
  });
  function editContact(id) {
    api("/api/contacts").then((data) => {
      const r = data.rows.find((x) => x.id === id);
      if (!r) return;
      Modal.open("Edit contact - " + esc(r.company),
        '<div class="field"><label>Company</label><input class="input" id="ec-company" value="' + esc(r.company) + '"></div>' +
        '<div class="field"><label>HR name</label><input class="input" id="ec-name" value="' + esc(r.hr_name) + '"></div>' +
        '<div class="field"><label>HR email</label><input class="input" id="ec-email" value="' + esc(r.hr_email) + '"></div>' +
        '<div class="field"><label>Notes</label><textarea class="input" id="ec-notes" rows="3">' + esc(r.notes || "") + "</textarea></div>",
        '<button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="ec-save">Save</button>');
      $("#ec-save").addEventListener("click", async () => {
        try {
          await api("/api/contacts/" + id, { body: {
            company: $("#ec-company").value, hr_name: $("#ec-name").value,
            hr_email: $("#ec-email").value, notes: $("#ec-notes").value,
          }});
          Modal.close(); toast("Saved", "ok"); load();
        } catch (err) { toast(err.message, "err"); }
      });
    });
  }
  load();
};

Pages.templates = async function () {
  const load = async () => {
    const data = await api("/api/templates");
    const grid = $("#tpl-grid");
    if (!data.templates.length) {
      grid.innerHTML = "<div class='empty' style='grid-column:1/-1'><div class='big'>&#9998;</div><p>No templates yet.</p><p>Upload a Word/HTML/text template, or create one with AI.</p></div>";
      return;
    }
    grid.innerHTML = data.templates.map((t) =>
      "<div class='card' style='padding:18px;position:relative'>" +
      (t.is_active ? "<span class='pill drafted' style='position:absolute;top:14px;right:14px'>ACTIVE</span>" : "") +
      "<div style='font-weight:700;font-size:14.5px;margin-bottom:4px'>" + esc(t.name) + "</div>" +
      "<div class='muted' style='margin-bottom:10px'>Subject: " + esc(t.subject || "(none)") + "</div>" +
      "<div style='margin-bottom:12px'>" + (t.fields.map((f) => "<span class='chip' style='margin:2px 3px 2px 0;font-size:11px'>" + esc(f) + "</span>").join("") || "<span class='muted'>no fields</span>") + "</div>" +
      "<div style='display:flex;gap:7px;flex-wrap:wrap'>" +
      "<button class='btn sm primary' data-act='edit' data-id='" + t.id + "'>Edit</button>" +
      "<button class='btn sm' data-act='preview' data-id='" + t.id + "'>Preview</button>" +
      (STATE && STATE.ai_has_key === "1" ? "<button class='btn sm ai' data-act='polish' data-id='" + t.id + "'>&#10022; AI polish</button>" : "") +
      "<button class='btn sm' data-act='activate' data-id='" + t.id + "'>Set active</button>" +
      "<button class='btn sm ghost' data-act='del' data-id='" + t.id + "'>Delete</button>" +
      "</div></div>"
    ).join("");
    $all("[data-act]", grid).forEach((b) => b.addEventListener("click", () => {
      const id = +b.dataset.id;
      if (b.dataset.act === "preview") tplPreview(id);
      if (b.dataset.act === "edit") tplEdit(data.templates.find((x) => x.id === id));
      if (b.dataset.act === "activate") api("/api/templates/" + id + "/activate", { body: {} }).then(() => { toast("Template set active", "ok"); load(); refreshState(); });
      if (b.dataset.act === "polish") {
        b.disabled = true;
        api("/api/templates/" + id + "/polish", { body: {} }).then(() => { toast("Template polished by AI", "ok"); load(); })
          .catch((err) => toast(err.message, "err"));
      }
      if (b.dataset.act === "del") typeToConfirm("Delete template", "Delete this template? Queued emails already generated keep their rendered bodies.", "DELETE",
        () => api("/api/templates/" + id + "/delete", { body: {} }).then(() => { toast("Deleted", "ok"); load(); }), "Delete");
    }));
    const editId = new URLSearchParams(location.search).get("edit");
    if (editId && data.templates.some((x) => x.id === +editId)) {
      const reveal = new URLSearchParams(location.search).get("reveal");
      history.replaceState(null, "", location.pathname);
      tplEdit(data.templates.find((x) => x.id === +editId), reveal);
    }
  };
  function tplEdit(t, reveal) {
    Modal.open("Edit template",
      "<p class='hint' style='margin-top:0'>The whole template is below - <b>subject on the first line</b>, then the body. Click anywhere and type, erase or press Enter just like in Word. Field codes like <b>[Recruiter Name]</b> work in both.</p>" +
      '<div class="field"><label>Template name</label><input class="input" id="te-name" value="' + esc(t.name) + '"></div>' +
      '<div class="ed-toolbar">' +
      '<button type="button" data-cmd="bold" title="Bold"><b>B</b></button>' +
      '<button type="button" data-cmd="italic" title="Italic"><i>I</i></button>' +
      '<button type="button" data-cmd="underline" title="Underline"><u>U</u></button>' +
      '<button type="button" data-cmd="insertUnorderedList" title="Bulleted list">&bull; list</button>' +
      '<button type="button" data-cmd="insertOrderedList" title="Numbered list">1. list</button>' +
      '<span style="width:12px"></span>' +
      '<button type="button" data-cmd="undo" title="Undo">&#8630;</button>' +
      '<button type="button" data-cmd="redo" title="Redo">&#8631;</button>' +
      "</div>" +
      '<div class="ed-doc">' +
      '<div class="ed-subj-line"><span class="ed-subj-label">Subject:</span><div id="te-subject" contenteditable="true">' + esc(t.subject || "") + "</div></div>" +
      '<div class="ed-div"></div>' +
      '<div class="ed-area" id="te-body" contenteditable="true">' + embedLocal(t.body_html) + "</div>" +
      "</div>",
      "<button class='btn ghost' data-close>Cancel</button><button class='btn primary' id='te-save'>Save changes</button>", true);
    const edBody = $("#te-body");
    const edSubj = $("#te-subject");
    let focusEl = edBody;
    [edSubj, edBody].forEach((el) => el.addEventListener("focus", () => { focusEl = el; }));
    $all(".ed-toolbar button").forEach((btn) => btn.addEventListener("mousedown", (e) => {
      e.preventDefault();
      focusEl.focus();
      document.execCommand(btn.dataset.cmd, false, null);
    }));
    let imgBar = null;
    let imgSel = null;
    function closeImgBar() {
      if (imgBar) { imgBar.remove(); imgBar = null; }
      if (imgSel) { imgSel.classList.remove("img-sel"); imgSel = null; }
    }
    function selectImg(im) {
      if (imgSel === im) return;
      closeImgBar();
      imgSel = im;
      im.classList.add("img-sel");
      const doc = $("#te-body").closest(".ed-doc");
      const dr = doc.getBoundingClientRect();
      const ir = im.getBoundingClientRect();
      imgBar = document.createElement("div");
      imgBar.className = "img-bar";
      imgBar.innerHTML = "<span>Image</span>" +
        [25, 50, 75, 100].map((pc) => "<button data-w='" + pc + "'>" + pc + "%</button>").join("") +
        "<button data-w='auto'>Fit</button>" +
        "<button data-w='del' title='Remove image'>&#10005;</button>";
      doc.appendChild(imgBar);
      const bw = imgBar.offsetWidth;
      imgBar.style.left = Math.max(4, Math.min(ir.left - dr.left, dr.width - bw - 4)) + "px";
      imgBar.style.top = (ir.bottom - dr.top + $("#te-body").scrollTop + 6) + "px";
      $all("button", imgBar).forEach((b) => b.addEventListener("mousedown", (e) => {
        e.preventDefault();
        const w = b.dataset.w;
        if (w === "del") { im.remove(); closeImgBar(); return; }
        if (w === "auto") { im.style.width = ""; im.style.height = "auto"; im.removeAttribute("width"); return; }
        const cw = edBody.clientWidth - 36;
        im.style.width = Math.round((cw * +w) / 100) + "px";
        im.style.height = "auto";
      }));
    }
    edBody.addEventListener("click", (e) => {
      const im = e.target && e.target.closest ? e.target.closest("img") : null;
      if (im) selectImg(im); else closeImgBar();
    });
    edBody.addEventListener("scroll", closeImgBar);
    $("#te-save").addEventListener("click", async () => {
      const subject = edSubj.textContent.replace(/\s+/g, " ").trim();
      const body = edBody.innerHTML.replace(/\/local_img\//g, "local:");
      try {
        await api("/api/templates/" + t.id, { body: { name: $("#te-name").value.trim(), subject: subject, body_html: body } });
        Modal.close(); toast("Template saved", "ok"); load(); refreshState();
      } catch (e) { toast(e.message, "err"); }
    });
    if (reveal === "img") {
      const firstImg = edBody.querySelector("img");
      if (firstImg) setTimeout(() => { firstImg.scrollIntoView({ block: "center" }); selectImg(firstImg); }, 400);
    }
  }
  function tplPreview(id) {
    Modal.open("Template preview",
      '<p class="hint" style="margin-top:0">Sample - rendered with your <b>first contact</b> from the sheet, so you can see exactly how the fields fill. Every company gets its own row (Dear <recruiter>, invite <organisation>, ...).</p>' +
      '<div id="tpl-subj" style="margin-bottom:10px"></div><iframe class="email-frame" id="tpl-frame"></iframe>',
      "<button class='btn ghost' data-close>Close</button>", true);
    api("/api/templates/" + id + "/preview").then((d) => {
      $("#tpl-subj").innerHTML = "<b>Subject:</b> " + esc(d.subject);
      $("#tpl-frame").srcdoc = "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body style='margin:0;background:#fff'>" + embedLocal(d.body) + "</body></html>";
    }).catch((e) => toast(e.message, "err"));
  }
  $("#btn-tpl-upload").addEventListener("click", () => {
    Modal.open("Upload template",
      "<p class='hint' style='margin-top:0'>The <b>subject is entered separately</b> from the body. Body: upload a Word (.docx) / HTML / text file, or paste it. Mark replaceable values as {field}, [Field] or guillemet-Field. Images in Word files come along automatically.</p>" +
      '<div class="field"><label>Template name</label><input class="input" id="tu-name" placeholder="e.g. Diwali Outreach"></div>' +
      '<div class="field"><label>Subject line (required - may include fields like [Company Name])</label><input class="input" id="tu-subject" placeholder="e.g. Invitation from the campus: 2027 Graduating Batch Drives"></div>' +
      '<div class="field"><label>Body - upload a file</label><input class="input" id="tu-file" type="file" accept=".docx,.html,.txt"></div>' +
      '<div class="field"><label>...or paste the body text here (leave one of the two empty)</label><textarea class="input" id="tu-body" rows="5" placeholder="Dear [Recruiter Name], ..."></textarea></div>',
      '<button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="tu-go">Upload</button>');
    $("#tu-go").addEventListener("click", async () => {
      if (!$("#tu-subject").value.trim()) return toast("Enter the subject line", "err");
      const f = $("#tu-file").files[0];
      const pasted = $("#tu-body").value.trim();
      if (!f && !pasted) return toast("Choose a file or paste the body", "err");
      const fd = new FormData();
      if (f) fd.append("file", f);
      else fd.append("body_html", pasted);
      fd.append("name", $("#tu-name").value);
      fd.append("subject", $("#tu-subject").value.trim());
      const res = await fetch("/api/templates/upload", { method: "POST", body: fd });
      const d = await res.json();
      if (d.ok) { Modal.close(); toast("Template added - " + d.fields.length + " fields detected", "ok"); load(); refreshState(); }
      else toast(d.error, "err");
    });
  });
  $("#btn-tpl-ai").addEventListener("click", () => {
    Modal.open("AI: create a template",
      '<div class="field"><label>Template name</label><input class="input" id="ai-name" placeholder="e.g. New Year Outreach"></div>' +
      '<div class="field"><label>Brief - what is this email about, who is it for, what should it achieve?</label><textarea class="input" id="ai-brief" rows="5" placeholder="e.g. Invite recruiters to our campus recruiter drive in Jan 2027. Mention 464 students, 93% internship completion, ask for JDs."></textarea></div>' +
      '<div class="field"><label>Tone</label><select class="input" id="ai-tone"><option>professional</option><option>warm</option><option>concise</option><option>formal</option></select></div>',
      '<button class="btn ghost" data-close>Cancel</button><button class="btn ai" id="ai-go">&#10022; Generate</button>');
    $("#ai-go").addEventListener("click", async () => {
      $("#ai-go").disabled = true;
      try {
        await api("/api/templates/ai", { body: { name: $("#ai-name").value, brief: $("#ai-brief").value, tone: $("#ai-tone").value } });
        Modal.close(); toast("AI template created", "ok"); load(); refreshState();
      } catch (err) { toast(err.message, "err"); }
      $("#ai-go").disabled = false;
    });
  });
  $("#btn-tpl-ai-rem").addEventListener("click", () => {
    if (!(STATE && STATE.ai_has_key === "1")) return toast("Set an AI key in Settings first", "err");
    Modal.open("AI: write a reminder template",
      "<div class='field'><label>What is the reminder about?</label>" +
      "<textarea class='input' id='remctx' rows='3' placeholder='e.g. invitation for the 2027 graduating batch, drives starting January 2027'></textarea></div>" +
      "<div class='field'><label>Tone</label><select class='input' id='remtone'><option>professional</option><option>warm</option><option>concise</option></select></div>",
      "<button class='btn ghost' data-close>Cancel</button><button class='btn ai' id='remgo'>&#10022; Generate</button>");
    $("#remgo").addEventListener("click", async () => {
      $("#remgo").disabled = true;
      try {
        await api("/api/templates/ai-reminder", { body: { context: $("#remctx").value, tone: $("#remtone").value } });
        Modal.close(); toast("Reminder template created", "ok"); load();
      } catch (err) { toast(err.message, "err"); }
      $("#remgo").disabled = false;
    });
  });
  load();
};

Pages.merge = async function () {
  const S = { campaign: 0, campaignName: "", tpl: null, mapping: {}, fields: [],
              source: "main", sheetId: 0, sheetName: "", sheetRows: 0,
              sheetHeaders: [], sheetPreview: [], samples: {} };
  const templates = await api("/api/templates");
  const contacts = await api("/api/contacts");
  const campaigns = await api("/api/campaigns");
  const sheets = await api("/api/sheets");
  if (!templates.templates.length) {
    $all(".wiz-panel").forEach((p) => { p.style.display = "none"; });
    $("#dataset-info").innerHTML = "No templates yet - create one in the <b>Templates</b> page first, then come back.";
    return;
  }
  const steps = ["Campaign", "Template", "Data source", "Map fields", "Preview & generate"];
  $("#wiz-steps").innerHTML = steps.map((st, i) =>
    "<div class='step' id='step-" + (i + 1) + "'><span class='n'>" + (i + 1) + "</span>" + st + "</div>").join("");
  const panels = $all(".wiz-panel");
  S.cols = ["serial", "company", "hr_name", "hr_email"];

  function setInfo() {
    const src = S.source === "main"
      ? "main contact list (" + contacts.total + " contacts)"
      : "sheet '" + S.sheetName + "' (" + S.sheetRows + " rows, " + S.sheetHeaders.length + " columns)";
    $("#dataset-info").innerHTML = "Campaign: <b>" + esc(S.campaignName || "not chosen") +
      "</b> &nbsp;&middot;&nbsp; Data: <b>" + src + "</b>";
  }
  function showStep(n) {
    for (let i = 1; i <= 5; i++) $("#step-" + i).className = "step" + (i === n ? " active" : i < n ? " done" : "");
    panels.forEach((p, i) => { p.style.display = (i + 1 === n ? "" : "none"); });
    if (n === 4) loadMapping();
    if (n === 5) { fillAccountSelect($("#merge-account")); loadPreview(); }
  }

  // ---------- step 1: campaign ----------
  function renderCampaigns() {
    $("#cam-pick").innerHTML = campaigns.campaigns.length
      ? campaigns.campaigns.map((cm) =>
          "<label class='card' style='display:flex;align-items:flex-start;gap:10px;padding:12px 16px;cursor:pointer;margin-bottom:8px'>" +
          "<input type='radio' name='campaign' value='" + cm.id + "'" + (S.campaign === cm.id ? " checked" : "") + " style='margin-top:3px'> " +
          "<div style='flex:1;min-width:0'><b>" + esc(cm.name) + "</b>" +
          (cm.description ? " <span class='muted'>" + esc(cm.description) + "</span>" : "") +
          "<div class='muted' style='margin-top:2px;font-size:11.5px'>" + cm.n_contacts + " contacts, " +
          cm.n_batches + " batch(es), " + cm.n_emails + " emails" +
          (cm.n_emails ? " (" + (cm.status_counts.sent || 0) + " sent, " + (cm.status_counts.replied || 0) + " replied)" : "") +
          "</div></div></label>").join("")
      : "<div class='muted' style='padding:8px 2px'>No campaigns yet - create one below to keep this outreach organised.</div>";
    $all("input[name=campaign]").forEach((r) => r.addEventListener("change", () => pickCampaign(+r.value)));
  }
  function pickCampaign(id) {
    const cm = campaigns.campaigns.find((x) => x.id === id);
    S.campaign = id || 0;
    S.campaignName = cm ? cm.name : "";
    if (cm) {
      const bn = $("#batch-name");
      if (bn) bn.value = cm.name + " " + new Date().toLocaleDateString();
    }
    setInfo();
  }
  $("#btn-cam-create").addEventListener("click", async () => {
    const name = $("#new-cam-name").value.trim();
    if (!name) return toast("Give the campaign a name", "err");
    try {
      const d = await api("/api/campaigns", { body: { name: name, description: "" } });
      toast("Campaign created: " + name, "ok");
      const fresh = await api("/api/campaigns");
      campaigns.campaigns = fresh.campaigns;
      renderCampaigns();
      pickCampaign(d.id);
      showStep(2);
    } catch (e) { toast(e.message, "err"); }
  });
  renderCampaigns();
  const pc = new URLSearchParams(location.search).get("campaign");
  if (pc && campaigns.campaigns.some((x) => x.id === +pc)) pickCampaign(+pc);
  setInfo();

  // ---------- step 2: template ----------
  function renderTplCards() {
    $("#tpl-cards").innerHTML = templates.templates.map((t) =>
      "<label class='card' style='display:flex;align-items:flex-start;gap:10px;padding:14px 16px;cursor:pointer;margin-bottom:10px'>" +
      "<input type='radio' name='tpl' value='" + t.id + "' style='margin-top:3px'> " +
      "<div style='min-width:0'><b>" + esc(t.name) + "</b>" +
      (t.is_active ? " <span class='pill drafted'>active</span>" : "") +
      "<div class='muted' style='margin-top:4px'>" + esc(t.fields.join(", ") || "no fields") + "</div></div></label>"
    ).join("");
    $all("input[name=tpl]").forEach((r) => r.addEventListener("change", () => {
      S.tpl = +r.value;
      $all("input[name=tpl]").forEach((x) => x.parentElement.style.boxShadow = x === r ? "0 0 0 1px var(--primary)" : "");
    }));
  }
  renderTplCards();
  $("#btn-tpl-upload").addEventListener("click", () => {
    Modal.open("Upload a new template",
      '<p class="hint" style="margin-top:0">Upload a Word/HTML file <b>or</b> paste the body text. The subject line is a separate input - it is never pulled from the body.</p>' +
      '<div class="field"><label>Template name (optional)</label><input class="input" id="nu-name" placeholder="e.g. Diwali recruiter invite"></div>' +
      '<div class="field"><label>Subject line</label><input class="input" id="nu-subject" placeholder="Invitation from the campus: 2027 Graduating Batch Drives"></div>' +
      '<div class="field"><label>Body - upload a file (Word .docx, HTML, or text)</label><input type="file" id="nu-file" accept=".docx,.html,.txt" style="max-width:360px"></div>' +
      '<div class="field"><label>...or paste the body text here (leave one of the two empty)</label><textarea class="input" id="nu-body" rows="6" placeholder="Dear [Recruiter Name], ..."></textarea></div>',
      '<button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="nu-go">Create template</button>');
    $("#nu-go").addEventListener("click", async () => {
      if (!$("#nu-subject").value.trim()) return toast("Enter the subject line", "err");
      const fd = new FormData();
      fd.append("name", $("#nu-name").value.trim());
      fd.append("subject", $("#nu-subject").value.trim());
      const f = $("#nu-file").files[0];
      if (f) fd.append("file", f);
      else if ($("#nu-body").value.trim()) fd.append("body_html", $("#nu-body").value.trim());
      else return toast("Upload a file or paste the body", "err");
      try {
        const res = await fetch("/api/templates/upload", { method: "POST", body: fd });
        const d = await res.json();
        if (!d.ok) return toast(d.error || "Upload failed", "err");
        toast("Template created" + (d.fields.length ? " - fields: " + d.fields.join(", ") : ""), "ok");
        Modal.close();
        const t2 = await api("/api/templates");
        templates.templates = t2.templates;
        renderTplCards();
      } catch (e) { toast(e.message, "err"); }
    });
  });

  // ---------- step 3: data source ----------
  function renderSrc() {
    $("#src-main").innerHTML =
      "<label class='card' style='display:flex;align-items:flex-start;gap:10px;padding:12px 16px;cursor:pointer;margin-bottom:8px'>" +
      "<input type='radio' name='datasrc' value='main'" + (S.source === "main" ? " checked" : "") + " style='margin-top:3px'>" +
      "<div style='flex:1;min-width:0'><b>Use the main contact list</b>" +
      "<div class='muted' style='margin-top:3px'>" + contacts.total + " contacts, columns: <code class='inline'>serial</code> <code class='inline'>company</code> <code class='inline'>hr_name</code> <code class='inline'>hr_email</code></div></div></label>";
    $("#src-sheetsel").innerHTML = sheets.sheets.length
      ? sheets.sheets.map((sh) => "<option value='" + sh.id + "'" + (S.sheetId === sh.id ? " selected" : "") + ">" +
        esc(sh.name) + " - " + sh.n_rows + " rows</option>").join("")
      : "<option value=''>- none uploaded yet -</option>";
  }
  renderSrc();
  $all("input[name=datasrc]").forEach((r) => r.addEventListener("change", () => {
    if (r.value === "main") {
      S.source = "main"; S.sheetId = 0; S.sheetName = ""; S.sheetRows = 0; S.sheetHeaders = [];
      $("#sheet-summary").style.display = "none";
    } else if (r.value === "sheet") {
      const v = $("#src-sheetsel").value;
      if (!v) return toast("Pick a sheet from the list", "err");
      useSheet(+v).catch((e) => toast(e.message, "err"));
    }
    setInfo();
  }));
  $("#src-sheetsel").addEventListener("change", () => {
    if ($("input[name=datasrc][value=sheet]").checked && $("#src-sheetsel").value)
      useSheet(+$("#src-sheetsel").value).catch((e) => toast(e.message, "err"));
  });
  async function useSheet(sid) {
    const d = await api("/api/sheets/" + sid);
    S.source = "sheet"; S.sheetId = d.sheet.id; S.sheetName = d.sheet.name;
    S.sheetRows = d.sheet.n_rows; S.sheetHeaders = d.sheet.headers;
    showSheetSummary(d.sheet.name, d.sheet.n_rows, d.sheet.headers, d.sheet.rows.slice(0, 3), 0, "");
    setInfo();
  }
  function showSheetSummary(name, n, headers, previewRows, noEmail, emailCol) {
    S.sheetPreview = previewRows || [];
    const box = $("#sheet-summary");
    box.style.display = "";
    box.innerHTML = "<b>" + esc(name) + "</b> - <b>" + n + "</b> row(s) &middot; columns: " +
      headers.map((h) => "<code class='inline'>" + esc(h) + "</code>").join(" ") +
      (noEmail ? "<div class='hint' style='margin-top:6px;color:var(--warn)'>" + noEmail +
        " row(s) have no email in column '" + esc(emailCol) + "' - those emails cannot be sent.</div>" : "");
  }
  $("#btn-sheet-upload").addEventListener("click", async () => {
    const f = $("#sheet-file").files[0];
    if (!f) return toast("Choose a CSV or .xlsx file first", "err");
    const fd = new FormData();
    fd.append("file", f);
    fd.append("campaign_id", String(S.campaign || 0));
    $("#sheet-up-status").textContent = "Uploading...";
    try {
      const res = await fetch("/api/sheets/upload", { method: "POST", body: fd });
      const d = await res.json();
      if (!d.ok) { $("#sheet-up-status").textContent = ""; return toast(d.error || "Upload failed", "err"); }
      S.source = "sheet"; S.sheetId = d.id; S.sheetName = d.name;
      S.sheetRows = d.n_rows; S.sheetHeaders = d.headers;
      showSheetSummary(d.name, d.n_rows, d.headers, d.preview, d.no_email || 0, d.email_col || "");
      $("#sheet-up-status").textContent = "";
      toast("Sheet ready - " + d.n_rows + " rows. Next, pick what replaces each field.", "ok");
      setInfo();
    } catch (e) { $("#sheet-up-status").textContent = ""; toast(e.message, "err"); }
  });

  // ---------- step 4: field mapping ----------
  async function loadMapping() {
    const t = templates.templates.find((x) => x.id === S.tpl);
    S.fields = t.fields;
    S.cols = S.source === "sheet" && S.sheetHeaders.length ? S.sheetHeaders : ["serial", "company", "hr_name", "hr_email"];
    const sug = await api("/api/merge/suggest?template_id=" + S.tpl + "&columns=" + encodeURIComponent(S.cols.join(",")));
    S.suggestion = sug.suggestion || {};
    S.fields.forEach((f) => {
      const c = S.suggestion[f] || "";
      S.mapping[f] = c ? "column:" + c : (S.mapping[f] || "");
    });
    S.samples = {};
    if (S.sheetPreview.length) {
      S.sheetPreview.forEach((row) => S.cols.forEach((col) => {
        if (S.samples[col] == null && row[col]) S.samples[col] = row[col];
      }));
    }
    $("#mapping-rows").innerHTML = S.fields.length ? S.fields.map((f) =>
      "<tr><td style='font-weight:600'>" + esc(f) + "</td><td>" + mapSelect(f) + "</td></tr>").join("")
      : "<tr><td class='muted' colspan='2'>This template has no replaceable fields - every email will be identical.</td></tr>";
    bindMapRows();
    const first = S.sheetPreview && S.sheetPreview[0];
    $("#map-sample").innerHTML = first
      ? "First row of your data - " + S.cols.map((c) => "<code class='inline'>" + esc(c) + "</code> = &#34;" + esc(first[c] || "(empty)") + "&#34;").join(" &nbsp;&middot;&nbsp; ")
      : "Fields are filled from the data source - review the suggestions above.";
    const aiOn = STATE && STATE.ai_has_key === "1";
    $("#btn-ai-map").style.display = aiOn ? "" : "none";
  }
  function mapSelect(f) {
    const sugCol = S.suggestion[f] || "";
    const colOpts = S.cols.map((c) => "<option value='" + esc(c) + "'" + (sugCol === c ? " selected" : "") + ">" + esc(c) +
      (S.samples[c] ? "  (e.g. " + esc(String(S.samples[c]).slice(0, 24)) + ")" : "") + "</option>").join("");
    return "<select class='input map-val' data-f='" + esc(f) + "' style='max-width:130px'>" +
      "<option value='column'" + (sugCol ? " selected" : "") + ">Use column</option>" +
      "<option value='const'" + (!sugCol ? " selected" : "") + ">Constant text</option>" +
      "<option value='blank'>Blank</option></select> " +
      "<select class='input map-col' data-f='" + esc(f) + "' style='max-width:240px" + (sugCol ? "" : ";display:none") + "'>" + colOpts + "</select> " +
      "<input class='input map-const' data-f='" + esc(f) + "' style='max-width:200px;display:none' placeholder='constant value'>";
  }
  function bindMapRows() {
    $all(".map-val").forEach((sel) => sel.addEventListener("change", () => {
      const f = sel.dataset.f;
      const colSel = $(".map-col[data-f='" + f + "']");
      const cv = $(".map-const[data-f='" + f + "']");
      if (sel.value === "column") { S.mapping[f] = "column:" + colSel.value; colSel.style.display = ""; cv.style.display = "none"; }
      else if (sel.value === "const") { S.mapping[f] = cv ? cv.value : ""; colSel.style.display = "none"; cv.style.display = ""; }
      else { S.mapping[f] = ""; colSel.style.display = "none"; cv.style.display = "none"; }
    }));
    $all(".map-col").forEach((sel) => sel.addEventListener("change", () => {
      if ($(".map-val[data-f='" + sel.dataset.f + "']").value === "column") S.mapping[sel.dataset.f] = "column:" + sel.value;
    }));
    $all(".map-const").forEach((inp) => inp.addEventListener("input", () => { S.mapping[inp.dataset.f] = inp.value; }));
  }
  $("#btn-ai-map").addEventListener("click", async () => {
    try {
      const d = await api("/api/ai/mapping", { body: { fields: S.fields, columns: S.cols } });
      Object.entries(d.mapping).forEach(([f, col]) => { if (S.fields.includes(f) && col) S.mapping[f] = "column:" + col; });
      toast("AI suggestions applied - review them below", "ok");
      loadMapping();
    } catch (err) { toast(err.message, "err"); }
  });

  // ---------- step 5: preview & generate ----------
  let previewGen = 0;
  let previewBadge = null;
  async function loadPreview() {
    const sigEl = $("#sig-append");
    const sf = $("#range-from") ? $("#range-from").value.trim() : "";
    const st = $("#range-to") ? $("#range-to").value.trim() : "";
    const gen = ++previewGen;
    if (!previewBadge) {
      previewBadge = document.createElement("div");
      previewBadge.className = "hint";
      previewBadge.style.cssText = "margin:0 0 8px;height:16px;line-height:16px;visibility:hidden";
      previewBadge.textContent = "Updating previews...";
      $("#preview-box").insertBefore(previewBadge, $("#preview-box").firstChild);
    }
    previewBadge.style.visibility = "";
    const d = await api("/api/merge/preview", { body: { template_id: S.tpl, mapping: S.mapping, limit: 3,
      append_signature: sigEl ? sigEl.checked : false, serial_from: sf, serial_to: st, sheet_id: S.sheetId } });
    if (gen !== previewGen) return;
    previewBadge.style.visibility = "hidden";
    $("#merge-total").innerHTML = "Generating will add <b>" + d.total + "</b> emails to the queue (one per row) as <b>pending</b>. Nothing is sent yet.";
    const pbox = $("#preview-box");
    pbox.innerHTML = d.previews.map((p) =>
      "<div style='margin-bottom:18px'><div class='preview-note'>Serial " + esc(p.serial) + " &middot; " + esc(p.hr_name) + " &middot; <b>" + esc(p.company) + "</b> &middot; " + esc(p.hr_email) +
      "<br>Subject: " + esc(p.subject) + "</div><iframe class='email-frame' style='height:380px'></iframe></div>"
    ).join("");
    if (previewBadge.parentNode !== pbox) pbox.insertBefore(previewBadge, pbox.firstChild);
    $all("#preview-box iframe").forEach((fr, i) => {
      fr.srcdoc = "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body style='margin:0;background:#fff'>" + embedLocal(d.previews[i].body) + "</body></html>";
    });
  }
  S.attachFiles = [];
  function renderAttach() {
    $("#attach-list").innerHTML = S.attachFiles.map((f, i) =>
      "<span class='chip' style='display:inline-flex;align-items:center;gap:6px'>" + esc(f.name) +
      " <span class='muted'>" + Math.round(f.size / 1024) + " KB</span>" +
      "<button class='x' data-rm-attach='" + i + "' style='border:none;background:none;cursor:pointer;color:inherit'>&#10005;</button></span>"
    ).join("");
    const total = S.attachFiles.reduce((a, f) => a + f.size, 0);
    $("#attach-info").textContent = S.attachFiles.length
      ? S.attachFiles.length + " file(s), " + Math.round(total / 1024) + " KB total - stored once, attached at send time"
      : "No attachments yet. PDFs, spreadsheets, brochures - up to 10 files, 15 MB total. Each file is stored once, attached at send time.";
    $all("[data-rm-attach]").forEach((b) => b.addEventListener("click", () => { S.attachFiles.splice(+b.dataset.rmAttach, 1); renderAttach(); }));
  }
  $("#btn-attach-add").addEventListener("click", () => $("#attach-file").click());
  $("#attach-file").addEventListener("change", (e) => {
    for (const f of Array.from(e.target.files || [])) S.attachFiles.push(f);
    renderAttach();
    e.target.value = "";
  });
  $("#rem-on").addEventListener("change", () => { $("#rem-box").style.display = $("#rem-on").checked ? "" : "none"; });
  (async function fillRemTpls() {
    const t = await api("/api/templates");
    const opts = t.templates.map((x) => "<option value='" + x.id + "'>" + esc(x.name) + "</option>").join("");
    $("#rem-tpl").innerHTML = opts || "<option value=''>- no templates -</option>";
  })();
  $("#btn-rem-ai").addEventListener("click", () => {
    if (!(STATE && STATE.ai_has_key === "1")) return toast("Set an AI key in Settings first", "err");
    Modal.open("AI: write a reminder template",
      "<div class='field'><label>What is the reminder about?</label>" +
      "<textarea class='input' id='rem-ai-ctx' rows='3' placeholder='e.g. invitation for 2027 batch drives, January start'></textarea></div>" +
      "<div class='field'><label>Tone</label><select class='input' id='rem-ai-tone'><option>professional</option><option>warm</option><option>concise</option></select></div>",
      "<button class='btn ghost' data-close>Cancel</button><button class='btn ai' id='rem-ai-go'>&#10022; Generate</button>");
    $("#rem-ai-go").addEventListener("click", async () => {
      $("#rem-ai-go").disabled = true;
      try {
        const d = await api("/api/templates/ai-reminder", { body: { context: $("#rem-ai-ctx").value, tone: $("#rem-ai-tone").value } });
        Modal.close(); toast("Reminder template created: " + d.subject, "ok");
        const t = await api("/api/templates");
        $("#rem-tpl").innerHTML = t.templates.map((x) => "<option value='" + x.id + "'" + (x.id === d.id ? " selected" : "") + ">" + esc(x.name) + "</option>").join("");
      } catch (err) { toast(err.message, "err"); }
      $("#rem-ai-go").disabled = false;
    });
  });
  const actSel = $("#gen-action");
  function actToggle() {
    $("#sched-fields").style.display = actSel.value === "schedule" ? "" : "none";
    $("#send-warn").style.display = actSel.value === "send" ? "" : "none";
  }
  actSel.addEventListener("change", actToggle);
  actToggle();
  ["range-from", "range-to"].forEach((id) => $("#" + id).addEventListener("change", () => {
    if (panels[4].style.display !== "none") loadPreview();
  }));
  api("/api/signature").then((sg) => {
    const box = $("#sig-append-box");
    if (box) box.style.display = (sg.html || "").trim() ? "" : "none";
    const el = $("#sig-append");
    if (el) el.addEventListener("change", () => loadPreview());
  }).catch(() => {});
  $("#btn-generate").addEventListener("click", () => {
    const accountId = requireAccount($("#merge-account"));
    if (!accountId) return;
    const action = actSel.value;
    const sf = $("#range-from").value.trim(), st = $("#range-to").value.trim();
    if (action === "schedule" && !$("#sched-at").value.trim()) return toast("Enter the send time (YYYY-MM-DD HH:MM)", "err");
    const ccTxt = $("#cc-input").value.trim();
    let ccErr = "";
    ccTxt.split(",").map((x) => x.trim()).filter(Boolean).forEach((a) => {
      if (!/^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/.test(a)) ccErr = a;
    });
    if (ccErr) return toast("Invalid CC address: " + ccErr, "err");
    const name = $("#batch-name").value.trim();
    const totalN = S.source === "main" ? contacts.total : S.sheetRows;
    let what = "put in the queue as <b>pending</b> (nothing sent)";
    if (action === "draft") what = "created as <b>Gmail drafts</b> (nothing sent)";
    if (action === "schedule") what = "scheduled for <b>" + $("#sched-at").value.trim() + "</b>";
    if (action === "send") what = "sent <b>immediately</b> - cannot be undone!";
    typeToConfirm("Generate batch",
      "This renders emails for " + ((sf || st) ? "serials <b>" + (sf || "1") + " to " + (st || "last") + "</b>" : "all <b>" + totalN + " rows</b>") +
      " (preview above shows the first 3) and: " + what + "." +
      (ccTxt ? "<br><br>CC on every email: <b>" + esc(ccTxt) + "</b>" : "") +
      (S.attachFiles.length ? "<br><br>Includes <b>" + S.attachFiles.length + " attachment(s)</b> on every email." : "") +
      ($("#rem-on").checked ? "<br><br>Auto-reminder: <b>" + $("#rem-days").value + " day(s)</b> after send, max <b>" + $("#rem-max").value + "&times;</b> if no reply." : ""),
      action === "send" ? "SEND" : "CONFIRM", async () => {
        const d = await api("/api/merge/generate", { body: { template_id: S.tpl, mapping: S.mapping, name: name, account_id: accountId,
          append_signature: $("#sig-append") ? $("#sig-append").checked : false,
          serial_from: sf, serial_to: st, cc: ccTxt, action: action,
          at: $("#sched-at").value.trim(), stagger: +$("#sched-stagger").value || 0,
          sheet_id: S.sheetId, campaign_id: S.campaign } });
        let extra = "";
        if (action === "draft") extra = " - " + d.created_drafts + " drafts created" + (d.failed ? ", " + d.failed + " failed" : "");
        if (action === "schedule") extra = " - " + d.scheduled + " emails scheduled";
        if (action === "send") extra = " - " + d.sent + " sent" + (d.failed ? ", " + d.failed + " failed" : "");
        if (S.attachFiles.length) {
          const fd = new FormData();
          S.attachFiles.forEach((f) => fd.append("files", f));
          await fetch("/api/batches/" + d.batch_id + "/attachments", { method: "POST", body: fd });
          extra = ", attachments attached";
        }
        if ($("#rem-on").checked && $("#rem-tpl").value) {
          await api("/api/batches/" + d.batch_id + "/reminders", {
            body: { days: +$("#rem-days").value || 0, template_id: +$("#rem-tpl").value, max: +$("#rem-max").value || 1 } });
          extra += ", auto-reminder on";
        }
        const verb = action === "draft" ? "drafted" : action === "schedule" ? "scheduled" : action === "send" ? "sent" : "queued";
        toast("Batch created - " + d.created + " emails " + verb + extra, "ok");
        refreshState();
        location.href = "/queue";
      }, "Generate");
  });

  // ---------- navigation ----------
  $("#btn-to-2").addEventListener("click", () => {
    if (!S.campaign) return toast("Pick a campaign or create one first", "err");
    showStep(2);
  });
  $("#btn-back-2").addEventListener("click", () => showStep(1));
  $("#btn-to-3").addEventListener("click", () => {
    if (!S.tpl) return toast("Pick a template first", "err");
    showStep(3);
  });
  $("#btn-back-3").addEventListener("click", () => showStep(2));
  $("#btn-to-4").addEventListener("click", () => {
    if (!S.tpl) return toast("Pick a template first", "err");
    const sel = $("input[name=datasrc]:checked");
    if (sel && sel.value !== "main" && !S.sheetId)
      return toast(sel.value === "upload" ? "Upload the sheet first" : "Pick a sheet from the list", "err");
    showStep(4);
  });
  $("#btn-back-4").addEventListener("click", () => showStep(3));
  $("#btn-to-5").addEventListener("click", () => showStep(5));
  $("#btn-back-5").addEventListener("click", () => showStep(4));

  const active = templates.templates.find((t) => t.is_active) || templates.templates[0];
  S.tpl = active.id;
  const r = $("input[name=tpl][value='" + active.id + "']");
  if (r) r.checked = true;
  showStep(1);
};

Pages.queue = async function () {
  const state = { status: "", sel: new Set(), rows: [] };
  const load = async () => {
    const u = new URL("/api/emails", location.origin);
    if (state.status) u.searchParams.set("status", state.status);
    const data = await api(u.pathname + u.search);
    state.rows = data.rows;
    const counts = await api("/api/emails").then((d) => {
      const m = { "": d.rows.length };
      d.rows.forEach((r) => m[r.status] = (m[r.status] || 0) + 1);
      return m;
    });
    $all("#queue-tabs .btn").forEach((b) => {
      b.classList.toggle("primary", b.dataset.s === state.status);
      b.innerHTML = b.dataset.label + " (" + (counts[b.dataset.s] || 0) + ")";
    });
    $("#queue-rows").innerHTML = state.rows.length ? state.rows.map((r) =>
      "<tr><td><input type='checkbox' class='q-sel' data-id='" + r.id + "' " + (state.sel.has(r.id) ? "checked" : "") + "></td>" +
      "<td class='num'>" + esc(r.serial || "-") + "</td><td>" + esc(r.to_addr) + "</td>" +
      "<td style='max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>" + esc(r.subject) + "</td>" +
      "<td>" + pill(r.status) + "</td>" +
      "<td class='muted'>" + (r.is_reminder ? "<b>reminder of</b> " : "") + esc(fmtTime(r.scheduled_at)) + (r.sent_at ? " <br>sent " + esc(fmtTime(r.sent_at)) : "") +
      (r.replied_at ? " <br><span style='color:var(--success)'>replied " + esc(fmtTime(r.replied_at)) + "</span>" : "") +
      (r.reminder_count ? " <br>reminders sent: " + r.reminder_count : "") +
      (r.error ? "<br><span style='color:var(--danger)'>" + esc(r.error.slice(0, 60)) + "</span>" : "") + "</td>" +
      "<td style='white-space:nowrap'><button class='btn sm ghost' data-view='" + r.id + "'>View</button> " +
      ((r.status === "scheduled" || r.status === "pending") ? "<button class='btn sm ghost' data-stop='" + r.id + "'>Stop</button> " : "") +
      (r.status === "scheduled" ? "<button class='btn sm ghost' data-resch='" + r.id + "'>Reschedule</button> " : "") +
      ((r.status === "sent" || r.status === "replied") ? "<button class='btn sm ghost' data-reply='" + r.id + "'>Reply</button> " : "") +
      "<button class='btn sm ghost' data-del='" + r.id + "'>Remove</button></td></tr>"
    ).join("") : "<tr><td colspan='7' class='muted'>Queue is empty - generate a batch in Mail Merge first.</td></tr>";
    $all(".q-sel").forEach((cb) => cb.addEventListener("change", () => {
      const id = +cb.dataset.id;
      cb.checked ? state.sel.add(id) : state.sel.delete(id);
      selInfo();
    }));
    $all("[data-view]").forEach((b) => b.addEventListener("click", () => viewEmail(+b.dataset.view)));
    $all("[data-del]").forEach((b) => b.addEventListener("click", () => {
      typeToConfirm("Remove from queue", "Remove this email from the queue? It will not be sent or drafted.", "DELETE",
        () => api("/api/emails/delete", { body: { ids: [+b.dataset.del] } }).then(() => { toast("Removed", "ok"); load(); refreshState(); }), "Remove");
    }));
    $all("[data-stop]").forEach((b) => b.addEventListener("click", () => {
      typeToConfirm("Stop sending", "This email will be stopped - it will NOT be sent or drafted. You can draft or delete it later.", "STOP",
        () => api("/api/emails/cancel", { body: { ids: [+b.dataset.stop] } }).then(() => { toast("Stopped", "ok"); load(); refreshState(); }), "Stop");
    }));
    $all("[data-resch]").forEach((b) => b.addEventListener("click", () => openScheduleModal([+b.dataset.resch])));
    $all("[data-reply]").forEach((b) => b.addEventListener("click", () => {
      const r = state.rows.find((x) => x.id === +b.dataset.reply);
      if (r) replyModal(r);
    }));
    $("#select-all").onchange = (e) => {
      state.sel = e.target.checked ? new Set(state.rows.map((r) => r.id)) : new Set();
      load();
    };
    selInfo();
  };
  const selInfo = () => { $("#q-sel-count").textContent = state.sel.size; };
  const selectedOrFilter = () => state.sel.size ? Array.from(state.sel) : state.rows.filter((r) => r.status === "pending").map((r) => r.id);
  $all("#queue-tabs .btn").forEach((b) => b.addEventListener("click", () => { state.status = b.dataset.s; state.sel.clear(); load(); }));

  $("#btn-q-draft").addEventListener("click", () => {
    const accountId = requireAccount($("#q-account"));
    if (!accountId) return;
    const ids = selectedOrFilter();
    if (!ids.length) return toast("Nothing to draft (select rows or have pending emails)", "err");
    typeToConfirm("Create Gmail drafts", "Creates <b>" + ids.length + "</b> draft(s) in the selected Gmail account's Drafts folder. Nothing is sent.", "CONFIRM",
      async () => {
        const d = await api("/api/emails/draft", { body: { ids: ids, account_id: accountId } });
        toast(d.failed ? d.created + " drafted, " + d.failed + " failed" : d.created + " drafts created", d.failed ? "err" : "ok");
        load(); refreshState();
      }, "Create drafts");
  });
  function openScheduleModal(ids) {
    const accountId = requireAccount($("#q-account"));
    if (!accountId) return;
    if (!ids.length) return toast("Nothing to schedule", "err");
    const defStagger = STATE ? STATE.settings.default_stagger_min : "0";
    Modal.open("Schedule send",
      '<div class="field"><label>Send at (local time)</label><input class="input" id="sch-when" placeholder="YYYY-MM-DD HH:MM   e.g. 2026-11-12 10:30"></div>' +
      '<div class="field"><label>Stagger (minutes between emails; 0 = all at once)</label><input class="input" id="sch-stagger" type="number" min="0" value="' + defStagger + '" style="max-width:140px"></div>' +
      '<div id="sch-est" class="hint"></div>',
      '<button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="sch-go">Schedule</button>');
    const est = () => {
      const when = $("#sch-when").value.trim();
      const st = +$("#sch-stagger").value || 0;
      if (when) {
        $("#sch-est").textContent = ids.length + " email(s); last one goes out at " +
          new Date(new Date(when.replace(" ", "T")).getTime() + st * 60000 * Math.max(0, ids.length - 1)).toLocaleString();
      }
    };
    $("#sch-when").addEventListener("input", est);
    $("#sch-stagger").addEventListener("input", est);
    $("#sch-go").addEventListener("click", async () => {
      try {
        const d = await api("/api/emails/schedule", { body: { ids: ids, at: $("#sch-when").value.trim(), stagger: +$("#sch-stagger").value || 0, account_id: accountId } });
        Modal.close(); toast(d.scheduled + " emails scheduled - they send themselves at the chosen time", "ok");
        load(); refreshState();
      } catch (err) { toast(err.message, "err"); }
    });
  }
  $("#btn-q-schedule").addEventListener("click", () => openScheduleModal(selectedOrFilter()));
  $("#btn-q-stop").addEventListener("click", () => {
    let ids = state.sel.size ? Array.from(state.sel) : state.rows.map((r) => r.id);
    ids = ids.filter((id) => {
      const r = state.rows.find((x) => x.id === id);
      return r && (r.status === "scheduled" || r.status === "pending");
    });
    if (!ids.length) return toast("Nothing to stop - select scheduled or pending rows", "err");
    typeToConfirm("Stop sending", "Stops <b>" + ids.length + "</b> email(s) - they will NOT be sent or drafted. You can draft or delete them later.", "STOP",
      async () => {
        const d = await api("/api/emails/cancel", { body: { ids: ids } });
        toast(d.cancelled + " stopped", "ok");
        load(); refreshState();
      }, "Stop");
  });
  function replyModal(r) {
    Modal.open("Reply to " + r.to_addr,
      "<div class='hint' style='margin-top:0'>Subject: <b>Re: " + esc(r.subject) + "</b></div>" +
      "<div class='field'><label>Your reply</label><textarea class='input' id='rep-body' rows='7' placeholder='Write your reply...'></textarea></div>" +
      "<div style='display:flex;gap:10px;align-items:center;flex-wrap:wrap'>" +
      "<button class='btn ai sm' id='rep-ai'>&#10022; Draft with AI</button>" +
      "<span class='hint'>" + (window.__safeMode ? "Safe mode ON - it will be saved as a Gmail draft (nothing is sent)." : "Sends from the selected account.") + "</span></div>",
      "<button class='btn ghost' data-close>Cancel</button><button class='btn primary' id='rep-go'>" + (window.__safeMode ? "Save as draft" : "Send reply") + "</button>");
    $("#rep-ai").addEventListener("click", async () => {
      $("#rep-ai").disabled = true;
      try {
        const d = await api("/api/emails/" + r.id + "/reply-suggest", { body: {} });
        const ta = $("#rep-body");
        ta.value = d.draft;
        ta.focus();
      } catch (e) { toast(e.message, "err"); }
      $("#rep-ai").disabled = false;
    });
    $("#rep-go").addEventListener("click", async () => {
      const body = $("#rep-body").value.trim();
      if (!body) return toast("Write the reply first", "err");
      const accountId = requireAccount($("#q-account"));
      if (!accountId) return;
      try {
        const d = await api("/api/emails/" + r.id + "/reply", { body: { body: body, account_id: accountId } });
        Modal.close();
        toast(d.drafted ? "Reply saved as a Gmail draft" : "Reply sent", "ok");
        load(); refreshState();
      } catch (e) { toast(e.message, "err"); }
    });
  }
  $("#btn-q-send").addEventListener("click", () => {
    if (window.__safeMode) return toast("Safe mode is ON - turn it off in Settings first", "err");
    const accountId = requireAccount($("#q-account"));
    if (!accountId) return;
    const ids = selectedOrFilter();
    if (!ids.length) return toast("Nothing to send", "err");
    typeToConfirm("Send immediately",
      "This sends <b>" + ids.length + "</b> email(s) RIGHT NOW from the selected Gmail account. This cannot be undone.",
      "SEND", async () => {
        const d = await api("/api/emails/send-now", { body: { ids: ids, account_id: accountId } });
        toast(d.failed ? d.sent + " sent, " + d.failed + " failed" : d.sent + " emails sent", d.failed ? "err" : "ok");
        load(); refreshState();
      }, "Send now");
  });
  $("#btn-q-trigger").addEventListener("click", async () => {
    try { await api("/api/scheduler/trigger", { body: {} }); toast("Scheduler ticked", "ok"); refreshState(); }
    catch (err) { toast(err.message, "err"); }
  });
  function viewEmail(id) {
    Modal.open("Email preview",
      '<div id="em-meta" class="muted" style="margin-bottom:10px"></div><div id="em-actions" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px"></div><iframe class="email-frame" id="em-frame" style="height:340px"></iframe>',
      "<button class='btn ghost' data-close>Close</button>", true);
    api("/api/emails/view?id=" + id).then((d) => {
      const e = d.email;
      $("#em-meta").innerHTML = "Serial " + esc(e.serial) + " &middot; To: <b>" + esc(e.to_addr) + "</b> &middot; " + pill(e.status) +
        (e.scheduled_at ? " &middot; scheduled " + esc(fmtTime(e.scheduled_at)) : "") +
        (e.sent_at ? " &middot; sent " + esc(fmtTime(e.sent_at)) : "") +
        (e.replied_at ? " &middot; <b style='color:var(--success)'>replied " + esc(fmtTime(e.replied_at)) + "</b>" : "") +
        (e.reminder_count ? " &middot; reminders: " + e.reminder_count : "") +
        (e.is_reminder ? " &middot; <b>reminder email</b>" : "");
      let acts = "";
      if (e.status === "sent") {
        acts += e.replied_at
          ? "<button class='btn sm' id='em-unreply'>Mark as NOT replied</button>"
          : "<button class='btn sm primary' id='em-reply'>Mark as replied</button>";
      }
      if (STATE && STATE.ai_has_key === "1") acts += "<button class='btn sm ai' id='em-polish'>&#10022; AI: polish this email</button>";
      $("#em-actions").innerHTML = acts;
      if (acts) $("#em-actions").style.display = "";
      if ($("#em-reply")) $("#em-reply").addEventListener("click", () => markReplied(id, true));
      if ($("#em-unreply")) $("#em-unreply").addEventListener("click", () => markReplied(id, false));
      if ($("#em-polish")) $("#em-polish").addEventListener("click", () => polishEmail(id, e));
      $("#em-frame").srcdoc = "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body style='margin:0;background:#fff'>" + embedLocal(e.body) + "</body></html>";
    }).catch((err) => toast(err.message, "err"));
  }
  function markReplied(id, on) {
    api("/api/emails/" + id + "/mark-replied", { body: { replied: on } })
      .then(() => { toast(on ? "Marked as replied" : "Marked as not replied", "ok"); load(); refreshState(); })
      .catch((err) => toast(err.message, "err"));
  }
  function polishEmail(id, e) {
    Modal.open("AI polish",
      "<div class='field'><label>Any specific instructions (optional)</label>" +
      "<input class='input' id='pl-instr' placeholder='e.g. make it shorter, more formal, mention the January start'></div>" +
      "<div id='pl-result' class='muted'>Working...</div>",
      "<button class='btn ghost' data-close>Cancel</button><button class='btn primary' id='pl-apply' style='display:none'>Save polished version</button>");
    let polished = null;
    api("/api/emails/" + id + "/polish", { body: { instructions: $("#pl-instr").value } }).then((d) => {
      polished = d;
      $("#pl-result").innerHTML = "<div class='field'><label>New subject</label><input class='input' id='pl-subj' value='" + esc(d.subject) + "'></div>" +
        "<div class='field'><label>New body (HTML)</label><textarea class='input' id='pl-body' rows='8'>" + esc(d.body) + "</textarea></div>" +
        "<iframe class='email-frame' id='pl-frame' style='height:220px'></iframe>";
      $("#pl-frame").srcdoc = "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body style='margin:0;background:#fff'>" + embedLocal(d.body) + "</body></html>";
      $("#pl-apply").style.display = "";
      $("#pl-apply").addEventListener("click", () => {
        api("/api/emails/" + id, { body: { subject: $("#pl-subj").value, body_html: $("#pl-body").value } })
          .then(() => { toast("Polished email saved - queue updated", "ok"); Modal.close(); load(); })
          .catch((err) => toast(err.message, "err"));
      });
    }).catch((err) => toast(err.message, "err"));
  }
  const prevOnState = window.onState;
  window.onState = (s) => {
    window.__safeMode = s.settings.safe_mode === "1";
    const safe = window.__safeMode;
    $("#btn-q-send").disabled = safe;
    $("#btn-q-send").title = safe ? "Safe mode is ON" : "";
    const sch = s.scheduler || {};
    $("#sch-status").innerHTML =
      "Worker: <b>" + (sch.running ? "running" : "stopped") + "</b> &middot; due now: <b>" + (sch.due_now || 0) + "</b>" +
      " &middot; next fire: <b>" + (sch.next_fire ? esc(fmtTime(sch.next_fire)) : "none") + "</b>" +
      (sch.last_error ? " &middot; <span style='color:var(--danger)'>" + esc(sch.last_error) + "</span>" : "");
    if (prevOnState) prevOnState(s);
  };
  fillAccountSelect($("#q-account"));
  if (STATE) window.onState(STATE);
  load();
};

Pages.activity = async function () {
  const d = await api("/api/activity");
  $("#activity-rows").innerHTML = d.rows.map((r) =>
    "<tr><td class='muted' style='white-space:nowrap'>" + esc(r.ts) + "</td><td>" + pill(r.action) + "</td><td class='muted'>" + esc(r.actor || "") + "</td><td>" + esc(r.detail) + "</td></tr>"
  ).join("") || "<tr><td colspan='4' class='muted'>No activity yet.</td></tr>";
};

Pages.settings = async function () {
  const d = await api("/api/settings");
  const s = d.settings;
  const me = STATE.me || {};
  const isAdmin = me.role === "admin";
  const set = (id, val) => { const el = $("#" + id); if (el) el.value = val; };

  // send accounts
  const loadAccounts = async () => {
    const a = await api("/api/accounts");
    $("#acct-list").innerHTML = a.accounts.length ? a.accounts.map((x) =>
      '<div class="acct-row"><span class="dot ' + (x.connected ? "on" : "") + '" style="width:9px;height:9px;border-radius:50%;display:inline-block;background:' + (x.connected ? "var(--success)" : "var(--text-3)") + '"></span>' +
      '<div class="main"><b>' + esc(x.email || x.label || ("account #" + x.id)) + "</b> " +
      (x.mine ? "" : "<span class='muted'>(added by " + esc(x.owner_name) + ")</span>") +
      (x.connected ? "" : "<div class='hint' style='color:var(--danger)'>not connected yet</div>") + "</div>" +
      '<label class="checkbox-row" style="font-size:12px"><input type="checkbox" data-vis="' + x.id + '" ' + (x.visibility === "shared" ? "checked" : "") + ' ' + (!x.mine && isAdmin ? "" : "disabled") + '> shared with team</label>' +
      ((x.mine || isAdmin) ? '<button class="btn sm ghost" data-rm-acct="' + x.id + '">Remove</button>' : "") +
      "</div>"
    ).join("") : "<p class='muted'>No Gmail send-accounts yet. Connect one below.</p>";
    $all("[data-vis]").forEach((cb) => cb.addEventListener("change", () => {
      api("/api/accounts/" + cb.dataset.vis, { body: { visibility: cb.checked ? "shared" : "private" } })
        .then(() => toast(cb.checked ? "Now shared with the team" : "Now private to you", "ok"))
        .catch((e) => toast(e.message, "err"));
    }));
    $all("[data-rm-acct]").forEach((b) => b.addEventListener("click", () => {
      typeToConfirm("Remove send account", "This removes the stored login for this Gmail account. Drafts already in Gmail are not affected.", "REMOVE",
        async () => {
          await api("/api/accounts/" + b.dataset.rmAcct + "/delete", { body: {} });
          toast("Removed", "ok"); loadAccounts(); refreshState();
        }, "Remove");
    }));
  };
  $("#btn-acct-add").addEventListener("click", () => {
    Modal.open("Connect a Gmail account",
      '<p class="hint" style="margin-top:0">Step 1 of 2. Give the account a name, then we open Google in this tab - sign in with the account that should send, and allow access. It is saved here for the whole team.</p>' +
      '<div class="field"><label>Label (optional)</label><input class="input" id="acct-label" placeholder="e.g. Suparna or the team mailbox"></div>' +
      '<label class="checkbox-row" style="font-size:13px"><input type="checkbox" id="acct-private"> Keep private to me (hide from the team)</label>',
      '<button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="acct-go">Continue to Google</button>');
    $("#acct-go").addEventListener("click", async () => {
      try {
        const r = await api("/api/accounts/add", {
          body: { label: $("#acct-label").value.trim(), private: $("#acct-private").checked } });
        if (r.auth_url) location.href = r.auth_url;
      } catch (e) { toast(e.message, "err"); }
    });
  });
  loadAccounts();

  // sending rules
  const safeT = $("#set-safe");
  safeT.classList.toggle("on", s.safe_mode !== "1");
  safeT.addEventListener("click", async () => {
    safeT.classList.toggle("on");
    const on = safeT.classList.contains("on");
    try {
      await api("/api/settings", { body: { safe_mode: on ? "0" : "1" } });
      toast(on ? "Safe mode OFF - live sending armed" : "Safe mode ON - drafts only", on ? "err" : "ok");
      refreshState();
    } catch (e) { toast(e.message, "err"); }
  });
  if (s.safe_mode === "1") $("#safe-desc").textContent = "ON - only drafts can be created. Scheduled and immediate sending is blocked.";
  else $("#safe-desc").textContent = "OFF - scheduled and immediate sends are ARMED. Be careful.";
  set("set-cap", s.max_sends_per_day);
  set("set-stagger", s.default_stagger_min);
  $("#set-catchup").value = s.catchup_policy;
  $("#btn-save-settings").addEventListener("click", async () => {
    try {
      await api("/api/settings", { body: {
        max_sends_per_day: $("#set-cap").value, default_stagger_min: $("#set-stagger").value,
        catchup_policy: $("#set-catchup").value,
      }});
      toast("Sending rules saved", "ok"); refreshState();
    } catch (e) { toast(e.message, "err"); }
  });

  // AI
  const aiT = $("#set-ai");
  aiT.classList.toggle("on", s.ai_enabled === "1");
  aiT.addEventListener("click", async () => {
    aiT.classList.toggle("on");
    const on = aiT.classList.contains("on");
    try { await api("/api/settings", { body: { ai_enabled: on ? "1" : "0" } }); } catch (e) {}
  });
  const PROVIDERS = {
    openai: ["https://api.openai.com/v1", "gpt-4o-mini"],
    groq: ["https://api.groq.com/openai/v1", "qwen/qwen3.8-27b"],
    gemini: ["https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-1.5-flash"],
    custom: ["", ""],
  };
  const provSel = $("#set-ai-provider");
  const cur = s.ai_base_url || "https://api.openai.com/v1";
  provSel.value = Object.keys(PROVIDERS).find((k) => PROVIDERS[k][0] === cur) || "custom";
  provSel.addEventListener("change", () => {
    const p = PROVIDERS[provSel.value];
    if (provSel.value !== "custom") { $("#set-base-url").value = p[0]; $("#set-model").value = p[1]; }
  });
  set("set-base-url", s.ai_base_url);
  set("set-model", s.ai_model);
  $("#set-key").placeholder = s.ai_has_key === "1" ? "(key saved - leave blank to keep)" : "paste your API key here";
  $("#btn-ai-test").addEventListener("click", async () => {
    try {
      await api("/api/settings", { body: { ai_base_url: $("#set-base-url").value, ai_model: $("#set-model").value, ai_api_key: $("#set-key").value || undefined } });
      const r = await api("/api/ai/test", { body: {} });
      toast(r.ok ? "AI connection OK (" + (r.model || "") + ")" : "AI test failed: " + (r.error || ""), r.ok ? "ok" : "err");
    } catch (e) { toast(e.message, "err"); }
  });
  $("#btn-save-ai").addEventListener("click", async () => {
    try {
      const body = { ai_base_url: $("#set-base-url").value.trim(), ai_model: $("#set-model").value.trim() };
      if ($("#set-key").value.trim()) body.ai_api_key = $("#set-key").value.trim();
      await api("/api/settings", { body });
      toast("AI settings saved", "ok");
    } catch (e) { toast(e.message, "err"); }
  });

  // cloud relay
  set("set-relay-url", s.cloud_relay_url || "");
  if (s.cloud_relay_secret) $("#set-relay-secret").placeholder = "(secret saved - leave blank to keep)";
  const relayChip = $("#relay-state");
  function relayChipRender() {
    const on = (s.cloud_relay_url || "").trim();
    relayChip.textContent = on ? "relay ON" : "relay off";
    relayChip.className = "chip " + (on ? "on" : "");
  }
  relayChipRender();
  $("#btn-save-relay").addEventListener("click", async () => {
    try {
      const body = { cloud_relay_url: $("#set-relay-url").value.trim() };
      if ($("#set-relay-secret").value.trim()) body.cloud_relay_secret = $("#set-relay-secret").value.trim();
      await api("/api/settings", { body });
      if (body.cloud_relay_url) s.cloud_relay_url = body.cloud_relay_url;
      relayChipRender();
      toast("Cloud relay settings saved", "ok");
    } catch (e) { toast(e.message, "err"); }
  });

  // relay setup pack (admin)
  if (isAdmin) {
    const rs = $("#relay-acct-select");
    api("/api/accounts").then((a) => {
      const connected = a.accounts.filter((x) => x.connected);
      rs.innerHTML = '<option value="">choose the Gmail account…</option>' +
        connected.map((x) => '<option value="' + x.id + '">' + esc(x.email || x.label) + '</option>').join("");
      if (connected.length === 1) rs.value = String(connected[0].id);
    }).catch(() => {});
    $("#btn-relay-pack").addEventListener("click", async () => {
      if (!rs.value) return toast("Pick the Gmail account the relay should send from.", "err");
      try {
        const r = await api("/api/settings/relay-pack?account_id=" + rs.value);
        const rows = Object.entries(r.pack).map(([k, v]) =>
          '<div style="display:flex;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid var(--border)">' +
          '<code style="min-width:210px;font-size:12px">' + k + '</code>' +
          '<code style="flex:1;word-break:break-all;font-size:12px">' + esc(v) + '</code>' +
          '<button class="btn sm" data-copy="' + esc(v) + '">Copy</button></div>'
        ).join("");
        Modal.open("Cloud relay setup values",
          '<p class="hint" style="margin-top:0">Paste these 5 values into Cloudflare (one-time setup, see <code>cloud/README-CLOUD.md</code>). ' +
          'The relay secret is already saved on this platform - once you paste the relay URL and this secret below, the cloud scheduler goes live.</p>' +
          '<div style="max-height:50vh;overflow:auto">' + rows + '</div>' +
          '<div style="display:flex;gap:8px;margin-top:10px"><button class="btn" id="rp-copyall">Copy all (name=value)</button></div>');
        $all("#modal-box [data-copy]").forEach((b) => b.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(b.dataset.copy); toast("Copied", "ok"); } catch (e) { toast("Copy failed - select manually", "err"); }
        }));
        $("#rp-copyall").addEventListener("click", async () => {
          const txt = Object.entries(r.pack).map(([k, v]) => k + "=" + v).join("\n");
          try { await navigator.clipboard.writeText(txt); toast("All copied", "ok"); } catch (e) { toast("Copy failed - select manually", "err"); }
        });
      } catch (e) { toast(e.message, "err"); }
    });
  }

  // admin: google sign-in + client json
  if (isAdmin) {
    set("set-gid", s.google_client_id || "");
    set("set-gsec", s.google_client_secret || "");
    set("set-admin-email", s.admin_email || "");
    set("set-brand-name", s.brand_team_name || "");
    set("set-brand-foot", s.brand_tagline || "");
    const cjs = $("#client-json-state");
    if (cjs) {
      cjs.textContent = s.client_secret_set === "1" ? "Gmail client JSON is set" : "Gmail client JSON missing";
      cjs.className = "chip " + (s.client_secret_set === "1" ? "on" : "warn");
    }
    $("#btn-save-google").addEventListener("click", async () => {
      try {
        await api("/api/settings", { body: {
          google_client_id: $("#set-gid").value.trim(),
          google_client_secret: $("#set-gsec").value.trim(),
          admin_email: $("#set-admin-email").value.trim().toLowerCase(),
          brand_team_name: $("#set-brand-name").value.trim(),
          brand_tagline: $("#set-brand-foot").value.trim(),
        }});
        toast("Google sign-in saved", "ok"); refreshState();
      } catch (e) { toast(e.message, "err"); }
    });
    $("#btn-save-client").addEventListener("click", async () => {
      const txt = $("#set-client-json").value.trim();
      if (!txt) return toast("Paste the JSON first", "err");
      try { JSON.parse(txt); } catch (e) { return toast("That is not valid JSON - copy the whole downloaded file", "err"); }
      try {
        await api("/api/settings", { body: { client_secret_json: txt } });
        toast("Gmail client JSON saved", "ok"); loadAccounts();
      } catch (e) { toast(e.message, "err"); }
    });
  }

  // signature
  (async function () {
    try {
      const s = await api("/api/signature");
      $("#sig-html").value = s.html || "";
      const render = () => {
        $("#sig-preview").srcdoc = "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body style='margin:0;background:#fff;font-family:Arial;font-size:12px'>" +
          $("#sig-html").value + "</body></html>";
      };
      render();
      $("#sig-html").addEventListener("input", render);
    } catch (e) {}
  })();
  $("#btn-sig-upload").addEventListener("click", () => $("#sig-file").click());
  $("#sig-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    const res = await fetch("/api/signature/upload", { method: "POST", body: fd });
    const d = await res.json();
    if (d.ok) {
      $("#sig-html").value += ($("#sig-html").value ? "\n" : "") + d.html;
      $("#sig-img-note").textContent = "Image code added to the box below - press Save signature.";
      $("#sig-preview").srcdoc = "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body style='margin:0;background:#fff;font-family:Arial;font-size:12px'>" +
        $("#sig-html").value + "</body></html>";
    } else toast(d.error, "err");
    e.target.value = "";
  });
  $("#btn-sig-save").addEventListener("click", async () => {
    try {
      await api("/api/signature", { body: { html: $("#sig-html").value } });
      toast("Signature saved - new text/AI templates will use it", "ok");
    } catch (e) { toast(e.message, "err"); }
  });

  // danger
  $("#btn-wipe-queue").addEventListener("click", () => {
    typeToConfirm("Empty the queue", "Removes ALL generated emails from the queue (drafts already in Gmail are not affected).", "CONFIRM",
      async () => {
        const rows = await api("/api/emails").then((d2) => d2.rows);
        if (rows.length) await api("/api/emails/delete", { body: { ids: rows.map((r) => r.id) } });
        toast("Queue emptied", "ok"); refreshState();
      }, "Empty queue");
  });
};

Pages.reports = async function () {
  const load = async () => {
    const s = await api("/api/reports/summary");
    const cards = [
      ["Sent emails", s.sent, "outreach delivered", "green"],
      ["Replied", s.replied, "recruiters responded", "accent"],
      ["Reply rate", s.reply_rate + "%", "of sent emails", ""],
      ["Reminders sent", s.reminders, "automatic follow-ups", "amber"],
    ];
    $("#rep-cards").innerHTML = cards.map((x) =>
      "<div class='stat " + x[3] + "'><div class='label'>" + esc(x[0]) + "</div><div class='value'>" + x[1] + "</div><div class='hint'>" + esc(x[2]) + "</div></div>"
    ).join("");
    const max = Math.max(1, ...s.days.map((d) => d.n));
    $("#rep-days").innerHTML = "<div style='display:flex;align-items:flex-end;gap:6px;height:120px'>" + s.days.map((d) =>
      "<div style='flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;height:100%;justify-content:flex-end'>" +
      "<span style='font-size:11px;font-weight:600'>" + (d.n || "") + "</span>" +
      "<div style='width:100%;max-width:34px;background:var(--primary);border-radius:4px 4px 0 0;height:" + Math.round((d.n / max) * 80) + "%;min-height:" + (d.n ? 4 : 1) + "px;opacity:" + (d.n ? 1 : 0.25) + "'></div>" +
      "<span class='muted' style='font-size:10px'>" + esc(d.date) + "</span></div>"
    ).join("") + "</div>";
    const order = [["pending", "var(--text-3)"], ["drafted", "#8b5cf6"], ["scheduled", "#f59e0b"],
                   ["sent", "#10b981"], ["replied", "#3b82f6"], ["error", "#ef4444"]];
    const total = Object.values(s.emails).reduce((a, x) => a + x, 0);
    $("#rep-buckets").innerHTML = total ? order.map(([k, color]) => {
      const n = s.emails[k] || 0;
      return "<div style='margin-bottom:10px'><div style='display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:4px'>" +
        "<span><b style='text-transform:capitalize'>" + k + "</b></span><span class='muted'>" + n + " (" + Math.round(100 * n / total) + "%)</span></div>" +
        "<div style='height:10px;background:var(--bg);border-radius:5px;overflow:hidden'><div style='width:" + Math.round(100 * n / total) + "%;height:100%;background:" + color + ";border-radius:5px'></div></div></div>";
    }).join("") : "<span class='muted'>No emails yet - run a mail merge to start tracking.</span>";
    $("#rep-companies").innerHTML = s.companies.length ? s.companies.map((c) => {
      const rate = c.sent ? Math.round(100 * (c.replied || 0) / c.sent) + "%" : "-";
      return "<tr><td>" + esc(c.company) + "</td><td class='num'>" + c.total + "</td><td class='num'>" + (c.sent || 0) +
        "</td><td class='num'>" + (c.replied || 0) + "</td><td class='num'>" + rate + "</td></tr>";
    }).join("") : "<tr><td colspan='5' class='muted'>Nothing here yet.</td></tr>";
    loadRows();
  };
  const state = { q: "", status: "" };
  let deb;
  async function loadRows() {
    const u = new URL("/api/reports/emails", location.origin);
    if (state.q) u.searchParams.set("q", state.q);
    if (state.status) u.searchParams.set("status", state.status);
    const d = await api(u.pathname + u.search);
    $("#rep-rows").innerHTML = d.rows.length ? d.rows.map((r) =>
      "<tr><td class='num'>" + esc(r.serial || "-") + "</td><td>" + esc(r.company || "-") + "</td><td>" + esc(r.to_addr) +
      "</td><td style='max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>" + esc(r.subject) + "</td>" +
      "<td>" + pill(r.status) + (r.is_reminder ? " <span class='pill' style='background:#e0e7ff;color:#3730a3'>reminder</span>" : "") + "</td>" +
      "<td class='muted'>" + (r.sent_at ? esc(fmtTime(r.sent_at)) : "-") + "</td>" +
      "<td class='muted'>" + (r.replied_at ? "<span style='color:var(--success)'>" + esc(fmtTime(r.replied_at)) + "</span>" : "-") + "</td>" +
      "<td class='num'>" + (r.reminder_count || "") + "</td></tr>"
    ).join("") : "<tr><td colspan='8' class='muted'>No emails match.</td></tr>";
  }
  $("#rep-q").addEventListener("input", (e) => { clearTimeout(deb); deb = setTimeout(() => { state.q = e.target.value.trim(); loadRows(); }, 300); });
  $("#rep-status").addEventListener("change", (e) => { state.status = e.target.value; loadRows(); });
  load();
};

// ---------- AI assistant (chat) ----------
Pages.assistant = async function () {
  if (!(STATE && STATE.ai_has_key === "1")) {
    $("#chat-log").innerHTML = "<div class='empty'><div class='big'>&#10022;</div><p>AI is not set up yet. Add your API key in <a href='/settings'>Settings</a> (AI section), then come back.</p></div>";
    $("#chat-send").disabled = true;
    return;
  }
  const log = $("#chat-log");
  let msgs = [];
  try { msgs = JSON.parse(localStorage.getItem("mf_chat") || "[]"); } catch (e) { msgs = []; }
  function addBubble(role, text, events) {
    const d = document.createElement("div");
    d.style.cssText = "margin-bottom:12px;max-width:88%";
    if (role === "user") d.style.marginLeft = "auto";
    let h = "<div style='background:" + (role === "user" ? "var(--primary)" : "#fff") + ";color:" + (role === "user" ? "#fff" : "var(--text)") + ";padding:10px 14px;border-radius:12px;font-size:13.5px;white-space:pre-wrap'>" + esc(text) + "</div>";
    if (events && events.length) {
      h += "<div style='margin-top:6px;display:flex;gap:5px;flex-wrap:wrap'>" + events.map((ev) =>
        "<span class='chip' style='font-size:11px'>&#9881; " + esc(ev.tool) + " &rarr; " + esc(JSON.stringify(ev.result).slice(0, 180)) + "</span>").join("") + "</div>";
    }
    d.innerHTML = h;
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
  }
  if (!msgs.length) addBubble("assistant", "Hi! I'm your CampusMail AI assistant. Ask me about your campaigns, or tell me to do something - create a template, plan a batch, schedule or stop emails. I can't send anything without your manual step.");
  msgs.forEach((m) => addBubble(m.role, m.content, m.events));
  const SUGG = [
    "What's the status of my campaigns?",
    "Draft a polite Diwali follow-up template",
    "Schedule the first 5 rows of my newest sheet for tomorrow 10:30 as drafts",
    "How many emails did I send and how many replies came back?",
  ];
  $("#chat-sugg").innerHTML = SUGG.map((t) => "<button class='btn sm' data-sugg='" + esc(t) + "'>" + esc(t) + "</button>").join("");
  async function send(text) {
    text = (text != null ? text : $("#chat-input").value).trim();
    if (!text) return;
    $("#chat-input").value = "";
    addBubble("user", text);
    msgs.push({ role: "user", content: text });
    $("#chat-send").disabled = true;
    const thinking = document.createElement("div");
    thinking.className = "muted";
    thinking.textContent = "Thinking...";
    log.appendChild(thinking);
    log.scrollTop = log.scrollHeight;
    try {
      const d = await api("/api/ai/chat", { body: { messages: msgs.map((m) => ({ role: m.role, content: m.content })) } });
      thinking.remove();
      addBubble("assistant", d.reply || "(no reply)", d.events);
      msgs.push({ role: "assistant", content: d.reply, events: d.events });
    } catch (e) {
      thinking.remove();
      addBubble("assistant", "Sorry - " + e.message);
    }
    $("#chat-send").disabled = false;
    $("#chat-input").focus();
    try { localStorage.setItem("mf_chat", JSON.stringify(msgs.slice(-30))); } catch (err) {}
  }
  $("#chat-send").addEventListener("click", () => send());
  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $all("[data-sugg]").forEach((b) => b.addEventListener("click", () => send(b.dataset.sugg)));
};

// ---------- campaigns (CRM) ----------
Pages.campaigns = async function () {
  const view = $("#cam-view");
  const m = location.pathname.match(/\/campaigns\/(\d+)/);
  if (m) {
    try {
      const d = await api("/api/campaigns/" + m[1]);
      renderCamDetail(view, d);
    } catch (e) {
      view.innerHTML = "<div class='empty'><div class='big'>&#9635;</div><p>" + esc(e.message) + "</p></div>";
    }
    return;
  }
  const d = await api("/api/campaigns");
  const cards = d.campaigns.map((cm) => {
    const st = cm.status_counts || {};
    const chips = ["drafted", "scheduled", "sent", "replied", "error"].filter((k) => st[k])
      .map((k) => "<span class='pill " + k + "' style='margin-right:5px'>" + k + " " + st[k] + "</span>").join("");
    return "<div class='card' style='padding:18px;position:relative'>" +
      "<div style='font-weight:700;font-size:15px'>" + esc(cm.name) + "</div>" +
      (cm.description ? "<div class='muted' style='margin-top:4px'>" + esc(cm.description) + "</div>" : "") +
      "<div class='muted' style='margin-top:8px;font-size:12px'>" + cm.n_contacts + " contacts &middot; " + cm.n_batches +
      " batch(es) &middot; " + cm.n_sheets + " sheet(s)" + (cm.last ? " &middot; last activity " + esc(fmtTime(cm.last)) : "") + "</div>" +
      (chips ? "<div style='margin-top:10px'>" + chips + "</div>" : "") +
      "<div style='display:flex;gap:7px;margin-top:12px;flex-wrap:wrap'>" +
      "<a class='btn sm' href='/campaigns/" + cm.id + "'>Open</a>" +
      "<a class='btn sm primary' href='/merge?campaign=" + cm.id + "'>Start mail merge</a>" +
      "</div></div>";
  }).join("");
  view.innerHTML =
    "<div style='display:flex;align-items:center;gap:10px;margin-bottom:16px;flex-wrap:wrap'>" +
    "<div class='hint' style='flex:1;min-width:260px'>Campaigns are your CRM home - each one holds the sheets you merge, the batches you create, and their full performance.</div>" +
    "<button class='btn primary' id='btn-cam-new'>+ New campaign</button></div>" +
    (d.campaigns.length ? "<div class='grid cols-2' style='align-items:start'>" + cards + "</div>"
      : "<div class='empty'><div class='big'>&#9635;</div><p>No campaigns yet. Create one to organise your outreach.</p></div>");
  $("#btn-cam-new").addEventListener("click", () => {
    Modal.open("New campaign",
      "<p class='hint' style='margin-top:0'>Give it a name you will recognise - e.g. Summer 2027 recruiter outreach, Winter internships.</p>" +
      "<div class='field'><label>Campaign name</label><input class='input' id='nc-name' placeholder='e.g. Summer 2027 recruiter outreach'></div>" +
      "<div class='field'><label>Note (optional)</label><input class='input' id='nc-note' placeholder='e.g. 2027 batch, all departments'></div>",
      "<button class='btn ghost' data-close>Cancel</button><button class='btn primary' id='nc-go'>Create</button>");
    $("#nc-go").addEventListener("click", async () => {
      const name = $("#nc-name").value.trim();
      if (!name) return toast("Give the campaign a name", "err");
      try {
        const d2 = await api("/api/campaigns", { body: { name: name, description: $("#nc-note").value.trim() } });
        Modal.close();
        toast("Campaign created", "ok");
        location.href = "/campaigns/" + d2.id;
      } catch (e) { toast(e.message, "err"); }
    });
  });
};

function renderCamDetail(view, d) {
  const c = d.campaign;
  const st = d.status_counts || {};
  const total = Object.keys(st).reduce((a, k) => a + st[k], 0);
  const stats = [
    ["Contacts", d.n_contacts, "in this campaign's sheets", ""],
    ["Emails", total, "across all batches", "accent"],
    ["Drafted", st.drafted || 0, "in Gmail drafts", ""],
    ["Scheduled", st.scheduled || 0, "waiting to send", "amber"],
    ["Sent", st.sent || 0, "delivered", "green"],
    ["Replied", st.replied || 0, "recruiters responded", "accent"],
    ["Errors", st.error || 0, "need attention", (st.error || 0) > 0 ? "red" : ""],
  ];
  const batchRows = d.batches.length ? d.batches.map((b) => {
    const cc = b.counts || {};
    const chips = ["drafted", "scheduled", "sent", "replied", "error", "pending"].filter((k) => cc[k])
      .map((k) => "<span class='pill " + k + "' style='margin:2px 3px 2px 0;font-size:10.5px'>" + k + " " + cc[k] + "</span>").join("");
    return "<tr><td><b>" + esc(b.name) + "</b></td><td class='muted'>" + esc(fmtTime(b.created_at)) + "</td>" +
      "<td>" + (chips || "<span class='muted'>empty</span>") + "</td>" +
      "<td><a class='btn sm' href='/queue'>Queue</a></td></tr>";
  }).join("") : "<tr><td colspan='4' class='muted'>No batches yet - start a mail merge in this campaign.</td></tr>";
  const sheetRows = d.sheets.length ? d.sheets.map((sh) =>
    "<tr><td><b>" + esc(sh.name) + "</b></td><td class='num'>" + sh.n_rows + " rows</td>" +
    "<td style='max-width:360px'>" + sh.headers.map((h) => "<code class='inline'>" + esc(h) + "</code>").join(" ") + "</td>" +
    "<td class='muted'>" + esc(fmtTime(sh.created_at)) + "</td>" +
    "<td><button class='btn sm ghost' data-del-sheet='" + sh.id + "' data-name='" + esc(sh.name) + "'>Delete</button></td></tr>"
  ).join("") : "<tr><td colspan='5' class='muted'>No sheets yet - upload one during a mail merge in this campaign.</td></tr>";
  view.innerHTML =
    "<a href='/campaigns' class='hint' style='display:inline-block;margin-bottom:12px'>&#8592; All campaigns</a>" +
    "<div style='display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap;margin-bottom:18px'>" +
    "<div style='flex:1;min-width:240px'><div style='font-size:20px;font-weight:800'>" + esc(c.name) + "</div>" +
    (c.description ? "<div class='muted' style='margin-top:4px'>" + esc(c.description) + "</div>" : "") +
    "<div class='muted' style='margin-top:6px;font-size:12px'>Created " + esc(fmtTime(c.created_at)) + " by " + esc(c.created_by || "-") + "</div></div>" +
    "<button class='btn primary' id='btn-cam-merge'>+ Start mail merge in this campaign</button></div>" +
    "<div class='grid cols-4' style='margin-bottom:18px'>" +
    stats.map((x) => "<div class='stat " + x[3] + "'><div class='label'>" + esc(x[0]) + "</div><div class='value'>" + x[1] + "</div><div class='hint'>" + esc(x[2]) + "</div></div>").join("") +
    "</div>" +
    "<div class='section-title'>Batches in this campaign</div>" +
    "<div class='table-wrap'><table class='tbl'><tr><th>Batch</th><th>Created</th><th>Status</th><th></th></tr>" + batchRows + "</table></div>" +
    "<div class='divider'></div>" +
    "<div class='section-title'>Sheets in this campaign</div>" +
    "<div class='table-wrap'><table class='tbl'><tr><th>Sheet</th><th>Rows</th><th>Columns</th><th>Uploaded</th><th></th></tr>" + sheetRows + "</table></div>";
  $("#btn-cam-merge").addEventListener("click", () => { location.href = "/merge?campaign=" + c.id; });
  $all("[data-del-sheet]").forEach((b) => b.addEventListener("click", () => typeToConfirm("Delete sheet",
    "Delete sheet '" + b.dataset.name + "'? Its contacts stay in your list; only the sheet record is removed.", "DELETE",
    () => api("/api/sheets/" + b.dataset.delSheet + "/delete", { body: {} }).then(() => { toast("Sheet deleted", "ok"); Pages.campaigns(); }), "Delete")));
}

refreshState().then(() => {
  const init = Pages[document.body.dataset.page];
  if (init) init().catch((e) => toast(e.message, "err"));
});
