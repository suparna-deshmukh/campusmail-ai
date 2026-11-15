/* Shared undo-window countdown for real sends (8.3 / D-15). Used by the
   send flow and the Emails tab so both show the same pattern. Renders into
   a modal body: "Sending N emails in Ns" + "You can still change your
   mind." + Cancel. Resolves { cancelled: false } when the window elapses,
   { cancelled: true } when the user cancels.
   releaseAt: server time "YYYY-MM-DD HH:MM:SS" (server-local wall clock,
   the same contract as every other time in the app) - the countdown syncs
   to it so a slow round-trip never overstates the window. */
import { el } from "../core/ui.js";
import { fmtInt } from "../core/format.js";

function parseWall(s) {
  const m = String(s || "").match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)).getTime();
}

export function startSendCountdown(m, { n, releaseAt = null, seconds = 10 } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const end = (releaseAt ? parseWall(releaseAt) : null) || (Date.now() + seconds * 1000);
    const count = el("div", { style: "font-size:26px;font-weight:650;text-align:center;padding:18px 0 6px" });
    const sub = el("div", { style: "text-align:center;font-size:13px;color:var(--text-2)" },
      "You can still change your mind.");
    const cancel = el("button", { class: "btn btn-secondary", style: "display:block;margin:14px auto 0" }, "Cancel");
    const paint = () => {
      const left = Math.max(0, Math.ceil((end - Date.now()) / 1000));
      count.textContent = "Sending " + fmtInt(n) + (n === 1 ? " email" : " emails") + " in " + left;
      return left;
    };
    m.body.innerHTML = "";
    m.body.append(count, sub, cancel);
    paint();
    const timer = setInterval(() => {
      if (paint() <= 0) {
        clearInterval(timer);
        if (!done) { done = true; resolve({ cancelled: false }); }
      }
    }, 250);
    cancel.addEventListener("click", () => {
      if (done) return;
      done = true;
      clearInterval(timer);
      resolve({ cancelled: true });
    });
  });
}
