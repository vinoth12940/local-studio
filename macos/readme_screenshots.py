"""The README's screenshots, taken from an isolated demo server with demo content only, so they can be retaken each release.

Nothing here touches your own Local Studio: the server below has its own home folder, search and voice ports, and is
attach-only, so it never starts a chat engine. Every chat, file and answer shown is demo content written by this script
(the files come from macos/make_search_samples.py). The header is shown as "Ready" by answering the page's engine
health poll in the browser; the UI itself is the real one.

  python3 macos/make_search_samples.py build/search_samples          # once
  mkdir -p build/readme-state/data && echo '{"chat_model": "qwen3.6-35b-a3b", "search": {"enabled": true,
    "on_battery": true}, "voice": {"enabled": true}}' > build/readme-state/data/settings.json
  shasum -a 256 search-requirements.txt | cut -d' ' -f1 > build/readme-state/data/search-deps.ok
  LOCAL_STUDIO_HOME=build/readme-state LOCAL_STUDIO_SEARCH_PORT=8096 LOCAL_STUDIO_VOICE_PORT=8097 \\
    LOCAL_STUDIO_SEARCH_ROOTS=$PWD/build/search_samples/files LOCAL_STUDIO_UI_PORT=7872 LOCAL_STUDIO_EVAL_ATTACH=1 \\
    uv run --no-project --python 3.12 --with-requirements requirements-app.txt uvicorn app:app --port 7872
  curl -X POST http://127.0.0.1:7872/api/search/start                 # and generate a few images in its Images view
  uv run --no-project --python 3.12 --with playwright==1.63.0 python macos/readme_screenshots.py

It drives the installed Google Chrome (no browser download) and writes docs/screenshots/*.jpg.
"""
import argparse
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "screenshots"
VIEWPORT = {"width": 1440, "height": 900}

DEBOUNCE_FIXED = '''import time


def debounce(wait):
    """Decorator: ignore calls that arrive less than `wait` seconds after the last one that ran."""
    def deco(fn):
        last = [float("-inf")]

        def inner(*args, **kwargs):
            now = time.monotonic()
            if now - last[0] >= wait:
                last[0] = now
                return fn(*args, **kwargs)
        return inner
    return deco
'''
DEBOUNCE_BUGGY = DEBOUNCE_FIXED.replace('last = [float("-inf")]', "last = [0.0]").replace("                last[0] = now\n", "")
TEST = '''from src.debounce import debounce


def test_first_call_runs():
    calls = []
    f = debounce(10)(lambda: calls.append(1))
    f()
    assert calls == [1]


def test_quick_second_call_is_ignored():
    calls = []
    f = debounce(10)(lambda: calls.append(1))
    f(); f()
    assert calls == [1]


def test_returns_the_result():
    assert debounce(10)(lambda: 42)() == 42
'''
EDIT_DIFF = """--- src/debounce.py
+++ src/debounce.py
@@ -6,10 +6,11 @@
     def deco(fn):
-        last = [0.0]
+        last = [float("-inf")]

         def inner(*args, **kwargs):
             now = time.monotonic()
             if now - last[0] >= wait:
+                last[0] = now
                 return fn(*args, **kwargs)"""


def step(i, name, summary, detail="", **extra):
    return {"id": f"s{i}", "name": name, "summary": summary, "status": "done", "detail": detail, **extra}


