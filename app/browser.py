"""Single background thread that owns the Playwright browser (persistent profile per user, so logins stick)."""
import queue
import re
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

from . import apply, chrome, db, search, sites
from .filler import captcha_visible, page_text, wait_settle


class Worker:
    def __init__(self):
        self.q = queue.Queue()
        self.state = {"busy": False, "current": None, "message": "idle", "waiting_human": False}
        self._human = threading.Event()
        self._human_result = None
        self._stop = False
        self.paused_until = 0           # bot waits while you sign in to job sites
        self.ctx = None
        self.ctx_pid = None
        self.browser = None
        self._owned = set()
        threading.Thread(target=self._run, daemon=True).start()

    # ---------- public API (called from web threads) ----------
    def enqueue(self, kind, pid, arg=None):
        self.q.put((kind, pid, arg))

    def queue_size(self):
        return self.q.qsize()

    def stop(self):
        self._stop = True
        while not self.q.empty():
            try:
                kind, pid, arg = self.q.get_nowait()
                if kind == "apply":
                    db.x("UPDATE jobs SET status='new' WHERE id=? AND status='queued'", (arg,))
            except queue.Empty:
                break
        self.human_reply("skip")

    def human_reply(self, result):
        self._human_result = result
        self._human.set()

    # ---------- called inside worker ----------
    def wait_human(self, actx, msg, timeout=900):
        actx.log("WAITING FOR YOU: " + msg, "warn")
        db.x("UPDATE jobs SET status='needs_human', status_note=? WHERE id=?", (msg, actx.job["id"]))
        self.state.update(waiting_human=True, message=msg)
        self._human.clear()
        self._human_result = None
        ok = self._human.wait(timeout)
        self.state.update(waiting_human=False)
        res = self._human_result if ok else "timeout"
        if self._stop:
            raise apply.Stopped()
        if res == "submitted":
            raise Submitted()
        if res != "continue":
            raise apply.NeedsHuman(msg if res == "timeout" else "skipped by you")
        db.x("UPDATE jobs SET status='applying', status_note=NULL WHERE id=?", (actx.job["id"],))

    def _alive(self):
        if not self.ctx:
            return False
        try:
            if self.browser is not None and not self.browser.is_connected():
                return False
            self.ctx.pages
            return True
        except Exception:
            return False

    def wait_for_user(self, pid, msg, timeout=600):
        """Yellow bar in the UI: ask the user to do something in the bot Chrome. True = Continue pressed."""
        db.log(pid, "WAITING FOR YOU: " + msg, "warn")
        self.state.update(waiting_human=True, message=msg)
        self._human.clear()
        self._human_result = None
        ok = self._human.wait(timeout)
        self.state.update(waiting_human=False)
        return ok and self._human_result == "continue" and not self._stop

    def _context(self, pid):
        """Attach to this user's real Chrome (started if needed); falls back to a Playwright browser."""
        if self.ctx_pid == pid and self._alive() and (self.browser is None or chrome.is_running(pid)):
            return self.ctx
        self._detach()
        headless = db.truthy(db.get_settings(pid).get("headless"))
        if chrome.ensure_running(pid, headless=headless):
            self.browser = self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{chrome.port_for(pid)}")
            self.ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        else:
            db.log(pid, "Chrome/Edge not found - using the built-in browser (Google sign-in may not work there). "
                        "Set chrome_path in Settings if Chrome is installed somewhere else.", "warn")
            self.browser = None
            self.ctx = self.pw.chromium.launch_persistent_context(
                user_data_dir=str(chrome.profile_dir(pid)), headless=headless, no_viewport=True)
        self.ctx_pid = pid
        return self.ctx

    def _detach(self):
        """Disconnect from Chrome without closing it (logins and tabs stay)."""
        try:
            if self.browser is not None:
                self.browser.close()        # for a CDP connection this only disconnects
            elif self.ctx is not None:
                self.ctx.close()
        except Exception:
            pass
        self.browser = None
        self.ctx = None

    def _page(self, pid):
        """A new tab for the bot. Tabs the bot opens are closed afterwards; your own tabs are never touched."""
        for attempt in range(2):
            ctx = self._context(pid)
            try:
                self._owned = set(ctx.pages)
                page = ctx.new_page()
                return page
            except Exception:
                self._detach()
        raise RuntimeError("could not open a browser tab")

    def _close_own_tabs(self, page):
        try:
            for p in list(page.context.pages):
                if p not in self._owned:
                    safe_close(p)
        except Exception:
            pass

    def _run(self):
        with sync_playwright() as pw:
            self.pw = pw
            while True:
                kind, pid, arg = self.q.get()
                while time.time() < self.paused_until and kind != "open":
                    self.state.update(message="paused while you sign in to job sites")
                    time.sleep(1)
                self._stop = False
                self.state.update(busy=True, current=(kind, arg), message=f"{kind} {arg or ''}")
                try:
                    if kind == "apply":
                        self._apply(pid, arg)
                    elif kind == "open":
                        if not chrome.ensure_running(pid, arg):
                            self._page(pid).goto(arg)
                    elif kind == "listing":
                        self._scrape_listing(pid, arg)
                except Exception as e:
                    db.log(pid, f"{kind} failed: {e}\n{traceback.format_exc()[-800:]}", "error")
                finally:
                    self.state.update(busy=False, current=None, message="idle")

    # ---------- tasks ----------
    def _apply(self, pid, job_id):
        job = db.q("SELECT * FROM jobs WHERE id=?", (job_id,), one=True)
        if not job or job["status"] in ("applied", "skipped"):
            return
        if search.is_avoided(pid, job["company"]):
            db.x("UPDATE jobs SET status='skipped', status_note='company is on avoid list' WHERE id=?", (job_id,))
            return
        dup = search.already_applied(pid, job["company"], job["title"], exclude_id=job_id)
        if dup:
            db.x("UPDATE jobs SET status='skipped', status_note=? WHERE id=?",
                 (f"already applied to {dup['company']} - {dup['title']} ({(dup['applied_at'] or '')[:10]})", job_id))
            db.log(pid, f"[job {job_id}] skipped: already applied to {dup['company']} - {dup['title']}")
            return
        prof = db.get_profile(pid)
        if not prof.get("resume_path"):
            db.log(pid, "Upload a resume first", "error")
            return
        db.x("UPDATE jobs SET status='applying', status_note=NULL, resume_used=? WHERE id=?",
             (prof["resume_path"], job_id))
        actx = apply.ApplyContext(pid, job, self)
        actx.log(f"applying: {job['title']} -> {job.get('apply_url') or job['url']}")
        page = self._page(pid)
        try:
            result = apply.apply_job(actx, page)
            if result == "skipped":
                actx.log("skipped (no sponsorship)")
                return
            db.x("UPDATE jobs SET status='applied', status_note='submitted by bot', applied_at=? WHERE id=?",
                 (db.now(), job_id))
            actx.log("APPLIED")
        except apply.Deferred as e:
            db.x("UPDATE jobs SET status='new', status_note=? WHERE id=?", (str(e), job_id))
            actx.log(str(e))
        except apply.Stopped:
            db.x("UPDATE jobs SET status='new', status_note='stopped by you' WHERE id=?", (job_id,))
            actx.log("stopped by you - job put back to New")
        except Submitted:
            db.x("UPDATE jobs SET status='applied', status_note='submitted by you', applied_at=? WHERE id=?",
                 (db.now(), job_id))
        except apply.NeedsInput as e:
            db.x("UPDATE jobs SET status='needs_input', status_note=? WHERE id=?", (str(e), job_id))
            actx.log(str(e), "warn")
        except apply.NeedsHuman as e:
            db.x("UPDATE jobs SET status='failed', status_note=? WHERE id=?", (f"needs manual: {e}", job_id))
            actx.log(f"needs manual: {e}", "warn")
        except Exception as e:
            db.x("UPDATE jobs SET status='failed', status_note=? WHERE id=?", (str(e)[:300], job_id))
            actx.log(f"error: {e}\n{traceback.format_exc()[-600:]}", "error")
        finally:
            self._close_own_tabs(page)

    def _scrape_listing(self, pid, url):
        """Read job cards from a search/listing page in your logged-in bot Chrome
        (jobright, LinkedIn, Dice, ZipRecruiter, Monster, or any other page you add)."""
        settings = db.get_settings(pid)
        profile = db.get_profile(pid)["data"]
        site = sites.site_of(url)
        label = sites.SITES[site]["label"] if site else url[:60]
        page = self._page(pid)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            wait_settle(page, 4000)
            if re.search(r"/(login|signin|sign-in|authwall|checkpoint|uas/login)", page.url, re.I):
                db.log(pid, f"{label}: you are not signed in. Click 'Open bot browser', sign in to {label} once, "
                            "then run Find jobs again.", "warn")
                return
            if captcha_or_block(page):
                page.bring_to_front()
                if not self.wait_for_user(pid, f"{label} is showing a robot check. Complete it in the bot Chrome window, "
                                               "then press Continue (or Skip to leave this site out)"):
                    db.log(pid, f"{label}: skipped (robot check not completed)", "warn")
                    return
                wait_settle(page, 3000)
                if captcha_or_block(page):
                    db.log(pid, f"{label}: still blocked by the robot check, skipping this search", "warn")
                    return
            for _ in range(8):                      # load more results (window and inner job lists)
                if self._stop:
                    return
                page.evaluate(SCROLL_JS)
                page.wait_for_timeout(1100)
            cards = page.evaluate(CARDS_JS, sites.SITES[site]["job_link"] if site else None)
            needs_sp = db.truthy(settings.get("needs_sponsorship"))
            exclude = [k.strip().lower() for k in settings.get("exclude_title_words", "").split(",") if k.strip()]
            added = 0
            for c in cards:
                href = sites.canonical(c["href"]) or c["href"]
                title, company = guess_title_company(c)
                if any(re.search(r"" + re.escape(e) + r"", (title or "").lower()) for e in exclude):
                    continue
                j = dict(url=href, apply_url=href, title=title, company=company, location=c.get("location"),
                         description=c["text"], ats=search.detect_ats(href))
                src = "listing" if j["ats"] in sites.BOARDS else "api"
                if search.already_applied(pid, company, title):
                    continue
                if search.save_job(pid, j, src, [], exclude, [], None, needs_sp, profile, force=True):
                    added += 1
            db.log(pid, f"{label}: {len(cards)} postings on the page, {added} new saved")
        finally:
            safe_close(page)


