"""Browser-level smoke tests for the send flow (CDP over headless Chrome).

Boots the Flask app in-process on a random port against the isolated temp
tenant (mocked Gmail/LLM) and drives real page loads. The tests never click
the create/send actions - no email is ever generated or sent.

Skipped when Chrome is not available.
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import pytest

import app as appmod  # noqa: E402
import db  # noqa: E402

ADMIN = "admin@test.dev"


def _find_chrome():
    for p in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ):
        if p and os.path.exists(p):
            return p
    return None


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CDP:
    """Minimal Chrome DevTools Protocol client (RFC6455 text frames)."""

    def __init__(self, ws_url):
        parts = ws_url.split("//", 1)[1].split("/", 1)
        host, port = parts[0].split(":")
        path = "/" + parts[1]
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n") % (path, parts[0], key)
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        assert b"101" in resp.split(b"\r\n", 1)[0], "websocket handshake failed"
        self._id = 0

    def send(self, method, params=None):
        self._id += 1
        payload = json.dumps({"id": self._id, "method": method, "params": params or {}}).encode()
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        while True:
            msg = self._recv()
            if msg is None:
                raise RuntimeError("CDP connection closed")
            d = json.loads(msg)
            if d.get("id") == self._id:
                if d.get("error"):
                    raise RuntimeError("CDP error: %s" % d["error"])
                return d["result"]

    def _readn(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _recv(self):
        partial = b""
        while True:
            head = self._readn(2)
            if head is None:
                return None
            fin = head[0] & 0x80
            opcode = head[0] & 0x0F
            n = head[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._readn(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._readn(8))[0]
            data = self._readn(n) if n else b""
            if data is None:
                return None
            if opcode == 8:
                return None
            if opcode == 9:
                self.sock.sendall(bytes([0x8A, len(data)]) + data)
                continue
            if opcode == 0:
                partial += data
            else:
                partial = data
            if fin:
                return partial.decode("utf-8", "replace")


def ev(cdp, expression):
    d = cdp.send("Runtime.evaluate",
                 {"expression": expression, "returnByValue": True, "awaitPromise": True})
    if d.get("exceptionDetails"):
        raise AssertionError("page JS error: %s" % json.dumps(d["exceptionDetails"])[:400])
    return d.get("result", {}).get("value")


def _wait_for(cdp, selector, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ev(cdp, "!!document.querySelector(%r)" % selector):
            return
        time.sleep(0.25)
    pytest.fail("timed out waiting for %s" % selector)


@pytest.fixture()
def cdp_session(tenant, mock_gmail, monkeypatch):
    chrome = _find_chrome()
    if not chrome:
        pytest.skip("Chrome not available")
    monkeypatch.setattr(appmod, "DEV_LOGIN_EMAIL", ADMIN)
    port = _free_port()
    base = "http://127.0.0.1:%d" % port
    srv = threading.Thread(
        target=lambda: appmod.app.run(host="127.0.0.1", port=port, debug=False,
                                      use_reloader=False, threaded=True),
        daemon=True)
    srv.start()
    for _ in range(100):
        try:
            if urllib.request.urlopen(base + "/api/login/state", timeout=1).status == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        pytest.fail("flask test server did not start")

    profile = tempfile.mkdtemp(prefix="cm-cdp-")
    proc = subprocess.Popen(
        [chrome, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    ws_url = None
    deadline = time.time() + 25
    while time.time() < deadline:
        line = proc.stderr.readline()
        if "DevTools listening on" in line:
            ws_url = line.split("ws://", 1)[1].strip()
            break
    if not ws_url:
        proc.terminate()
        pytest.fail("chrome devtools endpoint not found")
    hostport = ws_url.split("/devtools/", 1)[0]
    url = base + "/campaigns/1/send?theme=dark"
    req = urllib.request.Request("http://%s/json/new?%s" % (hostport, urllib.parse.quote(url, safe="")),
                                 method="PUT")
    target = json.load(urllib.request.urlopen(req, timeout=10))
    cdp = CDP(target["webSocketDebuggerUrl"])
    cdp.send("Page.enable")
    cdp.send("Runtime.enable")
    yield {"cdp": cdp, "base": base}
    try:
        cdp.sock.close()
    except Exception:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def _wait_checks(cdp, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = ev(cdp, "document.querySelector('#checks-card')?.textContent || ''")
        if t and "Running" not in t:
            return
        time.sleep(0.25)
    pytest.fail("timed out waiting for checks to finish")


def _assert_no_null(cdp, step):
    text = ev(cdp, "document.getElementById('send-root').textContent")
    assert text and len(text) > 200, "send flow did not render (len=%r)" % (len(text or ""),)
    for bad in ("null", "undefined", "NaN"):
        assert bad not in text, "literal %r rendered on step %r" % (bad, step)


def _current_step(cdp):
    return ev(cdp, """(() => {
      const i = [...document.querySelectorAll('.send-steps .send-step')]
        .findIndex(li => li.classList.contains('is-current'));
      return i < 0 ? null : ['audience','message','delivery','review'][i];
    })()""")


def _section(cdp, key):
    idx = ["audience", "message", "delivery", "review"].index(key) + 1
    rowk = {"audience": "To", "message": "Template", "delivery": "Delivery", "review": "Delivery"}[key]
    return ev(cdp, """(() => {
      const w = document.querySelector('#sec-wrap-%s');
      if (!w) return null;
      const dot = document.querySelector('.send-steps .send-step:nth-child(%d) .send-step-n');
      const row = [...document.querySelectorAll('.rail-row')].find(r => {
        const k = r.querySelector('.rail-k'); return k && k.textContent === %r;
      });
      const reason = document.querySelector('.send-sticky-foot .foot-reason');
      return {
        expanded: true,
        hidden: false,
        status: reason ? reason.textContent : (row ? row.querySelector('.rail-v').textContent : ""),
        statusCls: dot ? dot.className : "",
        sections: w.querySelectorAll('.step-body .step-sec').length,
      };
    })()""" % (key, idx, rowk))


def _open_section(cdp, key, wait=0.7):
    """Navigate to a step through the real UI (stepper or Continue)."""
    out = ev(cdp, """(async () => {
      const STEPS = ['audience','message','delivery','review'];
      const want = STEPS.indexOf('%s');
      for (let guard = 0; guard < 10; guard++) {
        const cur = [...document.querySelectorAll('.send-steps .send-step')]
          .findIndex(li => li.classList.contains('is-current'));
        if (cur === want) return 'ok';
        if (want > cur) {
          const b = document.querySelector('.send-sticky-foot .btn-primary');
          if (!b || b.disabled) return 'blocked-at-' + STEPS[cur];
          b.click();
        } else {
          const li = document.querySelectorAll('.send-steps .send-step')[want];
          const btn = li && li.querySelector('button.send-step-btn');
          if (!btn) return 'locked-at-' + STEPS[want];
          btn.click();
        }
        await new Promise(r => setTimeout(r, 700));
      }
      return 'timeout';
    })()""" % key)
    assert out == "ok", "navigation to %r stopped: %r" % (key, out)
    time.sleep(wait)


def test_step_flow_structure_and_default_state(cdp_session):
    """Journey A (structure): sequential 4-step flow (D-44) - stepper, rail,
    per-step footer, no literal nulls, at most 3 subsections per step."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    _wait_for(cdp, "#send-root #sec-wrap-audience")
    _wait_for(cdp, "#send-root .send-rail")
    # fresh visit with valid defaults -> lands on Audience
    assert _current_step(cdp) == "audience", _current_step(cdp)
    assert ev(cdp, "!!document.querySelector('.send-steps .send-step.is-current [aria-current=step]')"), \
        "current step carries aria-current"
    primary = ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').textContent")
    assert primary == "Continue to Message", primary
    assert ev(cdp, "document.querySelector('.send-sticky-foot .linklike')?.textContent") == "Skip to review"
    _assert_no_null(cdp, "audience")
    # each setup step: <= 3 subsections, status present
    for key in ("audience", "message", "delivery"):
        _open_section(cdp, key)
        s = _section(cdp, key)
        assert s["sections"] <= 3, "step %r shows %r subsections" % (key, s["sections"])
        assert s["status"], "no status line in %r" % key
        _assert_no_null(cdp, key)
    # review: preview + checks + summary, real action in footer (never clicked)
    _open_section(cdp, "review")
    _wait_for(cdp, "#send-root .summary-card")
    _wait_checks(cdp)
    assert ev(cdp, "document.querySelectorAll('#checks-card .check-line').length") >= 2
    _assert_no_null(cdp, "review")
    primary = ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').textContent")
    assert "Create 12 drafts" == primary or "draft" in primary.lower(), primary


