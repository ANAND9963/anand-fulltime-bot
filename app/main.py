"""FastAPI app: REST API + static UI. Run with:  python run.py   then open http://127.0.0.1:8765"""
import json
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import answers, chrome, db, email_reader, relay, resume, search, sites
from .browser import get_worker

app = FastAPI(title="Job Bot")
STATIC = db.ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_cache_ui(request, call_next):
    """Always revalidate the UI files so a code update shows up without a hard refresh."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
SECRETS = {"anthropic_api_key", "imap_password", "ats_account_password", "brave_api_key", "serper_api_key",
           "online_password"}
_search_running = {}
_search_cancel = set()


class SearchCancelled(Exception):
    pass

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
    stamp = datetime.now()
    d = db.DATA / "resumes" / str(pid) / stamp.strftime("%Y-%m-%d")      # one folder per upload date
    d.mkdir(parents=True, exist_ok=True)
    safe = Path(file.filename).name
    dest = d / safe
    if dest.exists():                                                   # same file name again that day
        dest = d / f"{dest.stem}_{stamp.strftime('%H%M%S')}{dest.suffix}"
    with open(dest, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    text = resume.extract_text(str(dest))
    parsed = resume.parse_profile(text, db.get_settings(pid))
    data = p["data"]
    for k, v in parsed.items():           # resume facts refresh; your own answers (visa, EEO, salary) are kept
        if k in RESUME_FIELDS or not data.get(k):
            data[k] = v
    db.x("UPDATE profiles SET resume_path=?, resume_text=? WHERE id=?", (str(dest), text, pid))
    db.x("INSERT INTO resumes (profile_id, filename, path, text, uploaded_at) VALUES (?,?,?,?,?)",
         (pid, safe, str(dest), text, db.now()))
    db.save_profile_data(pid, data)
    db.log(pid, f"Resume uploaded: {dest.parent.name}/{dest.name} ({len(text)} chars), {len(parsed)} fields parsed")
    return {"ok": True, "parsed": parsed}


@app.get("/api/profile/{pid}/resumes")
def list_resumes(pid: int):
    active = _profile(pid)["resume_path"]
    rows = db.q("SELECT r.id, r.filename, r.path, r.uploaded_at, LENGTH(r.text) chars, "
                "(SELECT COUNT(*) FROM jobs j WHERE j.profile_id=r.profile_id AND j.resume_used=r.path) used_in "
                "FROM resumes r WHERE r.profile_id=? ORDER BY r.uploaded_at DESC, r.id DESC", (pid,))
    for r in rows:
        r["active"] = r["path"] == active
        r["folder"] = Path(r["path"]).parent.name
        r["exists"] = Path(r["path"]).exists()
    return rows


@app.post("/api/profile/{pid}/resumes/{rid}/use")
def use_resume(pid: int, rid: int):
    r = db.q("SELECT * FROM resumes WHERE id=? AND profile_id=?", (rid, pid), one=True)
    if not r or not Path(r["path"]).exists():
        raise HTTPException(404, "resume file not found")
    db.x("UPDATE profiles SET resume_path=?, resume_text=? WHERE id=?", (r["path"], r["text"], pid))
    db.log(pid, f"Now applying with resume {Path(r['path']).parent.name}/{r['filename']}")
    return {"ok": True}


@app.get("/api/profile/{pid}/resumes/{rid}/file")
def resume_file(pid: int, rid: int):
    r = db.q("SELECT * FROM resumes WHERE id=? AND profile_id=?", (rid, pid), one=True)
    if not r or not Path(r["path"]).exists():
        raise HTTPException(404, "resume file not found")
    return FileResponse(r["path"], filename=r["filename"])


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
           " status_note, found_at, applied_at, resume_used FROM jobs WHERE profile_id=?")
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
    url = sites.canonical(body["url"].strip()) or body["url"].strip()
    ats = search.detect_ats(url)
    j = dict(url=url, apply_url=url, title=body.get("title") or "(added manually)", company=body.get("company") or "",
             ats=ats)
    profile = _profile(pid)["data"]
    src = "listing" if ats in sites.BOARDS else "manual"
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
        settings = db.get_settings(pid)
        keywords, locations, days = search.search_terms(pid)
        urls = sites.search_urls(body.get("sites") or sites.enabled_sites(settings), keywords, locations, days)
        urls += [u.strip() for u in settings.get("listing_urls", "").splitlines() + body.get("extra_urls", []) if u.strip()]
        for u in dict.fromkeys(urls):                       # keep order, drop duplicates
            get_worker().enqueue("listing", pid, u)
        db.log(pid, f"Searching {len(dict.fromkeys(urls))} job-board pages in the bot browser")
    if ("ats" in sources or "web" in sources) and not _search_running.get(pid):
        def progress(m):
            if pid in _search_cancel:
                raise SearchCancelled()
            _search_running[pid] = m

        def go():
            _search_running[pid] = True
            _search_cancel.discard(pid)
            try:
                search.run_search(pid, sources, progress=progress)
            except SearchCancelled:
                db.log(pid, "Search stopped by you")
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
            "paused_for": max(0, int(w.paused_until - time.time())),
            "logs": db.q("SELECT ts, level, msg FROM logs WHERE profile_id IN (0, ?) ORDER BY id DESC LIMIT 80", (pid,))}


@app.post("/api/worker/human")
def human(body: dict = Body(...)):
    get_worker().human_reply(body.get("result", "continue"))
    return {"ok": True}


@app.post("/api/worker/stop")
def stop(body: dict = Body(default={})):
    """Stop everything: clear the apply queue, abandon the current job, cancel a running search."""
    w = get_worker()
    busy = w.state.get("busy")
    queued = w.queue_size()
    w.stop()
    for pid, running in _search_running.items():
        if running:
            _search_cancel.add(pid)
    db.log(0, f"Stop pressed: {queued} queued task(s) cleared" + (", current job is being stopped" if busy else ""))
    return {"ok": True, "cleared": queued, "was_busy": bool(busy)}


@app.post("/api/browser/{pid}/open")
def open_browser(pid: int, body: dict = Body(default={})):
    """Open the user's bot Chrome right away (not through the apply queue) so they can sign in."""
    url = body.get("url") or "https://jobright.ai/jobs/recommend?from=homepage"
    if chrome.ensure_running(pid, url):
        return {"ok": True, "browser": Path(chrome.find_browser()).name}
    get_worker().enqueue("open", pid, url)          # no Chrome/Edge installed: use the built-in browser
    return {"ok": True, "browser": "built-in"}


