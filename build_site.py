#!/usr/bin/env python3
from __future__ import annotations

import base64
import html
import io
import json
import re
import tarfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import fitz
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

JOB_ID = "054d56957dbb"
SITE_DIR = Path(__file__).resolve().parent
SRC_DIR = Path.home() / ".hermes" / "cron" / "output" / JOB_ID
PREVIEW_DIR = SITE_DIR / "assets" / "previews"
PREVIEW_CACHE_VERSION = "v4"

SECTION_RE = re.compile(r"^##\s+(.*?)\s*$")
PAPER_RE = re.compile(r"^\d+\.\s+\*\*(.*?)\*\*\s*$")
ARXIV_RE = re.compile(r"^\s*-\s+\*\*arXiv:\*\*\s+(.*?)\s*$")
TIME_RE = re.compile(r"^\s*-\s+\*\*发布时间（北京时间）：\*\*\s+(.*?)\s*$")
TAGS_RE = re.compile(r"^\s*-\s+\*\*标签：\*\*\s+(.*?)\s*$")
INSIGHT_RE = re.compile(r"^\s*-\s+\*\*看点：\*\*\s+(.*?)\s*$")
SUMMARY_RE = re.compile(r"^\s*-\s+\*\*摘要总结：\*\*\s+(.*?)\s*$")
IMPL_RE = re.compile(r"^\s*-\s+\*\*实现概率：\*\*\s+(.*?)\s*$")
OPEN_RE = re.compile(r"^\s*-\s+\*\*开源：\*\*\s+(.*?)\s*$")
DEPLOY_RE = re.compile(r"^\s*-\s+\*\*可部署：\*\*\s+(.*?)\s*$")

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+\-]{1,}|[\u4e00-\u9fff]{2,6}")
NUMBER_RE = re.compile(r"(\d{1,3})\s*%")

STOPWORDS = {
    "论文", "方法", "模型", "系统", "任务", "研究", "网络", "数据", "结果", "实验", "实验室",
    "提出", "通过", "使用", "基于", "进行", "对于", "以及", "相关", "一种", "这个", "那个", "他们",
    "我们", "可以", "能够", "更", "很", "在于", "为了", "从而", "包括", "同时", "不同", "里面",
    "基线", "提升", "改善", "效果", "性能", "实现", "学习", "训练", "推理", "生成", "表示",
    "method", "methods", "model", "system", "task", "tasks", "paper", "papers", "based", "using",
    "with", "for", "the", "and", "or", "via", "towards", "toward", "better", "new", "our", "their",
    "robot", "robotics", "vision", "language", "video", "image", "multi", "multimodal", "benchmark",
    "an", "in", "on", "of", "to", "from", "by", "into", "at", "as", "is", "are", "be", "this", "that",
    "with", "without", "through", "across", "between", "after", "before", "under", "over", "during",
}

TERMS_NORMALIZE = {
    "vlm": "VLM",
    "vla": "VLA",
    "llm": "LLM",
    "rl": "RL",
    "mpc": "MPC",
    "bev": "BEV",
    "ocr": "OCR",
    "sim2real": "sim2real",
    "sim-to-real": "sim2real",
    "real world": "real-world",
    "real-world": "real-world",
    "mobile manipulation": "mobile manipulation",
}


def normalize_status(text: str | None) -> str:
    s = (text or "").strip()
    if not s:
        return "unknown"
    low = s.lower()
    if s.startswith(("是", "yes", "可", "open")) or "open-source" in low or "open source" in low:
        return "yes"
    if s.startswith(("否", "no", "不", "un", "uncertain")):
        return "uncertain" if "不确定" in s or "uncertain" in low or "maybe" in low else "no"
    return "uncertain"


def parse_percent(text: str | None) -> int:
    if not text:
        return 0
    m = NUMBER_RE.search(text)
    return int(m.group(1)) if m else 0


def normalize_term(term: str) -> str:
    raw = term.strip().strip("/|，,。；;:：()[]{}<>\"'“”‘’`~").lower()
    if not raw:
        return ""
    return TERMS_NORMALIZE.get(raw, term.strip().strip("/|，,。；;:：()[]{}<>\"'“”‘’`~"))


def extract_terms(text: str | None) -> list[str]:
    if not text:
        return []
    terms: list[str] = []
    for token in TOKEN_RE.findall(text):
        norm = normalize_term(token)
        if not norm:
            continue
        low = norm.lower()
        if low in STOPWORDS:
            continue
        if len(norm) < 2 and not norm.isupper():
            continue
        if norm.isdigit():
            continue
        terms.append(norm)
    return terms


def compact_terms(text: str | None, limit: int = 4) -> list[str]:
    seen: list[str] = []
    for term in extract_terms(text):
        if term not in seen:
            seen.append(term)
        if len(seen) >= limit:
            break
    return seen


def make_search_text(*parts: str | None) -> str:
    return " ".join(p for p in parts if p).lower()



def svg_data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")



def strip_arxiv_version(arxiv_id: str | None) -> str:
    base = (arxiv_id or "").strip()
    return re.sub(r"v\d+$", "", base)



def safe_preview_name(arxiv_id: str | None) -> str:
    base = strip_arxiv_version(arxiv_id)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base) or "paper"


def read_preview_meta(meta_path: Path) -> str | None:
    if not meta_path.exists():
        return None
    raw = meta_path.read_text(encoding="utf-8", errors="ignore").strip()
    if raw.startswith(f"{PREVIEW_CACHE_VERSION}|"):
        return raw.split("|", 1)[1].strip() or "已缓存"
    return None


def write_preview_meta(meta_path: Path, source: str) -> None:
    meta_path.write_text(f"{PREVIEW_CACHE_VERSION}|{source}", encoding="utf-8")



def fetch_url_bytes(url: str, timeout: int = 60) -> bytes:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()



def render_png_from_image_bytes(data: bytes) -> bytes | None:
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            else:
                img = img.convert("RGB")
            max_size = (1100, 720)
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", img.size, "white")
            canvas.paste(img)
            buf = io.BytesIO()
            canvas.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    except Exception:
        return None



def render_png_from_pdf_bytes(data: bytes) -> bytes | None:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        if not doc.page_count:
            return None
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
        return pix.tobytes("png")
    except Exception:
        return None



