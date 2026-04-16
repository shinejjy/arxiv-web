#!/usr/bin/env python3
from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

JOB_ID = "054d56957dbb"
SRC_DIR = Path.home() / ".hermes" / "cron" / "output" / JOB_ID
OUT_DIR = Path(__file__).resolve().parent / "docs"
OUT_FILE = OUT_DIR / "index.html"
ROOT_FILE = Path(__file__).resolve().parent / "index.html"

SECTION_RE = re.compile(r"^##\s+(.*?)\s*$")
PAPER_RE = re.compile(r"^\d+\.\s+\*\*(.*?)\*\*\s*$")
ARXIV_RE = re.compile(r"^\s*-\s+\*\*arXiv:\*\*\s+(.*?)\s*$")
TIME_RE = re.compile(r"^\s*-\s+\*\*发布时间（北京时间）：\*\*\s+(.*?)\s*$")
TAGS_RE = re.compile(r"^\s*-\s+\*\*标签：\*\*\s+(.*?)\s*$")
INSIGHT_RE = re.compile(r"^\s*-\s+\*\*看点：\*\*\s+(.*?)\s*$")


def latest_markdown() -> Path | None:
    if not SRC_DIR.exists():
        return None
    files = sorted(SRC_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def parse_markdown(text: str) -> dict:
    lines = text.splitlines()
    data = {
        "job_name": None,
        "job_id": None,
        "run_time": None,
        "schedule": None,
        "sections": [],
        "top3": [],
        "overall": None,
    }
    current_section = None
    current_paper = None
    in_response = False

    for line in lines:
        if line.startswith("# Cron Job:"):
            data["job_name"] = line.split(":", 1)[1].strip()
            continue
        if line.startswith("**Job ID:**"):
            data["job_id"] = line.split("**Job ID:**", 1)[1].strip()
            continue
        if line.startswith("**Run Time:**"):
            data["run_time"] = line.split("**Run Time:**", 1)[1].strip()
            continue
        if line.startswith("**Schedule:**"):
            data["schedule"] = line.split("**Schedule:**", 1)[1].strip()
            continue
        if line.strip() == "## Response":
            in_response = True
            continue
        if not in_response:
            continue

        m = SECTION_RE.match(line)
        if m:
            title = m.group(1)
            if title.startswith("今天最值得重点看的 3 篇"):
                current_section = "top3"
                continue
            if title.startswith("整体判断"):
                current_section = "overall"
                continue
            current_section = title
            data["sections"].append({"title": title, "papers": []})
            continue

        if current_section == "top3":
            stripped = line.strip()
            if stripped[:1] in {"1", "2", "3"} and "**" in stripped:
                data["top3"].append(stripped)
            continue
        if current_section == "overall":
            if line.strip():
                data["overall"] = line.strip()
            continue

        if not data["sections"]:
            continue
        papers = data["sections"][-1]["papers"]
        pm = PAPER_RE.match(line)
        if pm:
            current_paper = {"title": pm.group(1), "arxiv": None, "time": None, "tags": None, "insight": None}
            papers.append(current_paper)
            continue
        if current_paper is None:
            continue
        if (am := ARXIV_RE.match(line)):
            current_paper["arxiv"] = am.group(1)
        elif (tm := TIME_RE.match(line)):
            current_paper["time"] = tm.group(1)
        elif (tg := TAGS_RE.match(line)):
            current_paper["tags"] = tg.group(1)
        elif (im := INSIGHT_RE.match(line)):
            current_paper["insight"] = im.group(1)

    return data


def esc(x: str | None) -> str:
    return html.escape(x or "")


def badge(text: str, kind: str = "neutral") -> str:
    return f"<span class='badge {kind}'>{esc(text)}</span>"


def render() -> str:
    md = latest_markdown()
    if not md:
        body = """
        <section class='hero card hero-empty'>
          <div class='eyebrow'>arXiv 每日摘要</div>
          <h1>还没有找到最新输出</h1>
          <p class='lede'>请先运行一次定时任务，页面会自动读取最新的 cron 结果并展示为手机友好的日报。</p>
        </section>
        """
        source = ""
        count = 0
        sections = []
        top3 = []
        overall = None
        data = None
    else:
        data = parse_markdown(md.read_text(encoding="utf-8"))
        count = sum(len(sec["papers"]) for sec in data["sections"])
        sections = data["sections"]
        top3 = data["top3"]
        overall = data["overall"]
        source = f"<div class='source'>来源文件：{esc(md.name)} · 更新时间：{esc(datetime.fromtimestamp(md.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S'))}</div>"

        meta_bits = [
            f"Job ID {esc(data['job_id'])}" if data.get('job_id') else None,
            f"Run Time {esc(data['run_time'])}" if data.get('run_time') else None,
            f"Schedule {esc(data['schedule'])}" if data.get('schedule') else None,
        ]
        meta_bits = [x for x in meta_bits if x]

        section_cards = []
        for idx, sec in enumerate(sections):
            tone = ["blue", "green", "violet", "amber", "rose"][idx % 5]
            section_cards.append(
                f"<div class='section-chip {tone}'><strong>{esc(sec['title'])}</strong><span>{len(sec['papers'])} 篇</span></div>"
            )

        paper_cards = []
        for sec in sections:
            paper_cards.append(f"<section class='section-block'><div class='section-head'><h2>{esc(sec['title'])}</h2><span>{len(sec['papers'])} 篇</span></div><div class='grid'>")
            for p in sec["papers"]:
                arxiv_id = p.get("arxiv") or ""
                arxiv_url = f"https://arxiv.org/abs/{esc(arxiv_id)}" if arxiv_id else "https://arxiv.org"
                tags = p.get("tags") or ""
                tag_items = [t.strip() for t in re.split(r"[，,/]", tags) if t.strip()]
                tag_html = "".join(badge(t, "soft") for t in tag_items[:3])
                paper_cards.append(
                    f"""
                    <article class='paper-card'>
                      <div class='paper-top'>
                        <div class='paper-title'><a href='{arxiv_url}' target='_blank' rel='noreferrer'>{esc(p.get('title'))}</a></div>
                        <a class='arxiv-link' href='{arxiv_url}' target='_blank' rel='noreferrer'>{esc(arxiv_id)}</a>
                      </div>
                      <div class='paper-meta'>
                        {f"<div><span>发布时间</span><strong>{esc(p.get('time'))}</strong></div>" if p.get('time') else ""}
                        {f"<div><span>标签</span><strong>{esc(p.get('tags'))}</strong></div>" if p.get('tags') else ""}
                      </div>
                      {f"<p class='insight'>{esc(p.get('insight'))}</p>" if p.get('insight') else ""}
                      <div class='tags'>{tag_html}</div>
                    </article>
                    """
                )
            paper_cards.append("</div></section>")

        top3_html = ""
        if top3:
            top3_html = "<section class='section-block'><div class='section-head'><h2>今天最值得重点看的 3 篇</h2><span>编辑优先级</span></div><div class='top3'>" + "".join(f"<div class='top3-item'><span class='rank'>{i+1}</span><div class='top3-text'>{item}</div></div>" for i, item in enumerate(top3)) + "</div></section>"

        overall_html = ""
        if overall:
            overall_html = f"<section class='section-block'><div class='section-head'><h2>整体判断</h2><span>今日总评</span></div><div class='overall-card'>{esc(overall)}</div></section>"

        body = f"""
        <section class='hero card'>
          <div class='hero-copy'>
            <div class='eyebrow'>arXiv 每日摘要</div>
            <h1>{esc(data.get('job_name') or 'arXiv Daily')}</h1>
            <p class='lede'>聚焦自动驾驶、机械臂/操控、VLA 与 VLM。保留判断，不堆砌原始列表，适合手机快速浏览。</p>
            <div class='hero-badges'>
              {badge(f'{count} 篇论文', 'blue')}
              {badge('北京时间 8:00', 'green')}
              {badge('手机友好', 'violet')}
            </div>
          </div>
          <div class='hero-panel'>
            <div class='stat-card'>
              <span>Job ID</span>
              <strong>{esc(data.get('job_id'))}</strong>
            </div>
            <div class='stat-card'>
              <span>Run Time</span>
              <strong>{esc(data.get('run_time'))}</strong>
            </div>
            <div class='stat-card'>
              <span>Schedule</span>
              <strong>{esc(data.get('schedule'))}</strong>
            </div>
          </div>
        </section>

        <section class='section-block'>
          <div class='section-head'><h2>今日概览</h2><span>{count} 篇 · {len(sections)} 个主题块</span></div>
          <div class='section-chips'>
            {''.join(section_cards)}
          </div>
        </section>

        {top3_html}
        {overall_html}
        {''.join(paper_cards)}
        {source}
        """

    return f"""<!doctype html>
<html lang='zh-CN'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <meta http-equiv='refresh' content='300'>
  <title>arXiv 每日摘要</title>
  <link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap' rel='stylesheet'>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f5f4;
      --surface: rgba(255,255,255,.82);
      --surface-strong: rgba(255,255,255,.96);
      --text: rgba(0,0,0,.92);
      --muted: #615d59;
      --soft: #a39e98;
      --line: rgba(0,0,0,.09);
      --blue: #0075de;
      --blue-bg: #f2f9ff;
      --green: #1aae39;
      --violet: #6d4aff;
      --amber: #dd5b00;
      --rose: #ff64c8;
      --shadow: 0 24px 70px rgba(0,0,0,.08), 0 2px 10px rgba(0,0,0,.05);
      --shadow-soft: 0 10px 28px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
      --radius-xl: 28px;
      --radius-lg: 20px;
      --radius-md: 16px;
      --radius-sm: 12px;
      --max: 1180px;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(0,117,222,.14), transparent 26%),
        radial-gradient(circle at top right, rgba(109,74,255,.12), transparent 20%),
        linear-gradient(180deg, #fbfbfa 0%, var(--bg) 42%, #efeeec 100%);
      min-height: 100vh;
      line-height: 1.6;
      text-rendering: optimizeLegibility;
      -webkit-font-smoothing: antialiased;
    }}
    a {{ color: inherit; text-decoration: none; }}
    .page {{ max-width: var(--max); margin: 0 auto; padding: 24px 18px 56px; }}
    .topbar {{
      display: flex; justify-content: space-between; align-items: center;
      gap: 12px; margin-bottom: 18px; padding: 10px 14px;
      background: rgba(255,255,255,.55); backdrop-filter: blur(16px);
      border: 1px solid var(--line); border-radius: 999px; box-shadow: 0 8px 24px rgba(0,0,0,.04);
      position: sticky; top: 12px; z-index: 5;
    }}
    .brand {{ display: flex; align-items: center; gap: 10px; font-weight: 800; letter-spacing: -.02em; }}
    .brand-mark {{ width: 12px; height: 12px; border-radius: 50%; background: linear-gradient(135deg, var(--blue), var(--violet)); box-shadow: 0 0 0 6px rgba(0,117,222,.1); }}
    .topbar .hint {{ color: var(--muted); font-size: .92rem; }}
    .hero {{
      display: grid; grid-template-columns: 1.45fr .9fr; gap: 18px;
      padding: 28px; border-radius: var(--radius-xl);
      background: linear-gradient(180deg, rgba(255,255,255,.86), rgba(255,255,255,.72));
      border: 1px solid rgba(255,255,255,.8); box-shadow: var(--shadow);
    }}
    .eyebrow {{
      display: inline-flex; align-items: center; gap: 8px;
      font-size: .78rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase;
      color: var(--blue); background: var(--blue-bg); padding: 8px 12px; border-radius: 999px;
      margin-bottom: 14px;
    }}
    h1 {{ margin: 0; font-size: clamp(2rem, 4vw, 3.4rem); line-height: 1.02; letter-spacing: -0.045em; }}
    .lede {{ margin: 14px 0 0; max-width: 58ch; color: var(--muted); font-size: 1.02rem; }}
    .hero-badges {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }}
    .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 7px 12px; font-size: .86rem; font-weight: 700; border: 1px solid transparent; }}
    .badge.blue {{ color: #0056a8; background: var(--blue-bg); border-color: rgba(0,117,222,.12); }}
    .badge.green {{ color: #107426; background: rgba(26,174,57,.10); }}
    .badge.violet {{ color: #5234d6; background: rgba(109,74,255,.10); }}
    .badge.soft {{ color: var(--muted); background: rgba(255,255,255,.78); border-color: var(--line); }}
    .hero-panel {{ display: grid; gap: 12px; align-content: start; }}
    .stat-card, .card, .paper-card, .overall-card {{
      background: var(--surface);
      border: 1px solid rgba(255,255,255,.8);
      box-shadow: var(--shadow-soft);
      backdrop-filter: blur(16px);
    }}
    .stat-card {{ padding: 16px; border-radius: var(--radius-md); }}
    .stat-card span {{ display: block; color: var(--muted); font-size: .82rem; margin-bottom: 6px; }}
    .stat-card strong {{ font-size: 1rem; line-height: 1.35; }}
    .section-block {{ margin-top: 22px; }}
    .section-head {{
      display: flex; justify-content: space-between; align-items: baseline; gap: 10px;
      margin-bottom: 12px; padding: 0 4px;
    }}
    .section-head h2 {{ margin: 0; font-size: 1.15rem; letter-spacing: -.02em; }}
    .section-head span {{ color: var(--muted); font-size: .92rem; }}
    .section-chips {{ display: flex; flex-wrap: wrap; gap: 10px; }}
    .section-chip {{
      flex: 1 1 220px; display: flex; justify-content: space-between; gap: 12px;
      align-items: center; padding: 14px 16px; border-radius: 18px; background: rgba(255,255,255,.72);
      border: 1px solid var(--line); box-shadow: 0 8px 24px rgba(0,0,0,.03);
    }}
    .section-chip strong {{ font-size: .98rem; line-height: 1.35; }}
    .section-chip span {{ color: var(--muted); font-size: .9rem; white-space: nowrap; }}
    .section-chip.blue {{ background: linear-gradient(180deg, rgba(242,249,255,.95), rgba(255,255,255,.82)); }}
    .section-chip.green {{ background: linear-gradient(180deg, rgba(243,253,246,.95), rgba(255,255,255,.82)); }}
    .section-chip.violet {{ background: linear-gradient(180deg, rgba(245,242,255,.95), rgba(255,255,255,.82)); }}
    .section-chip.amber {{ background: linear-gradient(180deg, rgba(255,247,240,.95), rgba(255,255,255,.82)); }}
    .section-chip.rose {{ background: linear-gradient(180deg, rgba(255,244,251,.95), rgba(255,255,255,.82)); }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .paper-card {{
      padding: 18px; border-radius: var(--radius-lg); position: relative; overflow: hidden;
      background: linear-gradient(180deg, rgba(255,255,255,.96), rgba(255,255,255,.84));
    }}
    .paper-card::before {{
      content: ''; position: absolute; left: 0; top: 0; right: 0; height: 3px;
      background: linear-gradient(90deg, var(--blue), var(--violet), var(--rose));
      opacity: .9;
    }}
    .paper-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; margin-bottom: 10px; }}
    .paper-title {{ font-size: 1.02rem; font-weight: 800; line-height: 1.35; letter-spacing: -.02em; }}
    .paper-title a:hover {{ color: var(--blue); }}
    .arxiv-link {{
      flex: none; font-size: .78rem; font-weight: 800; color: var(--blue);
      background: var(--blue-bg); border: 1px solid rgba(0,117,222,.12);
      padding: 6px 10px; border-radius: 999px;
    }}
    .paper-meta {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 12px 0; }}
    .paper-meta div {{
      padding: 10px 11px; border-radius: 14px; background: rgba(255,255,255,.74); border: 1px solid var(--line);
    }}
    .paper-meta span {{ display: block; font-size: .78rem; color: var(--muted); margin-bottom: 2px; }}
    .paper-meta strong {{ font-size: .88rem; font-weight: 700; line-height: 1.45; }}
    .insight {{ margin: 10px 0 0; color: var(--text); font-size: .95rem; }}
    .tags {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
    .top3 {{ display: grid; gap: 10px; }}
    .top3-item {{
      display: grid; grid-template-columns: 34px 1fr; gap: 12px; align-items: start;
      padding: 14px 16px; border-radius: 16px; background: rgba(255,255,255,.76);
      border: 1px solid var(--line);
    }}
    .rank {{
      width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center;
      background: linear-gradient(135deg, var(--blue), var(--violet)); color: white; font-weight: 800;
    }}
    .top3-text {{ color: var(--text); font-size: .96rem; line-height: 1.5; }}
    .overall-card {{ padding: 18px; border-radius: 18px; color: var(--muted); font-size: .98rem; line-height: 1.7; }}
    .source {{ margin-top: 22px; color: var(--soft); font-size: .86rem; text-align: center; }}
    .hero-empty {{ display: block; }}
    @media (max-width: 900px) {{
      .hero, .grid {{ grid-template-columns: 1fr; }}
      .paper-meta {{ grid-template-columns: 1fr; }}
      .topbar {{ position: static; border-radius: 20px; }}
    }}
    @media (max-width: 560px) {{
      .page {{ padding: 14px 12px 40px; }}
      .hero {{ padding: 20px; border-radius: 22px; }}
      .paper-card {{ padding: 16px; }}
      h1 {{ font-size: 2.05rem; }}
      .section-chip {{ flex-basis: 100%; }}
      .section-head {{ flex-direction: column; align-items: start; }}
    }}
  </style>
</head>
<body>
  <div class='page'>
    <header class='topbar'>
      <div class='brand'><span class='brand-mark'></span>arXiv 每日摘要</div>
      <div class='hint'>自动刷新 · 手机友好 · 精选判断</div>
    </header>
    {body}
  </div>
</body>
</html>"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    html_text = render()
    OUT_FILE.write_text(html_text, encoding="utf-8")
    ROOT_FILE.write_text(html_text, encoding="utf-8")
    print(f"Wrote {OUT_FILE}")
    print(f"Wrote {ROOT_FILE}")


if __name__ == "__main__":
    main()
