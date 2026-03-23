import requests
import json
import os
import hashlib
import sys
import time
from datetime import datetime, timezone, timedelta

# ── Config from GitHub Actions secrets ───────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# ── Bot settings ──────────────────────────────────────────────────────────────
TARGET_USERS    = {"taraujo", "Novel_Calendar5168"}
TARGET_KEYWORDS = ["NBA Props Daily", "NBA Betting", "NBA Picks"]
SUBREDDIT       = "sportsbook"
STATE_FILE      = "state.json"
LOOKBACK_HOURS  = 30

HEADERS = {"User-Agent": "nba-picks-bot/1.0 (personal use, read-only)"}


# ── State management ──────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen": {}}


def save_state(state: dict):
    if len(state["seen"]) > 2000:
        keys = list(state["seen"].keys())
        state["seen"] = {k: state["seen"][k] for k in keys[-1000:]}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def body_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# ── Reddit JSON helpers ───────────────────────────────────────────────────────
def reddit_get(url: str) -> dict | None:
    """Fetch a Reddit JSON endpoint with retries."""
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 429:
                print(f"  ⏳  Rate limited — waiting 60s (attempt {attempt+1})")
                time.sleep(60)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"  ⚠️  Request error: {e}", file=sys.stderr)
            time.sleep(5)
    return None


def get_new_posts(subreddit: str, limit: int = 75) -> list:
    data = reddit_get(f"https://www.reddit.com/r/{subreddit}/new.json?limit={limit}")
    if not data:
        return []
    return data.get("data", {}).get("children", [])


def get_comments(post_id: str) -> list:
    """Return a flat list of all comments in a post."""
    data = reddit_get(f"https://www.reddit.com/comments/{post_id}.json?limit=500")
    if not data or len(data) < 2:
        return []

    comments = []
    def extract(listing):
        for child in listing.get("data", {}).get("children", []):
            kind = child.get("kind")
            if kind == "t1":   # regular comment
                comments.append(child["data"])
                # recurse into replies
                replies = child["data"].get("replies", "")
                if isinstance(replies, dict):
                    extract(replies)
    extract(data[1])
    return comments


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    if not resp.ok:
        print(f"  ⚠️  Telegram error {resp.status_code}: {resp.text}", file=sys.stderr)


# ── Formatting ────────────────────────────────────────────────────────────────
def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(post_title: str, post_url: str,
                   username: str, body: str, is_edit: bool = False) -> str:
    edit_tag  = "  ✏️ <i>(edited)</i>" if is_edit else ""
    safe_body = escape_html(body.strip())
    indented  = "\n".join(f"  {ln}" for ln in safe_body.splitlines() if ln.strip())

    return (
        f"🏀 <b>{escape_html(post_title)}</b>\n"
        f"{'─' * 32}\n"
        f"💬 <b>u/{username}</b>{edit_tag}\n\n"
        f"{indented}\n\n"
        f"🔗 <a href='{post_url}'>View thread on Reddit</a>"
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🤖  Bot starting — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    state     = load_state()
    cutoff    = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    new_sends = 0

    posts = get_new_posts(SUBREDDIT)
    print(f"  📥  Fetched {len(posts)} recent posts from r/{SUBREDDIT}")

    for post_child in posts:
        post = post_child.get("data", {})
        title = post.get("title", "")

        # Age filter
        created = datetime.fromtimestamp(post.get("created_utc", 0), tz=timezone.utc)
        if created < cutoff:
            continue

        # Keyword filter
        if not any(kw.lower() in title.lower() for kw in TARGET_KEYWORDS):
            continue

        post_id  = post.get("id")
        post_url = f"https://reddit.com{post.get('permalink', '')}"
        print(f"  📌  Scanning: {title[:70]}")

        comments = get_comments(post_id)
        time.sleep(2)  # be polite to Reddit's servers

        for comment in comments:
            author = comment.get("author", "")
            if author not in TARGET_USERS:
                continue

            cid   = comment.get("id")
            body  = comment.get("body", "")
            chash = body_hash(body)

            if cid not in state["seen"]:
                print(f"    ✅  New comment by u/{author} ({cid})")
                send_telegram(format_message(title, post_url, author, body, is_edit=False))
                state["seen"][cid] = chash
                new_sends += 1

            elif state["seen"][cid] != chash:
                print(f"    ✏️   Edited comment by u/{author} ({cid})")
                send_telegram(format_message(title, post_url, author, body, is_edit=True))
                state["seen"][cid] = chash
                new_sends += 1

    save_state(state)
    print(f"✅  Done — {new_sends} message(s) sent.")


if __name__ == "__main__":
    main()
