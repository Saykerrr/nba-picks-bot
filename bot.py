"""bot.py v5 — Reddit Picks Bot (2026-03-26)"""
import requests, json, os, hashlib, sys, time, re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

V = "5.0"
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
STATE_FILE = "state.json"
LOOKBACK_DAYS = 5
SUBREDDIT = "sportsbook"
HDRS = {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36","Accept":"*/*"}
ATOM = "http://www.w3.org/2005/Atom"

# ── Sports & Users ────────────────────────────────────────────────────────────
SPORTS = {
    "nba":    {"kw":["nba props","nba betting","nba picks","nba daily"],"emoji":"🏀","label":"NBA"},
    "mlb":    {"kw":["mlb props","mlb betting","mlb picks","mlb daily","baseball betting"],"emoji":"⚾","label":"MLB"},
    "ncaabb": {"kw":["ncaabb","ncaa basketball","college basketball","cbb betting","cbb picks",
                     "cbb daily","march madness","ncaa bb","ncaab "],"emoji":"🏀","label":"NCAABB"},
}
USERS = {"taraujo":{"nba","mlb"},"novel_calendar5168":{"nba","mlb"},"wnba_prodigy":{"ncaabb"}}

# Emojis that users add post-game — stripped before hashing
POSTGAME_RE = re.compile(r'[\u2705\u274C\u2714\uFE0F\u2611\u2612\U0001F525\U0001F4B0\U0001F4C8\U0001F4C9\U0001F7E2\U0001F534\U0001F44D\U0001F44E\U0001F3C6\U0001F389\U0001F4AF]')

# ── Helpers ───────────────────────────────────────────────────────────────────
def detect_sport(title):
    t = title.lower()
    for s, c in SPORTS.items():
        if any(k in t for k in c["kw"]): return s
    return None

def date_from_title(title):
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', title)
    if m:
        mo,d,y = int(m.group(1)),int(m.group(2)),int(m.group(3))
        if y<100: y+=2000
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def fmt_date(ds):
    """2026-03-24 → 24/03/2026"""
    try: p=ds.split("-"); return f"{p[2]}/{p[1]}/{p[0]}"
    except: return ds

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {}

def save_state(st):
    if "seen_comments" in st:
        cut = (datetime.now(timezone.utc)-timedelta(days=7)).strftime("%Y-%m-%d")
        st["seen_comments"] = {k:v for k,v in st["seen_comments"].items() if v.get("date","9")>=cut}
    with open(STATE_FILE,"w") as f: json.dump(st,f,indent=2)

def ensure_keys(st):
    st.pop("sent_today",None)
    for k,v in {"seen_comments":{},"sent_per_post":{},"pending_bets":[],"graded_bets":[],"stats":{}}.items():
        if k not in st: st[k]=v
    return st

def body_hash(text):
    c = POSTGAME_RE.sub("",text)
    c = "\n".join(l.strip() for l in c.splitlines())
    return hashlib.md5(c.encode()).hexdigest()

def norm_author(raw):
    n = raw.strip()
    for p in ("/u/","u/"):
        if n.startswith(p): n=n[len(p):]
    return n.lower()

# ── RSS ───────────────────────────────────────────────────────────────────────
def fetch_rss(url, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HDRS, timeout=20)
            if r.status_code==429: time.sleep(30); continue
            if r.status_code in (403,404): return None
            r.raise_for_status(); return r.content
        except Exception as e:
            print(f"  ⚠️ RSS err {i+1}: {e}",file=sys.stderr); time.sleep(3)
    return None

def parse_atom(content):
    if not content: return []
    try:
        root = ET.fromstring(content)
        out = []
        for e in root.findall(f"{{{ATOM}}}entry"):
            lk = e.find(f"{{{ATOM}}}link")
            au = e.find(f"{{{ATOM}}}author/{{{ATOM}}}name")
            co = e.find(f"{{{ATOM}}}content")
            out.append({"title":e.findtext(f"{{{ATOM}}}title",""),
                "link":lk.attrib.get("href","") if lk is not None else "",
                "updated":e.findtext(f"{{{ATOM}}}updated",""),
                "id":e.findtext(f"{{{ATOM}}}id",""),
                "author":au.text.strip() if au is not None else "",
                "content":co.text or "" if co is not None else ""})
        return out
    except ET.ParseError: return []

def post_id_from_url(url):
    p = url.rstrip("/").split("/")
    if "comments" in p:
        i = p.index("comments")
        if i+1<len(p): return p[i+1]
    return ""

def comment_id(eid):
    m = re.search(r't1_([a-z0-9]+)',eid)
    return m.group(1) if m else eid.rstrip("/").split("/")[-1]

def is_recent(upd):
    if not upd: return False
    try:
        dt = datetime.fromisoformat(upd.replace("Z","+00:00"))
        return dt >= datetime.now(timezone.utc)-timedelta(days=LOOKBACK_DAYS)
    except: return False

def strip_html(h):
    t = h
    for a,b in [("&lt;","<"),("&gt;",">"),("&amp;","&"),("&#39;","'"),("&quot;",'"'),
                 ("<br>","\n"),("<br/>","\n"),("<br />","\n")]:
        t = t.replace(a,b)
    t = re.sub(r"<(?:p|div|li|tr)[^>]*>","\n",t,flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>","",t)
    t = re.sub(r"\n{3,}","\n\n",t)
    return t.strip()

# ── Posts & Comments ──────────────────────────────────────────────────────────
def get_posts():
    entries = []
    c1 = fetch_rss(f"https://www.reddit.com/r/{SUBREDDIT}/new/.rss?limit=50")
    entries.extend(parse_atom(c1)); time.sleep(2)
    q = "NBA+Props+Daily+OR+NBA+Picks+OR+MLB+Props+OR+College+Basketball+OR+NCAABB"
    c2 = fetch_rss(f"https://www.reddit.com/r/{SUBREDDIT}/search.rss?q={q}&restrict_sr=1&sort=new&t=week&limit=30")
    entries.extend(parse_atom(c2))
    print(f"  📥 {len(entries)} RSS entries")
    seen, out = set(), []
    for e in entries:
        sp = detect_sport(e["title"])
        if not sp or not is_recent(e["updated"]): continue
        pid = post_id_from_url(e["link"])
        if not pid or pid in seen: continue
        seen.add(pid)
        out.append({"title":e["title"],"url":e["link"],"post_id":pid,"sport":sp})
        print(f"  📌 [{sp.upper()}] {e['title'][:60]}")
    return out

def get_comments(pid):
    c = fetch_rss(f"https://www.reddit.com/comments/{pid}/.rss")
    entries = parse_atom(c); time.sleep(2)
    out = []
    for e in entries:
        a = e.get("author","")
        if not a: continue
        b = strip_html(e.get("content","")).strip()
        if not b or len(b)<10: continue
        out.append({"author_raw":a,"author":norm_author(a),"body":b,"id":comment_id(e.get("id",""))})
    print(f"    💬 {len(out)} comments")
    return out

# ── Relevance ─────────────────────────────────────────────────────────────────
def is_picks(body):
    if len(body)<80: return False
    nums = bool(re.search(r'\d+\.?\d*',body))
    kws = len(re.findall(r'\b(over|under|o\d|u\d|parlay|prop|spread|moneyline|pts|reb|ast|3pm|pra|hits|HR|rbi|strikeout|total bases|pick|play|lean|fade|lock)\b',body,re.IGNORECASE))
    if nums and kws>=3: return True
    if not nums: return False
    return kws>=1

def is_real_edit(old_body, new_body):
    old_c = POSTGAME_RE.sub("",old_body).strip()
    new_c = POSTGAME_RE.sub("",new_body).strip()
    old_l = set(l.strip() for l in old_c.splitlines() if l.strip())
    new_l = set(l.strip() for l in new_c.splitlines() if l.strip())
    added = new_l - old_l
    if not added: return False
    txt = " ".join(added)
    if re.search(r'\d+\.?\d*',txt): return True
    if re.search(r'\b(injured|out|scratch|changed|lineup|over|under|parlay)\b',txt,re.IGNORECASE): return True
    return len(txt)>100

# ── Telegram ──────────────────────────────────────────────────────────────────
def tg_send(text):
    for chunk in _split(text):
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id":TELEGRAM_CHAT_ID,"text":chunk,"parse_mode":"HTML","disable_web_page_preview":True},timeout=15)
        if not r.ok: print(f"  ⚠️ TG: {r.text}",file=sys.stderr)
        time.sleep(0.5)

def _split(t, lim=4000):
    if len(t)<=lim: return [t]
    ch,cur=[],""
    for l in t.splitlines(keepends=True):
        if len(cur)+len(l)>lim:
            if cur: ch.append(cur.rstrip())
            cur=l
        else: cur+=l
    if cur.strip(): ch.append(cur.rstrip())
    return ch or [t[:lim]]

def esc(t): return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

# ── Claude Format ─────────────────────────────────────────────────────────────
def format_comment(user, body, title, sport, is_edit=False):
    if not ANTHROPIC_API_KEY: return esc(body)
    sl = SPORTS.get(sport,{}).get("label","Sports")
    se = "⚾" if sport=="mlb" else "🏀"
    ed = "Start with '✏️ <b>EDITED</b>' on its own line.\n\n" if is_edit else ""
    prompt = f"""{ed}Format this {sl} betting analyst's Reddit post for Telegram.

u/{user} posted in "{title}":
---
{body}
---

RULES:
- {se} before game matchup headers
- 🎯 before each individual pick/bet
- ⚠️ before injury/lineup news
- 📊 before stats/trends
- 💡 before analysis
- ⭐ for strongest play of the day
- 🎰 before parlays
- DO NOT use ✅ or ❌ (reserved for results)
- <b>bold</b> player and team names
- Convert American odds to decimal (+200→3.00, -130→1.77)
- REMOVE: thank yous, records, tip jars, social media, any ✅❌ emojis
- ONLY <b> and <i> HTML tags
- Return ONLY the formatted message"""
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":3000,
                  "messages":[{"role":"user","content":prompt}]},timeout=40)
        if r.ok: return r.json()["content"][0]["text"].strip()
    except Exception as e: print(f"  ⚠️ Format err: {e}",file=sys.stderr)
    return esc(body)

def build_msg(title, url, user, body, sport, is_edit=False):
    ed = "  ✏️ <i>(edited)</i>" if is_edit else ""
    em = SPORTS.get(sport,{}).get("emoji","🏀")
    dd = fmt_date(date_from_title(title))
    return (f"{em} <b>{esc(title)}</b>\n"
            f"───────────────\n"
            f"💬 <b>u/{user}</b>{ed}\n\n"
            f"{body}\n\n"
            f"🔗 <a href='{url}'>View on Reddit</a>")

# ── Bet Parsing (Sonnet for accuracy) ─────────────────────────────────────────
def parse_bets(user, body, title, date, sport):
    if not ANTHROPIC_API_KEY: return []
    sl = SPORTS.get(sport,{}).get("label","Sports")
    if sport=="mlb":
        sg = '"stat": one of "H","HR","RBI","R","SB","TB","K","ER","IP" or null'
    else:
        sg = '"stat": one of "PTS","REB","AST","3PM","BLK","STL","PRA","PR","PA","RA" or null. Use "3PM" for threes even if user writes "3s" or "3\'s"'

    prompt = f"""Extract {sl} bets from this Reddit comment. Be EXTREMELY precise.

Post: {title} | User: u/{user} | Date: {date}
---
{body}
---

Return JSON array. Each element:
{{"description":"Dejounte Murray Over 11.5 RA", "player":"Dejounte Murray", "team":"NOP", "opponent":"NYK", "bet_type":"player_prop", "stat":"RA", "line":11.5, "direction":"over", "confidence":"play"}}

For parlays:
{{"description":"Parlay 1: Dejounte Murray O11.5 RA + Devin Booker O5.5 AST", "player":null, "team":null, "opponent":null, "bet_type":"parlay", "stat":null, "line":null, "direction":null, "confidence":"play"}}

RULES — READ CAREFULLY:
1. {sg}
2. Every player_prop MUST have player + stat + line (number) + direction. NEVER create a bet without a line number.
3. PARLAYS: Users often write shorthand like "Parlay: Murray RA + Booker AST". These reference their individual picks listed above. You MUST expand each leg to full form with the line number from the individual pick. If "Dejounte Murray Over 11.5 RA" is listed above, then "Murray RA" in the parlay means "Dejounte Murray O11.5 RA". ALWAYS include the line number.
4. NEVER extract parlay leg references as separate individual bets. If a user lists individual picks and then says "Parlay: pick1 + pick2", only create one player_prop per pick and one parlay entry. Do NOT create extra player_props for the parlay legs.
5. If you cannot determine the line number for a parlay leg, SKIP the entire parlay.
6. Return ONLY JSON array, no markdown, no explanation."""

    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01"},
            json={"model":"claude-sonnet-4-20250514","max_tokens":3000,
                  "messages":[{"role":"user","content":prompt}]},timeout=45)
        if r.ok:
            raw = r.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
            bets = json.loads(raw)
            if not isinstance(bets,list): return []
            # ── STRICT VALIDATION ──
            valid = []
            for b in bets:
                bt = b.get("bet_type","other")
                desc = b.get("description","")
                if bt=="player_prop":
                    # Must have all 4: player, stat, line, direction
                    if not b.get("player") or b.get("line") is None or not b.get("direction") or not b.get("stat"):
                        print(f"    🗑️ Rejected incomplete: {desc[:50]}")
                        continue
                    # Description must contain a number
                    if not re.search(r'\d',desc):
                        print(f"    🗑️ Rejected no-number: {desc[:50]}")
                        continue
                elif bt=="parlay":
                    # Every leg in description must have a number
                    legs_part = re.sub(r'^[^:]+:\s*','',desc)
                    legs = re.split(r'\s+\+\s+',legs_part)
                    bad = False
                    for leg in legs:
                        if not re.search(r'\d',leg):
                            print(f"    🗑️ Rejected parlay leg without line: {leg[:40]}")
                            bad = True; break
                    if bad: continue
                valid.append(b)
            d = len(bets)-len(valid)
            if d: print(f"    🗑️ Dropped {d} malformed bet(s)")
            return valid
    except Exception as e: print(f"  ⚠️ Parse err: {e}",file=sys.stderr)
    return []

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🤖 Bot v{V} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    st = ensure_keys(load_state())
    sends = 0
    posts = get_posts()
    print(f"  🎯 {len(posts)} posts found")
    if not posts: save_state(st); return

    for post in posts:
        t,u,pid,sp = post["title"],post["url"],post["post_id"],post["sport"]
        pd = date_from_title(t)
        print(f"\n  📅 [{sp.upper()}] {pd} | {t[:55]}")
        comms = get_comments(pid)
        best = {}
        for c in comms:
            ak = c["author"]
            if ak not in USERS or sp not in USERS[ak]: continue
            if ak not in best or len(c["body"])>len(best[ak]["body"]): best[ak]=c

        for ak,c in best.items():
            disp = c["author_raw"].strip().lstrip("/u/").lstrip("u/")
            print(f"  🎯 u/{disp} ({len(c['body'])} chars)")
            if not is_picks(c["body"]): print(f"    ⏭️ Not picks"); continue

            cid,body = c["id"],c["body"]
            h = body_hash(body)
            seen = st["seen_comments"].get(cid,{})
            is_new = not seen
            is_ed = not is_new and seen.get("hash")!=h
            sk = f"{ak}:{pid}"

            if not is_new and not is_ed: print(f"    ⏭️ Unchanged"); continue
            if is_ed:
                ob = seen.get("body_preview","")
                if ob and not is_real_edit(ob,body):
                    st["seen_comments"][cid]["hash"]=h; continue
                print(f"    ✏️ Real edit detected")
            elif st["sent_per_post"].get(sk): print(f"    ⏭️ Already sent"); continue

            fmt = format_comment(disp,body,t,sp,is_edit=is_ed)
            msg = build_msg(t,u,disp,fmt,sp,is_edit=is_ed)
            tg_send(msg)
            st["seen_comments"][cid]={"hash":h,"date":pd,"body_preview":body[:500]}
            st["sent_per_post"][sk]=True; sends+=1

            if is_new:
                bets = parse_bets(disp,body,t,pd,sp)
                st["pending_bets"]=[b for b in st["pending_bets"] if not b.get("id","").startswith(f"{cid}_")]
                stored=0
                for i,b in enumerate(bets):
                    if not b.get("description"): continue
                    st["pending_bets"].append({"id":f"{cid}_{i}","user":disp,"date":pd,"sport":sp,
                        "post_title":t,"post_url":u,"description":b.get("description",""),
                        "player":b.get("player"),"team":b.get("team"),"opponent":b.get("opponent"),
                        "bet_type":b.get("bet_type","other"),"stat":b.get("stat"),
                        "line":b.get("line"),"direction":b.get("direction"),
                        "confidence":b.get("confidence"),"result":None})
                    stored+=1
                print(f"    💾 {stored} bets stored")

    save_state(st)
    print(f"\n✅ Bot v{V} done — {sends} sent")

if __name__=="__main__": main()