def safe_close(page):
    """Close a bot tab without closing Chrome: closing the last tab quits Chrome (and your sign-in in progress),
    so the last tab is turned into a blank tab instead."""
    try:
        if len([p for p in page.context.pages if not p.is_closed()]) <= 1:
            page.goto("about:blank")
        else:
            page.close()
    except Exception:
        pass


class Submitted(Exception):
    pass


CARDS_JS = r"""
(siteRe) => {
  const txt = el => (el ? (el.innerText || '') : '').trim();
  const site = siteRe ? new RegExp(siteRe) : null;
  const ats = /greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|icims\.com|smartrecruiters\.com|workable\.com|jobvite\.com/;
  const jobish = /\/jobs?\/(info|view|detail|details)?\/?[\w-]{4,}|\/job\/|\/careers?\/|\/positions?\/|currentJobId=/i;
  const seen = new Set(); const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href.split('#')[0];
    if (seen.has(href)) continue;
    const ok = site ? (site.test(href) || ats.test(href))
                    : (ats.test(href) || (jobish.test(href) && !/\/jobs\/(recommend|search|collections?)\b/.test(href)));
    if (!ok) continue;
    let card = a; for (let i = 0; i < 7 && card.parentElement; i++) { if (txt(card).length > 60) break; card = card.parentElement; }
    const t = txt(card);
    if (t.length < 15) continue;
    seen.add(href);
    out.push({href, anchor: txt(a).slice(0, 200), text: t.slice(0, 1500)});
  }
  return out.slice(0, 300);
}
"""

