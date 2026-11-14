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


def test_send_flow_renders_no_null_undefined_or_nan(cdp_session):
    cdp = cdp_session["cdp"]
    _wait_for(cdp, "#send-root .send-sticky-foot")
    text = ev(cdp, "document.getElementById('send-root').textContent")
    assert text and len(text) > 200, "send flow did not render (len=%r)" % (len(text or ""),)
    for bad in ("null", "undefined", "NaN"):
        assert bad not in text, "literal %r rendered in step 1 DOM" % bad
    ev(cdp, "document.querySelector('.send-sticky-foot .btn-primary').click()")
    _wait_for(cdp, "#send-root .summary-card")
    text = ev(cdp, "document.getElementById('send-root').textContent")
    assert text and len(text) > 200, "step 2 did not render (len=%r)" % (len(text or ""),)
    for bad in ("null", "undefined", "NaN"):
        assert bad not in text, "literal %r rendered in step 2 DOM" % bad
