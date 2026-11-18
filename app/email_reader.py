"""IMAP mailbox reader: verification codes/links during applications + tracking applied/rejected/interview mails."""
import email
import imaplib
import re
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from . import db, search


def _connect(settings):
    if not settings.get("imap_user") or not settings.get("imap_password"):
        raise RuntimeError("Email (IMAP) is not configured in Settings")
    m = imaplib.IMAP4_SSL(settings.get("imap_host") or "imap.gmail.com")
    m.login(settings["imap_user"], settings["imap_password"])
    return m


def _text(msg):
    parts = []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct in ("text/plain", "text/html") and not part.get_filename():
            try:
                payload = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
            except Exception:
                continue
            parts.append(payload if ct == "text/plain" else search.clean_html(payload))
            if ct == "text/html":
                parts.append(" ".join(re.findall(r'href="([^"]+)"', payload)))
    return "\n".join(parts)


def _hdr(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def _fetch_since(m, since: datetime, folder="INBOX", limit=200):
    m.select(folder, readonly=True)
    typ, data = m.search(None, f'(SINCE "{since.strftime("%d-%b-%Y")}")')
    ids = data[0].split()[-limit:]
    out = []
    for i in reversed(ids):
        typ, d = m.fetch(i, "(RFC822)")
        msg = email.message_from_bytes(d[0][1])
        try:
            dt = parsedate_to_datetime(msg["Date"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
        out.append(dict(id=msg.get("Message-ID") or i.decode(), date=dt, subject=_hdr(msg["Subject"]),
                        sender=_hdr(msg["From"]), body=_text(msg)))
    return out


def wait_for_code(settings, since: datetime, hint="", timeout=150):
    """Poll the inbox for a verification code (e.g. Greenhouse security code)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            m = _connect(settings)
            for e in _fetch_since(m, since - timedelta(days=1), limit=30):
                if e["date"] < since - timedelta(seconds=60):
                    continue
                blob = e["subject"] + "\n" + e["body"]
                if not re.search(r"code|verif|security", blob, re.I):
                    continue
                if hint and hint.lower() not in blob.lower() and "greenhouse" not in blob.lower():
                    continue
                m2 = re.search(r"(?:code|pin)[^A-Za-z0-9]{0,40}\b([A-Za-z0-9]{6,8})\b", blob, re.I) \
                    or re.search(r"\b(\d{6})\b", blob)
                if m2:
                    m.logout()
                    return m2.group(1)
            m.logout()
        except Exception as ex:
            db.log(0, f"IMAP: {ex}", "warn")
        time.sleep(8)
    return None


def wait_for_link(settings, since: datetime, domain_hint="myworkdayjobs", timeout=180):
    """Poll the inbox for an account-verification link (e.g. Workday)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            m = _connect(settings)
            for e in _fetch_since(m, since - timedelta(days=1), limit=30):
                if e["date"] < since - timedelta(seconds=60):
                    continue
                for link in re.findall(r"https?://[^\s\"'<>]+", e["body"]):
                    if domain_hint in link and re.search(r"verif|activat|confirm", link, re.I):
                        m.logout()
                        return link
            m.logout()
        except Exception as ex:
            db.log(0, f"IMAP: {ex}", "warn")
        time.sleep(8)
    return None


APPLIED = r"(thank you for (applying|your application|your interest)|application (has been )?(received|submitted)|we('ve| have) received your application|thanks for applying)"
REJECT = r"(unfortunately|not (to )?(move|moving) forward|other candidates|decided not to|no longer (being )?consider|position has been filled|will not be proceeding)"
INTERVIEW = r"(schedule (a|an|your) (call|interview|chat)|interview invitation|next steps? in (the|our) (process|interview)|would like to (invite|speak|schedule)|phone screen|coding (challenge|assessment)|online assessment|hackerrank|codesignal)"


def _company_from(e):
    s = e["subject"]
    for pat in [r"(?:applying|application|interest) (?:to|at|with) ([A-Z][\w&.\- ]{1,40}?)(?:[!.,|-]|$| for| -)",
                r"^([A-Z][\w&.\- ]{1,40}?)\s*[-|:]\s", r"at ([A-Z][\w&.\- ]{1,40})$"]:
        m = re.search(pat, s)
        if m:
            return m.group(1).strip()
    name, addr = parseaddr(e["sender"])
    name = re.sub(r"\b(careers?|recruiting|talent|jobs|hiring|team|no-?reply|hr)\b", "", name, flags=re.I).strip(" -|@")
    if name and not re.search(r"greenhouse|lever|workday|ashby|icims|notification", name, re.I):
        return name
    dom = addr.split("@")[-1].split(".")
    return dom[-2].title() if len(dom) >= 2 and dom[-2] not in ("gmail", "greenhouse", "lever", "myworkday", "icims", "ashbyhq") else ""


def scan_mailbox(pid, days=14):
    """Read recent mail, record applied/rejected/interview events and update job statuses."""
    settings = db.get_settings(pid)
    m = _connect(settings)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    n = 0
    for e in _fetch_since(m, since, limit=500):
        blob = e["subject"] + "\n" + e["body"][:5000]
        kind = None
        if re.search(INTERVIEW, blob, re.I):
            kind = "interview"
        elif re.search(REJECT, blob, re.I) and re.search(r"application|position|role|candidacy", blob, re.I):
            kind = "rejected"
        elif re.search(APPLIED, blob, re.I):
            kind = "applied"
        if not kind:
            continue
        company = _company_from(e)
        nc = search.norm_company(company)
        job = None
        if nc:
            for j in db.q("SELECT id, company, status FROM jobs WHERE profile_id=?", (pid,)):
                jc = search.norm_company(j["company"])
                if jc and (jc == nc or (len(jc) > 3 and (jc in nc or nc in jc))):
                    job = j
                    break
        try:
            new = db.x("INSERT OR IGNORE INTO email_events (profile_id, msg_id, company, subject, sender, kind, received_at, job_id)"
                       " VALUES (?,?,?,?,?,?,?,?)", (pid, e["id"], company, e["subject"][:300], e["sender"][:200], kind,
                                                      e["date"].isoformat(), job["id"] if job else None))
        except Exception:
            new = 0
        if job and new:
            if kind == "applied" and job["status"] != "applied":
                db.x("UPDATE jobs SET status='applied', status_note='confirmed by email', applied_at=? WHERE id=?",
                     (e["date"].isoformat(), job["id"]))
            elif kind in ("rejected", "interview"):
                db.x("UPDATE jobs SET status_note=? WHERE id=?", (f"email: {kind}", job["id"]))
        n += 1
    m.logout()
    db.log(pid, f"Mailbox scan: {n} application emails found")
    return n
