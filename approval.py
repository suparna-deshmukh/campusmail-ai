"""Server-enforced Approval Gate (spec 5.3).

An approval token binds {user, tenant, conversation, action, params hash,
preview hash, render hash} and expires after 10 minutes. Execution always
runs the STORED params - the client and the model never send params.
Edits void the token; bulk-send acknowledgements are verified here, not in
the UI.
"""
import hashlib
import json
import time
import uuid

import db

TTL = 10 * 60  # 10 minutes


class ApprovalError(Exception):
    def __init__(self, message, code=409):
        super().__init__(message)
        self.message = message
        self.code = code


def _h(s):
    return hashlib.sha256((s or "").encode("utf-8", "replace")).hexdigest()


def canonical(params):
    return json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)


def mint(action, params, conv_id, uid, render_hash=None, preview_hash=None):
    row = {
        "approval_id": "appr_" + uuid.uuid4().hex[:16],
        "conv_id": conv_id or "",
        "uid": int(uid),
        "action": action,
        "params_json": canonical(params),
        "params_hash": _h(canonical(params)),
        "render_hash": render_hash or "",
        "preview_hash": preview_hash or "",
        "expires_at": time.time() + TTL,
        "used": 0,
        "voided": 0,
        "created_at": db.now_iso(),
    }
    c = db.conn()
    c.execute("INSERT INTO approvals(approval_id, conv_id, uid, action, params_json, params_hash,"
              " render_hash, preview_hash, expires_at, used, voided, created_at)"
              " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (row["approval_id"], row["conv_id"], row["uid"], row["action"], row["params_json"],
               row["params_hash"], row["render_hash"], row["preview_hash"], row["expires_at"],
               row["used"], row["voided"], row["created_at"]))
    c.commit()
    c.close()
    return row


def void(approval_id):
    if not approval_id:
        return
    c = db.conn()
    c.execute("UPDATE approvals SET voided=1 WHERE approval_id=?", (approval_id,))
    c.commit()
    c.close()


def void_for_conv(conv_id):
    c = db.conn()
    c.execute("UPDATE approvals SET voided=1 WHERE conv_id=? AND used=0", (conv_id,))
    c.commit()
    c.close()


def _fetch(approval_id):
    c = db.conn()
    r = c.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    c.close()
    return dict(r) if r else None


def get(approval_id):
    return _fetch(approval_id)


def consume(approval_id, conv_id, uid, expected_role=None, ack=None, expected_count=None):
    """Validate and consume an approval. Returns the stored params dict.
    Raises ApprovalError with an HTTP-like code on any mismatch."""
    a = _fetch(approval_id)
    if not a:
        raise ApprovalError("That approval is no longer valid.", 404)
    if a["voided"]:
        raise ApprovalError("That approval was replaced by a newer edit - review it again.", 409)
    if a["used"]:
        raise ApprovalError("That approval was already used.", 409)
    if time.time() > a["expires_at"]:
        raise ApprovalError("That approval expired for safety - the card was regenerated.", 409)
    if a["uid"] != int(uid):
        raise ApprovalError("This approval belongs to a different account.", 403)
    if a["conv_id"] and a["conv_id"] != (conv_id or ""):
        raise ApprovalError("This approval belongs to a different conversation.", 403)
    if expected_role and expected_role != "any":
        u = db.get_user(int(uid))
        role_ok = expected_role == "any" or (u and u["role"] == expected_role)
        if not role_ok:
            raise ApprovalError("You no longer have permission for this action.", 403)
    # bulk-send acknowledgement (spec 7.8): server-verified, never trusted from UI state
    if expected_count is not None and expected_count >= _bulk_threshold():
        ack = ack or {}
        if not ack.get("reviewed"):
            raise ApprovalError("Large send: tick the review acknowledgement first.", 422)
        if expected_count >= 4 * _bulk_threshold():
            typed = ack.get("typed_count")
            if typed is None or str(typed).strip() != str(expected_count):
                raise ApprovalError("Type the exact recipient count (%d) to continue." % expected_count, 422)
    c = db.conn()
    c.execute("UPDATE approvals SET used=1 WHERE approval_id=?", (approval_id,))
    c.commit()
    c.close()
    try:
        return json.loads(a["params_json"])
    except Exception:
        raise ApprovalError("Approval data corrupt.", 409)


def _bulk_threshold():
    try:
        return int(db.get_setting("bulk_confirm_threshold") or 50)
    except (TypeError, ValueError):
        return 50
