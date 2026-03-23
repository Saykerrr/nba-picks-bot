import requests
import json
import os
import sys
import time
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
    return {"seen": {}, "pending_bets": [], "graded_bets": [], "stats": {}}


def save_state(state):
    if len(state.get("graded_bets", [])) > 500:
        state["graded_bets"] = state["graded_bets"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Helpers ───────────────────────────────────────────────────────────────────
def escape_html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }, timeout=15)
    if not resp.ok:
        print(f"Telegram error: {resp.text}", file=sys.stderr)


# ── ESPN API ──────────────────────────────────────────────────────────────────
def get_espn_scoreboard(date_str):
    """Fetch NBA scoreboard. date_str = YYYYMMDD."""
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
    """Return dict: player_name_lower -> {name, team, stats}"""
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
                    # Normalise common keys
                    s = {
                        "PTS": raw_stats.get("PTS", raw_stats.get("points", 0)),
                        "REB": raw_stats.get("REB", raw_stats.get("rebounds", 0)),
                        "AST": raw_stats.get("AST", raw_stats.get("assists", 0)),
                        "3PM": raw_stats.get("3PM", raw_stats.get("threePointFieldGoalsMade", 0)),
                        "BLK": raw_stats.get("BLK", raw_stats.get("blocks", 0)),
                        "STL": raw_stats.get("STL", raw_stats.get("steals", 0)),
                        "MIN": raw_stats.get("MIN", 0),
                    }
                    try:
                        s["PTS"] = float(s["PTS"] or 0)
                        s["REB"] = float(s["REB"] or 0)
                        s["AST"] = float(s["AST"] or 0)
                    except (ValueError, TypeError):
                        pass
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
        hs = int(home.get("score", 0) or 0)
        as_ = int(away.get("score", 0) or 0)
        ht = home.get("team", {}).get("abbreviation", "")
        at = away.get("team", {}).get("abbreviation", "")
        results.append({
            "home": ht, "away": at,
            "home_score": hs, "away_score": as_,
            "total": hs + as_,
            "winner": ht if hs > as_ else at,
            "status": game.get("status", {}).get("type", {}).get("description", ""),
        })
    return results


# ── Grading Logic ─────────────────────────────────────────────────────────────
def grade_bet(bet, player_stats, game_results):
    """Grade a bet deterministically where possible, else fall back to Claude."""
    btype     = bet.get("bet_type", "other")
    direction = (bet.get("direction") or "").lower()
    line      = bet.get("line")
    player    = (bet.get("player") or "").lower()
    stat_cat  = (bet.get("stat") or "").upper()
    team      = (bet.get("team") or "").upper()
    opponent  = (bet.get("opponent") or "").upper()

    # ── Player Prop ──────────────────────────────────────────────────────────
    if btype == "player_prop" and player and stat_cat and line is not None:
        pdata = None
        for pname, pd in player_stats.items():
            parts = [p for p in player.split() if len(p) > 2]
            if parts and all(p in pname for p in parts):
                pdata = pd
                break
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

    # ── Game Total ────────────────────────────────────────────────────────────
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

    # ── Moneyline ─────────────────────────────────────────────────────────────
    elif btype == "moneyline" and team:
        for g in game_results:
            if team in (g["home"], g["away"]):
                return "win" if g["winner"] == team else "loss"

    # ── Spread ────────────────────────────────────────────────────────────────
    elif btype == "spread" and team and line is not None:
        for g in game_results:
            if team in (g["home"], g["away"]):
                try:
                    line = float(line)
                    if g["home"] == team:
                        diff = g["home_score"] + line - g["away_score"]
                    else:
                        diff = g["away_score"] + line - g["home_score"]
                    if diff > 0:
                        return "win"
                    elif diff < 0:
                        return "loss"
                    else:
                        return "push"
                except (ValueError, TypeError):
                    pass

    # ── Claude fallback ───────────────────────────────────────────────────────
    return grade_with_claude(bet, player_stats, game_results)


def grade_with_claude(bet, player_stats, game_results):
    if not ANTHROPIC_API_KEY:
        return "unknown"

    # Build a tight relevant-player snapshot
    relevant = {}
    player = (bet.get("player") or "").lower()
    if player:
        for pname, pd in player_stats.items():
            parts = [p for p in player.split() if len(p) > 2]
            if parts and all(p in pname for p in parts):
                relevant[pd["name"]] = pd["stats"]

    prompt = f"""Grade this NBA bet using the data provided.

BET: {bet.get('description', 'N/A')}
Type: {bet.get('bet_type')} | Player: {bet.get('player')} | Stat: {bet.get('stat')} | Line: {bet.get('line')} | Direction: {bet.get('direction')}

GAME RESULTS:
{json.dumps(game_results, indent=2)}

PLAYER STATS:
{json.dumps(relevant, indent=2)}

Reply with ONLY one word: win, loss, push, or unknown.
Use "unknown" if the player didn't play, game wasn't played, or data is insufficient."""

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
                "max_tokens": 5,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        if resp.ok:
            result = resp.json()["content"][0]["text"].strip().lower()
            if result in ("win", "loss", "push", "unknown"):
                return result
    except Exception as e:
        print(f"Claude grade error: {e}", file=sys.stderr)

    return "unknown"