def test_schedule_gate_and_time_picker(cdp_session):
    """Schedule without a time: the footer primary names the missing thing
    (D-12); clicking it jumps to Delivery; picking a time unblocks (D-39)."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    _open_section(cdp, "delivery")
    ev(cdp, """(() => {
      const c = [...document.querySelectorAll('#acc-body-delivery .opt-card')]
        .find(x => x.querySelector('.opt-title') && x.querySelector('.opt-title').textContent.includes('Schedule'));
      c.click();
    })()""")
    time.sleep(0.7)
    # D-12: Continue is disabled with the reason shown in visible text
    assert ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').disabled") is True
    assert ev(cdp, "document.querySelector('.send-sticky-foot .foot-reason').textContent") == "Pick a time to schedule"
    st = _section(cdp, "delivery")
    assert "block" in st["statusCls"] and st["status"] == "Pick a time to schedule", st
    # the disabled primary navigates nowhere
    ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').click()")
    time.sleep(0.4)
    assert _current_step(cdp) == "delivery"
    # open the inline time block's picker and accept the valid default
    ev(cdp, "document.querySelector('#acc-body-delivery .card button.btn-ghost').click()")
    _wait_for(cdp, ".modal-foot .btn-primary")
    ev(cdp, "document.querySelector('.modal-foot .btn-primary').click()")
    time.sleep(0.7)
    st = _section(cdp, "delivery")
    assert st["status"].startswith("Scheduled"), st
    assert ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').disabled") is False
    # the chosen time survives navigation (state persists across steps)
    _open_section(cdp, "review")
    primary = ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').textContent")
    assert primary.startswith("Schedule"), primary


def test_old_saved_state_migrates_to_steps(cdp_session):
    """schemaVersion migration: old numeric steps (2-step flow) and the
    single-page "open" arrays (D-39) map onto the sequential steps; a
    corrupt draft never breaks the page (Journey G)."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    for saved, expected in [
        ('{"step": 2, "campaignId": 1, "outcome": "draft"}', "review"),
        ('{"step": 1, "campaignId": 1, "outcome": "draft"}', "audience"),
        ('{"step": "delivery", "campaignId": 1, "outcome": "draft", "schemaVersion": 4}', "delivery"),
        ("{'step': 2, broken json", None),
    ]:
        ev(cdp, "localStorage.setItem('cm_send_1', %r)" % saved)
        ev(cdp, "location.href = '/campaigns/1/send?theme=dark'")
        _wait_for(cdp, "#send-root .send-sticky-foot")
        time.sleep(0.6)
        assert ev(cdp, "!!document.querySelector('#send-stage .step-panel')"), \
            "page failed to render for %r" % saved
        if expected:
            assert _current_step(cdp) == expected, (saved, _current_step(cdp))
        _assert_no_null(cdp, "migrate")
    # v5 round-trip: a single-page "open" list maps onto that step
    ev(cdp, "localStorage.setItem('cm_send_1', JSON.stringify({schemaVersion: 5, campaignId: 1, outcome: 'draft', open: ['message'], listId: 0, templateId: 1, account: 1, mapping: {}, contactIds: []}))")
    ev(cdp, "location.href = '/campaigns/1/send?theme=dark'")
    _wait_for(cdp, "#send-root .send-sticky-foot")
    time.sleep(0.6)
    assert _current_step(cdp) == "message"


