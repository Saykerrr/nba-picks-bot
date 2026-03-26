"""catchup.py v5 — Nuclear wipe, forces complete re-fetch from Reddit"""
import json, os
from datetime import datetime, timezone

def main():
    print(f"🔧 Nuclear catchup v5 — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    sf = "state.json"
    old = {}
    if os.path.exists(sf):
        with open(sf) as f: old = json.load(f)
    print(f"  Before: {len(old.get('graded_bets',[]))} graded, {len(old.get('pending_bets',[]))} pending")
    
    # Complete wipe — only preserve tracking_start
    state = {
        "pending_bets": [],
        "graded_bets": [],
        "seen_comments": {},
        "sent_per_post": {},
        "tracking_start": old.get("tracking_start", "2026-03-23"),
        "stats": {}
    }
    with open(sf, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  ✅ Wiped everything. Clean slate.")

if __name__ == "__main__":
    main()
