"""Checks the bot Chrome is not flagged as automated and that logins survive a Chrome restart.
Uses a throwaway profile (99) and a local page that sets a persistent cookie + localStorage, like a site login."""
import shutil, sys, threading, time, pathlib, urllib.request, json
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright
from app import chrome

PID = 99
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        if self.path == "/login":
            self.send_header("Set-Cookie", "auth=token123; Max-Age=2592000; Path=/")
        self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(b"<script>if(location.pathname=='/login')localStorage.setItem('user','anand')</script>ok")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 8799), H); threading.Thread(target=srv.serve_forever, daemon=True).start()

def quit_chrome():
    ws = json.load(urllib.request.urlopen(f"http://127.0.0.1:{chrome.port_for(PID)}/json/version"))
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{chrome.port_for(PID)}")
        b.new_browser_cdp_session().send("Browser.close")      # like clicking X: Chrome saves its profile
    for _ in range(40):
        if not chrome.is_running(PID): return
        time.sleep(0.25)

shutil.rmtree(chrome.profile_dir(PID), ignore_errors=True)
try:
    assert chrome.ensure_running(PID, headless=True)
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{chrome.port_for(PID)}")
        p = b.contexts[0].new_page(); p.goto("http://127.0.0.1:8799/login")
        print("navigator.webdriver =", p.evaluate("navigator.webdriver"))
    quit_chrome(); print("chrome closed:", not chrome.is_running(PID)); time.sleep(1)
    prefs = json.loads((chrome.profile_dir(PID) / "Default" / "Preferences").read_text(encoding="utf-8"))
    print("restore_on_startup =", prefs.get("session", {}).get("restore_on_startup"))
    assert chrome.ensure_running(PID, headless=True)
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{chrome.port_for(PID)}")
        p = b.contexts[0].new_page(); p.goto("http://127.0.0.1:8799/home")
        cookies = {c["name"]: c["value"] for c in b.contexts[0].cookies()}
        print("after restart: cookie auth =", cookies.get("auth"), "| localStorage user =", p.evaluate("localStorage.getItem('user')"))
        assert cookies.get("auth") == "token123" and p.evaluate("localStorage.getItem('user')") == "anand"
    print("LOGIN PERSISTS: OK")
finally:
    if chrome.is_running(PID): quit_chrome()
    time.sleep(1); shutil.rmtree(chrome.profile_dir(PID), ignore_errors=True); srv.shutdown()
