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
6. Enforce: max 2 models per company (batch variants don't consume slots)
7. Preserve previous selection when possible (load from data/previous_selection.json)

Output: data/pricing.json
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import tempfile
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

# Curated whitelist of major AI model companies.
COMPANY_WHITELIST = {
    "openai", "anthropic", "google", "xai",
    "deepseek", "mistralai",
    "alibaba", "qwen", "zhipu", "moonshotai", "minimax", "stepfun",
    "meta-llama",
    "nvidia", "ibm-granite", "perplexity",
}

# Suffixes that mark preview/beta/experimental versions — excluded from selection.
EXCLUDE_SUFFIXES = ("-preview", "-beta", "-exp", ":preview", ":beta")

# Substrings that mark non-chat model categories — excluded.
EXCLUDE_CATEGORIES = (
    "embed", "dall-e", "moderation", "guard", "whisper",
    "tts", "imagen", "sora",
)


def fetch_models() -> list[dict]:
    """Fetch all models from OpenRouter. Uses curl (more reliable than Python 3.14
    urllib against large chunked responses) and falls back to urllib on failure."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        r = subprocess.run(
            ["curl", "-sSL", "--max-time", "60", "-o", tmp_path, OPENROUTER_URL],
            capture_output=True, text=True, timeout=70
        )
        if r.returncode == 0 and os.path.getsize(tmp_path) > 1000:
            with open(tmp_path) as f:
                data = json.load(f)
            return data.get("data", [])
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    # Fallback
    req = urllib.request.Request(
        OPENROUTER_URL,
        headers={"User-Agent": "ai-radar/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
        cl = resp.headers.get("Content-Length")
        body = resp.read(int(cl)) if cl else resp.read()
    data = json.loads(body)
    return data.get("data", [])


def extract_company(model_id: str) -> str:
    if "/" in model_id:
        return model_id.split("/")[0]
    return "unknown"


def tier_of(pricing: dict, model_id: str = "") -> str | None:
    """Determine tier from output pricing.

    Batch variants (':' suffix) are downgraded one tier since they are typically
    50% cheaper than the real market price.
    """
    try:
        completion = float(pricing.get("completion", "0"))
        prompt = float(pricing.get("prompt", "0"))
    except (ValueError, TypeError):
        return None
    if completion <= 0 or prompt <= 0:
        return None
    is_batch = ":" in model_id
    output_per_million = completion * 1_000_000
    for tier, (low, high) in TIER_BANDS.items():
        if low <= output_per_million < high:
            if is_batch and tier in ("S", "A"):
                return {"S": "A", "A": "B"}.get(tier, tier)
            return tier
    return None


def is_excluded(m: dict) -> bool:
    """Return True if model should be filtered out."""
    mid = m.get("id", "")
    if not mid:
        return True
    if mid.startswith("~"):
        return True
    ml = mid.lower()
    if any(kw in ml for kw in EXCLUDE_CATEGORIES):
        return True
    if any(s in ml for s in EXCLUDE_SUFFIXES):
        return True
    company = extract_company(mid)
    if company not in COMPANY_WHITELIST:
        return True
    p = m.get("pricing") or {}
    try:
        prompt = float(p.get("prompt", "0"))
        completion = float(p.get("completion", "0"))
    except (ValueError, TypeError):
        return True
    if prompt <= 0 or completion <= 0:
        return True
    return False


def filter_chat_models(models: list[dict]) -> list[dict]:
    return [m for m in models if not is_excluded(m)]


def group_by_tier(models: list[dict]) -> dict[str, list[dict]]:
    by_tier: dict[str, list[dict]] = {"S": [], "A": [], "B": [], "C": []}
    for m in models:
        p = m.get("pricing") or {}
        t = tier_of(p, m.get("id", ""))
        if t:
            m["_tier"] = t
            m["_company"] = extract_company(m.get("id", ""))
            by_tier[t].append(m)
    # Sort: S by completion price desc (most expensive first), others asc (cheapest first)
    for tier in by_tier:
        if tier == "S":
            by_tier[tier].sort(key=lambda m: float(m["pricing"]["completion"]), reverse=True)
        else:
            by_tier[tier].sort(key=lambda m: float(m["pricing"]["completion"]))
    return by_tier


def select_models(by_tier: dict, prev_ids: set[str]) -> list[dict]:
    """Select up to TOTAL_QUOTA models respecting tier quotas + max-per-company.

    Batch variants (':' suffix) are exempt from the per-company limit.
    """
    company_count: dict[str, int] = {}
    selected: list[dict] = []

    def can_take(m: dict) -> bool:
        is_batch = ":" in m["id"]
        comp = m["_company"]
        if not is_batch and company_count.get(comp, 0) >= MAX_PER_COMPANY:
            return False
        return True

    def take(m: dict) -> None:
        selected.append(m)
        if ":" not in m["id"]:
            comp = m["_company"]
            company_count[comp] = company_count.get(comp, 0) + 1

    # First pass: keep previous selections
    if prev_ids:
        for tier_models in by_tier.values():
            for m in tier_models:
                if m["id"] in prev_ids and m["id"] not in [s["id"] for s in selected]:
                    if can_take(m):
                        take(m)

    # Second pass: fill tier quotas
    for tier, quota in TIER_QUOTAS.items():
        tier_selected = [s for s in selected if s.get("_tier") == tier]
        for m in by_tier[tier]:
            if len(tier_selected) >= quota:
                break
            if m["id"] in [s["id"] for s in selected]:
                continue
            if can_take(m):
                take(m)
                tier_selected.append(m)

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
        print(f"  ⚠ only {len(selected)} models selected (target {TOTAL_QUOTA}) — see tier breakdown")
        # Report per-tier to help diagnose
        for tier, models in by_tier.items():
            selected_in_tier = [m for m in selected if m.get("_tier") == tier]
            if len(selected_in_tier) < TIER_QUOTAS[tier]:
                print(f"    tier {tier}: {len(selected_in_tier)}/{TIER_QUOTAS[tier]} (no more available without violating limits)")

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
