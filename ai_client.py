"""OpenAI-compatible chat client for MailForge. Key lives in settings (blank by default)."""
import json
import time
import urllib.error
import urllib.request

from db import get_setting


def ai_configured():
    return bool(get_setting("ai_api_key", "").strip())


def chat(messages, max_tokens=1200, temperature=0.7):
    key = get_setting("ai_api_key", "").strip()
    if not key:
        raise RuntimeError("AI API key is not set. Add it in Settings.")
    base = get_setting("ai_base_url", "https://api.openai.com/v1").rstrip("/")
    model = get_setting("ai_model", "gpt-4o-mini")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "MailForge/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def chat_raw(messages, max_tokens=1500, temperature=0.4, tools=None):
    """One chat round. Returns the assistant message dict (content and/or tool_calls)."""
    key = get_setting("ai_api_key", "").strip()
    if not key:
        raise RuntimeError("AI API key is not set. Add it in Settings.")
    base = get_setting("ai_base_url", "https://api.openai.com/v1").rstrip("/")
    model = get_setting("ai_model", "gpt-4o-mini")
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "MailForge/1.0",
        },
    )
    last_err = None
    for _try in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]
        except urllib.error.HTTPError as e:
            last_err = RuntimeError("HTTP " + str(e.code) + ": " + e.read().decode("utf-8", "ignore")[:200])
            if e.code in (400, 429, 500, 502, 503) and _try < 2:
                time.sleep(6 * (_try + 1))
                continue
            raise last_err
        except Exception as e:
            last_err = e
            if _try < 2:
                time.sleep(4)
                continue
            raise
    raise last_err


def test_connection():
    if not ai_configured():
        return {"ok": False, "error": "No API key set"}
    try:
        out = chat([{"role": "user", "content": "Reply with exactly: OK"}], max_tokens=10, temperature=0)
        return {"ok": out.upper() == "OK", "model": get_setting("ai_model", ""), "reply": out[:50]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def write_template(brief, tone):
    """Generate an email template (HTML body + subject) from a brief."""
    prompt = (
        "You are a B2B email writer. Write a professional outreach email.\n"
        "Brief: " + brief + "\nTone: " + tone + "\n\n"
        "Rules: keep it under 180 words. Use {field_name} placeholders for any value that "
        "changes per recipient (e.g. {recruiter_name}, {company_name}). Return JSON only, "
        "no markdown fences, with keys: subject (string), body_html (string). "
        "body_html must be a single HTML fragment using inline styles only, Arial 12px, "
        "simple paragraphs and <b> for emphasis. No tables, no external images."
    )
    raw = chat([{"role": "user", "content": prompt}], max_tokens=2000, temperature=0.6)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    data = json.loads(raw)
    return data["subject"].strip(), data["body_html"].strip()


def improve_subjects(subject, context, n=3):
    prompt = (
        "Here is an email subject line: \"" + subject + "\"\n"
        "Context: " + context + "\n\n"
        "Suggest " + str(n) + " improved subject lines. Under 60 characters each, professional, "
        "curiosity-driving but not clickbaity. Return JSON only: {\"subjects\": [ ... ]}"
    )
    raw = chat([{"role": "user", "content": prompt}], max_tokens=400, temperature=0.8)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    return json.loads(raw)["subjects"]


def suggest_mapping(fields, columns, sheet_note=""):
    prompt = (
        "I have a dataset with columns: " + ", ".join(columns) + "\n"
        "And an email template with placeholders: " + ", ".join(fields) + "\n"
        + (sheet_note if sheet_note else "") +
        "\n\nFor each placeholder, decide which column (or \"\" if none) should replace it. "
        "Return JSON only: {\"mapping\": {\"placeholder\": \"column\"}}. "
        "Use the exact placeholder and column names."
    )
    raw = chat([{"role": "user", "content": prompt}], max_tokens=500, temperature=0.2)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    return json.loads(raw)["mapping"]


def _parse_json(raw):
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def polish_email(subject, body_html, instructions=""):
    """Polish an email draft. Returns (subject, body_html). Preserves {field} placeholders."""
    prompt = (
        "Polish this outreach email. Make it clearer, warmer and more persuasive, but keep it "
        "under 220 words and professional. Keep ALL placeholder tokens like {recruiter_name} "
        "EXACTLY as written. Keep inline HTML styles intact (do not convert to plain text).\n"
        "Instructions: " + (instructions or "none") + "\n\n"
        "Subject: " + subject + "\nBody HTML:\n" + body_html + "\n\n"
        "Return JSON only, no markdown: {\"subject\": \"...\", \"body_html\": \"...\"}"
    )
    data = _parse_json(chat([{"role": "user", "content": prompt}], max_tokens=2200, temperature=0.5))
    return data["subject"].strip(), data["body_html"].strip()


def write_reminder(context, tone="professional"):
    """Generate a follow-up/reminder email template. Returns (subject, body_html)."""
    prompt = (
        "Write a short, polite follow-up/reminder email that will be sent automatically when a "
        "recruiter has not replied to our first outreach email.\n"
        "Context: " + context + "\nTone: " + tone + "\n\n"
        "Rules: 60-110 words. Refer to the original invitation politely; ask if they need more "
        "information; include a line offering a quick call. Use {field_name} placeholders for "
        "per-recipient values (e.g. {recruiter_name}, {company_name}). Return JSON only, no "
        "markdown fences: {\"subject\": \"...\", \"body_html\": \"...\"}. body_html must be an "
        "HTML fragment with inline styles only, Arial 12px, simple <p> paragraphs, <b> for emphasis."
    )
    data = _parse_json(chat([{"role": "user", "content": prompt}], max_tokens=1500, temperature=0.6))
    return data["subject"].strip(), data["body_html"].strip()


def suggest_reply_followup(email_body, reply_snippet):
    """Suggest a reply to a recruiter's response."""
    prompt = (
        "A recruiter replied to our recruiter outreach email. Draft a professional, concise reply "
        "(under 120 words) as the outreach cell. Do not commit to dates; invite a call.\n"
        "Original email:\n" + email_body[:2000] + "\n\n"
        "Their reply:\n" + reply_snippet + "\n\n"
        "Return the reply text only, plain text, no labels."
    )
    return chat([{"role": "user", "content": prompt}], max_tokens=500, temperature=0.6)
