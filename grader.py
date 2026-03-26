import requests
import json
import os
import sys
import time
import re
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

STATE_FILE = "state.json"
HEADERS    = {"User-Agent": "Mozilla/5.0 nba-picks-bot/1.0"}

RESULT_ICON = {
    "win": "✅", "loss": "❌", "push": "➡️",
    "unknown": "❓", "dnp": "🚫",
}

STAT_MAP = {
    "PTS": "PTS", "P": "PTS", "POINTS": "PTS", "PT": "PTS",
    "REB": "REB", "R": "REB", "REBOUNDS": "REB",
    "AST": "AST", "A": "AST", "ASSISTS": "AST",
    "3PM": "3PM", "3S": "3PM", "3": "3PM", "THREES": "3PM",
    "3'S": "3PM", "THREE": "3PM", "TPM": "3PM",
    "BLK": "BLK", "BLOCKS": "BLK",
    "STL": "STL", "STEALS": "STL",
    "PRA": "PRA", "PA": "PA", "PR": "PR", "RA": "RA",
}


# ── State ─────────────────────────────────────────────────────────────────────
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


def escape_html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def split_message(text, limit=4000):
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


def send_telegram(text):
    for chunk in split_message(text):
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
            print(f"Telegram error: {resp.text}", file=sys.stderr)
        time.sleep(0.3)


# ── ESPN ──────────────────────────────────────────────────────────────────────
def get_espn_scoreboard(date_str):
    """date_str = YYYYMMDD"""
    url = (f"https://site.api.espn.com/apis/site/v2/sports/basketball/"
           f"nba/scoreboard?dates={date_str}")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json().get("events", [])
    except Exception as e:
        print(f"ESPN scoreboard error: {e}", file=sys.stderr)
        return []


def get_espn_boxscore(game_id):
    url = (f"https://site.api.espn.com/apis/site/v2/sports/basketball/"
           f"nba/summary?event={game_id}")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"ESPN boxscore error: {e}", file=sys.stderr)
        return {}


def parse_espn_made_attempted(val):
    """Parse ESPN "made-attempted" strings like "4-6" → (made, attempted)."""
    if isinstance(val, str) and "-" in val:
        parts = val.split("-")
        try:
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return 0, 0
    try:
        return int(float(val or 0)), None
    except (ValueError, TypeError):
        return 0, None


def parse_espn_minutes(val):
    """Parse ESPN minutes — "32:14" or plain number."""
    if isinstance(val, str) and ":" in val:
        try:
            return float(val.split(":")[0])
        except (ValueError, IndexError):
            return 0.0
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def build_player_stats(games):
    """
    Returns {player_name_lower: {name, team, stats, played}}.

    ESPN stat names: ["MIN","FG","3PT","FT","OREB","DREB","REB","AST","STL","BLK","TO","PF","PTS","+/-"]
    FG/3PT/FT are "made-attempted" strings.  We map ESPN "3PT" → internal "3PM".
    """
    players = {}
    for game in games:
        game_id = game.get("id")
        if not game_id:
            continue
        box = get_espn_boxscore(game_id)
        time.sleep(0.5)

        for team_entry in box.get("boxscore", {}).get("players", []):
            team_abbr = team_entry.get("team", {}).get("abbreviation", "")

            for stat_group in team_entry.get("statistics", []):
                names = stat_group.get("names", [])

                for athlete_entry in stat_group.get("athletes", []):
                    athlete  = athlete_entry.get("athlete", {})
                    fullname = athlete.get("displayName", "")
                    raw      = athlete_entry.get("stats", [])
                    if not fullname:
                        continue

                    raw_map = {}
                    for i, n in enumerate(names):
                        if i < len(raw):
                            raw_map[n] = raw[i]

                    mins = parse_espn_minutes(raw_map.get("MIN", "0"))
                    played = mins > 0

                    fg_made,  _ = parse_espn_made_attempted(raw_map.get("FG", "0"))
                    tpm_made, _ = parse_espn_made_attempted(raw_map.get("3PT", "0"))
                    ft_made,  _ = parse_espn_made_attempted(raw_map.get("FT", "0"))

                    def safe_float(key):
                        v = raw_map.get(key, 0)
                        try:
                            return float(v or 0)
                        except (ValueError, TypeError):
                            return 0.0

                    pts = safe_float("PTS")
                    reb = safe_float("REB")
                    ast = safe_float("AST")
                    blk = safe_float("BLK")
                    stl = safe_float("STL")

                    s = {
                        "PTS": pts, "REB": reb, "AST": ast,
                        "3PM": float(tpm_made),
                        "BLK": blk, "STL": stl,
                        "FGM": float(fg_made),
                        "FTM": float(ft_made),
                        "PRA": pts + reb + ast,
                        "PR":  pts + reb,
                        "PA":  pts + ast,
                        "RA":  reb + ast,
                    }

                    players[fullname.lower()] = {
                        "name": fullname, "team": team_abbr,
                        "stats": s, "played": played,
                    }
                    print(f"    ESPN: {fullname} | PTS:{pts} REB:{reb} AST:{ast} "
                          f"3PM:{tpm_made} BLK:{blk} STL:{stl} "
                          f"PR:{pts+reb:.0f} RA:{reb+ast:.0f} PRA:{pts+reb+ast:.0f} "
                          f"| played:{played}")
    return players


