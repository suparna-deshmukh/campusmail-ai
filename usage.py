"""Per-user daily AI budget with graceful degradation (spec 8.10).

Counters live in the non-synced local table `ai_usage` (per instance by
design). Every LLM call - routing, generation, narration, repairs and
provider retries - is recorded before and after; a call is never started
when the budget is exhausted (except the connection test).
"""
import time
from datetime import datetime, timedelta

import db

DEFAULTS = {
    "ai_daily_calls_per_user": 150,
    "ai_daily_tokens_per_user": 400000,
    "ai_daily_calls_workspace": 0,   # 0 = unlimited
}
PER_MINUTE_LIMIT = 20  # fixed anti-loop limit, not configurable
NOTICE_PCT = 80


class BudgetExhausted(Exception):
    """Raised before an LLM call when the budget (or per-minute limit) is hit."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _limit(key):
    try:
        return int(db.get_setting(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return DEFAULTS[key]


def day_key(now=None):
    now = now or datetime.now().astimezone()
    return now.strftime("%Y-%m-%d")


def _row(user_id, day=None):
    day = day or day_key()
    c = db.conn()
    r = c.execute("SELECT * FROM ai_usage WHERE user_id=? AND day=?", (int(user_id), day)).fetchone()
    c.close()
    if r:
        d = dict(r)
        d.setdefault("minute_calls", 0)
        d.setdefault("minute_ts", 0)
        return d
    return {"user_id": user_id, "day": day, "calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "last_call_ts": 0,
            "minute_calls": 0, "minute_ts": 0}


def _workspace_day_total(day=None):
    day = day or day_key()
    c = db.conn()
    r = c.execute("SELECT COALESCE(SUM(calls),0) n FROM ai_usage WHERE day=?", (day,)).fetchone()
    c.close()
    return r["n"]


def reset_at(now=None):
    now = now or datetime.now().astimezone()
    return (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")


def status(user_id, now=None):
    """Current budget state for one user. pct = max of call/token usage."""
    now = now or datetime.now().astimezone()
    row = _row(user_id, day_key(now))
    calls_lim = _limit("ai_daily_calls_per_user")
    tok_lim = _limit("ai_daily_tokens_per_user")
    ws_lim = _limit("ai_daily_calls_workspace")
    ws = _workspace_day_total(day_key(now))
    pct = 0.0
    reasons = []
    if calls_lim > 0:
        pct = max(pct, min(100.0, 100.0 * row["calls"] / calls_lim))
        if row["calls"] >= calls_lim:
            pct = 100.0
            reasons.append("daily call limit")
    if tok_lim > 0:
        tot = row["prompt_tokens"] + row["completion_tokens"]
        pct = max(pct, min(100.0, 100.0 * tot / tok_lim))
        if tot >= tok_lim:
            pct = 100.0
            reasons.append("daily token limit")
    if ws_lim > 0:
        pct = max(pct, min(100.0, 100.0 * ws / ws_lim))
        if ws >= ws_lim:
            pct = 100.0
            reasons.append("workspace daily limit")
    pm = _calls_in_last_minute(user_id)
    per_min_hit = pm >= PER_MINUTE_LIMIT
    return {
        "user_id": int(user_id),
        "day": row["day"],
        "calls": row["calls"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "calls_limit": calls_lim,
        "tokens_limit": tok_lim,
        "workspace_limit": ws_lim,
        "workspace_calls": ws,
        "pct": pct,
        "per_minute_calls": pm,
        "per_minute_limit": PER_MINUTE_LIMIT,
        "exhausted": bool(reasons) or per_min_hit,
        "reason": "; ".join(reasons) or ("per-minute limit" if per_min_hit else ""),
        "reset_at": reset_at(now),
        "notice_pct": NOTICE_PCT,
    }


def _calls_in_last_minute(user_id):
    row = _row(user_id)
    if time.time() - (row.get("minute_ts") or 0) >= 60:
        return 0
    return int(row.get("minute_calls") or 0)


def check(user_id):
    """Raise BudgetExhausted when a call must not start. Returns status dict."""
    s = status(user_id)
    if s["exhausted"]:
        raise BudgetExhausted(s["reason"] or "limit")
    return s


def record(user_id, calls=1, prompt_tokens=0, completion_tokens=0):
    """Meter one or more LLM calls (include provider retries)."""
    day = day_key()
    now = time.time()
    row = _row(user_id, day)
    minute_calls = int(row.get("minute_calls") or 0)
    if now - (row.get("minute_ts") or 0) >= 60:
        minute_calls = 0
    minute_calls += int(calls)
    c = db.conn()
    c.execute("INSERT INTO ai_usage(user_id, day, calls, prompt_tokens, completion_tokens, last_call_ts,"
              " minute_calls, minute_ts) VALUES(?,?,?,?,?,?,?,?) "
              "ON CONFLICT(user_id, day) DO UPDATE SET "
              "calls=ai_usage.calls+excluded.calls, "
              "prompt_tokens=ai_usage.prompt_tokens+excluded.prompt_tokens, "
              "completion_tokens=ai_usage.completion_tokens+excluded.completion_tokens, "
              "last_call_ts=excluded.last_call_ts, "
              "minute_calls=excluded.minute_calls, minute_ts=excluded.minute_ts",
              (int(user_id), day, int(calls), int(prompt_tokens), int(completion_tokens), now,
               minute_calls, now))
    c.commit()
    c.close()


def usage_report(user_id=None):
    """Own usage (members) or per-user table (admins)."""
    c = db.conn()
    days = {}
    for r in c.execute("SELECT * FROM ai_usage ORDER BY user_id, day").fetchall():
        days.setdefault(r["user_id"], []).append(dict(r))
    c.close()
    if user_id is not None:
        rows = days.get(int(user_id), [])
        latest = rows[-1] if rows else None
        st = status(int(user_id))
        return {"users": [dict(latest or {})] if latest else [], "status": st}
    out = []
    for uid, rows in days.items():
        out.append({"user": (db.get_user(uid) or {}).get("email", "user " + str(uid)),
                    "latest": rows[-1], "status": status(uid)})
    return {"users": out}


def set_budget(values, user=None):
    """Admin: set limits. 0 = unlimited where applicable."""
    for k in ("ai_daily_calls_per_user", "ai_daily_tokens_per_user", "ai_daily_calls_workspace"):
        if k in values:
            n = int(values[k])
            if n < 0:
                raise db_service_error("Budget must be 0 (unlimited) or more.")
            db.set_setting(k, str(n))
    db.log_activity("ai_budget", "AI budget limits updated", (user or {}).get("email", "admin"))
    return {"ok": True}


def db_service_error(msg):
    import services
    return services.ServiceError(msg)
