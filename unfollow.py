#!/usr/bin/env python3
"""
Instagram bulk unfollow tool.

Flow:
  1. Log in (session is cached in session.json, so you rarely re-enter 2FA).
  2. Fetch everyone you follow + their follower counts (cached in following_cache.json).
     If Instagram starts throttling (HTTP 429 etc.), scanning stops right away and
     the next run continues with the accounts that are still unscanned.
  3. Sort by follower count (desc) and write following_sorted.csv.
  4. Ask which browser to use, then open a local page where you tick the accounts
     to unfollow. Accounts already unfollowed or still queued from an earlier run
     are not shown again.
  5. When you press Save, the selection is written to to_unfollow.txt and the
     script unfollows each account slowly, with random human-like pauses.

Progress is logged to unfollowed_log.txt, so if you stop (Ctrl+C) or Instagram
rate-limits you, just run the script again and it continues where it left off.

Usage:
  ./run.sh             # real run (creates .venv and installs dependencies)
  ./run.sh --demo      # preview the UI with fake data, no Instagram calls
  ./run.sh --refresh   # forget cached follower counts and scan everything again
"""

import argparse
import csv
import getpass
import json
import random
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "session.json"
CACHE_FILE = BASE_DIR / "following_cache.json"
SORTED_CSV = BASE_DIR / "following_sorted.csv"
TEMPLATE_FILE = BASE_DIR / "template.html"
HTML_FILE = BASE_DIR / "following.html"
SELECTION_FILE = BASE_DIR / "to_unfollow.txt"
LOG_FILE = BASE_DIR / "unfollowed_log.txt"

# ---- Safety knobs (slow on purpose) -------------------------------------
INFO_DELAY = (3, 7)            # seconds between profile lookups
INFO_BREAK_EVERY = 50          # take a longer break every N lookups
INFO_BREAK = (120, 240)

UNFOLLOW_DELAY = (35, 95)      # seconds between unfollows
UNFOLLOW_BREAK_EVERY = 10      # longer break every N unfollows
UNFOLLOW_BREAK = (300, 600)
MAX_UNFOLLOWS_PER_RUN = 150    # stay well under Instagram's daily limits
RATE_LIMIT_WAIT = 20 * 60      # wait this long when Instagram says "please wait"

# Browsers offered on macOS, if installed: (menu label, application name)
MAC_BROWSERS = [
    ("Safari", "Safari"),
    ("Google Chrome", "Google Chrome"),
    ("Firefox", "Firefox"),
    ("Brave", "Brave Browser"),
    ("Microsoft Edge", "Microsoft Edge"),
    ("Arc", "Arc"),
    ("Opera", "Opera"),
    ("Vivaldi", "Vivaldi"),
    ("Chromium", "Chromium"),
    ("Orion", "Orion"),
    ("Zen", "Zen"),
]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def human_sleep(bounds: tuple, reason: str = "") -> None:
    seconds = random.uniform(*bounds)
    if seconds > 60 or reason:
        log(f"Waiting {seconds:.0f}s {reason}".rstrip())
    time.sleep(seconds)


def throttle_errors() -> tuple:
    """Errors meaning 'Instagram wants us to stop for now'."""
    from instagrapi.exceptions import (ChallengeRequired, ClientThrottledError,
                                       FeedbackRequired, LoginRequired,
                                       PleaseWaitFewMinutes, RateLimitError)
    return (ClientThrottledError, PleaseWaitFewMinutes, RateLimitError,
            FeedbackRequired, ChallengeRequired, LoginRequired)


# ---- Login ---------------------------------------------------------------
def _login_with_2fa(cl, username: str, password: str) -> None:
    from instagrapi.exceptions import TwoFactorRequired
    try:
        cl.login(username, password)
    except TwoFactorRequired:
        code = input("2FA code: ").strip()
        cl.login(username, password, verification_code=code)


