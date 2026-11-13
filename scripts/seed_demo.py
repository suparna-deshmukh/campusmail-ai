"""Seed realistic demo data for CampusMail AI (dev only).

Usage:
  python scripts/seed_demo.py            # seed only if the workspace is empty
  python scripts/seed_demo.py --fresh    # back up the DB, wipe team data, reseed
  python scripts/seed_demo.py --fresh --yes   # no interactive confirmation
"""
import argparse
import datetime as dt
import json
import shutil
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "campusmail_ai.db"

NOW = dt.datetime.now()


def ts(d):
    return d.strftime("%Y-%m-%d %H:%M:%S")


def add(days=0, h=0, m=0):
    d = NOW + dt.timedelta(days=days, hours=h, minutes=m)
    return ts(d)


COMPANIES = [
    ("Infosys", "Ananya Rao", "ananya.rao@infosys.com"),
    ("Tata Consultancy Services", "Vikram Shastri", "vikram.s@tcs.com"),
    ("Wipro", "Meera Kulkarni", "meera.kulkarni@wipro.com"),
    ("Zoho Corporation", "Rahul Pillai", "rahul.pillai@zohocorp.com"),
    ("Cognizant", "Priya Sharma", "priya.sharma@cognizant.com"),
    ("Accenture", "Sneha Patil", "sneha.patil@accenture.com"),
    ("HCL Technologies", "Arjun Mehta", "arjun.mehta@hcltech.com"),
    ("Tech Mahindra", "Kavya Iyer", "kavya.iyer@techmahindra.com"),
    ("Capgemini", "Rohan Deshpande", "rohan.d@capgemini.com"),
    ("L&T", "Aditya Kulkarni", "aditya.kulkarni@lntech.com"),
    ("Bosch", "Farhan Qureshi", "farhan.q@india.bosch.com"),
    ("Deloitte", "Ishita Bhatt", "ishita.bhatt@deloitte.com"),
    ("EY", "Siddharth Nair", "siddharth.n@ey.com"),
    ("KPMG", "Ananya Deshmukh", "ananya.d@kpmg.com"),
    ("PwC", "Kunal Joshi", "kunal.joshi@pwc.com"),
    ("Amazon", "Divya Menon", "divya.menon@amazon.com"),
    ("Microsoft", "Aarav Gupta", "aarav.gupta@microsoft.com"),
    ("Google India", "Nisha Reddy", "nisha.reddy@google.com"),
    ("Salesforce", "Varun Khanna", "varun.k@salesforce.com"),
    ("Oracle", "Pooja Hegde", "pooja.hegde@oracle.com"),
]

# a few rows with Indic names so layouts are tested (4.9 / 14.3)
EXTRA = [
    ("Infosys", "प्रिया शर्मा", "priya.s.in@infosys.com"),
    ("TCS", "राहुल पाटिल", "rahul.p@tcs.com"),
    ("Wipro", "अनन्या जोशी", "ananya.j@wipro.com"),
    ("Zoho", "सहिल देशमुख", "sahil.d@zohocorp.com"),
    ("Cognizant", "भारत कुलकर्णी", "bharat.k@cognizant.com"),
]


def wipe(c):
    for t in ("emails", "batches", "sheets", "campaigns", "contacts", "email_templates", "activity"):
        c.execute("DELETE FROM " + t)


