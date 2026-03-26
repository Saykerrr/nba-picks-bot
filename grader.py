"""grader.py v5 — Bet Grader (2026-03-26)"""
import requests, json, os, sys, time, re
from datetime import datetime, timezone, timedelta

V = "5.0"
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
STATE_FILE = "state.json"
HDRS = {"User-Agent":"Mozilla/5.0 nba-picks-bot/1.0"}

ICON = {"win":"✅","loss":"❌","push":"➡️","unknown":"❓","dnp":"🚫"}
SPORT_EMOJI = {"nba":"🏀","ncaabb":"🏀","mlb":"⚾"}

ESPN = {
    "nba":    {"sb":"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={}",
               "box":"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={}"},
    "ncaabb": {"sb":"https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?dates={}&limit=100",
               "box":"https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/summary?event={}"},
    "mlb":    {"sb":"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={}",
               "box":"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event={}"},
}

BB_MAP = {"PTS":"PTS","P":"PTS","POINTS":"PTS","REB":"REB","R":"REB","AST":"AST","A":"AST",
          "3PM":"3PM","3S":"3PM","3":"3PM","THREES":"3PM","3'S":"3PM","TPM":"3PM",
          "BLK":"BLK","STL":"STL","PRA":"PRA","PA":"PA","PR":"PR","RA":"RA"}
MLB_MAP = {"H":"H","HITS":"H","HR":"HR","RBI":"RBI","R":"R","RUNS":"R","SB":"SB",
           "TB":"TB","K":"K","SO":"K","STRIKEOUTS":"K","BB":"BB","ER":"ER","IP":"IP",
           "HITS_ALLOWED":"HITS_ALLOWED"}

NICKS = {"kat":"karl-anthony towns","sga":"shai gilgeous-alexander","ad":"anthony davis",
         "pg":"paul george","rj":"rj barrett","cj":"cj mccollum","shohei":"shohei ohtani"}

def smap(sp): return MLB_MAP if sp=="mlb" else BB_MAP

# ── Util ──────────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {"pending_bets":[],"graded_bets":[],"stats":{}}

def save_state(st):
    if len(st.get("graded_bets",[]))>500: st["graded_bets"]=st["graded_bets"][-500:]
    with open(STATE_FILE,"w") as f: json.dump(st,f,indent=2)

def esc(t): return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def tg_send(text):
    for ch in _split(text):
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id":TELEGRAM_CHAT_ID,"text":ch,"parse_mode":"HTML","disable_web_page_preview":True},timeout=15)
        if not r.ok: print(f"TG err: {r.text}",file=sys.stderr)
        time.sleep(0.3)

def _split(t,lim=4000):
    if len(t)<=lim: return [t]
    ch,cur=[],""
    for l in t.splitlines(keepends=True):
        if len(cur)+len(l)>lim:
            if cur: ch.append(cur.rstrip()); cur=l
        else: cur+=l
    if cur.strip(): ch.append(cur.rstrip())
    return ch or [t[:lim]]

def fmt_date(ds):
    try: p=ds.split("-"); return f"{p[2]}/{p[1]}/{p[0]}"
    except: return ds

# ── ESPN ──────────────────────────────────────────────────────────────────────
def espn(url):
    try: r=requests.get(url,headers=HDRS,timeout=15); r.raise_for_status(); return r.json()
    except Exception as e: print(f"  ESPN: {e}",file=sys.stderr); return {}

def scoreboard(sp,yyyymmdd):
    c=ESPN.get(sp); return espn(c["sb"].format(yyyymmdd)).get("events",[]) if c else []

def boxscore(sp,gid):
    c=ESPN.get(sp); return espn(c["box"].format(gid)) if c else {}

def made_att(v):
    if isinstance(v,str) and "-" in v:
        try: p=v.split("-"); return int(p[0]),int(p[1])
        except: return 0,0
    try: return int(float(v or 0)),None
    except: return 0,None

def mins(v):
    if isinstance(v,str) and ":" in v:
        try: return float(v.split(":")[0])
        except: return 0.0
    try: return float(v or 0)
    except: return 0.0

def sf(rm,k):
    v=rm.get(k,0)
    try: return float(v or 0)
    except: return 0.0

