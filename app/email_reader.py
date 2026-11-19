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


# ------------------------------------------------------------------ classification
APPLIED = (r"thank(s| you)[^.\n]{0,25}(for (applying|your application|your interest|your submission|submitting))|"
           r"application (has been |was )?(successfully )?(received|submitted)|application received|"
           r"we('ve| have) (successfully )?received your (application|resume)|"
           r"your application (for|to) .{3,80} (has been|was) (received|submitted)|thanks for your submission")
REJECT = (r"unfortunately|not (to )?(move|moving) forward|(other|another) candidates?|decided not to (proceed|move)|"
          r"no longer (being )?consider|position has been filled|will not be proceeding|not been selected|"
          r"pursue other candidates|regret to inform")
INTERVIEW = (r"schedule (a|an|your) (call|interview|chat|time)|interview (invitation|request|availability)|"
             r"invite you to (an? )?(interview|call|chat)|would like to (invite you|speak with you|schedule)|"
             r"phone screen|technical (interview|screen)|coding (challenge|assessment)|online assessment|"
             r"hackerrank|codesignal|karat|next round")
# mail that mentions jobs but is not about one of your applications
NOISE = (r"verification code|verify your (email|account)|security code|one[- ]time (code|passcode)|digest|newsletter|"
         r"webinar|job alert|jobs? (for you|you may like|recommended)|recommended jobs|new jobs|apply now|"
         r"weekly|top picks|is hiring|are hiring|talent community|join our|unsubscribe from this alert|"
         r"career opportunities|new opportunities|opportunities at|stay informed|what.s next|imagine|explore|"
         r"don.t miss|last chance|invitation to (an? )?(event|webinar)")
NOISE_SENDERS = (r"leetcode|newsletters?-noreply|jobalerts|jobs-noreply@linkedin|glassdoor|indeed\.com|ziprecruiter|"
                 r"dice\.com|medium\.com|substack")
RECRUITER_SUBJ = r"^\s*(re|fw|fwd)\s*:"

ATS_NOISE = (r"\b(careers?|recruiting|recruitment|talent( acquisition)?|acquisition|jobs|hiring|team|no-?reply|"
             r"noreply|notifications?|hr4u|hr|people|myworkday|workday|greenhouse|lever|ashby|icims|smartrecruiters|"
             r"support|the|ta)\b")
ATS_DOMAINS = ("gmail", "greenhouse-mail", "greenhouse", "lever", "myworkday", "myworkdayjobs", "icims", "ashbyhq",
               "smartrecruiters", "ripplehire", "workablemail", "jobvite", "successfactors", "taleo", "ats", "hire",
               "outlook", "yahoo", "hotmail", "rippling", "bamboohr", "paylocity", "ultipro", "oraclecloud")
GENERIC = r"(?i)(software|senior|sr|the|our|this|your|application|applications?|job|position|role|engineer|update|status).*"


def _clean_company(c):
    c = re.sub(r"\s+", " ", (c or "")).strip(" -|:!.,'\"")
    c = re.sub(r"^(the|your)\s+", "", c, flags=re.I)
    return c[:60]


def company_from(subject, sender):
    s = re.sub(RECRUITER_SUBJ, "", subject, flags=re.I).strip()
    end = r"(?=\s*(?:[!.,|:–—-]|$|\sfor\s|\sas\s|\sin\s|'s\s))"
    pats = [
        r"(?:applying|application|apply|interest|submission|applied)\s+(?:to|at|with|for)\s+(?:the\s+)?([A-Z][\w&.'\- ]{1,40}?)" + end,
        r"(?:career|job|opportunit(?:y|ies)|position|role)s?\s+(?:at|with)\s+([A-Z][\w&.'\- ]{1,40}?)" + end,
        r"updated at ([A-Z][\w&.'\- ]{1,40}?)" + end,
        r"^([A-Z][\w&.']+(?: [A-Z][\w&.']+){0,3}) (?:Application Status|application update|\|)",
        r"^([A-Z][\w&.']+(?: [A-Z][\w&.']+){0,3})\s*[-–|:]\s*(?:your )?(?:application|thank)",
    ]
    for p in pats:
        m = re.search(p, s)
        if m:
            c = _clean_company(m.group(1))
            if c and not re.fullmatch(GENERIC, c):
                return c
    name, addr = parseaddr(sender)
    local, _, domain = addr.lower().partition("@")
    n = _clean_company(re.sub(ATS_NOISE, " ", name, flags=re.I))
    if n and len(n) > 1 and not re.fullmatch(GENERIC, n):
        return n
    if any(k in domain for k in ("myworkday", "greenhouse", "icims")):
        return local.title() if local and not re.search(r"no-?reply|notification", local) else ""
    parts = domain.split(".")
    if len(parts) >= 2 and parts[-2] not in ATS_DOMAINS:
        return parts[-2].title()
    return ""


def classify(subject, sender, body):
    blob = subject + "\n" + body[:6000]
    if re.search(NOISE_SENDERS, sender, re.I) or re.search(NOISE, subject, re.I):
        return None
    recruiter_thread = bool(re.search(RECRUITER_SUBJ, subject, re.I))
    if re.search(INTERVIEW, blob, re.I):
        return "recruiter" if recruiter_thread and not re.search(r"interview", subject, re.I) else "interview"
    if re.search(REJECT, blob, re.I) and re.search(r"application|position|role|candidacy|interest", blob, re.I):
        return "rejected"
    if re.search(APPLIED, blob, re.I):
        return "applied"
    if recruiter_thread and re.search(r"resume|position|role|opportunit|developer|engineer", subject, re.I):
        return "recruiter"
    return None