def build_game_results(games):
    results = []
    for game in games:
        comps = game.get("competitions", [{}])[0]
        teams = comps.get("competitors", [])
        if len(teams) < 2:
            continue
        home = next((t for t in teams if t.get("homeAway") == "home"), teams[0])
        away = next((t for t in teams if t.get("homeAway") == "away"), teams[1])
        hs   = int(home.get("score", 0) or 0)
        as_  = int(away.get("score", 0) or 0)
        ht   = home.get("team", {}).get("abbreviation", "")
        at   = away.get("team", {}).get("abbreviation", "")
        results.append({
            "home": ht, "away": at,
            "home_score": hs, "away_score": as_,
            "total": hs + as_,
            "winner": ht if hs > as_ else at,
        })
    return results


# ── Player Lookup ─────────────────────────────────────────────────────────────
def find_player(player_name, player_stats):
    if not player_name or not player_stats:
        return None
    name_lower = player_name.lower().strip()

    if name_lower in player_stats:
        return player_stats[name_lower]

    parts = [p for p in name_lower.split() if len(p) > 2]
    for pname, pd in player_stats.items():
        if parts and all(p in pname for p in parts):
            return pd

    last = name_lower.split()[-1] if name_lower.split() else ""
    if len(last) > 4:
        matches = [pd for pname, pd in player_stats.items()
                   if last in pname.split()]
        if len(matches) == 1:
            return matches[0]

    nickname_map = {
        "kat": "karl-anthony towns",
        "sga": "shai gilgeous-alexander",
        "ad":  "anthony davis",
        "pg":  "paul george",
        "rj":  "rj barrett",
        "cj":  "cj mccollum",
    }
    mapped = nickname_map.get(name_lower)
    if mapped and mapped in player_stats:
        return player_stats[mapped]

    return None


# ── Grading ───────────────────────────────────────────────────────────────────
def grade_single_bet(bet, player_stats, game_results):
    btype     = bet.get("bet_type", "other")
    direction = (bet.get("direction") or "").lower()
    line      = bet.get("line")
    player    = bet.get("player") or ""
    stat_cat  = (bet.get("stat") or "").upper()
    team      = (bet.get("team") or "").upper()
    opponent  = (bet.get("opponent") or "").upper()

    stat_cat = STAT_MAP.get(stat_cat, stat_cat)

    if btype == "player_prop" and player and stat_cat and line is not None:
        pdata = find_player(player, player_stats)

        if pdata is not None:
            if not pdata.get("played", True):
                return "dnp"
            actual = pdata["stats"].get(stat_cat)
            if actual is not None:
                try:
                    actual = float(actual)
                    line   = float(line)
                    print(f"    Grading {player}: {stat_cat} "
                          f"actual={actual} line={line} dir={direction}")
                    if direction == "over":
                        if actual > line:   return "win"
                        elif actual == line: return "push"
                        else:               return "loss"
                    elif direction == "under":
                        if actual < line:   return "win"
                        elif actual == line: return "push"
                        else:               return "loss"
                except (ValueError, TypeError):
                    pass
        elif player_stats:
            print(f"    ⚠️  {player} not found in ESPN data — marking DNP")
            return "dnp"

    elif btype == "total" and line is not None:
        for g in game_results:
            if team in (g["home"], g["away"]) or opponent in (g["home"], g["away"]):
                try:
                    tot  = float(g["total"])
                    line = float(line)
                    if direction == "over":
                        return "win" if tot > line else ("push" if tot == line else "loss")
                    elif direction == "under":
                        return "win" if tot < line else ("push" if tot == line else "loss")
                except (ValueError, TypeError):
                    pass

    elif btype == "moneyline" and team:
        for g in game_results:
            if team in (g["home"], g["away"]):
                return "win" if g["winner"] == team else "loss"

    elif btype == "spread" and team and line is not None:
        for g in game_results:
            if team in (g["home"], g["away"]):
                try:
                    line = float(line)
                    team_score = (g["home_score"] if g["home"] == team
                                  else g["away_score"])
                    opp_score  = (g["away_score"] if g["home"] == team
                                  else g["home_score"])
                    diff = team_score + line - opp_score
                    if diff > 0:    return "win"
                    elif diff == 0: return "push"
                    else:           return "loss"
                except (ValueError, TypeError):
                    pass

    return grade_with_claude(bet, player_stats, game_results)


