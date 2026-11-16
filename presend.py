"""Deterministic pre-send check (spec 7.5) + SSRF-safe best-effort link checker.

Zero LLM calls. Runs before every R4 final review and after every field
change in Compact mode. Severities: "block" (confirm stays disabled),
"warn" (visible, never blocks), "info" (optional suggestion, never forced).
"""
import hashlib
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import db
import services

LINK_TIMEOUT = 3
LINK_CAP = 10
LINK_CACHE_SECS = 600
PLACEHOLDER_HOSTS = ("example.com", "example.org", "localhost", "127.0.0.1", "0.0.0.0",
                     "192.0.2.", "203.0.113.", "test.", "placeholder")


def _line(lid, severity, text, fix=None, icon=None):
    line = {"id": lid, "severity": severity, "text": text}
    if fix:
        line["fix"] = fix
    if icon:
        line["icon"] = icon
    return line


def _render_sample(params):
    """Render the first recipient's email. Returns (subject, body_html, row, batch_like) or raises ServiceError."""
    c = db.conn()
    tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (int(params["template_id"]),)).fetchone()
    if not tpl:
        c.close()
        raise services.ServiceError("Template not found.", 404)
    rows = services.audience_rows(int(params.get("sheet_id") or 0), params.get("serial_from"),
                                  params.get("serial_to"), params.get("contact_ids"))
    c.close()
    if not rows:
        raise services.ServiceError("No contacts in that selection.", 404)
    subject, body = services.render_email(dict(tpl), services.row_vals(rows[0]), params.get("mapping") or {})
    if params.get("append_signature"):
        body = services.append_signature(body)
    return subject, body, rows[0], dict(tpl)


def _cc_lines(params, out):
    try:
        cc_list = services.parse_cc(params.get("cc"))
    except services.ServiceError as e:
        out.append(_line("cc_bad", "block", str(e.message), fix="edit_cc"))
        return
    if len(cc_list) > 20:
        out.append(_line("cc_many", "block", "CC has %d addresses - the platform allows at most 20." % len(cc_list), fix="edit_cc"))
    # unresolvable CC merge field (a {field} that is not in the template's fields)
    raw = (params.get("cc") or "")
    for f in re.findall(r"\{([a-zA-Z0-9_]+)\}", raw):
        if f not in services.all_fields("", "") and f not in (params.get("mapping") or {}):
            out.append(_line("cc_field", "block", "CC uses {{%s}} which is not a known field." % f, fix="edit_cc"))


def _audience_lines(params, rows, out):
    no_email = [r for r in rows if not (r["hr_email"] or "").strip()]
    if no_email:
        out.append(_line("no_email", "warn",
                         "%d row(s) have no email address - they will be skipped." % len(no_email)))
    addrs = [r["hr_email"].strip().lower() for r in rows if (r["hr_email"] or "").strip()]
    seen, dups = set(), set()
    for a in addrs:
        if a in seen:
            dups.add(a)
        seen.add(a)
    if dups:
        out.append(_line("dup_email", "warn",
                         "%d duplicate email address(es) will be skipped." % len(dups)))
    if params.get("contact_ids"):
        c = db.conn()
        b = c.execute("SELECT COUNT(*) n FROM contacts WHERE id IN (%s) AND status='bounced'"
                      % ",".join("?" * len(params["contact_ids"])), [int(i) for i in params["contact_ids"]]).fetchone()["n"]
        c.close()
    else:
        c = db.conn()
        q = "SELECT COUNT(*) n FROM contacts ct WHERE ct.sheet_id=?"
        qargs = [int(params.get("sheet_id") or 0)]
        if params.get("serial_from"):
            q += " AND ct.serial >= ?"
            qargs.append(int(params["serial_from"]))
        if params.get("serial_to"):
            q += " AND ct.serial <= ?"
            qargs.append(int(params["serial_to"]))
        b = c.execute(q + " AND ct.status='bounced'", qargs).fetchone()["n"]
        c.close()
    if b and not params.get("just_fixed"):
        out.append(_line("bounced", "warn",
                         "%d bounced contact(s) are in this audience. Fix them first or they will bounce again." % b,
                         fix="fix_bounces"))


