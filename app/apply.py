"""Application flows per ATS. All of them share the generic Filler; Workday gets its own multi-step driver."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import answers, db, email_reader, search, sites
from .filler import (SUCCESS_RE, Filler, best_form_scope, captcha_visible, click_first, page_text,
                     visible_errors, wait_settle)


class NeedsInput(Exception):
    pass


class NeedsHuman(Exception):
    pass


class Stopped(Exception):
    """User pressed Stop: abandon the current job without marking it failed."""


class ApplyContext:
    def __init__(self, pid, job, worker):
        self.pid = pid
        self.job = job
        self.worker = worker
        p = db.get_profile(pid)
        self.profile = p["data"]
        self.resume_path = p["resume_path"]
        self.resume_text = p["resume_text"] or ""
        self.settings = db.get_settings(pid)
        self._saved = None

    def saved_answers(self):
        if self._saved is None:
            self._saved = [{"q": r["question"], "a": r["answer"]} for r in
                           db.q("SELECT question, answer FROM answers WHERE profile_id=?", (self.pid,))]
        return self._saved

    def cover_letter_path(self):
        p = self.profile.get("cover_letter_path")
        return p if p and Path(p).exists() else None

    def log(self, msg, level="info"):
        db.log(self.pid, f"[job {self.job['id']} {self.job.get('company')}] {msg}", level)

    def check_stop(self):
        if getattr(self.worker, "_stop", False):
            raise Stopped()


def record_pending(ctx, pending):
    seen = set()
    for p in pending:
        k = answers.norm(p["question"])
        if k in seen or not k:
            continue
        seen.add(k)
        answers.add_pending(ctx.pid, ctx.job["id"], p["question"], p["field_type"], p.get("options"))
    qs = "; ".join(p["question"][:60] for p in pending[:5])
    raise NeedsInput(f"{len(seen)} question(s) need your answer: {qs}")


def check_human(ctx, page):
    if captcha_visible(page):
        ctx.worker.wait_human(ctx, "CAPTCHA on page - solve it in the bot browser, then press Continue in the UI")


def submit_and_confirm(ctx, page, scope, submit_patterns):
    ctx.check_stop()
    auto = db.truthy(ctx.settings.get("auto_submit"))
    if not auto:
        ctx.worker.wait_human(ctx, "Form is filled. Review it in the bot browser and click Submit yourself, then press "
                                   "'I submitted it' in the UI (auto-submit is off)")
        return True
    started = datetime.now(timezone.utc)
    if not click_first(scope, submit_patterns):
        raise NeedsHuman("could not find the submit button")
    wait_settle(page, 3000)
    for _ in range(3):
        check_human(ctx, page)
        scope = best_form_scope(page)
        text = page_text(scope) + "\n" + page_text(page)
        if SUCCESS_RE.search(text) or re.search(r"/(confirmation|thank|thanks|success)", page.url):
            return True
        # Greenhouse (and some others) email a security code before accepting the application
        if re.search(r"security code|verification code|enter (the )?code", text, re.I):
            code = email_reader.wait_for_code(ctx.settings, started, hint=ctx.job.get("company") or "")
            if not code:
                raise NeedsHuman("waiting for email verification code timed out (is IMAP set up in Settings?)")
            ctx.log("got email verification code")
            boxes = scope.locator('input[maxlength="1"]:visible')
            if boxes.count() >= len(code):
                for i, ch in enumerate(code):
                    boxes.nth(i).fill(ch)
            else:
                box = scope.locator('input[id*="code" i]:visible, input[name*="code" i]:visible, '
                                    'input[aria-label*="code" i]:visible').first
                box.fill(code)
            click_first(scope, submit_patterns + [r"verify", r"confirm"])
            wait_settle(page, 3000)
            continue
        errs = visible_errors(scope)
        if errs:
            pending = []
            Filler(ctx).fill(scope, page, ctx.job, pending)
            if pending:
                record_pending(ctx, pending)
            click_first(scope, submit_patterns)
            wait_settle(page, 3000)
            continue
        wait_settle(page, 3000)
    if SUCCESS_RE.search(page_text(page)):
        return True
    raise NeedsHuman("submitted but no confirmation detected - please check the page")


SUBMIT = [r"^\s*submit( application| my application)?\s*$", r"submit application", r"^\s*apply( now)?\s*$",
          r"send application", r"^\s*submit\s*$"]


def generic_form(ctx, page, open_patterns=(r"^\s*apply( now| for this (job|position))?\s*$", r"apply now", r"i'?m interested")):
    """Single-page application forms: Greenhouse, Lever, Ashby, SmartRecruiters, Workable, most others."""
    wait_settle(page)
    scope = best_form_scope(page)
    n_fields = scope.locator("input:visible, textarea:visible").count() if scope else 0
    if n_fields < 3:
        click_first(page, list(open_patterns))
        wait_settle(page, 2500)
        if len(page.context.pages) > 1 and page.context.pages[-1] != page:
            page = page.context.pages[-1]
            wait_settle(page)
        scope = best_form_scope(page)
    check_human(ctx, page)
    if scope.locator('input[type="password"]:visible').count() and search.detect_ats(page.url) != "workday":
        raise NeedsHuman("this site wants an account login - sign in in the bot browser, then press Continue")
    filler = Filler(ctx)
    pending = []
    for _ in range(2):           # second pass catches fields revealed by earlier answers
        filler.fill(scope, page, ctx.job, pending)
        page.wait_for_timeout(800)
    if pending:
        record_pending(ctx, pending)
    return submit_and_confirm(ctx, page, scope, SUBMIT)


# ---------------------------------------------------------------- Workday
WD = lambda aid: f'[data-automation-id="{aid}"]'


def _wd_click(page, aid, timeout=5000):
    loc = page.locator(WD(aid)).first
    if loc.count() and loc.is_visible():
        try:
            loc.click(timeout=timeout)
        except Exception:
            loc.click(timeout=timeout, force=True)
        return True
    return False


def workday(ctx, page):
    wait_settle(page, 2500)
    pw = ctx.settings.get("ats_account_password")
    user = ctx.profile.get("email") or ctx.settings.get("imap_user")
    if not pw:
        raise NeedsHuman("Set 'ATS account password' in Settings so the bot can sign in / create Workday accounts")
    if not page.locator(WD("signInSubmitButton") + "," + WD("createAccountSubmitButton") + "," +
                        WD("bottom-navigation-next-button")).count():
        _wd_click(page, "adventureButton") or click_first(page, [r"^\s*apply\s*$"])
        wait_settle(page, 2000)
        _wd_click(page, "applyManually") or click_first(page, [r"apply manually"])
        wait_settle(page, 2500)

    # --- sign in, or create the account ---
    def sign_in():
        page.locator(WD("email")).first.fill(user)
        page.locator(WD("password")).first.fill(pw)
        if not _wd_click(page, "signInSubmitButton"):
            click_first(page, [r"^\s*sign in\s*$"])
        wait_settle(page, 3500)

    if page.locator(WD("signInLink")).count() and not page.locator(WD("email")).count():
        _wd_click(page, "signInLink")
        wait_settle(page)
    if page.locator(WD("password")).count() and not page.locator(WD("verifyPassword")).count():
        sign_in()
        if page.locator(WD("password")).count() and not page.locator(WD("bottom-navigation-next-button")).count():
            ctx.log("Workday sign-in failed, creating account")
            _wd_click(page, "createAccountLink") or click_first(page, [r"create account"])
            wait_settle(page)
    if page.locator(WD("verifyPassword")).count():
        started = datetime.now(timezone.utc)
        page.locator(WD("email")).first.fill(user)
        page.locator(WD("password")).first.fill(pw)
        page.locator(WD("verifyPassword")).first.fill(pw)
        cb = page.locator(WD("createAccountCheckbox"))
        if cb.count():
            cb.first.check(force=True)
        _wd_click(page, "createAccountSubmitButton") or click_first(page, [r"create account"])
        wait_settle(page, 4000)
        check_human(ctx, page)
        if re.search(r"verify|verification", page_text(page), re.I) or page.locator(WD("password")).count():
            link = email_reader.wait_for_link(ctx.settings, started, "myworkday")
            if link:
                ctx.log("opening Workday verification link")
                vp = page.context.new_page()
                vp.goto(link)
                wait_settle(vp, 3000)
                vp.close()          # page is still open, so Chrome keeps running
                page.reload()
                wait_settle(page, 3000)
            if page.locator(WD("password")).count():
                sign_in()
    if page.locator(WD("password")).count():
        raise NeedsHuman("Workday login did not succeed - finish sign-in in the bot browser, then press Continue")

    # --- multi-step wizard ---
    filler = Filler(ctx)
    last_title = None
    same = 0
    for step in range(12):
        ctx.check_stop()
        wait_settle(page, 2500)
        check_human(ctx, page)
        title = ""
        try:
            title = page.locator(WD("progressBarActiveStep") + ", h2").first.inner_text(timeout=3000)
        except Exception:
            pass
        same = same + 1 if title == last_title else 0
        last_title = title
        if same >= 2:
            errs = page.locator(WD("errorMessage") + "," + WD("inputAlert")).all_inner_texts()
            raise NeedsHuman(f"stuck on Workday step '{title}': {'; '.join(errs)[:200]}")
        ctx.log(f"Workday step: {title.strip()[:60]}")
        if page.locator(WD("file-upload-input-ref")).count() and not page.locator(WD("file-upload-successful")).count():
            try:
                page.locator(WD("file-upload-input-ref")).first.set_input_files(ctx.resume_path)
                page.wait_for_timeout(4000)
            except Exception:
                pass
        pending = []
        filler.fill(page, page, ctx.job, pending)
        if pending:
            record_pending(ctx, pending)
        if re.search(r"review", title, re.I) or page.locator(WD("pageFooterSubmitButton")).count() or \
                page.locator("button").filter(has_text=re.compile(r"^\s*submit\s*$", re.I)).count():
            return submit_and_confirm(ctx, page, page, [r"^\s*submit\s*$"])
        if not _wd_click(page, "bottom-navigation-next-button") and not click_first(page, [r"save and continue", r"^\s*next\s*$", r"^\s*continue\s*$"]):
            raise NeedsHuman(f"no Next button on Workday step '{title}'")
    raise NeedsHuman("Workday flow took too many steps")


# ---------------------------------------------------------------- dispatcher
def resolve_external(ctx, page):
    """For listing sites (jobright, linkedin...) click Apply and follow to the company's ATS page."""
    wait_settle(page, 2500)
    before = set(page.context.pages)
    with_popup = None
    try:
        with page.context.expect_page(timeout=8000) as pinfo:
            if not click_first(page, [r"apply (now|on company site|on employer site|externally)", r"^\s*apply\s*$",
                                      r"apply with autofill", r"original job post", r"apply"]):
                raise RuntimeError("no apply button")
        with_popup = pinfo.value
    except Exception:
        pass
    target = with_popup or page
    wait_settle(target, 3000)
    new_pages = [p for p in page.context.pages if p not in before]
    if not with_popup and new_pages:
        target = new_pages[-1]
    return target