def grade_with_claude(bet, player_stats, game_results):
    if not ANTHROPIC_API_KEY:
        return "unknown"

    relevant = {}
    pdata = find_player(bet.get("player") or "", player_stats)
    if pdata:
        relevant[pdata["name"]] = pdata["stats"]

    prompt = f"""Grade this NBA bet using the stats provided.

BET: {bet.get('description', 'N/A')}
Player: {bet.get('player')} | Stat: {bet.get('stat')} | Line: {bet.get('line')} | Direction: {bet.get('direction')}

PLAYER STATS:
{json.dumps(relevant, indent=2)}

GAME RESULTS:
{json.dumps(game_results[:5], indent=2)}

Reply with ONLY one word: win, loss, push, dnp, or unknown.
Use "dnp" if the player did not play. Use "unknown" only if data is truly insufficient."""

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
                "max_tokens": 10,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        if resp.ok:
            result = resp.json()["content"][0]["text"].strip().lower()
            if result in ("win", "loss", "push", "dnp", "unknown"):
                return result
    except Exception as e:
        print(f"Claude grade error: {e}", file=sys.stderr)
    return "unknown"


# ── Parlay Parsing ────────────────────────────────────────────────────────────
def parse_parlay_legs(description):
    desc = re.sub(r'^[^:]+:\s*', '', description, count=1)
    desc = re.sub(r'\s*\([+-]\d+\)\s*$', '', desc).strip()
    raw_legs = re.split(r'\s+\+\s+', desc)
    legs = []
    for leg in raw_legs:
        leg = leg.strip()
        if not leg:
            continue
        legs.append(parse_one_leg(leg))
    return legs


def parse_one_leg(leg):
    # Clean "3's" / "3s" → "3PM"
    clean_leg = re.sub(r"(\d)\s*3'?s\b", r"\1 3PM", leg, flags=re.IGNORECASE)

    # Pattern 1: "Player O/U line STAT"
    m = re.match(
        r'(.+?)\s+(O|U|Over|Under)\s*([\d.]+)\+?\s*([A-Za-z0-9]+)?',
        clean_leg, re.IGNORECASE
    )
    if m:
        player_name = m.group(1).strip()
        direction   = "over" if m.group(2).lower() in ("o", "over") else "under"
        line_val    = float(m.group(3))
        stat_raw    = (m.group(4) or "PTS").upper().replace("+", "").strip()
        stat        = STAT_MAP.get(stat_raw, stat_raw)
        return {
            "description": leg, "player": player_name, "bet_type": "player_prop",
            "stat": stat, "line": line_val, "direction": direction,
            "team": None, "opponent": None,
        }

    # Pattern 2: "Player line+ STAT"
    m2 = re.match(r'(.+?)\s+([\d.]+)\+\s*([A-Za-z0-9]+)', clean_leg, re.IGNORECASE)
    if m2:
        player_name = m2.group(1).strip()
        line_val    = float(m2.group(2))
        stat_raw    = m2.group(3).upper().strip()
        stat        = STAT_MAP.get(stat_raw, stat_raw)
        return {
            "description": leg, "player": player_name, "bet_type": "player_prop",
            "stat": stat, "line": line_val, "direction": "over",
            "team": None, "opponent": None,
        }

    # Pattern 3: "Player line STAT"
    m3 = re.match(r'(.+?)\s+([\d.]+)\s+([A-Za-z0-9]+)', clean_leg, re.IGNORECASE)
    if m3:
        player_name = m3.group(1).strip()
        line_val    = float(m3.group(2))
        stat_raw    = m3.group(3).upper().strip()
        stat        = STAT_MAP.get(stat_raw, stat_raw)
        return {
            "description": leg, "player": player_name, "bet_type": "player_prop",
            "stat": stat, "line": line_val, "direction": "over",
            "team": None, "opponent": None,
        }

    return {
        "description": leg, "player": None, "bet_type": "other",
        "stat": None, "line": None, "direction": None,
        "team": None, "opponent": None,
    }