def build_bb_stats(games, sp):
    pl = {}
    for g in games:
        gid=g.get("id"); 
        if not gid: continue
        box=boxscore(sp,gid); time.sleep(0.5)
        for te in box.get("boxscore",{}).get("players",[]):
            ta=te.get("team",{}).get("abbreviation","")
            for sg in te.get("statistics",[]):
                ns=sg.get("names",[])
                for ae in sg.get("athletes",[]):
                    fn=ae.get("athlete",{}).get("displayName",""); raw=ae.get("stats",[])
                    if not fn: continue
                    rm={ns[i]:raw[i] for i in range(min(len(ns),len(raw)))}
                    m=mins(rm.get("MIN","0")); tpm,_=made_att(rm.get("3PT","0"))
                    pts,reb,ast=sf(rm,"PTS"),sf(rm,"REB"),sf(rm,"AST")
                    blk,stl=sf(rm,"BLK"),sf(rm,"STL")
                    pl[fn.lower()]={"name":fn,"team":ta,"played":m>0,
                        "stats":{"PTS":pts,"REB":reb,"AST":ast,"3PM":float(tpm),
                                 "BLK":blk,"STL":stl,"PRA":pts+reb+ast,
                                 "PR":pts+reb,"PA":pts+ast,"RA":reb+ast}}
                    print(f"    {sp.upper()}: {fn} PTS:{pts} REB:{reb} AST:{ast} 3PM:{tpm}")
    return pl

def build_mlb_stats(games):
    pl = {}
    for g in games:
        gid=g.get("id");
        if not gid: continue
        box=boxscore("mlb",gid); time.sleep(0.5)
        for te in box.get("boxscore",{}).get("players",[]):
            ta=te.get("team",{}).get("abbreviation","")
            for sg in te.get("statistics",[]):
                gt=sg.get("type",""); ns=sg.get("names",[])
                for ae in sg.get("athletes",[]):
                    fn=ae.get("athlete",{}).get("displayName",""); raw=ae.get("stats",[])
                    if not fn: continue
                    rm={ns[i]:raw[i] for i in range(min(len(ns),len(raw)))}
                    k=fn.lower()
                    if k not in pl: pl[k]={"name":fn,"team":ta,"stats":{},"played":True}
                    s=pl[k]["stats"]
                    if "batting" in gt.lower() or "AB" in ns:
                        s["H"]=sf(rm,"H"); s["HR"]=sf(rm,"HR"); s["RBI"]=sf(rm,"RBI")
                        s["R"]=sf(rm,"R"); s["SB"]=sf(rm,"SB"); s["AB"]=sf(rm,"AB")
                        sin=s["H"]-sf(rm,"2B")-sf(rm,"3B")-s["HR"]
                        s["TB"]=max(0,sin+2*sf(rm,"2B")+3*sf(rm,"3B")+4*s["HR"])
                        pl[k]["played"]=s["AB"]>0
                    elif "pitching" in gt.lower() or "IP" in ns:
                        try: s["IP"]=float(rm.get("IP",0) or 0)
                        except: s["IP"]=0.0
                        s["K"]=sf(rm,"K"); s["ER"]=sf(rm,"ER"); s["HITS_ALLOWED"]=sf(rm,"H")
                        pl[k]["played"]=s["IP"]>0
    return pl

def player_stats(games,sp): return build_mlb_stats(games) if sp=="mlb" else build_bb_stats(games,sp)

def game_results(games):
    out=[]
    for g in games:
        cs=g.get("competitions",[{}])[0]; ts=cs.get("competitors",[])
        if len(ts)<2: continue
        h=next((t for t in ts if t.get("homeAway")=="home"),ts[0])
        a=next((t for t in ts if t.get("homeAway")=="away"),ts[1])
        hs,as_=int(h.get("score",0) or 0),int(a.get("score",0) or 0)
        ht=h.get("team",{}).get("abbreviation",""); at=a.get("team",{}).get("abbreviation","")
        out.append({"home":ht,"away":at,"home_score":hs,"away_score":as_,"total":hs+as_,"winner":ht if hs>as_ else at})
    return out

