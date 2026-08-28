#!/usr/bin/env python3
"""
build.py — Generate index.html from pricing.json + news.json + changelog.json.

Output: index.html (root of repo, used by GitHub Pages)
"""
from __future__ import annotations

import json
import html
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_FILE = Path(__file__).resolve().parent.parent / "index.html"

TIER_ORDER = ["S", "A", "B", "C"]
TIER_LABELS = {
    "S": ("Super-flagship", ">$30/M output"),
    "A": ("Flagship", "$10-30/M output"),
    "B": ("Mainstream", "$1-10/M output"),
    "C": ("Economy", "<$1/M output"),
}


def fmt_price(n: float) -> str:
    if n == 0:
        return "free"
    if n < 0.01:
        return f"${n:.4f}"
    if n < 1:
        return f"${n:.3f}"
    if n < 100:
        return f"${n:.2f}"
    return f"${n:.0f}"


def esc(s: str) -> str:
    return html.escape(s or "")


def render(pricing: dict, news: dict, changelog: dict) -> str:
    models = pricing.get("models", [])
    grouped: dict[str, list[dict]] = {t: [] for t in TIER_ORDER}
    for m in models:
        grouped.setdefault(m.get("tier", "?"), []).append(m)
    for t in grouped:
        grouped[t].sort(key=lambda m: m["pricing"]["completion_per_million"])

    last_run = pricing.get("generated_at", "unknown")
    last_run_short = last_run[:19].replace("T", " ") if last_run != "unknown" else "unknown"

    # Header
    out: list[str] = []
    out.append("<!DOCTYPE html>")
    out.append('<html lang="en">')
    out.append("<head>")
    out.append('<meta charset="UTF-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    out.append("<title>AI Radar — frontier model pricing & AI company news</title>")
    out.append('<meta name="description" content="AI API pricing & news tracker · 20 frontier models + 12 AI companies, cross-validated via Hacker News + RSS. Updated every 6 hours.">')
    out.append("<style>")
    out.append("""
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; line-height: 1.6; color: #1a1a1a; background: #fafafa; -webkit-font-smoothing: antialiased; }
  .container { max-width: 1100px; margin: 0 auto; padding: 48px 24px; }
  header { margin-bottom: 48px; padding-bottom: 24px; border-bottom: 1px solid #e5e5e5; }
  h1 { font-size: 2rem; font-weight: 700; letter-spacing: -0.02em; margin-bottom: 8px; }
  .tagline { color: #555; font-size: 1rem; }
  .meta { color: #888; font-size: 0.85rem; margin-top: 8px; }
  h2 { font-size: 1.4rem; font-weight: 600; margin: 40px 0 16px; letter-spacing: -0.01em; }
  h3 { font-size: 1.05rem; font-weight: 600; margin: 24px 0 8px; }
  table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e5e5e5; border-radius: 8px; overflow: hidden; margin-bottom: 24px; }
  th, td { padding: 10px 14px; text-align: left; font-size: 0.9rem; border-bottom: 1px solid #eee; }
  th { background: #f5f5f5; font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  td.id { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.82rem; }
  td.price { font-variant-numeric: tabular-nums; text-align: right; }
  .tier-S { border-left: 3px solid #c0392b; }
  .tier-A { border-left: 3px solid #d68910; }
  .tier-B { border-left: 3px solid #229954; }
  .tier-C { border-left: 3px solid #5b6e8c; }
  .news-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
  .news-card { background: #fff; border: 1px solid #e5e5e5; border-radius: 8px; padding: 16px; }
  .news-card h4 { font-size: 0.92rem; font-weight: 600; margin-bottom: 6px; }
  .news-card h4 a { color: #1a1a1a; text-decoration: none; }
  .news-card h4 a:hover { text-decoration: underline; }
  .news-card .company { color: #888; font-size: 0.78rem; margin-bottom: 6px; }
  .news-card .source { display: inline-block; font-size: 0.72rem; padding: 1px 6px; border-radius: 3px; margin-top: 6px; }
  .source-official { background: #d4edda; color: #155724; }
  .source-community { background: #fff3cd; color: #856404; }
  .empty { color: #888; font-style: italic; padding: 12px; }
  .footer { margin-top: 48px; padding-top: 16px; border-top: 1px solid #e5e5e5; color: #888; font-size: 0.82rem; text-align: center; }
  @media (max-width: 600px) { .container { padding: 32px 16px; } h1 { font-size: 1.6rem; } table { font-size: 0.82rem; } }
""")
    out.append("</style>")
    out.append("</head><body>")
    out.append('<div class="container">')

    out.append("<header>")
    out.append("<h1>AI Radar</h1>")
    out.append('<div class="tagline">Frontier model pricing &amp; AI company news — 20 models, 12 companies, cross-validated.</div>')
    out.append(f'<div class="meta">Last run: {esc(last_run_short)} UTC · Source: <a href="https://openrouter.ai">OpenRouter</a> + Hacker News + RSS · <a href="https://github.com/w1977-0/ai-radar">GitHub</a></div>')
    out.append("</header>")

    # Pricing section
    out.append("<h2>Pricing · 价格</h2>")
    out.append(f'<div class="meta">20 models, selected from 380+ OpenRouter models, grouped by output price tier.</div>')
    for tier in TIER_ORDER:
        items = grouped.get(tier, [])
        if not items:
            continue
        label, desc = TIER_LABELS[tier]
        out.append(f"<h3>Tier {tier} — {esc(label)} <span style='color:#888;font-weight:400;font-size:0.85rem'>({esc(desc)})</span></h3>")
        out.append('<table>')
        out.append("<thead><tr><th>Model</th><th>Company</th><th>Context</th><th class='price'>Input $/1M</th><th class='price'>Output $/1M</th></tr></thead>")
        out.append("<tbody>")
        for m in items:
            ctx = m.get("context_length") or 0
            ctx_str = f"{ctx//1000}K" if ctx >= 1000 else str(ctx)
            out.append(
                f"<tr class='tier-{tier}'>"
                f"<td class='id'>{esc(m['id'])}</td>"
                f"<td>{esc(m['company'] or '?')}</td>"
                f"<td>{ctx_str}</td>"
                f"<td class='price'>{fmt_price(m['pricing']['prompt_per_million'])}</td>"
                f"<td class='price'>{fmt_price(m['pricing']['completion_per_million'])}</td>"
                f"</tr>"
            )
        out.append("</tbody></table>")

    # News section
    out.append("<h2>News · 新闻</h2>")
    out.append('<div class="meta">Top stories per company, last 7 days. Official = also in company RSS feed. Community = HN only.</div>')
    out.append('<div class="news-grid">')
    for key, info in news.get("companies", {}).items():
        out.append('<div class="news-card">')
        out.append(f'<div class="company">{esc(info.get("name", key))}</div>')
        stories = info.get("stories", [])
        if not stories:
            out.append('<div class="empty">No stories in window.</div>')
        else:
            for s in stories[:3]:
                title = s.get("title", "")
                url = s.get("hn_url") or s.get("url", "#")
                out.append(f"<h4><a href='{esc(url)}' target='_blank' rel='noopener'>{esc(title)}</a></h4>")
                out.append(f"<div style='color:#888;font-size:0.75rem'>↑ {s.get('points',0)} · 💬 {s.get('num_comments',0)}</div>")
                src = s.get("source", "community")
                src_class = f"source-{src}"
                src_label = "✓ official" if src == "official" else "community"
                out.append(f"<span class='source {src_class}'>{src_label}</span>")
        out.append("</div>")
    out.append("</div>")

    # Changelog section
    out.append("<h2>Recent changes · 近期价格变化</h2>")
    changes = changelog.get("changes", [])[-15:]  # last 15
    if not changes:
        out.append('<div class="empty">No price changes tracked yet (first run will show all models as "added").</div>')
    else:
        out.append("<table>")
        out.append("<thead><tr><th>Time</th><th>Model</th><th>Kind</th><th class='price'>Old $/M</th><th class='price'>New $/M</th><th class='price'>Δ%</th></tr></thead>")
        out.append("<tbody>")
        for c in changes:
            ts = c.get("timestamp", "")[:19].replace("T", " ")
            out.append(
                f"<tr>"
                f"<td>{esc(ts)}</td>"
                f"<td class='id'>{esc(c.get('model','?'))}</td>"
                f"<td>{esc(c.get('kind','?'))}</td>"
                f"<td class='price'>{fmt_price(c.get('old_price') or 0)}</td>"
                f"<td class='price'>{fmt_price(c.get('new_price') or 0)}</td>"
                f"<td class='price'>{(str(c.get('pct_change')) + '%') if c.get('pct_change') is not None else '—'}</td>"
                f"</tr>"
            )
        out.append("</tbody></table>")

    # Footer
    out.append('<div class="footer">')
    out.append('Data sources: <a href="https://openrouter.ai">OpenRouter</a> (pricing) · <a href="https://hn.algolia.com/api">HN Algolia</a> (community) · Company RSS feeds (official).')
    out.append('<br>Generated by <a href="https://github.com/w1977-0/ai-radar">w1977-0/ai-radar</a>.')
    out.append("</div>")

    out.append("</div></body></html>")
    return "\n".join(out)


def main() -> int:
    pricing = json.loads((DATA_DIR / "pricing.json").read_text())
    news = json.loads((DATA_DIR / "news.json").read_text())
    changelog = json.loads((DATA_DIR / "changelog.json").read_text())

    html_out = render(pricing, news, changelog)
    OUT_FILE.write_text(html_out)
    print(f"  ✓ wrote {OUT_FILE} ({len(html_out)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
