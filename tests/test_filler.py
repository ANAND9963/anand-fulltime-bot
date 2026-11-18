"""Offline test: fills tests/test_form.html with the real Filler + submit logic (no network, no real application)."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright
from app import db, apply, answers

PID = db.x("INSERT INTO profiles (name, data, created_at) VALUES ('__test__', '{}', ?)", (db.now(),))
db.save_profile_data(PID, {"first_name": "Test", "last_name": "User", "email": "t@example.com", "phone": "555-111-2222",
                           "linkedin": "https://linkedin.com/in/test", "needs_sponsorship": "Yes",
                           "work_authorization_answer": "Yes", "referral_source": "LinkedIn"})
res = str(pathlib.Path(__file__).with_name("test_form.html"))
db.x("UPDATE profiles SET resume_path=?, resume_text='resume' WHERE id=?", (res, PID))
db.set_settings(PID, {"auto_submit": "true", "anthropic_api_key": "-"})
db.x("INSERT INTO jobs (profile_id, url, title, company, status) VALUES (?,?,?,?, 'new')", (PID, "file://" + res, "SE", "Acme"))
job = db.q("SELECT * FROM jobs WHERE profile_id=?", (PID,), one=True)

class FakeWorker:
    def wait_human(self, ctx, msg, timeout=0): raise apply.NeedsHuman(msg)

def run():
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.goto(pathlib.Path(res).as_uri())
        ctx = apply.ApplyContext(PID, job, FakeWorker())
        ctx.settings["anthropic_api_key"] = ""
        try:
            return apply.generic_form(ctx, page), page
        except apply.NeedsInput as e:
            print("NeedsInput:", e); return None, None
        finally:
            if page: print("PAGE:", page.inner_text("body")[:600])
            b.close()

try:
    r, _ = run()
    pend = db.q("SELECT question FROM pending_questions WHERE profile_id=?", (PID,))
    print("pending:", pend)
    assert r is None and any("paradigm" in p["question"] for p in pend), "expected the unknown question to be pending"
    answers.save(PID, pend[0]["question"], "Functional")
    r, _ = run()
    print("second run result:", r)
    assert r is True
    print("saved answers:", [(a["question"], a["answer"]) for a in db.q("SELECT * FROM answers WHERE profile_id=?", (PID,))])
    print("ALL OK")
finally:
    for t in ("answers", "jobs", "pending_questions", "settings", "logs"):
        db.x(f"DELETE FROM {t} WHERE profile_id=?", (PID,))
    db.x("DELETE FROM profiles WHERE id=?", (PID,))
