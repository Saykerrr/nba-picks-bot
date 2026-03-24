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
HEADERS    = {"User-Agent": "nba-picks-bot/1.0"}


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
            print(f"Telegram error: {resp.text}", file=sys.stderr)
        if len(chunks) > 1:
            time.sleep(0.5)

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


# ── ESPN API ──────────────────────────────────────────────────────────────────
def get_espn_scoreboard(date_str):
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date_str}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json().get("events", [])
    except Exception as e:
        print(f"ESPN scoreboard error: {e}", file=sys.stderr)
        return []

def get_espn_boxscore(game_id):
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={game_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"ESPN boxscore error: {e}", file=sys.stderr)
        return {}

def build_player_stats(games):
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
                    if not fullname or not raw:
                        continue
                    raw_stats = {}
                    for i, n in enumerate(names):
                        if i < len(raw):
                            try:
                                raw_stats[n] = float(raw[i])
                            except (ValueError, TypeError):
                                raw_stats[n] = raw[i]
                    s = {
                        "PTS": float(raw_stats.get("PTS", raw_stats.get("points", 0)) or 0),
                        "REB": float(raw_stats.get("REB", raw_stats.get("rebounds", 0)) or 0),
                        "AST": float(raw_stats.get("AST", raw_stats.get("assists", 0)) or 0),
                        "3PM": float(raw_stats.get("3PM", raw_stats.get("threePointFieldGoalsMade", 0)) or 0),
                        "BLK": float(raw_stats.get("BLK", raw_stats.get("blocks", 0)) or 0),
                        "STL": float(raw_stats.get("STL", raw_stats.get("steals", 0)) or 0),
                    }
                    s["PRA"] = s["PTS"] + s["REB"] + s["AST"]
                    s["PR"]  = s["PTS"] + s["REB"]
                    s["PA"]  = s["PTS"] + s["AST"]
                    s["RA"]  = s["REB"] + s["AST"]
                    players[fullname.lower()] = {"name": fullname, "team": team_abbr, "stats": s}
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


# ── Grading ───────────────────────────────────────────────────────────────────
def find_player(player_name, player_stats):
    if not player_name:
        return None
    parts = [p for p in player_name.lower().split() if len(p) > 2]
    for pname, pd in player_stats.items():
        if parts and all(p in pname for p in parts):
            return pd
    return None

def grade_single_bet(bet, player_stats, game_results):
    """Grade one straight bet. Returns 'win', 'loss', 'push', or 'unknown'."""
    btype     = bet.get("bet_type", "other")
    direction = (bet.get("direction") or "").lower()
    line      = bet.get("line")
    player    = (bet.get("player") or "").lower()
    stat_cat  = (bet.get("stat") or "").upper()
    team      = (bet.get("team") or "").upper()
    opponent  = (bet.get("opponent") or "").upper()

    # Player prop
    if btype == "player_prop" and player and stat_cat and line is not None:
        pdata = find_player(player, player_stats)
        if pdata:
            actual = pdata["stats"].get(stat_cat)
            if actual is not None:
                try:
                    actual = float(actual)
                    line   = float(line)
                    if direction == "over":
                        return "win" if actual > line else ("push" if actual == line else "loss")
                    elif direction == "under":
                        return "win" if actual < line else ("push" if actual == line else "loss")
                except (ValueError, TypeError):
                    pass

    # Game total
    elif btype == "total" and line is not None:
        for g in game_results:
            if team in (g["home"], g["away"]) or opponent in (g["home"], g["away"]):
                try:
                    if direction == "over":
                        return "win" if float(g["total"]) > float(line) else "loss"
                    elif direction == "under":
                        return "win" if float(g["total"]) < float(line) else "loss"
                except (ValueError, TypeError):
                    pass

    # Moneyline
    elif btype == "moneyline" and team:
        for g in game_results:
            if team in (g["home"], g["away"]):
                return "win" if g["winner"] == team else "loss"

    # Spread
    elif btype == "spread" and team and line is not None:
        for g in game_results:
            if team in (g["home"], g["away"]):
                try:
                    line = float(line)
                    diff = (g["home_score"] if g["home"] == team else g["away_score"]) + line - \
                           (g["away_score"] if g["home"] == team else g["home_score"])
                    return "win" if diff > 0 else ("push" if diff == 0 else "loss")
                except (ValueError, TypeError):
                    pass

    # Claude fallback
    return grade_with_claude(bet, player_stats, game_results)


def grade_with_claude(bet, player_stats, game_results):
    if not ANTHROPIC_API_KEY:
        return "unknown"
    relevant = {}
    player = (bet.get("player") or "").lower()
    if player:
        pdata = find_player(player, player_stats)
        if pdata:
            relevant[pdata["name"]] = pdata["stats"]

    prompt = f"""Grade this NBA bet.

BET: {bet.get('description', 'N/A')}
Type: {bet.get('bet_type')} | Player: {bet.get('player')} | Stat: {bet.get('stat')} | Line: {bet.get('line')} | Direction: {bet.get('direction')}

GAME RESULTS:
{json.dumps(game_results, indent=2)}

PLAYER STATS:
{json.dumps(relevant, indent=2)}

Reply with ONLY one word: win, loss, push, or unknown."""
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 5,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20,
        )
        if resp.ok:
            result = resp.json()["content"][0]["text"].strip().lower()
            if result in ("win", "loss", "push", "unknown"):
                return result
    except Exception as e:
        print(f"Claude grade error: {e}", file=sys.stderr)
    return "unknown"


