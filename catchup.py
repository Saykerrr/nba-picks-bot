"""
ONE-TIME CATCHUP SCRIPT — Run this once to fix the 3PM grading bug.

What it does:
  1. Moves ALL graded_bets back into pending_bets (strips result/leg_results/graded_date)
  2. Resets all user stats to zero
  3. Saves state.json

After this runs, grader.py will re-grade everything from scratch with the
fixed ESPN "3PT" parser that correctly reads "made-attempted" strings.

This script is run BEFORE grader.py in the catchup workflow.
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

    graded = state.get("graded_bets", [])
    pending = state.get("pending_bets", [])
    stats = state.get("stats", {})

    print(f"  📋  Found {len(graded)} graded bets to move back to pending")
    print(f"  📋  Found {len(pending)} existing pending bets")
    print(f"  📊  Current stats: {json.dumps(stats, indent=4)}")

    # ── Step 1: Move all graded bets back to pending ──
    # Strip grading fields so they look like fresh pending bets
    moved = 0
    existing_ids = {b["id"] for b in pending}

    for bet in graded:
        bid = bet.get("id", "")
        if bid in existing_ids:
            print(f"    ⏭️  Skipping duplicate: {bid}")
            continue

        # Strip grading results
        bet.pop("result", None)
        bet.pop("leg_results", None)
        bet.pop("graded_date", None)
        bet["result"] = None

        pending.append(bet)
        existing_ids.add(bid)
        moved += 1

    print(f"  ✅  Moved {moved} bets back to pending")

    # ── Step 2: Reset all stats to zero ──
    for user in stats:
        stats[user] = {
            "straight_wins": 0,
            "straight_losses": 0,
            "straight_pushes": 0,
            "parlay_wins": 0,
            "parlay_losses": 0,
        }
    print(f"  ✅  Reset stats to zero for {len(stats)} user(s)")

    # ── Step 3: Clear graded_bets ──
    state["graded_bets"] = []
    state["pending_bets"] = pending
    state["stats"] = stats

    # ── Step 4: Save ──
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    print(f"\n✅  Catchup done — {len(pending)} total pending bets ready for re-grading")
    print(f"    Now run grader.py to re-grade everything with the fixed ESPN parser.")


if __name__ == "__main__":
    main()