def chats(workspace):
    """Demo conversations: the first is the hero, the second is the Code view's; the rest fill the sidebar."""
    hero = [
        {"role": "user", "content": "When does the washing machine warranty run out, and is my car insurance due soon?"},
        {"role": "assistant", "rounds": [
            {"reasoning": "These are the user's own documents, so I search their files on this Mac rather than the web, "
                          "then read the warranty to get the exact purchase date.", "thoughtSecs": 2,
             "tools": [step(1, "search_files", "Search my files: washing machine warranty",
                            "warranty.pdf · page 1 · Bosch Series 6 washing machine, model WAU28T64\nbudget.xlsx · Utilities"),
                       step(2, "search_files", "Search my files: car insurance renewal",
                            "car_insurance.txt · Policy renewal notice: comprehensive cover for the 2021 Mazda CX-5"),
                       step(3, "read_file", "Read: warranty.pdf",
                            "Warranty certificate\nProduct: Bosch Series 6 washing machine, model WAU28T64.\n"
                            "The warranty period is 24 months from the date of purchase, 3 March 2026.")]}],
         "content": "**Washing machine:** your Bosch Series 6 (WAU28T64) was bought on **3 March 2026** with a "
                    "**24-month** warranty, so it runs until **3 March 2028**. It covers parts and labour for "
                    "manufacturing faults; keep the certificate with your receipt.\n\n"
                    "**Car insurance:** the comprehensive policy for your 2021 Mazda CX-5 renews on **14 January**.\n\n"
                    "| | Date | Details |\n|---|---|---|\n| Washing machine warranty ends | 3 Mar 2028 | Parts and labour |\n"
                    "| Car insurance renews | 14 Jan | $1,180 a year, $750 excess |\n\n"
                    "Both answers come from your files: `warranty.pdf` and `car_insurance.txt`.",
         "stats": {"decode_tokens_per_sec": 61.4, "ceiling_tokens_per_sec": 23.8, "accept_len": 3.4, "ttft_seconds": 0.41,
                   "prompt_tokens": 5120, "completion_tokens": 164, "context_tokens": 5284}}]
    code = [
        {"role": "user", "content": "The debounce test fails. Find the bug, fix it, and run the tests."},
        {"role": "assistant", "rounds": [
            {"reasoning": "Run the tests first to see the failure, then read the decorator.", "thoughtSecs": 1,
             "tools": [step(1, "run_shell", "Bash: python -m pytest -q",
                            "$ python -m pytest -q\nFAILED tests/test_debounce.py::test_quick_second_call_is_ignored\n"
                            "1 failed, 2 passed in 0.05s\n(exit code 1)"),
                       step(2, "read_file", "Read: src/debounce.py", "\n".join(f"{i:>3}  {line}" for i, line in
                                                                         enumerate(DEBOUNCE_FIXED.splitlines(), 1)))]},
            {"note": "`inner` never records when a call ran, so every call goes through. It should store the time, and "
                     "start from minus infinity so the first call always runs.",
             "tools": [step(3, "edit_file", "Update: src/debounce.py", EDIT_DIFF, syntax_check="OK", syntax_ok=True),
                       step(4, "run_shell", "Bash: python -m pytest -q", "$ python -m pytest -q\n3 passed in 0.04s\n(exit code 0)")]}],
         "content": "Fixed. `inner` now records the time of each call that runs (`last[0] = now`), and the first call "
                    "always runs because the clock starts at minus infinity. All 3 tests pass.",
         "stats": {"decode_tokens_per_sec": 58.2, "ceiling_tokens_per_sec": 23.8, "accept_len": 3.2, "ttft_seconds": 0.38,
                   "prompt_tokens": 7240, "completion_tokens": 72, "context_tokens": 7312}}]
    filler = [("Kyoto itinerary for cherry blossom week", "Plan four days in Kyoto in early April."),
              ("What the lease says about pets", "Can I keep a cat under my lease?"),
              ("Lasagna for six: shopping list", "Scale grandma's lasagna to six people."),
              ("Summarize today's standup", "What did we agree in the team sync?")]
    out = [("a1b2c3d4e5f60001", "Warranty and car insurance dates", hero, None),
           ("a1b2c3d4e5f60002", "Fix the failing debounce test", code, str(workspace))]
    for n, (title, question) in enumerate(filler, 3):
        out.append((f"a1b2c3d4e5f600{n:02d}", title, [{"role": "user", "content": question},
                                                      {"role": "assistant", "content": "…"}], None))
    return out


def seed(url, home):
    """Demo chats through the server's own API, and a demo project with one commit plus the agent's uncommitted fix."""
    workspace = Path("/tmp/demo-project").resolve()
    shutil.rmtree(workspace, ignore_errors=True)
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src" / "__init__.py").write_text("")
    (workspace / "src" / "debounce.py").write_text(DEBOUNCE_BUGGY)
    (workspace / "tests" / "test_debounce.py").write_text(TEST)
    (workspace / "README.md").write_text("# demo-project\n\nA tiny debounce decorator and its tests.\n")
    (workspace / "AGENTS.md").write_text("Run the tests with `python -m pytest -q` before saying a change works.\n")
    git = ["git", "-c", "user.name=Demo", "-c", "user.email=demo@example.com", "-c", "init.defaultBranch=main"]
    subprocess.run(git + ["init", "-q"], cwd=workspace, check=True)
    subprocess.run(git + ["add", "."], cwd=workspace, check=True)
    subprocess.run(git + ["commit", "-q", "-m", "Add a debounce decorator"], cwd=workspace, check=True)
    (workspace / "src" / "debounce.py").write_text(DEBOUNCE_FIXED)           # the agent's edit, not yet committed
    for cid, title, messages, ws in reversed(chats(workspace)):         # saved last = newest: the hero tops the list
        body = {"title": title, "messages": messages, **({"workspace": ws} if ws else {})}
        req = urllib.request.Request(f"{url}/api/chats/{cid}", data=json.dumps(body).encode(), method="PUT",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        time.sleep(0.05)
    return workspace


def ready_engine(page):
    """The page polls the engine's health; answer it as a loaded engine would, so the header reads "Ready"."""
    health = {"status": "ok", "mode": "text", "model_id": "qwen3.6-35b-a3b", "model_name": "Qwen3.6 35B-A3B"}
    page.route("**/api/llm/health", lambda route: route.fulfill(json=health))
    page.route("**/api/llm/metrics", lambda route: route.fulfill(json={}))
    # Voice: the page sees a running service; every other voice request is held unanswered, so opening the Voice view
    # never installs or starts the service (or a microphone).
    page.route("**/api/voice/**", lambda route: None)
    page.route("**/api/voice/status", lambda route: route.fulfill(json={"available": True, "installed": True, "running": True,
                                                                         "setup": {"status": "idle", "step": None, "error": None}}))


def save(page, name, tmp):
    png = Path(tmp) / f"{name}.png"
    page.screenshot(path=str(png))
    OUT.mkdir(parents=True, exist_ok=True)
    subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "82", "-Z", "2000", str(png),
                    "--out", str(OUT / f"{name}.jpg")], check=True, capture_output=True)
    print(f"   {OUT.relative_to(REPO) / (name + '.jpg')}  {(OUT / (name + '.jpg')).stat().st_size // 1024} KB")


