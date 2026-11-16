"""Spec 8.9 + 17.1.14: Qwen/Gemini output handling - thinking stripping,
native tool-tag fallback parsing, one JSON repair retry, thinking_config."""
import json

import ai_client
import db
from conftest import ADMIN

TOPEN = "<" + "think" + ">"
TCLOSE = "<" + "/think" + ">"
TTOPEN = "<" + "thinking" + ">"
TTCLOSE = "<" + "/thinking" + ">"
TOOL_OPEN = "<" + "tool_call" + ">"
TOOL_CLOSE = "<" + "/tool_call" + ">"
FN_CLOSE = "<" + "/function" + ">"


def test_strip_thinking_variants(client, tenant):
    assert ai_client.strip_thinking("plain text") == "plain text"
    assert ai_client.strip_thinking("") == ""
    out = ai_client.strip_thinking("A " + TOPEN + "secret reasoning" + TCLOSE + " B")
    assert out == "A  B" and "secret" not in out
    out = ai_client.strip_thinking("A" + TTOPEN + " more " + TTCLOSE + "B")
    assert out == "AB" and "more" not in out
    out = ai_client.strip_thinking("A <THINK> shout </THINK> B")
    assert out == "A  B"
    out = ai_client.strip_thinking("A <thinking\n>deep breath</thinking\n>B")
    assert out == "A B"


def test_fallback_tool_calls_xml(client, tenant):
    content = TOOL_OPEN + '{"name": "get_overview", "arguments": {"a": 1}}' + TOOL_CLOSE + "\nAnd another one."
    tcs = ai_client._fallback_tool_calls(content)
    assert tcs and tcs[0]["function"]["name"] == "get_overview"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"a": 1}
    content2 = '<function=list_campaigns>' + '{"limit": 5}' + '</function>'
    tcs2 = ai_client._fallback_tool_calls(content2)
    assert tcs2 and tcs2[0]["function"]["name"] == "list_campaigns"
    assert json.loads(tcs2[0]["function"]["arguments"]) == {"limit": 5}
    assert ai_client._fallback_tool_calls("no tools in here") is None


def test_chat_raw_uses_fallback_tool_calls(monkeypatch, client, tenant):
    import importlib
    # the mock fixture replaced chat_raw; re-execute the module to restore the
    # real implementation for this one test
    importlib.reload(ai_client)
    def fake_post_chat(payload, timeout=120):
        return {"choices": [{"message": {"content": TOOL_OPEN + '{"name": "get_overview", "arguments": {"a": 1}}' + TOOL_CLOSE}}]}, 1
    monkeypatch.setattr(ai_client, "_post_chat", fake_post_chat)
    out = ai_client.chat_raw([{"role": "user", "content": "overview"}], user_id=None)
    assert out.get("tool_calls"), "native tool tags must be parsed into tool_calls"
    assert out["tool_calls"][0]["function"]["name"] == "get_overview"
