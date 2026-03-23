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

# Lowercase for case-insensitive matching
TARGET_USERS    = {"taraujo", "novel_calendar5168"}
TARGET_KEYWORDS = ["NBA Props Daily", "NBA Betting", "NBA Picks"]
SUBREDDIT       = "sportsbook"
STATE_FILE      = "state.json"
LOOKBACK_HOURS  = 30
HEADERS         = {"User-Agent": "nba-picks-bot/1.0 (personal use, read-only)"}
MAX_TG_CHARS    = 4000  # Telegram limit is 4096, stay safe


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
            state[key] = {} if key == "seen" else []
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


def get_top_level_comments(post_id):
    """Fetch only top-level comments — no sub-comments/replies."""
    data = reddit_get(f"https://www.reddit.com/comments/{post_id}.json?limit=500&depth=1")
    if not data or len(data) < 2:
        return []
    comments = []
    for child in data[1].get("data", {}).get("children", []):
        if child.get("kind") == "t1":
            comments.append(child["data"])
    return comments


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text):
    """Send a message, automatically splitting if over Telegram's limit."""
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     chunk,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            print(f"  ⚠️  Telegram error {resp.status_code}: {resp.text}", file=sys.stderr)
        else:
            print(f"  📨  Sent chunk {i+1}/{len(chunks)} ({len(chunk)} chars)")
        if len(chunks) > 1:
            time.sleep(0.5)


def split_message(text, limit=MAX_TG_CHARS):
    """Split a long message at newlines to stay under Telegram's character limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current.strip():
        chunks.append(current.rstrip())
    return chunks if chunks else [text[:limit]]


def escape_html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Claude Formatting ─────────────────────────────────────────────────────────
def format_with_claude(username, body, post_title):
    """Use Claude to reformat a raw Reddit comment into clean Telegram HTML."""
    if not ANTHROPIC_API_KEY:
        return fallback_format(body)

    prompt = f"""You are formatting an NBA betting picks post from Reddit for Telegram.

The user u/{username} posted this in the thread "{post_title}":

---
{body}
---

Reformat this into a clean, easy-to-read Telegram message using HTML formatting.
Rules:
- Use <b>bold</b> for player names, team names, and bet labels
- Use emojis: 🏀 for games, 📊 for stats/analysis sections, 💡 for notes/reasoning, ⭐ for strong plays
- Group bets clearly — each bet on its own line
- Keep ALL the original analysis and reasoning, just make it cleaner
- For each bet show: Player/Team · Stat line · Over/Under · any odds if mentioned
- Use ✅ for bets the user is confident about, 🔸 for leans/fades
- Preserve section headers if any (e.g. "Player Props", "Game Picks")
- Do NOT add anything that wasn't in the original
- Do NOT use markdown (no **, no ##) — ONLY these HTML tags: <b>, <i>, <u>
- Return ONLY the formatted message body, no intro text, no preamble"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 2000,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.ok:
            return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  ⚠️  Claude format error: {e}", file=sys.stderr)

    return fallback_format(body)


def fallback_format(body):
    safe_body = escape_html(body.strip())
    return "\n".join(f"  {ln}" for ln in safe_body.splitlines() if ln.strip())


def build_message(post_title, post_url, username, formatted_body, is_edit=False):
    edit_tag = "  ✏️ <i>(edited)</i>" if is_edit else ""
    return (
        f"🏀 <b>{escape_html(post_title)}</b>\n"
        f"{'━' * 30}\n"
        f"💬 <b>u/{username}</b>{edit_tag}\n\n"
        f"{formatted_body}\n\n"
        f"🔗 <a href='{post_url}'>View on Reddit</a>"
    )


# ── Bet Parsing ───────────────────────────────────────────────────────────────
def parse_bets_with_claude(username, body, post_title, date_str):
    if not ANTHROPIC_API_KEY:
        return [{"description": body[:300], "player": None, "team": None,
                 "opponent": None, "bet_type": "other", "stat": None,
                 "line": None, "direction": None, "confidence": None}]

    prompt = f"""Extract all individual NBA bets from this r/sportsbook comment.

Post: {post_title}
User: u/{username}
Date: {date_str}
Comment:
{body}

Return a JSON array. Each bet object must have:
- "description": concise label (e.g. "LeBron James Over 25.5 PTS")
- "player": full player name or null
- "team": team abbreviation or null
- "opponent": opponent abbreviation or null
- "bet_type": "player_prop", "spread", "moneyline", "total", "parlay", or "other"
- "stat": "PTS","REB","AST","3PM","BLK","STL","PRA","PR","PA","RA" or null
- "line": numeric line value or null
- "direction": "over", "under", "yes", "no" or null
- "confidence": "lean", "like", "play", "strong", or "fade" or null

Return ONLY a valid JSON array, no markdown, no explanation. Return [] if no bets found."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         ANTHROPIC_API_KEY,
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

    return [{"description": body[:300], "player": None, "team": None,
             "opponent": None, "bet_type": "other", "stat": None,
             "line": None, "direction": None, "confidence": None}]


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

        comments = get_top_level_comments(post_id)
        print(f"  💬  {len(comments)} top-level comments found")
        time.sleep(2)

        for comment in comments:
            author = comment.get("author", "")
            # Case-insensitive username match
            if author.lower() not in TARGET_USERS:
                continue

            print(f"  🎯  Found comment from u/{author}")

            cid   = comment.get("id")
            body  = comment.get("body", "")
            chash = body_hash(body)

            is_new    = cid not in state["seen"]
            is_edited = not is_new and state["seen"].get(cid) != chash

            if not (is_new or is_edited):
                print(f"    ⏭️   Already seen, skipping")
                continue

            print(f"    {'✅' if is_new else '✏️ '} {'New' if is_new else 'Edited'} — formatting...")

            formatted_body = format_with_claude(author, body, title)
            message        = build_message(title, post_url, author, formatted_body, is_edit=is_edited)

            send_telegram(message)
            state["seen"][cid] = chash
            sends += 1

            # Parse and store bets for grading
            if is_new:
                bets = parse_bets_with_claude(author, body, title, today)
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
                print(f"    💾  Stored {stored} bet(s) for grading")

    save_state(state)
    print(f"✅  Done — {sends} message(s) sent.")


if __name__ == "__main__":
    main()
