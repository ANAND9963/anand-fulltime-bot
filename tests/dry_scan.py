"""Read-only: open a real application page, list detected fields and the answer the bot would use. Never submits."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright
from app import answers, db
from app.filler import SCAN_JS, best_form_scope, clean_label

url, pid = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1
profile = db.get_profile(pid)["data"]
with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True)
    p = b.new_page(); p.goto(url, wait_until="domcontentloaded"); p.wait_for_timeout(4000)
    scope = best_form_scope(p)
    for f in scope.evaluate(SCAN_JS):
        lab = clean_label(f.get("label"))
        ans, src = answers.lookup(pid, profile, lab, f.get("options")) if f["kind"] != "file" else ("<resume file>", "file")
        print(f"{f['kind']:9} {'REQ' if f.get('required') else '   '} {lab[:70]:70} -> {ans!r:.40} ({src}) {('opts=' + str(f.get('options'))[:60]) if f.get('options') else ''}")
    b.close()