def find_player(name,ps):
    if not name or not ps: return None
    nl=name.lower().strip()
    if nl in ps: return ps[nl]
    parts=[p for p in nl.split() if len(p)>2]
    for pn,pd in ps.items():
        if parts and all(p in pn for p in parts): return pd
    last=nl.split()[-1] if nl.split() else ""
    if len(last)>4:
        ms=[pd for pn,pd in ps.items() if last in pn.split()]
        if len(ms)==1: return ms[0]
    mapped=NICKS.get(nl)
    if mapped and mapped in ps: return ps[mapped]
    return None

# ── Grade ─────────────────────────────────────────────────────────────────────
def grade_bet(bet,ps,gr,sp="nba"):
    bt=bet.get("bet_type","other"); d=(bet.get("direction") or "").lower()
    ln=bet.get("line"); pl=bet.get("player") or ""; sc=(bet.get("stat") or "").upper()
    tm=(bet.get("team") or "").upper(); op=(bet.get("opponent") or "").upper()
    sc=smap(sp).get(sc,sc)
    if bt=="player_prop" and pl and sc and ln is not None:
        pd=find_player(pl,ps)
        if pd:
            if not pd.get("played",True): return "dnp"
            a=pd["stats"].get(sc)
            if a is not None:
                try:
                    a,l=float(a),float(ln)
                    print(f"    {pl}: {sc}={a} vs {l} ({d})")
                    if d=="over": return "win" if a>l else ("push" if a==l else "loss")
                    if d=="under": return "win" if a<l else ("push" if a==l else "loss")
                except: pass
        elif ps: return "dnp"
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
                    df=ts+l-os_; return "win" if df>0 else ("push" if df==0 else "loss")
                except: pass
    # Claude fallback
    if not ANTHROPIC_API_KEY: return "unknown"
    rel={}; pd=find_player(pl,ps)
    if pd: rel[pd["name"]]=pd["stats"]
    try:
        r=requests.post("https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":10,
                  "messages":[{"role":"user","content":f"Grade bet: {bet.get('description','')}\nPlayer: {pl} Stat: {sc} Line: {ln} Dir: {d}\nStats: {json.dumps(rel)}\nGames: {json.dumps(gr[:3])}\nReply ONLY: win/loss/push/dnp/unknown"}]},timeout=20)
        if r.ok:
            res=r.json()["content"][0]["text"].strip().lower()
            if res in ("win","loss","push","dnp","unknown"): return res
    except: pass
    return "unknown"

# ── Parlay ────────────────────────────────────────────────────────────────────
def parse_legs(desc,sp="nba"):
    d=re.sub(r'^[^:]+:\s*','',desc,count=1)
    d=re.sub(r'\s*\([+-]\d+\)\s*$','',d).strip()
    return [parse_leg(l.strip(),sp) for l in re.split(r'\s+\+\s+',d) if l.strip()]

def parse_leg(leg,sp="nba"):
    sm=smap(sp); cl=re.sub(r"(\d)\s*3'?s\b",r"\1 3PM",leg,flags=re.IGNORECASE)
    m=re.match(r'(.+?)\s+(O|U|Over|Under)\s*([\d.]+)\+?\s*([A-Za-z_0-9]+)?',cl,re.IGNORECASE)
    if m:
        sr=(m.group(4) or "PTS").upper()
        return {"description":leg,"player":m.group(1).strip(),"bet_type":"player_prop",
                "stat":sm.get(sr,sr),"line":float(m.group(3)),
                "direction":"over" if m.group(2).lower() in ("o","over") else "under","team":None,"opponent":None}
    m2=re.match(r'(.+?)\s+([\d.]+)\+\s*([A-Za-z_0-9]+)',cl,re.IGNORECASE)
    if m2:
        sr=m2.group(3).upper()
        return {"description":leg,"player":m2.group(1).strip(),"bet_type":"player_prop",
                "stat":sm.get(sr,sr),"line":float(m2.group(2)),"direction":"over","team":None,"opponent":None}
    m3=re.match(r'(.+?)\s+([\d.]+)\s+([A-Za-z_0-9]+)',cl,re.IGNORECASE)
    if m3:
        sr=m3.group(3).upper()
        return {"description":leg,"player":m3.group(1).strip(),"bet_type":"player_prop",
                "stat":sm.get(sr,sr),"line":float(m3.group(2)),"direction":"over","team":None,"opponent":None}
    return {"description":leg,"player":None,"bet_type":"other","stat":None,"line":None,"direction":None,"team":None,"opponent":None}

