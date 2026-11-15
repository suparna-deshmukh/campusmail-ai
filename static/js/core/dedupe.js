/* Pure duplicate helpers for send audiences (8.2 / D-11 / D-38).
   Case-insensitive, trimmed email comparison. No DOM, no fetch -
   unit-testable in node. */

const norm = (e) => String(e || "").trim().toLowerCase();

/* Group rows by normalized email.
   Returns { dupes, crossCompany, groups }, where each group is
   { email, count, companies, firstId, ids } in first-seen order. */
export function dedupeRows(rows) {
  const seen = new Map();
  for (const r of rows || []) {
    const e = norm(r.hr_email);
    if (!e) continue;
    if (!seen.has(e)) seen.set(e, { email: e, count: 0, companies: new Set(), firstId: r.id, ids: [] });
    const g = seen.get(e);
    g.count += 1;
    g.ids.push(r.id);
    if (r.company) g.companies.add(String(r.company).trim());
  }
  const groups = [...seen.values()]
    .filter((g) => g.count > 1)
    .map((g) => ({ email: g.email, count: g.count, companies: [...g.companies], firstId: g.firstId, ids: g.ids }));
  const dupes = groups.reduce((n, g) => n + (g.count - 1), 0);
  const crossCompany = groups.filter((g) => g.companies.length > 1).length;
  return { dupes, crossCompany, groups };
}

/* Ids to keep when skipping duplicates: first occurrence per email wins
   (list order); rows without an email are kept untouched. */
export function keepFirstIds(rows) {
  const seen = new Set();
  const keep = [];
  for (const r of rows || []) {
    const e = norm(r.hr_email);
    if (e) {
      if (seen.has(e)) continue;
      seen.add(e);
    }
    keep.push(r.id);
  }
  return keep;
}
