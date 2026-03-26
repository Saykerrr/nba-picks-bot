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

STATE_FILE     = "state.json"
MAX_TG_CHARS   = 4000
LOOKBACK_DAYS  = 3
SUBREDDIT      = "sportsbook"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Multi-sport configuration ─────────────────────────────────────────────────
# Each sport has: keyword patterns for post titles, emoji, display name
SPORT_CONFIG = {
    "nba": {
        "keywords": ["nba props", "nba betting", "nba picks", "nba daily"],
        "emoji":    "🏀",
        "label":    "NBA",
    },
    "mlb": {
        "keywords": ["mlb props", "mlb betting", "mlb picks", "mlb daily", "baseball betting"],
        "emoji":    "⚾",
        "label":    "MLB",
    },
    "ncaabb": {
        "keywords": [
            "ncaabb", "ncaa basketball", "college basketball",
            "cbb betting", "cbb picks", "cbb props",
            "march madness", "ncaa bb",
        ],
        "emoji":    "🏀",
        "label":    "NCAABB",
    },
}

# Target users and which sports they post about
# The bot will look for ANY of these users in ANY matching thread
TARGET_USERS = {
    "taraujo":              {"nba", "mlb"},
    "novel_calendar5168":   {"nba", "mlb"},
    "wnba_prodigy":         {"ncaabb"},
}

# Emojis that indicate post-game edits (check marks, etc.)
# Stripped before hashing to avoid re-sending on cosmetic edits
RESULT_EMOJIS = re.compile(
    r'[\u2705\u274C\u2714\uFE0F\u2611\u2612\u2B50\U0001F525\U0001F4B0\U0001F4C8'
    r'\U0001F4C9\u2B06\u2B07\U0001F7E2\U0001F534\U0001F7E1\U0001F44D\U0001F44E'
    r'\U0001F3C6\U0001F389\U0001F4A5\U0001F4AF]'
)


# ── Date extraction from post title ──────────────────────────────────────────
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
    """Detect sport from post title.  Returns sport key or None."""
    lower = title.lower()
    for sport, cfg in SPORT_CONFIG.items():
        if any(kw in lower for kw in cfg["keywords"]):
            return sport
    return None


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
    if "sent_today" in state:
        if "sent_per_post" not in state:
            state["sent_per_post"] = {}
        del state["sent_today"]
    for k, v in {
        "seen_comments": {}, "sent_per_post": {},
        "pending_bets": [], "graded_bets": [], "stats": {},
    }.items():
        if k not in state:
            state[k] = v
    return state


def smart_body_hash(text):
    """
    Hash the body AFTER stripping result emojis (✅❌🔥💰📈 etc).
    This prevents re-sending when a user just adds checkmarks to hits.
    """
    cleaned = RESULT_EMOJIS.sub("", text)
    # Also strip leading/trailing whitespace changes per line
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines())
    return hashlib.md5(cleaned.encode()).hexdigest()


