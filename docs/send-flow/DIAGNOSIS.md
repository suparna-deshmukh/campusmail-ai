# Send flow — Phase 0 diagnosis report

> Date: 2026-11-15 · Author: Qwen (product engineer + designer) · Rule of truth: code beats spec.
> Method: every defect reproduced in the running local app (127.0.0.1:5002, workspace
> "Diwali Outreach", 1,046 contacts, 3 templates, cap `max_sends_per_day = 1000`, safe mode OFF,
> dark/light, 1440x900 + 390x844) with headless-Chrome CDP captures, plus a full code read of
> `static/js/pages/send.js`, `static/css/pages/send.css`, `static/js/core/{ui,format,copy,shell}.js`,
> `static/js/components/timepicker.js`, `static/js/pages/emails.js`, `services.py`, `app.py`.
> Evidence: the six originals in `docs/screenshots/send-flow/` plus fresh live captures in
> `docs/screenshots/send-flow/before-live/`. No email was created or sent during diagnosis.

## 1. Bottom line

- **24 of the 27 listed defects are confirmed** in code and/or live. **D-07 and D-24 are
  misidentified / capture artifacts** (details below). **D-26 is confirmed and worse than
  described**: the quick-pick chips are empty at *all* times, not just at 05:31.
- The **shared-CSS root-cause hypothesis is confirmed**: D-02 to D-06 are one problem — the
  page's component classes (`send-stepper`, `mono-chip`, `ct-tools`, `upload-drop`,
  `match-note`, `opt-cols`, `opt-count`, `tpl-*`, `pick-*`, `send-cap`) were **never styled in
  any stylesheet**. No stale cache, no renamed class: the rules simply don't exist. One fix
  group restores all of them.
- **10 new defects found (D-28 to D-37)**, including one **P0 security issue** (D-32: the
  client can bypass safe mode via `/api/merge/generate`) and one **P1 data-loss-adjacent issue**
  (D-37: retry after a failed long send can double-send).
- **Environment quirk that matters:** in this machine's JS engines (Chrome used by the app and
  Node 26.8.1), `Date + number` evaluates to **string concatenation**, not arithmetic. This
  silently breaks the time-picker chip filter (root cause of D-26) and is a latent hazard for
  any future date math. Verified empirically in both runtimes (Section 6).
- All five Phase-0 backend questions answered (Section 4). **No backend change is blocking**:
  the ones needed are small, backward-compatible, and already named by the plan (Section 9).

## 2. Defect table (brief's IDs)

Status: CONF = confirmed (code + live), CODE = confirmed by code, ARTIFACT = not a defect as described.
Effort: S < 1 h, M half day, L > 1 day (implementation, not diagnosis).