def login():
    from instagrapi import Client
    from instagrapi.exceptions import LoginRequired

    cl = Client()
    cl.delay_range = [1, 3]  # small built-in delay between every API request

    username = input("Instagram username: ").strip()
    password = getpass.getpass("Instagram password (not stored): ")

    if SESSION_FILE.exists():
        cl.load_settings(SESSION_FILE)
        try:
            _login_with_2fa(cl, username, password)
            cl.get_timeline_feed()  # check the saved session is still valid
        except LoginRequired:
            log("Saved session expired; logging in again.")
            old_uuids = cl.get_settings().get("uuids", {})
            cl.set_settings({})
            cl.set_uuids(old_uuids)  # keep the same "device" so Instagram isn't suspicious
            _login_with_2fa(cl, username, password)
    else:
        _login_with_2fa(cl, username, password)

    cl.dump_settings(SESSION_FILE)
    SESSION_FILE.chmod(0o600)
    log(f"Logged in as @{username}")
    return cl


# ---- Progress files ------------------------------------------------------
def read_lines(path: Path) -> list:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def already_done() -> set:
    """Usernames unfollowed in any earlier run (from unfollowed_log.txt)."""
    return {line.split("\t")[1] for line in read_lines(LOG_FILE) if line.count("\t") >= 1}


def pending_from_last_run(done: set) -> list:
    """Accounts selected earlier but not unfollowed yet (e.g. run stopped or hit the daily cap)."""
    return [u for u in read_lines(SELECTION_FILE) if u not in done]


# ---- Step 1 + 2: fetch following and follower counts --------------------
def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def is_scanned(entry: dict) -> bool:
    """True once we have a follower count, or a permanent error (deleted/blocked account).
    Older versions stored -1 for throttled lookups too; those get rescanned."""
    f = entry.get("followers")
    return f is not None and (f >= 0 or entry.get("error") == "permanent")


def fetch_following(cl, skip: set) -> tuple:
    """Returns (accounts, unscanned_count). Stops scanning at the first throttle.
    Accounts in `skip` (already unfollowed or queued) are never looked up."""
    cache = load_cache()

    log("Fetching the accounts you follow...")
    try:
        following = cl.user_following(cl.user_id, amount=0)
        users = list(following.values()) if isinstance(following, dict) else list(following)
        log(f"You follow {len(users)} accounts.")
        current = {str(u.pk): u for u in users}
        # Drop accounts you no longer follow; add new ones as unscanned
        cache = {pk: v for pk, v in cache.items() if pk in current}
        for pk, u in current.items():
            cache.setdefault(pk, {"pk": pk, "username": u.username,
                                  "full_name": u.full_name or "", "followers": None})
        save_cache(cache)
    except throttle_errors() as e:
        if not cache:
            sys.exit(f"Instagram is throttling ({type(e).__name__}) and there is no saved "
                     "list yet. Try again in an hour or so.")
        log(f"Could not refresh the following list ({type(e).__name__}); "
            "using the saved list from the last run.")

    todo = [v for v in cache.values() if not is_scanned(v) and v["username"] not in skip]
    if todo:
        mins = len(todo) * sum(INFO_DELAY) / 2 / 60
        log(f"Looking up follower counts for {len(todo)} accounts (~{mins:.0f}+ min). "
            "Results are cached, so you can stop and resume.")

    for i, entry in enumerate(todo, 1):
        try:
            info = cl.user_info_v1(entry["pk"])
            entry["followers"] = int(info.follower_count or 0)
            entry.pop("error", None)
        except throttle_errors() as e:
            unscanned = len(todo) - i + 1
            log(f"Instagram is throttling ({type(e).__name__}). Stopping the scan now; "
                f"{unscanned} accounts stay unscanned and will be scanned on the next run.")
            break
        except Exception as e:  # deleted / blocked / unavailable account
            log(f"  @{entry['username']}: could not read follower count ({type(e).__name__})")
            entry["followers"] = -1
            entry["error"] = "permanent"
        save_cache(cache)
        log(f"  {i}/{len(todo)} @{entry['username']}: {entry['followers']:,}")

        if i == len(todo):
            break
        if i % INFO_BREAK_EVERY == 0:
            human_sleep(INFO_BREAK, "(short break)")
        else:
            human_sleep(INFO_DELAY)

    save_cache(cache)
    scanned = [v for v in cache.values() if is_scanned(v)]
    unscanned = sum(1 for v in cache.values() if not is_scanned(v) and v["username"] not in skip)
    return scanned, unscanned


