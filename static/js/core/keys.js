/* Keyboard shortcuts.
   - onKey("k", fn)            single key
   - onSeq("g c", fn)          press g, then c within 900 ms
   - Ctrl/Cmd+K is reserved for the command palette (fires cm:palette)
   Shortcuts never fire while typing in a field. */

const _single = new Map();
const _seqStart = new Map();  // first key -> [ [restKey, fn], ... ]
let _seqState = { key: null, at: 0 };

export function onKey(key, fn) { _single.set(key.toLowerCase(), fn); }

export function onSeq(seq, fn) {
  const parts = seq.toLowerCase().split(/\s+/);
  if (!parts[0] || parts.length < 2) throw new Error("bad sequence");
  if (!_seqStart.has(parts[0])) _seqStart.set(parts[0], []);
  _seqStart.get(parts[0]).push([parts.slice(1).join(" "), fn]);
}

function isTyping(t) {
  return !!(t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable));
}

export function initKeys() {
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("cm:palette"));
      }
      return;
    }
    if (e.altKey || isTyping(e.target)) return;
    const k = e.key.toLowerCase();

    const seqs = _seqStart.get(k);
    if (seqs) {
      if (_seqState.key === k && Date.now() - _seqState.at < 900) {
        _seqState = { key: null, at: 0 };
        for (const [rest, fn] of seqs) {
          if (rest.split(" ")[0] === k) { e.preventDefault(); fn(e); return; }
        }
        return;
      }
      e.preventDefault();
      _seqState = { key: k, at: Date.now() };
      return;
    }
    const fn = _single.get(k);
    if (fn) { e.preventDefault(); fn(e); }
  });
}

export function shortcutList(extra = []) {
  const base = [
    ["Ctrl/⌘ K", "Search or jump to"],
    ["/", "Search or jump to"],
    ["?", "Keyboard shortcuts"],
  ];
  return base.concat(extra);
}
