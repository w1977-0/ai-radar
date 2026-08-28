#!/usr/bin/env python3
"""
fetch_pricing.py — Pull AI model pricing from OpenRouter.

Pipeline:
1. GET https://openrouter.ai/api/v1/models  (no auth needed)
2. Filter out non-chat models (embedding/image/moderation)
3. Group by company (extracted from model ID prefix)
4. Apply 4-tier pricing bands based on output $/1M tokens
5. Select 20 models:
   - 4 from Tier S (>$30/M output)
   - 6 from Tier A ($10-30/M)
   - 6 from Tier B ($1-10/M)
   - 4 from Tier C (<$1/M)
6. Enforce: max 2 models per company
7. Preserve previous selection when possible (load from data/previous_selection.json)

Output: data/pricing.json
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# macOS Python 3.14 lacks system certs by default.
# Use certifi if available; otherwise fall back to unverified context.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl._create_unverified_context()

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PREV_SELECTION = DATA_DIR / "previous_selection.json"
OUT_FILE = DATA_DIR / "pricing.json"

TIER_BANDS = {
    "S": (30.0, float("inf")),    # super-flagship
    "A": (10.0, 30.0),            # flagship
    "B": (1.0, 10.0),             # main
    "C": (0.0, 1.0),              # economy
}
TIER_QUOTAS = {"S": 4, "A": 6, "B": 6, "C": 4}
MAX_PER_COMPANY = 2
TOTAL_QUOTA = 20


def fetch_models() -> list[dict]:
    req = urllib.request.Request(
        OPENROUTER_URL,
        headers={"User-Agent": "ai-radar/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
        # Read all chunks to avoid IncompleteRead on chunked transfer
        chunks = []
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
        body = b"".join(chunks)
    data = json.loads(body)
    return data.get("data", [])


def extract_company(model_id: str) -> str:
    """Extract company from model ID like 'openai/gpt-5.6' -> 'openai'."""
    if "/" in model_id:
        return model_id.split("/")[0]
    return "unknown"


def tier_of(pricing: dict) -> str | None:
    """Determine tier from output pricing. Returns None if not applicable."""
    try:
        completion = float(pricing.get("completion", "0"))
        prompt = float(pricing.get("prompt", "0"))
    except (ValueError, TypeError):
        return None
    if completion <= 0 or prompt <= 0:
        return None
    output_per_million = completion * 1_000_000
    for tier, (low, high) in TIER_BANDS.items():
        if low <= output_per_million < high:
            return tier
    return None


def filter_chat_models(models: list[dict]) -> list[dict]:
    """Keep only chat-capable models with non-zero prompt+completion pricing."""
    out = []
    for m in models:
        p = m.get("pricing") or {}
        try:
            prompt = float(p.get("prompt", "0"))
            completion = float(p.get("completion", "0"))
        except (ValueError, TypeError):
            continue
        if prompt <= 0 or completion <= 0:
            continue  # skip free models
        # Skip image/embedding/moderation/guard
        mid = m.get("id", "").lower()
        if any(kw in mid for kw in ["embed", "dall-e", "moderation", "guard", "whisper", "tts", "imagen", "sora"]):
            continue
        out.append(m)
    return out


def group_by_tier(models: list[dict]) -> dict[str, list[dict]]:
    by_tier: dict[str, list[dict]] = {"S": [], "A": [], "B": [], "C": []}
    for m in models:
        p = m.get("pricing") or {}
        t = tier_of(p)
        if t:
            m["_tier"] = t
            m["_company"] = extract_company(m.get("id", ""))
            by_tier[t].append(m)
    # Sort each tier by prompt price (proxy for "importance" — lower isn't always better,
    # but ordering by price gives reproducible selection)
    for tier in by_tier:
        by_tier[tier].sort(key=lambda m: float(m["pricing"]["prompt"]))
    return by_tier


def select_models(by_tier: dict, prev_ids: set[str]) -> list[dict]:
    """Select up to TOTAL_QUOTA models respecting tier quotas + max-per-company."""
    company_count: dict[str, int] = {}
    selected: list[dict] = []

    # First pass: keep previous selections if they still qualify
    if prev_ids:
        for tier_models in by_tier.values():
            for m in tier_models:
                if m["id"] in prev_ids and m["id"] not in [s["id"] for s in selected]:
                    comp = m["_company"]
                    if company_count.get(comp, 0) >= MAX_PER_COMPANY:
                        continue
                    selected.append(m)
                    company_count[comp] = company_count.get(comp, 0) + 1

    # Second pass: fill tier quotas
    for tier, quota in TIER_QUOTAS.items():
        tier_selected = [s for s in selected if s.get("_tier") == tier]
        for m in by_tier[tier]:
            if len(tier_selected) >= quota:
                break
            if m["id"] in [s["id"] for s in selected]:
                continue
            comp = m["_company"]
            if company_count.get(comp, 0) >= MAX_PER_COMPANY:
                continue
            selected.append(m)
            tier_selected.append(m)
            company_count[comp] = company_count.get(comp, 0) + 1

    return selected


def load_previous_selection() -> set[str]:
    if not PREV_SELECTION.exists():
        return set()
    try:
        with PREV_SELECTION.open() as f:
            data = json.load(f)
        return {m["id"] for m in data.get("selected", [])}
    except (json.JSONDecodeError, KeyError):
        return set()


def save_previous_selection(selected: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with PREV_SELECTION.open("w") as f:
        json.dump({"selected": [{"id": m["id"], "tier": m.get("_tier")} for m in selected]}, f, indent=2)


def main() -> int:
    print("→ Fetching OpenRouter models...")
    raw = fetch_models()
    print(f"  total models: {len(raw)}")

    chat = filter_chat_models(raw)
    print(f"  chat-capable: {len(chat)}")

    by_tier = group_by_tier(chat)
    for tier, models in by_tier.items():
        print(f"  tier {tier}: {len(models)}")

    prev_ids = load_previous_selection()
    print(f"  previous selection: {len(prev_ids)} models")

    selected = select_models(by_tier, prev_ids)
    if len(selected) < TOTAL_QUOTA:
        print(f"  ⚠ only {len(selected)} models selected (target {TOTAL_QUOTA})")

    # Build output
    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "OpenRouter /api/v1/models",
        "tier_bands_usd_per_million_output": TIER_BANDS,
        "tier_quotas": TIER_QUOTAS,
        "max_per_company": MAX_PER_COMPANY,
        "models": [
            {
                "id": m["id"],
                "name": m.get("name", m["id"]),
                "company": m.get("_company"),
                "tier": m.get("_tier"),
                "context_length": m.get("context_length"),
                "pricing": {
                    "prompt_per_million": float(m["pricing"]["prompt"]) * 1_000_000,
                    "completion_per_million": float(m["pricing"]["completion"]) * 1_000_000,
                    "request": float(m["pricing"].get("request", 0)) if m["pricing"].get("request") else 0,
                    "image": float(m["pricing"].get("image", 0)) if m["pricing"].get("image") else 0,
                },
                "previously_selected": m["id"] in prev_ids,
            }
            for m in selected
        ],
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_FILE.open("w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    save_previous_selection(selected)
    print(f"  ✓ saved {len(selected)} models to {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