def test_review_change_links_jump_steps_and_return(cdp_session):
    """Review 'Change' links jump to the owning step; the next Continue
    brings the user back to Review (D-44)."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    _open_section(cdp, "review")
    _wait_for(cdp, "#send-root .summary-card")
    _wait_checks(cdp)
    for label, key in (("Template", "message"), ("To", "audience")):
        ev(cdp, """(() => {
          const b = [...document.querySelectorAll('.summary-change')]
            .find(x => x.closest('.summary-line') && x.closest('.summary-line').children[1] &&
                       x.closest('.summary-line').children[1].textContent === %r);
          if (b) b.click();
        })()""" % label)
        time.sleep(0.7)
        assert _current_step(cdp) == key, (label, _current_step(cdp))
        _assert_no_null(cdp, key)
        ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').click()")
        time.sleep(0.8)
        assert _current_step(cdp) == "review", (label, _current_step(cdp))


def test_hold_to_confirm_renders_single_label(cdp_session):
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    out = ev(cdp, """(async () => {
      const { holdToConfirm } = await import("/static/js/core/ui.js");
      const btn = document.createElement("button");
      btn.textContent = "Hold to send 5";
      document.body.append(btn);
      let confirmed = 0;
      holdToConfirm(btn, { label: "Hold to send 5", duration: 250, onConfirm: () => { confirmed++; } });
      const settled = btn.textContent.trim();
      let during = "";
      btn.dispatchEvent(new MouseEvent("mousedown"));
      const t0 = Date.now();
      while (confirmed === 0 && Date.now() - t0 < 6000) {
        const t = btn.textContent.trim();
        if (/holding/i.test(t)) during = t;
        await new Promise(r => setTimeout(r, 50));
      }
      btn.dispatchEvent(new MouseEvent("mouseup"));
      await new Promise(r => setTimeout(r, 200));
      const after = btn.textContent.trim();
      return { settled, during, after, confirmed };
    })()""")
    assert out["settled"] == "Hold to send 5", "label duplicated: %r" % out["settled"]
    assert out["confirmed"] == 1, "hold never fired (rAF throttled?)"
    assert "holding" in out["during"].lower() and "%" in out["during"]
    assert out["after"] == "Hold to send 5"


def test_duplicate_skip_default_on(cdp_session, tenant):
    """8.2: duplicates are skipped by default (keep first), the toggle is
    visible on Audience and in the Review checks, and the counts follow it."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    _open_section(cdp, "review")
    assert ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').textContent") == "Create 12 drafts"
    # add two duplicate rows: rec1 (same company) and rec2 (different company)
    c = db.conn()
    for serial, email, company in (("13", "rec1@example.com", "Company1"),
                                   ("14", "rec2@example.com", "OtherCo")):
        c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, sheet_id, created_at) "
                  "VALUES(?, ?, ?, ?, 'new', 1, '%s')" % db.now_iso(),
                  (serial, company, "Dup" + serial, email))
    c.commit(); c.close()
    ev(cdp, "location.href = '/campaigns/1/send?theme=dark'")
    _wait_for(cdp, "#send-root .send-sticky-foot")
    time.sleep(0.6)
    # footer counts the effective audience (14 - 2 dupes)
    _open_section(cdp, "review")
    assert ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').textContent") == "Create 12 drafts"
    # audience quality line: dupes named + toggle ON by default
    _open_section(cdp, "audience")
    q = ev(cdp, "document.querySelector('#sec-aud-quality').textContent")
    assert "2 duplicate emails" in q, q
    assert ev(cdp, "document.querySelector('#sec-aud-quality input[type=checkbox]').checked") is True
    # review: ok line + Skipped summary row
    _open_section(cdp, "review")
    _wait_for(cdp, "#send-root .summary-card")
    _wait_checks(cdp)
    t = ev(cdp, "document.querySelector('#checks-card').textContent")
    assert "will be skipped (keep first)" in t, t
    assert "2+ companies" in t, t
    summ = ev(cdp, "document.querySelector('#summary-card').textContent")
    assert "Skipped" in summ and "2 duplicates" in summ, summ
    # turn skipping off: counts go back, warning line appears
    _open_section(cdp, "audience")
    ev(cdp, "document.querySelector('#sec-aud-quality input[type=checkbox]').click()")
    time.sleep(0.8)
    _open_section(cdp, "review")
    assert ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').textContent") == "Create 14 drafts"
    t = ev(cdp, "document.querySelector('#checks-card').textContent")
    assert "each one still gets an email" in t, t
    assert "Skipped" not in ev(cdp, "document.querySelector('#summary-card').textContent")
    # view duplicates modal
    ev(cdp, "document.querySelector('#view-dupes').click()")
    _wait_for(cdp, ".modal .match-table")
    assert ev(cdp, "document.querySelectorAll('.modal .match-table tbody tr').length") == 2
    assert "different companies!" in ev(cdp, "document.querySelector('.modal').textContent")
    ev(cdp, "document.querySelector('.modal .btn-ghost').click()")