def grade_parlay(bet,ps,gr,sp="nba"):
    legs=parse_legs(bet.get("description",""),sp)
    if not legs: return "unknown",[]
    lr=[]
    for leg in legs:
        r=grade_bet(leg,ps,gr,sp)
        lr.append({"description":leg["description"],"result":r})
        print(f"      Leg: {leg['description'][:50]} → {r}")
    # DNP legs VOIDED — standard sportsbook rules
    active=[r for r in lr if r["result"]!="dnp"]
    if not active: return "dnp",lr
    rs={r["result"] for r in active}
    if "loss" in rs: return "loss",lr
    if "unknown" in rs: return "unknown",lr
    if all(r["result"] in ("win","push") for r in active):
        return ("win" if any(r["result"]=="win" for r in active) else "push"),lr
    return "unknown",lr

# ── Format ────────────────────────────────────────────────────────────────────
def format_results(graded, date_str, sport=None):
    if not graded: return None
    by_user={}
    for b in graded: by_user.setdefault(b["user"],[]).append(b)
    se=SPORT_EMOJI.get(sport,"📊"); sl=(sport or "").upper(); dd=fmt_date(date_str)
    hdr = f"{se} <b>{sl} Bet Results — {dd}</b>" if sl else f"📊 <b>Bet Results — {dd}</b>"
    lines=[hdr, "───────────────"]
    for user,bets in by_user.items():
        st=[b for b in bets if b.get("bet_type")!="parlay"]
        pa=[b for b in bets if b.get("bet_type")=="parlay"]
        sw=sum(1 for b in st if b["result"]=="win"); sl_=sum(1 for b in st if b["result"]=="loss")
        sp=sum(1 for b in st if b["result"]=="push"); sd=sum(1 for b in st if b["result"] in ("dnp","unknown"))
        pw=sum(1 for b in pa if b["result"]=="win"); pl_=sum(1 for b in pa if b["result"]=="loss")
        stt=sw+sl_; ptt=pw+pl_
        sr=f"{round(sw/stt*100,1)}%" if stt>0 else "—"
        pr=f"{round(pw/ptt*100,1)}%" if ptt>0 else "—"
        lines.append(f"\n💬 <b>u/{user}</b>")
        pt="" if not sp else f"  <i>({sp} push)</i>"
        vt="" if not sd else f"  <i>({sd} DNP)</i>"
        lines.append(f"  📈 Straight: <b>{sw}W / {sl_}L</b>  {sr}{pt}{vt}")
        lines.append(f"  🎰 Parlays:  <b>{pw}W / {pl_}L</b>  {pr}")
        if st:
            lines.append("\n<b>Individual Plays:</b>")
            for b in st:
                ic=ICON.get(b["result"],"❓"); ds=esc(b.get("description",""))
                vd="  <i>(DNP)</i>" if b["result"]=="dnp" else ""
                lines.append(f"  {ic} {ds}{vd}")
        if pa:
            lines.append("\n<b>Parlays:</b>")
            for b in pa:
                oi=ICON.get(b["result"],"❓")
                lb=esc(b.get("description","Parlay").split(":")[0].strip())
                lines.append(f"\n  {oi} <b>{lb}</b>")
                for lg in b.get("leg_results",[]):
                    li=ICON.get(lg["result"],"❓"); ld=esc(lg["description"])
                    dt="  <i>(DNP void)</i>" if lg["result"]=="dnp" else ""
                    lines.append(f"      {li} {ld}{dt}")
    return "\n".join(lines)

