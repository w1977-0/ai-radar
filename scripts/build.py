#!/usr/bin/env python3
"""
build.py — Generate index.html from pricing.json + news.json + changelog.json.

Output: index.html (root of repo, used by GitHub Pages)

Design: minimal terminal-billing-dashboard aesthetic (OpenRouter + AA + Hacker News).
- Inter for body, JetBrains Mono for data
- 8px spacing, 12px radius, 1px borders
- Light + dark mode via prefers-color-scheme
- Tables for pricing data (per OpenRouter / AA style)
- Cards for news (3 stories per company, vertical list inside card)
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
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


def esc(s: str) -> str:
    return html.escape(s or "")


def fmt_price(n: float) -> str:
    """Right-aligned price with tabular-nums. Use '—' for missing."""
    if n is None or n == 0:
        return "—"
    if n < 0.01:
        return f"{n:.4f}"
    if n < 1:
        return f"{n:.3f}"
    if n < 100:
        return f"{n:.2f}"
    return f"{n:.0f}"


def fmt_ctx(n) -> str:
    if not n:
        return "—"
    if n >= 1000:
        v = n / 1000
        return f"{v:.0f}K" if v == int(v) else f"{v:.1f}K"
    return str(n)


def build_changelog_index(changelog: dict) -> dict[str, dict]:
    """Map model_id -> latest change (by timestamp)."""
    latest: dict[str, dict] = {}
    for c in changelog.get("changes", []):
        mid = c.get("model", "")
        if not mid:
            continue
        ts = c.get("timestamp", "")
        if mid not in latest or ts > latest[mid].get("timestamp", ""):
            latest[mid] = c
    return latest


def trend_cell(model: dict, chg_index: dict[str, dict]) -> tuple[str, str]:
    """Return (text, class) for the trend column.

    Primary signal: previously_selected flag from pricing.json (set during fetch).
    Secondary: look up latest pct_change in changelog for that model.
    """
    # First-run detection: previously_selected is False → NEW
    if not model.get("previously_selected"):
        return ("NEW", "trend-new")
    # Find the latest meaningful change (skip "added" entries from first run)
    chg = chg_index.get(model["id"])
    if chg and chg.get("kind") in ("changed",) and chg.get("pct_change") is not None:
        pct = chg["pct_change"]
        if pct > 0.5:
            return (f"↑ {pct:.1f}%", "trend-up")
        if pct < -0.5:
            return (f"↓ {pct:.1f}%", "trend-down")
    return ("—", "trend-flat")


CSS = r"""
:root {
  --bg: #ffffff;
  --bg-subtle: #f6f8fa;
  --bg-row-hover: #f6f8fa;
  --border: #d0d7de;
  --border-strong: #afb8c1;
  --text: #1f2328;
  --text-muted: #656d76;
  --text-dim: #8c959f;
  --accent: #0969da;
  --accent-hover: #0550ae;
  --green: #1a7f37;
  --green-bg: #dafbe1;
  --red: #cf222e;
  --red-bg: #ffebe9;
  --orange: #9a6700;
  --orange-bg: #fff8c5;
  --shadow: 0 1px 0 rgba(31,35,40,0.04);
  --radius: 6px;
  --radius-lg: 12px;
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --font-mono: 'JetBrains Mono', 'SF Mono', ui-monospace, Menlo, Consolas, monospace;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --bg-subtle: #161b22;
    --bg-row-hover: #161b22;
    --border: #30363d;
    --border-strong: #484f58;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --text-dim: #6e7681;
    --accent: #58a6ff;
    --accent-hover: #79c0ff;
    --green: #3fb950;
    --green-bg: rgba(46, 160, 67, 0.15);
    --red: #f85149;
    --red-bg: rgba(248, 81, 73, 0.15);
    --orange: #d29922;
    --orange-bg: rgba(187, 128, 9, 0.15);
  }
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 1.6;
  color: var(--text);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
a { color: var(--accent); text-decoration: none; }
a:hover { color: var(--accent-hover); text-decoration: underline; }
code, .mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
hr { border: 0; border-top: 1px solid var(--border); margin: 32px 0; }

.container { max-width: 1180px; margin: 0 auto; padding: 32px 24px 64px; }

