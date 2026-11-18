"""Resume text extraction + profile field extraction."""
import re
from pathlib import Path

from . import llm

PROFILE_FIELDS = [
    "first_name", "last_name", "full_name", "email", "phone", "linkedin", "github", "website",
    "city", "state", "country", "zip", "address",
    "current_title", "current_company", "years_experience",
    "school", "degree", "major", "graduation_year",
    "work_authorization", "needs_sponsorship", "visa_status",
    "desired_salary", "notice_period", "willing_to_relocate",
    "gender", "race", "veteran_status", "disability_status", "pronouns",
    "skills", "target_titles", "summary",
]


def extract_text(path: str) -> str:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)
    if p.suffix.lower() == ".docx":
        import docx
        return "\n".join(par.text for par in docx.Document(str(p)).paragraphs)
    return p.read_text(errors="ignore")


def heuristic_profile(text: str) -> dict:
    d = {}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        name = re.sub(r"[^A-Za-z .'-]", "", lines[0]).strip()
        if 1 < len(name.split()) <= 4:
            d["full_name"] = name.title()
            d["first_name"], d["last_name"] = name.title().split()[0], name.title().split()[-1]
    if m := re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
        d["email"] = m.group(0)
    if m := re.search(r"\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}", text):
        d["phone"] = m.group(0)
    if m := re.search(r"https?://(www\.)?linkedin\.com/in/[\w-]+/?", text):
        d["linkedin"] = m.group(0)
    if m := re.search(r"https?://(www\.)?github\.com/[\w-]+", text):
        d["github"] = m.group(0)
    if m := re.search(r"(\d+)\+?\s+years", text, re.I):
        d["years_experience"] = m.group(1)
    if m := re.search(r"skills?\s*\n(.*?)(\n[A-Z ]{6,}\n)", text, re.S | re.I):
        s = re.sub(r"(^|\s)[A-Z][\w &/]{2,30}:\s*", ", ", m.group(1))   # drop "Languages:" style category labels
        d["skills"] = ", ".join(x.strip() for x in re.split(r"[,\n]", s) if x.strip())[:1500]
    return d


def parse_profile(text: str, settings: dict) -> dict:
    base = heuristic_profile(text)
    out = llm.ask_json(
        settings,
        "You extract job-application profile fields from a resume.",
        "Return JSON with any of these keys you can fill confidently from the resume "
        f"(omit unknown ones): {', '.join(PROFILE_FIELDS)}.\n"
        "skills: comma separated list. target_titles: 3-6 comma separated job titles this person should apply to. "
        "years_experience: a number. summary: 2 sentences.\n\nRESUME:\n" + text[:30000],
        effort="low",
    )
    if out:
        base.update({k: str(v) for k, v in out.items() if v not in (None, "", [])})
    return base
