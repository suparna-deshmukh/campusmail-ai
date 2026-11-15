# Send Flow Improvement — Final Report

Owner: Suparna · Date: 2026-11-15 · Branch: `redesign`
Companion docs: `DIAGNOSIS.md` (all defects), `CHANGELOG.md` (per-defect files).

## What changed, in one paragraph

The send flow is now a **sequential 4-step flow** (Audience → Message → Delivery → Review) instead of one long screen: one decision per step, at most three visible sections per step, a persistent "Your send" summary that updates live, and a single primary button per step that says in visible text why it's disabled. Safety logic is unchanged and strengthened in places: the daily cap is stated as a real promise before every confirmation, duplicates are skipped by default with a visible toggle, send-now has a 10-second undo window plus a stop control, and confirmations restate the exact numbers. Accessibility is built in (radio semantics, arrow-key navigation, labelled dialogs, reduced-motion support, full keyboard reachability).

## The flow, what moved where

| Step | Content | What moved here from the old Step 1 |
|---|---|---|
| 1. Audience "Who are you emailing?" | One compact list card + "Change list" (radio cards ≤ 3 lists, searchable select above), secondary actions (choose contacts / paste / upload), data-quality line with the **duplicate-skip toggle** (8.2) | list + tools + quality; no other sections on this screen |
| 2. Message "What will they read?" | One large template card with live preview, subject, fields + "Change template" (full gallery opens as a modal — the long thumbnail stack is gone), field matching directly under it (calm one-liner when auto-matched, editable table when not, Continue blocked with the field named), collapsed "Message options" (signature, CC, attachments) | template + matching + message options |
| 3. Delivery "How should it go out?" | Send-from account, outcome cards (drafts default "Safest", schedule, send-now, queue) with the **cap-aware sentence inside the selected card**, safe-mode banner only here and only when on, collapsed follow-up reminders; Schedule reveals the time block inline and blocks Continue until a time is picked (D-12) | account + outcome + time + follow-up |
| 4. Review "Check before it goes" | Full email preview for any recipient (search + issue jumping), checklist with inline fixes, summary card with per-row "Change" links that jump back and return, test send, confirmation | old Step 2, unchanged in logic |

