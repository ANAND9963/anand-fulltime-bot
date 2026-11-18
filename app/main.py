"""FastAPI app: REST API + static UI. Run with:  python run.py   then open http://127.0.0.1:8765"""
import json
import shutil
import threading
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import answers, db, email_reader, resume, search
from .browser import get_worker

app = FastAPI(title="Job Bot")
STATIC = db.ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
SECRETS = {"anthropic_api_key", "imap_password", "ats_account_password", "brave_api_key", "serper_api_key"}
_search_running = {}

if not db.q("SELECT 1 FROM profiles"):
    db.x("INSERT INTO profiles (name, data, created_at) VALUES (?,?,?)", ("Me", "{}", db.now()))
# jobs left mid-flight by a previous run
db.x("UPDATE jobs SET status='new' WHERE status IN ('queued','applying')")


def _profile(pid):
    p = db.get_profile(pid)
    if not p:
        raise HTTPException(404, "profile not found")
    return p


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# ---------------- profiles / resume ----------------
@app.get("/api/profiles")
def profiles():
    return db.q("SELECT id, name, resume_path FROM profiles ORDER BY id")


@app.post("/api/profiles")
def create_profile(body: dict = Body(...)):
    pid = db.x("INSERT INTO profiles (name, data, created_at) VALUES (?,?,?)", (body.get("name") or "New user", "{}", db.now()))
    return {"id": pid}


@app.delete("/api/profiles/{pid}")
def delete_profile(pid: int):
    for t in ("answers", "avoid_companies", "jobs", "pending_questions", "email_events", "settings"):
        db.x(f"DELETE FROM {t} WHERE profile_id=?", (pid,))
    db.x("DELETE FROM profiles WHERE id=?", (pid,))
    return {"ok": True}


@app.get("/api/profile/{pid}")
def get_profile(pid: int):
    p = _profile(pid)
    return {"id": p["id"], "name": p["name"], "data": p["data"], "resume_path": p["resume_path"],
            "resume_chars": len(p["resume_text"] or ""), "fields": resume.PROFILE_FIELDS}


@app.put("/api/profile/{pid}")
def put_profile(pid: int, body: dict = Body(...)):
    _profile(pid)
    if "name" in body:
        db.x("UPDATE profiles SET name=? WHERE id=?", (body["name"], pid))
    if "data" in body:
        db.save_profile_data(pid, {k: v for k, v in body["data"].items() if v not in (None,)})
    return {"ok": True}


RESUME_FIELDS = {"first_name", "last_name", "full_name", "email", "phone", "linkedin", "github", "website",
                 "current_title", "current_company", "years_experience", "school", "degree", "major",
                 "graduation_year", "skills", "target_titles", "summary"}


