import requests
import json
import os
import hashlib
import sys
import time
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

TARGET_USERS    = {"taraujo", "novel_calendar5168"}
TARGET_KEYWORDS = ["nba props daily", "nba betting", "nba picks"]
SUBREDDIT       = "sportsbook"
STATE_FILE      = "state.json"
MAX_TG_CHARS    = 4000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    if "seen_comments" in state:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        state["seen_comments"] = {
            k: v for k, v in state["seen_comments"].items()
            if v.get("date", "9999") >= cutoff
        }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def ensure_keys(state):
    for k, v in {
        "seen_comments": {},
        "sent_today": {},
        "pending_bets": [],
        "graded_bets": [],
        "stats": {},
    }.items():
        if k not in state:
            state[k] = v
    return state

def body_hash(text):
    return hashlib.md5(text.encode()).hexdigest()

def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def normalize_author(raw):
    """Strip /u/ or u/ prefix, lowercase."""
    name = raw.strip()
    if name.startswith("/u/"):
        name = name[3:]
    elif name.startswith("u/"):
        name = name[2:]
    return name.lower()

# ── RSS helpers ───────────────────────────────────────────────────────────────
ATOM_NS = "http://www.w3.org/2005/Atom"

def fetch_rss(url, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 429:
                print(f"  ⏳  Rate limited, waiting 30s...")
                time.sleep(30)
                continue
            if resp.status_code in (403, 404):
                print(f"  ⚠️  HTTP {resp.status_code} for {url}")
                return None
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            print(f"  ⚠️  Fetch error attempt {attempt+1}: {e}", file=sys.stderr)
            time.sleep(3)
    return None

def parse_atom(content):
    if not content:
        return []
    try:
        root    = ET.fromstring(content)
        entries = []
        for entry in root.findall(f"{{{ATOM_NS}}}entry"):
            link_el    = entry.find(f"{{{ATOM_NS}}}link")
            author_el  = entry.find(f"{{{ATOM_NS}}}author/{{{ATOM_NS}}}name")
            content_el = entry.find(f"{{{ATOM_NS}}}content")
            entries.append({
                "title":   entry.findtext(f"{{{ATOM_NS}}}title", ""),
                "link":    link_el.attrib.get("href", "") if link_el is not None else "",
                "updated": entry.findtext(f"{{{ATOM_NS}}}updated", ""),
                "id":      entry.findtext(f"{{{ATOM_NS}}}id", ""),
                "author":  author_el.text.strip() if author_el is not None else "",
                "content": content_el.text or "" if content_el is not None else "",
            })
        return entries
    except ET.ParseError as e:
        print(f"  ⚠️  RSS parse error: {e}", file=sys.stderr)
        return []

def extract_post_id(url):
    parts = url.rstrip("/").split("/")
    if "comments" in parts:
        idx = parts.index("comments")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return ""

def extract_comment_id(entry_id):
    # entry id looks like: t1_abc1234 or a URL containing it
    match = re.search(r't1_([a-z0-9]+)', entry_id)
    if match:
        return match.group(1)
    return entry_id.rstrip("/").split("/")[-1]

def is_today(updated_str):
    if not updated_str:
        return False
    try:
        dt = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        return dt.date() == datetime.now(timezone.utc).date()
    except Exception:
        return False

def strip_html(html):
    text = html
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    text = text.replace("&#39;", "'").replace("&quot;", '"')
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<(?:p|div|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ── Posts ─────────────────────────────────────────────────────────────────────
def get_todays_matching_posts():
    content = fetch_rss(f"https://www.reddit.com/r/{SUBREDDIT}/new/.rss?limit=50")
    entries = parse_atom(content)
    print(f"  📥  Fetched {len(entries)} posts from RSS")

    matching = []
    for entry in entries:
        title = entry["title"].lower()
        if not any(kw in title for kw in TARGET_KEYWORDS):
            continue
        if not is_today(entry["updated"]):
            print(f"  ⏭️   Old post skipped: {entry['title'][:50]}")
            continue
        post_id = extract_post_id(entry["link"])
        if post_id:
            matching.append({
                "title":   entry["title"],
                "url":     entry["link"],
                "post_id": post_id,
            })
            print(f"  📌  Today's match: {entry['title'][:60]}")
    return matching

# ── Comments ──────────────────────────────────────────────────────────────────
def get_comments_rss(post_id):
    url     = f"https://www.reddit.com/comments/{post_id}/.rss"
    content = fetch_rss(url)
    entries = parse_atom(content)
    time.sleep(2)

    comments = []
    for entry in entries:
        raw_author = entry.get("author", "")
        if not raw_author:
            continue
        body = strip_html(entry.get("content", "")).strip()
        if not body or len(body) < 10:
            continue
        cid = extract_comment_id(entry.get("id", ""))
        comments.append({
            "author_raw": raw_author,
            "author":     normalize_author(raw_author),
            "body":       body,
            "id":         cid,
            "link":       entry.get("link", ""),
        })

    print(f"    💬  {len(comments)} comments fetched")

    # Debug: show all unique authors found
    authors_found = sorted(set(c["author"] for c in comments))
    print(f"    👥  Authors in thread: {authors_found[:15]}")  # show first 15

    return comments

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
        return escape_html(body.strip())

    prompt = f"""You are formatting an NBA betting picks post from Reddit for Telegram.

u/{username} posted this in "{post_title}":

---
{body}
---

Reformat into a clean, easy-to-read Telegram message using HTML only.
Rules:
- Use <b>bold</b> for player names, team names, bet labels
- Emojis: 🏀 for game matchups, 📊 for stats/analysis sections, 💡 for reasoning, ⭐ for strong plays
- Each individual bet on its own line: Player · Stat · Over/Under line · odds if mentioned
- ✅ for confident bets, 🔸 for leans/fades
- Keep ALL original analysis and reasoning — just make it cleaner
- Preserve section headers if any
- Do NOT add anything not in the original
- ONLY use <b> and <i> HTML tags — no markdown, no **, no ##
- Return ONLY the formatted body, no intro, no preamble"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.ok:
            return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  ⚠️  Claude format error: {e}", file=sys.stderr)

    return escape_html(body.strip())

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
        return []
    prompt = f"""Extract all NBA bets from this comment.
Post: {post_title} | User: u/{username} | Date: {date_str}
---
{body}
---
Return JSON array only. Each object:
- "description": e.g. "LeBron James Over 25.5 PTS"
- "player": full name or null
- "team": abbreviation or null
- "opponent": abbreviation or null
- "bet_type": "player_prop","spread","moneyline","total","parlay","other"
- "stat": "PTS","REB","AST","3PM","BLK","STL","PRA","PR","PA","RA" or null
- "line": numeric or null
- "direction": "over","under" or null
- "confidence": "lean","like","play","strong","fade" or null
Return [] if no clear bets. No markdown, no explanation."""

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
            raw = resp.json()["content"][0]["text"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            bets = json.loads(raw)
            return bets if isinstance(bets, list) else []
    except Exception as e:
        print(f"  ⚠️  Claude parse error: {e}", file=sys.stderr)
    return []

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🤖  Bot starting — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    state = ensure_keys(load_state())
    today = today_utc()
    sends = 0

    posts = get_todays_matching_posts()
    print(f"  🎯  {len(posts)} matching post(s) for today")

    if not posts:
        print("  💤  No matching posts today yet — done.")
        save_state(state)
        return

    for post in posts:
        title    = post["title"]
        post_url = post["url"]
        post_id  = post["post_id"]

        comments = get_comments_rss(post_id)

        for comment in comments:
            author_key = comment["author"]  # already normalized/lowercase
            author_display = comment["author_raw"].lstrip("/u/").lstrip("u/")

            if author_key not in TARGET_USERS:
                continue

            print(f"  🎯  Found comment from u/{author_display}")

            cid   = comment["id"]
            body  = comment["body"]
            chash = body_hash(body)

            # Once-per-day-per-user guard
            sent_key = f"{author_key}:{today}"
            if state["sent_today"].get(sent_key):
                print(f"    ⏭️   Already sent u/{author_display}'s picks today")
                continue

            # Duplicate / edit detection
            seen      = state["seen_comments"].get(cid, {})
            is_new    = not seen
            is_edited = not is_new and seen.get("hash") != chash

            if not (is_new or is_edited):
                print(f"    ⏭️   Already seen and unchanged")
                continue

            print(f"    {'✅' if is_new else '✏️ '} {'New' if is_new else 'Edited'} — formatting...")
            formatted = format_with_claude(author_display, body, title)
            message   = build_message(title, post_url, author_display, formatted, is_edit=is_edited)

            send_telegram(message)

            state["seen_comments"][cid] = {"hash": chash, "date": today}
            state["sent_today"][sent_key] = True
            sends += 1

            if is_new:
                bets = parse_bets_with_claude(author_display, body, title, today)
                state["pending_bets"] = [
                    b for b in state["pending_bets"]
                    if not b.get("id", "").startswith(f"{cid}_")
                ]
                stored = 0
                for i, bet in enumerate(bets):
                    if not bet.get("description"):
                        continue
                    state["pending_bets"].append({
                        "id": f"{cid}_{i}", "user": author_display, "date": today,
                        "post_title": title, "post_url": post_url,
                        "description": bet.get("description", ""),
                        "player": bet.get("player"), "team": bet.get("team"),
                        "opponent": bet.get("opponent"),
                        "bet_type": bet.get("bet_type", "other"),
                        "stat": bet.get("stat"), "line": bet.get("line"),
                        "direction": bet.get("direction"),
                        "confidence": bet.get("confidence"),
                        "result": None,
                    })
                    stored += 1
                print(f"    💾  Stored {stored} bet(s)")

    save_state(state)
    print(f"✅  Done — {sends} message(s) sent.")

if __name__ == "__main__":
    main()
