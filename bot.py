import requests
import json
import os
import hashlib
import sys
import time
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

TARGET_USERS    = {"taraujo", "Novel_Calendar5168"}
TARGET_KEYWORDS = ["NBA Props Daily", "NBA Betting", "NBA Picks"]
SUBREDDIT       = "sportsbook"
STATE_FILE      = "state.json"
LOOKBACK_HOURS  = 30
HEADERS         = {"User-Agent": "nba-picks-bot/1.0 (personal use, read-only)"}


# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen": {}, "pending_bets": [], "graded_bets": [], "stats": {}}


def save_state(state):
    if len(state.get("seen", {})) > 2000:
        keys = list(state["seen"].keys())
        state["seen"] = {k: state["seen"][k] for k in keys[-1000:]}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def ensure_state_keys(state):
    for key in ["seen", "pending_bets", "graded_bets"]:
        if key not in state:
            state[key] = {}  if key == "seen" else []
    if "stats" not in state:
        state["stats"] = {}
    return state


def body_hash(text):
    return hashlib.md5(text.encode()).hexdigest()


# ── Reddit JSON API ───────────────────────────────────────────────────────────
def reddit_get(url):
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


def get_new_posts(subreddit, limit=75):
    data = reddit_get(f"https://www.reddit.com/r/{subreddit}/new.json?limit={limit}")
    if not data:
        return []
    return data.get("data", {}).get("children", [])


def get_comments(post_id):
    data = reddit_get(f"https://www.reddit.com/comments/{post_id}.json?limit=500")
    if not data or len(data) < 2:
        return []
    comments = []
    def extract(listing):
        for child in listing.get("data", {}).get("children", []):
            if child.get("kind") == "t1":
                comments.append(child["data"])
                replies = child["data"].get("replies", "")
                if isinstance(replies, dict):
                    extract(replies)
    extract(data[1])
    return comments


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text):
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


def escape_html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_comment_message(post_title, post_url, username, body, is_edit=False):
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


# ── Bet Parsing ───────────────────────────────────────────────────────────────
def parse_bets_with_claude(username, body, post_title, date_str):
    """Use Claude API to extract individual structured bets from a comment."""
    if not ANTHROPIC_API_KEY:
        # Graceful fallback: store full comment as one unstructured bet
        return [{
            "description": body[:300],
            "player": None, "team": None, "opponent": None,
            "bet_type": "other", "stat": None,
            "line": None, "direction": None, "confidence": None
        }]

    prompt = f"""Extract all individual NBA bets from this r/sportsbook comment.

Post: {post_title}
User: u/{username}
Date: {date_str}
Comment:
{body}

Return a JSON array. Each bet object must have:
- "description": concise human-readable label (e.g. "LeBron James Over 25.5 PTS", "Lakers -5.5 vs Warriors")
- "player": full player name or null
- "team": team abbreviation (LAL, GSW, BOS...) or null
- "opponent": opponent abbreviation or null
- "bet_type": "player_prop", "spread", "moneyline", "total", "parlay", or "other"
- "stat": for props use: "PTS","REB","AST","3PM","BLK","STL","PRA","PR","PA","RA" — or null
- "line": numeric line value (e.g. 25.5) or null
- "direction": "over", "under", "yes", "no" or null
- "confidence": "lean", "like", "play", "strong", or "fade" based on language used — or null

Return ONLY a valid JSON array, no markdown fences, no explanation.
If no clear bets are found, return [].
Ignore any meta-commentary, non-bet text, or notes."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":    "application/json",
                "x-api-key":       ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 1000,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.ok:
            raw = resp.json()["content"][0]["text"].strip()
            raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
            bets = json.loads(raw)
            return bets if isinstance(bets, list) else []
    except Exception as e:
        print(f"  ⚠️  Claude parse error: {e}", file=sys.stderr)

    return [{"description": body[:300], "player": None, "team": None, "opponent": None,
             "bet_type": "other", "stat": None, "line": None, "direction": None, "confidence": None}]


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🤖  Bot starting — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    state  = ensure_state_keys(load_state())
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sends  = 0

    posts = get_new_posts(SUBREDDIT)
    print(f"  📥  Fetched {len(posts)} posts from r/{SUBREDDIT}")

    for post_child in posts:
        post  = post_child.get("data", {})
        title = post.get("title", "")

        created = datetime.fromtimestamp(post.get("created_utc", 0), tz=timezone.utc)
        if created < cutoff:
            continue
        if not any(kw.lower() in title.lower() for kw in TARGET_KEYWORDS):
            continue

        post_id  = post.get("id")
        post_url = f"https://reddit.com{post.get('permalink', '')}"
        print(f"  📌  Scanning: {title[:70]}")

        comments = get_comments(post_id)
        time.sleep(2)

        for comment in comments:
            author = comment.get("author", "")
            if author not in TARGET_USERS:
                continue

            cid   = comment.get("id")
            body  = comment.get("body", "")
            chash = body_hash(body)

            is_new    = cid not in state["seen"]
            is_edited = not is_new and state["seen"].get(cid) != chash

            if not (is_new or is_edited):
                continue

            print(f"    {'✅' if is_new else '✏️ '} {'New' if is_new else 'Edited'} comment by u/{author} ({cid})")
            send_telegram(format_comment_message(title, post_url, author, body, is_edit=is_edited))
            state["seen"][cid] = chash
            sends += 1

            # Parse and store bets only on new comments
            if is_new:
                bets = parse_bets_with_claude(author, body, title, today)

                # Remove any previous entries for this comment (re-run safety)
                state["pending_bets"] = [
                    b for b in state["pending_bets"]
                    if not b.get("id", "").startswith(f"{cid}_")
                ]

                stored = 0
                for i, bet in enumerate(bets):
                    if not bet.get("description"):
                        continue
                    state["pending_bets"].append({
                        "id":          f"{cid}_{i}",
                        "user":        author,
                        "date":        today,
                        "post_title":  title,
                        "post_url":    post_url,
                        "description": bet.get("description", body[:100]),
                        "player":      bet.get("player"),
                        "team":        bet.get("team"),
                        "opponent":    bet.get("opponent"),
                        "bet_type":    bet.get("bet_type", "other"),
                        "stat":        bet.get("stat"),
                        "line":        bet.get("line"),
                        "direction":   bet.get("direction"),
                        "confidence":  bet.get("confidence"),
                        "result":      None,
                    })
                    stored += 1

                print(f"    💾  Stored {stored} bet(s)")

    save_state(state)
    print(f"✅  Done — {sends} message(s) sent.")


if __name__ == "__main__":
    main()
