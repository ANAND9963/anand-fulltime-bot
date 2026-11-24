"""Connects this PC to the online dashboard (Netlify).

Every few seconds it asks the site for anything you did online (apply, answer a question, upload a resume, run a
search...), runs it on this PC exactly as if you had clicked it here, and sends the reply back. It also uploads
the pages you look at (jobs, stats, answers...) so the online dashboard shows them even when this PC is off.
Secrets (API keys, mail password) are never uploaded: the settings page already masks them."""
import base64
import hashlib
import threading
import time
from urllib.parse import quote

import httpx

from . import db

JOB_FILTERS = ["", "new", "queued,applying", "needs_input,needs_human", "applied", "failed", "skipped"]

state = {"connected": False, "last_ok": 0, "last_error": "", "url": ""}
_started = False


def _config():
    row = lambda k: (db.q("SELECT value FROM settings WHERE key=? AND value != '' ORDER BY profile_id DESC LIMIT 1",
                          (k,), one=True) or {}).get("value", "")
    return row("online_url").rstrip("/"), row("online_password")


def _pages():
    """The UI's GET requests, spelled exactly like static/app.js builds them (they are the cache keys)."""
    out = ["/api/profiles"]
    for p in db.q("SELECT id FROM profiles ORDER BY id"):
        pid = p["id"]
        out += [f"/api/status/{pid}", f"/api/stats/{pid}", f"/api/profile/{pid}", f"/api/profile/{pid}/resumes",
                f"/api/settings/{pid}", f"/api/answers/{pid}", f"/api/pending/{pid}", f"/api/avoid/{pid}",
                f"/api/emails/{pid}", f"/api/setup/{pid}"]
        out += [f"/api/jobs/{pid}?status={quote(s, safe='')}&q=" for s in JOB_FILTERS]
    return out


class Relay:
    def __init__(self, app):
        from fastapi.testclient import TestClient
        self.local = TestClient(app)          # calls this PC's own API in-process
        self.sent = {}                        # cache key -> hash of what the site already has
        self.last_push = 0

    def run_local(self, method, path, ctype="", body=b""):
        headers = {"content-type": ctype} if ctype else {}
        r = self.local.request(method, path, content=body or None, headers=headers)
        return {"status": r.status_code, "ctype": r.headers.get("content-type", "application/json"),
                "body": base64.b64encode(r.content).decode()}

    def snapshot(self, force=False):
        cache = {}
        for path in _pages():
            try:
                res = self.run_local("GET", path)
            except Exception as e:
                db.log(0, f"online sync: {path}: {e}", "warn")
                continue
            if res["status"] != 200:
                continue
            h = hashlib.sha1(res["body"].encode()).hexdigest()
            if force or self.sent.get(path) != h:
                cache[path] = res
                self.sent[path] = h
        return cache

    def loop(self):
        client = httpx.Client(timeout=30)
        while True:
            url, pw = _config()
            if not url or not pw:
                state.update(connected=False, url="", last_error="not set up")
                time.sleep(15)
                continue
            if url != state["url"]:
                self.sent = {}
                state["url"] = url
            headers = {"x-app-password": pw}
            try:
                r = client.get(f"{url}/api/_worker/poll", headers=headers)
                if r.status_code == 401:
                    raise RuntimeError("wrong online password")
                r.raise_for_status()
                data = r.json()
                responses = []
                for q in data.get("requests", []):
                    try:
                        res = self.run_local(q["method"], q["path"], q.get("ctype", ""),
                                             base64.b64decode(q.get("body") or ""))
                    except Exception as e:
                        res = {"status": 500, "ctype": "application/json",
                               "body": base64.b64encode(f'{{"detail": "{e}"}}'.encode()).decode()}
                    responses.append({"id": q["id"], **res})
                    if q["method"] != "GET":
                        db.log(0, f"online: {q['method']} {q['path'].split('?')[0]} -> {res['status']}")
                active = data.get("ui_active")
                due = time.time() - self.last_push > (8 if active else 120)
                cache = self.snapshot() if (responses or due) else {}
                if responses or cache:
                    client.post(f"{url}/api/_worker/respond", headers=headers,
                                json={"responses": responses, "cache": cache}).raise_for_status()
                    self.last_push = time.time()
                state.update(connected=True, last_ok=time.time(), last_error="")
                time.sleep(1.5 if (active or responses) else 20)
            except Exception as e:
                if state.get("last_error") != str(e):
                    db.log(0, f"online dashboard: {e}", "warn")
                state.update(connected=False, last_error=str(e)[:200])
                time.sleep(30)


def start(app):
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=Relay(app).loop, daemon=True, name="online-relay").start()
