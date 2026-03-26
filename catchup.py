"""
Nuclear Catchup v4.0 — Wipes ALL bet data so bot re-fetches from Reddit fresh.
"""
import json, os
from datetime import datetime, timezone

STATE_FILE = "state.json"
VERSION = "4.0"

def main():
    print(f"🔧  Nuclear catchup v{VERSION} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    print(f"  Before: {len(state.get('graded_bets',[]))} graded, "
          f"{len(state.get('pending_bets',[]))} pending, "
          f"{len(state.get('seen_comments',{}))} seen, "
          f"{len(state.get('sent_per_post',{}))} sent")

    tracking = state.get("tracking_start", "2026-03-23")
    stats = state.get("stats", {})
    for u in stats:
        stats[u] = {"straight_wins":0,"straight_losses":0,"straight_pushes":0,
                     "parlay_wins":0,"parlay_losses":0}

    state = {"pending_bets":[],"graded_bets":[],"seen_comments":{},"sent_per_post":{},
             "tracking_start":tracking,"stats":stats}

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  ✅  Wiped everything. Stats reset. tracking_start={tracking}")
    print(f"  Bot will re-fetch all comments from Reddit on next run.")

if __name__ == "__main__":
    main()