def grade_parlay(bet, player_stats, game_results):
    legs = parse_parlay_legs(bet.get("description", ""))
    if not legs:
        return "unknown", []

    leg_results = []
    for leg in legs:
        result = grade_single_bet(leg, player_stats, game_results)
        leg_results.append({"description": leg["description"], "result": result})
        print(f"      Leg: {leg['description'][:50]} → {result}")

    results_set = {r["result"] for r in leg_results}

    if "loss" in results_set:
        overall = "loss"
    elif "unknown" in results_set:
        overall = "unknown"
    elif all(r["result"] in ("win", "push") for r in leg_results):
        non_push = [r for r in leg_results if r["result"] != "push"]
        overall  = "win" if non_push else "push"
    elif any(r["result"] == "dnp" for r in leg_results):
        overall = "dnp"
    else:
        overall = "unknown"

    return overall, leg_results


# ── Message Formatting ────────────────────────────────────────────────────────
def format_daily_results(graded, date_str):
    if not graded:
        return None

    by_user = {}
    for bet in graded:
        by_user.setdefault(bet["user"], []).append(bet)

    lines = [f"📊 <b>Bet Results — {date_str}</b>", "━" * 32]

    for user, bets in by_user.items():
        straights = [b for b in bets if b.get("bet_type") != "parlay"]
        parlays   = [b for b in bets if b.get("bet_type") == "parlay"]

        sw = sum(1 for b in straights if b["result"] == "win")
        sl = sum(1 for b in straights if b["result"] == "loss")
        sp = sum(1 for b in straights if b["result"] == "push")
        sd = sum(1 for b in straights if b["result"] in ("dnp", "unknown"))
        pw = sum(1 for b in parlays if b["result"] == "win")
        pl = sum(1 for b in parlays if b["result"] == "loss")

        st = sw + sl
        pt = pw + pl
        s_rate = f"{round(sw/st*100,1)}%" if st > 0 else "—"
        p_rate = f"{round(pw/pt*100,1)}%" if pt > 0 else "—"

        lines.append(f"\n💬 <b>u/{user}</b>")
        push_tag = f"  <i>({sp} push)</i>" if sp else ""
        void_tag = f"  <i>({sd} void/DNP)</i>" if sd else ""
        lines.append(f"  📈 Straight: <b>{sw}W / {sl}L</b>  {s_rate}"
                      f"{push_tag}{void_tag}")
        lines.append(f"  🎰 Parlays:  <b>{pw}W / {pl}L</b>  {p_rate}")

        if straights:
            lines.append("\n<b>Individual Plays:</b>")
            for bet in straights:
                icon = RESULT_ICON.get(bet["result"], "❓")
                desc = escape_html(bet.get("description", ""))
                void = "  <i>(void — DNP)</i>" if bet["result"] == "dnp" else ""
                lines.append(f"  {icon} {desc}{void}")

        if parlays:
            lines.append("\n<b>Parlays:</b>")
            for bet in parlays:
                overall_icon = RESULT_ICON.get(bet["result"], "❓")
                label = escape_html(
                    bet.get("description", "Parlay").split(":")[0].strip()
                )
                lines.append(f"\n  {overall_icon} <b>{label}</b>")
                for leg in bet.get("leg_results", []):
                    leg_icon = RESULT_ICON.get(leg["result"], "❓")
                    lines.append(
                        f"      {leg_icon} {escape_html(leg['description'])}"
                    )

    return "\n".join(lines)


