"""
CATCHUP SCRIPT — Moves all graded_bets back to pending_bets and resets stats.
Safe to run multiple times.  After running, grader.py re-grades everything.
"""

import json
import os
import sys
from datetime import datetime, timezone

STATE_FILE = "state.json"


def main():
    print(f"🔧  Catchup starting — "
          f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    if not os.path.exists(STATE_FILE):
        print("  ❌  No state.json found — nothing to do")
        return

    with open(STATE_FILE) as f:
        state = json.load(f)

    graded  = state.get("graded_bets", [])
    pending = state.get("pending_bets", [])
    stats   = state.get("stats", {})

    print(f"  📋  Graded: {len(graded)}  |  Pending: {len(pending)}")

    # ── Step 1: Move all graded bets back to pending ──
    moved = 0
    existing_ids = {b["id"] for b in pending}

    for bet in graded:
        bid = bet.get("id", "")
        if bid in existing_ids:
            continue

        bet.pop("result", None)
        bet.pop("leg_results", None)
        bet.pop("graded_date", None)
        bet["result"] = None

        if "sport" not in bet:
            bet["sport"] = "nba"

        pending.append(bet)
        existing_ids.add(bid)
        moved += 1

    print(f"  ✅  Moved {moved} bets back to pending")

    # ── Step 2: Strip any leftover results from pending bets ──
    for bet in pending:
        if bet.get("result") is not None:
            bet["result"] = None
            bet.pop("leg_results", None)
            bet.pop("graded_date", None)
        if "sport" not in bet:
            bet["sport"] = "nba"

    # ── Step 3: Reset all stats to zero ──
    for user in stats:
        stats[user] = {
            "straight_wins": 0, "straight_losses": 0,
            "straight_pushes": 0,
            "parlay_wins": 0, "parlay_losses": 0,
        }
    print(f"  ✅  Reset stats for {len(stats)} user(s)")

    # ── Step 4: Clear graded_bets ──
    state["graded_bets"]  = []
    state["pending_bets"] = pending
    state["stats"]        = stats

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    dates = sorted(set(b["date"] for b in pending))
    print(f"\n✅  Done — {len(pending)} pending bets | Dates: {', '.join(dates)}")


if __name__ == "__main__":
    main()
