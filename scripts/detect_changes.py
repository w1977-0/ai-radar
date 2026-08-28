#!/usr/bin/env python3
"""
detect_changes.py — Compare current pricing.json vs previous and record diffs.

Output: data/changelog.json (last 30 days of price changes)
        data/previous_pricing.json (snapshot of last run)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PRICING_FILE = DATA_DIR / "pricing.json"
PREV_PRICING = DATA_DIR / "previous_pricing.json"
CHANGELOG = DATA_DIR / "changelog.json"
KEEP_DAYS = 30


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with path.open() as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None


def main() -> int:
    current = load_json(PRICING_FILE)
    if not current:
        print("  ! no current pricing.json — skip")
        return 1

    previous = load_json(PREV_PRICING)
    changelog = load_json(CHANGELOG) or {"changes": []}

    if previous is None:
        # First run — record all current models as "added"
        for m in current.get("models", []):
            changelog["changes"].append({
                "timestamp": current["generated_at"],
                "model": m["id"],
                "kind": "added",
                "old_price": None,
                "new_price": m["pricing"]["completion_per_million"],
            })
    else:
        prev_by_id = {m["id"]: m for m in previous.get("models", [])}
        cur_by_id = {m["id"]: m for m in current.get("models", [])}
        ts = current["generated_at"]

        # Added
        for mid in cur_by_id:
            if mid not in prev_by_id:
                changelog["changes"].append({
                    "timestamp": ts, "model": mid, "kind": "added",
                    "old_price": None,
                    "new_price": cur_by_id[mid]["pricing"]["completion_per_million"],
                })
        # Removed
        for mid in prev_by_id:
            if mid not in cur_by_id:
                changelog["changes"].append({
                    "timestamp": ts, "model": mid, "kind": "removed",
                    "old_price": prev_by_id[mid]["pricing"]["completion_per_million"],
                    "new_price": None,
                })
        # Changed
        for mid in cur_by_id:
            if mid in prev_by_id:
                old_p = prev_by_id[mid]["pricing"]
                new_p = cur_by_id[mid]["pricing"]
                if old_p != new_p:
                    pct = None
                    if old_p["completion_per_million"]:
                        pct = round(100 * (new_p["completion_per_million"] - old_p["completion_per_million"]) / old_p["completion_per_million"], 2)
                    changelog["changes"].append({
                        "timestamp": ts, "model": mid, "kind": "changed",
                        "old_price": old_p["completion_per_million"],
                        "new_price": new_p["completion_per_million"],
                        "pct_change": pct,
                    })

    # Trim
    cutoff = datetime.now(timezone.utc).timestamp() - KEEP_DAYS * 86400
    changelog["changes"] = [
        c for c in changelog["changes"]
        if (c.get("timestamp") and
            datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00")).timestamp() >= cutoff)
    ]
    changelog["last_run"] = current["generated_at"]
    changelog["total_changes_tracked"] = len(changelog["changes"])

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CHANGELOG.open("w") as f:
        json.dump(changelog, f, indent=2, ensure_ascii=False)
    with PREV_PRICING.open("w") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)

    new_changes = sum(1 for c in changelog["changes"] if c.get("timestamp") == current["generated_at"])
    print(f"  ✓ detected {new_changes} changes this run; {len(changelog['changes'])} total in last {KEEP_DAYS} days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