def format_overall_stats(stats, start_date):
    if not stats:
        return None

    lines = [
        f"📈 <b>Overall Record</b>  <i>(since {start_date})</i>",
        "━" * 32,
    ]

    for user, rec in stats.items():
        sw = rec.get("straight_wins", 0)
        sl = rec.get("straight_losses", 0)
        sp = rec.get("straight_pushes", 0)
        pw = rec.get("parlay_wins", 0)
        pl = rec.get("parlay_losses", 0)

        st = sw + sl
        pt = pw + pl
        tt = st + pt

        s_rate = f"{round(sw/st*100,1)}%" if st > 0 else "—"
        p_rate = f"{round(pw/pt*100,1)}%" if pt > 0 else "—"
        t_rate = f"{round((sw+pw)/tt*100,1)}%" if tt > 0 else "—"

        lines.append(f"\n💬 <b>u/{user}</b>")
        lines.append(
            f"  📊 <b>Total Hit Rate:</b>  <b>{t_rate}</b>  "
            f"({sw+pw}W / {sl+pl}L of {tt} bets)"
        )
        lines.append(
            f"  🎯 <b>Individual:</b>  {sw}W / {sl}L"
            + (f" / {sp}P" if sp else "")
            + f"  →  {s_rate}"
        )
        lines.append(f"  🎰 <b>Parlays:</b>  {pw}W / {pl}L  →  {p_rate}")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🎯  Grader starting — "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    state = load_state()
    for key in ["pending_bets", "graded_bets"]:
        if key not in state:
            state[key] = []
    if "stats" not in state:
        state["stats"] = {}

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if "tracking_start" not in state:
        state["tracking_start"] = today_str

    # ── KEY CHANGE: grade ALL pending bets for past dates, not just yesterday ──
    # This makes the grader resilient to downtime — missed days get caught up.
    pending = [b for b in state["pending_bets"] if b.get("date", "9999") < today_str]

    if not pending:
        print(f"No pending bets for any past date — nothing to do.")
        save_state(state)
        return

    # Group pending bets by date so we can fetch ESPN data per date
    dates_needed = sorted(set(b["date"] for b in pending))
    print(f"  📋  {len(pending)} bets to grade across {len(dates_needed)} date(s): "
          f"{', '.join(dates_needed)}")

    # Fetch ESPN data for each date
    all_player_stats = {}   # {date_str: player_stats_dict}
    all_game_results = {}   # {date_str: game_results_list}

    for date_str in dates_needed:
        espn_date = date_str.replace("-", "")
        print(f"\n  🏀  Fetching ESPN data for {date_str}...")
        games = get_espn_scoreboard(espn_date)
        print(f"      {len(games)} games found")

        if not games:
            print(f"      ⚠️  No ESPN data for {date_str} — those bets will be skipped")
            continue

        all_player_stats[date_str] = build_player_stats(games)
        all_game_results[date_str] = build_game_results(games)
        print(f"      👤  {len(all_player_stats[date_str])} players loaded")
        time.sleep(1)

    # Grade bets, grouped by date for correct ESPN data lookup
    all_graded = []
    dates_graded = set()

    for bet in pending:
        date_str     = bet["date"]
        player_stats = all_player_stats.get(date_str, {})
        game_results = all_game_results.get(date_str, [])

        # Skip if no ESPN data available for this date
        if not player_stats and not game_results:
            print(f"  ⏭️  Skipping {bet.get('description','')[:50]} — no ESPN data for {date_str}")
            continue

        user = bet["user"]
        if user not in state["stats"]:
            state["stats"][user] = {
                "straight_wins": 0, "straight_losses": 0,
                "straight_pushes": 0,
                "parlay_wins": 0, "parlay_losses": 0,
            }
        for k in ("straight_wins", "straight_losses", "straight_pushes",
                   "parlay_wins", "parlay_losses"):
            state["stats"][user].setdefault(k, 0)

        if bet.get("bet_type") == "parlay":
            print(f"\n  🎰  Grading parlay: {bet.get('description','')[:60]}")
            result, leg_results = grade_parlay(bet, player_stats, game_results)
            bet["result"]      = result
            bet["leg_results"] = leg_results
        else:
            result = grade_single_bet(bet, player_stats, game_results)
            bet["result"] = result

        bet["graded_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Update stats
        if bet.get("bet_type") == "parlay":
            if result == "win":
                state["stats"][user]["parlay_wins"] += 1
            elif result == "loss":
                state["stats"][user]["parlay_losses"] += 1
        else:
            if result == "win":
                state["stats"][user]["straight_wins"] += 1
            elif result == "loss":
                state["stats"][user]["straight_losses"] += 1
            elif result == "push":
                state["stats"][user]["straight_pushes"] += 1

        icon = RESULT_ICON.get(result, "❓")
        print(f"  {icon}  {bet.get('description','')[:60]}  →  {result}")
        all_graded.append(bet)
        dates_graded.add(date_str)
        time.sleep(0.2)

    # Move graded bets out of pending
    graded_ids = {b["id"] for b in all_graded}
    state["pending_bets"] = [
        b for b in state["pending_bets"] if b["id"] not in graded_ids
    ]
    state["graded_bets"].extend(all_graded)
    save_state(state)

    # Send daily results per date
    for date_str in sorted(dates_graded):
        day_graded = [b for b in all_graded if b["date"] == date_str]
        results_msg = format_daily_results(day_graded, date_str)
        if results_msg:
            send_telegram(results_msg)
            print(f"  📨  Daily results sent for {date_str}")
            time.sleep(1)

    # Send overall stats (once, after all daily results)
    stats_msg = format_overall_stats(
        state["stats"], state.get("tracking_start", today_str)
    )
    if stats_msg:
        send_telegram(stats_msg)
        print("  📨  Overall stats sent")

    print(f"\n✅  Grader done — {len(all_graded)} bets graded "
          f"across {len(dates_graded)} date(s)")


if __name__ == "__main__":
    main()
