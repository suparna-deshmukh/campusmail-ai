# Post-Diwali batch (serials 51-150) — paused 2026-11-17 ~14:55 IST

Stopped at user request (internet going down). Nothing will send while offline.

## State
- Batch 34 "Post-Diwali Outreach · Serials 52-150" (template 18 = Post-Diwali + Sir/Ma'am salutation, compact signature, Cc: adityaa@goa.bits-pilani.ac.in, saylees@goa.bits-pilani.ac.in, account 8)
- Serial 51 (Appurva Rekhi): SENT (batch 33, email id 1268) — note: recipient got 2 copies (user sent the draft manually at 13:09:42, script re-sent 13:10:10)
- Serial 52 (Madhav Gaggar): got 2 copies (race between two scripts)
- Serials 53-150: see DB (batch 34). DB rows may lag Gmail for rows the killed background script sent (show 'pending' but are in Gmail Sent)
- 5 rows marked 'error' with socket timeouts — MAY have been sent server-side: serials 115, 118, 119, 127, 131
- Last confirmed sent serial: 144

## Cc double-delivery bug (fixed)
- Root cause: build_raw in gmail_client.py wrote one "Cc:" header line per address; Gmail expands duplicate Cc header fields into the full list twice -> each CC got 2 copies of every mail
- FIX APPLIED: build_raw now writes a single "Cc: a, b" line (verified in raw of newest mails, serials 116+ = single Cc line)
- App server restarted with fixed code (port 5001). Future sends from UI are safe.
- Mails sent before ~13:50 (serials 51-114) still carry the duplicated Cc in Gmail — cannot be changed retroactively

## Resume procedure (when internet is back)
1. Fetch Gmail in:SENT newer_than:1d, map To -> serial for 51-150 (pace API calls ~1.5s apart)
2. For serials 115/118/119/127/131: if present in Gmail Sent -> mark 'sent' in DB; if absent -> resend
3. Send serials 145-150 (and any other missing) via send_with_retry, single Cc line (fixed build_raw), pace ~12s between sends
4. Reconcile DB: every serial 51-150 present in Gmail Sent -> emails.status='sent' + contacts.status='sent' (commit per row)
5. Report: sent=100, dups: serial 51 and 52 got 2 copies; CCs got 2 copies on pre-fix mails only

## Files
- /tmp/finish_send.py (last send loop), /tmp/check_recent.py (Cc raw check)
- DB backup with old signature: data/signature_html_backup_20261117.html
- DB backup before salutations: data/campusmail_ai.db.pre-salutations-20261117
