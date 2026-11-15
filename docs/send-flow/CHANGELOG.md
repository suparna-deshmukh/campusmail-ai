# Send Flow — Change Log

Companion to `DIAGNOSIS.md` (defect IDs) and `FINAL_REPORT.md`.
All work on branch `redesign`. Commits name the defect IDs.

## Phase 1 — Stop the bleeding (P0/P1)

| ID | What changed | Files |
|---|---|---|
| D-01 | Literal `null`/`undefined` no longer render: `appendSafe()` helper, nullable values guarded at every call site | `ui.js`, `send.js`, `tests/test_send_dom.py` (DOM scan) |
| D-02..D-06, D-09, D-25 | One shared CSS root cause: send-flow component styles were never loaded on the new shell. Restored stepper pills, chip styles, note rows, badges, drop zone, keycap hint, and the time-picker segmented control | `send.css`, `layout.css`, `timepicker.js`, `shell.js` |
| D-08 | Sidebar user row shows avatar initials + name (no bare ellipsis) | `shell.js` |
| D-10 | Cap honesty: pure `planDelivery()` (shared by cards, confirm, success), `/api/state` exposes `sent_today`/`remaining_today` (B1), all send labels state what actually happens | `send-plan.js`, `app.py`, `send.js`, `send.css` |
| D-12 | Scheduling without a time can no longer reach Review: Continue disabled with the reason in visible text | `send.js` |
| D-13 | All counts use Indian grouping (`Intl.NumberFormat('en-IN')`), including button labels | `format.js`, `timepicker.js` |
| D-14/D-29 | Hold-to-confirm renders a single label that becomes "Keep holding… N%" | `ui.js` (+ unit test) |

## Phase 2 — Send safety

| ID | What changed | Files |
|---|---|---|
| D-11/D-38 | Duplicates skipped by default (keep-first, case-insensitive), visible toggle on Audience + Review, "View duplicates" table, per-campaign persistence; send-now without skipping requires acknowledgement | `dedupe.js`, `send.js`, `tests/test_js_core.py`, `tests/test_send_dom.py` |
| D-15/D-16/D-37 | Shared 10 s undo window (`SendCountdown`) used by both the send flow and the Emails tab; truthful confirm modal (real numbers, from/to/template, only relevant warnings); stoppable during send; no double-retry | `send-countdown.js`, `emails.js`, `send.js`, `tests/test_send_dom.py` |
| D-32 | Safety strengthening: client-controlled `internal_hold*` keys stripped server-side; server-sanctioned hold flag; new batch undo route (B2) | `app.py`, `tests/test_undo.py` |
| D-41 | Outcome-card cap sentences now computed from the dedupe-aware count (`effN()`), not the raw audience | `send.js` |
| D-42 | "Choose time" button label reads "Change" only when a time is actually set | `send.js` |

## Phase 3 — Review quality

| ID | What changed | Files |
|---|---|---|
| D-18 | Full email chrome in preview: From, To, CC, Subject, attachments row, body, signature | `send.js`, `send.css`, `app.py` |
| D-19 | Preview any recipient: search by company/name/email, on-demand preview via `/api/merge/preview`, "Rows with issues" jumping | `send.js`, `send.css` |
| D-20/D-31 | Fill highlighting fixed (race: audience rows loaded before first paint) + "Highlight filled values" toggle | `send.js` |
| D-22/D-23 | Template cards show differentiators (last edited, used-in, fields, same-subject warning); template choice explicit — Step-2 header chip "Template: … · Change"; last-used notice | `send.js`, `send.css` |
| D-26/D-27/D-36, D-34, 8.5 | Time picker: quick-pick chips (past ones hidden), default slot never empty, cap-aware live line, tz line in plain language, office-hours/weekend warn-only | `timepicker.js`, `format.js`, `send.js` |
| D-30 | Real HTML escaping restored in email previews | `send.js` |
| D-40 | Scheduled times render in recipient-local time | `send.js`/`format.js` |

## Phase 4 — Structure, state, craft

| ID | What changed | Files |
|---|---|---|
| D-28 → D-43 | **The restructure (final layout):** sequential 4-step flow — Audience / Message / Delivery / Review. One decision per screen, ≤ 3 visible sections, stepper strip (visited clickable, future locked, `aria-current`), persistent "Your send" rail (desktop) / footer summary sheet (mobile), per-step footer (Back + one-line summary + one primary with visible disable reason), "Skip to review" fast path, `?step=` deep links, Review Change-links that return to Review. `schemaVersion` 8 migrates old numeric steps and the interim single-page state | `send.js`, `send.css`, `tests/test_send_dom.py` |
| (D-39 interim) | Single-page progressive disclosure — shipped, then superseded by D-43 per owner direction ("sequential, bucketed… multi screen flow"); its section ids (`#sec-wrap-<key>`, `#acc-body-<key>`) were kept | `send.js`, `send.css` |
| 8.7 | Section-level paints + focus/caret preservation (`rebuildWithFocus`, `data-focus-key`), `aria-live` regions | `send.js` |
| 8.8 | `schemaVersion` + defensive state restore with visible "deleted, we picked X" notices; attachments persist with "N files need re-selecting"; `cm:state` re-evaluates delivery cards | `send.js` |
| 8.10 | Mobile: single column, full-width footer action, safe-area inset | `send.css` |
| 8.11 | `copy.js` GLOSSARY (people/contacts/emails/send) + copy lint test (double spaces, trailing dots, unreplaced `${}`) | `copy.js`, `tests/test_js_core.py` |
| 8.12 | Local telemetry behind `TELEMETRY_ON` flag (default off), console-only, key whitelist, no PII | `telemetry.js`, `send.js` |

## Phase 5 — Verification, a11y, late finds

| ID | What changed | Files |
|---|---|---|
| D-44 | Fixing the last unmatched field no longer blanks the match section and locks Continue (`rebuildWithFocus` builder fix). Test harness `ev()` now raises on page JS errors instead of returning `None` — which is how D-44 (and D-45) had been masked | `send.js`, `tests/test_send_dom.py` |
| **D-45 (P0)** | **Step content was invisible** — `.acc-body` (kept from the old accordion) inherits `display: none` from `settings.css`; the 4-step layout had no `.acc-item` wrapper. Fixed with `.acc-body.step-body { display: block }`. New regression test asserts computed display + visible controls + Tab reachability on all four steps. d44-01..12 screenshots re-captured (the earlier ones show the empty state) | `send.css`, `tests/test_send_dom.py`, `after/d44-01..12.png` |
| D-46 | Accessibility (plan 8.9): radio-group semantics on all three card groups (`role=radiogroup`/`role=radio`/`aria-checked`/roving tabindex, arrow keys move selection and keep focus; list-picker selection restores focus to "Change list"); `pendingFocusKey` focus restore across full repaints; `prefers-reduced-motion` global CSS override + hold-fill gate (progress still announced as text). Verified: zero unnamed interactive elements, dialog already labelled, Esc + focus trap work | `send.js`, `ui.js`, `base.css` |
| seed | Hardcoded signature block (photo + name/role + institute + mobile/email) removed from `seed_template/body.html` so newly seeded templates carry the message only — the app's signature (Settings) is the single signature. Live DB templates were already clean; `signature_html` in settings kept as the direct-signature source | `seed_template/` |

## Test evidence

- `tests/test_send_dom.py`: 13 CDP-driven journey tests (A: structure/null-scan, D: mapping block+unblock, undo window with zero Gmail calls, preview chrome, recipient search, template gallery, **content visibility**, …) — all green.
- Full suite: **127 passed, 0 failed** (2026-11-15).