@app.post("/api/profile/{pid}/resume")
async def upload_resume(pid: int, file: UploadFile = File(...)):
    p = _profile(pid)
    d = db.DATA / "resumes" / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    safe = Path(file.filename).name
    dest = d / safe
    with open(dest, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    text = resume.extract_text(str(dest))
    parsed = resume.parse_profile(text, db.get_settings(pid))
    data = p["data"]
    for k, v in parsed.items():           # resume facts refresh; your own answers (visa, EEO, salary) are kept
        if k in RESUME_FIELDS or not data.get(k):
            data[k] = v
    db.x("UPDATE profiles SET resume_path=?, resume_text=? WHERE id=?", (str(dest), text, pid))
    db.save_profile_data(pid, data)
    db.log(pid, f"Resume uploaded: {safe} ({len(text)} chars), {len(parsed)} fields parsed")
    return {"ok": True, "parsed": parsed}


# ---------------- settings ----------------
@app.get("/api/settings/{pid}")
def get_settings(pid: int):
    s = db.get_settings(pid)
    return {k: ("__SET__" if k in SECRETS and v else v) for k, v in s.items() if k != "learned_boards"}


@app.put("/api/settings/{pid}")
def put_settings(pid: int, body: dict = Body(...)):
    db.set_settings(pid, {k: v for k, v in body.items() if v != "__SET__" and k in db.DEFAULT_SETTINGS})
    return {"ok": True}


# ---------------- answers / pending ----------------
@app.get("/api/answers/{pid}")
def get_answers(pid: int):
    return db.q("SELECT * FROM answers WHERE profile_id=? ORDER BY updated_at DESC", (pid,))


@app.post("/api/answers/{pid}")
def add_answer(pid: int, body: dict = Body(...)):
    if body.get("id"):
        db.x("UPDATE answers SET answer=?, source='user', updated_at=? WHERE id=? AND profile_id=?",
             (body["answer"], db.now(), body["id"], pid))
    else:
        answers.save(pid, body["question"], body["answer"])
    return {"ok": True}


@app.delete("/api/answers/{pid}/{aid}")
def del_answer(pid: int, aid: int):
    db.x("DELETE FROM answers WHERE id=? AND profile_id=?", (aid, pid))
    return {"ok": True}


@app.get("/api/pending/{pid}")
def get_pending(pid: int):
    rows = db.q("SELECT p.*, j.title, j.company, j.url FROM pending_questions p LEFT JOIN jobs j ON j.id=p.job_id "
                "WHERE p.profile_id=? ORDER BY p.id", (pid,))
    for r in rows:
        r["options"] = json.loads(r["options"] or "[]")
    return rows


@app.post("/api/pending/{pid}")
def answer_pending(pid: int, body: dict = Body(...)):
    for item in body.get("answers", []):
        if str(item.get("answer", "")).strip():
            answers.save(pid, item["question"], item["answer"].strip())
    requeued = 0
    if not db.q("SELECT 1 FROM pending_questions WHERE profile_id=?", (pid,)) or body.get("retry"):
        requeued = _queue(pid, [r["id"] for r in db.q("SELECT id FROM jobs WHERE profile_id=? AND status='needs_input'", (pid,))])
    return {"ok": True, "requeued": requeued}


@app.delete("/api/pending/{pid}/{qid}")
def del_pending(pid: int, qid: int):
    db.x("DELETE FROM pending_questions WHERE id=? AND profile_id=?", (qid, pid))
    return {"ok": True}


# ---------------- avoid list ----------------
@app.get("/api/avoid/{pid}")
def get_avoid(pid: int):
    return [r["company"] for r in db.q("SELECT company FROM avoid_companies WHERE profile_id=? ORDER BY company", (pid,))]


@app.post("/api/avoid/{pid}")
def add_avoid(pid: int, body: dict = Body(...)):
    for c in [c.strip() for c in body.get("companies", "").replace("\n", ",").split(",") if c.strip()]:
        db.x("INSERT OR IGNORE INTO avoid_companies (profile_id, company) VALUES (?,?)", (pid, c))
    # retroactively skip queued/new jobs from these companies
    for j in db.q("SELECT id, company FROM jobs WHERE profile_id=? AND status IN ('new','queued','needs_input')", (pid,)):
        if search.is_avoided(pid, j["company"]):
            db.x("UPDATE jobs SET status='skipped', status_note='company is on avoid list' WHERE id=?", (j["id"],))
    return {"ok": True}


@app.delete("/api/avoid/{pid}")
def del_avoid(pid: int, company: str):
    db.x("DELETE FROM avoid_companies WHERE profile_id=? AND company=?", (pid, company))
    return {"ok": True}


# ---------------- jobs ----------------
@app.get("/api/jobs/{pid}")
def jobs(pid: int, status: str = "", q: str = "", limit: int = 500):
    sql = ("SELECT id, url, apply_url, title, company, location, source, ats, posted_at, sponsorship, score, status,"
           " status_note, found_at, applied_at FROM jobs WHERE profile_id=?")
    args = [pid]
    if status:
        sql += " AND status IN (%s)" % ",".join("?" * len(status.split(",")))
        args += status.split(",")
    if q:
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"
        args += [f"%{q}%"] * 3
    sql += " ORDER BY CASE status WHEN 'needs_human' THEN 0 WHEN 'applying' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END," \
           " score DESC, found_at DESC LIMIT ?"
    args.append(limit)
    return db.q(sql, args)


@app.get("/api/stats/{pid}")
def stats(pid: int):
    rows = db.q("SELECT status, COUNT(*) n FROM jobs WHERE profile_id=? GROUP BY status", (pid,))
    out = {r["status"]: r["n"] for r in rows}
    out["pending_questions"] = db.q("SELECT COUNT(*) n FROM pending_questions WHERE profile_id=?", (pid,), one=True)["n"]
    out["emails"] = db.q("SELECT kind, COUNT(*) n FROM email_events WHERE profile_id=? GROUP BY kind", (pid,))
    return out


def _queue(pid, ids):
    w = get_worker()
    n = 0
    for i in ids:
        j = db.q("SELECT status FROM jobs WHERE id=? AND profile_id=?", (i, pid), one=True)
        if j and j["status"] not in ("applied", "queued", "applying"):
            db.x("UPDATE jobs SET status='queued', status_note=NULL WHERE id=?", (i,))
            w.enqueue("apply", pid, i)
            n += 1
    return n


@app.post("/api/jobs/{pid}/apply")
def apply_jobs(pid: int, body: dict = Body(...)):
    if not _profile(pid)["resume_path"]:
        raise HTTPException(400, "Upload a resume first")
    ids = body.get("ids") or []
    if body.get("all_new"):
        ms = float(body.get("min_score", db.get_settings(pid).get("min_score") or 0))
        ids = [r["id"] for r in db.q("SELECT id FROM jobs WHERE profile_id=? AND status='new' AND score>=? "
                                     "ORDER BY score DESC LIMIT ?", (pid, ms, int(body.get("limit", 50))))]
    return {"queued": _queue(pid, ids)}


@app.post("/api/jobs/{pid}/status")
def set_status(pid: int, body: dict = Body(...)):
    for i in body.get("ids", []):
        db.x("UPDATE jobs SET status=?, status_note=?, applied_at=CASE WHEN ?='applied' THEN ? ELSE applied_at END "
             "WHERE id=? AND profile_id=?", (body["status"], "set by you", body["status"], db.now(), i, pid))
    return {"ok": True}


@app.post("/api/jobs/{pid}/add")
def add_job(pid: int, body: dict = Body(...)):
    url = body["url"].strip()
    ats = search.detect_ats(url)
    j = dict(url=url, apply_url=url, title=body.get("title") or "(added manually)", company=body.get("company") or "",
             ats=ats)
    profile = _profile(pid)["data"]
    src = "listing" if ats in ("jobright", "linkedin", "other") else "manual"
    search.save_job(pid, j, src, [], [], [], None, False, profile, force=True)
    row = db.q("SELECT id FROM jobs WHERE profile_id=? AND url=?", (pid, url), one=True)
    if body.get("apply_now") and row:
        _queue(pid, [row["id"]])
    return {"ok": True, "id": row["id"] if row else None}


@app.delete("/api/jobs/{pid}")
def clear_jobs(pid: int, status: str = "new,skipped"):
    st = status.split(",")
    db.x(f"DELETE FROM jobs WHERE profile_id=? AND status IN ({','.join('?' * len(st))})", [pid] + st)
    return {"ok": True}


# ---------------- search ----------------
@app.post("/api/search/{pid}")
def run_search(pid: int, body: dict = Body(default={})):
    sources = body.get("sources") or ["ats", "web", "listings"]
    if "listings" in sources:
        for u in [u.strip() for u in db.get_settings(pid).get("listing_urls", "").splitlines() + body.get("extra_urls", [])
                  if u.strip()]:
            get_worker().enqueue("listing", pid, u)
    if ("ats" in sources or "web" in sources) and not _search_running.get(pid):
        def go():
            _search_running[pid] = True
            try:
                search.run_search(pid, sources, progress=lambda m: _search_running.__setitem__(pid, m))
            except Exception as e:
                db.log(pid, f"search failed: {e}", "error")
            finally:
                _search_running[pid] = False
        threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


# ---------------- email ----------------
@app.post("/api/email/{pid}/scan")
def scan_email(pid: int, body: dict = Body(default={})):
    def go():
        try:
            email_reader.scan_mailbox(pid, int(body.get("days", 14)))
        except Exception as e:
            db.log(pid, f"mail scan failed: {e}", "error")
    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


@app.get("/api/emails/{pid}")
def emails(pid: int):
    return db.q("SELECT e.*, j.title FROM email_events e LEFT JOIN jobs j ON j.id=e.job_id WHERE e.profile_id=? "
                "ORDER BY received_at DESC LIMIT 500", (pid,))


# ---------------- worker / browser ----------------
@app.get("/api/status/{pid}")
def status(pid: int):
    w = get_worker()
    return {"worker": w.state, "queue": w.queue_size(), "search": _search_running.get(pid) or False,
            "logs": db.q("SELECT ts, level, msg FROM logs WHERE profile_id IN (0, ?) ORDER BY id DESC LIMIT 80", (pid,))}


@app.post("/api/worker/human")
def human(body: dict = Body(...)):
    get_worker().human_reply(body.get("result", "continue"))
    return {"ok": True}


@app.post("/api/worker/stop")
def stop():
    get_worker().stop()
    return {"ok": True}


@app.post("/api/browser/{pid}/open")
def open_browser(pid: int, body: dict = Body(default={})):
    get_worker().enqueue("open", pid, body.get("url") or "https://jobright.ai/jobs/recommend?from=homepage")
    return {"ok": True}