def test_send_now_undo_window(cdp_session, tenant, mock_gmail):
    """8.3/D-15: send now -> 2 s hold -> undo countdown synced to the
    server's release_at -> Cancel undoes the batch. Zero Gmail calls."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    ev(cdp, 'fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({send_undo_seconds: "10"})})')
    ev(cdp, "location.href = '/campaigns/1/send?theme=dark'")
    _wait_for(cdp, "#send-root .send-sticky-foot")
    time.sleep(0.6)
    _open_section(cdp, "delivery")
    ev(cdp, """(() => {
      const c = [...document.querySelectorAll('#acc-body-delivery .opt-card')]
        .find(x => x.querySelector('.opt-title').textContent.includes('Send now'));
      c.click();
    })()""")
    time.sleep(0.6)
    _open_section(cdp, "review")
    ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').click()")
    _wait_for(cdp, ".modal-title")
    title = ev(cdp, "document.querySelector('.modal-title').textContent")
    assert title == "Send 12 real emails now?", title
    sub = ev(cdp, "document.querySelector('.modal-sub').textContent")
    assert "one after another" in sub and "can't be recalled" in sub, sub
    # 2 s hold (pointer)
    btn = ev(cdp, "!!document.querySelector('.modal-foot .btn-danger')")
    assert btn, "hold button expected for < 100 emails"
    ev(cdp, "document.querySelector('.modal-foot .btn-danger').dispatchEvent(new MouseEvent('mousedown'))")
    # the hold auto-fires after 2 s (a real user's late mouseup lands on a
    # closed modal and is a no-op, so we do not dispatch it)
    # progress panel, then the undo countdown modal (a second modal replaces
    # the confirm one once the batch is held)
    deadline = time.time() + 15
    while time.time() < deadline:
        subs = ev(cdp, "[...document.querySelectorAll('.modal-sub')].map(x => x.textContent)")
        if any(str(x).startswith("Sending 12 emails from") for x in subs):
            break
        time.sleep(0.4)
    else:
        pytest.fail("countdown modal never appeared: %r" % subs)
    big = ev(cdp, "[...document.querySelectorAll('.modal-body > div')].map(x => x.textContent).join('|')")
    assert "Sending 12 emails in " in big, big
    # cancel within the window (the countdown modal's Cancel button)
    ev(cdp, """(() => {
      const m = [...document.querySelectorAll('.modal')].pop();
      m.querySelector('.modal-body button.btn-secondary').click();
    })()""")
    deadline = time.time() + 12
    t = ""
    while time.time() < deadline:
        t = ev(cdp, "document.querySelector('.send-success .ss-title')?.textContent || ''")
        if "cancelled" in t.lower() or "undo window" in t.lower():
            break
        time.sleep(0.4)
    assert "Send cancelled" in t, t
    sub = ev(cdp, "document.querySelector('.send-success .ss-sub').textContent")
    assert "went back to your queue" in sub, sub
    assert mock_gmail.sends == [], "zero Gmail calls before undo"
    c = db.conn()
    st = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM emails GROUP BY status")}
    holds = c.execute("SELECT COUNT(*) n FROM undo_holds").fetchone()["n"]
    c.close()
    assert st.get("cancelled") == 12 and holds == 0, (st, holds)


def test_send_now_typed_confirm_required_at_500(cdp_session, tenant):
    """8.9 (decision #9): 500+ real emails require typing the number; no
    hold button is offered."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    # raise the daily cap so the whole audience fits today (cap honesty is
    # tested separately); otherwise the modal would truthfully show 100.
    ev(cdp, 'fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({max_sends_per_day: "1000"})})')
    c = db.conn()
    for i in range(15, 513):  # 498 more rows -> 510 contacts total (12 + 498)
        c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, sheet_id, created_at) "
                  "VALUES(?, ?, ?, ?, 'new', 1, '%s')" % db.now_iso(),
                  (str(i), "Company" + str(i % 9), "Recruiter" + str(i), "bulk%d@example.com" % i))
    c.commit(); c.close()
    ev(cdp, "location.href = '/campaigns/1/send?theme=dark'")
    _wait_for(cdp, "#send-root .send-sticky-foot")
    time.sleep(0.8)
    _open_section(cdp, "delivery")
    ev(cdp, """(() => {
      const c = [...document.querySelectorAll('#acc-body-delivery .opt-card')]
        .find(x => x.querySelector('.opt-title').textContent.includes('Send now'));
      c.click();
    })()""")
    time.sleep(0.6)
    _open_section(cdp, "review")
    ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').click()")
    _wait_for(cdp, ".modal-title")
    assert ev(cdp, "document.querySelector('.modal-title').textContent") == "Send 510 real emails now?"
    assert ev(cdp, "!!document.querySelector('.modal-foot .btn-danger')") is False, "no hold button at 500+"
    inp = ev(cdp, "document.querySelector('.modal input[aria-label*=number]')?.placeholder || ''")
    assert inp == "510", "typed-number input expected"
    ok = ev(cdp, "document.querySelector('.modal input[aria-label*=number] + .btn-danger, .modal .btn-danger')?.disabled")
    # wrong number keeps it disabled
    ev(cdp, """(() => { const i = document.querySelector('.modal input[aria-label*=number]');
      i.value = "999"; i.dispatchEvent(new Event('input'));
      return document.querySelectorAll('.modal .btn-danger')[0].disabled; })()""")
    assert ev(cdp, "document.querySelectorAll('.modal .btn-danger')[0].disabled") is True
    ev(cdp, """(() => { const i = document.querySelector('.modal input[aria-label*=number]');
      i.value = "510"; i.dispatchEvent(new Event('input')); })()""")
    time.sleep(0.3)
    assert ev(cdp, "document.querySelectorAll('.modal .btn-danger')[0].disabled") is False
    ev(cdp, "document.querySelector('.modal .btn-ghost').click()")
    time.sleep(0.5)
    assert ev(cdp, "document.querySelectorAll('.modal').length") == 0