def format_overall(stats,start):
    if not stats: return None
    sd=fmt_date(start)
    lines=[f"📈 <b>Overall Record</b>  <i>(since {sd})</i>","───────────────"]
    for user,r in stats.items():
        sw=r.get("straight_wins",0); sl=r.get("straight_losses",0)
        sp=r.get("straight_pushes",0); pw=r.get("parlay_wins",0); pl=r.get("parlay_losses",0)
        stt=sw+sl; ptt=pw+pl; tt=stt+ptt
        sr=f"{round(sw/stt*100,1)}%" if stt>0 else "—"
        pr=f"{round(pw/ptt*100,1)}%" if ptt>0 else "—"
        tr=f"{round((sw+pw)/tt*100,1)}%" if tt>0 else "—"
        lines.append(f"\n💬 <b>u/{user}</b>")
        lines.append(f"  📊 <b>Total:</b>  <b>{tr}</b>  ({sw+pw}W / {sl+pl}L of {tt})")
        lines.append(f"  🎯 Individual: {sw}W / {sl}L" + (f" / {sp}P" if sp else "") + f"  →  {sr}")
        lines.append(f"  🎰 Parlays: {pw}W / {pl}L  →  {pr}")
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🎯 Grader v{V} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    st=load_state()
    for k in ["pending_bets","graded_bets"]:
        if k not in st: st[k]=[]
    if "stats" not in st: st["stats"]={}
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if "tracking_start" not in st: st["tracking_start"]=today

    pending=[b for b in st["pending_bets"] if b.get("date","9")<today]
    if not pending: print("No pending bets."); save_state(st); return

    combos=sorted(set((b["date"],b.get("sport","nba")) for b in pending))
    print(f"  📋 {len(pending)} bets, {len(combos)} date/sport combos")

    all_ps,all_gr={},{}
    for ds,sp in combos:
        ed=ds.replace("-","")
        print(f"\n  {SPORT_EMOJI.get(sp,'📊')} {sp.upper()} {ds}")
        games=scoreboard(sp,ed); print(f"    {len(games)} games")
        if not games: continue
        all_ps[(ds,sp)]=player_stats(games,sp)
        all_gr[(ds,sp)]=game_results(games)
        print(f"    {len(all_ps[(ds,sp)])} players"); time.sleep(1)

    graded=[]; dates_done=set()
    for bet in pending:
        ds,sp=bet["date"],bet.get("sport","nba")
        ps=all_ps.get((ds,sp),{}); gr=all_gr.get((ds,sp),[])
        if not ps and not gr: continue
        u=bet["user"]
        if u not in st["stats"]:
            st["stats"][u]={"straight_wins":0,"straight_losses":0,"straight_pushes":0,"parlay_wins":0,"parlay_losses":0}

        if bet.get("bet_type")=="parlay":
            print(f"\n  🎰 {bet.get('description','')[:60]}")
            res,lr=grade_parlay(bet,ps,gr,sp); bet["result"]=res; bet["leg_results"]=lr
        else:
            res=grade_bet(bet,ps,gr,sp); bet["result"]=res
        bet["graded_date"]=today

        s=st["stats"][u]
        if bet.get("bet_type")=="parlay":
            if res=="win": s["parlay_wins"]+=1
            elif res=="loss": s["parlay_losses"]+=1
        else:
            if res=="win": s["straight_wins"]+=1
            elif res=="loss": s["straight_losses"]+=1
            elif res=="push": s["straight_pushes"]+=1

        print(f"  {ICON.get(res,'❓')} {bet.get('description','')[:60]} → {res}")
        graded.append(bet); dates_done.add((ds,sp)); time.sleep(0.2)

    gids={b["id"] for b in graded}
    st["pending_bets"]=[b for b in st["pending_bets"] if b["id"] not in gids]
    st["graded_bets"].extend(graded); save_state(st)

    for ds,sp in sorted(dates_done):
        dg=[b for b in graded if b["date"]==ds and b.get("sport","nba")==sp]
        msg=format_results(dg,ds,sp)
        if msg: tg_send(msg); print(f"  📨 {sp.upper()} {ds} results sent"); time.sleep(1)

    msg=format_overall(st["stats"],st.get("tracking_start",today))
    if msg: tg_send(msg); print("  📨 Overall stats sent")
    print(f"\n✅ Grader v{V} done — {len(graded)} graded")

if __name__=="__main__": main()