def _content_lines(params, subject, body, rows, out):
    tpl = None
    c = db.conn()
    tpl = c.execute("SELECT * FROM email_templates WHERE id=?", (int(params["template_id"]),)).fetchone()
    c.close()
    tpl = dict(tpl) if tpl else {}
    # field detection must run on the TEMPLATE text, not the rendered text
    # (rendering already replaced the placeholders)
    fields = services.tpl_fields(tpl)
    if not fields:
        fields = services.all_fields(tpl.get("subject") or "", tpl.get("body_html") or "")
    mapping = params.get("mapping") or {}
    unmapped = [f for f in fields if f not in mapping]
    if unmapped:
        out.append(_line("unmapped", "block",
                         "Unresolved merge field(s): " + ", ".join("{" + f + "}" for f in unmapped)
                         + " - they would render blank for every recipient.", fix="edit_mapping"))
    # fields that resolve to blank for some recipients
    blank_counts = {}
    if mapping and rows:
        for r in rows:
            vals = services.row_vals(r)
            for f in fields:
                m = mapping.get(f)
                if not m or not str(m).strip():
                    continue
                if str(m).startswith("column:"):
                    col = str(m)[7:]
                    if not (vals.get(col) or "").strip():
                        blank_counts.setdefault(f, []).append(r["serial"])
    for f, serials in blank_counts.items():
        shown = ", ".join(str(s) for s in serials[:5])
        out.append(_line("blank_field", "warn",
                         "%d recipient(s) have an empty %s (e.g. serials %s) - the email would read with a gap."
                         % (len(serials), f, shown), fix="show_rows"))
    if not (params.get("subject_override") or subject).strip():
        out.append(_line("subject_empty", "block", "Subject is empty.", fix="edit_email"))
    else:
        subj = (params.get("subject_override") or subject).strip()
        if len(subj) > 78:
            out.append(_line("subject_long", "warn",
                             "Subject is %d characters - it may be cut off in inbox lists." % len(subj)))
    text = re.sub(r"<[^>]+>", "", body).replace("&nbsp;", " ")
    if not text.strip():
        out.append(_line("body_empty", "block", "Body is empty.", fix="edit_email"))
    # inline local: images
    for ref in re.findall(r'(?:src|href)="(local:[^"]+)"', body):
        name = ref[6:]
        import filestore
        if not filestore.exists(name) and not filestore.exists(name, ns="images"):
            out.append(_line("img_missing", "block", "Inline image missing from storage: " + name, fix="edit_email"))


def _link_syntax_lines(tpl_body, mapping, out, links):
    # Always check the TEMPLATE body, never the rendered one: rendered hrefs
    # may carry recipient-specific values, which must not be fetched (spec 13).
    hrefs = re.findall(r'href="([^"]*)"', tpl_body or "") + re.findall(r'src="([^"]*)"', tpl_body or "")
    for raw in hrefs:
        u = raw.strip()
        if not u or u.startswith("#") or u.startswith("local:") or u.startswith("data:") or u.startswith("mailto:"):
            if u in ("", "#"):
                out.append(_line("link_empty", "warn", "A link in the email has an empty target."))
            continue
        if "{" in u:
            names = re.findall(r"\{([a-zA-Z0-9_]+)\}", u)
            missing = [n for n in names if n not in (mapping or {})]
            if missing:
                out.append(_line("link_field", "block",
                                 "A link contains an unresolved merge field (%s) - the URL would be broken."
                                 % ", ".join("{" + n + "}" for n in missing)))
            else:
                # mapped merge field: recipient-specific URL, never fetched
                links["skipped_fields"] += 1
            continue
        low = u.lower()
        if low.startswith("www."):
            out.append(_line("link_scheme", "warn", "Link 'www.' has no http(s):// scheme - some clients won't make it clickable."))
            continue
        if not re.match(r"^https?://", low):
            out.append(_line("link_scheme", "warn", "Link '%s' has no http(s):// scheme." % u[:60]))
            continue
        host = (urllib.parse.urlparse(low).hostname or "").rstrip(".")
        if any(host == h.rstrip(".") or host.startswith(h) for h in PLACEHOLDER_HOSTS):
            out.append(_line("link_dummy", "warn", "Link points to a placeholder host (%s)." % host))
            continue
        links.setdefault("fetch", set()).add(u.split("#", 1)[0].rstrip("/"))


