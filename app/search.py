"""Job discovery: public ATS APIs (Greenhouse, Lever, Ashby, Workday) + internet search to find new companies.

Listing sites that need a login (jobright.ai, LinkedIn, ...) are scraped in the browser worker (browser.py)."""
import base64
import html
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from . import db, llm

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36"}

# Starter set; anything found through web search is added automatically.
SEED = {
    "greenhouse": ["stripe", "airbnb", "databricks", "robinhood", "coinbase", "discord", "figma", "instacart",
                   "lyft", "pinterest", "reddit", "dropbox", "asana", "gitlab", "cloudflare", "affirm", "brex",
                   "samsara", "okta", "twilio", "datadog", "elastic", "mongodb", "roblox", "anthropic"],
    "lever": ["palantir", "spotify", "plaid", "netflix"],
    "ashby": ["ramp", "notion", "openai", "linear", "vercel", "supabase", "retool"],
    "workday": [],   # "tenant|wdN|site", e.g. "nvidia|wd5|NVIDIAExternalCareerSite"
}

ATS_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)")),
    ("workday", re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)")),
]


def detect_ats(url: str) -> str:
    u = (url or "").lower()
    for name, key in [("greenhouse", "greenhouse.io"), ("lever", "lever.co"), ("ashby", "ashbyhq.com"),
                      ("workday", "myworkdayjobs.com"), ("icims", "icims.com"), ("smartrecruiters", "smartrecruiters.com"),
                      ("workable", "workable.com"), ("jobvite", "jobvite.com"), ("bamboohr", "bamboohr.com"),
                      ("taleo", "taleo.net"), ("successfactors", "successfactors"), ("oracle", "oraclecloud.com"),
                      ("linkedin", "linkedin.com"), ("jobright", "jobright.ai"), ("rippling", "rippling.com"),
                      ("dover", "dover.com"), ("wellfound", "wellfound.com")]:
        if key in u:
            return name
    return "other"


# ---------------- analysis ----------------
NO_SPONSOR = [
    r"(not|unable to|cannot|can't|won't|will not|do not|does not|don't|doesn't|no)\s+(be\s+able\s+to\s+)?(offer|provide|support|sponsor)\w*\s*(\w+\s+){0,3}(visa|sponsor|h-?1b)",
    r"without\s+(the\s+)?(need\s+for\s+)?(current\s+or\s+future\s+|future\s+)?(employer\s+|employment\s+|visa\s+)?sponsorship",
    r"sponsorship\s+(is\s+)?(not|un)\s*(available|offered|provided|possible)",
    r"not\s+(eligible|available)\s+for\s+(visa\s+)?sponsorship",
    r"u\.?s\.?\s+citizen(ship)?\s+(is\s+)?required|must\s+be\s+(a\s+)?u\.?s\.?\s+citizen",
    r"(active|current|ability to obtain)\s+(\w+\s+)?(security\s+)?clearance",
    r"(only|must be)\s+(\w+\s+){0,3}(green card|permanent resident)",
    r"\bno\s+h-?1b\b|h-?1b\s+not\s+(sponsored|available)",
]
YES_SPONSOR = [
    r"(will|can|able to|happy to|open to)\s+(\w+\s+){0,2}sponsor",
    r"(visa|h-?1b)\s+sponsorship\s+(is\s+)?(available|provided|offered|supported)",
    r"h-?1b\s+(transfer|sponsorship)",
    r"sponsorship\s+(is\s+)?available",
    r"h-?1b\s+sponsor(ed|ship)?\s+likely|h-?1b\s+sponsored",
]


def sponsorship_of(text: str) -> str:
    t = (text or "").lower()
    if any(re.search(p, t) for p in NO_SPONSOR):
        return "no"
    if any(re.search(p, t) for p in YES_SPONSOR):
        return "yes"
    return "unknown"


def norm_company(c: str) -> str:
    c = (c or "").lower()
    c = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|group|holdings)\b\.?", "", c)
    return re.sub(r"[^a-z0-9]", "", c)


def is_avoided(pid, company) -> bool:
    nc = norm_company(company)
    if not nc:
        return False
    for r in db.q("SELECT company FROM avoid_companies WHERE profile_id=?", (pid,)):
        a = norm_company(r["company"])
        if a and (a == nc or (len(a) > 3 and (a in nc or nc in a))):
            return True
    return False


