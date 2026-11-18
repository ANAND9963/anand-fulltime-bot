"""SQLite storage. Everything is scoped by profile_id so several people can use one install."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "jobbot.db"

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',          -- parsed/edited profile fields (json)
    resume_path TEXT,
    resume_text TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    profile_id INTEGER NOT NULL,              -- 0 = global
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (profile_id, key)
);
CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    qkey TEXT NOT NULL,                       -- normalized question
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    source TEXT DEFAULT 'user',               -- user | llm
    updated_at TEXT,
    UNIQUE (profile_id, qkey)
);
CREATE TABLE IF NOT EXISTS avoid_companies (
    profile_id INTEGER NOT NULL,
    company TEXT NOT NULL,
    PRIMARY KEY (profile_id, company)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    apply_url TEXT,
    title TEXT, company TEXT, location TEXT,
    source TEXT, ats TEXT,
    posted_at TEXT,
    description TEXT,
    sponsorship TEXT DEFAULT 'unknown',       -- yes | no | unknown
    score REAL DEFAULT 0,
    status TEXT DEFAULT 'new',                -- new | queued | applying | needs_input | needs_human | applied | failed | skipped
    status_note TEXT,
    found_at TEXT,
    applied_at TEXT,
    UNIQUE (profile_id, url)
);
CREATE TABLE IF NOT EXISTS pending_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    job_id INTEGER,
    qkey TEXT NOT NULL,
    question TEXT NOT NULL,
    field_type TEXT,
    options TEXT,                             -- json list
    created_at TEXT,
    UNIQUE (profile_id, qkey)
);
CREATE TABLE IF NOT EXISTS email_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    msg_id TEXT,
    company TEXT, subject TEXT, sender TEXT,
    kind TEXT,                                -- applied | rejected | interview | other
    received_at TEXT,
    job_id INTEGER,
    UNIQUE (profile_id, msg_id)
);
CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,                       -- data/resumes/<profile>/<YYYY-MM-DD>/<file>
    text TEXT,
    uploaded_at TEXT
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER, ts TEXT, level TEXT, msg TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


_conn = conn()
with _lock:
    _conn.executescript(SCHEMA)
    _cols = {r[1] for r in _conn.execute("PRAGMA table_info(jobs)")}
    if "resume_used" not in _cols:                  # which resume file went with each application
        _conn.execute("ALTER TABLE jobs ADD COLUMN resume_used TEXT")
    _conn.commit()


def migrate_resumes():
    """Move resumes uploaded before dated folders existed into data/resumes/<pid>/<date>/ and record them."""
    import shutil
    for p in q("SELECT id, resume_path, resume_text, created_at FROM profiles WHERE resume_path IS NOT NULL"):
        if q("SELECT 1 FROM resumes WHERE profile_id=? AND path=?", (p["id"], p["resume_path"]), one=True):
            continue
        src = Path(p["resume_path"])
        path = src
        if src.exists() and src.parent.name == str(p["id"]):          # old flat layout
            day = datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d")
            dest = src.parent / day / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dest)
            path = dest
            x("UPDATE profiles SET resume_path=? WHERE id=?", (str(dest), p["id"]))
        x("INSERT INTO resumes (profile_id, filename, path, text, uploaded_at) VALUES (?,?,?,?,?)",
          (p["id"], src.name, str(path), p["resume_text"],
           datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
           if path.exists() else now()))


def q(sql, args=(), one=False):
    with _lock:
        cur = _conn.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid


# ---------- logs ----------
def log(profile_id, msg, level="info"):
    print(f"[{level}] p{profile_id}: {msg}", flush=True)
    x("INSERT INTO logs (profile_id, ts, level, msg) VALUES (?,?,?,?)", (profile_id, now(), level, str(msg)[:2000]))


# ---------- settings ----------
DEFAULT_SETTINGS = {
    "anthropic_api_key": "",
    "llm_model": "claude-opus-5-5",
    "auto_submit": "true",
    "headless": "false",
    "needs_sponsorship": "true",
    "max_job_age_days": "7",
    "min_score": "0.15",
    "imap_host": "imap.gmail.com",
    "imap_user": "",
    "imap_password": "",                 # Gmail: use an App Password
    "ats_account_password": "",          # password the bot uses when it must create Workday/iCIMS accounts
    "search_keywords": "Software Engineer, Backend Engineer, Java Developer, Full Stack Engineer",
    "search_locations": "United States, Remote",
    "brave_api_key": "",                 # optional, better web search
    "serper_api_key": "",                # optional, Google results via serper.dev
    "greenhouse_boards": "",             # extra company board tokens, comma separated
    "lever_companies": "",
    "ashby_orgs": "",
    "workday_sites": "",                 # tenant|wdN|site, comma separated
    "exclude_title_words": "intern, internship, director, manager, principal, staff, lead, clearance, phd",
    "listing_urls": "https://jobright.ai/jobs/recommend?from=homepage",
}


def get_settings(profile_id):
    s = dict(DEFAULT_SETTINGS)
    for r in q("SELECT key, value FROM settings WHERE profile_id IN (0, ?) ORDER BY profile_id", (profile_id,)):
        s[r["key"]] = r["value"]
    return s


def set_settings(profile_id, values: dict):
    for k, v in values.items():
        x("INSERT INTO settings (profile_id, key, value) VALUES (?,?,?) "
          "ON CONFLICT(profile_id, key) DO UPDATE SET value=excluded.value", (profile_id, k, str(v)))


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ---------- profiles ----------
def get_profile(pid):
    p = q("SELECT * FROM profiles WHERE id=?", (pid,), one=True)
    if p:
        p["data"] = json.loads(p["data"] or "{}")
    return p


def save_profile_data(pid, data: dict):
    x("UPDATE profiles SET data=? WHERE id=?", (json.dumps(data), pid))


migrate_resumes()
