#!/usr/bin/env python3
"""
fetch_benchmarks.py — Pull AI model benchmarks from Artificial Analysis.

Artificial Analysis is the de-facto standard for independent LLM benchmarks:
- Intelligence Index (v4.1.1: 9 evals incl. GPQA, HLE, LCR, TerminalBench, CritPt, ...)
- Coding Index
- Output speed (median tokens/s)
- Time to first token
- Pricing (per-1M input/output/blended)

API: GET https://artificialanalysis.ai/api/v2/data/llms/models
Auth: x-api-key header (token in ~/.hermes/.env as AA_API_KEY)
Attribution: https://artificialanalysis.ai/  (required by their terms)
Rate limit: 1000 requests/day (free tier)

We fuzzy-match AA's model names against our 20 OpenRouter-tracked models and
emit only the matches. Fuzzy match uses 3 strategies in priority order:
1. Slug normalization: 'claude-fable-5' ↔ 'anthropic/claude-fable-5'
2. Name normalization: strip '(high|medium|low|xhigh|max)' tier suffixes
3. Substring containment (longer = stricter)

Output: data/benchmarks.json  (model_id -> {intelligence, coding, speed, ...})
        data/aa_models.json   (full AA list, for debugging)
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# macOS Python 3.14 cert handling
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl._create_unverified_context()

AA_API_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
ENV_PATH = Path.home() / ".hermes" / ".env"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PRICING_FILE = DATA_DIR / "pricing.json"
OUT_FILE = DATA_DIR / "benchmarks.json"
FULL_FILE = DATA_DIR / "aa_models.json"

# Tier suffixes we strip for matching (AA names like "GPT-5.6 Sol (high)")
TIER_SUFFIX_RE = re.compile(r"\s*\((high|medium|low|xhigh|max|min|reasoning|non-reasoning|fast|slow)\)\s*$", re.IGNORECASE)
# Variant suffixes like " with fallback", " (Non-reasoning)" etc.
NOISE_RE = re.compile(r"\s*\(?(with fallback|non-reasoning|reasoning|batch)\)?\s*$", re.IGNORECASE)


def get_api_key() -> str | None:
    """Read AA_API_KEY from ~/.hermes/.env without exposing it in output."""
    if not ENV_PATH.exists():
        return None
    with ENV_PATH.open() as f:
        for line in f:
            if line.startswith("AA_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def fetch_aa_models(api_key: str) -> list[dict]:
    """Fetch all AA models. Use curl (more reliable than Python 3.14 urllib)."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        r = subprocess.run(
            ["curl", "-sSL", "--max-time", "60",
             "-H", f"x-api-key: {api_key}",
             "-o", tmp_path, AA_API_URL],
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
    # Fallback to urllib
    req = urllib.request.Request(
        AA_API_URL, headers={"x-api-key": api_key, "User-Agent": "ai-radar/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
        cl = resp.headers.get("Content-Length")
        body = resp.read(int(cl)) if cl else resp.read()
    return json.loads(body).get("data", [])


def normalize_name(s: str) -> str:
    """Strip tier suffixes, lower-case, strip punctuation → comparable form."""
    if not s:
        return ""
    s = TIER_SUFFIX_RE.sub("", s)
    s = NOISE_RE.sub("", s)
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def match_score(aa_model: dict, or_id: str) -> float:
    """Return 0-1 confidence that an AA model matches an OpenRouter model id.

    or_id format: 'company/model-name' (e.g. 'anthropic/claude-fable-5')
    """
    aa_name = aa_model.get("name", "")
    aa_slug = aa_model.get("slug", "")
    or_model = or_id.split("/", 1)[1] if "/" in or_id else or_id  # 'claude-fable-5'

    aa_n = normalize_name(aa_name)
    aa_s = normalize_name(aa_slug)
    or_n = normalize_name(or_model)

    # Exact match on normalized forms
    if aa_n and aa_n == or_n:
        return 1.0
    if aa_s and aa_s == or_n:
        return 0.95

    # Containment (longer = better)
    if or_n in aa_n or aa_n in or_n:
        longer, shorter = (aa_n, or_n) if len(aa_n) > len(or_n) else (or_n, aa_n)
        if shorter in longer:
            # Penalize very different lengths to avoid false positives
            ratio = len(shorter) / len(longer) if longer else 0
            if ratio > 0.6:
                return 0.7

    # Same on slug
    if or_n in aa_s or aa_s in or_n:
        longer, shorter = (aa_s, or_n) if len(aa_s) > len(or_n) else (or_n, aa_s)
        if shorter in longer:
            ratio = len(shorter) / len(longer) if longer else 0
            if ratio > 0.6:
                return 0.6

    return 0.0


def extract_benchmark(eval_data: dict) -> dict[str, Any]:
    """Extract the fields we care about from AA's evaluations block."""
    return {
        "intelligence_index": eval_data.get("artificial_analysis_intelligence_index"),
        "coding_index": eval_data.get("artificial_analysis_coding_index"),
        "math_index": eval_data.get("artificial_analysis_math_index"),
        "gpqa": eval_data.get("gpqa"),
        "hle": eval_data.get("hle"),
        "lcr": eval_data.get("lcr"),
    }


def extract_pricing(pricing_data: dict) -> dict[str, Any]:
    return {
        "input_per_million": pricing_data.get("price_1m_input_tokens"),
        "output_per_million": pricing_data.get("price_1m_output_tokens"),
        "blended_3_to_1": pricing_data.get("price_1m_blended_3_to_1"),
    }


def extract_performance(m: dict) -> dict[str, Any]:
    return {
        "median_output_tokens_per_second": m.get("median_output_tokens_per_second"),
        "median_time_to_first_token_s": m.get("median_time_to_first_token_seconds"),
    }


def main() -> int:
    api_key = get_api_key()
    if not api_key:
        print("ERROR: AA_API_KEY not found in ~/.hermes/.env")
        print("  Run: echo 'AA_API_KEY=***  >> ~/.hermes/.env")
        return 1

    print("→ Fetching AA models...")
    aa_models = fetch_aa_models(api_key)
    print(f"  total AA models: {len(aa_models)}")

    if not PRICING_FILE.exists():
        print(f"  ! pricing.json not found, run fetch_pricing.py first")
        return 1
    pricing = json.loads(PRICING_FILE.read_text())
    or_models = pricing.get("models", [])
    print(f"  OpenRouter tracked models: {len(or_models)}")

    # Save full AA list for debugging
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with FULL_FILE.open("w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "https://artificialanalysis.ai/api/v2/data/llms/models",
            "attribution": "https://artificialanalysis.ai/",
            "total": len(aa_models),
            "models": [{"name": m.get("name"), "slug": m.get("slug"),
                        "creator": m.get("model_creator", {}).get("name")} for m in aa_models],
        }, f, ensure_ascii=False, indent=2)

    # Match each OpenRouter model to best AA model
    benchmarks: dict[str, dict] = {}
    match_log: list[dict] = []

    for om in or_models:
        or_id = om["id"]
        best: tuple[float, dict | None] = (0.0, None)
        for am in aa_models:
            score = match_score(am, or_id)
            if score > best[0]:
                best = (score, am)
        score, am = best
        if am and score >= 0.6:
            eval_data = am.get("evaluations", {}) or {}
            pricing_data = am.get("pricing", {}) or {}
            benchmarks[or_id] = {
                "aa_id": am.get("id"),
                "aa_name": am.get("name"),
                "aa_slug": am.get("slug"),
                "match_score": round(score, 2),
                "intelligence_index": eval_data.get("artificial_analysis_intelligence_index"),
                "coding_index": eval_data.get("artificial_analysis_coding_index"),
                "speed_tokens_per_sec": am.get("median_output_tokens_per_second"),
                "ttft_s": am.get("median_time_to_first_token_seconds"),
                "aa_input_per_million": pricing_data.get("price_1m_input_tokens"),
                "aa_output_per_million": pricing_data.get("price_1m_output_tokens"),
            }
            match_log.append({"or_id": or_id, "aa_name": am.get("name"), "score": round(score, 2)})
        else:
            match_log.append({"or_id": or_id, "aa_name": None, "score": round(score, 2)})

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Artificial Analysis (https://artificialanalysis.ai/)",
        "attribution": "https://artificialanalysis.ai/",
        "api": "https://artificialanalysis.ai/api/v2/data/llms/models",
        "matched": len(benchmarks),
        "unmatched": len(or_models) - len(benchmarks),
        "models": benchmarks,
    }

    with OUT_FILE.open("w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n  matched: {len(benchmarks)} / {len(or_models)}")
    for entry in match_log:
        flag = "✓" if entry["score"] >= 0.6 else "✗"
        print(f"  {flag} {entry['or_id']:<45} → {entry['aa_name']!r:<35} (score={entry['score']})")
    print(f"\n  ✓ saved {OUT_FILE}")
    print(f"  ✓ saved {FULL_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