def pad_preview_png(data: bytes, pad_x: int | None = None, pad_y: int | None = None) -> bytes | None:
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            img = img.convert("RGB")
            px = pad_x if pad_x is not None else max(48, img.width // 16)
            py = pad_y if pad_y is not None else max(32, img.height // 22)
            canvas = Image.new("RGB", (img.width + px * 2, img.height + py * 2), "white")
            canvas.paste(img, (px, py))
            buf = io.BytesIO()
            canvas.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
    except Exception:
        return None



def render_png_from_source_file(name: str, data: bytes) -> bytes | None:
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        rendered = render_png_from_pdf_bytes(data)
    elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
        rendered = render_png_from_image_bytes(data)
    elif data[:4] == b"%PDF":
        rendered = render_png_from_pdf_bytes(data)
    else:
        rendered = render_png_from_image_bytes(data)
    if not rendered:
        return None
    padded = pad_preview_png(rendered)
    return padded or rendered



def png_quality_score(data: bytes) -> tuple[int, int, int, float]:
    if Image is None:
        return (-10_000, 0, 0, 0.0)
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except Exception:
        return (-10_000, 0, 0, 0.0)
    if width <= 0 or height <= 0:
        return (-10_000, width, height, 0.0)
    area = width * height
    aspect = width / height
    score = 0
    score += min(area // 22_000, 120)
    if 0.8 <= aspect <= 2.2:
        score += 28
    elif 0.55 <= aspect <= 3.2:
        score += 10
    else:
        score -= 24
    if width < 320 or height < 220:
        score -= 70
    if width >= 900 and height >= 500:
        score += 18
    return score, width, height, aspect



def infer_visual_keywords(*parts: str | None) -> set[str]:
    terms = {t.lower() for t in extract_terms(" ".join(p for p in parts if p))}
    keywords: set[str] = set()
    for term in terms:
        if len(term) < 3 and not term.isupper():
            continue
        keywords.add(term)
        if "drive" in term or "驾驶" in term:
            keywords.update({"driving", "autonomous", "planning", "bev", "lane", "map"})
        if "manip" in term or "grasp" in term or "机械臂" in term or "操控" in term:
            keywords.update({"manipulation", "grasp", "robot", "policy", "affordance"})
        if term.lower() in {"vla", "vlm", "llm", "rl", "mpc", "bev"}:
            keywords.add(term.lower())
    return keywords



def score_source_candidate(name: str, visual_keywords: set[str]) -> int:
    lower = name.lower()
    basename = Path(lower).name
    stem = Path(lower).stem
    score = 0
    primary_hits = {
        "teaser": 52,
        "framework": 50,
        "pipeline": 48,
        "architecture": 44,
        "overview": 42,
        "method": 34,
        "system": 30,
        "diagram": 30,
        "fig1": 28,
        "figure1": 28,
        "main": 24,
    }
    secondary_hits = {
        "figure": 18,
        "fig": 14,
        "network": 14,
        "model": 10,
        "approach": 10,
        "result": -6,
        "sample": -18,
        "samples": -18,
        "setup": -16,
        "compare": -10,
        "comparison": -10,
        "experiment": -10,
        "dataset": -12,
        "ablation": -18,
        "appendix": -28,
        "supp": -28,
        "supplement": -28,
        "table": -20,
        "logo": -24,
        "author": -12,
        "qualitative": -6,
        "quantitative": -6,
    }
    if any(seg in lower for seg in ("latex/", "tex/", "fig/", "figure/", "image/", "img/", "images/")):
        score += 24
    for token, weight in primary_hits.items():
        if token in basename or token in stem:
            score += weight
    for token, weight in secondary_hits.items():
        if token in lower:
            score += weight
    for keyword in visual_keywords:
        if keyword and keyword in lower:
            score += 9
    if len(basename) < 42:
        score += 6
    return score



def build_placeholder_preview(title: str, arxiv_id: str | None, note: str = "无法提取原图时的占位预览") -> bytes:
    if Image is None or ImageDraw is None or ImageFont is None:
        return b""
    img = Image.new("RGB", (1200, 760), (244, 249, 255))
    draw = ImageDraw.Draw(img)
    try:
        font_big = ImageFont.load_default()
        font_med = ImageFont.load_default()
        font_small = ImageFont.load_default()
    except Exception:
        font_big = font_med = font_small = None

    draw.rounded_rectangle((28, 28, 1172, 732), radius=28, fill=(255, 255, 255), outline=(225, 230, 238), width=2)
    draw.rounded_rectangle((56, 56, 180, 92), radius=18, fill=(240, 247, 255), outline=None)
    draw.text((72, 65), "framework", fill=(0, 86, 168), font=font_small)
    draw.text((56, 136), (title or "未命名论文")[:48], fill=(12, 41, 66), font=font_big)
    draw.text((56, 182), (arxiv_id or "")[:40], fill=(109, 103, 97), font=font_med)
    draw.text((56, 236), note, fill=(109, 103, 97), font=font_med)

    box_y = 314
    draw.rounded_rectangle((56, box_y, 1144, 654), radius=24, fill=(248, 251, 255), outline=(234, 239, 246), width=1)
    stages = [
        ((88, 356, 298, 466), (242, 249, 255), (0, 117, 222), "输入", "论文元信息"),
        ((340, 356, 550, 466), (246, 242, 255), (74, 47, 208), "理解", "抽取视觉/文本特征"),
        ((592, 356, 802, 466), (243, 251, 245), (17, 102, 45), "判断", "实现/开源/部署"),
        ((844, 356, 1054, 466), (255, 247, 239), (169, 75, 0), "输出", "阅读导航框架图"),
    ]
    for i, (box, fill, accent, head, sub) in enumerate(stages):
        draw.rounded_rectangle(box, radius=20, fill=fill, outline=accent, width=2)
        cx = (box[0] + box[2]) // 2
        draw.text((cx - 18, box[1] + 18), head, fill=accent, font=font_med, anchor="ma")
        draw.text((cx - 18, box[1] + 56), sub, fill=(92, 107, 123), font=font_small, anchor="ma")
        if i < len(stages) - 1:
            x0 = box[2] + 10
            x1 = x0 + 16
            y = box[1] + 55
            draw.line((x0, y, x1, y), fill=(0, 0, 0, 40), width=4)
            draw.polygon([(x1 - 2, y - 8), (x1 + 8, y), (x1 - 2, y + 8)], fill=(0, 0, 0, 40))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()



def generate_paper_preview(
    arxiv_id: str | None,
    title: str | None,
    tags: str | None = None,
    insight: str | None = None,
    summary: str | None = None,
    section_title: str | None = None,
) -> tuple[str, str]:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = safe_preview_name(arxiv_id)
    preview_path = PREVIEW_DIR / f"{safe_name}.png"
    meta_path = PREVIEW_DIR / f"{safe_name}.source"
    if preview_path.exists() and preview_path.stat().st_size > 0:
        source = read_preview_meta(meta_path)
        if source:
            return preview_path.name, source

    if Image is None:
        fallback_source = read_preview_meta(PREVIEW_DIR / "paper.source") or "占位预览 · 环境缺少 Pillow"
        return "paper.png", fallback_source

    base_id = strip_arxiv_version(arxiv_id)
    if not base_id:
        png = build_placeholder_preview(title or "未命名论文", arxiv_id, "缺少 arXiv 编号")
        if png:
            preview_path.write_bytes(png)
            write_preview_meta(meta_path, "占位预览 · 缺少 arXiv 编号")
        return preview_path.name, "占位预览 · 缺少 arXiv 编号"

    visual_keywords = infer_visual_keywords(title, tags, insight, summary, section_title)

    # 1) 优先尝试源代码包里的原图，并结合尺寸/比例/关键词做更细的排序
    source_urls = [f"https://arxiv.org/e-print/{base_id}", f"https://arxiv.org/src/{base_id}"]
    candidate_bytes = None
    candidate_source = None
    candidate_score = -10_000
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".pdf", ".eps"}
    for src_url in source_urls:
        try:
            src_data = fetch_url_bytes(src_url, timeout=45)
            with tarfile.open(fileobj=io.BytesIO(src_data), mode="r:*") as tf:
                ranked: list[tuple[int, str, bytes]] = []
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    name = member.name
                    suffix = Path(name.lower()).suffix
                    if suffix not in image_suffixes:
                        continue
                    try:
                        blob = tf.extractfile(member).read()  # type: ignore[union-attr]
                    except Exception:
                        continue
                    ranked.append((score_source_candidate(name, visual_keywords), name, blob))
                if ranked:
                    ranked.sort(key=lambda x: x[0], reverse=True)
                    for base_score, name, blob in ranked[:12]:
                        png = render_png_from_source_file(name, blob)
                        if not png:
                            continue
                        quality_score, width, height, aspect = png_quality_score(png)
                        total_score = base_score + quality_score
                        if width >= 900 and 0.95 <= aspect <= 2.1:
                            total_score += 14
                        if total_score > candidate_score:
                            candidate_score = total_score
                            candidate_bytes = png
                            candidate_source = f"源代码原图 · {Path(name).name} · {width}×{height}"
        except Exception:
            continue
        if candidate_score >= 120:
            break

    # 2) 回退到 PDF 首页
    if candidate_bytes is None:
        pdf_urls = [f"https://arxiv.org/pdf/{base_id}.pdf", f"https://arxiv.org/pdf/{base_id}"]
        for pdf_url in pdf_urls:
            try:
                pdf_data = fetch_url_bytes(pdf_url, timeout=50)
                png = render_png_from_pdf_bytes(pdf_data)
                if png:
                    _, width, height, _ = png_quality_score(png)
                    candidate_bytes = png
                    candidate_source = f"PDF 首页截取 · {width}×{height}"
                    break
            except Exception:
                continue

    if candidate_bytes is None:
        candidate_bytes = build_placeholder_preview(title or "未命名论文", arxiv_id, "原图和 PDF 截图都未获取到") or b""
        candidate_source = "占位预览 · 未获取到原图/PDF"

    if candidate_bytes:
        preview_path.write_bytes(candidate_bytes)
        write_preview_meta(meta_path, candidate_source or "已生成")
    return preview_path.name, candidate_source or "已生成"



def build_wordcloud_svg(top_terms: list[tuple[str, int]], window_label: str) -> str:
    width, height = 1200, 420
    safe_label = html.escape(window_label or "最近 7 天", quote=True)
    if not top_terms:
        svg = f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' role='img' aria-label='最近 7 天主题词云'>
  <defs>
    <linearGradient id='bg' x1='0' y1='0' x2='1' y2='1'>
      <stop offset='0%' stop-color='#f6fbff'/>
      <stop offset='100%' stop-color='#fff8fb'/>
    </linearGradient>
  </defs>
  <rect width='100%' height='100%' rx='28' fill='url(#bg)'/>
  <rect x='26' y='26' width='{width-52}' height='{height-52}' rx='24' fill='rgba(255,255,255,.72)' stroke='rgba(0,0,0,.08)'/>
  <text x='58' y='86' font-size='34' font-family='Inter, Arial, sans-serif' font-weight='800' fill='#0c2942'>最近 7 天主题词云</text>
  <text x='58' y='128' font-size='18' font-family='Inter, Arial, sans-serif' fill='#6d6761'>{safe_label} · 暂无足够词频数据</text>
</svg>"""
        return svg_data_uri(svg)

    max_term = max((count for _, count in top_terms), default=1)
    palette = ["#0056a8", "#4a2fd0", "#11662d", "#a94b00", "#c13f8f"]
    items = []
    x, y = 54, 120
    row_height = 0
    max_x = width - 54
    display_terms = top_terms[:18]
    for idx, (term, count) in enumerate(display_terms):
        scale = 0.95 + (count / max_term) * 0.92 if max_term else 1.0
        font = round(24 + (scale - 0.95) * 14)
        pad_x = round(font * 0.58)
        pad_y = round(font * 0.44)
        est_w = max(92, round(len(term) * font * 0.64) + pad_x * 2 + 34)
        box_h = font + pad_y * 2 + 8
        if x + est_w > max_x:
            x = 54
            y += row_height + 18
            row_height = 0
        if y + box_h > height - 42:
            break
        tone = palette[idx % len(palette)]
        fill = ["#f2f9ff", "#f6f2ff", "#f3fbf5", "#fff7ef", "#fff2f8"][idx % 5]
        items.append(
            f"""
    <g transform='translate({x},{y})'>
      <rect x='0' y='0' rx='18' ry='18' width='{est_w}' height='{box_h}' fill='{fill}' stroke='rgba(0,0,0,.08)'/>
      <text x='{pad_x}' y='{box_h - pad_y - 6}' font-size='{font}' font-family='Inter, Arial, sans-serif' font-weight='800' fill='{tone}'>{html.escape(term, quote=True)}</text>
      <text x='{est_w - 14}' y='{box_h - 12}' text-anchor='end' font-size='15' font-family='Inter, Arial, sans-serif' font-weight='700' fill='#7b746d'>{count}</text>
    </g>"""
        )
        x += est_w + 14
        row_height = max(row_height, box_h)
    if not items:
        items.append(f"<text x='58' y='170' font-size='20' font-family='Inter, Arial, sans-serif' fill='#6d6761'>词频不足，先看主题卡片。</text>")
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' role='img' aria-label='最近 7 天主题词云'>
  <defs>
    <linearGradient id='bg' x1='0' y1='0' x2='1' y2='1'>
      <stop offset='0%' stop-color='#f4f9ff'/>
      <stop offset='100%' stop-color='#fff7fb'/>
    </linearGradient>
    <filter id='shadow' x='-20%' y='-20%' width='140%' height='140%'>
      <feDropShadow dx='0' dy='8' stdDeviation='12' flood-color='rgba(0,0,0,.12)'/>
    </filter>
  </defs>
  <rect width='100%' height='100%' rx='28' fill='url(#bg)'/>
  <rect x='24' y='24' width='{width-48}' height='{height-48}' rx='24' fill='rgba(255,255,255,.78)' stroke='rgba(0,0,0,.08)' filter='url(#shadow)'/>
  <text x='58' y='86' font-size='34' font-family='Inter, Arial, sans-serif' font-weight='800' fill='#0c2942'>最近 7 天主题词云</text>
  <text x='58' y='128' font-size='18' font-family='Inter, Arial, sans-serif' fill='#6d6761'>{safe_label}</text>
  {''.join(items)}
</svg>"""
    return svg_data_uri(svg)



def build_framework_svg(run: dict, weekly: dict | None = None) -> str:
    width, height = 1200, 280
    sections = [sec.get("title") for sec in (run.get("sections") or []) if sec.get("title")]
    sec_label = " / ".join(sections[:4]) if sections else "自动驾驶 / 机械臂 / VLA / VLM"
    sec_label = sec_label if len(sec_label) <= 40 else sec_label[:38] + "…"
    weekly = weekly or {}
    summary = f"{weekly.get('paper_count') or run.get('paper_count') or 0} 篇 · {weekly.get('window_label') or '最近 7 天'}"
    steps = [
        ("输入", "抓取 arXiv 日报 / 论文元信息", "标题、标签、看点、摘要"),
        ("理解", "提炼主题并聚合词频", sec_label),
        ("判断", "评估实现概率 / 开源 / 可部署", "生成可读的阅读判断"),
        ("输出", "Top 3 · 词云 · 主题卡片 · 归档", summary),
    ]
    boxes = []
    gap = 28
    box_w = 250
    start_x = 38
    y = 74
    for idx, (head, title, desc) in enumerate(steps):
        x = start_x + idx * (box_w + gap)
        tone = ["#0056a8", "#4a2fd0", "#11662d", "#a94b00"][idx]
        fill = ["#f2f9ff", "#f6f2ff", "#f3fbf5", "#fff7ef"][idx]
        arrow = "" if idx == len(steps) - 1 else f"<path d='M {x + box_w + 10} 139 H {x + box_w + gap - 8}' stroke='rgba(0,0,0,.2)' stroke-width='4' stroke-linecap='round'/><path d='M {x + box_w + gap - 18} 129 L {x + box_w + gap - 8} 139 L {x + box_w + gap - 18} 149' fill='none' stroke='rgba(0,0,0,.2)' stroke-width='4' stroke-linecap='round' stroke-linejoin='round'/>"
        boxes.append(
            f"""
    <g transform='translate({x}, {y})'>
      <rect width='{box_w}' height='132' rx='22' fill='{fill}' stroke='rgba(0,0,0,.08)'/>
      <rect x='18' y='18' width='64' height='28' rx='14' fill='{tone}' opacity='.12'/>
      <text x='50' y='38' text-anchor='middle' font-size='16' font-family='Inter, Arial, sans-serif' font-weight='800' fill='{tone}'>{head}</text>
      <text x='18' y='72' font-size='20' font-family='Inter, Arial, sans-serif' font-weight='800' fill='#0c2942'>{html.escape(title, quote=True)}</text>
      <text x='18' y='101' font-size='15' font-family='Inter, Arial, sans-serif' fill='#6d6761'>{html.escape(desc, quote=True)}</text>
    </g>{arrow}"""
        )
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' role='img' aria-label='论文阅读框架图'>
  <defs>
    <linearGradient id='bg' x1='0' y1='0' x2='1' y2='1'>
      <stop offset='0%' stop-color='#f8fbff'/>
      <stop offset='100%' stop-color='#fff8fa'/>
    </linearGradient>
    <filter id='shadow' x='-20%' y='-20%' width='140%' height='140%'>
      <feDropShadow dx='0' dy='8' stdDeviation='12' flood-color='rgba(0,0,0,.10)'/>
    </filter>
  </defs>
  <rect width='100%' height='100%' rx='28' fill='url(#bg)'/>
  <rect x='24' y='24' width='{width-48}' height='{height-48}' rx='24' fill='rgba(255,255,255,.80)' stroke='rgba(0,0,0,.08)' filter='url(#shadow)'/>
  <text x='42' y='58' font-size='30' font-family='Inter, Arial, sans-serif' font-weight='800' fill='#0c2942'>论文阅读框架图</text>
  <text x='42' y='88' font-size='16' font-family='Inter, Arial, sans-serif' fill='#6d6761'>从输入到判断，帮助你先抓重点，再点开细节</text>
  {''.join(boxes)}
</svg>"""
    return svg_data_uri(svg)



def esc(text: str | None) -> str:
    return html.escape(text or "")


def latest_markdowns() -> list[Path]:
    if not SRC_DIR.exists():
        return []
    candidates = sorted(SRC_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    valid: list[Path] = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # 只接受真正包含论文正文的输出；避免把异常的技能提示/空跑结果当成最新日报
        if "## 自动驾驶" in text and "- **看点：**" in text and "- **摘要总结：**" in text:
            valid.append(path)
    return valid or candidates


def parse_markdown(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    data = {
        "path": path,
        "slug": path.stem,
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
            current_paper = None
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
            current_paper = {"title": pm.group(1), "arxiv": None, "time": None, "tags": None, "insight": None, "summary": None, "impl": None, "open": None, "deploy": None}
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
        elif (sm := SUMMARY_RE.match(line)):
            current_paper["summary"] = sm.group(1)
        elif (pm2 := IMPL_RE.match(line)):
            current_paper["impl"] = pm2.group(1)
        elif (om := OPEN_RE.match(line)):
            current_paper["open"] = om.group(1)
        elif (dm := DEPLOY_RE.match(line)):
            current_paper["deploy"] = dm.group(1)

    data["run_time_dt"] = None
    if data.get("run_time"):
        try:
            data["run_time_dt"] = datetime.strptime(data["run_time"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    data["paper_count"] = sum(len(sec["papers"]) for sec in data["sections"])
    return data


def all_runs() -> list[dict]:
    return [parse_markdown(p) for p in latest_markdowns()]


def collect_weekly_stats(runs: list[dict], window_size: int = 7) -> dict:
    window = runs[:window_size]
    term_counts: Counter[str] = Counter()
    section_counts: Counter[str] = Counter()
    impl_values: list[int] = []
    open_yes = 0
    deploy_yes = 0
    paper_count = 0
    for run in window:
        for section in run.get("sections", []):
            section_counts[section["title"]] += len(section.get("papers", []))
            for paper in section.get("papers", []):
                paper_count += 1
                impl_values.append(parse_percent(paper.get("impl")))
                if normalize_status(paper.get("open")) == "yes":
                    open_yes += 1
                if normalize_status(paper.get("deploy")) == "yes":
                    deploy_yes += 1
                weighted = Counter()
                for text, weight in (
                    (paper.get("title"), 4),
                    (paper.get("tags"), 3),
                    (paper.get("insight"), 2),
                    (paper.get("summary"), 2),
                    (section.get("title"), 2),
                ):
                    for term in extract_terms(text):
                        weighted[term] += weight
                term_counts.update(weighted)
    total_impl = sum(impl_values)
    impl_avg = round(total_impl / len(impl_values)) if impl_values else 0
    run_dates = [r.get("run_time_dt") for r in window if r.get("run_time_dt")]
    start = min(run_dates).strftime("%Y-%m-%d") if run_dates else None
    end = max(run_dates).strftime("%Y-%m-%d") if run_dates else None
    return {
        "window_runs": window,
        "paper_count": paper_count,
        "term_counts": term_counts,
        "section_counts": section_counts,
        "impl_avg": impl_avg,
        "open_yes": open_yes,
        "deploy_yes": deploy_yes,
        "window_label": f"{start} — {end}" if start and end else "最近 7 天",
        "top_terms": term_counts.most_common(18),
        "top_sections": section_counts.most_common(8),
    }

def build_run_detail(
    run: dict,
    weekly: dict | None = None,
    home_link: str = "index.html",
    archive_link: str = "archive/index.html",
    asset_prefix: str = "",
) -> str:
    weekly = weekly or {}
    counts = [f"{sec['title']} {len(sec['papers'])}" for sec in run["sections"]]
    chips = "".join(f"<button type='button' class='rail-chip' data-section='{esc(sec['title'])}'>{esc(sec['title'])}<span>{len(sec['papers'])}</span></button>" for sec in run["sections"])
    top3_html = "".join(f"<div class='top3-item'><span class='rank'>{i+1}</span><div class='top3-text'>{esc(item)}</div></div>" for i, item in enumerate(run.get("top3", [])))
    top_terms = weekly.get("top_terms") or []
    max_term = max((count for _, count in top_terms), default=1)
    cloud_html = []
    for idx, (term, count) in enumerate(top_terms):
        scale = 0.9 + (count / max_term) * 0.95 if max_term else 1.0
        tone = ["blue", "violet", "green", "amber", "rose"][idx % 5]
        cloud_html.append(
            f"<button type='button' class='cloud-chip {tone}' data-term='{esc(term)}' style='--scale:{scale:.2f};'><span>{esc(term)}</span><em>{count}</em></button>"
        )
    cloud_html = "".join(cloud_html) or "<div class='cloud-empty'>最近 7 天暂无足够词频数据。</div>"
    window_label = weekly.get("window_label") or "最近 7 天"
    weekly_total = weekly.get("paper_count") or 0
    weekly_impl = weekly.get("impl_avg") or 0
    weekly_open = weekly.get("open_yes") or 0
    weekly_deploy = weekly.get("deploy_yes") or 0
    cloud_svg = build_wordcloud_svg(top_terms, window_label)
    framework_svg = build_framework_svg(run, weekly)
    overall_note = run.get("overall") or "今天的内容整体偏向系统化、工程化与可落地方法，适合先看 Top 3，再按主题筛选阅读。"
    parts = []
    parts.append(
        f"""
        <section class='hero card'>
          <div class='hero-copy'>
            <div class='eyebrow'>arXiv 每日摘要</div>
            <h1>{esc(run.get('job_name') or 'arXiv Daily')}</h1>
            <p class='lede'>聚焦自动驾驶、机械臂 / 操控、VLA 与 VLM。首页按手机优先设计：先看总览，再看词云和主题卡片，最后展开具体论文。</p>
            <div class='hero-badges'>
              <a class='badge blue' href='{home_link}'>回到首页</a>
              <a class='badge violet' href='{archive_link}'>历史归档</a>
              <span class='badge green'>{run['paper_count']} 篇论文</span>
              <span class='badge soft'>7 天窗口：{esc(window_label)}</span>
            </div>
          </div>
          <div class='hero-panel'>
            <div class='stat-card'><span>Job ID</span><strong>{esc(run.get('job_id'))}</strong></div>
            <div class='stat-card'><span>Run Time</span><strong>{esc(run.get('run_time'))}</strong></div>
            <div class='stat-card'><span>Schedule</span><strong>{esc(run.get('schedule'))}</strong></div>
            <div class='stat-card'><span>本周论文</span><strong>{weekly_total}</strong></div>
            <div class='stat-card'><span>平均实现概率</span><strong>{weekly_impl}%</strong></div>
            <div class='stat-card'><span>开源 / 可部署</span><strong>{weekly_open} / {weekly_deploy}</strong></div>
          </div>
        </section>
        """
    )
    parts.append(
        f"""
        <section class='section-block'>
          <div class='section-head'><h2>快速筛选</h2><span>点词云、点主题，手机上直接筛选</span></div>
          <div class='search-bar'>
            <input id='paper-search' type='search' placeholder='搜索标题、标签、摘要关键词…' autocomplete='off'>
            <button type='button' class='paper-action' data-filter='clear'>清除</button>
          </div>
          <div class='filter-rail'>
            <button type='button' class='rail-chip active' data-filter='all'>全部</button>
            <button type='button' class='rail-chip' data-filter='high'>高实现概率</button>
            <button type='button' class='rail-chip' data-filter='deploy'>可部署</button>
            <button type='button' class='rail-chip' data-filter='open'>开源</button>
            <button type='button' class='rail-chip' data-filter='paper'>论文</button>
          </div>
          <div class='section-rail'>{chips}</div>
        </section>
        """
    )
    if top3_html:
        parts.append(
            f"""
            <section class='section-block' id='top3'>
              <div class='section-head'><h2>今天最值得重点看的 3 篇</h2><span>编辑优先级</span></div>
              <div class='top3'>{top3_html}</div>
            </section>
            """
        )
    parts.append(
        f"""
        <section class='section-block' id='week-cloud'>
          <div class='section-head'><h2>最近 7 天主题词云</h2><span>{esc(window_label)} · {weekly_total} 篇 · {weekly.get('term_counts') and len(weekly.get('term_counts')) or 0} 个词</span></div>
          <div class='weekly-grid'>
            <div class='figure-card cloud-figure'>
              <div class='figure-head'><strong>词云图</strong><span>可视化最近 7 天词频</span></div>
              <img class='figure-image' src='{cloud_svg}' alt='最近 7 天主题词云'>
              <div class='figure-caption'>这是一张真正生成出来的主题词云图。下面的词条仍然可以点击筛选论文，图和交互同时保留。</div>
            </div>
            <div class='weekly-stats'>
              <div class='stat-card'><span>窗口长度</span><strong>{esc(window_label)}</strong></div>
              <div class='stat-card'><span>累计论文</span><strong>{weekly_total}</strong></div>
              <div class='stat-card'><span>高频词数量</span><strong>{len(top_terms)}</strong></div>
              <div class='stat-card'><span>平均实现概率</span><strong>{weekly_impl}%</strong></div>
              <div class='stat-card'><span>开源 / 可部署</span><strong>{weekly_open} / {weekly_deploy}</strong></div>
              <div class='stat-card'><span>周判断</span><strong>{'偏工程化、值得看' if weekly_impl >= 75 else '偏研究型、择优看'}</strong></div>
            </div>
          </div>
          <div style='margin-top:12px;'>{cloud_html}</div>
        </section>
        """
    )
    parts.append(
        f"""
        <section class='section-block' id='framework'>
          <div class='section-head'><h2>阅读框架图</h2><span>先看流程，再看论文细节</span></div>
          <div class='figure-card framework-figure'>
            <div class='figure-head'>
              <div><strong>解读流程</strong><span>从输入到判断</span></div>
              <button type='button' class='paper-action preview-inline-action' data-preview-open='{framework_svg}' data-preview-title='每日阅读框架图' data-preview-source='阅读导航图 · 系统生成 SVG'>流程图</button>
            </div>
            <img class='figure-image figure-zoomable' src='{framework_svg}' alt='论文阅读框架图' data-preview-open='{framework_svg}' data-preview-title='每日阅读框架图' data-preview-source='阅读导航图 · 系统生成 SVG'>
            <div class='figure-caption'>这张框架图把每日阅读拆成四步：输入、理解、判断、输出。它不是论文原始结构图，而是你浏览这份日报时的阅读导航。</div>
          </div>
        </section>
        """
    )
    if run.get("overall"):
        parts.append(
            f"""
            <section class='section-block'>
              <div class='section-head'><h2>整体判断</h2><span>今日总评</span></div>
              <div class='overall-card'>{esc(overall_note)}</div>
            </section>
            """
        )
    for sec in run["sections"]:
        parts.append(
            f"<section class='section-block' data-section-group='{esc(sec['title'])}'><div class='section-head'><h2>{esc(sec['title'])}</h2><span>{len(sec['papers'])} 篇</span></div><div class='grid'>"
        )
        for p in sec["papers"]:
            arxiv_id = p.get("arxiv") or ""
            arxiv_url = f"https://arxiv.org/abs/{html.escape(arxiv_id)}" if arxiv_id else "https://arxiv.org"
            title = p.get("title") or "未命名论文"
            summary_text = p.get("summary") or p.get("insight") or ""
            tags = [t.strip() for t in re.split(r"[，,/]", p.get("tags") or "") if t.strip()]
            tag_html = "".join(f"<span class='badge soft'>{esc(t)}</span>" for t in tags[:5])
            title_terms = compact_terms(title, 5)
            content_terms = compact_terms(make_search_text(title, p.get("tags"), p.get("insight"), p.get("summary"), sec["title"]), 8)
            search_text = make_search_text(title, p.get("tags"), p.get("insight"), p.get("summary"), p.get("time"), sec["title"])
            preview_name, preview_source = generate_paper_preview(
                arxiv_id,
                title,
                p.get("tags"),
                p.get("insight"),
                p.get("summary"),
                sec["title"],
            )
            preview_url = f"{asset_prefix}assets/previews/{preview_name}"
            preview_kind = "源图优先" if preview_source.startswith("源代码原图") else "PDF 回退" if preview_source.startswith("PDF") else "占位图"
            impl_score = parse_percent(p.get("impl"))
            open_status = normalize_status(p.get("open"))
            deploy_status = normalize_status(p.get("deploy"))
            impl_bucket = "good" if impl_score >= 80 else "neutral" if impl_score >= 70 else "bad"
            open_label = p.get("open") or "未注明"
            deploy_label = p.get("deploy") or "未注明"
            summary_preview = summary_text[:110] + ("…" if len(summary_text) > 110 else "")
            keyword_items = content_terms + [t for t in title_terms if t not in content_terms]
            keywords_html = "".join(f"<span class='keyword-chip' data-term='{esc(term)}'>{esc(term)}</span>" for term in keyword_items)
            parts.append(
                f"""
                <details class='paper-card' data-paper-card='1' data-section='{esc(sec['title'])}' data-tags='{esc(' '.join(tags))}' data-search='{esc(search_text)}' data-impl='{impl_score}' data-open='{open_status}' data-deploy='{deploy_status}' data-top3='false'>
                  <summary>
                    <div class='paper-top'>
                      <div class='paper-title'>{esc(title)}</div>
                      <a class='arxiv-link' href='{arxiv_url}' target='_blank' rel='noreferrer'>{esc(arxiv_id)}</a>
                    </div>
                    <div class='paper-summary'>{esc(summary_preview)}</div>
                    <div class='paper-preview'>
                      <div class='paper-preview-toolbar'>
                        <span class='preview-kind {'source' if preview_source.startswith('源代码原图') else 'pdf' if preview_source.startswith('PDF') else 'placeholder'}'>{preview_kind}</span>
                        <button type='button' class='preview-open' data-preview-open='{preview_url}' data-preview-title='{esc(title)}' data-preview-source='{esc(preview_source)}'>预览</button>
                      </div>
                      <img src='{preview_url}' alt='{esc(title)} 框架图预览' loading='lazy' decoding='async' data-preview-open='{preview_url}' data-preview-title='{esc(title)}' data-preview-source='{esc(preview_source)}'>
                      <div class='paper-preview-meta'><strong>预览图</strong><span>{esc(preview_source)}</span></div>
                    </div>
                    <div class='paper-badges'>
                      <span class='badge {'green' if impl_score >= 80 else 'violet' if impl_score >= 70 else 'soft'}'>实现概率 {impl_score}%</span>
                      <span class='badge {'green' if open_status == 'yes' else 'amber' if open_status == 'uncertain' else 'soft'}'>开源：{esc(open_label)}</span>
                      <span class='badge {'green' if deploy_status == 'yes' else 'amber' if deploy_status == 'uncertain' else 'soft'}'>可部署：{esc(deploy_label)}</span>
                    </div>
                  </summary>
                  <div class='paper-body'>
                    <div class='paper-meta'>
                      {f"<div><span>发布时间</span><strong>{esc(p.get('time'))}</strong></div>" if p.get('time') else ""}
                      {f"<div><span>主题</span><strong>{esc(sec['title'])}</strong></div>"}
                    </div>
                    {f"<p class='insight'>{esc(p.get('insight'))}</p>" if p.get('insight') else ""}
                    {f"<div class='paper-facts'><div class='fact {impl_bucket}'><span>摘要总结</span><strong>{esc(p.get('summary'))}</strong></div>" if p.get('summary') else "<div class='paper-facts'>"}
                    <div class='fact {'good' if open_status == 'yes' else 'neutral'}'><span>开源判断</span><strong>{esc(open_label)}</strong></div>
                    <div class='fact {'good' if deploy_status == 'yes' else 'bad' if deploy_status == 'no' else 'neutral'}'><span>部署判断</span><strong>{esc(deploy_label)}</strong></div>
                    <div class='fact {impl_bucket}'><span>实现概率</span><strong>{esc(p.get('impl'))}</strong></div>
                    </div>
                    <div class='paper-actions'>
                      <a class='paper-action primary' href='{arxiv_url}' target='_blank' rel='noreferrer'>打开 arXiv</a>
                      <button type='button' class='paper-action' data-filter='term'>相似</button>
                    </div>
                    {f"<div class='tags'>{tag_html}</div>" if tag_html else ""}
                    {f"<div class='paper-keywords'>{keywords_html}</div>" if keywords_html else ""}
                  </div>
                </details>
                """
            )
        parts.append("</div></section>")
        parts.append("""
    <div class='lightbox' id='preview-lightbox' aria-hidden='true'>
      <div class='lightbox-backdrop' data-lightbox-close='1'></div>
      <figure class='lightbox-panel' role='dialog' aria-modal='true' aria-label='论文大图预览'>
        <button type='button' class='lightbox-close' aria-label='关闭大图' data-lightbox-close='1'>×</button>
        <img id='preview-lightbox-image' alt='论文框架图大图'>
        <figcaption class='lightbox-caption'><strong id='preview-lightbox-title'></strong><span id='preview-lightbox-source'></span></figcaption>
      </figure>
    </div>
    """)
    script = """
    <script>
    (() => {
      const cards = Array.from(document.querySelectorAll('[data-paper-card]'));
      const sectionButtons = Array.from(document.querySelectorAll('[data-section]'));
      const termButtons = Array.from(document.querySelectorAll('[data-term]'));
      const filterButtons = Array.from(document.querySelectorAll('[data-filter]'));
      const search = document.getElementById('paper-search');
      const lightbox = document.getElementById('preview-lightbox');
      const lightboxImg = document.getElementById('preview-lightbox-image');
      const lightboxTitle = document.getElementById('preview-lightbox-title');
      const lightboxSource = document.getElementById('preview-lightbox-source');
      const previewTriggers = Array.from(document.querySelectorAll('[data-preview-open]'));
      const closeTriggers = Array.from(document.querySelectorAll('[data-lightbox-close]'));
      const state = { mode: 'all', section: '', term: '', query: '' };

      const lower = v => (v || '').toString().trim().toLowerCase();
      const matches = (card) => {
        const text = lower(card.dataset.search);
        const tags = lower(card.dataset.tags);
        const section = lower(card.dataset.section);
        const impl = parseInt(card.dataset.impl || '0', 10);
        const open = card.dataset.open || 'unknown';
        const deploy = card.dataset.deploy || 'unknown';
        if (state.mode === 'high' && impl < 80) return false;
        if (state.mode === 'deploy' && deploy !== 'yes') return false;
        if (state.mode === 'open' && open !== 'yes') return false;
        if (state.section && section !== lower(state.section)) return false;
        if (state.term && !(text.includes(lower(state.term)) || tags.includes(lower(state.term)))) return false;
        if (state.query && !text.includes(lower(state.query))) return false;
        return true;
      };

      const openPreview = (url, title, source) => {
        if (!lightbox || !lightboxImg) return;
        lightboxImg.src = url;
        lightboxImg.alt = title ? `${title} 框架图大图` : '论文框架图大图';
        if (lightboxTitle) lightboxTitle.textContent = title || '论文框架图';
        if (lightboxSource) lightboxSource.textContent = source || '';
        lightbox.setAttribute('aria-hidden', 'false');
        document.body.style.overflow = 'hidden';
      };

      const closePreview = () => {
        if (!lightbox) return;
        lightbox.setAttribute('aria-hidden', 'true');
        document.body.style.overflow = '';
      };

      const sync = () => {
        cards.forEach(card => {
          const visible = matches(card);
          card.hidden = !visible;
        });
        filterButtons.forEach(btn => {
          const mode = btn.dataset.filter || '';
          const section = btn.dataset.section || '';
          const term = btn.dataset.term || '';
          const active = (mode && mode === state.mode) || (section && lower(section) === lower(state.section)) || (term && lower(term) === lower(state.term));
          btn.classList.toggle('active', active);
        });
      };

      filterButtons.forEach(btn => btn.addEventListener('click', () => {
        const mode = btn.dataset.filter || '';
        const section = btn.dataset.section || '';
        const term = btn.dataset.term || '';
        if (mode) {
          if (mode === 'clear') {
            state.mode = 'all'; state.section = ''; state.term = ''; state.query = '';
            if (search) search.value = '';
          } else {
            state.mode = mode;
            state.section = '';
            state.term = '';
          }
        }
        if (section) {
          state.section = section;
          state.term = '';
          state.mode = 'all';
        }
        if (term) {
          state.term = term;
          state.section = '';
          state.mode = 'all';
        }
        sync();
      }));

      termButtons.forEach(btn => btn.addEventListener('click', () => {
        state.term = btn.dataset.term || '';
        state.section = '';
        state.mode = 'all';
        sync();
      }));

      if (search) {
        search.addEventListener('input', () => {
          state.query = search.value || '';
          sync();
        });
      }

      previewTriggers.forEach(trigger => trigger.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        openPreview(trigger.dataset.previewOpen || trigger.src || '', trigger.dataset.previewTitle || '', trigger.dataset.previewSource || '');
      }));

      closeTriggers.forEach(trigger => trigger.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        closePreview();
      }));

      if (lightbox) {
        lightbox.addEventListener('click', (event) => {
          if (event.target && event.target.matches('[data-lightbox-close]')) closePreview();
        });
      }

      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closePreview();
      });

      sync();
    })();
    </script>
    """
    return "\n".join(parts) + script


def build_archive_page(runs: list[dict]) -> str:
    cards = []
    if not runs:
        cards.append("<div class='overall-card'>还没有历史归档，先运行一次定时任务。</div>")
    else:
        for run in runs:
            date = run.get("run_time") or run["path"].stem
            top = run["top3"][0] if run["top3"] else ""
            cards.append(
                f"""
                <article class='archive-card'>
                  <div class='archive-head'>
                    <div>
                      <div class='archive-date'>{esc(date)}</div>
                      <div class='archive-meta'>{run['paper_count']} 篇 · {len(run['sections'])} 主题块 · {esc(run.get('schedule'))}</div>
                    </div>
                    <a class='archive-open' href='../runs/{esc(run['slug'])}.html'>查看详情</a>
                  </div>
                  {f"<div class='archive-top'>{esc(top)}</div>" if top else ''}
                </article>
                """
            )
    latest = runs[0] if runs else None
    latest_time = latest.get("run_time") if latest else "暂无"
    return f"""<!doctype html>