def parse_parlay_legs(description):
    """
    Parse a parlay description string into individual leg dicts.
    Handles formats like:
      "Parlay 1: Amen Thompson O12.5 RA + Stephon Castle O13.5 RA + Cooper Flagg O12.5 RA"
    """
    # Strip leading label e.g. "Parlay 1: " or "Degen Parlay 2: "
    desc = re.sub(r'^.*?:\s*', '', description, count=1)
    # Remove odds at end e.g. "(+600)"
    desc = re.sub(r'\([+-]\d+\)', '', desc)
    # Split on + or /
    raw_legs = re.split(r'\s*\+\s*|\s*/\s*', desc)

    legs = []
    for leg in raw_legs:
        leg = leg.strip()
        if not leg:
            continue
        # Try to parse: "Player Name O/U line STAT"
        m = re.match(
            r'(.+?)\s+(O|U|Over|Under)\s*([\d.]+)\s*([A-Z0-9+]+)?',
            leg, re.IGNORECASE
        )
        if m:
            player_name = m.group(1).strip()
            direction   = "over" if m.group(2).lower() in ("o", "over") else "under"
            line        = float(m.group(3))
            stat_raw    = (m.group(4) or "").upper().replace("+", "")
            # Map stat shorthand
            stat_map = {
                "PTS": "PTS", "P": "PTS", "POINTS": "PTS",
                "REB": "REB", "R": "REB",
                "AST": "AST", "A": "AST",
                "3PM": "3PM", "3S": "3PM", "3": "3PM",
                "BLK": "BLK", "STL": "STL",
                "PRA": "PRA", "PA": "PA", "PR": "PR", "RA": "RA",
            }
            stat = stat_map.get(stat_raw, stat_raw or "PTS")
            legs.append({
                "description": leg,
                "player":      player_name,
                "bet_type":    "player_prop",
                "stat":        stat,
                "line":        line,
                "direction":   direction,
                "team":        None,
                "opponent":    None,
            })
        else:
            # Can't parse — store as-is for Claude fallback
            legs.append({
                "description": leg,
                "player":      None,
                "bet_type":    "other",
                "stat":        None,
                "line":        None,
                "direction":   None,
                "team":        None,
                "opponent":    None,
            })
    return legs


def grade_parlay(bet, player_stats, game_results):
    """
    Grade a parlay by parsing and grading each leg.
    Returns (overall_result, leg_results_list)
    where leg_results_list = [{"description": ..., "result": ...}, ...]
    """
    legs = parse_parlay_legs(bet.get("description", ""))
    if not legs:
        return "unknown", []

    leg_results = []
    for leg in legs:
        result = grade_single_bet(leg, player_stats, game_results)
        leg_results.append({"description": leg["description"], "result": result})

    # Parlay wins only if ALL legs win (pushes reduce legs, one loss = loss)
    results_set = {r["result"] for r in leg_results}
    if "loss" in results_set:
        overall = "loss"
    elif "unknown" in results_set:
        overall = "unknown"
    elif all(r["result"] == "win" for r in leg_results):
        overall = "win"
    elif all(r["result"] in ("win", "push") for r in leg_results):
        overall = "push"
    else:
        overall = "loss"

    return overall, leg_results


