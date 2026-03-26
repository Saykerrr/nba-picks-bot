"""
NBA/MLB/NCAABB Reddit Picks Bot  —  v4.0  (2026-03-26)
Fetches comments from target users on r/sportsbook, formats with Claude,
sends to Telegram, and stores bets for grading.
"""

import requests
import json
import os
import hashlib
import sys
import time
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

VERSION = "4.0"

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

STATE_FILE     = "state.json"
MAX_TG_CHARS   = 4000
LOOKBACK_DAYS  = 5
SUBREDDIT      = "sportsbook"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Multi-sport configuration ────────────────────────────────────────────────
SPORT_CONFIG = {
    "nba": {
        "keywords": ["nba props", "nba betting", "nba picks", "nba daily"],
        "emoji": "🏀", "label": "NBA",
    },
    "mlb": {
        "keywords": ["mlb props", "mlb betting", "mlb picks", "mlb daily", "baseball betting"],
        "emoji": "⚾", "label": "MLB",
    },
    "ncaabb": {
        "keywords": ["ncaabb", "ncaa basketball", "college basketball",
                     "cbb betting", "cbb picks", "cbb props", "march madness", "ncaa bb"],
        "emoji": "🏀", "label": "NCAABB",
    },
}

TARGET_USERS = {
    "taraujo":            {"nba", "mlb"},
    "novel_calendar5168": {"nba", "mlb"},
    "wnba_prodigy":       {"ncaabb"},
}

# Emojis stripped before hashing (so adding ✅❌ doesn't trigger re-send)
RESULT_EMOJIS = re.compile(
    r'[\u2705\u274C\u2714\uFE0F\u2611\u2612\u2B50\U0001F525\U0001F4B0\U0001F4C8'
    r'\U0001F4C9\u2B06\u2B07\U0001F7E2\U0001F534\U0001F7E1\U0001F44D\U0001F44E'
    r'\U0001F3C6\U0001F389\U0001F4A5\U0001F4AF]'
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def extract_date_from_title(title):
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', title)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return f"{year:04d}-{month:02d}-{day:02d}"
        except Exception:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def detect_sport(title):
    lower = title.lower()
    for sport, cfg in SPORT_CONFIG.items():
        if any(kw in lower for kw in cfg["keywords"]):
            return sport
    return None


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
    if "sent_today" in state:
        state.pop("sent_today", None)
    for k, v in {"seen_comments": {}, "sent_per_post": {},
                 "pending_bets": [], "graded_bets": [], "stats": {}}.items():
        if k not in state:
            state[k] = v
    return state


def smart_body_hash(text):
    cleaned = RESULT_EMOJIS.sub("", text)
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines())
    return hashlib.md5(cleaned.encode()).hexdigest()