def test_review_preview_chrome_highlight_and_tpl_chip(cdp_session, tenant):
    """D-18: preview shows From + CC chrome; D-20: highlight toggle;
    D-23: template chip in Review."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    _open_section(cdp, "review")
    _wait_for(cdp, "#send-root .preview-paper")
    time.sleep(2.5)
    src = ev(cdp, "document.querySelector('.preview-paper').srcdoc")
    assert "From: teambox@gmail.com" in src, "preview must show the From account"
    assert "CC:" not in src
    assert "cm-fill" in src, "fill highlight on by default"
    # D-23: template chip with Change action
    chip = ev(cdp, "document.querySelector('.review-tpl-chip')?.textContent || ''")
    assert "Template:" in chip and "Outreach" in chip and "Change" in chip
    # D-20: toggle highlight off
    ev(cdp, "(() => { const h = document.querySelector('.recip-hi input'); h.click(); return 1; })()")
    time.sleep(0.8)
    src2 = ev(cdp, "document.querySelector('.preview-paper').srcdoc")
    assert "var fills=" not in src2, "highlight off removes the fill-marking script"
    # D-18: CC set on the Message step shows up in the Review preview
    _open_section(cdp, "message")
    ev(cdp, "(() => { const h = [...document.querySelectorAll('#acc-body-message .addon-head')]"
            ".find(x => x.textContent.includes('CC')); if (h) h.click(); return 1; })()")
    time.sleep(0.6)
    ev(cdp, "(() => { const i = document.querySelector('#acc-body-message input[aria-label=\"CC addresses\"]');"
            " i.value = 'cc-check@example.com'; i.dispatchEvent(new Event('change')); return 1; })()")
    time.sleep(0.8)
    _open_section(cdp, "review")
    _wait_for(cdp, "#send-root .preview-paper")
    time.sleep(2.5)
    src3 = ev(cdp, "document.querySelector('.preview-paper').srcdoc")
    assert "CC: cc-check@example.com" in src3, "CC must appear in the live preview"


def test_review_recipient_search_and_issue_rows(cdp_session, tenant):
    """D-19: preview any recipient by search, and jump to rows with issues."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    c = db.conn()
    c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, sheet_id, created_at) "
              "VALUES('99','UniqueCo','Uniqua Recruiter','uniqua@example.com','new',1,'%s')" % db.now_iso())
    c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, sheet_id, created_at) "
              "VALUES('98','NoEmailCo','No Mail Person','','new',1,'%s')" % db.now_iso())
    c.commit(); c.close()
    ev(cdp, "location.href = '/campaigns/1/send?theme=dark'")
    _wait_for(cdp, "#send-root .send-sticky-foot")
    _open_section(cdp, "review")
    _wait_for(cdp, "#send-root .preview-paper")
    time.sleep(2.5)
    chip = ev(cdp, "document.querySelector('#issue-chip')?.textContent || ''")
    assert "Rows with issues (1)" in chip, "no-email row must be flagged"
    ev(cdp, "document.querySelector('#issue-chip').click()")
    time.sleep(1.8)
    cap = ev(cdp, "document.querySelector('#recip-caption').textContent")
    assert "Issue 1 of 1" in cap and "no email" in cap
    # back to the first 8
    ev(cdp, "document.querySelector('#recip-caption a').click()")
    time.sleep(0.9)
    assert "Person 1 of" in ev(cdp, "document.querySelector('#recip-caption').textContent")
    # search a specific person -> on-demand single preview
    ev(cdp, "(() => { const i = document.querySelector('.recip-search');"
            " i.value = 'uniqua'; i.dispatchEvent(new Event('input')); })()")
    time.sleep(2.5)
    cap2 = ev(cdp, "document.querySelector('#recip-caption').textContent")
    assert "Previewing" in cap2 and "Uniqua" in cap2
    src = ev(cdp, "document.querySelector('.preview-paper').srcdoc")
    assert "uniqua@example.com" in src


