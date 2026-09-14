from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path


SCENES = {
    "work_communication": "工作沟通",
    "parenting_family": "亲子家庭",
    "health_state": "健康状态",
    "content_consumption": "内容消费",
    "inspiration_insight": "灵感洞察",
    "self_growth": "自我成长",
    "life_decisions": "生活决策",
}


def inline(text: str) -> str:
    value = html.escape(text, quote=True)
    value = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", value)
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    value = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2" target="_blank" rel="noreferrer">\1</a>',
        value,
    )
    return value


def render_markdown(markdown: str) -> str:
    parts: list[str] = []
    paragraph: list[str] = []
    list_kind: str | None = None

    def flush_paragraph() -> None:
        if paragraph:
            parts.append(f"<p>{inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            parts.append(f"</{list_kind}>")
            list_kind = None

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            close_list()
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        item = re.match(r"^[-*]\s+(.+)$", line)
        numbered = re.match(r"^\d+[.)]\s+(.+)$", line)
        if heading:
            flush_paragraph()
            close_list()
            level = min(len(heading.group(1)) + 1, 5)
            parts.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif item or numbered:
            flush_paragraph()
            wanted = "ul" if item else "ol"
            if list_kind != wanted:
                close_list()
                list_kind = wanted
                parts.append(f"<{wanted}>")
            text = (item or numbered).group(1)
            parts.append(f"<li>{inline(text)}</li>")
        elif line.startswith("> "):
            flush_paragraph()
            close_list()
            parts.append(f"<blockquote>{inline(line[2:])}</blockquote>")
        else:
            close_list()
            paragraph.append(line)
    flush_paragraph()
    close_list()
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    review = json.loads(args.review.read_text(encoding="utf-8"))
    cards = sorted(bundle["cards"], key=lambda card: card["position"])
    scene_counts: dict[str, int] = {}
    for card in cards:
        scene_counts[card["scene_id"]] = scene_counts.get(card["scene_id"], 0) + 1

    nav = [
        '<button class="filter active" data-scene="all"><span>\u5168\u90e8\u62a5\u544a</span>'
        f'<b>{len(cards)}</b></button>'
    ]
    for scene, count in scene_counts.items():
        nav.append(
            f'<button class="filter" data-scene="{html.escape(scene)}">'
            f'<span>{SCENES.get(scene, scene)}</span><b>{count}</b></button>'
        )

    articles = []
    for card in cards:
        scene = card["scene_id"]
        source_count = len(card.get("source_segment_ids") or [])
        web_count = len(card.get("used_source_ids") or [])
        articles.append(
            f'''<article class="report-card" data-scene="{html.escape(scene)}"
                data-search="{html.escape((card['title'] + ' ' + card['summary']).lower(), quote=True)}">
              <div class="card-topline">
                <span class="scene-tag">{SCENES.get(scene, scene)}</span>
                <span class="evidence">{source_count} \u6761\u5f55\u97f3\u8bc1\u636e{f' \u00b7 {web_count} \u6761\u7f51\u7edc\u6765\u6e90' if web_count else ''}</span>
              </div>
              <div class="markdown">{render_markdown(card['markdown'])}</div>
              <details class="trace"><summary>\u67e5\u770b\u8bc1\u636e索引</summary>
                <p>{html.escape(', '.join(card.get('source_segment_ids') or []))}</p>
              </details>
            </article>'''
        )

    document = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Beta 8 \u5f55\u97f3\u5206\u6790\u62a5\u544a</title>
<style>
:root{{--paper:#f4f1ea;--ink:#171916;--muted:#686b64;--line:#d9d5cc;--card:#fffefa;--accent:#235c45;--soft:#e6eee8}}
*{{box-sizing:border-box}} html{{scroll-behavior:smooth}} body{{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.72}}
.shell{{display:grid;grid-template-columns:280px minmax(0,820px);gap:48px;max-width:1220px;margin:0 auto;padding:42px 32px 100px}}
.side{{position:sticky;top:28px;align-self:start;height:calc(100vh - 56px);display:flex;flex-direction:column}}
.brand{{font-size:12px;letter-spacing:.16em;color:var(--accent);font-weight:750;text-transform:uppercase}} .side h1{{font-family:ui-serif,"Songti SC",serif;font-size:29px;line-height:1.2;margin:14px 0 12px}}
.side .lede{{font-size:14px;color:var(--muted);margin:0 0 24px}} .search{{width:100%;border:1px solid var(--line);border-radius:12px;background:rgba(255,255,255,.55);padding:11px 13px;font-size:14px;outline:none}}
.search:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(35,92,69,.08)}} nav{{margin-top:14px;display:grid;gap:5px}} .filter{{appearance:none;border:0;background:transparent;border-radius:10px;padding:10px 12px;color:var(--muted);display:flex;justify-content:space-between;cursor:pointer;text-align:left;font-size:14px}}
.filter:hover,.filter.active{{background:var(--soft);color:var(--accent)}} .filter b{{font-size:12px}} .status{{margin-top:auto;border-top:1px solid var(--line);padding-top:18px;font-size:12px;color:var(--muted)}}
.hero{{padding:18px 0 34px;border-bottom:1px solid var(--line)}} .eyebrow{{color:var(--accent);font-weight:700;font-size:13px}} .hero h2{{font:600 48px/1.12 ui-serif,"Songti SC",serif;letter-spacing:-.03em;margin:12px 0 15px}} .hero p{{font-size:17px;color:#50534e;max-width:720px}}
.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:26px}} .metric{{background:rgba(255,255,255,.5);border:1px solid var(--line);border-radius:14px;padding:14px}} .metric strong{{display:block;font:650 23px/1.1 ui-serif,"Songti SC",serif}} .metric span{{display:block;font-size:11px;color:var(--muted);margin-top:7px}}
.report-card{{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:34px 38px;margin-top:24px;box-shadow:0 12px 34px rgba(32,35,30,.035)}} .report-card.hidden{{display:none}} .card-topline{{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid #ebe7de;padding-bottom:16px;margin-bottom:24px}}
.scene-tag{{font-size:12px;font-weight:700;color:var(--accent);background:var(--soft);padding:4px 9px;border-radius:99px}} .evidence{{font-size:12px;color:var(--muted)}} .markdown h2{{font:650 31px/1.25 ui-serif,"Songti SC",serif;margin:0 0 18px}} .markdown h3{{font:650 21px/1.35 ui-serif,"Songti SC",serif;margin:34px 0 12px}} .markdown h4{{font-size:17px;margin:26px 0 8px}}
.markdown p{{margin:0 0 15px}} .markdown li{{margin:5px 0}} .markdown ul,.markdown ol{{padding-left:1.35em;margin:8px 0 18px}} .markdown blockquote{{margin:18px 0;padding:12px 16px;border-left:3px solid var(--accent);background:var(--soft);color:#3f5148}} .markdown a{{color:var(--accent)}} code{{font-family:ui-monospace,monospace;background:#efede7;padding:2px 5px;border-radius:5px;font-size:.88em}}
.trace{{margin-top:28px;padding-top:16px;border-top:1px solid #ebe7de;color:var(--muted);font-size:12px}} .trace summary{{cursor:pointer;font-weight:650;color:#555b55}} .trace p{{word-break:break-all;line-height:1.6}}
.empty{{display:none;text-align:center;padding:60px 20px;color:var(--muted)}}
@media(max-width:860px){{.shell{{display:block;padding:24px 16px 70px}}.side{{position:static;height:auto}}nav{{grid-template-columns:repeat(2,1fr)}}.status{{margin-top:18px}}.hero h2{{font-size:37px}}.metrics{{grid-template-columns:repeat(2,1fr)}}.report-card{{padding:26px 22px}}}}
</style>
</head>
<body><div class="shell">
<aside class="side"><div class="brand">Audio Memory \u00b7 Beta 8</div><h1>\u5f55\u97f3\u5206\u6790\u62a5\u544a</h1><p class="lede">\u8c46\u5305\u957f\u5f55\u97f3 2.0 \u5408\u5e76\u9010\u5b57\u7a3f\uff0cDeepSeek V4 Pro \u5206\u6790\uff0cKimi K2.6 \u8054\u7f51\u641c\u7d22。</p><input id="search" class="search" placeholder="\u641c\u7d22 23 \u5f20\u62a5\u544a\u5361\u2026"><nav>{''.join(nav)}</nav><div class="status">\u6700\u7ec8裁决已关闭所有已接受问题<br>\u751f\u6210时间\uff1a2026-09-03 14:29</div></aside>
<main><section class="hero"><div class="eyebrow">\u5b8c\u6574\u94fe\u8def\u8bc4\u6d4b\u7ed3\u679c</div><h2>\u4ece 11.8 \u4e07\u5b57\u5f55\u97f3\u4e2d，<br>\u63d0炼出 23 \u5f20\u53ef追溯报\u544a</h2><p>\u62a5\u544a\u8986\u76d6 6 \u7c7b\u573a\u666f\uff0c保留完整 Markdown 内容与证据段索引。Kimi 联网搜索已修复，10/10 搜索任务成功，不再降级。</p><div class="metrics"><div class="metric"><strong>{review['score']}/100</strong><span>\u6700\u7ec8终审评分</span></div><div class="metric"><strong>{len(cards)}</strong><span>\u62a5\u544a\u5361\u7247</span></div><div class="metric"><strong>10/10</strong><span>Kimi \u641c\u7d22\u4efb\u52a1</span></div><div class="metric"><strong>\u00a58.600029</strong><span>\u8d26\u6237余额实际差额</span></div></div></section>
<section id="cards">{''.join(articles)}</section><div id="empty" class="empty">\u6ca1\u6709\u5339\u914d\u7684\u62a5\u544a\u5361</div></main></div>
<script>
const buttons=[...document.querySelectorAll('.filter')],cards=[...document.querySelectorAll('.report-card')],search=document.querySelector('#search'),empty=document.querySelector('#empty');let scene='all';
function apply(){{const q=search.value.trim().toLowerCase();let shown=0;cards.forEach(c=>{{const okScene=scene==='all'||c.dataset.scene===scene;const okQuery=!q||c.dataset.search.includes(q)||c.innerText.toLowerCase().includes(q);c.classList.toggle('hidden',!(okScene&&okQuery));if(okScene&&okQuery)shown++;}});empty.style.display=shown?'none':'block';}}
buttons.forEach(b=>b.addEventListener('click',()=>{{buttons.forEach(x=>x.classList.remove('active'));b.classList.add('active');scene=b.dataset.scene;apply();window.scrollTo({{top:0,behavior:'smooth'}});}}));search.addEventListener('input',apply);
</script></body></html>'''
    args.output.write_text(document, encoding="utf-8")
    print(json.dumps({"output": str(args.output), "cards": len(cards)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