| ID | Sev | Status | Root cause (file:line) | Live evidence | Effort |
|---|---|---|---|---|---|
| D-01 | P0 | CONF | Native `Element.append(null)` coerces `null` to the text "null". `renderStep1()` appends `S.outcome === "schedule" ? whenBlock() : null` at `send.js:259`. The `el()` helper skips nulls, but `panel.append()` is native DOM. DOM scan of `#send-root` finds exactly one "null", in the exact position from the screenshots. | before-live/step1-default-dark.png; DOM scan | S |
| D-02 | P0 | CONF | G1: `.send-stepper` / `.send-step` / `.send-step-n` have no CSS anywhere. Stepper is `<nav>` of two `<button>`s (`send.js:183-199`) → renders as bare text "1What2Check" / "✓What2Check". Also missing `aria-current="step"`. | before-live/step1-default-dark.png | S |
| D-03 | P1 | CONF | G1: `.ct-tools` and `.upload-drop` unstyled (`send.js:343-381`). | before-live/step1-default-dark.png | S |
| D-04 | P1 | CONF | G1: `.mono-chip`, `.opt-cols`, `.opt-count` unstyled (`send.js:329`). "All contacts 1046 contacts companyhr_namehr_email" run together. | before-live/step1-default-dark.png | S |
| D-05 | P1 | CONF | G1: `.match-note` / `.match-disc` unstyled (`send.js:584-608`). Icon on its own line; `<details>` "See how" is a raw browser disclosure triangle. | before-live/step1-default-dark.png | S |
| D-06 | P2 | CONF (minor) | `.badge.neutral` **does** exist (`components.css:217`); the "Default"/"Safest" badges are styled but have no spacing context (`.opt-title` has no gap) so they read as plain text. Real gap is inside G1. | before-live/step1-default-dark.png | S |
| D-07 | P2 | ARTIFACT (misidentified) | The orange bar is **`.live-line`** (`layout.css:105-111`), a fixed 3px `var(--warn)` line shown whenever safe mode is OFF (`body.is-live`, `shell.js:86`). It is an intentional live-mode indicator, not a stuck progress bar. It appears in screenshots 1-5 because the workspace has safe mode OFF. In screenshot 4 (safe-mode mock) it persists because the mock only intercepted the send flow's `/api/state`, not the shell's own polling. The `.topbar-progress` loadbar (`layout.css:195`) is a separate, 0.8 s element. | live: `body.is-live === true` with safe_mode=0 | S (decision + optional clarity tweak) |
| D-08 | P2 | CONF | `#side-user-name` (initial "…") and `#side-avatar` are **never populated by any JS** — `renderShell()` (`shell.js:59-66`) has `state.me` but never writes them. `base_new.html:56-57`. | live: `sideUserName === "…"` | S |
| D-09 | P2 | CONF | `.kbd` is 11px mono with 1px padding (`layout.css:144`); chip is a bare "⌘K" (`base_new.html` topbar palette button) — unreadable at a glance and wrong on Windows/Linux. | before-live/step1-default-dark.png (topbar) | S |
| D-10 | P0 | CONF | Confirm modal promises the full audience (`send.js:1083-1108`: "Send 1,046 real emails?"), server stops at the cap (`services.py:695-717` `merge()` send loop `if sent_today + ok >= cap: break`; remaining rows stay `pending` = "Ready"). Result carries `capped: true` which the UI never reads. Time picker does show the cap line for schedule (verified live: "Your daily limit is 1000. This send puts 1046 on one day." — note it appears only after the picker's own async `/api/state` resolves, see D-36). Success screen reports `res.sent` (the real number) but never mentions the remainder. Also: `/api/state` does not expose `sent_today` / `remaining_today` (needed for a true plan). | before-live/step2-send-confirm-dark.png; services.py read | M (Phase 1: truthful statements; Phase 2: full plan + spread) |
| D-11 | P1 | CONF | `drawChecks()` counts duplicates (case-insensitive, trimmed) and shows amber "80 duplicate emails - each one still gets an email" (`send.js:764-774`). No skip option; server has no dedupe option (Section 4). 80 duplicates verified live in this workspace. | live step 2 checks | M |
| D-12 | P1 | CONF | Step-1 primary is disabled only for `!nPeople() \|\| !S.templateId \|\| matchBlocking()` (`send.js:264-266`) — schedule-without-time is not gated. Step 2 then shows "Scheduled (pick a time first)" while the CTA reads "Schedule N emails"; the only stop is a toast on click (`send.js:1126`). | code + repro (outcome=schedule, when="" reaches step 2) | S |
| D-13 | P1 | CONF | G3: `fmtInt()` is an identity function — `String(Math.round(Number(n) || 0))`, no locale grouping (`format.js:10-12`). Used everywhere, so every count renders `1046`. | before-live (every screen) | S |
| D-14 | P0 | CONF | G2: `confirmSendNow()` builds the button with text "Hold to send N" (`send.js:1094`) **and** `holdToConfirm()` appends a second `.hold-label` span with the same text (`ui.js:304-312`). Live button text: `"Hold to send 1046Hold to send 1046"`. | before-live/step2-send-confirm-dark.png; CDP read | S |
| D-15 | P0 | CONF | Send flow: 2 s hold → `startCreate()` → single synchronous `/api/merge/generate` (`send.js:1153-1199`); no countdown, no server hold, no undo, no stop. Emails tab: hold → 10 s cancel countdown (`emails.js:555-579`) → `/api/emails/send-now` with `send_undo_seconds = 20` server-side undo-hold (`app.py:1872`, `services.py:1565`). The riskiest action has the weakest protection. | code read both paths | L |
| D-16 | P1 | CONF | `confirmSendNow()` body is static copy (`send.js:1088-1092`): no duplicates count, no cap sentence, no "preview covered 8 of N", and a stale-orphaned account renders "They go out from  immediately" (D-35). | before-live/step2-send-confirm-dark.png | M |
| D-17 | P1 | CONF (partial) | `holdToConfirm` supports Space/Enter hold (`ui.js:334-337`) but has no path for switch/assistive users or reduced motion; `confirmTyped` exists in `ui.js` but is unused by this flow. | code read | M |
| D-18 | P1 | CONF (partial) | `previewDoc()` renders only `To:` + subject + body (`send.js:686-706`). No From/CC chrome. **Correction to the brief**: the signature *is* in the preview — `/api/merge/preview` appends `signature_html` to the body when `append_signature` (verified live: `hasSignature: true`); only From/CC rows are missing. | live iframe DOM read | M |
| D-19 | P1 | CONF | Preview is capped at 8 (`limit: 8`, `send.js:833`), navigation is prev/next over those 8 only; no search, no jump-to-issue. | code + screenshot 5 | M |
| D-20 | P1 | CONF — new root cause | G5b: `drawPreview()` (`send.js:724-743`) runs **before** `data.allRows` is fetched (that fetch happens inside `drawChecks()` → `checkRows()` → `audienceRows()`). For "All contacts" the lookup `rows.find(...)` returns nothing → `fills = []` → **no `.cm-fill` highlights on first paint** (verified live: `fills: []`, `fillSpans: 0`). Highlights appear only after a prev/next click (rows loaded by then). For sheet lists, `sheetRows` is loaded on entry, so fills work there. | live srcdoc read (`fillsJson: "[]"`) | S |
| D-21 | P2 | CONF | Footer summary shows "Checking…" (`send.js:825`) while the primary already shows the final count; both update only after the preview round-trip. Cosmetic single-source-of-truth gap. | code | S |
| D-22 | P1 | CONF | Template cards: no last-edited / usage count / Preview action (`send.js:496-516`); thumbnail is an unstyled `.tpl-thumb` iframe (no CSS at all → browser-default 300x150, hard cut, `G1`); placeholder syntax is whatever the template contains (`docx_convert.field_names` normalises detection server-side, but mixed styles are never surfaced to the user). | screenshot 1 | M |
| D-23 | P1 | CONF | `load()` silently pre-selects the default template (`send.js:127-130`); step 2 header shows no template chip, so the choice is invisible at confirmation. | code | S |
| D-24 | P1 | ARTIFACT | In a real 1440x900 viewport the sticky footer pins correctly (before-live captures). The mid-page overlap in full-page captures is a known full-page-screenshot behaviour with `position: sticky`. No fix needed in normal viewports; will still verify mobile bottom padding in Phase 4. | before-live/step1-default-dark.png | — |
| D-25 | P1 | CONF | `.seg` is `overflow-x: auto` with hidden scrollbar (`components.css:113-123`) — at 1440px "Custom" is clipped 15px (CDP-measured: item right edge 453 vs container 438); at 390px far worse. No scroll affordance, clipped item looks broken. | before-live/step1-timepicker-dark.png; CDP rects | S |
| D-26 | P1 | CONF — worse than described | Root cause in G5a: chip filter `c.at() > now + 10 * 60e3` (`timepicker.js:27`). In this runtime `now + 600000` is a **string** (Date+number → concatenation), so the comparison is `number > NaN` → always false → **all chips filtered out at every time of day**. Screenshots from other machines/older engines showed chips, which is why this looked time-dependent. | live: `chips: []` at 04:52 IST; engine tests Section 6 | S |
| D-27 | P1 | CONF | G5c: initial `d = now + 1h` rarely lands on a 15-min boundary; `syncFields()` does `timeSel.value = "HH:MM"` with a value that matches no `<option>`, which deselects everything → select renders **empty** (CDP: `timeValue: ""`). `readFields()` then falls back to "10:00" (`timepicker.js:39`) — a silent change of the user's intent. | live CDP read | S |
| — | — | Sidebar `100vh` (brief §3.6) | `.app-shell { min-height: 100dvh; display: flex }`, `.side` is a flex column (`layout.css:3,6-12`) — correct in viewport; sidebar cutoff in full-page captures is the same artifact as D-24. | — | — |
| — | — | Spec jumps 15 → 17 | Confirmed: no Section 16 exists; the intro's "Section 16 lists observed inconsistencies" actually refers to **Section 14** (seeds). Noted; the seeds were taken from §14 as instructed. | — | — |

## 3. New defects found during diagnosis

| ID | Sev | Title | Root cause / evidence | Fix |
|---|---|---|---|---|
| D-28 | P1 | **Step 1 overloaded: eight decisions on one screen** | Owner direction (change of course, 2026-11-15). Hick's law + progressive disclosure + one-decision-per-screen + error proximity: campaign, list, contact tools, template, field matching, account, outcome, and four add-ons currently share one scrolling column; the CTA is ~6 screens of scroll away; a schedule-without-time error only appears at step 2 (D-12); full re-render on every change (`send.js:175-181`). | **Restructure into a 4-step flow: Audience → Message → Delivery → Review** per the owner's spec (one compact card per decision, ≤ 3 visible sections per step, live summary rail, `?step=audience|message|delivery|review`, schemaVersion migration 1→audience / 2→review). Its own PR, before further layout work. |
| D-29 | P1 | Hold-to-confirm label duplication is component-level | G2 affects **every** caller: `emails.js:543-554` builds "Hold to send N emails" and then `holdToConfirm` appends the same label → the Emails-tab send-now button duplicates too. | Fix once in `ui.js` (single label; during hold → "Keep holding… NN%"). |
| D-30 | P2 | Preview escaping is a no-op | `previewHtmlEsc` (`send.js:684`) and the inlined `esc()` in `previewDoc` (`send.js:698`) replace `&`→`&`, `<`→`<`, `>`→`>` — broken entities (original `&`/`<`/`>` lost). Latent: a filled value containing `&`, `<`, or `>` corrupts the preview DOM (and is a preview-scoped injection vector). | Restore real entity escaping. |
| D-31 | P1 | Fill-highlight race (D-20 root cause) | `drawPreview` before `data.allRows` load → `fills: []` for "All contacts" (live-verified). | Await/fetch audience rows before drawing previews; keep a single rows loader shared with checks. |
| D-32 | P0 (security) | Client can bypass safe mode via `/api/merge/generate` | `api_merge_generate` (`app.py:1339-1349`) passes the raw request JSON to `services.merge()` with no key filtering. `merge()` reads `internal_hold` / `internal_hold_seconds` from that dict (`services.py:599-600, 606`). A client sending `{"action":"send","internal_hold":true}` skips the safe-mode 409 and the batch is then held + released by the scheduler — **real sends while safe mode is ON**. The comment says "never from client/tool" but nothing enforces it. | Strip `internal_hold*` keys in the endpoint (strengthening a guard — permitted). |
| D-33 | P2 | Step-2 primary enabled while "Checking…" | Spec §5.3 says disabled while checks run; code passes `disabled=false` and only disables afterwards if `preview_total == 0` (`send.js:821-828`). Clicking early uses `nPeople()` fallback. | Disable until preview+checks settle (fold into restructure). |
| D-34 | P2 | Time-zone line inconsistent | `tzLabel()` (`format.js:93-102`) produced "Asia/Kolkata (IST)" in one capture and "Asia/Calcutta (GMT+5:30)" in another (engine ICU variance). Cosmetic, but the legacy name looks stale. | Normalise display name (8.5 scope). |
| D-35 | P2 | Orphaned saved account breaks the confirm modal | Saved `S.account` can reference a deleted/renamed account; `accountEmail()` (`send.js:1060-1062`) returns "" → "They go out from  immediately" (live-verified with orphan id; real account id in this workspace is 8). No validation of restored ids against loaded data (also 8.8 scope). | Validate restored ids; one-line notice + fallback. |
| D-36 | P2 | Time-picker cap line flashes in | The picker fetches `/api/state` again after opening and only then computes the cap line (`timepicker.js:171-176`) → the warning is absent for the first ~200-800 ms (this is why the reference screenshot 3 lacks it). Also a second redundant request. | Pass `dailyCap` into `openTimePicker()` from the caller's already-loaded state. |
| D-37 | P1 | Retry after failed send can double-send | `startCreate()` catch → error card "Couldn't finish." with `onRetry: startCreate` (`send.js:1190-1199`). The send path is one long synchronous request; on network failure mid-batch some emails are already sent, and Retry creates a **new batch for the whole audience again** → duplicates. Also `merge()`'s send loop never re-checks row status, so no endpoint can stop an in-flight batch. | Phase 2: async job + progress + Stop (8.3); never blind-retry a send outcome. |
| D-38 | P2 | "Same email, different company" not distinguished | `drawChecks` dupes are flat (`send.js:764-774`); 8.2 wants the cross-company case flagged separately. | Fold into 8.2 duplicate work. |
| D-39 | P1 | **4-step flow inflates the happy path** | Owner second change of course (2026-11-15): four full screens put the primary action 3 navigations from the default state; a 2-second send takes ~8 s of navigation; first-time users are lost between screens. Fix: **single page, four collapsible sections** (Audience, Message, Delivery, Review) with status headers, one footer primary acting on the first incomplete section; valid config opens collapsed and ready; error proximity = expand the failing section. Supersedes the D-28 4-step layout (section renderers and all safety logic are kept, only the navigation layer changes). `schemaVersion` 5 migrates step names to expanded sections. |

| D-40 | P2 | Scheduled time shown in UTC | `fmtLong`/picker displayed the stored time without local conversion in some captures (live, 2026-11-15). | Convert to the recipient-local / device time zone consistently (8.5 scope). |
| D-41 | P1 | Outcome cards showed cap lines that ignored duplicate skipping | `outcomeCards` cap sentences computed `n` from `nPeople()` (raw audience) while the send itself uses `effN()` (dedupe-aware): with 1,058 contacts / 91 dupes / cap 1,000 the Schedule card claimed "58 the next day" and Send-now "82 stay in queue" although all 967 fit today. Found during Phase 2 live verification. | Cap sentences computed from `effN()` (same function as the confirm modal and success screen - one source of truth). |
| D-42 | P2 | "Choose time" button label always read "Change" | `whenBlock` tested bare `when` (which resolves to an inherited native function, always truthy) instead of `S.when`. | Test `S.when`. |
| D-44 | P1 | Fixing the last unmatched field on the Message step left the field-matching section **empty** and Continue disabled forever: the change handler calls `paintMatchSection()`, which passed an already-built node to `rebuildWithFocus(host, build)`; the helper runs `build()` and threw `TypeError: build is not a function` *after* `host.innerHTML = ""`, so the section was wiped and `updateFooterChrome()` never ran. Latent since the D-39 era; masked because the test harness swallowed page JS errors. | Pass a builder: `rebuildWithFocus(host, () => matchSection())`. Audited all callers (only one). Test `test_unmatched_field_blocks_continue` now asserts the section repainted, "All 2 fields matched" shows, no unmatched rows remain, the footer reason is gone, and Continue is re-enabled. The harness `ev()` was also fixed to surface page JS errors (it previously returned `None` on any page exception, hiding defects like this one). |
| D-45 | P0 | **Step content invisible**: every step's body rendered `display: none` - only header, stepper, rail and footer were visible. Root cause: the 4-step layout (D-43) reused the `.acc-body` class, and `settings.css` (imported after `send.css`) globally sets `.acc-body { display: none }` for the old accordion pattern (shown only under `.acc-item.is-open`). The D-43 restructure dropped the `.acc-item` wrapper but kept the class. All earlier verification was DOM-level (textContent/queries), which passes even for hidden nodes, so the defect survived; the `d44-01..12` captures actually show the empty steps. | `send.css`: `.acc-body.step-body { display: block; }` (higher specificity than the settings.css rule, order-independent). Regression test `test_step_content_is_visible` asserts computed display, visible controls and Tab reachability on all four steps. Screenshots re-captured. |
| D-46 | P1 | **Accessibility gaps (plan 8.9)**: the option-card groups (list, template gallery, outcome) were plain buttons with no radio semantics - no `role=radiogroup`, no `aria-checked`, no arrow-key navigation; no `prefers-reduced-motion` support anywhere (CSS and the hold-to-confirm fill); focus was lost across full step repaints when interacting with the field-matching table or radios. | Radio pattern implemented in `send.js` (`radioGroup()` helper): `role=radiogroup` + labelled container, `role=radio` + `aria-checked` + roving `tabindex` cards, arrow keys move selection and keep focus (WAI-ARIA APG pattern); selection that collapses the list picker restores focus to "Change list". `pendingFocusKey` + `paintStage()` restore focus to the `data-focus-key` node after repaints (8.7). `base.css`: global `prefers-reduced-motion` override; `ui.js` `holdToConfirm` skips the fill animation when reduced motion is set (the % label still announces progress). Verified live: arrow nav on all three groups, focus retention, reduced-motion rules served, zero unnamed interactive elements on all four steps, modal already had `role=dialog`/`aria-modal`/`aria-label` (no change needed). |

| D-43 | P1 | **Single-page layout has no hierarchy; owner directed a sequential multi-screen flow** | After D-39 shipped, the owner reviewed the landing screen (2026-11-15, screenshot `docs/screenshots/send-flow/`): four identical collapsed accordion rows read as a flat log - no focal point, no persistent send summary, primary action detached from the content, "Checking…" status left in a header. Owner direction: "everything should be sequential, bucketed... seamlessly ask these questions one after another... multi screen flow", with a tight screen count. | **Sequential 4-step flow** (supersedes D-39): Audience → Message → Delivery → Review. Stepper strip (visited steps clickable, future locked, aria-current), persistent "Your send" rail on desktop (To/Template/From/Delivery + Change), per-step footer (Back + one-line summary + one primary; Continue disabled with visible reason; "Skip to review" fast path), `?step=` deep links with pushState/popstate, Review Change-links jump to the owning step and the next Continue returns to Review. `schemaVersion` 8; single-page `open` arrays and old numeric steps migrate. One decision per screen, ≤ 3 visible subsections, all safety logic and API calls unchanged. |

## 4. Backend answers (Phase 0 questions)

1. **Batch vs `max_sends_per_day`?** The send loop **truncates**: `if sent_today + ok >= cap: break`
   (`services.py:699`, same in `batch_send_as` :1534 and `emails_send_now` :917). Unsent rows keep
   their creation status `pending` — i.e. they remain **Ready** in the batch (Emails tab vocabulary
   via `copy.js statusOf`). The response carries `sent`, `failed`, `capped: true`; no error, no
   auto-reschedule, and the UI currently ignores `capped`.
2. **Is `/api/merge/generate` synchronous for `send`?** **Yes.** The whole batch is sent sequentially
   inside one request with `time.sleep(0.4)` between emails (`services.py:701-716`) — ≈ 7-10 min for
   1,046 emails, a single long-lived fetch on the client (`send.js:1160`), no progress, no stop.
3. **Where is `internal_hold_seconds` honoured?** Three places, all server-side: `merge()` send path
   (as `hold_seconds`, `services.py:620-640`), `batch_send_as()` (`services.py:1512-1545`), and
   `send_now_held()` (`services.py:1565-1590`) used by `/api/emails/send-now` when the setting
   `send_undo_seconds` > 0 (currently **20** in this workspace). Mechanics: rows are stored as
   `scheduled` with `release_at = now + hold`, an `undo_holds` row is recorded, a one-shot release
   timer nudges the scheduler, and `undo_send()` / `stop_remaining()` (`services.py:784, 828`) cancel
   before release. **The send flow never uses it** — and see D-32: it is not stripped from client
   input to `/api/merge/generate`.
4. **Dedupe option?** **None.** `merge()` turns every audience row into an email. Duplicate detection
   exists only for display (client checks) and in sheet-parse stats. 8.2 requires either a
   `skip_duplicates` server option or client-side `contact_ids` filtering (fallback, stated in the
   summary as "Skipped: N duplicates").
5. **Can a batch be stopped mid-send?** For **held/scheduled** batches: yes (`stop_remaining`,
   `/api/emails/cancel`, Stop action in the Emails tab). For the **send flow's synchronous send**:
   **no** — the in-request loop does not re-check status and no endpoint can interrupt it (see D-37).

## 5. Spec vs code discrepancies (code wins)

| # | Spec says | Code does | Action |
|---|---|---|---|
| 1 | §5.1 caption "N people in total" formatted with thousands separators | `fmtInt` has no grouping (D-13) | Fix code (decision #6: en-IN). |
| 2 | §4.8 zone line example "Mon 17 Nov at 09:00" | `fmtLong` → "Monday 17 November at 09:00" | Keep code; update spec. |
| 3 | Screenshot 3 sub "Pick when these 1046 emails go out." | "Choose when the 1,046 emails should go out." | Reference screenshots predate current code; no action. |
| 4 | §5.3 primary "disabled while checks run" | Enabled until preview settles (D-33) | Fix code in restructure. |
| 5 | §4.3.1 "Select all (selects all *visible* rows)" | Selects **all matches** (render is capped at 500) | Code is better; keep code, note in spec. |
| 6 | §3.2 "every change calls save()" | `touched` guard: plain visits save nothing (anti-false-banner fix) | Keep code; spec update. |
| 7 | §7.5 "daily cap (default 100)" | Code default 100; **this workspace sets 1000** | Setting wins; 8.1 shows the configured value + Settings link, no hard-coding. |
| 8 | Intro: "Section 16 lists observed inconsistencies" | Section 16 doesn't exist; the seeds are §14 | Note only (per owner instruction, seeds taken from §14). |
| 9 | §4.2 "up to 6 column-header chips" | Code matches (6) | Owner's new structure wants "4 + '+N'"; apply in restructure. |
| 10 | §5.1 preview "To / subject / body" only | Same (signature is inside body; no From/CC) | Upgrade per 6.5/D-18. |

## 6. Runtime quirk: `Date + number` → string concatenation (verified)

Evidence (this machine, both runtimes):

```
node -p 'typeof (new Date() + 600000)'                      -> "string"
node -p 'Object.prototype.toString.call(new Date() + 0)'    -> "[object String]"
Chrome (app runtime), same expressions:                      -> string / false
```

Consequence: `c.at() > now + 10 * 60e3` (timepicker.js:27) becomes
`number > NaN` → **always false** → quick-pick chips always empty (D-26). Only one occurrence in
the codebase (grep for `Date +`/`60e3` patterns). **Engineering rule for this repo:** never add
arithmetic to a `Date` object; always `date.getTime() ± ms`. All new date math in the send flow
follows this, and the time-picker fix does too. (If this is an engine change specific to 2026
releases, the `getTime()` form is correct on any engine.)

## 7. Root-cause groups (one fix per group)

- **G1 — Missing send-flow component CSS** → D-02, D-03, D-04, D-05, D-06 (+ D-25 seg polish,
  `.tpl-thumb` crop in D-22). Fix once in `static/css/pages/send.css` using existing tokens.
- **G2 — `holdToConfirm` double label** → D-14, D-29. Fix in `ui.js`.
- **G3 — `fmtInt` identity** → D-13. Fix in `format.js` (en-IN), then apply to the two raw
  `${n}` uses in `timepicker.js`.
- **G4 — native `append(null)`** → D-01. Guard at call sites + DOM smoke test.
- **G5 — Time-picker defaults** → D-26 (chip filter), D-27 (empty time select: snap default to
  next 15-min slot ≥ 10 min ahead), D-36 (cap line passed in, not re-fetched).
- **G6 — Synchronous send** → D-10 (truthful statements first), D-15, D-37 (Phase 2 async job).
- **G7 — State restore without validation** → D-35 + 8.8 (schemaVersion, id validation, notices).

## 8. Proposed order (per owner's change of course)

1. **Phase 0** — this report. ✅
2. **Phase 1 (stop the bleeding), one commit per group**: D-01 (+ null-scan test), D-13,
   D-14/D-29, G1 CSS (D-02…D-06, D-09), D-08, D-12 gating, D-10 truthful statements (needs the two
   new `/api/state` fields), D-26/D-27 picker fixes (functional breakage, pulled forward from
   Phase 3).
3. **D-28 restructure — its own PR**: 4-step flow (Audience / Message / Delivery / Review) per the
   owner's spec, with `schemaVersion` migration, summary rail, ≤ 3 sections per step, visible
   Continue reasons, "Skip to review", Change-links that return to Review. Safety logic from 8.1-8.4
   renders in its new location (cap sentence on Delivery + Review, duplicate toggle on Audience +
   Review).
4. **Phase 2 (send safety)** in the new locations: 8.1 full (spread across days), 8.2 duplicate
   skipping end-to-end, 8.3 undo window + real progress + Stop, 8.4 truthful confirmations,
   8.9 accessible confirmation path, D-32 hardening, D-37 no-double-retry.
5. **Phase 3 (review quality)**: 6.5 preview upgrades (D-18/D-19/D-20/D-31), checklist inline
   fixes, test-send clarity, D-22/D-23 template identification, 8.5 picker polish (D-25 wrap,
   office-hours/weekend warning, D-34 tz label).
6. **Phase 4**: 8.7 (largely satisfied by the restructure), 8.8 state hardening, 8.10 responsive,
   8.11 microcopy, 8.12 telemetry flag.
7. **Phase 5**: QA matrix A-H re-run on the 4 steps, a11y audit, before/after screenshots
   (1440/390 × dark/light) for all four steps under `docs/screenshots/send-flow/after/`,
   CHANGELOG.md + FINAL_REPORT.md.

## 9. Backend changes required (all backward-compatible; none blocking)

| # | Change | Why | Scope |
|---|---|---|---|
| B1 | `/api/state` adds `sent_today` and `remaining_today` (computed like `sends_today()` incl. held) | 8.1 plan honesty; UI currently re-guesses | S, additive fields |
| B2 | `/api/merge/generate` strips `internal_hold` / `internal_hold_seconds` from the client payload | D-32 safe-mode bypass (strengthening a guard) | S |
| B3 | `merge()` accepts `skip_duplicates` (keep-first, case-insensitive) | 8.2; fallback: client-side `contact_ids` filter, stated in summary | S |
| B4 | Async batch-send job (create batch → job → poll status) with stoppable progress | 8.3 / D-37; **large — separate PR**, UI ships with fallback per plan §10 | L |
| B5 | None needed for "spread across days": per-email `scheduled_at` already exists (stagger); the client can split into per-day batches | 8.1 | — |

Per the Phase-0 rule this report would normally stop for owner sign-off on backend contract
changes — all five above are already named by the plan (B1/B3/B4 in §8.1/8.2/8.3; B2 is a
guard-strengthening the plan permits without asking). Proceeding on that basis; B4 is proposed
separately as its own backend PR.