Nothing was cut; **nothing exceeded the 3-section limit** on any step (the Review step's preview/checks/summary are the three). Fast path: "Skip to review" appears on Audience whenever the saved config is already valid.

## Decisions taken (owner's Section 11 list — all built as recommended)

1. Duplicates: **skipped by default** (keep-first), visible toggle, persisted per campaign.
2. Schedule over cap: **spread across days automatically**, day split shown explicitly in the modal and success screen.
3. Daily cap: **kept the existing setting** — the UI shows its value, source and a Settings link; nothing hard-coded.
4. Undo window for send-now: **10 s**, shared `SendCountdown` component with the Emails tab.
5. Amber checks: acknowledgement required for **send-now only**; drafts/queue never nag.
6. Numbers: **Indian grouping** (`en-IN`) everywhere, including buttons.
7. Send-time guidance (office hours, weekends): **warn only**.
8. "You used 'X' last time" template notice: **on**.
9. Typed-number confirmation: **on as the accessible alternative ≥ 100**, **required ≥ 500**.

## Outcomes vs Section 2 metrics

| Goal | Status |
|---|---|
| Complete without confusion | 4 steps, one decision each, ≤ 3 sections, visible disable reasons, "Skip to review" fast path. Telemetry hooks in place (flag off) to measure completion next cycle. |
| Fewer mistakes reaching recruiters | Duplicates skipped by default (blocks the #1 reputational error); unmatched fields block Continue with the field named; cap promises pre-confirmed; 13 CDP journey tests guard the regressions. |
| Honesty of promises | `planDelivery()` is the single source for cards, confirm and success screens; the confirm modal and success text use the same numbers as the server batch (verified in tests, zero Gmail calls). |
| Speed | Returning user: 3 Continue clicks or one "Skip to review" + outcome button. |
| Trust | Success screen states partial results explicitly; "where did my emails go" answered by the queued/split-day lines. |
| Accessibility (WCAG 2.2 AA) | Radio semantics, roving tabindex + arrow keys, labelled dialogs with focus trap, reduced motion, zero unnamed controls, Tab reaches every control on all four steps. No critical/serious issues found in the pass. |
| Robustness | 127/127 tests green, including a test that fails if step content is not actually painted (D-45 guard) and a DOM scan for literal null/undefined/NaN. |

## Backend changes (all backward-compatible)

- **B1** `/api/state` now returns `sent_today` / `remaining_today` (additive fields; old clients ignore them).
- **B2** Client-supplied `internal_hold*` keys are stripped; the hold is server-sanctioned. New `POST /api/batches/<id>/undo` (used by the 10 s window). Migration note: nothing to migrate — additive endpoints/fields.
- **`last_template_id`** on campaigns (additive column, defaults 0) powers the "used X last time" notice.
- **B3** duplicate skipping implemented **client-side** (`contact_ids` filter), stated in the summary ("Skipped: N duplicates"); a server-side `skip_duplicates` option can still be added later.
- **B4 (deferred)** async batch-send job with pollable progress: not done. Current large sends run as one request; the UI already reconciles on timeout ("Checking what was sent…"). Proposed as a separate backend PR.

## Deferred / known risks

- **B4 async job** (above) — the main remaining robustness gap for very large sends.
- Lighthouse score not run in this environment (no CI browser budget); the manual axe-equivalent pass above covered the flows.
- The two owner-WIP test files (`test_delivery_buckets.py`, `test_funnel_metric.py`) failed mid-way through this work in combination with other tests; they pass on their own and in the final full run (127/127). They are untracked owner work — left untouched.
- **D-45 lesson:** an earlier "done" state had every step's content `display: none` while all DOM-level checks passed. A paint-level regression test now prevents recurrence.
- Dev server note: the instance on **:5002 is a nohup process, not launchd-managed** (env `CAMPUSMAIL_AI_DEV_LOGIN`); frontend changes need no restart (static files served from disk).

## Owner instructions honoured

- **No mail sent or scheduled to anyone except f20230611@goa.bits-pilani.ac.in.** All send-path tests use the mock Gmail seam (zero real Gmail calls) or the safe-mode/cancel-only path against :5002; every send-now test in the suite asserts zero outbound calls.
- **Signature removal from templates:** the hardcoded signature block (photo, name, role, institute, mobile, email) is removed from `seed_template/body.html` (new tenants), and the orphaned photo asset was deleted. The three live templates were already clean (verified in DB). The Settings signature (`signature_html`) is untouched — it is the "add a signature directly" source and now the only signature.

## Decisions made on my own (flagged per plan rule 8)

- **D-39 → D-43:** shipped the single-page progressive-disclosure layout first, then rebuilt it as the sequential 4-step flow when you directed "sequential, bucketed… multi screen flow". Both commits are in history; the interim layout's section ids were kept so existing tests and saved state still work.
- **List picker focus behaviour:** arrow-key selection on the list picker collapses back to the compact card (same as mouse); focus is restored to "Change list" so keyboard users never drop to <body>.
- **Compact audience card by default** (with "Change list" to expand to radio cards) rather than always showing the list stack — keeps the screen at one decision.
- **`ev()` test-harness fix:** the CDP helper silently swallowed page JS errors (returned `None`), which is why D-44 and D-45 survived earlier verification. It now raises; the undo-window test was adjusted to match real user timing (the 2 s hold auto-fires; a late mouseup is a no-op).

## Suggested next experiments

- A/B the duplicate-skip default (on vs off) measuring duplicate sends per campaign.
- Measure undo usage and stop-mid-send usage from the (flagged) telemetry to size the risk B4 addresses.
- Track time-to-"Create drafts" for returning users against the 60 s target once telemetry is sampled.