def open_page(browser, url, scheme):
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=2, color_scheme=scheme)
    page = context.new_page()
    ready_engine(page)
    page.goto(url)
    page.wait_for_function("() => typeof window.LS?.showView === 'function'")
    page.evaluate("localStorage.clear()")
    page.reload()
    page.wait_for_function("() => typeof window.LS?.showView === 'function'")
    page.wait_for_timeout(1500)
    return context, page


def chat_shot(page, cid, my_files=False):
    page.evaluate(f"LS.openChat('{cid}')")
    page.wait_for_timeout(1200)
    if my_files:
        page.click("#filesPill")
    page.evaluate("document.querySelectorAll('.turn.bot details.chattoolstep').forEach((d, i) => { d.open = i === 2 })")
    page.evaluate("document.querySelector('#chatScroll')?.scrollTo(0, 0)")
    page.wait_for_timeout(300)


def voice_shot(page):
    page.evaluate("LS.showView('voice')")
    page.wait_for_timeout(800)
    page.evaluate("""() => {
      const $ = (id) => document.getElementById(id);
      $('vv').dataset.state = 'speaking'; $('vvState').textContent = 'Speaking';
      $('vvGo').className = 'vv-btn end'; $('vvGoTxt').textContent = 'End'; $('vvGoIcon').setAttribute('href', '#i-x');
      $('vvAsk').textContent = 'When do I land in Tokyo, and how long am I in Shinjuku?';
      $('vvSay').textContent = 'You land at Haneda on the third of April, then spend four nights in Shinjuku before the bullet train to Kyoto.';
      const shape = [0.35, 0.55, 0.75, 0.9, 1, 0.9, 0.75, 0.55, 0.35], wobble = [0.9, 1.1, 0.8, 1, 0.95, 1.05, 0.85, 1, 0.9];
      document.querySelectorAll('#vvBars span').forEach((el, i) => { el.style.transform = `scaleY(${(0.8 * shape[i] * wobble[i]).toFixed(2)})`; });
      const name = document.querySelector('#vvChars .vv-ch.on b')?.textContent || 'Assistant';
      $('vvTime').textContent = '9:41 AM';
      $('vvLog').innerHTML = `<div class="vv-m you"><small>You</small><p>When do I land in Tokyo, and how long am I in Shinjuku?</p></div>
        <p class="vv-step">Searched your files · tokyo_trip.txt</p>
        <div class="vv-m bot"><small>${name}</small><p>You land at Haneda on the third of April, then spend four nights in Shinjuku before the bullet train to Kyoto.</p></div>`;
    }""")
    page.wait_for_timeout(400)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:7872")
    ap.add_argument("--home", default=str(REPO / "build" / "readme-state"), help="the demo server's LOCAL_STUDIO_HOME")
    ap.add_argument("--only", nargs="*", help="retake only these (chat, code, files, images, models, voice)")
    args = ap.parse_args()
    want = lambda name: not args.only or name in args.only
    workspace = seed(args.url, Path(args.home))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p, tempfile.TemporaryDirectory() as tmp:
        browser = p.chromium.launch(channel="chrome", headless=True)
        for scheme in ("light", "dark") if want("chat") else ():
            context, page = open_page(browser, args.url, scheme)
            chat_shot(page, "a1b2c3d4e5f60001", my_files=True)
            save(page, f"chat-{scheme}", tmp)
            context.close()
        context, page = open_page(browser, args.url, "light")
        if want("code"):
            chat_shot(page, "a1b2c3d4e5f60002")
            page.evaluate("LS.showView('code')")
            page.wait_for_timeout(1500)
            page.evaluate("LS.openCodeFile('src/debounce.py')")
            page.wait_for_timeout(1200)
            page.evaluate("document.querySelectorAll('.turn.bot details.chattoolstep').forEach((d) => { d.open = false })")
            page.click(".xterm")                                          # the real terminal, in the demo project
            page.keyboard.type("git --no-pager diff --stat\n")
            page.wait_for_timeout(1500)
            save(page, "code", tmp)
        if want("files"):
            page.evaluate("LS.showView('search')")
            page.fill("#srQuery", "washing machine warranty")
            page.wait_for_timeout(3500)
            save(page, "files", tmp)
        if want("images"):
            page.evaluate("LS.showView('images')")
            page.wait_for_timeout(2000)
            save(page, "images", tmp)
        if want("models"):
            page.evaluate("LS.showView('library')")
            page.wait_for_timeout(1500)
            save(page, "models", tmp)
        if want("voice"):
            voice_shot(page)
            save(page, "voice", tmp)
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
