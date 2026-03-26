"""
NBA/MLB/NCAABB Grader  —  v4.0  (2026-03-26)
Grades pending bets from state.json against ESPN boxscores.
"""

import requests
import json
import os
import sys
import time
import re
from datetime import datetime, timezone, timedelta

VERSION = "4.0"

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

STATE_FILE = "state.json"
HEADERS    = {"User-Agent": "Mozilla/5.0 nba-picks-bot/1.0"}

RESULT_ICON = {"win": "✅", "loss": "❌", "push": "➡️", "unknown": "❓", "dnp": "🚫"}
SPORT_EMOJI = {"nba": "🏀", "ncaabb": "🏀", "mlb": "⚾"}

ESPN_CONFIG = {
    "nba":    {"scoreboard": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={}",
               "summary":    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={}"},
    "ncaabb": {"scoreboard": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?dates={}&limit=100",
               "summary":    "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/summary?event={}"},
    "mlb":    {"scoreboard": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={}",
               "summary":    "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event={}"},
}

BASKETBALL_STAT_MAP = {
    "PTS": "PTS", "P": "PTS", "POINTS": "PTS",
    "REB": "REB", "R": "REB", "REBOUNDS": "REB",
    "AST": "AST", "A": "AST", "ASSISTS": "AST",
    "3PM": "3PM", "3S": "3PM", "3": "3PM", "THREES": "3PM", "3'S": "3PM", "TPM": "3PM",
    "BLK": "BLK", "STL": "STL",
    "PRA": "PRA", "PA": "PA", "PR": "PR", "RA": "RA",
}
MLB_STAT_MAP = {
    "H": "H", "HITS": "H", "HR": "HR", "RBI": "RBI", "R": "R", "RUNS": "R",
    "SB": "SB", "TB": "TB", "TOTAL BASES": "TB",
    "K": "K", "SO": "K", "STRIKEOUTS": "K",
    "BB": "BB", "ER": "ER", "IP": "IP", "HITS_ALLOWED": "HITS_ALLOWED",
}

def get_stat_map(sport):
    return MLB_STAT_MAP if sport == "mlb" else BASKETBALL_STAT_MAP

NICKNAME_MAP = {
    "kat": "karl-anthony towns", "sga": "shai gilgeous-alexander",
    "ad": "anthony davis", "pg": "paul george",
    "rj": "rj barrett", "cj": "cj mccollum", "shohei": "shohei ohtani",
}


# ── State / Telegram ─────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"pending_bets": [], "graded_bets": [], "stats": {}}

def save_state(state):
    if len(state.get("graded_bets", [])) > 500:
        state["graded_bets"] = state["graded_bets"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def escape_html(t):
    return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def split_message(text, limit=4000):
    if len(text) <= limit: return [text]
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit:
            if cur: chunks.append(cur.rstrip())
            cur = line
        else: cur += line
    if cur.strip(): chunks.append(cur.rstrip())
    return chunks or [text[:limit]]

def send_telegram(text):
    for chunk in split_message(text):
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk,
                  "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
        if not resp.ok: print(f"TG error: {resp.text}", file=sys.stderr)
        time.sleep(0.3)


# ── ESPN ──────────────────────────────────────────────────────────────────────
def espn_get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15); r.raise_for_status(); return r.json()
    except Exception as e:
        print(f"  ESPN err: {e}", file=sys.stderr); return {}

def get_scoreboard(sport, yyyymmdd):
    cfg = ESPN_CONFIG.get(sport)
    return espn_get(cfg["scoreboard"].format(yyyymmdd)).get("events", []) if cfg else []

def get_boxscore(sport, gid):
    cfg = ESPN_CONFIG.get(sport)
    return espn_get(cfg["summary"].format(gid)) if cfg else {}

def parse_made_att(val):
    if isinstance(val, str) and "-" in val:
        p = val.split("-")
        try: return int(p[0]), int(p[1])
        except: return 0, 0
    try: return int(float(val or 0)), None
    except: return 0, None

def parse_mins(val):
    if isinstance(val, str) and ":" in val:
        try: return float(val.split(":")[0])
        except: return 0.0
    try: return float(val or 0)
    except: return 0.0

def sf(raw_map, key):
    v = raw_map.get(key, 0)
    try: return float(v or 0)
    except: return 0.0

def build_basketball_stats(games, sport):
    players = {}
    for game in games:
        gid = game.get("id")
        if not gid: continue
        box = get_boxscore(sport, gid); time.sleep(0.5)
        for te in box.get("boxscore",{}).get("players",[]):
            team_abbr = te.get("team",{}).get("abbreviation","")
            for sg in te.get("statistics",[]):
                names = sg.get("names",[])
                for ae in sg.get("athletes",[]):
                    fn = ae.get("athlete",{}).get("displayName","")
                    raw = ae.get("stats",[])
                    if not fn: continue
                    rm = {names[i]: raw[i] for i in range(min(len(names),len(raw)))}
                    mins = parse_mins(rm.get("MIN","0"))
                    tpm,_ = parse_made_att(rm.get("3PT","0"))
                    fg,_  = parse_made_att(rm.get("FG","0"))
                    ft,_  = parse_made_att(rm.get("FT","0"))
                    pts,reb,ast = sf(rm,"PTS"),sf(rm,"REB"),sf(rm,"AST")
                    blk,stl = sf(rm,"BLK"),sf(rm,"STL")
                    players[fn.lower()] = {
                        "name":fn,"team":team_abbr,"played":mins>0,
                        "stats":{"PTS":pts,"REB":reb,"AST":ast,"3PM":float(tpm),
                                 "BLK":blk,"STL":stl,"FGM":float(fg),"FTM":float(ft),
                                 "PRA":pts+reb+ast,"PR":pts+reb,"PA":pts+ast,"RA":reb+ast}}
                    print(f"    {sport.upper()}: {fn} | PTS:{pts} REB:{reb} AST:{ast} 3PM:{tpm}")
    return players

def build_mlb_stats(games):
    players = {}
    for game in games:
        gid = game.get("id")
        if not gid: continue
        box = get_boxscore("mlb", gid); time.sleep(0.5)
        for te in box.get("boxscore",{}).get("players",[]):
            ta = te.get("team",{}).get("abbreviation","")
            for sg in te.get("statistics",[]):
                gt = sg.get("type",""); names = sg.get("names",[])
                for ae in sg.get("athletes",[]):
                    fn = ae.get("athlete",{}).get("displayName","")
                    raw = ae.get("stats",[])
                    if not fn: continue
                    rm = {names[i]: raw[i] for i in range(min(len(names),len(raw)))}
                    key = fn.lower()
                    if key not in players:
                        players[key] = {"name":fn,"team":ta,"stats":{},"played":True,"is_pitcher":False}
                    s = players[key]["stats"]
                    if "batting" in gt.lower() or "AB" in names:
                        s["H"]=sf(rm,"H"); s["HR"]=sf(rm,"HR"); s["RBI"]=sf(rm,"RBI")
                        s["R"]=sf(rm,"R"); s["BB"]=sf(rm,"BB"); s["SB"]=sf(rm,"SB")
                        s["AB"]=sf(rm,"AB")
                        singles = s["H"]-sf(rm,"2B")-sf(rm,"3B")-s["HR"]
                        s["TB"]=max(0,singles+2*sf(rm,"2B")+3*sf(rm,"3B")+4*s["HR"])
                        players[key]["played"] = s["AB"]>0 or s["R"]>0
                    elif "pitching" in gt.lower() or "IP" in names:
                        players[key]["is_pitcher"]=True
                        try: s["IP"]=float(rm.get("IP",0) or 0)
                        except: s["IP"]=0.0
                        s["K"]=sf(rm,"K"); s["ER"]=sf(rm,"ER")
                        s["HITS_ALLOWED"]=sf(rm,"H"); s["BB_PITCH"]=sf(rm,"BB")
                        players[key]["played"]=s["IP"]>0
    return players

def build_player_stats(games, sport):
    return build_mlb_stats(games) if sport=="mlb" else build_basketball_stats(games, sport)

def build_game_results(games):
    results = []
    for game in games:
        comps = game.get("competitions",[{}])[0]
        teams = comps.get("competitors",[])
        if len(teams)<2: continue
        home = next((t for t in teams if t.get("homeAway")=="home"), teams[0])
        away = next((t for t in teams if t.get("homeAway")=="away"), teams[1])
        hs,as_ = int(home.get("score",0) or 0), int(away.get("score",0) or 0)
        ht = home.get("team",{}).get("abbreviation","")
        at = away.get("team",{}).get("abbreviation","")
        results.append({"home":ht,"away":at,"home_score":hs,"away_score":as_,
                        "total":hs+as_,"winner":ht if hs>as_ else at})
    return results

def find_player(name, ps):
    if not name or not ps: return None
    nl = name.lower().strip()
    if nl in ps: return ps[nl]
    parts = [p for p in nl.split() if len(p)>2]
    for pn,pd in ps.items():
        if parts and all(p in pn for p in parts): return pd
    last = nl.split()[-1] if nl.split() else ""
    if len(last)>4:
        ms = [pd for pn,pd in ps.items() if last in pn.split()]
        if len(ms)==1: return ms[0]
    mapped = NICKNAME_MAP.get(nl)
    if mapped and mapped in ps: return ps[mapped]
    return None


# ── Grading ───────────────────────────────────────────────────────────────────
def grade_single_bet(bet, ps, gr, sport="nba"):
    bt = bet.get("bet_type","other")
    d  = (bet.get("direction") or "").lower()
    ln = bet.get("line")
    pl = bet.get("player") or ""
    sc = (bet.get("stat") or "").upper()
    tm = (bet.get("team") or "").upper()
    op = (bet.get("opponent") or "").upper()
    sc = get_stat_map(sport).get(sc, sc)

    if bt=="player_prop" and pl and sc and ln is not None:
        pd = find_player(pl, ps)
        if pd is not None:
            if not pd.get("played",True): return "dnp"
            actual = pd["stats"].get(sc)
            if actual is not None:
                try:
                    a,l = float(actual),float(ln)
                    print(f"    Grade {pl}: {sc} actual={a} line={l} dir={d}")
                    if d=="over":  return "win" if a>l else ("push" if a==l else "loss")
                    if d=="under": return "win" if a<l else ("push" if a==l else "loss")
                except: pass
        elif ps:
            print(f"    ⚠️  {pl} not found → DNP"); return "dnp"
    elif bt=="total" and ln is not None:
        for g in gr:
            if tm in (g["home"],g["away"]) or op in (g["home"],g["away"]):
                try:
                    t,l=float(g["total"]),float(ln)
                    if d=="over": return "win" if t>l else ("push" if t==l else "loss")
                    if d=="under": return "win" if t<l else ("push" if t==l else "loss")
                except: pass
    elif bt=="moneyline" and tm:
        for g in gr:
            if tm in (g["home"],g["away"]): return "win" if g["winner"]==tm else "loss"
    elif bt=="spread" and tm and ln is not None:
        for g in gr:
            if tm in (g["home"],g["away"]):
                try:
                    l=float(ln)
                    ts=g["home_score"] if g["home"]==tm else g["away_score"]
                    os_=g["away_score"] if g["home"]==tm else g["home_score"]
                    diff=ts+l-os_
                    return "win" if diff>0 else ("push" if diff==0 else "loss")
                except: pass
    return grade_with_claude(bet, ps, gr)

def grade_with_claude(bet, ps, gr):
    if not ANTHROPIC_API_KEY: return "unknown"
    rel = {}
    pd = find_player(bet.get("player") or "", ps)
    if pd: rel[pd["name"]] = pd["stats"]
    prompt = (f"Grade this bet:\nBET: {bet.get('description','N/A')}\n"
              f"Player: {bet.get('player')} | Stat: {bet.get('stat')} | Line: {bet.get('line')} | Dir: {bet.get('direction')}\n"
              f"STATS: {json.dumps(rel,indent=2)}\nGAMES: {json.dumps(gr[:5],indent=2)}\n"
              f"Reply ONLY: win, loss, push, dnp, or unknown.")
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":10,
                  "messages":[{"role":"user","content":prompt}]},timeout=20)
        if r.ok:
            res=r.json()["content"][0]["text"].strip().lower()
            if res in ("win","loss","push","dnp","unknown"): return res
    except: pass
    return "unknown"