POPUP = '[role="dialog"], [aria-modal="true"], .artdeco-modal, .jobs-easy-apply-modal'
NEXT = [r"^\s*next\s*$", r"^\s*continue( to next step)?\s*$", r"^\s*review( your application)?\s*$",
        r"^\s*save (and|&) continue\s*$", r"^\s*continue to review\s*$", r"^\s*proceed\s*$"]
FINAL = [r"^\s*submit( your)?( application)?\s*$", r"^\s*send application\s*$", r"^\s*apply now\s*$"]


class Deferred(Exception):
    """Not applied now (e.g. daily limit); the job stays New."""


def _visible_popup(page):
    loc = page.locator(POPUP)
    for i in range(loc.count() - 1, -1, -1):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return None


def _has_button(scope, patterns):
    for pat in patterns:
        b = scope.locator("button, [role=button], input[type=submit]").filter(has_text=re.compile(pat, re.I))
        for k in range(min(b.count(), 6)):
            if b.nth(k).is_visible() and b.nth(k).is_enabled():
                return True
    return False


def _applied_marker(page):
    """'Applied' badge/button that boards show after a one-click application."""
    m = page.locator("button, span, div").filter(has_text=re.compile(r"^\s*(applied|application submitted|"
                                                                      r"you applied|applied \d+ \w+ ago)\s*$", re.I))
    return any(m.nth(k).is_visible() for k in range(min(m.count(), 5)))


