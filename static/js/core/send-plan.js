/* Pure send-planning helpers (D-10 / 8.1): one source of truth for the
   numbers shown in outcome cards, footers, confirm modals and success
   screens. No DOM, no fetch - unit-testable in node. */
import { fmtInt } from "./format.js";

export function remainingToday(cap, sentToday) {
  if (!cap || cap <= 0) return Infinity;
  return Math.max(0, cap - (sentToday || 0));
}

/* plan.n = audience size, plan.today = what actually leaves today,
   plan.later = what is kept for later (send now), plan.overCap = schedule
   exceeds the daily limit. */
export function planDelivery({ outcome, n, cap = 0, sentToday = 0 } = {}) {
  const nN = Number(n) || 0;
  const capN = Number(cap) || 0;
  const p = { outcome, n: nN, cap: capN, sentToday: Number(sentToday) || 0,
    today: nN, later: 0, capped: false, overCap: false };
  if (outcome === "send") {
    p.today = Math.min(nN, remainingToday(capN, p.sentToday));
    p.later = nN - p.today;
    p.capped = p.later > 0;
  } else if (outcome === "schedule") {
    // The scheduler honours the cap at fire time: anything that does not fit
    // on the scheduled day carries over to the next available day.
    p.overCap = capN > 0 && nN > capN;
    p.today = capN > 0 ? Math.min(nN, capN) : nN;
    p.later = nN - p.today;
  }
  return p;
}

export function sendCapSentence(plan) {
  if (!plan.capped) return "";
  return fmtInt(plan.today) + " can go out today (daily limit " + fmtInt(plan.cap) +
    "). The other " + fmtInt(plan.later) + " stay in your queue - schedule them or send them tomorrow.";
}

export function scheduleCapSentence(plan) {
  if (!plan.overCap) return "";
  return "Over your daily limit of " + fmtInt(plan.cap) + ": the first " + fmtInt(plan.today) +
    " go out on the scheduled day, the other " + fmtInt(plan.later) + " the next day.";
}
