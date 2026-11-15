"""Unit tests for the pure JS helpers, run through node (ESM, no DOM)."""
import json
import pathlib
import shutil
import subprocess

import pytest

BASE = pathlib.Path(__file__).resolve().parent.parent
node = shutil.which("node")
pytestmark = pytest.mark.skipif(not node, reason="node not available")


def run_js(code):
    r = subprocess.run([node, "--input-type=module", "-e", code],
                       capture_output=True, text=True, cwd=str(BASE), timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    return json.loads(r.stdout)


def test_dedupe_groups_and_counts():
    out = run_js("""
      import { dedupeRows } from './static/js/core/dedupe.js';
      const rows = [
        { id: 1, hr_email: "A@X.com", company: "X" },
        { id: 2, hr_email: "a@x.com", company: "X" },
        { id: 3, hr_email: "b@x.com", company: "Y" },
        { id: 4, hr_email: "b@x.com", company: "Z" },
        { id: 5, hr_email: "", company: "W" },
        { id: 6, hr_email: "c@x.com", company: "" },
      ];
      const d = dedupeRows(rows);
      console.log(JSON.stringify({ dupes: d.dupes, crossCompany: d.crossCompany,
        groups: d.groups.map(g => ({ email: g.email, count: g.count, firstId: g.firstId,
                                    companies: g.companies })) }));
    """)
    assert out["dupes"] == 2
    assert out["crossCompany"] == 1
    assert out["groups"][0] == {"email": "a@x.com", "count": 2, "firstId": 1, "companies": ["X"]}
    assert out["groups"][1]["companies"] == ["Y", "Z"]


def test_keep_first_ids():
    out = run_js("""
      import { keepFirstIds } from './static/js/core/dedupe.js';
      const rows = [
        { id: 1, hr_email: "A@X.com" }, { id: 2, hr_email: "a@x.com" },
        { id: 3, hr_email: "" }, { id: 4, hr_email: "b@x.com" },
      ];
      console.log(JSON.stringify(keepFirstIds(rows)));
    """)
    assert out == [1, 3, 4]


def test_plan_delivery_cap_edges():
    out = run_js("""
      import { planDelivery } from './static/js/core/send-plan.js';
      const p = (o, n, cap, sent) => planDelivery({ outcome: o, n, cap, sentToday: sent });
      console.log(JSON.stringify({
        below: p("send", 900, 1000, 0),
        at: p("send", 1000, 1000, 0),
        above: p("send", 1046, 1000, 0),
        partial: p("send", 1200, 1000, 500),
        sched: p("schedule", 1046, 1000, 0),
        draft: p("draft", 1046, 1000, 0),
      }));
    """)
    assert out["below"] == {"outcome": "send", "n": 900, "cap": 1000, "sentToday": 0, "today": 900, "later": 0, "capped": False, "overCap": False}
    assert out["at"]["today"] == 1000 and out["at"]["later"] == 0
    assert out["above"]["today"] == 1000 and out["above"]["later"] == 46 and out["above"]["capped"]
    assert out["partial"]["today"] == 500 and out["partial"]["later"] == 700
    assert out["sched"]["overCap"] and out["sched"]["today"] == 1000 and out["sched"]["later"] == 46
    assert not out["draft"]["capped"] and not out["draft"]["overCap"]


def test_formatting_in_grouping_and_plurals():
    out = run_js("""
      import { fmtInt } from './static/js/core/format.js';
      console.log(JSON.stringify({ a: fmtInt(1046), b: fmtInt(1046000), c: fmtInt(1) }));
    """)
    assert out["a"] == "1,046"
    assert out["b"] == "10,46,000"
    assert out["c"] == "1"


def test_copy_no_junk_or_jargon():
    """8.11: status copy must have no double spaces, literal '...', unreplaced
    ${} placeholders, edge whitespace, or banned jargon (GLOSSARY)."""
    out = run_js("""
      import { MSG, STATUS, GLOSSARY } from './static/js/core/copy.js';
      const samples = [
        MSG.draftsCreated(1, "Gmail"), MSG.draftsCreated(1046, "Gmail"),
        MSG.scheduled(1, "tomorrow 09:00"), MSG.scheduled(2, "tomorrow 09:00"),
        MSG.sent(1), MSG.sent(3),
        MSG.partial(9, 10, 1), MSG.partial(10, 10, 0),
        MSG.stopped(2), MSG.markedReplied(), MSG.liveOn(), MSG.liveOff(),
        MSG.importDone(3, 2), MSG.importDone(1, 0),
        MSG.gmailReconnect("a@b.co"), MSG.limitExceeded(100, 1046),
        MSG.engineStopped(), MSG.aiOff(), MSG.saveFailed(), MSG.sessionExpired(),
        ...Object.values(STATUS).map(s2 => s2.label),
      ];
      const bad = samples.filter(x => typeof x === "string" && (
        /  /.test(x) || /\\.{3}/.test(x) || /\\$\\{/.test(x) || /^\\s|\\s$/.test(x)));
      const jargon = samples.filter(x => typeof x === "string" &&
        GLOSSARY.banned.some(w => new RegExp("\\b" + w + "\\b", "i").test(x)));
      console.log(JSON.stringify({ n: samples.length, bad, jargon }));
    """)
    assert out["n"] >= 15
    assert out["bad"] == []
    assert out["jargon"] == []
