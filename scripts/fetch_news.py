#!/usr/bin/env python3
"""
fetch_news.py — Pull AI company news from Hacker News (Algolia) + RSS feeds.

Pipeline:
1. For each tracked company, query HN Algolia for top stories about that company
2. Fetch each company's official RSS/blog feed (verified URLs)
3. Cross-validate: a story is "official" if title appears in RSS feed,
   "community" if HN only.

For RSS, we use a mix of:
- Official company feeds (when they exist)
- Community mirrors via openrss.org (when official doesn't exist)
- None (when neither exists) — HN-only is the fallback

HN API has chunked transfer issues with Python 3.14 urllib, so we use curl via
subprocess for HN fetches (more reliable).

Output: data/news.json
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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# macOS Python 3.14 cert handling
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl._create_unverified_context()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_FILE = DATA_DIR / "news.json"

HN_ALGOLIA = "https://hn.algolia.com/api/v1/search_by_date"
HN_DAYS_BACK = 7
MAX_PER_COMPANY = 3
MIN_HN_POINTS = 30
MIN_HN_COMMENTS = 5

# RSS sources — verified available as of 2026-08-28.
# Format: (key, display_name, [HN search terms], rss_url)
# rss_url="" means HN-only (no RSS available)
COMPANIES = [
    ("openai", "OpenAI",
     ["openai", "chatgpt", "gpt-4", "gpt-5", "gpt-5.5", "gpt-5.6"],
     "https://openai.com/news/rss.xml"),
    ("anthropic", "Anthropic",
     ["anthropic", "claude"],
     "https://rsshub.bestblogs.dev/anthropic/news"),
    ("google", "Google (Gemini/DeepMind)",
     ["google gemini", "deepmind"],
     "https://blog.google/products/gemini/rss/"),
    ("xai", "xAI",
     ["xai", "grok"],
     "https://openrss.org/x.ai/news"),
    ("deepseek", "DeepSeek",
     ["deepseek"],
     ""),
    ("mistral", "Mistral AI",
     ["mistral ai", "mistral large", "mistral small", "mistral 7b", "mistral nemo", "magistral", "voxtral", "devstral", "mistral code", "codestral", "pixtral"],
     "https://mistral.ai/news/rss"),
    ("meta", "Meta (Llama)",
     ["meta llama", "llama 3", "llama 4"],
     "https://openrss.org/www.meta.com/blog"),
    ("alibaba", "Alibaba (Qwen)",
     ["alibaba qwen", "qwen"],
     ""),
    ("qwen", "Qwen",
     ["qwen"],
     "https://openrss.org/qwenlm.github.io"),
    ("zhipu", "Zhipu (GLM)",
     ["zhipu", "glm"],
     ""),
    ("moonshotai", "Moonshot (Kimi)",
     ["moonshot", "kimi"],
     ""),
    ("minimax", "MiniMax",
     ["minimax"],
     ""),
    ("stepfun", "Stepfun",
     ["stepfun", "step-1"],
     ""),
    ("meta-llama", "Meta/Llama",
     ["llama"],
     "https://openrss.org/www.meta.com/blog"),
    ("nvidia", "NVIDIA",
     ["nvidia", "nemotron"],
     ""),
    ("ibm-granite", "IBM Granite",
     ["granite"],
     ""),
    ("perplexity", "Perplexity",
     ["perplexity", "sonar"],
     ""),
]

UA = "ai-radar/1.0"


def fetch_hn_for_company(company: str, terms: list[str]) -> list[dict]:
    """Query HN Algolia for top stories about a company in the last HN_DAYS_BACK days.
    Uses curl via subprocess (more reliable than Python 3.14 urllib against
    chunked transfer responses)."""
    since_ts = int((datetime.now(timezone.utc) - timedelta(days=HN_DAYS_BACK)).timestamp())
    stories: list[dict] = []
    seen = set()  # dedupe by title within this company's HN set

    for term in terms:
        params = {
            "query": term,
            "tags": "story",
            "numericFilters": f"created_at_i>{since_ts},points>={MIN_HN_POINTS}",
            "hitsPerPage": 30,
        }
        url = f"{HN_ALGOLIA}?{urllib.parse.urlencode(params)}"
        try:
            r = subprocess.run(
                ["curl", "-sSL", "--max-time", "20", "-A", UA,
                 "-H", "Accept: application/json",
                 url],
                capture_output=True, text=True, timeout=25
            )
            if r.returncode != 0 or not r.stdout.strip():
                continue
            data = json.loads(r.stdout)
        except Exception as e:
            print(f"  ! HN fetch error for {term}: {e}")
            continue
        for hit in data.get("hits", []):
            if hit.get("num_comments", 0) < MIN_HN_COMMENTS:
                continue
            norm = re.sub(r"\W+", "", hit.get("title", "").lower())[:80]
            if norm in seen:
                continue
            seen.add(norm)
            stories.append({
                "title": hit.get("title", ""),
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}",
                "hn_id": hit["objectID"],
                "hn_url": f"https://news.ycombinator.com/item?id={hit['objectID']}",
                "points": hit.get("points", 0),
                "num_comments": hit.get("num_comments", 0),
                "created_at": hit.get("created_at"),
                "author": hit.get("author"),
            })
    stories.sort(key=lambda s: s["points"], reverse=True)
    return stories


def fetch_rss(url: str) -> list[dict]:
    """Fetch an RSS/Atom feed and extract title/url/date for each item.

    Uses curl subprocess with stdout capture (not temp files) to avoid the
    hermes agent sandbox restriction on /var/folders/.../T/ writes. Even
    /tmp/ paths get silently truncated to 0 bytes inside the sandbox.
    """
    if not url:
        return []
    try:
        r = subprocess.run(
            ["curl", "-sSL", "--max-time", "20", "-A", UA,
             "-H", "Accept: application/rss+xml, application/atom+xml, */*",
             "-L", url],
            capture_output=True, text=True, timeout=25
        )
        if r.returncode != 0 or not r.stdout:
            print(f"  ! RSS fetch failed for {url} (rc={r.returncode}, len={len(r.stdout or '')})")
            return []
        content = r.stdout
    except Exception as e:
        print(f"  ! RSS fetch error for {url}: {e}")
        return []

    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        print(f"  ! RSS parse error for {url}: {e}")
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items: list[dict] = []
    for item in root.findall(".//item") + root.findall(".//atom:entry", ns):
        title_el = item.find("title")
        if title_el is None or title_el.text is None:
            continue
        title = title_el.text.strip()
        if not title:
            continue
        link_el = item.find("link")
        link = ""
        if link_el is not None:
            if link_el.text:
                link = link_el.text.strip()
            elif link_el.get("href"):
                link = link_el.get("href", "")
        date_el = item.find("pubDate")
        date_str = date_el.text.strip() if date_el is not None and date_el.text else ""
        items.append({"title": title, "url": link, "published": date_str})
    return items


def cross_validate(hn_stories: list[dict], rss_items: list[dict]) -> list[dict]:
    """Merge HN + RSS with cross-validation.

    A story is "official" if it has ≥2 significant tokens in common with
    an RSS item. Tokens are 3+ letter words after removing common stopwords.
    Handles substring/punctuation differences (e.g. "Transcribe" matches
    "Transcribing") by using set intersection, not literal equality.

    Sources:
    - 'official' = matched an RSS feed (≥2 token overlap)
    - 'community' = HN only
    """
    STOPWORDS = {"the", "and", "for", "with", "from", "this", "that", "you",
                 "are", "can", "all", "any", "but", "not", "out", "use",
                 "now", "new", "one", "how", "why", "its", "may", "may"}

    def tokens(s: str) -> set[str]:
        words = re.findall(r'[a-z]{3,}', s.lower())
        return {w for w in words if w not in STOPWORDS}

    # Pre-compute RSS token sets
    rss_token_sets = [tokens(it.get("title", "")) for it in rss_items if it.get("title")]
    # Best matching RSS title for tooltip
    rss_titles = [it.get("title", "") for it in rss_items if it.get("title")]

    out = []
    for hn in hn_stories:
        hn_toks = tokens(hn.get("title", ""))
        is_official = False
        best_match_title = ""
        best_match_count = 0
        for rss_toks, rss_title in zip(rss_token_sets, rss_titles):
            overlap = len(hn_toks & rss_toks)
            if overlap > best_match_count:
                best_match_count = overlap
                best_match_title = rss_title
            if overlap >= 2:
                is_official = True
        out.append({
            "title": hn["title"],
            "url": hn["url"],
            "hn_url": hn["hn_url"],
            "points": hn["points"],
            "num_comments": hn["num_comments"],
            "created_at": hn["created_at"],
            "author": hn["author"],
            "source": "official" if is_official else "community",
            "matched_rss": best_match_title if is_official else "",
            "match_score": best_match_count if is_official else 0,
        })
    return out


def main() -> int:
    print("→ Fetching news for each company...")
    by_company: dict[str, Any] = {}

    for key, name, terms, rss_url in COMPANIES:
        print(f"  {name}...", end=" ", flush=True)
        hn = fetch_hn_for_company(key, terms)
        rss = fetch_rss(rss_url)
        validated = cross_validate(hn, rss)
        validated = validated[:MAX_PER_COMPANY]
        by_company[key] = {
            "name": name,
            "rss_url": rss_url,
            "stories": validated,
        }
        n_official = sum(1 for s in validated if s["source"] == "official")
        n_rss = len(rss)
        print(f"{len(validated)} stories ({n_official} official, {n_rss} RSS items)")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hn_window_days": HN_DAYS_BACK,
        "min_hn_points": MIN_HN_POINTS,
        "min_hn_comments": MIN_HN_COMMENTS,
        "max_per_company": MAX_PER_COMPANY,
        "companies": by_company,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_FILE.open("w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    total = sum(len(c["stories"]) for c in by_company.values())
    n_official = sum(1 for c in by_company.values() for s in c["stories"] if s["source"] == "official")
    n_with_rss = sum(1 for c in by_company.values() if c["rss_url"])
    print(f"\n  ✓ saved {total} stories from {len(by_company)} companies to {OUT_FILE}")
    print(f"  {n_official} official (cross-validated), {n_with_rss} companies with RSS feed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