def normalize_author(raw):
    name = raw.strip()
    for prefix in ("/u/", "u/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.lower()


# ── RSS ───────────────────────────────────────────────────────────────────────
ATOM_NS = "http://www.w3.org/2005/Atom"


def fetch_rss(url, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 429:
                time.sleep(30)
                continue
            if resp.status_code in (403, 404):
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
        root = ET.fromstring(content)
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
    match = re.search(r't1_([a-z0-9]+)', entry_id)
    return match.group(1) if match else entry_id.rstrip("/").split("/")[-1]


def is_recent(updated_str):
    if not updated_str:
        return False
    try:
        dt = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        return dt >= datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    except Exception:
        return False


def strip_html(html):
    text = html
    for old, new in [("&lt;","<"),("&gt;",">"),("&amp;","&"),("&#39;","'"),
                     ("&quot;",'"'),("<br>","\n"),("<br/>","\n"),("<br />","\n")]:
        text = text.replace(old, new)
    text = re.sub(r"<(?:p|div|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Posts ─────────────────────────────────────────────────────────────────────
def get_recent_matching_posts():
    all_entries = []
    content = fetch_rss(f"https://www.reddit.com/r/{SUBREDDIT}/new/.rss?limit=50")
    all_entries.extend(parse_atom(content))
    time.sleep(2)
    search_q = "NBA+Props+Daily+OR+NBA+Betting+OR+NBA+Picks+OR+MLB+Props+OR+College+Basketball"
    content2 = fetch_rss(
        f"https://www.reddit.com/r/{SUBREDDIT}/search.rss"
        f"?q={search_q}&restrict_sr=1&sort=new&t=week&limit=25"
    )
    all_entries.extend(parse_atom(content2))
    print(f"  📥  Fetched {len(all_entries)} total RSS entries")

    seen_ids = set()
    matching = []
    for entry in all_entries:
        sport = detect_sport(entry["title"])
        if not sport:
            continue
        if not is_recent(entry["updated"]):
            continue
        post_id = extract_post_id(entry["link"])
        if not post_id or post_id in seen_ids:
            continue
        seen_ids.add(post_id)
        matching.append({"title": entry["title"], "url": entry["link"],
                         "post_id": post_id, "sport": sport})
        print(f"  📌  [{sport.upper()}] {entry['title'][:60]}")
    return matching


# ── Comments ──────────────────────────────────────────────────────────────────
def get_comments_rss(post_id):
    content = fetch_rss(f"https://www.reddit.com/comments/{post_id}/.rss")
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
        comments.append({"author_raw": raw_author, "author": normalize_author(raw_author),
                         "body": body, "id": cid, "link": entry.get("link", "")})
    print(f"    💬  {len(comments)} comments fetched")
    return comments


# ── Comment relevance check ──────────────────────────────────────────────────
def is_picks_comment(body, sport):
    if len(body) < 80:
        return False
    has_numbers = bool(re.search(r'\d+\.?\d*', body))
    bet_kw = re.compile(
        r'\b(over|under|o\d|u\d|parlay|prop|spread|moneyline|pts|reb|ast|3pm|'
        r'pra|RA\b|PA\b|PR\b|hits|HR\b|rbi|strikeouts|total bases|'
        r'picks|play|lean|fade|bol|lock)\b', re.IGNORECASE
    )
    keyword_count = len(bet_kw.findall(body))
    if has_numbers and keyword_count >= 3:
        return True
    if not has_numbers:
        return False
    if not ANTHROPIC_API_KEY:
        return keyword_count >= 1
    sport_label = SPORT_CONFIG.get(sport, {}).get("label", "sports")
    prompt = (f"Is this Reddit comment an {sport_label} betting picks post with actual "
              f"specific bets (player props, parlays, over/under picks)?\n"
              f"Answer NO if it's just a recap, record update, or generic chat.\n\n"
              f"Comment:\n---\n{body[:1500]}\n---\n\nReply ONLY \"YES\" or \"NO\".")
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 5,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=15)
        if resp.ok:
            answer = resp.json()["content"][0]["text"].strip().upper()
            if "NO" in answer:
                print(f"    ⏭️  Not a picks comment — skipping")
                return False
    except Exception:
        pass
    return keyword_count >= 1


def is_meaningful_edit(old_body, new_body):
    old_clean = RESULT_EMOJIS.sub("", old_body).strip()
    new_clean = RESULT_EMOJIS.sub("", new_body).strip()
    old_lines = set(line.strip() for line in old_clean.splitlines() if line.strip())
    new_lines = set(line.strip() for line in new_clean.splitlines() if line.strip())
    added = new_lines - old_lines
    if not added:
        return False
    added_text = " ".join(added)
    has_numbers = bool(re.search(r'\d+\.?\d*', added_text))
    has_keywords = bool(re.search(
        r'\b(over|under|o\d|u\d|parlay|prop|spread|pts|reb|ast|3pm|'
        r'hits|HR|rbi|strikeouts|injured|out|scratch|lineup|changed)\b',
        added_text, re.IGNORECASE))
    if has_numbers or has_keywords or len(added_text) > 100:
        return True
    return False


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text):
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15)
        if not resp.ok:
            print(f"  ⚠️  Telegram error {resp.status_code}: {resp.text}", file=sys.stderr)
        else:
            print(f"  📨  Sent part {i+1}/{len(chunks)} ({len(chunk)} chars)")
        if len(chunks) > 1:
            time.sleep(1)


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
def format_with_claude(username, body, post_title, sport, is_edit=False):
    if not ANTHROPIC_API_KEY:
        return escape_html(body.strip())
    sport_label = SPORT_CONFIG.get(sport, {}).get("label", "Sports")
    sport_emoji = "⚾" if sport == "mlb" else "🏀"
    edit_note = "NOTE: This is an EDITED version — start with '✏️ <b>EDITED</b>' on its own line.\n\n" if is_edit else ""

    prompt = f"""{edit_note}You are formatting a {sport_label} betting analyst's Reddit post for Telegram.

u/{username} posted in "{post_title}":

---
{body}
---

Format as a clean Telegram message:

EMOJIS (NEVER use ✅ or ❌ — reserved for results):
- {sport_emoji} before game matchup headers
- 🎯 before each pick/bet
- ⚠️ before injury/lineup news
- 📊 before stats/trends
- 💡 before analysis/reasoning
- ⭐ for the strongest play
- 🎰 before parlays

RULES:
- <b>bold</b> player and team names
- Convert American odds to decimal: +200→3.00, -130→1.77
- REMOVE: thank yous, follower counts, records, tip jars, social plugs, any ✅/❌ emojis
- ONLY use <b> and <i> HTML tags
- Return ONLY the formatted message"""

    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 3000,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=40)
        if resp.ok:
            return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"    ⚠️  Claude error: {e}", file=sys.stderr)
    return escape_html(body.strip())