# ---------------- getting started: checklist + signing in to job sites ----------------
CORE_FIELDS = {"first_name": "first name", "last_name": "last name", "email": "email", "phone": "phone",
               "city": "city", "state": "state", "country": "country",
               "needs_sponsorship": "needs visa sponsorship (Yes/No)",
               "work_authorization_answer": "authorized to work in the US (Yes/No)"}


def _signed_in(pid):
    return [s for s in (db.get_settings(pid).get("signed_in_sites") or "").split(",") if s]


@app.get("/api/setup/{pid}")
def setup(pid: int):
    """What is done and what is left before the bot can apply well."""
    p = _profile(pid)
    s = db.get_settings(pid)
    data = p["data"]
    missing = [label for k, label in CORE_FIELDS.items() if not str(data.get(k) or "").strip()]
    keywords, _, _ = search.search_terms(pid)
    enabled = sites.enabled_sites(s)
    signed = _signed_in(pid)
    online_url = s.get("online_url")
    steps = [
        {"id": "resume", "title": "Upload your resume", "done": bool(p["resume_path"]), "required": True,
         "detail": "The bot uploads it and reads it to answer questions.", "tab": "profile"},
        {"id": "profile", "title": "Fill in your basic details", "done": not missing, "required": True,
         "detail": ("Missing: " + ", ".join(missing)) if missing else "Contact, location and work authorization are filled in.",
         "tab": "profile"},
        {"id": "search", "title": "Choose the jobs to look for", "done": bool(s.get("search_keywords") or data.get("target_titles")),
         "required": True, "detail": "Searching for: " + ", ".join(keywords[:6]), "tab": "settings"},
        {"id": "sites", "title": "Sign in to the job sites", "done": all(x in signed for x in enabled if x != "jobright") and bool(enabled),
         "required": True, "detail": ("Signed in: " + ", ".join(sites.SITES[x]["label"] for x in signed if x in sites.SITES))
                                     if signed else "LinkedIn, Dice and the others need you to sign in once.", "tab": "signin"},
        {"id": "email", "title": "Connect your Gmail", "done": bool(s.get("imap_user") and s.get("imap_password")),
         "required": False, "detail": "Reads confirmations, rejections and verification codes.", "tab": "settings"},
        {"id": "workday", "title": "Pick a password for Workday accounts", "done": bool(s.get("ats_account_password")),
         "required": False, "detail": "Workday asks for an account on every company site.", "tab": "settings"},
        {"id": "ai", "title": "Add a Claude API key", "done": bool(s.get("anthropic_api_key")), "required": False,
         "detail": "Lets the bot answer new questions from your resume instead of asking you.", "tab": "settings"},
        {"id": "online", "title": "Use it from your phone", "done": bool(online_url and relay.state.get("connected")),
         "required": False, "detail": ("Connected to " + online_url) if relay.state.get("connected")
                                      else "Optional: put the dashboard online (see README).", "tab": "settings"},
    ]
    return {"steps": steps, "ready": all(x["done"] for x in steps if x["required"]),
            "sites": [{"id": k, "label": v["label"], "signed_in": k in signed, "enabled": k in enabled}
                      for k, v in sites.SITES.items()]}


@app.post("/api/signin/{pid}")
def signin(pid: int, body: dict = Body(...)):
    """Open a job site's sign-in page in the bot Chrome and pause the bot so it doesn't open tabs in your way."""
    site = sites.SITES.get(body.get("site"))
    url = site["login"] if site else (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "Unknown site")
    w = get_worker()
    w.paused_until = time.time() + 20 * 60
    if not chrome.ensure_running(pid, url):
        w.enqueue("open", pid, url)
    db.log(pid, f"Paused the bot while you sign in to {site['label'] if site else url}")
    return {"ok": True, "paused_minutes": 20}


@app.post("/api/signin/{pid}/done")
def signin_done(pid: int, body: dict = Body(default={})):
    signed = _signed_in(pid)
    for k in body.get("sites", []):
        if k in sites.SITES and k not in signed:
            signed.append(k)
    db.set_settings(pid, {"signed_in_sites": ",".join(signed)})
    get_worker().paused_until = 0
    db.log(pid, "Bot resumed after sign-in")
    return {"ok": True, "signed_in": signed}


@app.post("/api/worker/resume")
def resume_worker():
    get_worker().paused_until = 0
    return {"ok": True}

# ---------------- online dashboard (Netlify) ----------------
@app.get("/api/_meta")
def meta():
    """The PC answers this itself; online the Netlify function answers it (online: true)."""
    return {"online": False, "relay": relay.state}


relay.start(app)
