"""Runs the user's real Chrome (or Edge) with a dedicated profile per bot user.

Why not let Playwright launch the browser? A Playwright-launched browser carries automation flags, and Google
sign-in (used by jobright, LinkedIn, ...) refuses to log in there. A normally started Chrome with a remote
debugging port behaves like any other Chrome: you sign in yourself, the profile keeps the login, and the
bot attaches to it only when it needs to fill a form."""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import db

_lock = threading.Lock()

CANDIDATES = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
]


def find_browser():
    row = db.q("SELECT value FROM settings WHERE key='chrome_path' AND value != '' LIMIT 1", one=True)
    custom = row["value"] if row else None
    for c in ([custom] if custom else []) + CANDIDATES:
        p = os.path.expandvars(c)
        if p and Path(p).is_file():
            return p
    return None


def port_for(pid: int) -> int:
    return 9300 + int(pid)


def profile_dir(pid: int) -> Path:
    return db.DATA / "browser" / f"profile_{pid}"


def is_running(pid: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port_for(pid)}/json/version", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _restore_session_pref(udir: Path):
    """'Continue where you left off' makes Chrome keep session-only login cookies across restarts."""
    prefs = udir / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text(encoding="utf-8")) if prefs.exists() else {}
    except Exception:
        return
    session = data.setdefault("session", {})
    if session.get("restore_on_startup") == 1:
        return
    session["restore_on_startup"] = 1
    prefs.parent.mkdir(parents=True, exist_ok=True)
    prefs.write_text(json.dumps(data), encoding="utf-8")


def ensure_running(pid: int, url: str | None = None, headless: bool = False) -> bool:
    """Start Chrome for this profile if needed. If it is already running and url is given, open url in a new tab.
    Returns False when no Chrome/Edge is installed."""
    with _lock:
        port = port_for(pid)
        if is_running(pid):
            if url:
                try:
                    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe=':/?&=%#')}",
                                                 method="PUT")
                    urllib.request.urlopen(req, timeout=5).close()
                except Exception as e:
                    db.log(pid, f"could not open tab: {e}", "warn")
            return True
        exe = find_browser()
        if not exe:
            return False
        udir = profile_dir(pid)
        udir.mkdir(parents=True, exist_ok=True)
        _restore_session_pref(udir)
        args = [exe, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={udir}", "--no-first-run", "--no-default-browser-check", "--start-maximized"]
        if headless:
            args.append("--headless=new")
        if url:
            args.append(url)
        flags = 0
        if os.name == "nt":   # keep Chrome open even if the bot's console window is closed
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
        for _ in range(60):
            if is_running(pid):
                db.log(pid, f"Bot Chrome started ({Path(exe).name}, profile {udir.name})")
                return True
            time.sleep(0.5)
        db.log(pid, "Chrome started but its debugging port did not open. Close every window of the bot "
                    "Chrome profile and try again.", "error")
        return False
