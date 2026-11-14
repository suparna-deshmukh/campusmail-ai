"""Seed realistic demo data for CampusMail AI (dev only).

Usage:
  python scripts/seed_demo.py            # seed only if the workspace is empty
  python scripts/seed_demo.py --fresh    # back up the DB, wipe team data, reseed
  python scripts/seed_demo.py --fresh --yes   # no interactive confirmation
"""
import argparse
import datetime as dt
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "campusmail_ai.db"
sys.path.insert(0, str(BASE))
from docx_convert import detect_fields  # noqa: E402


def template_fields(subject, body):
    """Field names used by a template (from subject + body placeholders)."""
    f = detect_fields(subject or "")
    for x in detect_fields(body or ""):
        if x.lower() not in {y.lower() for y in f}:
            f.append(x)
    return json.dumps(f)

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
EXTRA = [
    ("Infosys", "प्रिया शर्मा", "priya.s.in@infosys.com"),
    ("TCS", "राहुल पाटिल", "rahul.p@tcs.com"),
    ("Wipro", "अनन्या जोशी", "ananya.j@wipro.com"),
    ("Zoho", "सहिल देशमुख", "sahil.d@zohocorp.com"),
    ("Cognizant", "भारत कुलकर्णी", "bharat.k@cognizant.com"),
]

# 25 more recruiters so the Diwali list reaches 50
DIWALI_EXTRA = [
    ("Persistent Systems", "Kavita Joshi", "kavita.joshi@persistent.com"),
    ("Mphasis", "Ritesh Malhotra", "ritesh.m@mphasis.com"),
    ("HDFC Bank", "Shalini Menon", "shalini.menon@hdfcbank.com"),
    ("ICICI Bank", "Aditya Bhandari", "aditya.b@icicibank.com"),
    ("Reliance Retail", "Meghna Saxena", "meghna.s@relianceretail.in"),
    ("Bajaj Finance", "Harsh Vora", "harsh.vora@bajajfinserv.in"),
    ("Tata Motors", "Pankaj Gokhale", "pankaj.g@tatamotors.com"),
    ("Asian Paints", "Ritu Choudhury", "ritu.choudhury@asianpaints.com"),
    ("Nestle India", "Sameer Kulkarni", "sameer.k@nestle.com"),
    ("Unilever", "Farida Qureshi", "farida.q@unilever.com"),
    ("Hindustan Unilever", "Devang Shah", "devang.shah@hindustanunilever.in"),
    ("Maruti Suzuki", "Lakshmi Pillai", "lakshmi.p@marutisuzuki.com"),
    ("Honda R&D", "Akshat Verma", "akshat.verma@hondardindia.com"),
    ("Siemens", "Nikhil Deshpande", "nikhil.d@siemens.com"),
    ("Hitachi", "Sonal Mehra", "sonal.mehra@hitachi.com"),
    ("Ericsson", "Tanvi Naik", "tanvi.naik@ericsson.com"),
    ("Cisco", "Rahul Bhatia", "rahul.bhatia@cisco.com"),
    ("IBM", "Pooja Nair", "pooja.nair@ibm.com"),
    ("ServiceNow", "Ishaan Kapoor", "ishaan.kapoor@servicenow.com"),
    ("Intuit", "Deepa Iyer", "deepa.iyer@intuit.com"),
    ("Adobe", "Kunal Mehta", "kunal.mehta@adobe.com"),
    ("Uber", "Sanya Kapoor", "sanya.kapoor@uber.com"),
    ("PayPal", "Vivek Jain", "vivek.jain@paypal.com"),
    ("Flipkart", "Neha Bansal", "neha.bansal@flipkart.com"),
    ("Swiggy", "Aakash Jain", "aakash.jain@swiggy.com"),
]


def wipe(c):
    for t in ("emails", "batches", "sheets", "campaigns", "contacts", "email_templates", "activity"):
        c.execute("DELETE FROM " + t)