def _tokens(s):
    return set(re.findall(r"[a-z0-9+#]+", (s or "").lower()))


def title_matches(title: str, keywords: list, exclude: list) -> bool:
    t = (title or "").lower()
    if any(e and re.search(r"\b" + re.escape(e) + r"\b", t) for e in exclude):
        return False
    tt = _tokens(t)
    for kw in keywords:
        kt = _tokens(kw)
        if kt and len(kt & tt) / len(kt) >= 0.6:
            return True
    return False


US_STATES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
             "illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
             "mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|"
             "north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
             "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming")


US_ABBR = ("(AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|"
           "ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)")


NON_US = (r"\b(canada|india|united kingdom|uk|london|germany|france|ireland|netherlands|spain|poland|brazil|mexico|"
          r"singapore|japan|australia|israel|toronto|vancouver|bangalore|bengaluru|hyderabad|berlin|dublin|"
          r"amsterdam|emea|apac|latam|europe)\b")


def location_ok(loc: str, wanted: list) -> bool:
    if not loc or not wanted:
        return True
    l = loc.lower()
    us_wanted = any(w.strip().lower() in ("united states", "usa", "us") for w in wanted)
    if us_wanted and re.search(NON_US, l) and not re.search(r"\b(usa|united states|u\.s\.)(\W|$)", l):
        return False
    for w in wanted:
        w = w.strip().lower()
        if not w:
            continue
        if w in l:
            return True
        if w in ("united states", "usa", "us") and (re.search(r"\b(usa|u\.s\.|united states|us)\b", l)
                                                    or re.search(r",\s*" + US_ABBR + r"\b", loc)
                                                    or re.search(r"\b(" + US_STATES + r")\b", l)):
            return True
    return False


def score(job: dict, profile: dict) -> float:
    skills = [s.strip().lower() for s in re.split(r"[,;|]", profile.get("skills", "")) if 1 < len(s.strip()) < 40]
    text = ((job.get("title") or "") + " " + (job.get("description") or "")).lower()
    if not skills:
        return 0.5
    hits = sum(1 for s in skills if s in text)
    return round(min(1.0, hits / max(5, min(15, len(skills) * 0.4))), 2)


def clean_html(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", html.unescape(s)).strip()


# ---------------- sources ----------------
def greenhouse(client, token):
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    if r.status_code != 200:
        return []
    name = token
    try:
        b = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}")
        name = b.json().get("name") or token
    except Exception:
        pass
    out = []
    for j in r.json().get("jobs", []):
        form = f"https://job-boards.greenhouse.io/embed/job_app?for={token}&token={j['id']}"
        out.append(dict(url=j["absolute_url"], apply_url=form, title=j.get("title"), company=name,
                        location=(j.get("location") or {}).get("name"), ats="greenhouse",
                        posted_at=j.get("first_published") or j.get("updated_at"),
                        description=clean_html(j.get("content", ""))))
    return out


def lever(client, co):
    r = client.get(f"https://api.lever.co/v0/postings/{co}?mode=json")
    if r.status_code != 200:
        return []
    out = []
    for j in r.json():
        desc = (j.get("descriptionPlain") or "") + "\n" + (j.get("additionalPlain") or "")
        for l in j.get("lists", []):
            desc += "\n" + l.get("text", "") + "\n" + clean_html(l.get("content", ""))
        ts = j.get("createdAt")
        out.append(dict(url=j["hostedUrl"], apply_url=j.get("applyUrl") or j["hostedUrl"] + "/apply",
                        title=j.get("text"), company=co.replace("-", " ").title(),
                        location=(j.get("categories") or {}).get("location"), ats="lever",
                        posted_at=datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None,
                        description=desc))
    return out


def ashby(client, org):
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true")
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        out.append(dict(url=j.get("jobUrl"), apply_url=j.get("applyUrl") or j.get("jobUrl"), title=j.get("title"),
                        company=unquote(org).replace("-", " ").title(), location=j.get("location"), ats="ashby",
                        posted_at=j.get("publishedAt"), description=j.get("descriptionPlain") or ""))
    return out