# ── Parlay ────────────────────────────────────────────────────────────────────
def parse_parlay_legs(desc, sport="nba"):
    d = re.sub(r'^[^:]+:\s*','',desc,count=1)
    d = re.sub(r'\s*\([+-]\d+\)\s*$','',d).strip()
    return [parse_one_leg(l.strip(),sport) for l in re.split(r'\s+\+\s+',d) if l.strip()]

def parse_one_leg(leg, sport="nba"):
    sm = get_stat_map(sport)
    cl = re.sub(r"(\d)\s*3'?s\b", r"\1 3PM", leg, flags=re.IGNORECASE)
    m = re.match(r'(.+?)\s+(O|U|Over|Under)\s*([\d.]+)\+?\s*([A-Za-z_0-9]+)?', cl, re.IGNORECASE)
    if m:
        return {"description":leg,"player":m.group(1).strip(),"bet_type":"player_prop",
                "stat":sm.get((m.group(4) or "PTS").upper(),m.group(4) or "PTS"),
                "line":float(m.group(3)),
                "direction":"over" if m.group(2).lower() in ("o","over") else "under",
                "team":None,"opponent":None}
    m2 = re.match(r'(.+?)\s+([\d.]+)\+\s*([A-Za-z_0-9]+)', cl, re.IGNORECASE)
    if m2:
        return {"description":leg,"player":m2.group(1).strip(),"bet_type":"player_prop",
                "stat":sm.get(m2.group(3).upper(),m2.group(3).upper()),
                "line":float(m2.group(2)),"direction":"over","team":None,"opponent":None}
    m3 = re.match(r'(.+?)\s+([\d.]+)\s+([A-Za-z_0-9]+)', cl, re.IGNORECASE)
    if m3:
        return {"description":leg,"player":m3.group(1).strip(),"bet_type":"player_prop",
                "stat":sm.get(m3.group(3).upper(),m3.group(3).upper()),
                "line":float(m3.group(2)),"direction":"over","team":None,"opponent":None}
    return {"description":leg,"player":None,"bet_type":"other",
            "stat":None,"line":None,"direction":None,"team":None,"opponent":None}