def write_sorted(accounts: list) -> list:
    accounts = sorted(accounts, key=lambda a: a["followers"], reverse=True)
    with SORTED_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["username", "full_name", "followers", "pk"])
        for a in accounts:
            w.writerow([a["username"], a["full_name"], a["followers"], a["pk"]])
    log(f"Sorted list written to {SORTED_CSV.name}")
    return accounts


# ---- Browser choice ------------------------------------------------------
def available_browsers() -> list:
    """List of (label, opener) where opener(url) opens the page in that browser."""
    options = []
    if sys.platform == "darwin" and shutil.which("open"):
        app_dirs = [Path("/Applications"), Path.home() / "Applications"]
        for label, app in MAC_BROWSERS:
            if any((d / f"{app}.app").exists() for d in app_dirs):
                options.append((label, lambda url, app=app:
                                subprocess.run(["open", "-a", app, url], check=False)))
    else:
        for name in ("firefox", "google-chrome", "chromium", "chromium-browser",
                     "brave-browser", "microsoft-edge", "opera", "windows-default"):
            try:
                b = webbrowser.get(name)
                options.append((name, lambda url, b=b: b.open(url)))
            except webbrowser.Error:
                pass
    return options


def open_in_chosen_browser(url: str) -> None:
    options = available_browsers()
    print("\nWhich browser should open the selection page?")
    print("  0) System default")
    for n, (label, _) in enumerate(options, 1):
        print(f"  {n}) {label}")
    while True:
        choice = input(f"Choose 0-{len(options)} [0]: ").strip() or "0"
        if choice.isdigit() and 0 <= int(choice) <= len(options):
            break
        print("Please enter one of the numbers above.")
    if choice == "0":
        webbrowser.open(url)
    else:
        label, opener = options[int(choice) - 1]
        opener(url)
        log(f"Opening in {label}")
    log(f"If nothing opened, paste this into any browser: {url}")


# ---- Step 3: HTML selection page served locally --------------------------
def build_html(accounts: list, meta: dict) -> bytes:
    data = json.dumps(
        [{"u": a["username"], "n": a["full_name"], "f": a["followers"]} for a in accounts],
        ensure_ascii=False,
    ).replace("</", "<\\/")
    html = (TEMPLATE_FILE.read_text(encoding="utf-8")
            .replace("/*__DATA__*/[]", data)
            .replace("/*__META__*/{}", json.dumps(meta)))
    HTML_FILE.write_text(html, encoding="utf-8")
    return html.encode("utf-8")


