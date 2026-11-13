/* CampusMail AI copy: glossary, one status vocabulary, message builders.
   Every user-visible status label and status wording comes from here. */

export const STATUS = {
  // email status (effective status, see statusOf)
  ready:      { label: "Ready",            short: "Ready",      cls: "ready",      icon: "circle-dashed" },
  drafted:    { label: "In Gmail drafts",  short: "In drafts",  cls: "drafted",    icon: "file-pen" },
  scheduled:  { label: "Scheduled",        short: "Scheduled",  cls: "scheduled",  icon: "clock" },
  sent:       { label: "Sent",             short: "Sent",       cls: "sent",       icon: "send" },
  replied:    { label: "Replied",          short: "Replied",    cls: "replied",    icon: "reply" },
  bounced:    { label: "Bounced",          short: "Bounced",    cls: "bounced",    icon: "mail-x" },
  failed:     { label: "Failed",           short: "Failed",     cls: "failed",     icon: "alert-triangle" },
  stopped:    { label: "Stopped",          short: "Stopped",    cls: "stopped",    icon: "pause-circle" },
};

// contact status (same labels where shared)
export const CONTACT_STATUS = {
  new:         { label: "Not contacted",   cls: "neutral",   icon: "circle-dashed" },
  pending:     STATUS.ready,
  drafted:     STATUS.drafted,
  scheduled:   STATUS.scheduled,
  sent:        STATUS.sent,
  replied:     STATUS.replied,
  bounced:     STATUS.bounced,
  failed:      STATUS.failed,
};

export const CAMPAIGN_STATUS = {
  draft:    { label: "Draft",    cls: "draft",    icon: "circle-dashed" },
  active:   { label: "Active",   cls: "active",   icon: "zap" },
  finished: { label: "Finished", cls: "finished", icon: "check-circle-2" },
};

// Precedence for a sent email with several flags: Bounced, then Replied, then Sent.
export function statusOf(email) {
  if (email.status === "cancelled") return "stopped";
  if (email.status === "error") return "failed";
  if (email.status === "drafted") return "drafted";
  if (email.status === "scheduled") return "scheduled";
  if (email.status === "pending") return "ready";
  if (email.status === "sent") {
    if (email.bounced_at) return "bounced";
    if (email.replied_at) return "replied";
    return "sent";
  }
  return "ready";
}

export function contactStatusOf(contact) {
  const s = contact.status || "new";
  return (CONTACT_STATUS[s] || CONTACT_STATUS.new).label;
}

// Message builders (the catalogue from section 11.4)
export const MSG = {
  draftsCreated: (n, account) => `${n} ${plural(n, "draft")} saved in ${account}.`,
  scheduled: (n, when) => `${n} ${plural(n, "email")} scheduled for ${when}.`,
  sent: (n) => `${n} ${plural(n, "email")} sent.`,
  partial: (done, total, failed) => `${done} of ${total} done. ${failed} ${plural(failed, "mail")} ${failed === 1 ? "failed" : "failed"}.`,
  stopped: (n) => `Stopped ${n} ${plural(n, "email")}.`,
  markedReplied: () => "Marked as replied.",
  liveOn: () => "Live sending is on. Scheduled emails will go out.",
  liveOff: () => "Drafts only. Nothing will be sent.",
  importDone: (added, updated) => `${added} ${plural(added, "contact")} added, ${updated} ${plural(updated, "contact")} updated.`,
  gmailReconnect: (email) => `${email} needs to be reconnected.`,
  limitExceeded: (limit, n) => `Your daily limit is ${limit} ${plural(limit, "email")}. This send is ${n}.`,
  engineStopped: () => "Scheduled emails are overdue because sending is paused.",
  aiOff: () => "AI is off. Add an API key to turn it on.",
  saveFailed: () => "Could not save. Check your connection and try again.",
  sessionExpired: () => "You were signed out. Sign in again to continue.",
};

export function plural(n, word) {
  const nn = Number(n) || 0;
  return nn === 1 ? word : word + "s";
}

// Bounce reason codes -> plain words
export function bounceReason(code) {
  const c = (code || "").toLowerCase();
  if (/unknown|not.exist|invalid|no.email|550|551/.test(c)) return "This address does not exist.";
  if (/full|quota|552/.test(c)) return "Their mailbox is full.";
  if (/block|reject|policy|spam|553/.test(c)) return "Their server refused the message.";
  if (/domain|dns|host|554/.test(c)) return "The company domain could not be found.";
  if (c) return "The address did not work.";
  return "The address did not work.";
}