def normalize_author(raw):
    name = raw.strip()
    for prefix in ("/u/", "u/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
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


def is_recent(updated_str, days=LOOKBACK_DAYS):
    if not updated_str:
        return False
    try:
        dt = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        return dt >= datetime.now(timezone.utc) - timedelta(days=days)
    except Exception:
        return False


def strip_html(html):
    text = html
    for old, new in [
        ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
        ("&#39;", "'"), ("&quot;", '"'),
        ("<br>", "\n"), ("<br/>", "\n"), ("<br />", "\n"),
    ]:
        text = text.replace(old, new)
    text = re.sub(r"<(?:p|div|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Posts ─────────────────────────────────────────────────────────────────────
def get_recent_matching_posts():
    """
    Fetch matching posts from the last LOOKBACK_DAYS days.
    Scans for ALL configured sports (NBA, MLB, NCAABB).
    """
    all_entries = []

    # Source 1: /new feed
    content = fetch_rss(
        f"https://www.reddit.com/r/{SUBREDDIT}/new/.rss?limit=50"
    )
    all_entries.extend(parse_atom(content))
    time.sleep(2)

    # Source 2: search for each sport
    all_keywords = set()
    for cfg in SPORT_CONFIG.values():
        all_keywords.update(cfg["keywords"][:2])  # Top 2 per sport

    search_q = "+OR+".join(kw.replace(" ", "+") for kw in list(all_keywords)[:6])
    content2 = fetch_rss(
        f"https://www.reddit.com/r/{SUBREDDIT}/search.rss"
        f"?q={search_q}&restrict_sr=1&sort=new&t=week&limit=25"
    )
    all_entries.extend(parse_atom(content2))

    print(f"  📥  Fetched {len(all_entries)} total entries from RSS")

    # Deduplicate and filter
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
        matching.append({
            "title":   entry["title"],
            "url":     entry["link"],
            "post_id": post_id,
            "sport":   sport,
        })
        emoji = SPORT_CONFIG[sport]["emoji"]
        print(f"  📌  {emoji} [{sport.upper()}] {entry['title'][:60]}")
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
        comments.append({
            "author_raw": raw_author,
            "author":     normalize_author(raw_author),
            "body":       body,
            "id":         cid,
            "link":       entry.get("link", ""),
        })
    print(f"    💬  {len(comments)} comments fetched")
    return comments


# ── Comment Relevance Check ───────────────────────────────────────────────────
def is_picks_comment(body, sport):
    """
    Quick check: does this comment contain actual betting picks?
    Filters out short chatter like "6-2 yesterday was great" or "tailing!".
    Uses fast heuristic first, falls back to Claude for ambiguous cases.
    """
    # ── Fast filters ──
    # Very short comments are almost never picks
    if len(body) < 80:
        print(f"    ⏭️  Too short ({len(body)} chars) — skipping")
        return False

    # Heuristic: picks comments usually contain numbers + betting keywords
    has_numbers  = bool(re.search(r'\d+\.?\d*', body))
    bet_keywords = re.compile(
        r'\b(over|under|o\d|u\d|parlay|prop|spread|moneyline|pts|reb|ast|3pm|'
        r'pra|RA\b|PA\b|PR\b|hits|HR\b|rbi|strikeouts|total bases|'
        r'picks|play|lean|fade|bol|best of luck|record|lock)\b',
        re.IGNORECASE
    )
    keyword_count = len(bet_keywords.findall(body))

    # Strong signal: multiple betting keywords + numbers → definitely picks
    if has_numbers and keyword_count >= 3:
        return True

    # Strong non-signal: no numbers at all → definitely not picks
    if not has_numbers:
        print(f"    ⏭️  No numbers found — not a picks comment")
        return False

    # ── Ambiguous → ask Claude (cheap, fast) ──
    if not ANTHROPIC_API_KEY:
        # No API key → be permissive
        return keyword_count >= 1

    sport_label = SPORT_CONFIG.get(sport, {}).get("label", "sports")
    prompt = f"""Is this Reddit comment an {sport_label} betting picks post with actual specific bets?
Answer YES if it contains player props, parlays, specific over/under picks, or game predictions with lines.
Answer NO if it's just a recap of past results, a "tailing" reply, a record update, social media plug, thanks message, or generic commentary.

Comment:
---
{body[:1500]}
---

Reply with ONLY "YES" or "NO"."""

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
                "max_tokens": 5,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        if resp.ok:
            answer = resp.json()["content"][0]["text"].strip().upper()
            if "NO" in answer:
                print(f"    ⏭️  Claude says not picks — skipping")
                return False
            return True
    except Exception as e:
        print(f"    ⚠️  Relevance check failed: {e}", file=sys.stderr)

    # Default: allow if has some keywords
    return keyword_count >= 1


def is_meaningful_edit(old_body, new_body):
    """
    Determine if an edit is meaningful (new picks, changed lines, injury news)
    vs cosmetic (added ✅/❌ emojis, minor wording).
    """
    # Strip result emojis from both versions
    old_clean = RESULT_EMOJIS.sub("", old_body).strip()
    new_clean = RESULT_EMOJIS.sub("", new_body).strip()

    # If the cleaned versions are identical, this is just emoji additions
    old_lines = set(line.strip() for line in old_clean.splitlines() if line.strip())
    new_lines = set(line.strip() for line in new_clean.splitlines() if line.strip())

    added   = new_lines - old_lines
    removed = old_lines - new_lines

    if not added and not removed:
        return False

    # Check if added content is substantive (contains picks-like content)
    added_text = " ".join(added)
    has_numbers = bool(re.search(r'\d+\.?\d*', added_text))
    has_keywords = bool(re.search(
        r'\b(over|under|o\d|u\d|parlay|prop|spread|pts|reb|ast|3pm|'
        r'hits|HR|rbi|strikeouts|injured|out|scratch|lineup|changed)\b',
        added_text, re.IGNORECASE
    ))

    if has_numbers or has_keywords:
        return True

    # Significant amount of new text → meaningful
    if len(added_text) > 100:
        return True

    print(f"    ⏭️  Edit is cosmetic (emojis/minor wording) — skipping")
    return False


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
            print(f"  ⚠️  Telegram error {resp.status_code}: {resp.text}",
                  file=sys.stderr)
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
    print(f"    🤖  Formatting with Claude...")

    if not ANTHROPIC_API_KEY:
        return escape_html(body.strip())

    sport_label = SPORT_CONFIG.get(sport, {}).get("label", "Sports")

    edit_note = (
        "NOTE: This is an EDITED version — start with "
        "'✏️ <b>EDITED</b>' on its own line.\n\n"
        if is_edit else ""
    )

    # Sport-specific emoji guidance
    if sport == "mlb":
        emoji_guide = """EMOJIS (do NOT use ✅ or ❌ — reserved for graded results only):
- ⚾ before every game matchup header
- 🎯 before every individual pick/bet line
- ⚠️ before injury or lineup news
- 📊 before supporting stats or trends
- 💡 before reasoning or analysis
- ⭐ for the analyst's strongest play of the night
- 🎰 before parlay suggestions and parlay legs"""
    elif sport == "ncaabb":
        emoji_guide = """EMOJIS (do NOT use ✅ or ❌ — reserved for graded results only):
- 🏀 before every game matchup header
- 🎯 before every individual pick/bet line
- ⚠️ before injury or lineup news
- 📊 before supporting stats or trends
- 💡 before reasoning or analysis
- ⭐ for the analyst's strongest play of the night
- 🎰 before parlay suggestions and parlay legs"""
    else:
        emoji_guide = """EMOJIS (do NOT use ✅ or ❌ — reserved for graded results only):
- 🏀 before every game matchup header
- 🎯 before every individual pick/bet line
- ⚠️ before injury or lineup news
- 📊 before supporting stats or trends
- 💡 before reasoning or analysis
- ⭐ for the analyst's strongest play of the night
- 🎰 before parlay suggestions and parlay legs"""

    prompt = f"""{edit_note}You are formatting a {sport_label} betting analyst's Reddit post for Telegram messenger.

The analyst u/{username} posted this in "{post_title}":

---
{body}
---

Transform this into a clean, well-formatted Telegram message. Follow these rules STRICTLY:

{emoji_guide}

FORMATTING:
- <b>bold</b> every player name and team name
- Each game gets its own bold header with a blank line before it
- Each bet/pick on its own line
- Parlay section clearly separated with header

ODDS — always convert American odds to decimal:
- Positive: decimal = (odds/100) + 1  →  +200 = 3.00, +600 = 7.00, +120 = 2.20
- Negative: decimal = (100/|odds|) + 1  →  -130 = 1.77, -110 = 1.91
- Show as "(3.00)" replacing "(+200)"

CONTENT:
- Keep ALL betting picks, analysis, injury news, stats, and odds
- REMOVE: thank you messages, follower counts, season records, "buy me a coffee", social media plugs, any ✅ or ❌ result emojis from past bets
- Keep focused on tonight's bets and analysis only

OUTPUT:
- ONLY <b> and <i> HTML tags — no markdown, no **, no ##
- Return ONLY the formatted message, no intro, no preamble"""

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
                "max_tokens": 3000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=40,
        )
        if resp.ok:
            result = resp.json()["content"][0]["text"].strip()
            print(f"    ✅  Claude formatted ({len(result)} chars)")
            return result
        else:
            print(f"    ⚠️  Claude API error: {resp.status_code}",
                  file=sys.stderr)
    except Exception as e:
        print(f"    ⚠️  Claude error: {e}", file=sys.stderr)

    return escape_html(body.strip())


def build_message(post_title, post_url, username, formatted_body, sport, is_edit=False):
    edit_tag = "  ✏️ <i>(edited)</i>" if is_edit else ""
    emoji = SPORT_CONFIG.get(sport, {}).get("emoji", "🏀")
    return (
        f"{emoji} <b>{escape_html(post_title)}</b>\n"
        f"{'━' * 30}\n"
        f"💬 <b>u/{username}</b>{edit_tag}\n\n"
        f"{formatted_body}\n\n"
        f"🔗 <a href='{post_url}'>View on Reddit</a>"
    )


# ── Bet Parsing ───────────────────────────────────────────────────────────────
def parse_bets_with_claude(username, body, post_title, date_str, sport):
    if not ANTHROPIC_API_KEY:
        return []

    sport_label = SPORT_CONFIG.get(sport, {}).get("label", "Sports")

    # Sport-specific stat guidance
    if sport == "mlb":
        stat_guide = (
            '- "stat": "H","HR","RBI","R","SB","TB","K","BB","ER","IP","HITS_ALLOWED" or null\n'
            '- For pitcher props use stat like "K" (strikeouts), "ER", "IP"\n'
            '- For batter props use "H" (hits), "HR", "RBI", "R" (runs), "SB", "TB" (total bases)'
        )
    else:
        stat_guide = (
            '- "stat": "PTS","REB","AST","3PM","BLK","STL","PRA","PR","PA","RA" or null\n'
            '- Use "3PM" for three-pointers made, even if the user writes "3s" or "3\'s" or "threes"'
        )

    prompt = f"""Extract ALL {sport_label} bets from this comment — both individual plays AND parlays.

Post: {post_title} | User: u/{username} | Date: {date_str} | Sport: {sport_label}
---
{body}
---

Return a JSON array. Each object must have:
- "description": clear label e.g. "Player Over 12.5 STAT" or "Parlay 1: Leg1 + Leg2"
- "player": full name or null (null for parlays)
- "team": abbreviation or null
- "opponent": abbreviation or null
- "bet_type": "player_prop", "parlay", "spread", "moneyline", "total", or "other"
{stat_guide}
- "line": numeric or null (null for parlays)
- "direction": "over","under" or null (null for parlays)
- "confidence": "lean","like","play","strong","fade" or null

IMPORTANT:
- Include ALL individual plays listed
- Include ALL parlays as separate entries
- For parlays, use " + " between legs in the "description" field
- Do NOT skip any bets
- Return [] only if truly no bets exist
- No markdown, no explanation, just valid JSON array"""

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
            raw = (resp.json()["content"][0]["text"].strip()
                   .replace("```json", "").replace("```", "").strip())
            bets = json.loads(raw)
            return bets if isinstance(bets, list) else []
    except Exception as e:
        print(f"  ⚠️  Claude parse error: {e}", file=sys.stderr)
    return []


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🤖  Bot starting — "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"    Anthropic key: {'✅ set' if ANTHROPIC_API_KEY else '❌ NOT SET'}")
    print(f"    Lookback: {LOOKBACK_DAYS} days | Sports: "
          f"{', '.join(s.upper() for s in SPORT_CONFIG)}")
    print(f"    Users: {', '.join(TARGET_USERS.keys())}")

    state = ensure_keys(load_state())
    sends = 0

    posts = get_recent_matching_posts()
    print(f"  🎯  {len(posts)} matching post(s) found")

    if not posts:
        print("  💤  No matching posts — done.")
        save_state(state)
        return

    for post in posts:
        title    = post["title"]
        post_url = post["url"]
        post_id  = post["post_id"]
        sport    = post["sport"]

        post_date = extract_date_from_title(title)
        emoji = SPORT_CONFIG[sport]["emoji"]
        print(f"\n  {emoji}  [{sport.upper()}] {post_date} | {title[:60]}")

        comments = get_comments_rss(post_id)

        # Group by user — pick the longest comment (main writeup)
        user_comments = {}
        for comment in comments:
            ak = comment["author"]
            if ak not in TARGET_USERS:
                continue
            # Check if this user tracks this sport
            if sport not in TARGET_USERS[ak]:
                continue
            if (ak not in user_comments
                    or len(comment["body"]) > len(user_comments[ak]["body"])):
                user_comments[ak] = comment

        for author_key, comment in user_comments.items():
            author_display = (comment["author_raw"].strip()
                              .lstrip("/u/").lstrip("u/"))
            print(f"  🎯  u/{author_display} ({len(comment['body'])} chars)")

            cid  = comment["id"]
            body = comment["body"]

            # ── Relevance check — skip non-picks comments ──
            if not is_picks_comment(body, sport):
                continue

            chash = smart_body_hash(body)
            seen  = state["seen_comments"].get(cid, {})
            is_new    = not seen
            is_edited = not is_new and seen.get("hash") != chash

            sent_key = f"{author_key}:{post_id}"

            if not is_new and not is_edited:
                print(f"    ⏭️   Already seen and unchanged")
                continue

            if is_edited:
                # Check if the edit is meaningful
                old_body = seen.get("body_preview", "")
                if old_body and not is_meaningful_edit(old_body, body):
                    # Update hash silently so we don't re-check next time
                    state["seen_comments"][cid]["hash"] = chash
                    continue
                print(f"    ✏️   Meaningful edit detected — resending")
            elif state["sent_per_post"].get(sent_key):
                print(f"    ⏭️   Already sent for this post — skipping")
                continue

            tag = "✅ New" if is_new else "✏️  Edited"
            print(f"    {tag} — formatting with Claude...")
            formatted = format_with_claude(
                author_display, body, title, sport, is_edit=is_edited
            )
            message = build_message(
                title, post_url, author_display, formatted, sport,
                is_edit=is_edited
            )

            send_telegram(message)
            state["seen_comments"][cid] = {
                "hash": chash,
                "date": post_date,
                "body_preview": body[:500],  # Store for meaningful edit comparison
            }
            state["sent_per_post"][sent_key] = True
            sends += 1

            if is_new:
                bets = parse_bets_with_claude(
                    author_display, body, title, post_date, sport
                )
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
                        "user":        author_display,
                        "date":        post_date,
                        "sport":       sport,
                        "post_title":  title,
                        "post_url":    post_url,
                        "description": bet.get("description", ""),
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
                print(f"    💾  Stored {stored} bet(s) for {post_date}")

    save_state(state)
    print(f"\n✅  Done — {sends} message(s) sent.")


if __name__ == "__main__":
    main()