# ── Message Formatting ────────────────────────────────────────────────────────
RESULT_ICON = {"win": "✅", "loss": "❌", "push": "➡️", "unknown": "❓"}


def format_daily_results(graded, date_str):
    if not graded:
        return None

    by_user = {}
    for bet in graded:
        by_user.setdefault(bet["user"], []).append(bet)

    lines = [f"📊 <b>Bet Results — {date_str}</b>", "━" * 30]

    for user, bets in by_user.items():
        w = sum(1 for b in bets if b["result"] == "win")
        l = sum(1 for b in bets if b["result"] == "loss")
        p = sum(1 for b in bets if b["result"] == "push")
        u = sum(1 for b in bets if b["result"] == "unknown")

        record = f"{w}W / {l}L"
        if p: record += f" / {p}P"

        lines.append(f"\n💬 <b>u/{user}</b>  ·  {record}")
        for bet in bets:
            icon = RESULT_ICON.get(bet["result"], "❓")
            desc = escape_html(bet.get("description", "Unknown bet"))
            conf = bet.get("confidence")
            conf_tag = f"  <i>({conf})</i>" if conf else ""
            lines.append(f"  {icon} {desc}{conf_tag}")
        if u:
            lines.append(f"\n  ❓ {u} bet(s) could not be graded automatically")

    return "\n".join(lines)


def format_overall_stats(stats, start_date):
    if not stats:
        return None

    lines = [
        f"📈 <b>Overall Record</b>  <i>(tracking since {start_date})</i>",
        "━" * 30,
    ]

    for user, rec in stats.items():
        w = rec.get("wins", 0)
        l = rec.get("losses", 0)
        p = rec.get("pushes", 0)
        total = w + l
        rate  = f"{round(w / total * 100, 1)}%" if total > 0 else "—"

        lines.append(f"\n💬 <b>u/{user}</b>")
        lines.append(f"  ✅  Wins:      <b>{w}</b>")
        lines.append(f"  ❌  Losses:    <b>{l}</b>")
        if p:
            lines.append(f"  ➡️  Pushes:    <b>{p}</b>")
        lines.append(f"  📊  Hit Rate:  <b>{rate}</b>  ({total} graded bets)")

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

    # Grade yesterday (grader runs at 8 AM UTC — all NBA games are finished)
    yesterday_dt  = datetime.now(timezone.utc) - timedelta(days=1)
    yesterday_str = yesterday_dt.strftime("%Y-%m-%d")
    yesterday_espn= yesterday_dt.strftime("%Y%m%d")

    # Record first tracking date
    if "tracking_start" not in state:
        state["tracking_start"] = yesterday_str

    pending = [b for b in state["pending_bets"] if b.get("date") == yesterday_str]

    if not pending:
        print(f"No pending bets for {yesterday_str}")
        # Still send stats recap if we have data
        if any(v.get("wins", 0) + v.get("losses", 0) > 0 for v in state["stats"].values()):
            msg = format_overall_stats(state["stats"], state.get("tracking_start", yesterday_str))
            if msg:
                send_telegram(msg)
        save_state(state)
        return

    print(f"  📋  {len(pending)} bets to grade for {yesterday_str}")

    # Fetch ESPN data
    games = get_espn_scoreboard(yesterday_espn)
    print(f"  🏀  {len(games)} games found on ESPN")

    if not games:
        print("  ⚠️  No ESPN data — skipping grading (will retry tomorrow)")
        save_state(state)
        return

    player_stats = build_player_stats(games)
    game_results = build_game_results(games)
    print(f"  👤  {len(player_stats)} players loaded, {len(game_results)} games")

    # Grade
    graded = []
    for bet in pending:
        result = grade_bet(bet, player_stats, game_results)
        bet["result"]       = result
        bet["graded_date"]  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        graded.append(bet)

        user = bet["user"]
        if user not in state["stats"]:
            state["stats"][user] = {"wins": 0, "losses": 0, "pushes": 0}
        if result == "win":
            state["stats"][user]["wins"]   += 1
        elif result == "loss":
            state["stats"][user]["losses"] += 1
        elif result == "push":
            state["stats"][user]["pushes"] += 1

        icon = RESULT_ICON.get(result, "❓")
        print(f"  {icon}  {bet.get('description', '?')[:55]}  →  {result}")
        time.sleep(0.3)

    # Move from pending → graded
    state["pending_bets"] = [b for b in state["pending_bets"] if b.get("date") != yesterday_str]
    state["graded_bets"].extend(graded)
    save_state(state)

    # ── Send Telegram messages ────────────────────────────────────────────────
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