def check_daily_limit(ctx, site):
    cap = int(ctx.settings.get("max_per_site_per_day") or 0)
    if cap <= 0:
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = db.q("SELECT COUNT(*) n FROM jobs WHERE profile_id=? AND status='applied' AND applied_at LIKE ? AND url LIKE ?",
             (ctx.pid, today + "%", f"%{sites.SITES[site]['domain']}%"), one=True)["n"]
    if n >= cap:
        raise Deferred(f"daily limit reached for {sites.SITES[site]['label']} ({n}/{cap}); will try again tomorrow")


def board_apply(ctx, page, site):
    """Use the board's own quick-apply (LinkedIn Easy Apply, Dice Easy Apply, ZipRecruiter 1-Click, Monster Quick
    Apply). Returns (True, page) when applied here, (False, page) when the board sent us to the company's site,
    (None, page) when there is no quick-apply button (caller then follows the normal Apply link)."""
    s = sites.SITES[site]
    if not s["quick_apply"]:
        return None, page
    wait_settle(page, 2500)
    if _applied_marker(page) and not _has_button(page, s["quick_apply"]):
        return True, page                                   # already applied on this board earlier
    before = set(page.context.pages)
    if not click_first(page, s["quick_apply"]):
        return None, page
    wait_settle(page, 3000)
    new_pages = [p for p in page.context.pages if p not in before]
    if new_pages:
        page = new_pages[-1]
        wait_settle(page, 2500)
    if sites.site_of(page.url) != site:                    # it was an external "Apply" after all
        return False, page
    if re.search(r"/(login|signin|sign-in|authwall|checkpoint)", page.url, re.I):
        raise NeedsHuman(f"sign in to {s['label']} in the bot browser, then press Continue")

    filler = Filler(ctx)
    last_sig, same = None, 0
    for step in range(15):
        ctx.check_stop()
        check_human(ctx, page)
        popup = _visible_popup(page)
        scope = popup or page
        text = page_text(page)
        if SUCCESS_RE.search(text) or _applied_marker(page):
            click_first(page, [r"^\s*(done|close|dismiss|not now)\s*$"], timeout=2000)
            return True, page
        pending = []
        filler.fill(page, page, ctx.job, pending, root=POPUP if popup else None)
        if pending:
            record_pending(ctx, pending)
        sig = (page.url, (scope.inner_text(timeout=3000) if popup else text)[:400])
        same = same + 1 if sig == last_sig else 0
        last_sig = sig
        if same >= 2:
            errs = visible_errors(scope)
            raise NeedsHuman(f"stuck on a {s['label']} apply step: {'; '.join(errs)[:200] or 'no Next/Submit button'}")
        if _has_button(scope, FINAL):
            submit_and_confirm(ctx, page, scope, FINAL)
            click_first(page, [r"^\s*(done|close|dismiss|not now)\s*$"], timeout=2000)
            return True, page
        if not click_first(scope, NEXT):
            if SUCCESS_RE.search(page_text(page)) or _applied_marker(page):
                return True, page
            raise NeedsHuman(f"no Next/Submit button on the {s['label']} application")
        page.wait_for_timeout(1500)
    raise NeedsHuman(f"{s['label']} application took too many steps")


