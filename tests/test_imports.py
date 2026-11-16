"""Spec F-21 + 17.1.12: import edge cases - TSV, Unicode, 1 row, wrong type,
>5 MB, messy headers, duplicates / missing emails."""
import json

import db
from conftest import post, find_card, find_outcome, b64, ADMIN


def walk_import(client, card, key, link="1"):
    d = post(client, "/api/ai/cards/%s/respond" % card["id"],
             {"response": {"id": "import"}, "idempotency_key": key + "1"})
    assert d.json["ok"], d.json
    link_card = find_card(d.json["outcomes"], "options")
    assert link_card and link_card["step_id"] == "link"
    d = post(client, "/api/ai/cards/%s/respond" % link_card["id"],
             {"response": {"id": link}, "idempotency_key": key + "2"})
    assert d.json["ok"], d.json
    gate = find_card(d.json["outcomes"], "confirm")
    assert gate and gate["payload"]["action"] == "import_contacts_sheet"
    d = post(client, "/api/ai/cards/%s/respond" % gate["id"],
             {"response": {"action": "confirm"}, "idempotency_key": key + "3"})
    assert d.json["ok"], d.json
    return d.json


def test_tsv_import_unicode_preserved(client, tenant, mock_llm):
    tsv = ("serial\tCompany\tRecruiter\tEmail\n"
           "1\tM\u00fcller & S\u00f6hne\tDr. Br\u00e4u\u00dfer\tb@mueller.de\n"
           "2\t\u00c9cole Labs\tJos\u00e9e\tj@ecole.fr\n").encode("utf-8")
    d = post(client, "/api/ai/agent", {"file": b64("mueller.tsv", tsv)})
    assert d.json["ok"], d.json
    card = find_card(d.json["outcomes"], "options")
    assert card and card["step_id"] == "confirm"
    walk_import(client, card, "im1")
    c = db.conn()
    rows = {r["serial"]: dict(r) for r in c.execute("SELECT * FROM contacts WHERE sheet_id != 1")}
    c.close()
    assert rows["1"]["company"] == "M\u00fcller & S\u00f6hne", "unicode must survive the import"
    assert rows["1"]["hr_email"] == "b@mueller.de"


def test_one_row_file(client, tenant, mock_llm):
    csv = b"serial,company,hr_name,hr_email\n1,Acme,Alice,alice@acme.com\n"
    d = post(client, "/api/ai/agent", {"file": b64("one.csv", csv)})
    assert d.json["ok"], d.json
    card = find_card(d.json["outcomes"], "options")
    note = json.dumps(d.json["outcomes"])
    assert "First 5 of 1 rows" in note or "1 rows" in note
    walk_import(client, card, "im2", link="0")  # don't link
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM contacts WHERE hr_email='alice@acme.com'").fetchone()["n"]
    c.close()
    assert n == 1


def test_wrong_type_rejected(client, tenant, mock_llm):
    d = post(client, "/api/ai/agent", {"file": b64("doc.pdf", b"%PDF-1.4 fake")})
    assert d.status_code == 400
    assert "Unsupported file type" in d.json["error"]


def test_too_large_rejected(client, tenant, mock_llm):
    big = b"serial,company,hr_name,hr_email\n" + b"1,x,y,z@e.com\n" * (1400 * 1024)  # ~5.5 MB
    d = post(client, "/api/ai/agent", {"file": b64("big.csv", big)})
    assert d.status_code == 400
    assert "File too large" in d.json["error"]


def test_messy_headers_normalised(client, tenant, mock_llm):
    csv = (" Company Name ,HR Email,company, Company \n"
           "Acme,alice@acme.com,x,Acme Inc\n").encode("utf-8")
    d = post(client, "/api/ai/agent", {"file": b64("messy.csv", csv)})
    assert d.json["ok"], d.json
    out = json.dumps(d.json["outcomes"])
    for h in ("company_name", "hr_email", "company_2"):
        assert h in out, "expected normalised header %s in %s" % (h, out)
    card = find_card(d.json["outcomes"], "options")
    walk_import(client, card, "im3", link="0")


def test_dups_and_missing_email_counted(client, tenant, mock_llm):
    csv = b"serial,company,hr_name,hr_email\n1,Acme,A,shared@x.com\n2,Beta,B,shared@x.com\n3,Gamma,C,\n"
    d = post(client, "/api/ai/agent", {"file": b64("dups.csv", csv)})
    assert d.json["ok"], d.json
    out = json.dumps(d.json["outcomes"])
    assert "Without email: 1" in out
    assert "Duplicates: 1" in out
    card = find_card(d.json["outcomes"], "options")
    d = post(client, "/api/ai/cards/%s/respond" % card["id"],
             {"response": {"id": "cancel"}, "idempotency_key": "im4x"})
    assert d.json["ok"]
    c = db.conn()
    n = c.execute("SELECT COUNT(*) n FROM sheets WHERE name LIKE 'dups%'").fetchone()["n"]
    c.close()
    assert n == 0, "declining the import must not create a sheet"
