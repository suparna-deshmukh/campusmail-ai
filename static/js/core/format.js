/* Dates, numbers, relative time. Times always named with the zone in pickers. */

const IN = { hour12: false };

export function fmtInt(n) {
  return Math.round(Number(n) || 0).toLocaleString("en-IN");
}

export function fmtPct(n) {
  const x = Number(n) || 0;
  return (x >= 10 || x <= -10 ? Math.round(x) : x.toFixed(1).replace(/\.0$/, "")) + "%";
}

const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

function toLocalDate(s) {
  if (!s) return null;
  // backend stores "YYYY-MM-DD HH:MM" local time or ISO
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(s)) {
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  }
  const d = new Date(s);
  return isNaN(d) ? null : d;
}

export function parseWhen(s) { return toLocalDate(s); }

// "Tue 18 Nov"
export function fmtDay(s) {
  const d = toLocalDate(s);
  if (!d) return "—";
  const day = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()];
  return `${day} ${d.getDate()} ${MONTHS[d.getMonth()].slice(0, 3)}`;
}

// "10:30"
export function fmtTime(s) {
  const d = toLocalDate(s);
  if (!d) return "—";
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

// "Tue 10:30"
export function fmtDayTime(s) {
  const d = toLocalDate(s);
  if (!d) return "—";
  return `${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()]} ${fmtTime(s)}`;
}

// "Tuesday 18 November at 10:00"
export function fmtLong(s) {
  const d = toLocalDate(s);
  if (!d) return "—";
  return `${DAY_NAMES[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]} at ${fmtTime(s)}`;
}

// relative: "just now", "4 min ago", "2 days ago", "Yesterday", "Today"
export function relTime(s) {
  const d = toLocalDate(s);
  if (!d) return "—";
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 45) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) {
    const h = Math.round(diff / 3600);
    if (now.getDate() === d.getDate()) return `${h} h ago`;
    return "Yesterday";
  }
  const days = Math.round(diff / 86400);
  if (days < 8) {
    const dd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()];
    return `${dd} ${d.getDate()} ${MONTHS[d.getMonth()].slice(0, 3)}`;
  }
  return `${d.getDate()} ${MONTHS[d.getMonth()].slice(0, 3)} ${d.getFullYear()}`;
}

// "Tomorrow 10:30", "Today 10:30", "Fri 10:30" — for upcoming lists
export function upcomingWhen(s) {
  const d = toLocalDate(s);
  if (!d) return "—";
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const that = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const delta = Math.round((that - today) / 86400000);
  if (delta === 0) return "Today " + fmtTime(s);
  if (delta === 1) return "Tomorrow " + fmtTime(s);
  return fmtDayTime(s);
}

export function tzLabel() {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
    const abbr = new Date().toLocaleTimeString("en-US", { timeZoneName: "short" })
      .split(" ").pop();
    return tz.replace(/_/g, " ") + (abbr && abbr !== "GMT" ? ` (${abbr})` : "");
  } catch { return "local time"; }
}

// "YYYY-MM-DD HH:MM" for the backend (local time)
export function toBackend(d) {
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// greeting by hour: Good morning / afternoon / evening
export function greeting(date = new Date()) {
  const h = date.getHours();
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

export function initials(name) {
  const parts = (name || "?").trim().split(/\s+/);
  return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || "?";
}

const AVATAR_HUES = [239, 262, 205, 172, 140, 25, 340, 288];
export function avatarColor(seed) {
  let h = 0;
  for (const ch of String(seed || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const hue = AVATAR_HUES[h % AVATAR_HUES.length];
  return `hsl(${hue} 45% 52%)`;
}