def title_from(subject):
    s = re.sub(RECRUITER_SUBJ, "", subject, flags=re.I)
    m = re.search(r"((?:Sr\.?|Senior|Junior|Lead|Staff|Principal)?\s*[\w/&+.()' ]*?"
                  r"(?:Engineer|Developer|Programmer|Architect|SDE|Analyst|Scientist)\b(?:[\w ,()/]{0,30}?(?=\s*(?:[!|–—]|$| at | with | - | on ))|))", s, re.I)
    if not m:
        return ""
    t = re.sub(r"\s+", " ", m.group(1)).strip(" -,")
    t = re.sub(r"^(?:.*\b(?:for|to|applying|application|submission)\s+(?:the\s+)?)", "", t, flags=re.I)
    t = re.sub(r"^(?:update on|re|your|the|our|a|an)\s+|\b(?:REQ|R|JR)[-_]?\d{3,}\b\s*", "", t, flags=re.I).strip(" -,")
    return t[:100]


def _match_job(pid, company, title):
    nc = search.norm_company(company)
    if len(nc) < 3:
        return None
    best = None
    for j in db.q("SELECT id, company, title, status FROM jobs WHERE profile_id=? AND company IS NOT NULL", (pid,)):
        jc = search.norm_company(j["company"])
        if len(jc) < 3 or not (jc == nc or jc.startswith(nc) or nc.startswith(jc)):
            continue
        if title and j["title"] and search.title_similar(title, j["title"]):
            return j
        best = best or j
    return None if title else best


def scan_mailbox(pid, days=21):
    """Read recent mail: record applied/rejected/interview/recruiter mails, mark matching jobs, and save
    applications you made anywhere else as 'applied' jobs so the bot never applies to them twice."""
    settings = db.get_settings(pid)
    m = _connect(settings)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    counts = {}
    seen = set()
    me = (settings.get("imap_user") or "").lower()
    for e in reversed(_fetch_since(m, since, limit=800)):     # oldest first: "applied" before "rejected"
        if (me and parseaddr(e["sender"])[1].lower() == me) or re.match(r"\s*(fw|fwd)\s*:", e["subject"], re.I):
            continue                                            # your own sent / forwarded mail
        kind = classify(e["subject"], e["sender"], e["body"])
        if not kind:
            continue
        key = (parseaddr(e["sender"])[1].lower(), re.sub(r"\W+", "", e["subject"].lower()), e["date"].strftime("%Y%m%d%H"))
        if key in seen:                 # the same mail delivered twice
            continue
        seen.add(key)
        # recruiter threads ("RE: Java Developer || Austin, TX") name a role, not a company: use the sender
        company = company_from("" if kind == "recruiter" else e["subject"], e["sender"])
        title = title_from(e["subject"])
        if company and title.lower().startswith(company.lower()):
            title = title[len(company):].strip(" -,")
        job = _match_job(pid, company, title) if kind != "recruiter" else None
        if db.q("SELECT 1 FROM email_events WHERE profile_id=? AND msg_id=?", (pid, e["id"]), one=True):
            continue                    # already recorded by an earlier scan
        new = db.x("INSERT OR IGNORE INTO email_events (profile_id, msg_id, company, subject, sender, kind, received_at, job_id)"
                   " VALUES (?,?,?,?,?,?,?,?)", (pid, e["id"], company, e["subject"][:300], e["sender"][:200], kind,
                                                  e["date"].isoformat(), job["id"] if job else None))
        if not new:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "applied":
            if job:
                if job["status"] != "applied":
                    db.x("UPDATE jobs SET status='applied', status_note='confirmed by email', applied_at=? WHERE id=?",
                         (e["date"].isoformat(), job["id"]))
            elif company and (dup := search.already_applied(pid, company, title)):
                db.x("UPDATE email_events SET job_id=? WHERE profile_id=? AND msg_id=?", (dup["id"], pid, e["id"]))
                if dup["title"] == "(from email)" and title:           # a later mail names the role
                    db.x("UPDATE jobs SET title=? WHERE id=?", (title, dup["id"]))
            elif company:
                # applied outside the bot: record it so it shows under Applied and is never re-applied
                db.x("INSERT OR IGNORE INTO jobs (profile_id, url, apply_url, title, company, source, ats, status,"
                     " status_note, found_at, applied_at) VALUES (?,?,?,?,?,'email',?,'applied',?,?,?)",
                     (pid, f"email:{e['id']}", None, title or "(from email)", company, search.detect_ats(e["sender"]),
                      "applied (found in email)", db.now(), e["date"].isoformat()))
                row = db.q("SELECT id FROM jobs WHERE profile_id=? AND url=?", (pid, f"email:{e['id']}"), one=True)
                if row:
                    db.x("UPDATE email_events SET job_id=? WHERE profile_id=? AND msg_id=?", (row["id"], pid, e["id"]))
        elif job and kind in ("rejected", "interview"):
            db.x("UPDATE jobs SET status_note=? WHERE id=?", (f"email: {kind} ({e['date']:%b %d})", job["id"]))
    m.logout()
    summary = ", ".join(f"{v} {k}" for k, v in counts.items()) or "nothing new"
    db.log(pid, f"Mailbox scan ({days} days): {summary}")
    return counts
