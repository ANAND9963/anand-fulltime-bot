"""Answer memory: maps form questions -> answers. Learns every time the user answers something new."""
import difflib
import json
import re

from . import db, llm

STOP = {"please", "your", "the", "a", "an", "you", "do", "are", "is", "of", "what", "to", "if", "in", "for",
        "this", "position", "role", "company", "required", "optional", "select", "one", "enter", "provide"}


def norm(question: str) -> str:
    s = re.sub(r"[*∗]", " ", question.lower())
    s = re.sub(r"\(.*?optional.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = [w for w in s.split() if w not in STOP]
    return " ".join(words)[:200]


# Questions we can answer straight from profile fields. Patterns run against the normalized question.
PROFILE_RULES = [
    (r"^(legal )?first name|^given name|^first$", "first_name"),
    (r"^(legal )?last name|^family name|^surname|^last$", "last_name"),
    (r"^(full )?name$|^full legal name|^legal name$", "full_name"),
    (r"preferred name", "first_name"),
    (r"e ?mail", "email"),
    (r"phone|mobile|cell", "phone"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"website|portfolio|personal site", "website"),
    (r"city and state|city state", "location"),
    (r"^city|current city|location city", "city"),
    (r"^state|province", "state"),
    (r"^country$|country.*(reside|live|located)|^country residence", "country"),
    (r"zip|postal", "zip"),
    (r"^(street )?address( line 1)?$", "address"),
    (r"^(current )?location$|where.*(located|based|reside)", "location"),
    (r"(current|previous|most recent|latest).*(job )?title", "current_title"),
    (r"(current|previous|most recent|latest).*(employer|company)", "current_company"),
    (r"^(how many |total )?(years|yrs)( of)?( professional| relevant| total| work)? experience( have)?$", "years_experience"),
    (r"^school|university|college|institution", "school"),
    (r"^degree", "degree"),
    (r"major|field of study|discipline", "major"),
    (r"pronoun", "pronouns"),
]

# Common yes/no questions derived from work authorization settings.
def _derived(profile: dict, nq: str):
    sponsor = str(profile.get("needs_sponsorship", "")).lower()
    if re.search(r"sponsor", nq):
        if sponsor in ("yes", "true", "1"):
            return "Yes"
        if sponsor in ("no", "false", "0"):
            return "No"
    if re.search(r"(legally )?authori[sz]ed.*work|eligible.*work|right to work", nq):
        return profile.get("work_authorization_answer") or ("Yes" if profile.get("work_authorization") else None)
    if re.search(r"^gender|gender identity", nq):
        return profile.get("gender")
    if re.search(r"race|ethnicity|hispanic|latino", nq):
        return profile.get("race")
    if re.search(r"veteran", nq):
        return profile.get("veteran_status")
    if re.search(r"disabilit", nq):
        return profile.get("disability_status")
    if re.search(r"relocat", nq):
        return profile.get("willing_to_relocate")
    if re.search(r"salary|compensation|pay expectation", nq):
        return profile.get("desired_salary")
    if re.search(r"notice period|start date|when.*start|earliest", nq):
        return profile.get("notice_period")
    if re.search(r"how did you hear|where did you (hear|find|learn)|source", nq):
        return profile.get("referral_source") or "Company website"
    if re.search(r"previously (worked|employed)|ever (worked|been employed).*(for|by|at)|former employee", nq):
        return "No"
    if re.search(r"18 years|age of 18|at least 18", nq):
        return "Yes"
    if re.search(r"privacy|consent|acknowledge|certify|agree", nq):
        return "Yes"
    return None


def lookup(pid: int, profile: dict, question: str, options=None):
    """Return (answer, source) or (None, None)."""
    nq = norm(question)
    if not nq:
        return None, None
    ans, src = _lookup(pid, profile, question, nq)
    # an answer that can't be mapped to any visible option is useless here -> let Claude / the user answer
    if ans is not None and options and pick_option(ans, options) is None:
        return None, None
    return ans, src


def _lookup(pid, profile, question, nq):
    for pat, field in PROFILE_RULES:
        if re.search(pat, nq):
            if field == "location":
                v = ", ".join(x for x in [profile.get("city"), profile.get("state")] if x)
            else:
                v = profile.get(field)
            if v:
                return str(v), "profile"
    row = db.q("SELECT answer FROM answers WHERE profile_id=? AND qkey=?", (pid, nq), one=True)
    if row:
        return row["answer"], "saved"
    saved = db.q("SELECT qkey, answer FROM answers WHERE profile_id=?", (pid,))
    if saved:
        best = difflib.get_close_matches(nq, [r["qkey"] for r in saved], n=1, cutoff=0.86)
        if best:
            return next(r["answer"] for r in saved if r["qkey"] == best[0]), "saved"
    d = _derived(profile, nq + " || " + re.sub(r"\s+", " ", question.lower()))
    if d:
        return str(d), "rule"
    return None, None


def llm_answer(settings, profile, resume_text, saved, question, field_type, options, job=None):
    """Ask Claude to answer only when it can be grounded in the resume/profile. Returns str or None."""
    if not llm.available(settings):
        return None
    out = llm.ask_json(
        settings,
        "You fill job application forms for the candidate below. Answer ONLY from the resume, profile and saved "
        "answers. Never invent facts (dates, salary, legal status, demographic info, references). If the "
        "answer is not clearly supported, return {\"answer\": null}. For choice fields return one option text "
        "exactly as given. For free-text 'why do you want to work here' style questions write 2-4 sincere "
        "sentences grounded in the resume and the job.",
        json.dumps({
            "question": question, "field_type": field_type, "options": options,
            "job": {k: (job or {}).get(k) for k in ("title", "company", "description")} if job else None,
            "profile": profile, "saved_answers": saved[:80], "resume": resume_text[:15000],
        })[:60000],
        max_tokens=2000,
    )
    if out and out.get("answer") not in (None, "", "null"):
        return str(out["answer"])
    return None


def save(pid, question, answer, source="user"):
    nq = norm(question)
    db.x("INSERT INTO answers (profile_id, qkey, question, answer, source, updated_at) VALUES (?,?,?,?,?,?) "
         "ON CONFLICT(profile_id, qkey) DO UPDATE SET answer=excluded.answer, source=excluded.source, "
         "updated_at=excluded.updated_at", (pid, nq, question, answer, source, db.now()))
    db.x("DELETE FROM pending_questions WHERE profile_id=? AND qkey=?", (pid, nq))


def add_pending(pid, job_id, question, field_type, options):
    db.x("INSERT OR IGNORE INTO pending_questions (profile_id, job_id, qkey, question, field_type, options, created_at)"
         " VALUES (?,?,?,?,?,?,?)", (pid, job_id, norm(question), question, field_type,
                                     json.dumps(options or []), db.now()))


def pick_option(answer: str, options: list):
    """Map a free answer onto one of the visible options."""
    if not options:
        return None
    a = answer.strip().lower()
    for o in options:
        if o.strip().lower() == a:
            return o
    for o in options:
        ol = o.strip().lower()
        if a and (ol.startswith(a) or a.startswith(ol)):
            return o
    if a in ("yes", "no"):
        for o in options:
            if o.strip().lower().startswith(a):
                return o
    for o in options:
        if a and a in o.lower():
            return o
    m = difflib.get_close_matches(a, [o.lower() for o in options], n=1, cutoff=0.5)
    if m:
        return next(o for o in options if o.lower() == m[0])
    return None