<html lang='zh-CN'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <meta http-equiv='refresh' content='300'>
  <title>arXiv 历史归档</title>
  <link href='https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap' rel='stylesheet'>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f5; --surface: rgba(255,255,255,.92); --text: #171717; --muted: #5b5b5b;
      --soft: #8c8c8c; --line: rgba(0,0,0,.08); --blue: #0a72ef; --blue-bg: #ebf5ff; --violet: #6d4aff;
      --green: #1aa64b; --shadow: 0 20px 60px rgba(0,0,0,.06); --shadow-soft: 0 8px 24px rgba(0,0,0,.05);
      --radius-xl: 28px; --radius-lg: 20px; --max: 1180px;
    }}
    * {{ box-sizing: border-box; }} body {{ margin: 0; font-family: 'Geist', Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; color: var(--text);
      background: linear-gradient(180deg, #ffffff 0%, #fafafa 42%, #f4f4f2 100%); min-height: 100vh; }}
    a {{ color: inherit; text-decoration: none; }} .page {{ max-width: var(--max); margin: 0 auto; padding: 24px 18px 56px; }}
    .topbar {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 18px; padding: 10px 14px; background: rgba(255,255,255,.72); backdrop-filter: blur(16px);
      border: 1px solid rgba(0,0,0,.08); border-radius: 999px; box-shadow: 0 8px 24px rgba(0,0,0,.04); position: sticky; top: 12px; z-index: 5; }}
    .brand {{ display: flex; align-items: center; gap: 10px; font-weight: 800; letter-spacing: -.02em; }} .brand-mark {{ width: 12px; height: 12px; border-radius: 50%; background: #171717; box-shadow: 0 0 0 6px rgba(23,23,23,.08); }}
    .hint {{ color: var(--muted); font-size: .92rem; }}
    .hero {{ padding: 28px; border-radius: var(--radius-xl); background: rgba(255,255,255,.92); border: 1px solid rgba(0,0,0,.08); box-shadow: var(--shadow); }}
    .eyebrow {{ display: inline-flex; align-items: center; gap: 8px; font-size: .78rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); background: rgba(255,255,255,.82); padding: 8px 12px; border-radius: 999px; margin-bottom: 14px; border: 1px solid var(--line); }}
    h1 {{ margin: 0; font-size: clamp(2rem, 4vw, 3.2rem); line-height: 1.02; letter-spacing: -.055em; }} .lede {{ margin: 14px 0 0; max-width: 60ch; color: var(--muted); font-size: 1.02rem; line-height: 1.7; }}
    .hero-badges {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }} .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 7px 12px; font-size: .86rem; font-weight: 700; border: 1px solid transparent; letter-spacing: -.01em; }}
    .badge.blue {{ color: #0a72ef; background: var(--blue-bg); border-color: rgba(10,114,239,.12); }} .badge.violet {{ color: #5234d6; background: rgba(109,74,255,.08); }} .badge.green {{ color: #107426; background: rgba(26,166,75,.08); }} .badge.soft {{ background: rgba(255,255,255,.84); border-color: rgba(0,0,0,.06); color: var(--muted); }}
    .section-block {{ margin-top: 22px; }}
    .section-head {{
      display: flex; justify-content: space-between; align-items: baseline; gap: 10px;
      margin-bottom: 12px; padding: 0 4px;
    }}
    .section-head h2 {{ margin: 0; font-size: 1.15rem; letter-spacing: -.02em; }}
    .section-head span {{ color: var(--muted); font-size: .92rem; }}
    .search-bar {{
      display: flex; gap: 10px; align-items: center; margin-bottom: 12px;
      padding: 12px; border-radius: 18px; background: rgba(255,255,255,.82); border: 1px solid rgba(0,0,0,.08);
      box-shadow: 0 10px 24px rgba(0,0,0,.03);
    }}
    .search-bar input {{
      flex: 1; min-width: 0; border: 0; outline: none; background: transparent; color: var(--text);
      font-size: .98rem; padding: 4px 2px;
    }}
    .search-bar input::placeholder {{ color: var(--soft); }}
    .filter-rail, .section-rail, .hero-badges, .tags {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .filter-rail {{ margin-bottom: 10px; }}
    .rail-chip, .paper-action, .cloud-chip {{
      appearance: none; border: 1px solid rgba(0,0,0,.08); background: rgba(255,255,255,.88); color: var(--text);
      border-radius: 999px; padding: 9px 12px; font: inherit; font-weight: 700; cursor: pointer;
      transition: transform .16s ease, box-shadow .16s ease, background .16s ease, border-color .16s ease;
    }}
    .rail-chip {{ display: inline-flex; align-items: center; gap: 8px; }}
    .rail-chip span {{ color: var(--muted); font-size: .82rem; }}
    .rail-chip.active {{ background: linear-gradient(135deg, var(--blue-bg), rgba(255,255,255,.96)); border-color: rgba(0,117,222,.18); color: #0056a8; }}
    .rail-chip:hover, .paper-action:hover, .cloud-chip:hover {{ transform: translateY(-1px); box-shadow: 0 10px 18px rgba(0,0,0,.05); }}
    .section-rail {{ margin-bottom: 4px; }}
    .section-rail .rail-chip {{ flex: 1 1 220px; justify-content: space-between; background: rgba(255,255,255,.74); }}
    .section-rail .rail-chip span {{ font-weight: 800; }}
    .weekly-grid {{ display: grid; grid-template-columns: 1.35fr .9fr; gap: 14px; align-items: start; }}
    .figure-card {{ padding: 18px; border-radius: var(--radius-lg); background: rgba(255,255,255,.74); border: 1px solid var(--line); box-shadow: var(--shadow-soft); }}
    .figure-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: baseline; margin-bottom: 12px; }}
    .figure-head > div {{ display: grid; gap: 4px; }}
    .figure-head strong {{ font-size: 1.02rem; }}
    .figure-head span {{ color: var(--muted); font-size: .9rem; }}
    .preview-inline-action {{ white-space: nowrap; }}
    .figure-image {{
      width: 100%; display: block; border-radius: 20px; overflow: hidden; background: rgba(255,255,255,.96);
      border: 1px solid rgba(0,0,0,.05); object-fit: contain; height: auto;
    }}
    .figure-zoomable {{ cursor: zoom-in; }}
    .figure-caption {{ margin-top: 10px; color: var(--muted); font-size: .9rem; line-height: 1.6; }}
    .cloud-figure .figure-image {{ min-height: 0; max-height: 420px; }}
    .framework-figure .figure-image {{ min-height: 0; max-height: 320px; }}
    .cloud {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
    .cloud-empty {{ color: var(--muted); padding: 14px 4px; }}
    .cloud-chip {{ display: inline-flex; align-items: baseline; gap: 8px; padding: 10px 14px; line-height: 1; --scale: 1; transform-origin: center; }}
    .cloud-chip span {{ font-size: calc(.92rem * var(--scale)); }}
    .cloud-chip em {{ font-style: normal; color: var(--muted); font-size: .76rem; }}
    .cloud-chip.blue {{ background: linear-gradient(180deg, rgba(242,249,255,.98), rgba(255,255,255,.92)); color: #004a8f; }}
    .cloud-chip.violet {{ background: linear-gradient(180deg, rgba(245,242,255,.98), rgba(255,255,255,.92)); color: #4a2fd0; }}
    .cloud-chip.green {{ background: linear-gradient(180deg, rgba(243,253,246,.98), rgba(255,255,255,.92)); color: #11662d; }}
    .cloud-chip.amber {{ background: linear-gradient(180deg, rgba(255,247,240,.98), rgba(255,255,255,.92)); color: #a94b00; }}
    .cloud-chip.rose {{ background: linear-gradient(180deg, rgba(255,244,251,.98), rgba(255,255,255,.92)); color: #c13f8f; }}
    .weekly-stats {{ display: grid; gap: 10px; }}
    .paper-card {{
      padding: 18px; border-radius: var(--radius-lg); position: relative; overflow: hidden;
      background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(248,251,255,.92));
      border: 1px solid rgba(0,0,0,.08);
      box-shadow: var(--shadow-soft);
    .paper-card {{
      padding: 20px; border-radius: var(--radius-lg); position: relative; overflow: hidden;
      background: rgba(255,255,255,.96);
      box-shadow: 0 14px 36px rgba(15,23,42,.06);
      border: 1px solid rgba(17,24,39,.06);
      transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
    }}
    .paper-card:hover {{ transform: translateY(-2px); box-shadow: 0 18px 44px rgba(15,23,42,.10); border-color: rgba(0,0,0,.12); }}
    .paper-card::before {{
      content: ''; position: absolute; left: 0; top: 0; right: 0; height: 1px;
      background: linear-gradient(90deg, rgba(0,0,0,.18), rgba(0,0,0,.04));
      opacity: .85;
    }}
    summary {{ list-style: none; cursor: pointer; }}
    summary::-webkit-details-marker {{ display: none; }}
    .paper-top {{ display: flex; justify-content: space-between; gap: 14px; align-items: start; margin-bottom: 12px; }}
    .paper-title {{ font-size: 1.04rem; font-weight: 850; line-height: 1.38; letter-spacing: -.025em; }}
    .arxiv-link {{ flex: none; align-self: flex-start; padding: 6px 10px; border-radius: 999px; border: 1px solid rgba(0,117,222,.16); background: rgba(242,249,255,.9); color: #0056a8; font-size: .8rem; font-weight: 800; letter-spacing: .01em; box-shadow: inset 0 1px 0 rgba(255,255,255,.78); }}
    .arxiv-link:hover {{ background: #fff; }}
    .paper-summary {{ margin-top: 8px; color: var(--muted); font-size: .96rem; line-height: 1.62; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
    .paper-badges {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
    .paper-badges .badge {{ background: rgba(255,255,255,.88); border-color: rgba(0,0,0,.06); }}
    .paper-preview {{
      margin-top: 12px; border-radius: 20px; overflow: hidden; background: linear-gradient(180deg, rgba(248,251,255,.98), rgba(255,255,255,.95));
      border: 1px solid rgba(0,0,0,.06); position: relative; box-shadow: inset 0 1px 0 rgba(255,255,255,.7);
    }}
    .paper-preview-toolbar {{
      display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 12px 14px 0;
    }}
    .preview-kind {{
      display: inline-flex; align-items: center; padding: 6px 10px; border-radius: 999px; font-size: .75rem; font-weight: 800; letter-spacing: .02em;
      border: 1px solid rgba(0,0,0,.06); background: rgba(255,255,255,.86); color: var(--muted);
    }}
    .preview-kind.source {{ color: #0b6a22; background: rgba(26,174,57,.10); }}
    .preview-kind.pdf {{ color: #8a4a00; background: rgba(221,91,0,.10); }}
    .preview-kind.placeholder {{ color: #6d4aff; background: rgba(109,74,255,.10); }}
    .paper-preview img {{
      width: auto; max-width: calc(100% - 32px); height: auto; margin: 16px auto; display: block;
      background: #fff; cursor: zoom-in; border-radius: 16px;
      border: 1px solid rgba(0,0,0,.05);
    }}
    .preview-open {{
      position: static; z-index: 2; border: 1px solid rgba(0,0,0,.08); border-radius: 999px; padding: 7px 10px;
      background: rgba(255,255,255,.92); color: var(--muted); font-weight: 700; box-shadow: none; cursor: zoom-in;
    }}
    .preview-open:hover {{ background: rgba(0,0,0,.03); border-color: rgba(0,0,0,.14); transform: translateY(-1px); }}
    .paper-preview-meta {{
      display: flex; justify-content: space-between; gap: 8px; align-items: center; padding: 0 14px 12px; color: var(--muted); font-size: .82rem; flex-wrap: wrap;
    }}
    .paper-preview-meta strong {{ color: var(--text); font-size: .86rem; }}
    .paper-preview-meta span {{ font-weight: 700; }}
    .paper-body {{ margin-top: 8px; display: grid; gap: 12px; }}

    .lightbox {{ position: fixed; inset: 0; z-index: 999; display: none; }}
    .lightbox[aria-hidden='false'] {{ display: grid; place-items: center; }}
    .lightbox-backdrop {{ position: absolute; inset: 0; background: rgba(10,14,20,.72); backdrop-filter: blur(8px); }}
    .lightbox-panel {{
      position: relative; z-index: 1; width: min(94vw, 980px); max-height: 92vh; margin: 0;
      display: grid; grid-template-rows: 1fr auto; gap: 10px; padding: 14px; border-radius: 24px;
      background: rgba(255,255,255,.98); box-shadow: 0 24px 80px rgba(0,0,0,.28);
    }}
    .lightbox-panel img {{
      width: auto; max-width: 100%; height: auto; max-height: calc(92vh - 86px); justify-self: center;
      object-fit: contain; border-radius: 16px; background: #fff;
    }}
    .lightbox-caption {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; color: var(--muted); font-size: .92rem; }}
    .lightbox-caption strong {{ color: var(--text); font-size: .98rem; }}
    .lightbox-close {{
      position: absolute; top: 12px; right: 12px; z-index: 2; border: 0; width: 38px; height: 38px; border-radius: 50%;
      background: rgba(255,255,255,.92); color: #0c2942; font-size: 22px; font-weight: 700; cursor: pointer; box-shadow: 0 8px 20px rgba(0,0,0,.14);
    }}

    .paper-meta div {{
      padding: 10px 11px; border-radius: 14px; background: rgba(255,255,255,.74); border: 1px solid var(--line);
    }}
    .paper-actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 2px; }}
    .paper-action.primary {{ background: #171717; color: #fff; border-color: #171717; box-shadow: none; }}
    .paper-action.primary:hover {{ background: #000; border-color: #000; }}
    .preview-inline-action {{ padding-inline: 14px; }}
    .paper-keywords {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .keyword-chip {{ background: rgba(255,255,255,.82); border: 1px solid var(--line); color: var(--muted); }}
    .overall-card {{ padding: 18px; border-radius: 18px; color: var(--muted); line-height: 1.7; background: var(--surface); border: 1px solid rgba(255,255,255,.8); box-shadow: var(--shadow-soft); }}
    .archive-grid {{ display: grid; gap: 14px; }} .archive-card {{ padding: 18px; border-radius: var(--radius-lg); background: var(--surface); border: 1px solid rgba(255,255,255,.8); box-shadow: var(--shadow-soft); }}
    .archive-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; }} .archive-date {{ font-weight: 800; font-size: 1.03rem; }} .archive-meta {{ color: var(--muted); font-size: .92rem; margin-top: 4px; }}
    .archive-open {{ flex: none; color: var(--blue); background: var(--blue-bg); border: 1px solid rgba(0,117,222,.12); padding: 8px 12px; border-radius: 999px; font-weight: 800; }}
    .archive-top {{ margin-top: 12px; color: var(--text); line-height: 1.6; }}
    .source {{ margin-top: 22px; color: var(--soft); font-size: .86rem; text-align: center; }}
    @media (max-width: 900px) {{
      .hero, .grid, .weekly-grid {{ grid-template-columns: 1fr; }}
      .paper-meta {{ grid-template-columns: 1fr; }}
      .topbar {{ position: static; border-radius: 20px; }}
      .figure-head {{ flex-direction: column; align-items: start; }}
    }}
    @media (max-width: 560px) {{
      .page {{ padding: 14px 12px 40px; }}
      .hero {{ padding: 20px; border-radius: 22px; }}
      .paper-card {{ padding: 16px; }}
      h1 {{ font-size: 2.05rem; }}
      .section-chip, .rail-chip {{ flex-basis: 100%; }}
      .section-head {{ flex-direction: column; align-items: start; }}
      .archive-head {{ flex-direction: column; }}
      .search-bar {{ flex-direction: column; align-items: stretch; }}
      .search-bar button {{ width: 100%; }}
      .paper-top, .paper-actions, .paper-preview-toolbar, .paper-preview-meta, .lightbox-caption {{ flex-direction: column; align-items: stretch; }}
      .arxiv-link {{ align-self: flex-start; }}
      .cloud-figure .figure-image {{ max-height: 240px; }}
      .paper-preview img {{ width: auto; max-width: calc(100% - 24px); margin: 12px auto; border-radius: 14px; }}
      .paper-preview-toolbar {{ padding: 10px 10px 0; }}
      .paper-preview-meta {{ padding: 0 10px 10px; font-size: .76rem; }}
      .preview-open, .preview-inline-action {{ width: 100%; justify-content: center; text-align: center; }}
      .preview-open {{ min-height: 42px; }}
    }}
  </style>
</head>
<body>
  <div class='page'>
    <header class='topbar'>
      <div class='brand'><span class='brand-mark'></span>arXiv 历史归档</div>
      <div class='hint'>最新：{esc(latest_time)}</div>
    </header>
    <section class='hero'>
      <div class='eyebrow'>Archive</div>
      <h1>历史归档</h1>
      <p class='lede'>所有已生成的 arXiv 日报都会自动归档到这里。每次定时任务跑完后，首页和历史页都会同步更新。</p>
      <div class='hero-badges'>
              <a class='badge blue' href='../index.html'>回到首页</a>
              <span class='badge violet'>{len(runs)} 次运行</span>
<span class='badge green'>自动同步 GitHub</span>
      </div>
    </section>
    <section class='section-block'>
      <div class='section-head'><h2>按时间倒序</h2><span>点击查看每次运行的完整详情</span></div>
      <div class='archive-grid'>
        {''.join(cards)}
      </div>
    </section>
    <div class='source'>页面由最新 cron 输出自动生成</div>
  </div>
</body>
</html>"""


def build_css() -> str:
    return """
    :root {
      color-scheme: light;
      --bg: #f6f5f4;
      --surface: rgba(255,255,255,.86);
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
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
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
    }
    a { color: inherit; text-decoration: none; }
    .page { max-width: var(--max); margin: 0 auto; padding: 24px 18px 56px; }
    .topbar {
      display: flex; justify-content: space-between; align-items: center;
      gap: 12px; margin-bottom: 18px; padding: 10px 14px;
      background: rgba(255,255,255,.55); backdrop-filter: blur(16px);
      border: 1px solid var(--line); border-radius: 999px; box-shadow: 0 8px 24px rgba(0,0,0,.04);
      position: sticky; top: 12px; z-index: 5;
    }
    .brand { display: flex; align-items: center; gap: 10px; font-weight: 800; letter-spacing: -.02em; }
    .brand-mark { width: 12px; height: 12px; border-radius: 50%; background: linear-gradient(135deg, var(--blue), var(--violet)); box-shadow: 0 0 0 6px rgba(0,117,222,.1); }
    .topbar .hint { color: var(--muted); font-size: .92rem; }
    .hero {
      display: grid; grid-template-columns: 1.45fr .9fr; gap: 18px;
      padding: 28px; border-radius: var(--radius-xl);
      background: linear-gradient(180deg, rgba(255,255,255,.86), rgba(255,255,255,.72));
      border: 1px solid rgba(255,255,255,.8); box-shadow: var(--shadow);
    }
    .eyebrow {
      display: inline-flex; align-items: center; gap: 8px;
      font-size: .78rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase;
      color: var(--blue); background: var(--blue-bg); padding: 8px 12px; border-radius: 999px;
      margin-bottom: 14px;
    }
    h1 { margin: 0; font-size: clamp(2rem, 4vw, 3.4rem); line-height: 1.02; letter-spacing: -0.045em; }
    .lede { margin: 14px 0 0; max-width: 58ch; color: var(--muted); font-size: 1.02rem; }
    .hero-badges { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }
    .badge { display: inline-flex; align-items: center; border-radius: 999px; padding: 7px 12px; font-size: .86rem; font-weight: 700; border: 1px solid transparent; }
    .badge.blue { color: #0056a8; background: var(--blue-bg); border-color: rgba(0,117,222,.12); }
    .badge.green { color: #107426; background: rgba(26,174,57,.10); }
    .badge.violet { color: #5234d6; background: rgba(109,74,255,.10); }
    .badge.soft { color: var(--muted); background: rgba(255,255,255,.78); border-color: var(--line); }
    .hero-panel { display: grid; gap: 12px; align-content: start; }
    .stat-card, .card, .paper-card, .overall-card {
      background: var(--surface);
      border: 1px solid rgba(255,255,255,.8);
      box-shadow: var(--shadow-soft);
      backdrop-filter: blur(16px);
    }
    .stat-card { padding: 16px; border-radius: var(--radius-md); }
    .stat-card span { display: block; color: var(--muted); font-size: .82rem; margin-bottom: 6px; }
    .stat-card strong { font-size: 1rem; line-height: 1.35; }
    .section-block { margin-top: 22px; }
    .section-head {
      display: flex; justify-content: space-between; align-items: baseline; gap: 10px;
      margin-bottom: 12px; padding: 0 4px;
    }
    .section-head h2 { margin: 0; font-size: 1.15rem; letter-spacing: -.02em; }
    .section-head span { color: var(--muted); font-size: .92rem; }
    .section-chips { display: flex; flex-wrap: wrap; gap: 10px; }
    .section-chip {
      flex: 1 1 220px; display: flex; justify-content: space-between; gap: 12px;
      align-items: center; padding: 14px 16px; border-radius: 18px; background: rgba(255,255,255,.72);
      border: 1px solid var(--line); box-shadow: 0 8px 24px rgba(0,0,0,.03);
    }
    .section-chip strong { font-size: .98rem; line-height: 1.35; }
    .section-chip span { color: var(--muted); font-size: .9rem; white-space: nowrap; }
    .section-chip.blue { background: linear-gradient(180deg, rgba(242,249,255,.95), rgba(255,255,255,.82)); }
    .section-chip.green { background: linear-gradient(180deg, rgba(243,253,246,.95), rgba(255,255,255,.82)); }
    .section-chip.violet { background: linear-gradient(180deg, rgba(245,242,255,.95), rgba(255,255,255,.82)); }
    .section-chip.amber { background: linear-gradient(180deg, rgba(255,247,240,.95), rgba(255,255,255,.82)); }
    .section-chip.rose { background: linear-gradient(180deg, rgba(255,244,251,.95), rgba(255,255,255,.82)); }
    .grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
    .paper-card {
      padding: 18px; border-radius: var(--radius-lg); position: relative; overflow: hidden;
      background: linear-gradient(180deg, rgba(255,255,255,.96), rgba(255,255,255,.84));
    }
    .paper-card::before {
      content: ''; position: absolute; left: 0; top: 0; right: 0; height: 3px;
      background: linear-gradient(90deg, var(--blue), var(--violet), var(--rose));
      opacity: .9;
    }
    .paper-top { display: flex; justify-content: space-between; gap: 12px; align-items: start; margin-bottom: 10px; }
    .paper-title { font-size: 1.02rem; font-weight: 800; line-height: 1.35; letter-spacing: -.02em; }
    .paper-title a:hover { color: var(--blue); }
    .arxiv-link {
      flex: none; font-size: .78rem; font-weight: 800; color: var(--blue);
      background: var(--blue-bg); border: 1px solid rgba(0,117,222,.12);
      padding: 6px 10px; border-radius: 999px;
    }
    .paper-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 12px 0; }
    .paper-meta div {
      padding: 10px 11px; border-radius: 14px; background: rgba(255,255,255,.74); border: 1px solid var(--line);
    }
    .paper-meta span { display: block; font-size: .78rem; color: var(--muted); margin-bottom: 2px; }
    .paper-meta strong { font-size: .88rem; font-weight: 700; line-height: 1.45; }
    .insight { margin: 10px 0 0; color: var(--text); font-size: .95rem; }
    .paper-facts { display: grid; gap: 8px; margin-top: 12px; }
    .paper-facts div { display: grid; gap: 3px; padding: 10px 12px; border-radius: 14px; background: rgba(247,249,252,.9); border: 1px solid rgba(0,0,0,.05); }
    .paper-facts span { font-size: .74rem; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); font-weight: 800; }
    .paper-facts strong { font-size: .92rem; line-height: 1.45; font-weight: 600; }
    .tags { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .top3 { display: grid; gap: 10px; }
    .top3-item {
      display: grid; grid-template-columns: 34px 1fr; gap: 12px; align-items: start;
      padding: 14px 16px; border-radius: 16px; background: rgba(255,255,255,.76);
      border: 1px solid var(--line);
    }
    .rank {
      width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center;
      background: linear-gradient(135deg, var(--blue), var(--violet)); color: white; font-weight: 800;
    }
    .top3-text { color: var(--text); font-size: .96rem; line-height: 1.5; }
    .overall-card { padding: 18px; border-radius: 18px; color: var(--muted); font-size: .98rem; line-height: 1.7; }
    .source { margin-top: 22px; color: var(--soft); font-size: .86rem; text-align: center; }
    .hero-empty { display: block; }
    .archive-grid { display: grid; gap: 14px; }
    .archive-card { padding: 18px; border-radius: var(--radius-lg); background: var(--surface); border: 1px solid rgba(255,255,255,.8); box-shadow: var(--shadow-soft); }
    .archive-head { display: flex; justify-content: space-between; gap: 12px; align-items: start; }
    .archive-date { font-weight: 800; font-size: 1.03rem; }
    .archive-meta { color: var(--muted); font-size: .92rem; margin-top: 4px; }
    .archive-open { flex: none; color: var(--blue); background: var(--blue-bg); border: 1px solid rgba(0,117,222,.12); padding: 8px 12px; border-radius: 999px; font-weight: 800; }
    .archive-top { margin-top: 12px; color: var(--text); line-height: 1.6; }
    @media (max-width: 900px) {
      .hero, .grid { grid-template-columns: 1fr; }
      .paper-meta { grid-template-columns: 1fr; }
      .topbar { position: static; border-radius: 20px; }
    }
    @media (max-width: 560px) {
      .page { padding: 14px 12px 40px; }
      .hero { padding: 20px; border-radius: 22px; }
      .paper-card { padding: 16px; }
      h1 { font-size: 2.05rem; }
      .section-chip { flex-basis: 100%; }
      .section-head { flex-direction: column; align-items: start; }
      .archive-head { flex-direction: column; }
    }
    """


def render_page(title: str, body: str, hint: str = "自动刷新 · 手机友好 · 精选判断") -> str:
    css = build_css()
    return f"""<!doctype html>
<html lang='zh-CN'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <meta http-equiv='refresh' content='300'>
  <title>{esc(title)}</title>
  <link href='https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap' rel='stylesheet'>
  <style>{css}</style>
</head>
<body>
  <div class='page'>
    <header class='topbar'>
      <div class='brand'><span class='brand-mark'></span>{esc(title)}</div>
      <div class='hint'>{esc(hint)}</div>
    </header>
    {body}
  </div>
</body>
</html>"""


def generate_site() -> dict[str, str]:
    runs = all_runs()
    weekly = collect_weekly_stats(runs, window_size=7)
    pages: dict[str, str] = {}
    if runs:
        latest = runs[0]
        pages["index.html"] = render_page(
            title="arXiv 每日摘要",
            hint=f"最新：{latest.get('run_time') or '未知'}",
            body=build_run_detail(latest, weekly=weekly, home_link='./index.html', archive_link='archive/index.html', asset_prefix=''),
        )
        pages["archive/index.html"] = build_archive_page(runs)
        for run in runs:
            pages[f"runs/{run['slug']}.html"] = render_page(
                title="arXiv 每日摘要",
                hint=f"归档运行：{run.get('run_time') or run['slug']}",
                body=build_run_detail(run, weekly=weekly, home_link='../index.html', archive_link='../archive/index.html', asset_prefix='../'),
            )
    else:
        empty_body = """
        <section class='hero card hero-empty'>
          <div class='eyebrow'>arXiv 每日摘要</div>
          <h1>还没有找到 cron 输出</h1>
          <p class='lede'>请先运行一次定时任务，随后这里会自动生成首页与历史归档。</p>
          <div class='hero-badges'>
            <a class='badge blue' href='archive/'>查看归档</a>
          </div>
        </section>
        """
        pages["index.html"] = render_page("arXiv 每日摘要", empty_body)
        pages["archive/index.html"] = render_page("arXiv 历史归档", "<div class='overall-card'>暂无历史归档。</div>", hint="自动同步 GitHub")
    return pages


def write_site(pages: dict[str, str]) -> None:
    for rel, content in pages.items():
        out = SITE_DIR / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")


def main() -> None:
    pages = generate_site()
    write_site(pages)
    print(json.dumps({"written": sorted(pages.keys()), "count": len(pages)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
