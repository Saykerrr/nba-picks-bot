import requests
import json
import os
import hashlib
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

TARGET_USERS    = {"taraujo", "novel_calendar5168"}
TARGET_KEYWORDS = ["NBA Props Daily", "NBA Betting", "NBA Picks"]
SUBREDDIT       = "sportsbook"
STATE_FILE      = "state.json"
LOOKBACK_HOURS  = 30
MAX_TG_CHARS    = 4000

# Browser-like headers to avoid blocks
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


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


# ── Reddit via RSS + JSON fallback ────────────────────────────────────────────
def safe_get(url, headers=None, timeout=15):
    """HTTP GET with retries."""
    h = headers or HEADERS
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=h, timeout=timeout)
            if resp.status_code == 429:
                print(f"  ⏳  Rate limited — waiting 60s")
                time.sleep(60)
                continue
            if resp.status_code == 403:
                print(f"  ⚠️  403 Blocked on attempt {attempt+1}: {url}")
                time.sleep(3)
                continue
            resp.raise_for_status()
            return resp
        except Exception as e:
            print(f"  ⚠️  Request error (attempt {attempt+1}): {e}", file=sys.stderr)
            time.sleep(5)
    return None


def get_new_posts_rss(subreddit):
    """Fetch new posts via RSS — less blocked than JSON API."""
    url  = f"https://www.reddit.com/r/{subreddit}/new/.rss?limit=50"
    resp = safe_get(url)
    if not resp:
        return []

    posts = []
    try:
        root = ET.fromstring(resp.content)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            title   = entry.findtext("atom:title", default="", namespaces=ns)
            link_el = entry.find("atom:link", ns)
            link    = link_el.attrib.get("href", "") if link_el is not None else ""
            updated = entry.findtext("atom:updated", default="", namespaces=ns)
            # Extract post ID from link e.g. /r/sub/comments/POST_ID/title/
            post_id = ""
            parts   = link.rstrip("/").split("/")
            if "comments" in parts:
                idx     = parts.index("comments")
                post_id = parts[idx + 1] if idx + 1 < len(parts) else ""
            posts.append({
                "title":    title,
                "url":      link,
                "post_id":  post_id,
                "updated":  updated,
            })
    except ET.ParseError as e:
        print(f"  ⚠️  RSS parse error: {e}", file=sys.stderr)

    return posts


def get_top_level_comments(post_id):
    """Fetch top-level comments only (depth=1)."""
    # Try old.reddit.com first — less aggressive blocking
    for base in ["https://old.reddit.com", "https://www.reddit.com"]:
        url  = f"{base}/comments/{post_id}.json?limit=500&depth=1"
        resp = safe_get(url)
        if resp:
            try:
                data = resp.json()
                if len(data) < 2:
                    continue
                comments = []
                for child in data[1].get("data", {}).get("children", []):
                    if child.get("kind") == "t1":
                        comments.append(child["data"])
                print(f"    💬  {len(comments)} top-level comments via {base}")
                return comments
            except Exception as e:
                print(f"  ⚠️  Comment parse error: {e}", file=sys.stderr)
        time.sleep(2)
    return []


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text):
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     chunk,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"  ⚠️  Telegram error {resp.status_code}: {resp.text}", file=sys.stderr)
        else:
            print(f"  📨  Sent part {i+1}/{len(chunks)} ({len(chunk)} chars)")
        if len(chunks) > 1:
            time.sleep(0.5)


def split_message(text, limit=MAX_TG_CHARS):
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current.strip():
        chunks.append(current.rstrip())
    return chunks or [text[:limit]]