def workday(client, spec, keywords):
    tenant, wd, site = spec.split("|")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    out = []
    for kw in keywords[:4]:
        try:
            r = client.post(f"{base}/wday/cxs/{tenant}/{site}/jobs",
                            json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": kw})
            posts = r.json().get("jobPostings", []) if r.status_code == 200 else []
        except Exception:
            posts = []
        for p in posts:
            posted = p.get("postedOn", "")
            m = re.search(r"(\d+)\+? days? ago", posted)
            days = 0 if "today" in posted.lower() else 1 if "yesterday" in posted.lower() else int(m.group(1)) if m else 30
            url = f"{base}/en-US/{site}{p['externalPath']}"
            desc = ""
            try:
                d = client.get(f"{base}/wday/cxs/{tenant}/{site}{p['externalPath']}").json()
                desc = clean_html(d.get("jobPostingInfo", {}).get("jobDescription", ""))
            except Exception:
                pass
            out.append(dict(url=url, apply_url=url, title=p.get("title"), company=tenant.title(),
                            location=p.get("locationsText"), ats="workday",
                            posted_at=(datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
                            description=desc))
    return out


def web_search(settings, query, freshness_days=7):
    """Returns list of URLs. Uses Serper or Brave if keys are set, otherwise DuckDuckGo HTML."""
    urls = []
    with httpx.Client(headers=UA, timeout=25, follow_redirects=True) as c:
        try:
            if settings.get("serper_api_key"):
                tbs = "qdr:d" if freshness_days <= 1 else "qdr:w" if freshness_days <= 7 else "qdr:m"
                r = c.post("https://google.serper.dev/search", json={"q": query, "num": 50, "tbs": tbs},
                           headers={"X-API-KEY": settings["serper_api_key"]})
                urls = [o["link"] for o in r.json().get("organic", [])]
            elif settings.get("brave_api_key"):
                fr = "pd" if freshness_days <= 1 else "pw" if freshness_days <= 7 else "pm"
                r = c.get(f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(query)}&count=20&freshness={fr}",
                          headers={"X-Subscription-Token": settings["brave_api_key"], "Accept": "application/json"})
                urls = [o["url"] for o in r.json().get("web", {}).get("results", [])]
            else:
                ez = "ez1" if freshness_days <= 1 else "ez2" if freshness_days <= 7 else "ez3"
                for first in (0, 11):
                    r = c.get("https://www.bing.com/search", params={"q": query, "filters": f'ex1:"{ez}"', "first": first})
                    for href in re.findall(r'<li class="b_algo".*?<a[^>]+href="(https?://[^"]+)"', r.text, re.S):
                        href = html.unescape(href)
                        if "bing.com/ck/" in href:
                            u = parse_qs(urlparse(href).query).get("u", [""])[0]
                            if u.startswith("a1"):
                                try:
                                    href = base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode()
                                except Exception:
                                    continue
                        urls.append(href)
                if not urls:
                    df = "d" if freshness_days <= 1 else "w" if freshness_days <= 7 else "m"
                    r = c.post("https://html.duckduckgo.com/html/", data={"q": query, "df": df})
                    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text):
                        if "uddg=" in href:
                            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
                        urls.append(html.unescape(href))
        except Exception as e:
            db.log(0, f"web search failed: {e}", "warn")
    return urls