def wait_for_selection(accounts: list, meta: dict) -> list:
    html_bytes = build_html(accounts, meta)
    valid = {a["username"] for a in accounts}
    done = threading.Event()
    result = {"selected": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)

        def do_POST(self):
            if self.path != "/save":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            if length > 2_000_000:
                self.send_error(413)
                return
            try:
                names = json.loads(self.rfile.read(length) or b"[]")
                selected = [n for n in names if n in valid]
            except (ValueError, TypeError):
                self.send_error(400)
                return
            result["selected"] = selected
            body = json.dumps({"saved": len(selected)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    open_in_chosen_browser(url)
    log("Tick the accounts to unfollow and press Save. Waiting...")
    done.wait()
    time.sleep(0.5)  # let the response reach the browser
    server.shutdown()
    return result["selected"]


def save_queue(names: list) -> None:
    SELECTION_FILE.write_text("\n".join(names) + ("\n" if names else ""), encoding="utf-8")


# ---- Step 4: unfollow slowly ---------------------------------------------
def unfollow_all(cl, queue: list, pk_by_name: dict) -> None:
    from instagrapi.exceptions import (ChallengeRequired, FeedbackRequired,
                                       LoginRequired, PleaseWaitFewMinutes, RateLimitError)
    from instagrapi.exceptions import ClientThrottledError

    rate_limited = (PleaseWaitFewMinutes, RateLimitError, FeedbackRequired, ClientThrottledError)

    if not queue:
        log("Nothing to unfollow.")
        return
    if len(queue) > MAX_UNFOLLOWS_PER_RUN:
        log(f"{len(queue)} in the queue; this run does the first {MAX_UNFOLLOWS_PER_RUN}. "
            "Run again tomorrow for the rest (they stay queued).")
        queue = queue[:MAX_UNFOLLOWS_PER_RUN]

    log(f"Unfollowing {len(queue)} accounts, slowly. Ctrl+C to stop anytime.")
    count = 0
    for idx, name in enumerate(queue):
        pk = pk_by_name.get(name)
        if pk is None:
            try:
                pk = cl.user_id_from_username(name)
            except Exception as e:
                log(f"  @{name}: account not found ({type(e).__name__}); skipping.")
                continue

        ok = None
        for attempt in (1, 2):
            try:
                ok = cl.user_unfollow(pk)
                break
            except rate_limited as e:
                if attempt == 2:
                    log(f"Instagram is still limiting ({type(e).__name__}). "
                        "Stopping; run the script again later to resume.")
                    return
                log(f"Rate limited ({type(e).__name__}). Pausing {RATE_LIMIT_WAIT // 60} min.")
                time.sleep(RATE_LIMIT_WAIT)
            except (ChallengeRequired, LoginRequired) as e:
                log(f"Instagram wants verification ({type(e).__name__}). "
                    "Open the Instagram app, confirm it's you, then rerun.")
                return
            except Exception as e:
                log(f"  @{name}: failed ({type(e).__name__}: {e}); skipping.")
                break

        if ok:
            count += 1
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{name}\n")
            log(f"  [{count}/{len(queue)}] unfollowed @{name}")

        if idx == len(queue) - 1:
            break
        if count and count % UNFOLLOW_BREAK_EVERY == 0:
            human_sleep(UNFOLLOW_BREAK, "(longer break)")
        else:
            human_sleep(UNFOLLOW_DELAY)

    log(f"Done. Unfollowed {count} accounts this run.")


# ---- Demo data -----------------------------------------------------------
def demo_accounts() -> list:
    rnd = random.Random(7)
    words = ["travel", "food", "fit", "daily", "style", "tech", "art", "moda", "life", "nl"]
    return [{"pk": str(i), "username": f"{rnd.choice(words)}.{rnd.choice(words)}{i}",
             "full_name": "", "followers": int(10 ** rnd.uniform(2, 7.5))} for i in range(120)]


def main() -> None:
    p = argparse.ArgumentParser(description="Bulk unfollow Instagram accounts, slowly.")
    p.add_argument("--demo", action="store_true", help="preview the UI with fake data")
    p.add_argument("--refresh", action="store_true", help="ignore cached follower counts")
    args = p.parse_args()

    if args.demo:
        accounts = sorted(demo_accounts(), key=lambda a: a["followers"], reverse=True)
        selected = wait_for_selection(accounts, {"unscanned": 0, "queued": 0})
        log(f"Demo mode: would unfollow {len(selected)} accounts. Nothing was sent.")
        return

    try:
        import instagrapi  # noqa: F401
    except ImportError:
        sys.exit("instagrapi is not installed for this Python:\n"
                 f"  {sys.executable} -m pip install \"instagrapi>=2.6.9\"\n"
                 "(see the setup steps: using a venv is recommended)")

    if args.refresh and CACHE_FILE.exists():
        CACHE_FILE.unlink()

    try:
        cl = login()
        done = already_done()
        pending = pending_from_last_run(done)
        hidden = done | set(pending)
        scanned, unscanned = fetch_following(cl, hidden)
        if pending:
            log(f"{len(pending)} accounts selected last time are still queued; "
                "they will be unfollowed first and are not shown again.")

        visible = write_sorted([a for a in scanned if a["username"] not in hidden])
        selected = wait_for_selection(visible, {"unscanned": unscanned, "queued": len(pending)})

        queue = pending + [u for u in selected if u not in set(pending)]
        save_queue(queue)
        log(f"{len(selected)} new accounts selected; {len(queue)} queued in {SELECTION_FILE.name}")

        pk_by_name = {a["username"]: a["pk"] for a in load_cache().values()}
        unfollow_all(cl, queue, pk_by_name)
    except KeyboardInterrupt:
        log("Stopped by you. Progress is saved; run again to continue.")
        sys.exit(130)


if __name__ == "__main__":
    main()