def _ssrf_safe(url):
    """Resolve host; reject private/loopback/link-local/ reserved ranges."""
    try:
        host = urllib.parse.urlparse(url).hostname
        if not host:
            return None
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
                return None
        return url
    except Exception:
        return None


def _link_reachability(urls):
    """Best-effort HEAD (GET fallback). Never raises, never blocks. Returns {url: 'ok'|'error'|'blocked'}."""
    res = {}
    for u in list(urls)[:LINK_CAP]:
        # cache
        h = hashlib.sha256(u.encode("utf-8", "replace")).hexdigest()
        try:
            c = db.conn()
            row = c.execute("SELECT result_json, checked_at FROM link_check_cache WHERE url_hash=?", (h,)).fetchone()
            c.close()
            if row and time.time() - row["checked_at"] < LINK_CACHE_SECS:
                res[u] = json.loads(row["result_json"]).get("status", "error")
                continue
        except Exception:
            pass
        status = _probe(u)
        try:
            c = db.conn()
            c.execute("INSERT OR REPLACE INTO link_check_cache(url_hash, result_json, checked_at) VALUES(?,?,?)",
                      (h, json.dumps({"status": status}), time.time()))
            c.commit()
            c.close()
        except Exception:
            pass
        res[u] = status
    return res


def _probe(u):
    for attempt in range(3):  # follow up to 2 redirects
        safe = _ssrf_safe(u)
        if not safe:
            return "blocked"
        req = urllib.request.Request(safe, method="HEAD")
        req.add_header("User-Agent", "CampusMail-LinkCheck/1.0")
        req.add_header("Accept", "*/*")
        try:
            with urllib.request.urlopen(req, timeout=LINK_TIMEOUT) as r:
                if 200 <= r.status < 400:
                    return "ok"
                if r.status in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location")
                    if loc and attempt < 2:
                        u = urllib.parse.urljoin(u, loc)
                        continue
                return "error"
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and attempt < 2:
                loc = e.headers.get("Location")
                if loc:
                    u = urllib.parse.urljoin(u, loc)
                    continue
            if e.code in (405, 403, 501, 500, 502):
                # retry as GET without reading body
                try:
                    req2 = urllib.request.Request(_ssrf_safe(u) or u)
                    with urllib.request.urlopen(req2, timeout=LINK_TIMEOUT) as r2:
                        r2.read(0)
                        return "ok" if 200 <= r2.status < 400 else "error"
                except Exception:
                    return "error"
            return "error"
        except Exception:
            return "offline"
    return "error"


def template_test_suggested(params, content_hash):
    """Suggest a test send when content is new and audience >= 10 (spec 7.5)."""
    try:
        tid = int(params.get("template_id") or 0)
    except (TypeError, ValueError):
        return False
    n = len(params.get("_rows") or [])
    if n < 10 or not tid or not content_hash:
        return False
    if params.get("test_skipped"):
        return False
    return not services.template_tested(tid, content_hash)