def grade_parlay(bet, ps, gr, sport="nba"):
    legs = parse_parlay_legs(bet.get("description",""), sport)
    if not legs: return "unknown", []
    leg_results = []
    for leg in legs:
        r = grade_single_bet(leg, ps, gr, sport)
        leg_results.append({"description":leg["description"],"result":r})
        print(f"      Leg: {leg['description'][:50]} → {r}")
    # DNP legs VOIDED — only active legs count
    active = [r for r in leg_results if r["result"] != "dnp"]
    if not active: return "dnp", leg_results
    rs = {r["result"] for r in active}
    if "loss" in rs: return "loss", leg_results
    if "unknown" in rs: return "unknown", leg_results
    if all(r["result"] in ("win","push") for r in active):
        return ("win" if any(r["result"]=="win" for r in active) else "push"), leg_results
    return "unknown", leg_results


# ── Formatting ────────────────────────────────────────────────────────────────
def fmt_date(ds):
    """2026-03-24 → 24/03/2026"""
    try: p=ds.split("-"); return f"{p[2]}/{p[1]}/{p[0]}"
    except: return ds

def format_daily_results(graded, date_str, sport=None):
    if not graded: return None
    by_user = {}
    for b in graded: by_user.setdefault(b["user"],[]).append(b)
    se = SPORT_EMOJI.get(sport,"📊")
    sl = (sport or "").upper()
    dd = fmt_date(date_str)
    lines = [f"{se} <b>{sl} Bet Results — {dd}</b>" if sl else f"📊 <b>Bet Results — {dd}</b>",
             "───────────────"]
    for user, bets in by_user.items():
        st = [b for b in bets if b.get("bet_type")!="parlay"]
        pa = [b for b in bets if b.get("bet_type")=="parlay"]
        sw=sum(1 for b in st if b["result"]=="win")
        sl_=sum(1 for b in st if b["result"]=="loss")
        sp=sum(1 for b in st if b["result"]=="push")
        sd=sum(1 for b in st if b["result"] in ("dnp","unknown"))
        pw=sum(1 for b in pa if b["result"]=="win")
        pl=sum(1 for b in pa if b["result"]=="loss")
        stt=sw+sl_; ptt=pw+pl
        sr=f"{round(sw/stt*100,1)}%" if stt>0 else "—"
        pr=f"{round(pw/ptt*100,1)}%" if ptt>0 else "—"
        lines.append(f"\n💬 <b>u/{user}</b>")
        push_t=f"  <i>({sp} push)</i>" if sp else ""
        void_t=f"  <i>({sd} void/DNP)</i>" if sd else ""
        lines.append(f"  📈 Straight: <b>{sw}W / {sl_}L</b>  {sr}{push_t}{void_t}")
        lines.append(f"  🎰 Parlays:  <b>{pw}W / {pl}L</b>  {pr}")
        if st:
            lines.append("\n<b>Individual Plays:</b>")
            for b in st:
                ic=RESULT_ICON.get(b["result"],"❓")
                ds=escape_html(b.get("description",""))
                vd="  <i>(void — DNP)</i>" if b["result"]=="dnp" else ""
                lines.append(f"  {ic} {ds}{vd}")
        if pa:
            lines.append("\n<b>Parlays:</b>")
            for b in pa:
                oi=RESULT_ICON.get(b["result"],"❓")
                lb=escape_html(b.get("description","Parlay").split(":")[0].strip())
                lines.append(f"\n  {oi} <b>{lb}</b>")
                for lg in b.get("leg_results",[]):
                    li=RESULT_ICON.get(lg["result"],"❓")
                    ld=escape_html(lg["description"])
                    dt="  <i>(void — DNP)</i>" if lg["result"]=="dnp" else ""
                    lines.append(f"      {li} {ld}{dt}")
    return "\n".join(lines)

