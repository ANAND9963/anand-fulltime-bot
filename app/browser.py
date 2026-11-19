"""Single background thread that owns the Playwright browser (persistent profile per user, so logins stick)."""
import queue
import re
import threading
import traceback
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

from . import apply, chrome, db, search
from .filler import page_text, wait_settle


class Worker:
    def __init__(self):
        self.q = queue.Queue()
        self.state = {"busy": False, "current": None, "message": "idle", "waiting_human": False}
        self._human = threading.Event()
        self._human_result = None
        self._stop = False
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
                    p.close()
        except Exception:
            pass

    def _run(self):
        with sync_playwright() as pw:
            self.pw = pw
            while True:
                kind, pid, arg = self.q.get()
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
        """Read job cards from a (logged-in) listing site like jobright.ai, LinkedIn, Indeed, Built In."""
        settings = db.get_settings(pid)
        profile = db.get_profile(pid)["data"]
        page = self._page(pid)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        wait_settle(page, 4000)
        if re.search(r"sign in|log in|login", page.url, re.I):
            db.log(pid, f"{url}: looks like you need to log in. Use 'Open browser' to sign in once, then scrape again.", "warn")
        for _ in range(8):     # infinite scroll
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1200)
        if self._stop:
            page.close()
            return
        cards = page.evaluate(CARDS_JS)
        db.log(pid, f"{url}: found {len(cards)} job links")
        needs_sp = db.truthy(settings.get("needs_sponsorship"))
        exclude = [k.strip().lower() for k in settings.get("exclude_title_words", "").split(",") if k.strip()]
        added = 0
        for c in cards:
            title, company = guess_title_company(c)
            if any(re.search(r"\b" + re.escape(e) + r"\b", (title or "").lower()) for e in exclude):
                continue
            j = dict(url=c["href"], apply_url=c["href"], title=title, company=company, location=c.get("location"),
                     description=c["text"], ats=search.detect_ats(c["href"]))
            src = "listing" if j["ats"] in ("jobright", "linkedin", "other") else "api"
            if search.save_job(pid, j, src, [], exclude, [], None, needs_sp, profile, force=True):
                added += 1
        db.log(pid, f"{url}: {added} new jobs saved")
        page.close()


class Submitted(Exception):
    pass


CARDS_JS = r"""
() => {
  const txt = el => (el ? (el.innerText || '') : '').trim();
  const ats = /greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|icims\.com|smartrecruiters\.com|workable\.com|jobvite\.com/;
  const jobish = /\/jobs?\/(info|view|detail|details)?\/?[\w-]{4,}|\/job\/|\/careers?\/|\/positions?\/|currentJobId=/i;
  const seen = new Set(); const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href.split('#')[0];
    if (seen.has(href)) continue;
    if (!(ats.test(href) || (jobish.test(href) && !/\/jobs\/(recommend|search|collections?)\b/.test(href)))) continue;
    let card = a; for (let i = 0; i < 6 && card.parentElement; i++) { if (txt(card).length > 60) break; card = card.parentElement; }
    const t = txt(card);
    if (t.length < 15) continue;
    seen.add(href);
    out.push({href, anchor: txt(a).slice(0, 200), text: t.slice(0, 1500)});
  }
  return out.slice(0, 300);
}
"""


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
