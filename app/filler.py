"""Generic form filler that works on any ATS page (or iframe).

1. JS scans the page and tags every visible field with data-jb-id, returning its question label, kind and options.
2. Python answers each question from: profile -> saved answers -> rules -> Claude (grounded in resume).
3. Required questions that still have no answer are saved as "pending" so the user answers them once in the UI;
   the answer is stored and reused on every later application."""
import re
import time

from . import answers

SCAN_JS = r"""
() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  let n = window.__jbN || 0;
  const tag = el => { if (!el.dataset.jbId) el.dataset.jbId = String(++n); window.__jbN = n; return el.dataset.jbId; };
  const LABELSEL = ':scope > label, :scope > legend, :scope > [class*="label" i], :scope > [class*="question" i], :scope > [class*="title" i], :scope > div > label, :scope > span > label';
  const labelOf = (el, skipOwn) => {
    let t = '';
    if (!skipOwn && el.labels && el.labels.length) t = txt(el.labels[0]);
    if (!t && el.getAttribute('aria-labelledby'))
      t = el.getAttribute('aria-labelledby').split(/\s+/).map(id => txt(document.getElementById(id))).join(' ').trim();
    if (!t && !skipOwn && el.getAttribute('aria-label')) t = el.getAttribute('aria-label');
    if (!t) {
      const fs = el.closest('fieldset'); const lg = fs && fs.querySelector('legend');
      if (lg) t = txt(lg);
    }
    if (!t) {
      let p = el.parentElement;
      for (let i = 0; i < 6 && p && !t; i++, p = p.parentElement) {
        for (const l of p.querySelectorAll(LABELSEL)) {
          if (!l.contains(el) && txt(l)) { t = txt(l); break; }
        }
      }
    }
    if (!t) t = el.placeholder || el.getAttribute('name') || el.id || '';
    return t.slice(0, 400);
  };
  const reqOf = (el, label) => !!(el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(label));
  const optLabel = el => {
    if (el.labels && el.labels.length) return txt(el.labels[0]);
    const pl = el.closest('label'); if (pl) return txt(pl);
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const sib = el.nextElementSibling; if (sib) return txt(sib);
    return el.value || '';
  };
  const out = []; const groups = {};
  document.querySelectorAll('input, textarea, select, button[aria-haspopup="listbox"]').forEach(el => {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (el.disabled || el.readOnly && type !== 'file') return;
    // hidden validation proxies used by react-select etc.
    if (type !== 'file' && type !== 'radio' && type !== 'checkbox' &&
        (el.getAttribute('aria-hidden') === 'true' || /requiredInput/i.test(el.className))) return;
    if (['hidden', 'submit', 'image', 'reset', 'search'].includes(type)) return;
    if (el.tagName === 'INPUT' && type === 'button') return;
    if (type === 'file') { out.push({id: tag(el), kind: 'file', label: (labelOf(el) + ' ' + (el.name || '') + ' ' + (el.id || '')).trim(),
                                     required: reqOf(el, labelOf(el)), filled: el.files && el.files.length > 0}); return; }
    if (type === 'radio' || type === 'checkbox') {
      const lab = el.labels && el.labels[0];
      if (!vis(el) && !vis(lab) && !vis(el.parentElement)) return;
      const key = type + ':' + (el.name || labelOf(el, true));
      const g = groups[key] || (groups[key] = {kind: type, els: []});
      g.els.push(el); return;
    }
    if (!vis(el)) return;
    const label = labelOf(el);
    if (el.tagName === 'BUTTON') {
      const v = txt(el);
      out.push({id: tag(el), kind: 'listbox', label, required: reqOf(el, label), filled: !!v && !/^select/i.test(v)});
      return;
    }
    if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') {
      const box = el.closest('[class*="container" i], [class*="select" i]') || el.parentElement;
      const sv = box && box.querySelector('[class*="singleValue" i], [class*="single-value" i], [class*="multiValue" i]');
      out.push({id: tag(el), kind: 'combo', label, required: reqOf(el, label), filled: !!(el.value || (sv && txt(sv)))});
      return;
    }
    if (el.tagName === 'SELECT') {
      const opts = [...el.options].map(o => o.text.trim()).filter(t => t && !/^(select|choose|--|please select)/i.test(t));
      out.push({id: tag(el), kind: 'select', label, required: reqOf(el, label), options: opts,
                filled: el.selectedIndex > 0 && !!el.value}); return;
    }
    out.push({id: tag(el), kind: el.tagName === 'TEXTAREA' ? 'textarea' : type, label, required: reqOf(el, label),
              filled: !!el.value, name: el.name || '', maxlength: el.maxLength});
  });
  for (const g of Object.values(groups)) {
    const first = g.els[0];
    const container = first.closest('fieldset') || first.closest('[role="radiogroup"], [role="group"]') || first.parentElement.parentElement;
    let label = '';
    if (g.els.length > 1 || g.kind === 'radio') {
      const lg = container && container.querySelector('legend');
      label = lg ? txt(lg) : labelOf(container.querySelector('input') ? first : first, true);
      if (!label || g.els.some(e => optLabel(e) === label)) {
        let p = container; for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
          const l = [...p.querySelectorAll(LABELSEL)].find(x => !g.els.some(e => x.contains(e)) && txt(x));
          if (l) { label = txt(l); break; }
        }
      }
    } else label = optLabel(first) || labelOf(first);
    out.push({kind: g.kind === 'radio' ? 'radio' : (g.els.length > 1 ? 'checkgroup' : 'checkbox'), label: label.slice(0, 400),
              required: g.els.some(e => e.required || e.getAttribute('aria-required') === 'true') || /\*/.test(label),
              options: g.els.map(optLabel), option_ids: g.els.map(tag), filled: g.els.some(e => e.checked), id: tag(first)});
  }
  // Yes/No style button groups (Ashby and others)
  document.querySelectorAll('div, fieldset').forEach(div => {
    const btns = [...div.querySelectorAll(':scope > button, :scope > div > button')].filter(b => vis(b) && b.type !== 'submit');
    if (btns.length < 2 || btns.length > 6) return;
    const texts = btns.map(txt);
    if (!texts.every(t => t && t.length < 40) || !texts.some(t => /^(yes|no)$/i.test(t))) return;
    if (btns.some(b => b.dataset.jbId && out.some(o => o.option_ids && o.option_ids.includes(b.dataset.jbId)))) return;
    let label = ''; let p = div;
    for (let i = 0; i < 4 && p && !label; i++, p = p.parentElement) {
      const l = [...p.querySelectorAll(LABELSEL)].find(x => !x.contains(div) && !btns.some(b => x.contains(b)) && txt(x));
      if (l) label = txt(l);
    }
    const sel = btns.some(b => /selected|active|checked/i.test(b.className) || b.getAttribute('aria-pressed') === 'true');
    out.push({kind: 'buttons', label, required: /\*/.test(label), options: texts, option_ids: btns.map(tag), filled: sel, id: tag(btns[0])});
  });
  return out;
}
"""