def discover_boards(settings, keywords, needs_sponsorship, days):
    """Use internet search to find ATS boards that are currently hiring for these titles."""
    found = {k: set() for k in SEED}
    sites = ["boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "myworkdayjobs.com"]
    extra = ' "visa sponsorship"' if needs_sponsorship else ""
    if settings.get("serper_api_key") or settings.get("brave_api_key"):
        queries = [f'site:{site} "{kw}"{extra}' for kw in keywords[:4] for site in sites]
        fetch = lambda qq: web_search(settings, qq, days)
    elif llm.available(settings):
        when = "today" if days <= 1 else f"in the last {days} days"
        queries = [f'"{kw}" jobs in the United States posted {when}'
                   + (" at companies that sponsor H-1B visas" if needs_sponsorship else "") for kw in keywords[:3]]
        fetch = lambda qq: llm.web_search_urls(settings, qq)
    else:
        db.log(0, "Internet search works best with a Serper/Brave key or Claude key (Settings); trying Bing", "warn")
        queries = [f'site:{site} {kw}' for kw in keywords[:3] for site in sites]
        fetch = lambda qq: web_search(settings, qq, days)
    for qq in queries:
        if True:
            for u in fetch(qq):
                for name, pat in ATS_PATTERNS:
                    m = pat.search(u)
                    if m:
                        if name == "workday":
                            found[name].add(f"{m.group(1)}|{m.group(2)}|{m.group(3)}")
                        elif m.group(1) not in ("embed", "jobs", "v1"):
                            found[name].add(m.group(1))
    return found


def run_search(pid, sources=("ats", "web"), progress=None):
    settings = db.get_settings(pid)
    profile = db.get_profile(pid)["data"]
    keywords = [k.strip() for k in settings["search_keywords"].split(",") if k.strip()]
    if profile.get("target_titles"):
        keywords += [k.strip() for k in profile["target_titles"].split(",") if k.strip() and k.strip() not in keywords]
    exclude = [k.strip().lower() for k in settings.get("exclude_title_words", "").split(",") if k.strip()]
    locations = [l.strip() for l in settings["search_locations"].split(",") if l.strip()]
    days = int(settings.get("max_job_age_days") or 7)
    needs_sp = db.truthy(settings.get("needs_sponsorship"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    boards = {k: set(v) for k, v in SEED.items()}
    for key, setting in [("greenhouse", "greenhouse_boards"), ("lever", "lever_companies"),
                         ("ashby", "ashby_orgs"), ("workday", "workday_sites")]:
        boards[key] |= {s.strip() for s in settings.get(setting, "").split(",") if s.strip()}
    # boards learned from previous searches
    learned = json.loads(db.get_settings(0).get("learned_boards", "{}") or "{}")
    for k, v in learned.items():
        boards.setdefault(k, set()).update(v)

    if "web" in sources:
        db.log(pid, "Searching the web for companies hiring now...")
        found = discover_boards(settings, keywords, needs_sp, days)
        for k, v in found.items():
            new = v - boards.get(k, set())
            if new:
                db.log(pid, f"web search found {len(new)} new {k} boards: {', '.join(sorted(new)[:15])}")
            boards[k] |= v
            learned.setdefault(k, [])
            learned[k] = sorted(set(learned[k]) | v)[-400:]
        db.set_settings(0, {"learned_boards": json.dumps(learned)})

    added = 0
    with httpx.Client(headers=UA, timeout=30, follow_redirects=True) as c:
        jobs = []
        for kind, items in boards.items():
            for item in sorted(items):
                if progress:
                    progress(f"{kind}: {item}")
                try:
                    if kind == "greenhouse":
                        jobs += greenhouse(c, item)
                    elif kind == "lever":
                        jobs += lever(c, item)
                    elif kind == "ashby":
                        jobs += ashby(c, item)
                    elif kind == "workday":
                        jobs += workday(c, item, keywords)
                except Exception as e:
                    db.log(pid, f"{kind}/{item} failed: {e}", "warn")
        for j in jobs:
            if save_job(pid, j, "api", keywords, exclude, locations, cutoff, needs_sp, profile):
                added += 1
    db.log(pid, f"Search finished: {added} new matching jobs")
    return added


def save_job(pid, j, source, keywords, exclude, locations, cutoff, needs_sp, profile, force=False):
    """Filter and store a job. Returns True if a new job was stored."""
    if not j.get("url"):
        return False
    if not force:
        if not title_matches(j.get("title"), keywords, exclude):
            return False
        if not location_ok(j.get("location"), locations):
            return False
        if j.get("posted_at") and cutoff:
            try:
                if datetime.fromisoformat(j["posted_at"].replace("Z", "+00:00")) < cutoff:
                    return False
            except ValueError:
                pass
    sp = sponsorship_of(j.get("description"))
    status = "new"
    note = None
    if is_avoided(pid, j.get("company")):
        status, note = "skipped", "company is on avoid list"
    elif needs_sp and sp == "no":
        status, note = "skipped", "posting says no visa sponsorship"
    if db.q("SELECT 1 FROM jobs WHERE profile_id=? AND url=?", (pid, j["url"]), one=True):
        return False
    db.x("INSERT INTO jobs (profile_id, url, apply_url, title, company, location, source, ats, posted_at, description,"
         " sponsorship, score, status, status_note, found_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (pid, j["url"], j.get("apply_url") or j["url"], j.get("title"), j.get("company"), j.get("location"),
          source, j.get("ats") or detect_ats(j.get("apply_url") or j["url"]), j.get("posted_at"),
          (j.get("description") or "")[:20000], sp, score(j, profile), status, note, db.now()))
    return status == "new"