def seed(c):
    now = ts(NOW)
    # templates — fields are detected from the placeholders in subject + body
    TEMPLATES = [
        ("Sem 2 placement outreach", "outreach", 1, "2026-09-01 10:00:00",
         "Recruiters from BITS Pilani Goa — campus drive in January",
         '<p>Hi {HR Name},</p><p>We would love to invite {Company} to our campus recruitment drive at '
         'BITS Pilani, Goa in January. 464 final-year students across CSE, ECE and ME.</p>'
         '<p>Could we get 30 minutes to share student profiles?</p><p>— Outreach team, BITS Goa</p>'),
        ("Follow-up reminder", "reminder", 0, "2026-09-01 10:05:00",
         "Following up: campus drive in January",
         '<p>Hi {HR Name},</p><p>Just following up on my note about the January campus drive. '
         'Happy to share resumes of 464 students.</p><p>— Outreach team</p>'),
        ("Alumni intro", "outreach", 0, "2026-09-10 10:00:00",
         "Alumni network intro from BITS Goa",
         '<p>Hi {HR Name},</p><p>Quick intro to our alumni network at {Company}.</p>'),
        ("Diwali wishes to recruiters", "outreach", 0, "2026-09-25 10:00:00",
         "Warm Diwali wishes from BITS Pilani, Goa",
         '<p>Hi {HR Name},</p><p>From all of us at the BITS Pilani, Goa Placement Cell — a very Happy Diwali '
         'to you and your team.</p><p>We are now planning our Sem 2 placement drive, and we would be delighted '
         'to have {Company} on our invite list.</p><p>Warm regards,<br>Placement Cell, BITS Pilani Goa</p>'),
        ("Winter internship request", "outreach", 0, "2026-10-20 10:00:00",
         "Winter internships with BITS Pilani Goa — December to February",
         '<p>Hi {HR Name},</p><p>We would like to invite {Company} to consider our final-year students for winter '
         'internships (December to February).</p><p>Could we share the shortlist of students with you?</p>'
         '<p>Warm regards,<br>Placement Cell, BITS Pilani Goa</p>'),
    ]
    for nm, kind, act, created, subj, body in TEMPLATES:
        c.execute("INSERT INTO email_templates(name, type, kind, subject, body_html, fields, is_active, created_at, updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (nm, "html", kind, subj, body, template_fields(subj, body), act, created, now))
    tpl_ids = [r[0] for r in c.execute(
        "SELECT id FROM email_templates ORDER BY id").fetchall()]
    t1, t2, t3, t4, t5 = tpl_ids[-5:]

    # campaigns
    c.execute("INSERT INTO campaigns(name, description, created_by, created_at, updated_at) VALUES(?,?,?,?,?)",
              ("Sem 2 Placement Campaign", "Placement Cell recruiters, core drive", "demo@campus.edu", add(-14), add(0, -3)))
    c1 = c.execute("SELECT last_insert_rowid() id").fetchone()[0]
    c.execute("INSERT INTO campaigns(name, description, created_by, created_at, updated_at) VALUES(?,?,?,?,?)",
              ("Diwali campaign", "Festival greetings to every recruiter on our list", "demo@campus.edu", add(-40), add(-1)))
    cd = c.execute("SELECT last_insert_rowid() id").fetchone()[0]
    c.execute("INSERT INTO campaigns(name, description, created_by, created_at, updated_at) VALUES(?,?,?,?,?)",
              ("Winter internships", "Internship drives, 6 weeks", "demo@campus.edu", add(-20), add(-4)))
    c2 = c.execute("SELECT last_insert_rowid() id").fetchone()[0]
    c.execute("INSERT INTO campaigns(name, description, created_by, created_at, updated_at) VALUES(?,?,?,?,?)",
              ("Alumni network", "", "demo@campus.edu", add(-6), add(-6)))
    c3 = c.execute("SELECT last_insert_rowid() id").fetchone()[0]

    # sheets + contacts (Sem 2: 20 companies; Diwali: 50; Winter: 12)
    rows1 = COMPANIES[:15] + EXTRA
    rowsw = COMPANIES[5:17]
    rowsd = COMPANIES + EXTRA + DIWALI_EXTRA
    contact_ids1, contact_idsw, contact_idsd = [], [], []
    for name, campaign_id, rows, ids_out, created in (
            ("Core drive list", c1, rows1, contact_ids1, add(-10)),
            ("Diwali list", cd, rowsd, contact_idsd, add(-10)),
            ("Internship sheet", c2, rowsw, contact_idsw, add(-15))):
        c.execute("INSERT INTO sheets(name, campaign_id, filename, headers, rows, n_rows, created_by, created_at, updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (name, campaign_id, name + ".csv",
                   json.dumps(["company", "HR name", "HR email"]),
                   json.dumps([list(r) for r in rows]), len(rows), "demo@campus.edu", created, now))
        sid = c.execute("SELECT last_insert_rowid() id").fetchone()[0]
        for i, (co, nm, em) in enumerate(rows, 1):
            c.execute("INSERT INTO contacts(serial, company, hr_name, hr_email, status, sheet_id, created_at, updated_at) "
                      "VALUES(?,?,?,?,?, ?,?,?)",
                      (str(i), co, nm, em, "new", sid, created, now))
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

    b1 = batch("Sem 2 placement wave 1", c1, t1, created=add(-6), sheet=1, reminder_days=3, reminder_tpl=t2, reminder_max=2)
    b2 = batch("Sem 2 placement wave 2", c1, t1, created=add(-3), sheet=1)
    bd = batch("Diwali greetings", cd, t4, created=add(-7), sheet=2)
    b3 = batch("Winter interns batch 1", c2, t5, created=add(-4), sheet=3)
    batch_contacts = {b1: contact_ids1, b2: contact_ids1, bd: contact_idsd, b3: contact_idsw}

    SEM2_SUBJECT = "Recruiters from BITS Pilani Goa — campus drive in January"
    SEM2_BODY = ("<p>Hi %s,</p><p>We are writing to invite %s to our Sem 2 placement drive. 464 final-year students "
                 "across CSE, ECE and ME will be available for pre-placement talks starting this week.</p>"
                 "<p>Would you be open to a short call to schedule a slot? We would be glad to share student "
                 "profiles in the meantime.</p><p>Warm regards,<br>Placement Cell</p>")
    WINTER_SUBJECT = "Winter internships with BITS Pilani Goa — December to February"
    WINTER_BODY = ("<p>Hi %s,</p><p>We would like to invite %s to consider our final-year students for winter "
                   "internships (December to February). Students from CSE, ECE and ME are available for roles in "
                   "software, data and hardware.</p><p>Could we share the shortlist of students with you? We would "
                   "be glad to answer any questions.</p><p>Warm regards,<br>Placement Cell</p>")
    DIWALI_SUBJECT = "Warm Diwali wishes from BITS Pilani, Goa"
    DIWALI_BODY = ("<p>Hi %s,</p><p>From all of us at the BITS Pilani, Goa Placement Cell — a very Happy Diwali to "
                   "you and your team.</p><p>We are now planning our Sem 2 placement drive, and we would be delighted "
                   "to have %s on our invite list. We will share student profiles in the coming weeks.</p>"
                   "<p>Warm regards,<br>Placement Cell, BITS Pilani Goa</p>")

    def mk_email(batch_id, i, co, nm, em, status, subject, body, **kw):
        c.execute("INSERT INTO emails(batch_id, contact_id, serial, to_addr, subject, body_html, status, "
                  "scheduled_at, sent_at, error, account_id, replied_at, bounced_at, bounce_reason, "
                  "reminder_count, is_reminder, created_at, updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (batch_id, kw.get("contact", batch_contacts[batch_id][i - 1]), str(i), em,
                   subject, body % (nm.split()[0], co), status,
                   kw.get("sched", None), kw.get("sent", None), kw.get("error", ""),
                   1, kw.get("replied", None), kw.get("bounced", None), kw.get("bounce_reason", ""),
                   kw.get("reminder_count", 0), 0, kw.get("created", add(-6)), kw.get("updated", now)))
        return c.execute("SELECT last_insert_rowid() id").fetchone()[0]

    # Sem 2: wave 1 saved as drafts, wave 2 ready — nothing sent, nothing scheduled
    for i, (co, nm, em) in enumerate(rows1, 1):
        mk_email(b1, i, co, nm, em, "drafted", SEM2_SUBJECT, SEM2_BODY,
                 created=add(-6), updated=add(0, -3))
    for i, (co, nm, em) in enumerate(rows1, 1):
        mk_email(b2, i, co, nm, em, "pending", SEM2_SUBJECT, SEM2_BODY, created=add(-3), updated=add(-3))

    # Diwali: all 50 sent on 8 Nov, ten replied, two bounced
    DIWALI_REPLIES = {5, 10, 17, 20, 25, 31, 38, 42, 47, 50}
    for i, (co, nm, em) in enumerate(rowsd, 1):
        sent = add(-6, h=3 + (i * 12) // 60, m=(i * 12) % 60)
        if i == 13:
            mk_email(bd, i, co, nm, em, "sent", DIWALI_SUBJECT, DIWALI_BODY, sent=sent,
                     bounced=add(-6, h=5), bounce_reason="550 5.1.1 The email account that you tried to reach does not exist",
                     created=add(-7), updated=sent)
        elif i == 29:
            mk_email(bd, i, co, nm, em, "sent", DIWALI_SUBJECT, DIWALI_BODY, sent=sent,
                     bounced=add(-6, h=6), bounce_reason="552 5.2.2 The mailbox of the recipient is full",
                     created=add(-7), updated=sent)
        elif i in DIWALI_REPLIES:
            mk_email(bd, i, co, nm, em, "sent", DIWALI_SUBJECT, DIWALI_BODY, sent=sent,
                     replied=add(-(5 - (i % 3)), h=2 + (i % 5), m=i % 60),
                     created=add(-7), updated=add(-(5 - (i % 3))))
        else:
            mk_email(bd, i, co, nm, em, "sent", DIWALI_SUBJECT, DIWALI_BODY, sent=sent,
                     created=add(-7), updated=sent)
    for cid in contact_idsd:
        c.execute("UPDATE contacts SET status='sent' WHERE id=?", (cid,))

    # Winter internships: ready to go, nothing sent
    for i, (co, nm, em) in enumerate(rowsw, 1):
        mk_email(b3, i, co, nm, em, "pending", WINTER_SUBJECT, WINTER_BODY, created=add(-4), updated=add(-4))

    # mark Sem 2 wave-1 contacts as drafted
    for cid in contact_ids1:
        c.execute("UPDATE contacts SET status='drafted' WHERE id=?", (cid,))

    # activity
    acts = [
        (add(0, -3), "Priya", "email_draft", "20 emails saved as drafts in Sem 2 placement wave 1"),
        (add(-1), "Arjun", "batch_create", "Created send Sem 2 placement wave 2"),
        (add(-2, 4), "System", "reply_detected", "Reply detected from TCS"),
        (add(-3), "Priya", "email_reply", "Replied to Vikram Shastri (Tata Consultancy Services)"),
        (add(-4), "Arjun", "batch_create", "Created send Winter interns batch 1"),
        (add(-5), "Priya", "contact_import", "Imported 20 contacts from Core drive list"),
        (add(-6, 2), "System", "send", "50 emails went out in Diwali greetings"),
        (add(-7), "Arjun", "batch_create", "Created send Diwali greetings"),
        (add(-10), "Priya", "contact_import", "Imported 50 contacts from Diwali list"),
        (add(-14), "Priya", "campaign_create", "Created campaign Sem 2 Placement Campaign"),
        (add(-40), "Priya", "campaign_create", "Created campaign Diwali campaign"),
    ]
    for a in acts:
        c.execute("INSERT INTO activity(ts, actor, action, detail) VALUES(?,?,?,?)", a)

    # bump campaign updated_at so ordering is sensible
    c.execute("UPDATE campaigns SET updated_at=? WHERE id=?", (add(0, -3), c1))
    c.execute("UPDATE campaigns SET updated_at=? WHERE id=?", (add(-1), cd))
    c.execute("UPDATE campaigns SET updated_at=? WHERE id=?", (add(-4), c2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="back up and wipe team data, then seed")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation with --fresh")
    ap.add_argument("--db", default=None, help="path to the database (default: data/campusmail_ai.db)")
    args = ap.parse_args()

    db_path = Path(args.db).expanduser().resolve() if args.db else DB
    if not db_path.exists():
        print("No database at", db_path, "— start the app once first.")
        sys.exit(1)

    import db as dbmod
    dbmod.set_tenant_db(db_path)
    dbmod.init_db(seed=False)  # schema + migrations, so the seed always matches the app

    c = sqlite3.connect(str(db_path), timeout=15)
    has_data = c.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] > 0

    if not args.fresh:
        if has_data:
            print("Workspace already has data (campaigns:",
                  c.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0], ") — not seeding.")
            print("Use --fresh to back up the DB and reseed.")
            sys.exit(0)
    else:
        if has_data and not args.yes:
            ans = input("This backs up and wipes team data in %s. Continue? [y/N] " % db_path)
            if ans.strip().lower() not in ("y", "yes"):
                print("Aborted.")
                sys.exit(0)
        backup = db_path.with_suffix(".db.bak-" + NOW.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(db_path, backup)
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