SUCCESS_RE = re.compile(r"thank(s| you)[^.]{0,30}(for applying|for your application|for your interest|application)|"
                        r"application (has been |was )?(successfully )?(submitted|received|sent)|"
                        r"we('ve| have) received your application|successfully (submitted|applied)|"
                        r"your application is (complete|in)", re.I)
ERROR_RE = re.compile(r"(this field is required|is required|please (complete|fill|select|enter)|can't be blank|required field)", re.I)


def clean_label(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").replace("*", "")).strip()


class Filler:
    def __init__(self, ctx):
        self.ctx = ctx  # ApplyContext from apply.py

    def scan(self, scope):
        try:
            return scope.evaluate(SCAN_JS)
        except Exception:
            return []

    def _options_of_popup(self, page):
        page.wait_for_timeout(700)
        opts = page.locator('[role="option"]:visible, [class*="option" i][id*="option" i]:visible')
        texts = []
        for i in range(min(opts.count(), 80)):
            try:
                t = opts.nth(i).inner_text(timeout=500).strip()
                if t:
                    texts.append(t)
            except Exception:
                pass
        return opts, texts

    def answer(self, f, job):
        """Find an answer for a field. Returns answer text or None."""
        label = clean_label(f.get("label"))
        if not label:
            return None
        opts = None if f["kind"] == "checkbox" else f.get("options")   # single checkbox: answer is yes/no
        ans, src = answers.lookup(self.ctx.pid, self.ctx.profile, label, opts)
        if ans is None and (f.get("required") or f["kind"] in ("select", "radio", "buttons", "listbox", "combo")):
            ans = answers.llm_answer(self.ctx.settings, self.ctx.profile, self.ctx.resume_text,
                                     self.ctx.saved_answers(), label, f["kind"], opts, job)
            if ans:
                answers.save(self.ctx.pid, label, ans, "llm")
                self.ctx.log(f"Claude answered '{label[:60]}' -> '{ans[:60]}' (saved, editable in Answers)")
        return ans

    def fill(self, scope, page, job, pending: list):
        """Fill every field we can in this scope. Unanswerable required fields are appended to pending."""
        fields = self.scan(scope)
        filled = 0
        for f in fields:
            self.ctx.check_stop()
            kind = f["kind"]
            loc = scope.locator(f'[data-jb-id="{f["id"]}"]').first
            label = clean_label(f.get("label"))
            try:
                if kind == "file":
                    if f.get("filled"):
                        continue
                    lab = label.lower()
                    if re.search(r"cover", lab):
                        if self.ctx.cover_letter_path():
                            loc.set_input_files(self.ctx.cover_letter_path())
                        continue
                    if re.search(r"resume|cv|attach|upload|file", lab) or not lab:
                        loc.set_input_files(self.ctx.resume_path)
                        filled += 1
                        page.wait_for_timeout(1500)
                    continue
                if f.get("filled"):
                    continue
                if kind in ("combo", "listbox") and not f.get("options"):
                    # open it to learn its options (helps both Claude and the user)
                    try:
                        loc.click(timeout=3000)
                        _, f["options"] = self._options_of_popup(page)
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                ans = self.answer(f, job)
                if ans is None:
                    if f.get("required"):
                        pending.append(dict(question=label, field_type=kind, options=f.get("options") or []))
                    continue
                if self._apply(scope, page, f, loc, ans):
                    filled += 1
                elif f.get("required"):
                    pending.append(dict(question=label, field_type=kind, options=f.get("options") or [],
                                        note=f"could not use answer '{ans}'"))
            except Exception as e:
                self.ctx.log(f"field '{label[:50]}' ({kind}) error: {str(e)[:150]}", "warn")
        return filled

    def _apply(self, scope, page, f, loc, ans):
        kind = f["kind"]
        opts = f.get("options") or []
        if kind in ("text", "email", "tel", "url", "number", "textarea", "date", "password", "input"):
            if kind == "number":
                m = re.search(r"\d+(\.\d+)?", ans.replace(",", ""))
                if not m:
                    return False
                ans = m.group(0)
            if f.get("maxlength") and 0 < f["maxlength"] < len(ans):
                ans = ans[: f["maxlength"]]
            loc.fill(ans, timeout=5000)
            return True
        if kind == "select":
            choice = answers.pick_option(ans, opts)
            if not choice:
                return False
            loc.select_option(label=choice, timeout=5000)
            return True
        if kind in ("radio", "buttons", "checkgroup"):
            wanted = [a.strip() for a in re.split(r";|\|", ans)] if kind == "checkgroup" else [ans]
            ok = False
            for w in wanted:
                choice = answers.pick_option(w, opts)
                if not choice:
                    continue
                oid = f["option_ids"][opts.index(choice)]
                el = scope.locator(f'[data-jb-id="{oid}"]').first
                try:
                    if kind == "buttons":
                        el.click(timeout=3000)
                    else:
                        el.check(force=True, timeout=3000)
                except Exception:
                    el.evaluate("e => e.click()")
                ok = True
            return ok
        if kind == "checkbox":
            if ans.strip().lower() in ("yes", "true", "1", "checked", "agree", "i agree", "y"):
                try:
                    loc.check(force=True, timeout=3000)
                except Exception:
                    loc.evaluate("e => e.click()")
            return True
        if kind in ("combo", "listbox"):
            loc.click(timeout=4000)
            if kind == "combo":
                loc.fill("", timeout=2000)
                loc.type(ans[:40], delay=30)
            opt_loc, texts = self._options_of_popup(page)
            choice = answers.pick_option(ans, texts) if texts else None
            if choice:
                opt_loc.nth(texts.index(choice)).click(timeout=3000)
                page.wait_for_timeout(400)
                return True
            if kind == "combo" and not texts:
                page.keyboard.press("Enter")
                return True
            page.keyboard.press("Escape")
            return False
        return False


def page_text(scope):
    try:
        return scope.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def best_form_scope(page):
    """The page or the iframe (e.g. embedded Greenhouse) holding the most form fields."""
    best, best_n = page, 0
    for fr in page.frames:
        try:
            n = fr.locator("input:visible, textarea:visible, select:visible").count()
        except Exception:
            continue
        if n > best_n:
            best, best_n = fr, n
    return best


def captcha_visible(page) -> bool:
    for fr in page.frames:
        u = fr.url or ""
        if ("recaptcha" in u and "bframe" in u) or "hcaptcha.com" in u and "challenge" in u or "challenges.cloudflare" in u:
            try:
                if fr.frame_element().is_visible():
                    return True
            except Exception:
                pass
    t = page_text(page)[:3000].lower()
    return "verify you are human" in t or "i'm not a robot" in t and "recaptcha" in t


def click_first(scope, patterns, timeout=4000):
    """Click the first visible button/link whose text matches one of the regexes."""
    for pat in patterns:
        rx = re.compile(pat, re.I)
        for sel in ("button", "a", "[role=button]", "input[type=submit]"):
            loc = scope.locator(sel).filter(has_text=rx) if sel != "input[type=submit]" else scope.locator(sel)
            try:
                n = loc.count()
            except Exception:
                continue
            for i in range(min(n, 10)):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    if sel == "input[type=submit]" and not rx.search(el.get_attribute("value") or ""):
                        continue
                    el.scroll_into_view_if_needed(timeout=2000)
                    el.click(timeout=timeout)
                    return True
                except Exception:
                    continue
    return False


def wait_settle(page, ms=1500):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def visible_errors(scope):
    t = page_text(scope)
    return [m.group(0) for m in ERROR_RE.finditer(t)][:5]


def now_ts():
    return time.time()
