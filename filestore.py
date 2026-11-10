"""File storage for CampusMail AI - local disk or Supabase Storage.

Two backends, switchable in Settings -> "File storage" (storage_backend):
  local    - files on the app's disk, in the current tenant's folders   (default)
  supabase - files in a Supabase Storage bucket (free tier ~1 GB)

Every object is namespaced by tenant prefix ("team" or "ws/<key>") so the
team and each personal workspace can never collide.  The rest of the app
talks only to (name, bytes) through this module.
"""
import json
import mimetypes
import urllib.error
import urllib.request
from contextvars import ContextVar
from pathlib import Path

import db

BASE = Path(__file__).resolve().parent
TEAM_ATTACH = BASE / "data" / "attachments"
TEAM_IMAGES = BASE / "template_images"

PREFIX = ContextVar("campusmail_fs_prefix", default="team")
ATTACH_ROOT = ContextVar("campusmail_fs_attach", default=TEAM_ATTACH)
IMAGES_ROOT = ContextVar("campusmail_fs_images", default=TEAM_IMAGES)


def set_tenant(attach_root, images_root, prefix):
    """Point this context at one tenant's storage (roots may be None = team)."""
    ATTACH_ROOT.set(attach_root or TEAM_ATTACH)
    IMAGES_ROOT.set(images_root or TEAM_IMAGES)
    PREFIX.set(prefix or "team")


def reset_tenant():
    set_tenant(TEAM_ATTACH, TEAM_IMAGES, "team")


def prefix():
    return PREFIX.get()


def _backend():
    try:
        return (db.get_setting("storage_backend", "local") or "local").strip().lower()
    except Exception:
        return "local"


def configured():
    return _backend() == "supabase"


def _sb_cfg():
    url = (db.get_setting("supabase_url", "") or "").strip().rstrip("/")
    key = (db.get_setting("supabase_service_key", "") or "").strip()
    bucket = (db.get_setting("supabase_bucket", "") or "").strip() or "campusmail-files"
    if not url or not key:
        raise RuntimeError("Supabase storage is not configured (URL + service key).")
    return url, key, bucket


def _full(name):
    return PREFIX.get() + "/" + name


def _root(ns):
    return IMAGES_ROOT.get() if ns == "images" else ATTACH_ROOT.get()


def local_path(name, ns="attach"):
    return _root(ns) / name


def _http(method, url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:200]
        raise RuntimeError("Supabase storage %s failed: HTTP %s %s" % (method, e.code, body))


def _sb_headers(key):
    return {"apikey": key, "Authorization": "Bearer " + key}


def put(name, data, ns="attach"):
    if configured():
        url, key, bucket = _sb_cfg()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        _http("POST", url + "/storage/v1/object/" + bucket + "/" + _full(name),
              data=data, headers={**_sb_headers(key), "Content-Type": mime, "x-upsert": "true"})
        return
    p = local_path(name, ns)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def get(name, ns="attach"):
    if configured():
        url, key, bucket = _sb_cfg()
        _, _, data = _http("GET", url + "/storage/v1/authenticated/" + bucket + "/" + _full(name),
                           headers=_sb_headers(key))
        return data
    p = local_path(name, ns)
    return p.read_bytes() if p.exists() else None


def exists(name, ns="attach"):
    if configured():
        url, key, bucket = _sb_cfg()
        status, _, _ = _http("HEAD", url + "/storage/v1/object/" + bucket + "/" + _full(name),
                             headers=_sb_headers(key))
        return status == 200
    return local_path(name, ns).exists()


def size(name, ns="attach"):
    if configured():
        url, key, bucket = _sb_cfg()
        _, headers, _ = _http("HEAD", url + "/storage/v1/object/" + bucket + "/" + _full(name),
                              headers=_sb_headers(key))
        return int(headers.get("Content-Length") or 0)
    p = local_path(name, ns)
    return p.stat().st_size if p.exists() else 0


def delete(name, ns="attach"):
    if configured():
        url, key, bucket = _sb_cfg()
        _http("DELETE", url + "/storage/v1/object/" + bucket,
              data=json.dumps([_full(name)]).encode(),
              headers={**_sb_headers(key), "Content-Type": "application/json"})
        return
    p = local_path(name, ns)
    if p.exists():
        p.unlink()


def local_stats():
    out = {"files": 0, "bytes": 0}
    for root in (ATTACH_ROOT.get(), IMAGES_ROOT.get()):
        if root and root.exists():
            for p in root.rglob("*"):
                if p.is_file():
                    out["files"] += 1
                    out["bytes"] += p.stat().st_size
    return out


def migrate_to_supabase():
    """Upload every local file of the current tenant to the bucket, then delete
    the local copies.  Returns (uploaded, failed)."""
    if not configured():
        raise RuntimeError("Configure Supabase storage first (URL + service key + bucket).")
    url, key, bucket = _sb_cfg()
    ok = fail = 0
    for ns, root in (("attach", ATTACH_ROOT.get()), ("images", IMAGES_ROOT.get())):
        if not root or not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            try:
                put(rel, p.read_bytes(), ns=ns)
                p.unlink()
                ok += 1
            except Exception:
                fail += 1
    return ok, fail