def escape_html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Claude Formatting ─────────────────────────────────────────────────────────
def format_with_claude(username, body, post_title):
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
- Use emojis: 🏀 for games, 📊 for stats/analysis, 💡 for reasoning, ⭐ for strong plays
- Each bet on its own line showing: Player · Stat · Over/Under line · odds if mentioned
- Use ✅ for confident bets, 🔸 for leans
- Keep ALL original analysis and reasoning — just make it cleaner and easier to read
- Preserve any section headers (e.g. "Player Props", "Game Picks")
- Do NOT add anything not in the original
- Use ONLY these HTML tags: <b>, <i> — no markdown, no **, no ##
- Return ONLY the formatted body, no intro, no preamble"""

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
    safe = escape_html(body.strip())
    return "\n".join(f"  {ln}" for ln in safe.splitlines() if ln.strip())


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
    prompt = f"""Extract all individual NBA bets from this comment.
Post: {post_title} | User: u/{username} | Date: {date_str}
Comment:
{body}

Return a JSON array. Each object:
- "description": concise label e.g. "LeBron James Over 25.5 PTS"
- "player": full name or null
- "team": abbreviation or null
- "opponent": abbreviation or null
- "bet_type": "player_prop","spread","moneyline","total","parlay","other"
- "stat": "PTS","REB","AST","3PM","BLK","STL","PRA","PR","PA","RA" or null
- "line": numeric or null
- "direction": "over","under","yes","no" or null
- "confidence": "lean","like","play","strong","fade" or null
Return ONLY valid JSON array, no markdown. Return [] if no bets."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        if resp.ok:
            raw = resp.json()["content"][0]["text"].strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            bets = json.loads(raw)
            return bets if isinstance(bets, list) else []
    except Exception as e:
        print(f"  ⚠️  Claude parse error: {e}", file=sys.stderr)
    return []


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🤖  Bot starting — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    state  = ensure_state_keys(load_state())
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sends  = 0

    posts = get_new_posts_rss(SUBREDDIT)
    print(f"  📥  Fetched {len(posts)} posts via RSS from r/{SUBREDDIT}")

    for post in posts:
        title   = post.get("title", "")
        post_id = post.get("post_id", "")
        post_url= post.get("url", "")

        if not any(kw.lower() in title.lower() for kw in TARGET_KEYWORDS):
            continue

        print(f"  📌  Scanning: {title[:70]}")

        comments = get_top_level_comments(post_id)
        time.sleep(2)

        for comment in comments:
            author = comment.get("author", "")
            if author.lower() not in TARGET_USERS:
                continue

            print(f"  🎯  Found comment from u/{author}")

            cid   = comment.get("id")
            body  = comment.get("body", "")
            chash = body_hash(body)

            is_new    = cid not in state["seen"]
            is_edited = not is_new and state["seen"].get(cid) != chash

            if not (is_new or is_edited):
                print(f"    ⏭️   Already seen")
                continue

            print(f"    {'✅' if is_new else '✏️ '} {'New' if is_new else 'Edited'} — formatting...")
            formatted = format_with_claude(author, body, title)
            message   = build_message(title, post_url, author, formatted, is_edit=is_edited)

            send_telegram(message)
            state["seen"][cid] = chash
            sends += 1

            if is_new:
                bets = parse_bets_with_claude(author, body, title, today)
                state["pending_bets"] = [b for b in state["pending_bets"]
                                          if not b.get("id", "").startswith(f"{cid}_")]
                stored = 0
                for i, bet in enumerate(bets):
                    if not bet.get("description"):
                        continue
                    state["pending_bets"].append({
                        "id": f"{cid}_{i}", "user": author, "date": today,
                        "post_title": title, "post_url": post_url,
                        "description": bet.get("description", body[:100]),
                        "player": bet.get("player"), "team": bet.get("team"),
                        "opponent": bet.get("opponent"), "bet_type": bet.get("bet_type", "other"),
                        "stat": bet.get("stat"), "line": bet.get("line"),
                        "direction": bet.get("direction"), "confidence": bet.get("confidence"),
                        "result": None,
                    })
                    stored += 1
                print(f"    💾  Stored {stored} bet(s)")

    save_state(state)
    print(f"✅  Done — {sends} message(s) sent.")


if __name__ == "__main__":
    main()