/* Header */
.site-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 0 32px; border-bottom: 1px solid var(--border); margin-bottom: 48px; }
.site-header .brand { display: flex; align-items: baseline; gap: 12px; }
.site-header h1 { font-size: 20px; font-weight: 600; margin: 0; letter-spacing: -0.3px; }
.site-header .tagline { color: var(--text-muted); font-size: 13px; }
.site-header nav { display: flex; gap: 20px; font-size: 13px; }
.site-header nav a { color: var(--text-muted); }
.site-header nav a:hover { color: var(--text); text-decoration: none; }

.meta { color: var(--text-dim); font-size: 12px; margin-top: 4px; }

/* Section */
section { margin-bottom: 56px; }
section > h2 {
  font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
  color: var(--text-muted); margin: 0 0 16px; padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}
section > h2 .count { color: var(--text-dim); font-weight: 400; margin-left: 8px; }

/* Tier blocks (pricing) */
.tier { margin-bottom: 24px; }
.tier-header { display: flex; align-items: baseline; justify-content: space-between; margin: 0 0 8px; padding: 0 4px; }
.tier-header h3 { font-size: 14px; font-weight: 600; margin: 0; }
.tier-header .tier-meta { color: var(--text-dim); font-size: 12px; font-family: var(--font-mono); }
.tier-bullet { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 8px; vertical-align: middle; }
.tier-bullet-S { background: #cf222e; }
.tier-bullet-A { background: #d29922; }
.tier-bullet-B { background: #1a7f37; }
.tier-bullet-C { background: #656d76; }

/* Pricing table */
table.data { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--bg); }
table.data th, table.data td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); }
table.data th { font-weight: 500; color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; background: var(--bg-subtle); }
table.data tbody tr:hover { background: var(--bg-row-hover); }
table.data td.id { font-family: var(--font-mono); font-size: 12.5px; }
table.data td.id a { color: var(--text); }
table.data td.id a:hover { color: var(--accent); }
table.data td.num, table.data td.right { text-align: right; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
table.data td.trend { text-align: right; font-family: var(--font-mono); font-size: 12px; }
table.data td.ctx { text-align: right; font-family: var(--font-mono); color: var(--text-muted); font-size: 12px; }
table.data td.company { color: var(--text-muted); font-size: 12.5px; }

/* Trend pills */
.trend-up { color: var(--green); }
.trend-down { color: var(--red); }
.trend-flat { color: var(--text-dim); }
.trend-new { color: var(--accent); }
.pill { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px; font-weight: 500; font-family: var(--font-mono); }
.pill-official { background: var(--green-bg); color: var(--green); }
.pill-community { background: var(--bg-subtle); color: var(--text-muted); border: 1px solid var(--border); }

/* News grid */
.news-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; }
.news-card { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px; display: flex; flex-direction: column; gap: 8px; }
.news-card:hover { border-color: var(--border-strong); }
.news-card .company { font-size: 12px; color: var(--text-muted); font-weight: 500; text-transform: uppercase; letter-spacing: 0.4px; }
.news-card .story { display: flex; flex-direction: column; gap: 4px; }
.news-card .story .title { font-size: 13.5px; line-height: 1.45; }
.news-card .story .title a { color: var(--text); }
.news-card .story .title a:hover { color: var(--accent); }
.news-card .story .meta { font-size: 11px; color: var(--text-dim); font-family: var(--font-mono); }
.news-card .empty { font-size: 12px; color: var(--text-dim); font-style: italic; padding: 8px 0; }

/* Changelog */
.changelog-wrap { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; }
.changelog-wrap table { margin: 0; }

/* Footer */
.site-footer { margin-top: 64px; padding-top: 24px; border-top: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: var(--text-dim); }
.site-footer a { color: var(--text-muted); }
.site-footer a:hover { color: var(--text); text-decoration: underline; }

@media (max-width: 720px) {
  .container { padding: 24px 16px 48px; }
  .site-header { flex-direction: column; align-items: flex-start; gap: 12px; }
  .news-grid { grid-template-columns: 1fr; }
  .site-footer { flex-direction: column; gap: 8px; align-items: flex-start; }
}
"""


def render_header(pricing: dict) -> str:
    last_run = pricing.get("generated_at", "unknown")
    last_run_short = last_run[:19].replace("T", " ") if last_run != "unknown" else "unknown"
    n_models = len(pricing.get("models", []))
    return f"""
<header class="site-header">
  <div class="brand">
    <h1>AI Radar</h1>
    <span class="tagline">Frontier model pricing &amp; AI company news.</span>
  </div>
  <nav>
    <a href="#pricing">Pricing</a>
    <a href="#news">News</a>
    <a href="#changelog">Changelog</a>
    <a href="https://github.com/w1977-0/ai-radar" target="_blank" rel="noopener">GitHub</a>
    <a href="https://artificialanalysis.ai/" target="_blank" rel="noopener" title="Intelligence Index source">AA</a>
    </nav>
    </header>
<div class="meta">Last run: {esc(last_run_short)} UTC · {n_models} models tracked · 12 companies · updated every 6 hours · sources: OpenRouter, Hacker News, RSS</div>
"""


def render_pricing(pricing: dict, chg_index: dict[str, dict], benchmarks: dict) -> str:
    models = pricing.get("models", [])
    grouped: dict[str, list[dict]] = {t: [] for t in TIER_ORDER}
    for m in models:
        grouped.setdefault(m.get("tier", "?"), []).append(m)
    for t in grouped:
        grouped[t].sort(key=lambda m: m["pricing"]["completion_per_million"], reverse=(t == "S"))

    out: list[str] = ['<section id="pricing">', '<h2>Pricing<span class="count">' + str(len(models)) + ' models</span></h2>']

    for tier in TIER_ORDER:
        items = grouped.get(tier, [])
        if not items:
            continue
        label, desc = TIER_LABELS[tier]
        out.append(f'<div class="tier">')
        out.append(f'<div class="tier-header"><h3><span class="tier-bullet tier-bullet-{tier}"></span>Tier {tier} — {esc(label)}</h3><span class="tier-meta">{esc(desc)}</span></div>')
        out.append('<table class="data">')
        out.append('<thead><tr><th>Model</th><th>Company</th><th class="right">Input</th><th class="right">Output</th><th class="right">Ctx</th><th class="right">Trend</th><th class="right">AI Index</th><th class="right">Coding</th><th class="right">Speed</th></tr></thead>')
        out.append('<tbody>')
        for m in items:
            trend_text, trend_cls = trend_cell(m, chg_index)
            ctx = m.get("context_length") or 0
            b = benchmarks.get(m["id"], {})
            ai_idx = b.get("intelligence_index")
            coding = b.get("coding_index")
            speed = b.get("speed_tokens_per_sec")
            ai_str = f'{ai_idx:.0f}' if isinstance(ai_idx, (int, float)) and ai_idx > 0 else '—'
            cod_str = f'{coding:.0f}' if isinstance(coding, (int, float)) and coding > 0 else '—'
            spd_str = f'{speed:.0f}' if isinstance(speed, (int, float)) and speed > 0 else '—'
            # AA attribution
            ai_title = b.get("aa_name", "—")
            if isinstance(ai_idx, (int, float)) and ai_idx > 0:
                ai_cell = f'<span title="{esc(ai_title)} (AA)">{ai_str}</span>'
            else:
                ai_cell = '—'
            out.append(
                f'<tr>'
                f'<td class="id"><a href="https://openrouter.ai/models/{esc(m["id"])}" target="_blank" rel="noopener">{esc(m["id"])}</a></td>'
                f'<td class="company">{esc(m["company"] or "?")}</td>'
                f'<td class="num">${fmt_price(m["pricing"]["prompt_per_million"])}</td>'
                f'<td class="num">${fmt_price(m["pricing"]["completion_per_million"])}</td>'
                f'<td class="ctx">{fmt_ctx(ctx)}</td>'
                f'<td class="trend {trend_cls}">{esc(trend_text)}</td>'
                f'<td class="num">{ai_cell}</td>'
                f'<td class="num">{cod_str}</td>'
                f'<td class="num">{spd_str}</td>'
                f'</tr>'
            )
        out.append('</tbody></table>')
        out.append('</div>')
    out.append('</section>')
    return "\n".join(out)


def render_news(news: dict) -> str:
    out: list[str] = ['<section id="news">', '<h2>News<span class="count">last 7 days</span></h2>']
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
                src = s.get("source", "community")
                pill = '<span class="pill pill-official">official</span>' if src == "official" else '<span class="pill pill-community">community</span>'
                out.append('<div class="story">')
                out.append(f'<div class="title"><a href="{esc(url)}" target="_blank" rel="noopener">{esc(title)}</a></div>')
                out.append(f'<div class="meta">↑ {s.get("points",0)} · 💬 {s.get("num_comments",0)} &nbsp; {pill}</div>')
                out.append('</div>')
        out.append('</div>')
    out.append('</div>')
    out.append('</section>')
    return "\n".join(out)


def render_changelog(changelog: dict) -> str:
    changes = changelog.get("changes", [])[-15:]  # last 15
    out: list[str] = ['<section id="changelog">', '<h2>Changelog<span class="count">last 30 days</span></h2>']
    if not changes:
        out.append('<div class="meta">No price changes tracked yet.</div>')
    else:
        out.append('<div class="changelog-wrap">')
        out.append('<table class="data">')
        out.append('<thead><tr><th>Time</th><th>Model</th><th>Kind</th><th class="right">Old</th><th class="right">New</th><th class="right">Δ%</th></tr></thead>')
        out.append('<tbody>')
        for c in changes:
            ts = c.get("timestamp", "")[:19].replace("T", " ")
            kind = c.get("kind", "?")
            # 'added' entries from the very first run are noise — show as "init" instead
            is_first_run_noise = (
                kind == "added"
                and c.get("old_price") is None
                and not c.get("pct_change")
            )
            display_kind = "init" if is_first_run_noise else kind
            kind_cls = "trend-new" if display_kind == "added" else "trend-down" if display_kind == "removed" else "trend-flat"
            pct = c.get("pct_change")
            pct_str = f'{pct:+.1f}%' if pct is not None else '—'
            out.append(
                f'<tr>'
                f'<td class="ctx">{esc(ts)}</td>'
                f'<td class="id">{esc(c.get("model","?"))}</td>'
                f'<td class="{kind_cls}">{esc(display_kind)}</td>'
                f'<td class="num">${fmt_price(c.get("old_price") or 0)}</td>'
                f'<td class="num">${fmt_price(c.get("new_price") or 0)}</td>'
                f'<td class="trend">{pct_str}</td>'
                f'</tr>'
            )
        out.append('</tbody></table>')
        out.append('</div>')
    out.append('</section>')
    return "\n".join(out)


def render_footer() -> str:
    return """
<footer class="site-footer">
  <div>Data: <a href="https://openrouter.ai">OpenRouter</a> · <a href="https://hn.algolia.com">HN Algolia</a> · <a href="https://artificialanalysis.ai/" title="Intelligence Index">Artificial Analysis</a> · company RSS</div>
  <div>© 2026 <a href="https://github.com/w1977-0">w1977-0</a> · <a href="https://github.com/w1977-0/ai-radar">w1977-0/ai-radar</a></div>
</footer>
"""


def main() -> int:
    pricing = json.loads((DATA_DIR / "pricing.json").read_text())
    news = json.loads((DATA_DIR / "news.json").read_text())
    changelog = json.loads((DATA_DIR / "changelog.json").read_text())
    benchmarks_path = DATA_DIR / "benchmarks.json"
    benchmarks = json.loads(benchmarks_path.read_text()) if benchmarks_path.exists() else {"models": {}}
    chg_index = build_changelog_index(changelog)

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        "<title>AI Radar — frontier model pricing & AI company news</title>",
        '<meta name="description" content="AI API pricing & news tracker · 20 frontier models + 12 AI companies, cross-validated via Hacker News + RSS. Updated every 6 hours.">',
        "<style>" + CSS + "</style>",
        "</head>",
        '<body><div class="container">',
        render_header(pricing),
        render_pricing(pricing, chg_index, benchmarks.get("models", {})),
        render_news(news),
        render_changelog(changelog),
        render_footer(),
        "</div></body></html>",
    ]
    html_out = "\n".join(parts)
    OUT_FILE.write_text(html_out)
    print(f"  ✓ wrote {OUT_FILE} ({len(html_out)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
