/* Native select chevrons are unreliable in WebKit (inconsistent between
   browsers, and a select's own background repeats across the open dropdown
   rows), so every app select (select.input and select.select) is wrapped in
   .select-wrap and a single caret is rendered by the app instead. */
const CARET = '<svg class="select-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

function adopt(sel) {
  const parent = sel.parentElement;
  if (!parent || parent.classList.contains("select-wrap")) return;
  const wrap = document.createElement("span");
  wrap.className = "select-wrap";
  for (const prop of ["width", "maxWidth", "minWidth"]) {
    const v = sel.style[prop];
    if (v) {
      wrap.style[prop] = v;
      sel.style.removeProperty(prop === "width" ? "width" : prop === "maxWidth" ? "max-width" : "min-width");
    }
  }
  parent.insertBefore(wrap, sel);
  wrap.append(sel);
  wrap.insertAdjacentHTML("beforeend", CARET);
}

export function initSelectCarets() {
  let queued = false;
  const sweep = (root) => root.querySelectorAll("select.input, select.select").forEach(adopt);
  document.addEventListener("DOMContentLoaded", () => {
    sweep(document.body);
    new MutationObserver((muts) => {
      if (queued) return;
      queued = true;
      queueMicrotask(() => {
        queued = false;
        for (const m of muts) {
          for (const n of m.addedNodes) {
            if (n.nodeType !== 1) continue;
            if (n.matches && n.matches("select.select")) adopt(n);
            else if (n.querySelectorAll) sweep(n);
          }
        }
      });
    }).observe(document.documentElement, { childList: true, subtree: true });
  });
}