def run(params, async_links=True):
    """Run the full pre-send check. params: template_id, mapping, sheet_id,
    serial_from, serial_to, contact_ids, cc, append_signature, attachments
    (list of names), action, at, stagger, account_id, subject_override,
    just_fixed (bool), test_skipped (bool).
    Returns {lines, count, blocked, warnings, info, links_status, render_hash, content_hash}."""
    out = []
    action = params.get("action") or "queue"
    links = {"fetch": set(), "skipped_fields": 0}
    try:
        subject, body, row, tpl = _render_sample(params)
    except services.ServiceError as e:
        out.append(_line("fatal", "block", e.message))
        return {"lines": out, "count": 0, "blocked": True, "warnings": 0, "info": 0,
                "links_status": {}, "render_hash": "", "content_hash": ""}

    # safety & account
    if action == "send" and db.get_setting("safe_mode") == "1":
        out.append(_line("safe_mode", "block", "Safe mode is ON - immediate sending is blocked.",
                         fix="safe_mode_options"))
    cap = int(db.get_setting("max_sends_per_day") or 100)
    used = services.sends_today(include_held=True)
    remaining = max(0, cap - used)
    n = len(params.get("_rows") or [])
    if action == "send" and n > remaining:
        if params.get("cap_ack"):
            out.append(_line("cap", "warn",
                             "Daily cap: %d used of %d - the first %d email(s) will send now and the remaining %d "
                             "stay queued for another day." % (used, cap, remaining, n - remaining)))
        else:
            out.append(_line("cap", "block",
                             "Daily cap: %d used of %d - only %d left, but %d emails are selected. Pick a remainder option."
                             % (used, cap, remaining, n), fix="cap_choice"))
    if action in ("draft", "schedule", "send"):
        acc = int(params.get("account_id") or 0)
        ok = acc and db.account_token_json(acc)
        if not ok:
            out.append(_line("account", "block", "No usable Gmail account for this action.", fix="settings_gmail"))
    if action == "schedule":
        try:
            services.parse_when(params.get("at"))
        except services.ServiceError as e:
            out.append(_line("when", "block", e.message, fix="edit_time"))
    if n == 0:
        out.append(_line("audience_empty", "block", "The audience is empty - no one would receive this.", fix="attach_list"))
    _audience_lines(params, params.get("_rows") or [], out)
    _cc_lines(params, out)
    _content_lines(params, subject, body, params.get("_rows") or [], out)
    # attachments
    total = 0
    import filestore
    for name in (params.get("attachments") or []):
        if not filestore.exists(name):
            out.append(_line("attach_missing", "block", "Attachment missing from storage: " + name, fix="edit_attachments"))
        else:
            total += filestore.size(name)
    try:
        if total > 15 * 1024 * 1024:
            out.append(_line("attach_size", "block", "Total attachments exceed the 15 MB limit.", fix="edit_attachments"))
    except Exception:
        pass
    _link_syntax_lines(tpl.get("body_html") or "", params.get("mapping") or {}, out, links)
    # template never tested (info only)
    content_hash = services.template_content_hash(tpl)
    cc_str = ""
    if params.get("cc"):
        try:
            cc_str = ", ".join(services.parse_cc(params.get("cc") or ""))
        except services.ServiceError:
            cc_str = ""  # invalid CC is reported as a cc_bad line above
    render_hash = services.render_hash(row["hr_email"] or "", subject, body,
                                       cc_str,
                                       [a for a in (params.get("attachments") or [])],
                                       int(params.get("account_id") or 0))
    if template_test_suggested(params, content_hash):
        out.append(_line("test_suggest", "info",
                         "This template content has never been test-sent. Consider sending a test to yourself first.",
                         fix="test_email"))
    links_status = {}
    if links["fetch"]:
        if async_links:
            def _bg():
                links_status.update(_link_reachability(links["fetch"]))
            t = threading.Thread(target=_bg, daemon=True)
            t.start()
            links_status = _link_reachability(links["fetch"])  # first pass inline so the card has data fast
        else:
            links_status = _link_reachability(links["fetch"])
    for u, st in links_status.items():
        if st == "offline":
            out.append(_line("link_offline", "info", "Couldn't check '%s' (network unavailable) - the send is not affected." % u[:80]))
        elif st == "blocked":
            out.append(_line("link_blocked", "info", "Couldn't verify '%s' (private/blocked address) - the send is not affected." % u[:80]))
    blocked = any(l["severity"] == "block" for l in out)
    warnings = sum(1 for l in out if l["severity"] == "warn")
    info = sum(1 for l in out if l["severity"] == "info")
    return {"lines": out, "count": n, "blocked": blocked, "warnings": warnings, "info": info,
            "links_status": links_status, "render_hash": render_hash, "content_hash": content_hash,
            "cap": {"used": used, "cap": cap, "remaining": remaining},
            "subject": subject, "body": body}


def render_hash_of(params):
    """Recompute the render hash for stored send params (approval integrity)."""
    try:
        subject, body, row, tpl = _render_sample(params)
        import services
        cc = ""
        if params.get("cc"):
            cc = ", ".join(services.parse_cc(params["cc"]))
        return services.render_hash(row["hr_email"] or "", subject, body, cc,
                                     [a for a in (params.get("attachments") or [])],
                                     int(params.get("account_id") or 0))
    except Exception:
        return ""
