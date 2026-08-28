#!/usr/bin/env python3
"""
fetch_news.py — Pull AI company news from Hacker News (Algolia) + RSS feeds.

Pipeline:
1. For each tracked company, query HN Algolia for top stories about that company
2. Fetch each company's official RSS/blog feed
3. Cross-validate: a story is "confirmed" if it appears in >=2 sources OR is from official RSS
4. For each company, keep top 3 stories by points/recency

Tracked companies: openai, anthropic, google (gemini/deepmind), xai, deepseek,
                    mistral, meta (llama), alibaba (qwen), zhipu (glm), moonshot (kimi),
                    moonshotai, minimax, stepfun

Output: data/news.json
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import sys
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
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

# (key, display name, search terms on HN, RSS URL)
COMPANIES = [
    ("openai", "OpenAI", ["openai", "chatgpt", "gpt-4", "gpt-5"], "https://openai.com/blog/rss.xml"),
    ("anthropic", "Anthropic", ["anthropic", "claude"], "https://www.anthropic.com/news/rss.xml"),
    ("google", "Google (Gemini/DeepMind)", ["google gemini", "deepmind"], "https://blog.google/technology/ai/rss/"),
    ("xai", "xAI", ["xai", "grok"], "https://x.ai/blog/rss.xml"),
    ("deepseek", "DeepSeek", ["deepseek"], "https://api-docs.deepseek.com/news.rss"),
    ("mistral", "Mistral AI", ["mistral"], "https://mistral.ai/news/rss.xml"),
    ("meta", "Meta (Llama)", ["meta llama", "llama 3", "llama 4"], "https://ai.meta.com/blog/rss/"),
    ("alibaba", "Alibaba (Qwen)", ["alibaba qwen", "qwen"], "https://qwenlm.github.io/feed.xml"),
    ("zhipu", "Zhipu (GLM)", ["zhipu", "glm"], ""),
    ("moonshot", "Moonshot (Kimi)", ["moonshot", "kimi"], ""),
    ("minimax", "MiniMax", ["minimax"], ""),
    ("stepfun", "Stepfun", ["stepfun", "step-1"], ""),
]

UA = "ai-radar/1.0"


def fetch_hn_for_company(company: str, terms: list[str]) -> list[dict]:
    """Query HN Algolia for top stories about a company in the last HN_DAYS_BACK days."""
    since_ts = int((datetime.now(timezone.utc) - timedelta(days=HN_DAYS_BACK)).timestamp())
    stories: list[dict] = []
    for term in terms:
        params = {
            "query": term,
            "tags": "story",
            "numericFilters": f"created_at_i>{since_ts},points>={MIN_HN_POINTS}",
            "hitsPerPage": 30,
        }
        url = f"{HN_ALGOLIA}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                chunks = []
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                data = json.loads(b"".join(chunks))
        except Exception as e:
            print(f"  ! HN fetch error for {term}: {e}")
            continue
        for hit in data.get("hits", []):
            if hit.get("num_comments", 0) < MIN_HN_COMMENTS:
                continue
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
    # Dedupe by title
    seen = set()
    unique = []
    for s in stories:
        norm = re.sub(r"\W+", "", s["title"].lower())[:80]
        if norm in seen:
            continue
        seen.add(norm)
        unique.append(s)
    # Sort by points
    unique.sort(key=lambda s: s["points"], reverse=True)
    return unique


def fetch_rss(url: str) -> list[dict]:
    """Fetch an RSS/Atom feed and extract title/url/date for each item."""
    if not url:
        return []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, */*"})
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            chunks = []
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            content = b"".join(chunks)
    except Exception as e:
        print(f"  ! RSS fetch error for {url}: {e}")
        return []
    items: list[dict] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        print(f"  ! RSS parse error for {url}: {e}")
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for item in root.findall(".//item") + root.findall(".//atom:entry", ns):
        title_el = item.find("title") or item.find("atom:title", ns)
        link_el = item.find("link") or item.find("atom:link", ns)
        date_el = item.find("pubDate") or item.find("atom:published", ns)
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        if not link and link_el is not None:
            link = link_el.get("href", "")
        date_str = date_el.text.strip() if date_el is not None and date_el.text else ""
        if title:
            items.append({"title": title, "url": link, "published": date_str})
    return items


def cross_validate(hn_stories: list[dict], rss_items: list[dict]) -> list[dict]:
    """Merge HN + RSS, mark each story's source.

    A story is "confirmed" if:
      - From official RSS (mark as 'official'), OR
      - Title keyword appears in >=2 HN stories (mark as 'cross-validated')
    """
    rss_titles_norm = {re.sub(r"\W+", "", it["title"].lower())[:60] for it in rss_items}
    out = []
    for hn in hn_stories:
        norm = re.sub(r"\W+", "", hn["title"].lower())[:60]
        # Check title overlap with any RSS item
        matched_rss = any(
            (norm[:30] in rss_norm) or (rss_norm[:30] in norm)
            for rss_norm in rss_titles_norm
        )
        out.append({
            "title": hn["title"],
            "url": hn["url"],
            "hn_url": hn["hn_url"],
            "points": hn["points"],
            "num_comments": hn["num_comments"],
            "created_at": hn["created_at"],
            "author": hn["author"],
            "source": "official" if matched_rss else "community",
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
        print(f"{len(validated)} stories ({sum(1 for s in validated if s['source']=='official')} official)")

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
    print(f"\n  ✓ saved {total} stories from {len(by_company)} companies to {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