def test_template_gallery_differentiators(cdp_session, tenant):
    """D-22: gallery cards show last-edited meta, same-subject warning and a
    Preview action."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    c = db.conn()
    c.execute("INSERT INTO email_templates(name, subject, body_html, kind, is_active, fields, created_at) "
              "VALUES('Outreach v2','Hi {company}','<p>Dear {hr_name},</p>','outreach',0,'[\"company\", \"hr_name\"]','%s')"
              % db.now_iso())
    c.commit(); c.close()
    ev(cdp, "location.href = '/campaigns/1/send?theme=dark'")
    _wait_for(cdp, "#send-root .send-sticky-foot")
    _open_section(cdp, "message")
    ev(cdp, "document.querySelector('#sec-msg-template .tpl-main-head .btn-secondary').click()")
    _wait_for(cdp, ".modal .modal-title")
    time.sleep(0.8)
    assert ev(cdp, "document.querySelectorAll('.modal .tpl-card').length") == 2
    metas = ev(cdp, "[...document.querySelectorAll('.modal .tpl-card .tpl-meta')].map(x => x.textContent)")
    assert any("Last edited" in m for m in metas), "cards show last-edited"
    warns = ev(cdp, "[...document.querySelectorAll('.modal .tpl-card .tpl-warn')].map(x => x.textContent)")
    assert len(warns) == 2 and all("Same subject as" in w for w in warns)
    ev(cdp, "document.querySelectorAll('.modal .tpl-card .tpl-preview')[0].click()")
    time.sleep(0.8)
    assert ev(cdp, "document.querySelectorAll('.modal .tpl-full-frame').length") == 1
    ev(cdp, "(() => { const b = [...document.querySelectorAll('.modal .btn-ghost')].pop(); b.click(); return 1; })()")
    time.sleep(0.4)
    ev(cdp, "document.querySelector('.modal .btn-ghost').click()")
    time.sleep(0.4)


def test_unmatched_field_blocks_continue(cdp_session, tenant):
    """Journey D: an unmapped field blocks Continue with the field named in
    visible text, the stepper dot flags it, and fixing the mapping
    unblocks."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    c = db.conn()
    cur = c.execute("INSERT INTO email_templates(name, subject, body_html, kind, is_active, fields, created_at) "
                    "VALUES('Odd Fields','Hi {company}','<p>Dear {hr_name}, code {oddcode}</p>','outreach',0,"
                    "'[\"company\", \"oddcode\"]','%s')" % db.now_iso())
    tid = cur.lastrowid
    c.commit(); c.close()
    ev(cdp, "localStorage.setItem('cm_send_1', JSON.stringify({schemaVersion: 8, campaignId: 1, listId: 0, templateId: %d, account: 1, mapping: {}, contactIds: [], outcome: 'draft', step: 'audience', visited: ['audience']}))" % tid)
    ev(cdp, "location.href = '/campaigns/1/send?theme=dark'")
    _wait_for(cdp, "#send-root .send-sticky-foot")
    time.sleep(1.0)
    # Audience is fine; Continue to Message is still offered...
    ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').click()")
    time.sleep(1.0)
    assert _current_step(cdp) == "message"
    # ...but on Message the primary is disabled and names the field
    assert ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').disabled") is True
    reason = ev(cdp, "document.querySelector('.send-sticky-foot .foot-reason').textContent")
    assert reason == "Match \u2018oddcode\u2019 to continue", reason
    # stepper dot flags the blocked step
    assert "is-block" in ev(cdp, "document.querySelector('.send-steps .send-step:nth-child(2) .send-step-n').className")
    # fixing the mapping unblocks (the match table is right here on this screen)
    out = ev(cdp, """(() => {
      const sel = document.querySelector('#sec-msg-match tr.is-unmatched select');
      if (!sel) return 'noselect';
      sel.value = 'company';
      sel.dispatchEvent(new Event('change'));
      return 'set';
    })()""")
    assert out == 'set', out
    time.sleep(0.8)
    # the section repainted itself and the block is gone
    assert ev(cdp, "document.querySelector('#sec-msg-match').children.length") > 0, "match section repainted"
    assert "All 2 fields matched" in ev(cdp, "document.querySelector('#sec-msg-match').textContent")
    assert ev(cdp, "document.querySelectorAll('#sec-msg-match tr.is-unmatched').length") == 0
    assert ev(cdp, "document.querySelector('.send-sticky-foot .foot-reason') === null") is True
    assert ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').disabled") is False
