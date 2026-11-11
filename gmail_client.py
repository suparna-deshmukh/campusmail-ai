"""Gmail OAuth + API for multiple accounts. Tokens live in the DB.

Scope: gmail.compose (create drafts + send). Never reads mail.
Client secret: pasted in Settings (stored in DB), or client_secret.json file.
"""
import base64
import json
import re
import urllib.parse
import urllib.request

import db

SCOPES = ["https://www.googleapis.com/auth/gmail.compose",
         "https://www.googleapis.com/auth/gmail.readonly"]
REDIRECT_URI = "http://localhost:5001/api/auth/callback"


def _client_secret_path():
    from pathlib import Path
    return Path(__file__).resolve().parent / "client_secret.json"


def oauth_ready():
    if db.get_setting("client_secret_json", "").strip():
        return True
    return _client_secret_path().exists()


def _client_secret_file():
    """Return a usable client_secret.json path (pasted one materialized)."""
    from pathlib import Path
    pasted = db.get_setting("client_secret_json", "").strip()
    if pasted:
        p = _client_secret_path()
        if not p.exists() or p.read_text() != pasted:
            p.write_text(pasted)
        return str(p)
    return str(_client_secret_path())


def token_exists(account_id):
    return bool(db.account_token_json(account_id))


def account_connected(accounts):
    return [a["id"] for a in accounts if db.account_token_json(a["id"])]


AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def _client_config():
    return json.loads(open(_client_secret_file()).read())["web"]


def start_auth(account_id):
    if not oauth_ready():
        raise RuntimeError("No OAuth client configured yet. Paste the client secret JSON in Settings first.")
    cfg = _client_config()
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": str(account_id),
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params), None


def complete_auth(code, account_id):
    cfg = _client_config()
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok = json.loads(urllib.request.urlopen(req, timeout=30).read())
    from datetime import datetime, timedelta, timezone
    expiry = None
    if tok.get("expires_in"):
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=int(tok["expires_in"]))).isoformat()
    d = {"token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
         "token_uri": TOKEN_ENDPOINT, "client_id": cfg["client_id"],
         "client_secret": cfg["client_secret"], "expiry": expiry}
    db.save_account_token(account_id, json.dumps(d))
    # learn the account email lazily via API
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        svc = build("gmail", "v1", credentials=Credentials(d["token"],
                  token_uri=TOKEN_ENDPOINT, client_id=cfg["client_id"],
                  client_secret=cfg["client_secret"], refresh_token=d.get("refresh_token")))
        email = svc.users().getProfile(userId="me").execute().get("emailAddress")
        c = db.conn()
        c.execute("UPDATE gmail_accounts SET email=?, label=COALESCE(NULLIF(label,''), ?) WHERE id=?",
                  (email, email, account_id))
        c.commit()
        c.close()
        return email
    except Exception:
        return None


def _creds_from(account_id):
    from google.oauth2.credentials import Credentials
    raw = db.account_token_json(account_id)
    if not raw:
        raise RuntimeError("This Gmail account has no stored login yet.")
    d = json.loads(raw)
    expiry = None
    if d.get("expiry"):
        try:
            from datetime import datetime, timezone
            expiry = datetime.fromisoformat(d["expiry"])
            if expiry.tzinfo is not None:
                expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            expiry = None
    c = Credentials(d["token"], token_uri=d.get("token_uri"),
                    client_id=d.get("client_id"), client_secret=d.get("client_secret"),
                    refresh_token=d.get("refresh_token"), expiry=expiry)
    if c.expired and c.refresh_token:
        from google.auth.transport.requests import Request
        c.refresh(Request())
        d["token"] = c.token
        if c.expiry:
            d["expiry"] = c.expiry.isoformat()
        db.save_account_token(account_id, json.dumps(d))
    return c


def get_service(account_id):
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_creds_from(account_id))


def current_email(account_id):
    try:
        c = db.conn()
        r = c.execute("SELECT email FROM gmail_accounts WHERE id=?", (account_id,)).fetchone()
        c.close()
        if r and r["email"]:
            return r["email"]
        return get_service(account_id).users().getProfile(userId="me").execute().get("emailAddress")
    except Exception:
        return None


