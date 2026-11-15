/* Local, privacy-preserving event log (8.12). Off by default.
   Flip TELEMETRY_ON to log structured events to the console so the
   Section 2 metrics can be measured. Only whitelisted numeric/string
   props are recorded - never email addresses or contact data. */
export const TELEMETRY_ON = false;
const SAFE_KEYS = new Set(["section", "outcome", "skip", "n", "ms", "code"]);
let seq = 0;
export function logEvent(name, props = {}) {
  if (!TELEMETRY_ON) return;
  const rec = { name, seq: ++seq, t: Date.now() };
  for (const [k, v] of Object.entries(props))
    if (SAFE_KEYS.has(k) && (typeof v === "number" || typeof v === "string")) rec[k] = v;
  try { console.debug("[campusmail:event]", rec); } catch { /* noop */ }
}