def build_message(post_title, post_url, username, formatted_body, sport, is_edit=False):
    edit_tag = "  ✏️ <i>(edited)</i>" if is_edit else ""
    emoji = SPORT_CONFIG.get(sport, {}).get("emoji", "🏀")
    return (
        f"{emoji} <b>{escape_html(post_title)}</b>\n"
        f"───────────────\n"
        f"💬 <b>u/{username}</b>{edit_tag}\n\n"
        f"{formatted_body}\n\n"
        f"🔗 <a href='{post_url}'>View on Reddit</a>"
    )


# ── Bet Parsing (uses Sonnet for accuracy) ────────────────────────────────────
def parse_bets_with_claude(username, body, post_title, date_str, sport):
    if not ANTHROPIC_API_KEY:
        return []
    sport_label = SPORT_CONFIG.get(sport, {}).get("label", "Sports")
    if sport == "mlb":
        stat_guide = ('- "stat": "H","HR","RBI","R","SB","TB","K","BB","ER","IP","HITS_ALLOWED" or null\n'
                      '- For pitcher props: "K" (strikeouts), "ER", "IP"\n'
                      '- For batter props: "H" (hits), "HR", "RBI", "R" (runs), "SB", "TB"')
    else:
        stat_guide = ('- "stat": "PTS","REB","AST","3PM","BLK","STL","PRA","PR","PA","RA" or null\n'
                      '- Use "3PM" for three-pointers, even if user writes "3s" or "3\'s"')

    prompt = f"""You are a precise sports betting data extractor. Extract ALL {sport_label} bets from this Reddit comment.

Post: {post_title} | User: u/{username} | Date: {date_str}
---
{body}
---

Return a JSON array. Each element:
- "description": COMPLETE bet, e.g. "Dejounte Murray Over 11.5 RA" or "Parlay 1: Dejounte Murray O11.5 RA + Devin Booker O5.5 AST"
- "player": full name or null (null for parlays)
- "team": abbreviation or null
- "opponent": abbreviation or null
- "bet_type": "player_prop" | "parlay" | "spread" | "moneyline" | "total" | "other"
{stat_guide}
- "line": number — REQUIRED for player_prop
- "direction": "over" | "under" — REQUIRED for player_prop
- "confidence": "lean" | "like" | "play" | "strong" | "fade" | null

ABSOLUTE RULES:
1. Every player_prop MUST have: full player name, stat, numeric line, direction. NEVER output incomplete bets like "Murray RA" or "Booker" alone.
2. PARLAYS: Users often write parlay legs as shorthand (e.g., "Murray RA + Booker AST + Banchero PTS"). These reference their individual picks listed ABOVE in the same comment. You MUST look up each reference and expand to the complete bet with the exact line number. Example: if individual picks list "Dejounte Murray Over 11.5 RA" and "Devin Booker Over 5.5 AST", then "Murray RA + Booker AST" becomes "Parlay: Dejounte Murray O11.5 RA + Devin Booker O5.5 AST".
3. If you cannot find the line number for a parlay leg reference, SKIP that entire parlay. Never include a parlay with vague or incomplete legs.
4. Use " + " between parlay legs.
5. Return ONLY valid JSON. No markdown fences, no explanation."""

    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 3000,
                  "messages": [{"role": "user", "content": prompt}]}, timeout=45)
        if resp.ok:
            raw = resp.json()["content"][0]["text"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            bets = json.loads(raw)
            if not isinstance(bets, list):
                return []
            # ── Strict validation ──
            valid = []
            for bet in bets:
                bt = bet.get("bet_type", "other")
                if bt == "player_prop":
                    if not bet.get("player"):
                        print(f"    🗑️  Rejected (no player): {bet.get('description','')[:50]}")
                        continue
                    if bet.get("line") is None:
                        print(f"    🗑️  Rejected (no line): {bet.get('description','')[:50]}")
                        continue
                    if not bet.get("direction"):
                        print(f"    🗑️  Rejected (no direction): {bet.get('description','')[:50]}")
                        continue
                    if not bet.get("stat"):
                        print(f"    🗑️  Rejected (no stat): {bet.get('description','')[:50]}")
                        continue
                elif bt == "parlay":
                    desc = bet.get("description", "")
                    # Check each leg has a number (line)
                    legs_text = re.sub(r'^[^:]+:\s*', '', desc)
                    legs = re.split(r'\s+\+\s+', legs_text)
                    bad_leg = False
                    for leg in legs:
                        if not re.search(r'\d+\.?\d*', leg):
                            print(f"    🗑️  Rejected parlay (leg missing line): {leg[:40]}")
                            bad_leg = True
                            break
                    if bad_leg:
                        continue
                valid.append(bet)
            dropped = len(bets) - len(valid)
            if dropped:
                print(f"    🗑️  Dropped {dropped} malformed bet(s)")
            return valid
    except Exception as e:
        print(f"  ⚠️  Claude parse error: {e}", file=sys.stderr)
    return []


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🤖  Bot v{VERSION} starting — "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"    Lookback: {LOOKBACK_DAYS} days")

    state = ensure_keys(load_state())
    sends = 0
    posts = get_recent_matching_posts()
    print(f"  🎯  {len(posts)} matching post(s)")

    if not posts:
        print("  💤  No matching posts — done.")
        save_state(state)
        return

    for post in posts:
        title, post_url, post_id, sport = post["title"], post["url"], post["post_id"], post["sport"]
        post_date = extract_date_from_title(title)
        print(f"\n  📅  [{sport.upper()}] {post_date} | {title[:60]}")

        comments = get_comments_rss(post_id)
        user_comments = {}
        for comment in comments:
            ak = comment["author"]
            if ak not in TARGET_USERS or sport not in TARGET_USERS[ak]:
                continue
            if ak not in user_comments or len(comment["body"]) > len(user_comments[ak]["body"]):
                user_comments[ak] = comment

        for author_key, comment in user_comments.items():
            author_display = comment["author_raw"].strip().lstrip("/u/").lstrip("u/")
            print(f"  🎯  u/{author_display} ({len(comment['body'])} chars)")

            cid, body = comment["id"], comment["body"]

            if not is_picks_comment(body, sport):
                continue

            chash     = smart_body_hash(body)
            seen      = state["seen_comments"].get(cid, {})
            is_new    = not seen
            is_edited = not is_new and seen.get("hash") != chash
            sent_key  = f"{author_key}:{post_id}"

            if not is_new and not is_edited:
                print(f"    ⏭️   Unchanged")
                continue
            if is_edited:
                old_body = seen.get("body_preview", "")
                if old_body and not is_meaningful_edit(old_body, body):
                    state["seen_comments"][cid]["hash"] = chash
                    continue
                print(f"    ✏️   Meaningful edit — resending")
            elif state["sent_per_post"].get(sent_key):
                print(f"    ⏭️   Already sent")
                continue

            formatted = format_with_claude(author_display, body, title, sport, is_edit=is_edited)
            message = build_message(title, post_url, author_display, formatted, sport, is_edit=is_edited)
            send_telegram(message)
            state["seen_comments"][cid] = {"hash": chash, "date": post_date, "body_preview": body[:500]}
            state["sent_per_post"][sent_key] = True
            sends += 1

            if is_new:
                bets = parse_bets_with_claude(author_display, body, title, post_date, sport)
                state["pending_bets"] = [b for b in state["pending_bets"]
                                         if not b.get("id", "").startswith(f"{cid}_")]
                stored = 0
                for i, bet in enumerate(bets):
                    if not bet.get("description"):
                        continue
                    state["pending_bets"].append({
                        "id": f"{cid}_{i}", "user": author_display,
                        "date": post_date, "sport": sport,
                        "post_title": title, "post_url": post_url,
                        "description": bet.get("description", ""),
                        "player": bet.get("player"), "team": bet.get("team"),
                        "opponent": bet.get("opponent"),
                        "bet_type": bet.get("bet_type", "other"),
                        "stat": bet.get("stat"), "line": bet.get("line"),
                        "direction": bet.get("direction"),
                        "confidence": bet.get("confidence"), "result": None,
                    })
                    stored += 1
                print(f"    💾  Stored {stored} bet(s)")

    save_state(state)
    print(f"\n✅  Bot v{VERSION} done — {sends} message(s) sent.")


if __name__ == "__main__":
    main()
