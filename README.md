# Instagram Unfollower

**English** | [Türkçe](README.tr.md)

Lists the accounts you follow, sorted by follower count, lets you pick the ones to
unfollow on a local web page, then unfollows them slowly, at human speed.

## Setup and usage (macOS)

```bash
git clone https://github.com/ferhatsanli/unfollower.git
cd unfollower
./run.sh --demo   # try the UI with fake data first (no Instagram calls)
./run.sh          # real run
```

On the first run, `run.sh` creates a `.venv` virtual environment and installs the
libraries from `requirements.txt`. Later runs skip the install. Python 3.10+ is required
(`brew install python`).

## How a run works

1. Fetches the accounts you follow and their follower counts (cached between runs).
2. If Instagram starts throttling (HTTP 429, "please wait" etc.), scanning stops
   immediately and you continue with the accounts scanned so far. The next run picks
   up the unscanned ones, and stops the same way if it is throttled again.
3. Asks which installed browser to open the selection page in.
4. On the page, click anywhere on a row to select it; the Instagram icon opens that
   profile in a new tab. Accounts already unfollowed, or still queued from an earlier
   run, are not shown again.
5. After Save, queued accounts from earlier runs go first, then your new selection.

## Options

- `--demo`: opens the UI with fake data and sends nothing.
- `--refresh`: discards the cached follower counts and fetches them again.

## Generated files

- `session.json`: your Instagram session (not your password). Never share it.
- `following_cache.json`: cached follower counts.
- `following_sorted.csv`: the list sorted by follower count, descending.
- `to_unfollow.txt`: the accounts you selected and saved on the page.
- `unfollowed_log.txt`: accounts already unfollowed; a new run resumes where it left off.

All of these are listed in `.gitignore`.

Press Ctrl+C to stop at any time. Each run unfollows at most 150 accounts; run it again
the next day to finish the rest.

## Disclaimer

This tool uses Instagram's unofficial private API through
[instagrapi](https://github.com/subzeroid/instagrapi). Automating your account is against
Instagram's terms; the random delays lower the risk of a block but do not remove it.
Use at your own risk.