def apply_job(ctx, page):
    job = ctx.job
    url = job.get("apply_url") or job["url"]
    ats = search.detect_ats(url)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    site = sites.site_of(url)
    if job.get("source") == "listing" and (site or ats == "other"):
        handled, target = None, page
        if site:
            check_daily_limit(ctx, site)
            if db.truthy(ctx.settings.get("needs_sponsorship")) and search.sponsorship_of(page_text(page)) == "no":
                db.x("UPDATE jobs SET status='skipped', status_note='posting says no visa sponsorship' WHERE id=?",
                     (job["id"],))
                return "skipped"
            handled, target = board_apply(ctx, page, site)
            if handled:
                ctx.log(f"applied through {sites.SITES[site]['label']}")
                return True
        page = target if handled is False else resolve_external(ctx, page)
        ats = search.detect_ats(page.url)
        ctx.log(f"external application page: {page.url[:120]} ({ats})")
        db.x("UPDATE jobs SET apply_url=?, ats=? WHERE id=?", (page.url, ats, job["id"]))
        if sites.site_of(page.url):
            raise NeedsHuman("could not find the company's application page from the job board - open it and press Continue")
    # description check (sponsorship / avoid) now that we are on the real page
    text = page_text(page)
    if db.truthy(ctx.settings.get("needs_sponsorship")) and search.sponsorship_of(text) == "no":
        db.x("UPDATE jobs SET status='skipped', status_note='posting says no visa sponsorship' WHERE id=?", (job["id"],))
        return "skipped"
    if ats == "workday":
        return workday(ctx, page)
    if ats == "lever" and not re.search(r"/apply", page.url):
        page.goto(page.url.rstrip("/") + "/apply", wait_until="domcontentloaded")
    if ats == "ashby" and not page.url.rstrip("/").endswith("/application"):
        page.goto(page.url.split("?")[0].rstrip("/") + "/application", wait_until="domcontentloaded")
    return generic_form(ctx, page)
