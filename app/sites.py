"""Job boards the bot searches in your logged-in bot Chrome: jobright, LinkedIn, Dice, ZipRecruiter, Monster.

Each site says how to build a search URL from your keywords, which links on its pages are job postings, how to
turn a link into one canonical URL (so the same job is stored once), and which button starts its own
quick-apply form (LinkedIn Easy Apply, Dice Easy Apply, ZipRecruiter 1-Click, Monster Quick Apply)."""
import re
from urllib.parse import quote_plus, urlparse

SITES = {
    "jobright": {
        "label": "jobright.ai",
        "domain": "jobright.ai",
        "login": "https://jobright.ai/",
        "search": lambda kw, loc, days: "https://jobright.ai/jobs/recommend?from=homepage",
        "per_keyword": False,                  # recommendations page, not a keyword search
        "job_link": r"jobright\.ai/jobs/info/[\w-]+",
        "quick_apply": [],
    },
    "linkedin": {
        "label": "LinkedIn",
        "domain": "linkedin.com",
        "login": "https://www.linkedin.com/login",
        "search": lambda kw, loc, days: (
            f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(kw)}&location={quote_plus(loc)}"
            f"&f_TPR=r{max(1, days) * 86400}&f_JT=F&sortBy=DD"),
        "job_link": r"linkedin\.com/(?:comm/)?jobs/view/(?:[\w-]*?-)?(\d{6,})|currentJobId=(\d{6,})",
        "canonical": lambda m: f"https://www.linkedin.com/jobs/view/{m.group(1) or m.group(2)}/",
        "quick_apply": [r"^\s*easy apply\s*$", r"easy apply"],
    },
    "dice": {
        "label": "Dice",
        "domain": "dice.com",
        "login": "https://www.dice.com/dashboard/login",
        "search": lambda kw, loc, days: (
            f"https://www.dice.com/jobs?q={quote_plus(kw)}&location={quote_plus(loc)}"
            f"&filters.postedDate={'ONE' if days <= 1 else 'THREE' if days <= 3 else 'SEVEN'}"
            f"&filters.employmentType=FULLTIME&pageSize=50"),
        "job_link": r"dice\.com/job-detail/([0-9a-f-]{20,})",
        "canonical": lambda m: f"https://www.dice.com/job-detail/{m.group(1)}",
        "quick_apply": [r"^\s*easy apply\s*$", r"easy apply", r"^\s*apply now\s*$"],
    },
    "ziprecruiter": {
        "label": "ZipRecruiter",
        "domain": "ziprecruiter.com",
        "login": "https://www.ziprecruiter.com/login",
        "search": lambda kw, loc, days: (
            f"https://www.ziprecruiter.com/jobs-search?search={quote_plus(kw)}&location={quote_plus(loc)}"
            f"&days={1 if days <= 1 else 5 if days <= 5 else 10}&refine_by_employment=employment_type%3Afull_time"),
        "job_link": r"ziprecruiter\.com/(?:c/[^/?#]+/[Jj]ob/[^?#]+|jobs/[^/?#]+/[^/?#]+|ek/l/[\w-]+|k/l/[\w-]+)",
        "canonical": lambda m: "https://www." + m.group(0).split("?")[0],
        "quick_apply": [r"1-click apply", r"quick apply", r"^\s*apply\s*$"],
    },
    "monster": {
        "label": "Monster",
        "domain": "monster.com",
        "login": "https://www.monster.com/profile/detail",
        "search": lambda kw, loc, days: (
            f"https://www.monster.com/jobs/search?q={quote_plus(kw)}&where={quote_plus(loc)}"
            f"&recency={'today' if days <= 1 else 'last+3+days' if days <= 3 else 'last+week'}"),
        "job_link": r"monster\.com/job-openings/[^?#\"' ]+",
        "canonical": lambda m: "https://www." + m.group(0).split("?")[0],
        "quick_apply": [r"quick apply", r"^\s*apply\s*$"],
    },
}

DEFAULT_ENABLED = "jobright, linkedin, dice, ziprecruiter, monster"


def site_of(url: str):
    host = urlparse(url or "").netloc.lower()
    for key, s in SITES.items():
        if host.endswith(s["domain"]):
            return key
    return None


def canonical(url: str):
    """One stable URL per posting (drops tracking parameters), or None if this is not a posting link."""
    key = site_of(url)
    if not key:
        return None
    s = SITES[key]
    m = re.search(s["job_link"], url)
    if not m:
        return None
    return s["canonical"](m) if "canonical" in s else url.split("#")[0]


def search_urls(enabled, keywords, locations, days, max_keywords=3):
    """Search pages to scrape: one per site x keyword (x first location)."""
    loc = next((l for l in locations if l.lower() != "remote"), "United States") if locations else "United States"
    out = []
    for key in enabled:
        s = SITES.get(key)
        if not s:
            continue
        if s.get("per_keyword", True) is False:
            out.append(s["search"]("", loc, days))
            continue
        for kw in keywords[:max_keywords]:
            out.append(s["search"](kw, loc, days))
    return out


def enabled_sites(settings):
    return [s.strip().lower() for s in (settings.get("job_sites") or "").split(",") if s.strip().lower() in SITES]

# sources whose postings are applied to through the board itself (its quick-apply or its "Apply" link)
BOARDS = set(SITES) | {"other"}
