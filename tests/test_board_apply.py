"""Offline test of the job-board quick-apply driver on a local LinkedIn-Easy-Apply-like page."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright
from app import apply, answers, db, sites

page_file = pathlib.Path(__file__).with_name("test_easy_apply.html")
PID = db.x("INSERT INTO profiles (name, data, created_at) VALUES ('__t__','{}',?)", (db.now(),))
db.save_profile_data(PID, {"first_name": "T", "last_name": "U", "email": "t@example.com", "phone": "555-111-2222"})
db.x("UPDATE profiles SET resume_path=?, resume_text='x' WHERE id=?", (str(page_file), PID))
db.set_settings(PID, {"auto_submit": "true", "anthropic_api_key": "", "max_per_site_per_day": "25"})
jid = db.x("INSERT INTO jobs (profile_id,url,title,company,status,source) VALUES (?,?,?,?,'new','listing')",
           (PID, page_file.as_uri(), "Java Developer", "Acme"))
job = db.q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
sites.site_of = lambda u: "linkedin"              # pretend the local page is LinkedIn

class W: _stop = False
def wait_human(*a, **k): raise apply.NeedsHuman(a[-1] if a else "")
W.wait_human = staticmethod(wait_human)

def run():
    with sync_playwright() as pw:
        b = pw.chromium.launch(); p = b.new_page(); p.goto(page_file.as_uri())
        try:
            ok, page = apply.board_apply(apply.ApplyContext(PID, job, W()), p, "linkedin")
            return ok, page.evaluate("window.__result || null")
        except apply.NeedsInput as e:
            return f"NeedsInput: {e}", None
        finally:
            b.close()
try:
    r1 = run(); print("run 1:", r1)
    pend = db.q("SELECT question FROM pending_questions WHERE profile_id=?", (PID,))
    assert isinstance(r1[0], str) and r1[0].startswith("NeedsInput") and any("Java" in p["question"] for p in pend), pend
    answers.save(PID, pend[0]["question"], "5")
    r2 = run(); print("run 2:", r2)
    ok, res = r2
    assert ok is True and res["ph"] == "555-111-2222" and res["em"] == "t@example.com" and res["yrs"] == "5"
    assert res["cv"] == "test_easy_apply.html", "resume should be uploaded"
    assert res["search"] == "", "must not type into the site's search box behind the popup"
    print("ALL OK")
finally:
    for t in ("answers", "jobs", "pending_questions", "settings", "logs"):
        db.x(f"DELETE FROM {t} WHERE profile_id=?", (PID,))
    db.x("DELETE FROM profiles WHERE id=?", (PID,))