def format_overall_stats(stats, start_date):
    if not stats: return None
    sd = fmt_date(start_date)
    lines = [f"📈 <b>Overall Record</b>  <i>(since {sd})</i>", "───────────────"]
    for user,rec in stats.items():
        sw=rec.get("straight_wins",0); sl=rec.get("straight_losses",0)
        sp=rec.get("straight_pushes",0); pw=rec.get("parlay_wins",0); pl=rec.get("parlay_losses",0)
        stt=sw+sl; ptt=pw+pl; tt=stt+ptt
        sr=f"{round(sw/stt*100,1)}%" if stt>0 else "—"
        prr=f"{round(pw/ptt*100,1)}%" if ptt>0 else "—"
        tr=f"{round((sw+pw)/tt*100,1)}%" if tt>0 else "—"
        lines.append(f"\n💬 <b>u/{user}</b>")
        lines.append(f"  📊 <b>Total Hit Rate:</b>  <b>{tr}</b>  ({sw+pw}W / {sl+pl}L of {tt} bets)")
        lines.append(f"  🎯 <b>Individual:</b>  {sw}W / {sl}L" + (f" / {sp}P" if sp else "") + f"  →  {sr}")
        lines.append(f"  🎰 <b>Parlays:</b>  {pw}W / {pl}L  →  {prr}")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🎯  Grader v{VERSION} starting — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    state = load_state()
    for k in ["pending_bets","graded_bets"]:
        if k not in state: state[k]=[]
    if "stats" not in state: state["stats"]={}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if "tracking_start" not in state: state["tracking_start"]=today

    pending = [b for b in state["pending_bets"] if b.get("date","9999")<today]
    if not pending:
        print("No pending bets — done."); save_state(state); return

    dsp = sorted(set((b["date"],b.get("sport","nba")) for b in pending))
    print(f"  📋  {len(pending)} bets across {len(dsp)} date/sport combos")

    all_ps, all_gr = {}, {}
    for ds,sp in dsp:
        ed = ds.replace("-","")
        print(f"\n  {SPORT_EMOJI.get(sp,'📊')}  {sp.upper()} {ds}...")
        games = get_scoreboard(sp, ed)
        print(f"      {len(games)} games")
        if not games: continue
        all_ps[(ds,sp)] = build_player_stats(games, sp)
        all_gr[(ds,sp)] = build_game_results(games)
        print(f"      {len(all_ps[(ds,sp)])} players"); time.sleep(1)

    all_graded = []; dates_graded = set()
    for bet in pending:
        ds,sp = bet["date"],bet.get("sport","nba")
        ps = all_ps.get((ds,sp),{}); gr = all_gr.get((ds,sp),[])
        if not ps and not gr: continue
        user = bet["user"]
        if user not in state["stats"]:
            state["stats"][user]={"straight_wins":0,"straight_losses":0,"straight_pushes":0,"parlay_wins":0,"parlay_losses":0}
        for k in ("straight_wins","straight_losses","straight_pushes","parlay_wins","parlay_losses"):
            state["stats"][user].setdefault(k,0)

        if bet.get("bet_type")=="parlay":
            print(f"\n  🎰  Parlay: {bet.get('description','')[:60]}")
            res,lr = grade_parlay(bet,ps,gr,sp); bet["result"]=res; bet["leg_results"]=lr
        else:
            res = grade_single_bet(bet,ps,gr,sp); bet["result"]=res
        bet["graded_date"]=today

        if bet.get("bet_type")=="parlay":
            if res=="win": state["stats"][user]["parlay_wins"]+=1
            elif res=="loss": state["stats"][user]["parlay_losses"]+=1
        else:
            if res=="win": state["stats"][user]["straight_wins"]+=1
            elif res=="loss": state["stats"][user]["straight_losses"]+=1
            elif res=="push": state["stats"][user]["straight_pushes"]+=1

        print(f"  {RESULT_ICON.get(res,'❓')}  {bet.get('description','')[:60]} → {res}")
        all_graded.append(bet); dates_graded.add((ds,sp)); time.sleep(0.2)

    gids = {b["id"] for b in all_graded}
    state["pending_bets"]=[b for b in state["pending_bets"] if b["id"] not in gids]
    state["graded_bets"].extend(all_graded); save_state(state)

    for ds,sp in sorted(dates_graded):
        dg=[b for b in all_graded if b["date"]==ds and b.get("sport","nba")==sp]
        msg = format_daily_results(dg, ds, sp)
        if msg: send_telegram(msg); print(f"  📨  Results for {sp.upper()} {ds}"); time.sleep(1)

    msg = format_overall_stats(state["stats"], state.get("tracking_start",today))
    if msg: send_telegram(msg); print("  📨  Overall stats sent")
    print(f"\n✅  Grader v{VERSION} done — {len(all_graded)} bets graded")

if __name__ == "__main__":
    main()
