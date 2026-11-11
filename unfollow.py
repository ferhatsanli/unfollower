#!/usr/bin/env python3
"""
Instagram bulk unfollow tool.

Flow:
  1. Log in (session is cached in session.json, so you rarely re-enter 2FA).
  2. Fetch everyone you follow + their follower counts (cached in following_cache.json).
  3. Sort by follower count (desc) and write following_sorted.csv.
  4. Open a local web page (following.html) where you tick the accounts to unfollow.
  5. When you press Save, the selection is written to to_unfollow.txt and the
     script continues: it unfollows each account slowly, with random human-like pauses.

Progress is logged to unfollowed_log.txt, so if you stop (Ctrl+C) or Instagram
rate-limits you, just run the script again and it continues where it left off.

Usage:
  pip install "instagrapi>=2.6.9"
  python3 unfollow.py            # real run
  python3 unfollow.py --demo     # preview the UI with fake data, no Instagram calls
"""

import argparse
import csv
import getpass
import json
import random
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


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def human_sleep(bounds: tuple, reason: str = "") -> None:
    seconds = random.uniform(*bounds)
    if seconds > 60 or reason:
        log(f"Waiting {seconds:.0f}s {reason}".rstrip())
    time.sleep(seconds)


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


# ---- Step 1 + 2: fetch following and follower counts --------------------
def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def fetch_following(cl) -> list:
    log("Fetching the accounts you follow...")
    following = cl.user_following(cl.user_id, amount=0)
    users = list(following.values()) if isinstance(following, dict) else list(following)
    log(f"You follow {len(users)} accounts.")

    cache = load_cache()
    current_pks = {str(u.pk) for u in users}
    # Drop accounts you no longer follow
    cache = {pk: v for pk, v in cache.items() if pk in current_pks}

    todo = [u for u in users if str(u.pk) not in cache]
    if todo:
        mins = len(todo) * sum(INFO_DELAY) / 2 / 60
        log(f"Looking up follower counts for {len(todo)} accounts (~{mins:.0f}+ min). "
            "Results are cached, so you can stop and resume.")

    for i, u in enumerate(todo, 1):
        pk = str(u.pk)
        try:
            info = cl.user_info_v1(pk)
            count = int(info.follower_count or 0)
        except Exception as e:  # private/deleted/blocked accounts etc.
            log(f"  @{u.username}: could not read follower count ({type(e).__name__})")
            count = -1
        cache[pk] = {"pk": pk, "username": u.username,
                     "full_name": u.full_name or "", "followers": count}
        save_cache(cache)
        log(f"  {i}/{len(todo)} @{u.username}: {count:,}")

        if i % INFO_BREAK_EVERY == 0:
            human_sleep(INFO_BREAK, "(short break)")
        else:
            human_sleep(INFO_DELAY)

    return list(cache.values())


def write_sorted(accounts: list) -> list:
    accounts = sorted(accounts, key=lambda a: a["followers"], reverse=True)
    with SORTED_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["username", "full_name", "followers", "pk"])
        for a in accounts:
            w.writerow([a["username"], a["full_name"], a["followers"], a["pk"]])
    log(f"Sorted list written to {SORTED_CSV.name}")
    return accounts


# ---- Step 3: HTML selection page served locally --------------------------
def build_html(accounts: list) -> str:
    data = json.dumps(
        [{"u": a["username"], "n": a["full_name"], "f": a["followers"]} for a in accounts],
        ensure_ascii=False,
    ).replace("</", "<\\/")
    html = TEMPLATE_FILE.read_text(encoding="utf-8").replace("/*__DATA__*/[]", data)
    HTML_FILE.write_text(html, encoding="utf-8")
    return html


def wait_for_selection(accounts: list) -> list:
    html_bytes = build_html(accounts).encode("utf-8")
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
            SELECTION_FILE.write_text("\n".join(selected) + ("\n" if selected else ""),
                                      encoding="utf-8")
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
    log(f"Selection page: {url}  (opening in your browser)")
    webbrowser.open(url)
    log("Tick the accounts to unfollow and press Save. Waiting...")
    done.wait()
    time.sleep(0.5)  # let the response reach the browser
    server.shutdown()
    log(f"{len(result['selected'])} accounts saved to {SELECTION_FILE.name}")
    return result["selected"]


# ---- Step 4: unfollow slowly ---------------------------------------------
def already_done() -> set:
    if not LOG_FILE.exists():
        return set()
    return {line.split("\t")[1] for line in LOG_FILE.read_text(encoding="utf-8").splitlines()
            if line.count("\t") >= 1}


def unfollow_all(cl, usernames: list, accounts: list) -> None:
    from instagrapi.exceptions import (ChallengeRequired, FeedbackRequired,
                                       LoginRequired, PleaseWaitFewMinutes, RateLimitError)

    pk_by_name = {a["username"]: a["pk"] for a in accounts}
    done = already_done()
    queue = [u for u in usernames if u not in done]
    if len(queue) < len(usernames):
        log(f"Skipping {len(usernames) - len(queue)} already unfollowed (from log).")
    if not queue:
        log("Nothing left to unfollow.")
        return
    if len(queue) > MAX_UNFOLLOWS_PER_RUN:
        log(f"{len(queue)} selected; this run does the first {MAX_UNFOLLOWS_PER_RUN}. "
            "Run again tomorrow for the rest.")
        queue = queue[:MAX_UNFOLLOWS_PER_RUN]

    log(f"Unfollowing {len(queue)} accounts, slowly. Ctrl+C to stop anytime.")
    count = 0
    for name in queue:
        pk = pk_by_name.get(name)
        for attempt in (1, 2):
            try:
                ok = cl.user_unfollow(pk)
                break
            except (PleaseWaitFewMinutes, RateLimitError, FeedbackRequired) as e:
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
                ok = None
                break

        if ok:
            count += 1
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{name}\n")
            log(f"  [{count}/{len(queue)}] unfollowed @{name}")

        if name == queue[-1]:
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
        selected = wait_for_selection(accounts)
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
        accounts = write_sorted(fetch_following(cl))
        selected = wait_for_selection(accounts)
        if selected:
            unfollow_all(cl, selected, accounts)
    except KeyboardInterrupt:
        log("Stopped by you. Progress is saved; run again to continue.")
        sys.exit(130)


if __name__ == "__main__":
    main()