# ── Message Formatting ────────────────────────────────────────────────────────
RESULT_ICON = {"win": "✅", "loss": "❌", "push": "➡️", "unknown": "❓"}


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

        pw = sum(1 for b in parlays if b["result"] == "win")
        pl = sum(1 for b in parlays if b["result"] == "loss")

        straight_total = sw + sl
        parlay_total   = pw + pl

        s_rate = f"{round(sw/straight_total*100,1)}%" if straight_total > 0 else "—"
        p_rate = f"{round(pw/parlay_total*100,1)}%"   if parlay_total   > 0 else "—"

        lines.append(f"\n💬 <b>u/{user}</b>")
        lines.append(f"  📈 Straight: <b>{sw}W / {sl}L</b>  ({s_rate})" + (f"  <i>{sp}P</i>" if sp else ""))
        lines.append(f"  🎰 Parlays:  <b>{pw}W / {pl}L</b>  ({p_rate})")

        if straights:
            lines.append("\n<b>Individual Plays:</b>")
            for bet in straights:
                icon = RESULT_ICON.get(bet["result"], "❓")
                desc = escape_html(bet.get("description", ""))
                lines.append(f"  {icon} {desc}")

        if parlays:
            lines.append("\n<b>Parlays:</b>")
            for bet in parlays:
                overall_icon = RESULT_ICON.get(bet["result"], "❓")
                # Build parlay header from first part of description
                label = escape_html(bet.get("description", "Parlay").split(":")[0])
                lines.append(f"\n  {overall_icon} <b>{label}</b>")
                # Show each leg result
                for leg in bet.get("leg_results", []):
                    leg_icon = RESULT_ICON.get(leg["result"], "❓")
                    lines.append(f"      {leg_icon} {escape_html(leg['description'])}")

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
        tt = sw + sl + pw + pl

        s_rate = f"{round(sw/st*100,1)}%" if st > 0 else "—"
        p_rate = f"{round(pw/pt*100,1)}%" if pt > 0 else "—"
        t_rate = f"{round((sw+pw)/tt*100,1)}%" if tt > 0 else "—"

        lines.append(f"\n💬 <b>u/{user}</b>")
        lines.append(f"  📊 <b>Total Hit Rate:</b>  {t_rate}  ({sw+pw}W / {sl+pl}L of {tt} bets)")
        lines.append(f"  🎯 <b>Individual:</b>  {sw}W / {sl}L" + (f" / {sp}P" if sp else "") + f"  →  {s_rate}")
        lines.append(f"  🎰 <b>Parlays:</b>  {pw}W / {pl}L  →  {p_rate}")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"🎯  Grader starting — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    state = load_state()
    for key in ["pending_bets", "graded_bets"]:
        if key not in state:
            state[key] = []
    if "stats" not in state:
        state["stats"] = {}

    yesterday_dt   = datetime.now(timezone.utc) - timedelta(days=1)
    yesterday_str  = yesterday_dt.strftime("%Y-%m-%d")
    yesterday_espn = yesterday_dt.strftime("%Y%m%d")

    if "tracking_start" not in state:
        state["tracking_start"] = yesterday_str

    pending = [b for b in state["pending_bets"] if b.get("date") == yesterday_str]

    if not pending:
        print(f"No pending bets for {yesterday_str}")
        if any((v.get("straight_wins",0) + v.get("straight_losses",0) +
                v.get("parlay_wins",0)   + v.get("parlay_losses",0)) > 0
               for v in state["stats"].values()):
            msg = format_overall_stats(state["stats"], state.get("tracking_start", yesterday_str))
            if msg:
                send_telegram(msg)
        save_state(state)
        return

    print(f"  📋  {len(pending)} bets to grade for {yesterday_str}")

    games = get_espn_scoreboard(yesterday_espn)
    print(f"  🏀  {len(games)} games found on ESPN")

    if not games:
        print("  ⚠️  No ESPN data — skipping")
        save_state(state)
        return

    player_stats = build_player_stats(games)
    game_results = build_game_results(games)
    print(f"  👤  {len(player_stats)} players, {len(game_results)} games")

    graded = []
    for bet in pending:
        user = bet["user"]
        if user not in state["stats"]:
            state["stats"][user] = {
                "straight_wins": 0, "straight_losses": 0, "straight_pushes": 0,
                "parlay_wins":   0, "parlay_losses":   0,
            }
        # Ensure old keys exist
        for k in ("straight_wins","straight_losses","straight_pushes","parlay_wins","parlay_losses"):
            state["stats"][user].setdefault(k, 0)

        if bet.get("bet_type") == "parlay":
            result, leg_results = grade_parlay(bet, player_stats, game_results)
            bet["result"]      = result
            bet["leg_results"] = leg_results
            bet["graded_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if result == "win":
                state["stats"][user]["parlay_wins"]   += 1
            elif result == "loss":
                state["stats"][user]["parlay_losses"] += 1
            icon = RESULT_ICON.get(result, "❓")
            print(f"  {icon}  PARLAY: {bet.get('description','')[:55]}  →  {result}")
            for lr in leg_results:
                li = RESULT_ICON.get(lr["result"], "❓")
                print(f"      {li}  {lr['description'][:50]}")
        else:
            result = grade_single_bet(bet, player_stats, game_results)
            bet["result"]      = result
            bet["graded_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if result == "win":
                state["stats"][user]["straight_wins"]   += 1
            elif result == "loss":
                state["stats"][user]["straight_losses"] += 1
            elif result == "push":
                state["stats"][user]["straight_pushes"] += 1
            icon = RESULT_ICON.get(result, "❓")
            print(f"  {icon}  {bet.get('description','')[:60]}  →  {result}")

        graded.append(bet)
        time.sleep(0.3)

    state["pending_bets"] = [b for b in state["pending_bets"] if b.get("date") != yesterday_str]
    state["graded_bets"].extend(graded)
    save_state(state)

    results_msg = format_daily_results(graded, yesterday_str)
    if results_msg:
        send_telegram(results_msg)
        print("  📨  Daily results sent")
        time.sleep(1)

    stats_msg = format_overall_stats(state["stats"], state.get("tracking_start", yesterday_str))
    if stats_msg:
        send_telegram(stats_msg)
        print("  📨  Overall stats sent")

    print(f"✅  Grader done — {len(graded)} bets graded")


if __name__ == "__main__":
    main()