def inline_local_images(body_html):
    """Replace src="local:file.png" with base64 data URIs.
    Resolves the image through filestore (local disk or Supabase Storage),
    then falls back to the legacy template_images/ directory."""
    from pathlib import Path
    import filestore
    img_dir = Path(__file__).resolve().parent / "template_images"

    def _sub(match):
        quote, name = match.group(1), match.group(2)
        if not name.startswith("local:"):
            return match.group(0)
        fname = name[len("local:"):]
        data = None
        try:
            data = filestore.get(fname, ns="images")
        except Exception:
            data = None
        if data is None:
            f = (img_dir / fname).resolve()
            if not str(f).startswith(str(img_dir.resolve())) or not f.exists():
                return match.group(0)
            data = f.read_bytes()
        ext = Path(fname).suffix.lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/png")
        b64 = base64.b64encode(data).decode("ascii")
        return "src=" + quote + "data:" + mime + ";base64," + b64 + quote

    q1, q2 = chr(39), chr(34)
    pattern = "src=([" + q1 + q2 + "])([^" + q1 + q2 + "]+)"
    return re.sub(pattern, _sub, body_html)


def build_raw(to_addr, subject, body_html, attachments=None, cc=None):
    """Build a MIME message. attachments: list of (filename, bytes, content_type). cc: list of emails."""
    NL = chr(10)
    cc_line = (NL.join(["Cc: " + a for a in cc]) + NL) if cc else ""
    headers = "To: " + to_addr + NL + "Subject: " + subject + NL + cc_line
    headers += "MIME-Version: 1.0" + NL
    if not attachments:
        headers += "Content-Type: text/html; charset=utf-8" + NL
        raw = (headers + NL + body_html).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")
    import uuid
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    outer = MIMEMultipart("mixed")
    outer["To"] = to_addr
    if cc:
        outer["Cc"] = ", ".join(cc)
    outer["Subject"] = subject
    inner = MIMEMultipart("alternative")
    inner.attach(MIMEText(body_html, "html", "utf-8"))
    outer.attach(inner)
    for fname, data, ctype in attachments:
        maintype, subtype = ctype.split("/", 1) if "/" in ctype else ("application", "octet-stream")
        part = MIMEBase(maintype, subtype)
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=fname)
        outer.attach(part)
    raw = outer.as_bytes()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def create_draft(account_id, to_addr, subject, body_html, attachments=None, cc=None):
    svc = get_service(account_id)
    msg = {"raw": build_raw(to_addr, subject, inline_local_images(body_html), attachments, cc)}
    return svc.users().drafts().create(userId="me", body={"message": msg}).execute()


def _message_id_of(svc, msg_id):
    try:
        meta = svc.users().messages().get(userId="me", id=msg_id,
                                          format="metadata",
                                          metadataHeaders=["Message-Id"]).execute()
        for h in meta.get("payload", {}).get("headers", []):
            if h.get("name", "").lower() == "message-id":
                return h.get("value", "").strip("<>")
    except Exception:
        pass
    return ""


def send_message(account_id, to_addr, subject, body_html, attachments=None, cc=None):
    svc = get_service(account_id)
    payload = {"raw": build_raw(to_addr, subject, inline_local_images(body_html), attachments, cc)}
    res = svc.users().messages().send(userId="me", body=payload).execute()
    return {"id": res.get("id", ""), "message_id": _message_id_of(svc, res.get("id", ""))}




def send_with_retry(account_id, to_addr, subject, body_html, attachments=None, cc=None):
    from googleapiclient.errors import HttpError
    import time
    last = None
    for attempt in range(4):
        try:
            return send_message(account_id, to_addr, subject, body_html, attachments, cc)
        except HttpError as e:
            last = e
            if e.resp.status in (403, 429) and attempt < 3:
                time.sleep(5 * (attempt + 1))
            else:
                raise
    raise last


def check_replies(account_id, pairs):
    """Check which of the given sent messages got a reply.
    pairs: list of (local_email_id, message_id). Returns dict local_email_id -> True."""
    import time
    svc = get_service(account_id)
    found = {}
    for local_id, mid in pairs:
        if not mid:
            continue
        hit = False
        for q in ('is:inbox "in-reply-to: ' + mid + '"', 'is:inbox "references: ' + mid + '"'):
            try:
                r = svc.users().messages().list(userId="me", q=q, maxResults=1).execute()
                if r.get("messages"):
                    hit = True
                    break
            except Exception:
                break
            time.sleep(0.15)
        if hit:
            found[str(local_id)] = True
        time.sleep(0.15)
    return found