def seed(c):
    now = ts(NOW)
    # templates
    c.execute("INSERT INTO email_templates(name, type, subject, body_html, fields, is_active, created_at, updated_at) "
              "VALUES(?,?,?,?,1,1,?,?)",
              ("Sem 2 placement outreach", "outreach",
               "Recruiters from BITS Pilani Goa — campus drive in January",
               '<p>Hi {HR Name},</p><p>We would love to invite {Company} to our campus recruitment drive at '
               'BITS Pilani, Goa in January. 464 final-year students across CSE, ECE and ME.</p>'
               '<p>Could we get 30 minutes to share student profiles?</p><p>— Outreach team, BITS Goa</p>',
               '2026-09-01 10:00:00', now))
    t1 = c.execute("SELECT last_insert_rowid() id").fetchone()[0]
    c.execute("INSERT INTO email_templates(name, type, subject, body_html, fields, is_active, created_at, updated_at) "
              "VALUES(?,?,?,?,0,0,?,?)",
              ("Follow-up reminder", "reminder",
               "Following up: campus drive in January",
               '<p>Hi {HR Name},</p><p>Just following up on my note about the January campus drive. '
               'Happy to share resumes of 464 students.</p><p>— Outreach team</p>',
               '2026-09-01 10:05:00', now))
    t2 = c.execute("SELECT last_insert_rowid() id").fetchone()[0]
    c.execute("INSERT INTO email_templates(name, type, subject, body_html, fields, is_active, created_at, updated_at) "
              "VALUES(?,?,?,?,0,0,?,?)",
              ("Alumni intro", "outreach",
               "Alumni network intro from BITS Goa",
               '<p>Hi {HR Name},</p><p>Quick intro to our alumni network at {Company}.</p>',
               '2026-09-10 10:00:00', now))
    t3 = c.execute("SELECT last_insert_rowid() id").fetchone()[0]

    # campaigns
    c.execute("INSERT INTO campaigns(name, description, created_by, created_at, updated_at) VALUES(?,?,?,?,?)",
              ("Sem 2 Placement Campaign", "Placement Cell recruiters, core drive", "demo@campus.edu", add(-40), add(-1)))
    c1 = c.execute("SELECT last_insert_rowid() id").fetchone()[0]
    c.execute("INSERT INTO campaigns(name, description, created_by, created_at, updated_at) VALUES(?,?,?,?,?)",
              ("Winter internships", "Internship drives, 6 weeks", "demo@campus.edu", add(-20), add(-3)))
    c2 = c.execute("SELECT last_insert_rowid() id").fetchone()[0]
    c.execute("INSERT INTO campaigns(name, description, created_by, created_at, updated_at) VALUES(?,?,?,?,?)",
              ("Alumni network", "", "demo@campus.edu", add(-6), add(-6)))
    c3 = c.execute("SELECT last_insert_rowid() id").fetchone()[0]

    # sheets + contacts (campaign 1: 20 companies; campaign 2: 12)
    rows1 = COMPANIES[:15] + EXTRA
    rows2 = COMPANIES[5:17]
    contact_ids1, contact_ids2 = [], []
    for name, campaign_id, rows, ids_out in (("Core drive list", c1, rows1, contact_ids1), ("Internship sheet", c2, rows2, contact_ids2)):
        c.execute("INSERT INTO sheets(name, campaign_id, filename, headers, rows, n_rows, created_by, created_at, updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (name, campaign_id, name + ".csv",
                   json.dumps(["company", "HR name", "HR email"]),
                   json.dumps([list(r) for r in rows]), len(rows), "demo@campus.edu", add(-30), now))
        sid = c.execute("SELECT last_insert_rowid() id").fetchone()[0]
        for i, (co, nm, em) in enumerate(rows, 1):
            c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, sheet_id, created_at, updated_at) "
                      "VALUES(?,?,?,?,?, ?,?,?)",
                      (str(i), co, nm, em, "new", sid, add(-30), now))
            ids_out.append(c.execute("SELECT last_insert_rowid() id").fetchone()[0])

    mapping = json.dumps({"HR Name": "HR name", "Company": "company", "HR email": "HR email"})

    # batches
    def batch(name, campaign, tpl, **kw):
        c.execute("INSERT INTO batches(name, template_id, mapping, account_id, campaign_id, sheet_id, "
                  "reminder_days, reminder_template_id, reminder_max, append_signature, created_at, created_by, updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (name, tpl, mapping, kw.get("account", 1), campaign, kw.get("sheet", 0),
                   kw.get("reminder_days", 0), kw.get("reminder_tpl", 0), kw.get("reminder_max", 1),
                   1, kw.get("created", add(-14)), "demo@campus.edu", now))
        return c.execute("SELECT last_insert_rowid() id").fetchone()[0]

    b1 = batch("Sem 2 placement wave 1", c1, t1, created=add(-25), sheet=1, reminder_days=3, reminder_tpl=t2, reminder_max=2)
    b2 = batch("Sem 2 placement wave 2", c1, t1, created=add(-10), sheet=1)
    b3 = batch("Winter interns batch 1", c2, t1, created=add(-5), sheet=2)
    batch_contacts = {b1: contact_ids1, b2: contact_ids1, b3: contact_ids2}

    # emails: wave 1 fully sent with replies/bounces/failures, wave 2 mixed, batch 3 scheduled
    b1_contacts = list(rows1)
    b2_contacts = list(rows1)
    b3_contacts = list(rows2)

    def mk_email(batch_id, i, co, nm, em, status, **kw):
        c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, status, "
                  "scheduled_at, sent_at, error, account_id, replied_at, bounced_at, bounce_reason, "
                  "reminder_count, is_reminder, created_at, updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (batch_id, kw.get("contact", batch_contacts[batch_id][i - 1]), str(i), em,
                   "Recruiters from BITS Pilani Goa — campus drive in January",
                   "<p>Hi " + nm.split()[0] + ",</p><p>We are writing to invite " + co + " to our Sem 2 placement drive. 464 final-year students across CSE, ECE and ME will be available for pre-placement talks starting this week.</p><p>Would you be open to a short call to schedule a slot? We would be glad to share student profiles in the meantime.</p><p>Warm regards,<br>Placement Cell</p>", status,
                   kw.get("sched", None), kw.get("sent", None), kw.get("error", ""),
                   1, kw.get("replied", None), kw.get("bounced", None), kw.get("bounce_reason", ""),
                   kw.get("reminder_count", 0), 0, kw.get("created", add(-25)), now))
        return c.execute("SELECT last_insert_rowid() id").fetchone()[0]

    for i, (co, nm, em) in enumerate(b1_contacts, 1):
        if i <= 12:   # sent, some replied
            replied = add(-(20 - i), 10, 0) if i % 3 == 0 else None
            mk_email(b1, i, co, nm, em, "sent", sent=add(-(25 - 0), 9, 0), replied=replied)
        elif i <= 14:  # bounced
            mk_email(b1, i, co, nm, em, "sent", sent=add(-24, 9, 0), bounced=add(-24, 11, 0),
                     bounce_reason="550 5.1.1 The email account that you tried to reach does not exist")
        elif i == 15:  # failed
            mk_email(b1, i, co, nm, em, "error", error="Gmail: daily quota exceeded, retry later", created=add(-24))
        else:          # the rest drafted (never sent)
            mk_email(b1, i, co, nm, em, "drafted")

    for i, (co, nm, em) in enumerate(b2_contacts, 1):
        if i <= 5:    # scheduled within 24h
            mk_email(b2, i, co, nm, em, "scheduled", sched=add(0, 8 + i, 0))
        elif i <= 8:  # scheduled later
            mk_email(b2, i, co, nm, em, "scheduled", sched=add(3, 10, 0))
        elif i <= 12: # pending
            mk_email(b2, i, co, nm, em, "pending")
        else:
            mk_email(b2, i, co, nm, em, "cancelled")

    for i, (co, nm, em) in enumerate(b3_contacts, 1):
        if i <= 4:
            mk_email(b3, i, co, nm, em, "scheduled", sched=add(1, 9, 30), created=add(-5))
        else:
            mk_email(b3, i, co, nm, em, "pending", created=add(-5))

    # activity
    acts = [
        (add(0, -2), "Priya", "email_draft", "12 emails saved as drafts in Diwali outreach wave 2"),
        (add(-1), "Arjun", "email_schedule", "8 emails scheduled for tomorrow 09:30 in Winter interns batch 1"),
        (add(-1, 3), "Priya", "email_reply", "Replied to Ananya Rao (Infosys)"),
        (add(-2), "System", "reply_detected", "Reply detected from TCS"),
        (add(-3), "Arjun", "contact_import", "Imported 15 contacts from Core drive list"),
        (add(-5), "Priya", "batch_create", "Created send Winter interns batch 1"),
        (add(-10), "Arjun", "batch_create", "Created send Diwali outreach wave 2"),
        (add(-25), "Priya", "batch_create", "Created send Diwali outreach wave 1"),
    ]
    for a in acts:
        c.execute("INSERT INTO activity(ts, actor, action, detail) VALUES(?,?,?,?)", a)

    # bump campaign updated_at so ordering is sensible
    c.execute("UPDATE campaigns SET updated_at=? WHERE id=?", (add(-1), c1))
    c.execute("UPDATE campaigns SET updated_at=? WHERE id=?", (add(-3), c2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="back up and wipe team data, then seed")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation with --fresh")
    args = ap.parse_args()

    if not DB.exists():
        print("No database at", DB, "— start the app once first.")
        sys.exit(1)

    c = sqlite3.connect(str(DB), timeout=15)
    has_data = c.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] > 0

    if not args.fresh:
        if has_data:
            print("Workspace already has data (campaigns:",
                  c.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0], ") — not seeding.")
            print("Use --fresh to back up the DB and reseed.")
            sys.exit(0)
    else:
        if has_data and not args.yes:
            ans = input("This backs up and wipes team data in %s. Continue? [y/N] " % DB)
            if ans.strip().lower() not in ("y", "yes"):
                print("Aborted.")
                sys.exit(0)
        backup = DB.with_suffix(".db.bak-" + NOW.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(DB, backup)
        print("Backed up to", backup.name)
        wipe(c)

    seed(c)
    c.commit()
    print("Seeded demo data:",
          c.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0], "campaigns,",
          c.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], "contacts,",
          c.execute("SELECT COUNT(*) FROM emails").fetchone()[0], "emails.")
    c.close()


if __name__ == "__main__":
    main()