def test_step_content_is_visible(cdp_session):
    """D-45: the step body must actually be painted. The old accordion CSS
    (settings.css: .acc-body { display: none }) leaked into the 4-step
    layout and hid every step's content while DOM-level checks still passed."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    for step in ("audience", "message", "delivery"):
        _open_section(cdp, step)
        assert _current_step(cdp) == step
    _open_section(cdp, "review")
    for step in ("audience", "message", "delivery", "review"):
        if _current_step(cdp) != step:
            _open_section(cdp, step)
        ok = ev(cdp, """(() => {
          const a = document.querySelector('#acc-body-' + arguments[0]);
          return a ? getComputedStyle(a).display : 'missing';
        })()""".replace("arguments[0]", '"%s"' % step))
        assert ok == "block", "step %r body display=%r" % (step, ok)
        vis = ev(cdp, """(() => {
          const a = document.querySelector('#acc-body-' + "%s");
          if (!a) return -1;
          const els = Array.from(a.querySelectorAll("button, input, select, .opt-card, .list-card, .tpl-main"));
          return els.filter(x => x.offsetParent !== null).length;
        })()""" % step)
        assert vis > 0, "step %r has no visible controls" % step
    # and the keyboard reaches the step content (focus boundary check)
    ev(cdp, "document.getElementById('send-root').setAttribute('tabindex', '-1')")
    ev(cdp, "document.getElementById('send-root').focus()")
    time.sleep(0.3)
    reached = []
    for _ in range(60):
        cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9})
        cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9})
        reached.append(ev(cdp, """(() => {
          const a = document.activeElement;
          return document.querySelector('#acc-body-review') && document.querySelector('#acc-body-review').contains(a) ? 'content' : a.tagName;
        })()"""))
        if "content" in reached:
            break
    assert "content" in reached, "Tab never reaches the review step content: %r" % reached
