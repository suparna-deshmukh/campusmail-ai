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
    r = d.get("result", {})
    if r.get("exceptionDetails"):
        raise AssertionError("page JS error: %s" % json.dumps(r["exceptionDetails"])[:400])
    return r.get("value")


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


def _wait_step(cdp, title, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ev(cdp, "document.querySelector('.send-step-title')?.textContent") == title:
            return
        time.sleep(0.25)
    pytest.fail("timed out waiting for step %r (title=%r)" % (
        title, ev(cdp, "document.querySelector('.send-step-title')?.textContent")))


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


def test_send_flow_walks_all_four_steps_clean(cdp_session):
    """Journey A (structure): all four steps render, no literal nulls, and
    each step shows at most 3 sections (D-28 progressive disclosure)."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    steps = [
        ("audience", "Who are you emailing?"),
        ("message", "What will they read?"),
        ("delivery", "How should it go out?"),
        ("review", "Check before it goes"),
    ]
    for i, (key, title) in enumerate(steps):
        _wait_step(cdp, title)
        assert ev(cdp, "new URLSearchParams(location.search).get('step')") == key
        sections = ev(cdp, "document.querySelectorAll('#send-panel .step-sec').length")
        assert sections <= 3, "step %r shows %r sections (> 3)" % (key, sections)
        _assert_no_null(cdp, key)
        if i < len(steps) - 1:
            ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').click()")
    # Review shows the preview and the real action (not clicked - no email is
    # ever created or sent by these tests).
    _wait_for(cdp, "#send-root .summary-card")
    _wait_checks(cdp)
    assert ev(cdp, "document.querySelectorAll('#checks-card .check-line').length") >= 2
    primary = ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').textContent")
    assert "draft" in primary.lower() or "Schedule" in primary or "Send" in primary


def test_stepper_states_and_future_steps_disabled(cdp_session):
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    _wait_step(cdp, "Who are you emailing?")
    state = ev(cdp, """[...document.querySelectorAll('.send-step')].map(b => ({
      cur: b.classList.contains('is-current'),
      future: b.classList.contains('is-future'),
      disabled: b.getAttribute('aria-disabled'),
    }))""")
    assert state[0]["cur"] is True
    for future in state[1:]:
        assert future["future"] is True and future["disabled"] == "true"
    # visited steps become clickable
    ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').click()")
    _wait_step(cdp, "What will they read?")
    state = ev(cdp, """[...document.querySelectorAll('.send-step')].map(b => b.classList.contains('is-done'))""")
    assert state[0] is True and state[1] is False


def test_old_saved_state_migrates_to_new_steps(cdp_session):
    """schemaVersion migration: step 1 -> audience, step 2 -> review, and a
    corrupt draft never breaks the page (Journey G)."""
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    for saved, title in [
        ('{"step": 2, "campaignId": 1, "outcome": "draft"}', "Check before it goes"),
        ('{"step": 1, "campaignId": 1, "outcome": "draft"}', "Who are you emailing?"),
        ("{'step': 2, broken json", "Who are you emailing?"),
    ]:
        ev(cdp, "localStorage.setItem('cm_send_1', %r)" % saved)
        ev(cdp, "location.href = '/campaigns/1/send?theme=dark'")
        _wait_step(cdp, title)
    # restored review state must land on the review step with the action ready
    ev(cdp, "localStorage.setItem('cm_send_1', JSON.stringify({step: 2, campaignId: 1, outcome: 'draft', schemaVersion: 4, visited: ['audience','message','delivery','review']}))")
    ev(cdp, "location.href = '/campaigns/1/send?theme=dark'")
    _wait_step(cdp, "Check before it goes")
    assert ev(cdp, "document.querySelectorAll('.send-step.is-done').length") == 3


def test_review_change_link_returns_to_review(cdp_session):
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    _wait_step(cdp, "Who are you emailing?")
    for title in ("What will they read?", "How should it go out?", "Check before it goes"):
        ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').click()")
        _wait_step(cdp, title)
    _wait_for(cdp, "#send-root .summary-card")
    _wait_checks(cdp)
    # jump to Message via the Template row's Change link
    ev(cdp, """(() => {
      const b = [...document.querySelectorAll('.summary-change')]
        .find(x => x.closest('.summary-line')?.children[1]?.textContent === 'Template');
      if (b) b.click();
    })()""")
    _wait_step(cdp, "What will they read?")
    # the primary now returns to Review
    assert ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').textContent") == "Back to Review"
    ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').click()")
    _wait_step(cdp, "Check before it goes")


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