# scroll the window and any scrollable list (LinkedIn keeps results in an inner pane)
SCROLL_JS = r"""
() => {
  window.scrollBy(0, 3000);
  for (const el of document.querySelectorAll('ul, div, section, main')) {
    const cs = getComputedStyle(el);
    if (/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 50 && el.querySelector('a[href]'))
      el.scrollTop += 2500;
  }
  const more = [...document.querySelectorAll('button')].find(b => /^(see more jobs|show more|load more)/i.test((b.innerText || '').trim()));
  if (more) more.click();
}
"""


def captcha_or_block(page):
    """Robot checks (Cloudflare, DataDome, reCAPTCHA...). The bot never tries to get past these itself."""
    if captcha_visible(page):
        return True
    if any(k in (f.url or "") for f in page.frames for k in ("captcha-delivery.com", "challenges.cloudflare.com")):
        return True
    t = (page.title() + " " + page_text(page)[:2000]).lower()
    return any(k in t for k in ("verify you are human", "are you a robot", "unusual traffic", "press & hold",
                                "checking your browser", "access denied", "performing security verification",
                                "just a moment", "security check"))


def guess_title_company(c):
    lines = [l.strip() for l in c["text"].splitlines() if l.strip() and len(l.strip()) < 120]
    lines = [l for l in lines if not re.search(r"^(\d+\s*(h|d|m|hours?|days?|minutes?)\s*ago|new|apply|save|match|\d+%)", l, re.I)]
    title = c.get("anchor") if c.get("anchor") and 3 < len(c["anchor"]) < 120 else (lines[0] if lines else "")
    title = title.split("\n")[0]
    company = ""
    for l in lines:
        if l != title and not re.search(r"\$|remote|onsite|hybrid|full[- ]time|ago|applicants|,\s*[A-Z]{2}\b|\d", l, re.I):
            company = l
            break
    return title, company


worker = None


def get_worker():
    global worker
    if worker is None:
        worker = Worker()
    return worker
