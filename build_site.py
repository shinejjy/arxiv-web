#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import os
import re
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import fitz

JOB_ID = "054d56957dbb"
SITE_DIR = Path(__file__).resolve().parent
SRC_DIR = Path.home() / ".hermes" / "cron" / "output" / JOB_ID
ASSET_DIR = SITE_DIR / "assets" / "figs"

SECTION_RE = re.compile(r"^##\s+(.*?)\s*$")
PAPER_RE = re.compile(r"^\d+[\.)]\s+[`*]*([^`*].*?[^`*])[`*]*\s*$")
FIXED_PAPER_HEADER_RE = re.compile(r"^\s*\d+[\.)]\s*论文标题与\s*arXiv id\s*$", re.I)
TITLE_WITH_ARXIV_RE = re.compile(r"^(?P<title>.+?)\s*\((?P<arxiv>\d{4}\.\d{4,5}(?:v\d+)?)\)\s*$")
TOP3_HEADER_RE = re.compile(r"^\s*Top\s*3\b")
TOP3_ITEM_RE = re.compile(r"^\s*\d+\.\s*(.+?)\s*$")
HEADING_PAPER_RE = re.compile(
    r"^\s*###\s*\d+[\.)]\s*(?P<arxiv>\d{4}\.\d{4,5}(?:v\d+)?)\s*[—\-–]+\s*(?P<title>.+?)\s*$"
)
ARXIV_RE = re.compile(r"^\s*-\s*(?:\*\*)?\s*arXiv:\s*(?:\*\*)?\s*(.*?)\s*$", re.I)
TIME_RE = re.compile(r"^\s*-\s*(?:\*\*)?\s*发布时间(?:（北京时间）|\(北京时间\))?:\s*(?:\*\*)?\s*(.*?)\s*$")
TAGS_RE = re.compile(r"^\s*-\s*(?:\*\*)?\s*标签?:\s*(?:\*\*)?\s*(.*?)\s*$")
INSIGHT_RE = re.compile(r"^\s*-\s*(?:\*\*)?\s*看点?:\s*(?:\*\*)?\s*(.*?)\s*$")
SUMMARY_RE = re.compile(r"^\s*-\s*(?:\*\*)?\s*(?:摘要总结|中文摘要总结):\s*(?:\*\*)?\s*(.*?)\s*$")
IMPL_RE = re.compile(r"^\s*-\s*(?:\*\*)?\s*(?:实现概率|个人主观实现概率)(?:估计)?(?:（百分比）)?\s*:\s*(?:\*\*)?\s*(.*?)\s*$")
OPEN_RE = re.compile(r"^\s*-\s*(?:\*\*)?\s*是否开源:\s*(?:\*\*)?\s*(.*?)\s*$")
DEPLOY_RE = re.compile(r"^\s*-\s*(?:\*\*)?\s*是否适合直接部署:\s*(?:\*\*)?\s*(.*?)\s*$")
PLAIN_FIELD_RE = re.compile(
    r"^\s*(?:-\s*)?(?:\*\*)?\s*"
    r"(?P<label>arXiv|发布时间(?:（北京时间）|\(北京时间\))?|标签|方向标签|看点|摘要总结|中文摘要总结|中文总结|抓取策略|实现概率|个人主观实现概率(?:估计)?(?:（百分比）)?|开源状态|直接部署性|图同步|是否开源|是否适合直接部署|框架图/视觉线索)"
    r"\s*(?:\*\*)?\s*[:：]\s*(?P<value>.*?)\s*$",
    re.I,
)


def esc(text: str | None) -> str:
    return html.escape(text or "")


FIGURE_CACHE_VERSION = "v5"


def slugify(text: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", text.strip())
    safe = re.sub(r"_+", "_", safe).strip("._-")
    if safe:
        return safe
    return "figure_" + hashlib.md5(text.encode("utf-8")).hexdigest()[:8]


def fact_class(text: str | None) -> str:
    value = (text or "").strip().lower()
    if not value:
        return "neutral"
    positive_tokens = ["yes", "true", "是", "可", "高", "已", "开源", "open", "deployable", "public"]
    negative_tokens = ["no", "false", "否", "不", "难", "低", "closed", "uncertain", "未知"]
    if any(tok in value for tok in positive_tokens):
        return "good"
    if any(tok in value for tok in negative_tokens):
        return "bad"
    return "neutral"


FIGURE_NAME_HINTS = (
    ("teaser", 120),
    ("pipeline", 110),
    ("architecture", 100),
    ("framework", 95),
    ("overview", 90),
    ("method", 80),
    ("figure", 70),
    ("fig", 65),
    ("main", 50),
    ("task", 45),
    ("real", 40),
    ("vis", 35),
)

FIGURE_CAPTION_HINTS = (
    ("framework", 150),
    ("pipeline", 140),
    ("architecture", 130),
    ("overview", 120),
    ("overall", 110),
    ("system", 100),
    ("method", 95),
    ("approach", 90),
    ("design", 85),
    ("main", 80),
    ("teaser", 75),
    ("illustration", 70),
    ("visual", 60),
    ("qualitative", -35),
    ("comparison", -30),
    ("ablation", -55),
    ("results", -45),
    ("performance", -20),
    ("benchmark", -20),
    ("table", -80),
    ("appendix", -50),
    ("supplement", -50),
    ("supp", -45),
)

FIGURE_ENV_RE = re.compile(r"\\begin\{figure\*?\}(?:\[[^\]]*\])?(.*?)\\end\{figure\*?\}", re.S)
INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
CAPTION_RE = re.compile(r"\\caption(?:\[[^\]]*\])?\{")


def _score_text(text: str, hints: tuple[tuple[str, int], ...]) -> int:
    lowered = text.lower()
    score = 0
    for key, value in hints:
        if key in lowered:
            score += value
    return score


def _figure_score(name: str) -> int:
    lowered = name.lower()
    score = _score_text(lowered, FIGURE_NAME_HINTS)
    if re.search(r"(?:^|[._-])(fig|figure)[._-]?(?:0*1|one)(?:[._-]|$)", lowered):
        score += 40
    if re.search(r"(?:^|[._-])(fig|figure)[._-]?(?:0*2|two|02)(?:[._-]|$)", lowered):
        score += 20
    if lowered.endswith(".pdf"):
        score += 15
    elif lowered.endswith((".png", ".jpg", ".jpeg", ".webp")):
        score += 10
    return score


def _extract_braced_text(text: str, start: int) -> tuple[str, int]:
    depth = 0
    buf: list[str] = []
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
            if depth > 1:
                buf.append(ch)
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(buf), i + 1
            if depth < 0:
                break
            buf.append(ch)
        else:
            if depth >= 1:
                buf.append(ch)
        i += 1
    return "", start


def _strip_latex_commands(text: str) -> str:
    text = re.sub(r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?", " ", text)
    text = text.replace("~", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _caption_score(caption: str) -> int:
    cleaned = _strip_latex_commands(caption)
    return _score_text(cleaned, FIGURE_CAPTION_HINTS)


def _resolve_graphics_path(ref: str, base_dir: str, members: dict[str, tarfile.TarInfo]) -> str | None:
    ref = ref.strip().strip('"')
    if not ref:
        return None

    raw = ref.replace("\\", "/")
    raw = re.sub(r"^\./", "", raw)
    base = base_dir.replace("\\", "/").strip("/")
    candidates: list[str] = []
    if base:
        candidates.append(f"{base}/{raw}")
    candidates.append(raw)

    root, ext = os.path.splitext(raw)
    if not ext:
        for suffix in (".pdf", ".png", ".jpg", ".jpeg", ".webp"):
            if base:
                candidates.append(f"{base}/{raw}{suffix}")
            candidates.append(f"{raw}{suffix}")

    seen: set[str] = set()
    for candidate in candidates:
        normalized = re.sub(r"^\./", "", candidate).lstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        if normalized in members:
            return normalized
        lowered = normalized.lower()
        for name in members:
            if name.lower() == lowered:
                return name
    return None


def _figure_candidates_from_tex(text: str, base_dir: str, members: dict[str, tarfile.TarInfo], tf: tarfile.TarFile) -> list[tuple[int, str, bytes]]:
    candidates: list[tuple[int, str, bytes]] = []
    for match in FIGURE_ENV_RE.finditer(text):
        block = match.group(1)
        caption = ""
        cap_match = CAPTION_RE.search(block)
        if cap_match:
            caption, _ = _extract_braced_text(block, cap_match.end() - 1)
        caption_score = _caption_score(caption)
        refs = INCLUDEGRAPHICS_RE.findall(block)
        for ref in refs:
            resolved = _resolve_graphics_path(ref, base_dir, members)
            if not resolved:
                continue
            member = members.get(resolved)
            if not member:
                continue
            fileobj = tf.extractfile(member)
            if not fileobj:
                continue
            data = fileobj.read()
            score = caption_score + _figure_score(resolved)
            # Prefer sources with explicit figure-like captions and main-text placements.
            if "figure" in block.lower():
                score += 5
            if any(token in caption.lower() for token in ("framework", "pipeline", "architecture", "overview", "overall")):
                score += 20
            if any(token in resolved.lower() for token in ("teaser", "framework", "pipeline", "architecture", "overview", "system", "main")):
                score += 15
            candidates.append((score, resolved, data))
    return candidates


def _download_bytes(url: str, timeout: int = 60) -> bytes:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _render_pdf_first_page(pdf_bytes: bytes, out_path: Path, zoom: float = 1.8) -> None:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.page_count < 1:
        raise ValueError("empty PDF")
    page = doc.load_page(0)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_path))


def _render_image_bytes(image_bytes: bytes, out_path: Path) -> None:
    png_sig = b"\x89PNG\r\n\x1a\n"
    if image_bytes.startswith(png_sig):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(image_bytes)
        return
    try:
        from PIL import Image
    except Exception as e:
        raise RuntimeError("Pillow not available") from e
    with Image.open(io.BytesIO(image_bytes)) as im:
        im = im.convert("RGB")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        im.save(out_path, format="PNG", optimize=True)


def _candidate_from_source(arxiv_id: str) -> tuple[Path | None, str | None]:
    asset_name = f"{slugify(arxiv_id)}_{FIGURE_CACHE_VERSION}.png"
    out_path = ASSET_DIR / asset_name
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path, "cached"

    source_urls = [
        f"https://arxiv.org/e-print/{arxiv_id}",
        f"https://arxiv.org/src/{arxiv_id}",
    ]
    source_data = None
    for url in source_urls:
        try:
            source_data = _download_bytes(url)
            if source_data:
                break
        except Exception:
            continue
    if not source_data:
        return None, None

    candidates: list[tuple[int, str, bytes]] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(source_data), mode="r:*") as tf:
            members = {member.name: member for member in tf.getmembers() if member.isfile()}
            tex_members = [member for member in members.values() if member.name.lower().endswith(".tex")]
            for member in tex_members:
                fileobj = tf.extractfile(member)
                if not fileobj:
                    continue
                text = fileobj.read().decode("utf-8", "ignore")
                candidates.extend(
                    _figure_candidates_from_tex(
                        text,
                        str(Path(member.name).parent),
                        members,
                        tf,
                    )
                )
            if not candidates:
                for member in members.values():
                    name = member.name
                    lowered = name.lower()
                    if not lowered.endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp")):
                        continue
                    score = _figure_score(name)
                    if score <= 0:
                        continue
                    fileobj = tf.extractfile(member)
                    if not fileobj:
                        continue
                    data = fileobj.read()
                    candidates.append((score, name, data))
    except Exception:
        candidates = []

    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (item[0], len(item[2])), reverse=True)
    score, name, data = candidates[0]
    try:
        if name.lower().endswith(".pdf"):
            _render_pdf_first_page(data, out_path)
        else:
            _render_image_bytes(data, out_path)
        return out_path, f"source:{name}"
    except Exception:
        return None, None


def _fallback_pdf_figure(arxiv_id: str) -> tuple[Path | None, str | None]:
    asset_name = f"{slugify(arxiv_id)}_{FIGURE_CACHE_VERSION}.png"
    out_path = ASSET_DIR / asset_name
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path, "cached"
    try:
        pdf_bytes = _download_bytes(f"https://arxiv.org/pdf/{arxiv_id}", timeout=90)
        _render_pdf_first_page(pdf_bytes, out_path, zoom=1.9)
        return out_path, "pdf:first-page"
    except Exception:
        return None, None


def ensure_figure_asset(arxiv_id: str) -> dict | None:
    if not arxiv_id:
        return None
    path, source = _candidate_from_source(arxiv_id)
    if path is None:
        path, source = _fallback_pdf_figure(arxiv_id)
    if path is None:
        return None
    return {
        "path": path,
        "rel": f"assets/figs/{path.name}",
        "source": source or "unknown",
    }


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
        # 接受当前与旧版日报格式，避免把技能提示/空跑结果误判为日报
        if "## Response" in text or ("## 自动驾驶" in text and ("- **看点：**" in text or "- **中文摘要总结：**" in text)):
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
    fixed_papers: list[dict] = []
    in_response = False
    in_top3 = False
    expecting_fixed_title = False

    def append_overall(text_line: str) -> None:
        stripped = text_line.strip()
        if not stripped:
            return
        if data["overall"]:
            data["overall"] = f"{data['overall']} {stripped}"
        else:
            data["overall"] = stripped

    def new_paper(title: str | None = None, arxiv: str | None = None) -> dict:
        return {
            "title": title,
            "arxiv": arxiv,
            "time": None,
            "tags": None,
            "insight": None,
            "summary": None,
            "strategy": None,
            "impl": None,
            "open": None,
            "deploy": None,
            "figure": None,
        }

    def apply_field(paper: dict, line: str) -> bool:
        if (am := ARXIV_RE.match(line)):
            paper["arxiv"] = am.group(1).strip()
            return True
        if (tm := TIME_RE.match(line)):
            paper["time"] = tm.group(1).strip()
            return True
        if (tg := TAGS_RE.match(line)):
            paper["tags"] = tg.group(1).strip()
            return True
        if (im := INSIGHT_RE.match(line)):
            paper["insight"] = im.group(1).strip()
            return True
        if (sm := SUMMARY_RE.match(line)):
            paper["summary"] = sm.group(1).strip()
            return True
        if (pm2 := IMPL_RE.match(line)):
            paper["impl"] = pm2.group(1).strip()
            return True
        if (om := OPEN_RE.match(line)):
            paper["open"] = om.group(1).strip()
            return True
        if (dm := DEPLOY_RE.match(line)):
            paper["deploy"] = dm.group(1).strip()
            return True
        plain = PLAIN_FIELD_RE.match(line)
        if not plain:
            return False
        label = plain.group("label").strip().lower()
        value = plain.group("value").strip()
        if label == "arxiv":
            paper["arxiv"] = value
        elif label.startswith("发布时间"):
            paper["time"] = value
        elif label in {"标签", "方向标签"}:
            paper["tags"] = value
        elif label == "看点":
            paper["insight"] = value
        elif label in {"摘要总结", "中文摘要总结", "中文总结"}:
            paper["summary"] = value
        elif label == "抓取策略":
            paper["strategy"] = value
        elif "实现概率" in label:
            paper["impl"] = value
        elif label in {"是否开源", "开源状态"}:
            paper["open"] = value
        elif label in {"是否适合直接部署", "直接部署性"}:
            paper["deploy"] = value
        elif label in {"框架图/视觉线索", "图同步"}:
            paper["figure"] = value
        else:
            return False
        return True

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
            current_section = None
            current_paper = None
            in_top3 = False
            expecting_fixed_title = False
            continue
        if not in_response:
            continue
        if line.strip() == "## Post-Run Hook":
            break

        stripped = line.strip()
        if not stripped:
            continue

        if in_top3:
            if TOP3_ITEM_RE.match(line):
                item = TOP3_ITEM_RE.match(line).group(1).strip()
                data["top3"].append(item)
                continue
            if SECTION_RE.match(line):
                in_top3 = False
            elif FIXED_PAPER_HEADER_RE.match(line):
                in_top3 = False
            else:
                continue

        if TOP3_HEADER_RE.match(line):
            current_section = "top3"
            current_paper = None
            expecting_fixed_title = False
            in_top3 = True
            continue

        if FIXED_PAPER_HEADER_RE.match(line):
            current_section = "fixed"
            current_paper = new_paper()
            fixed_papers.append(current_paper)
            expecting_fixed_title = True
            continue
        if (hm := HEADING_PAPER_RE.match(line)):
            current_section = "fixed"
            current_paper = new_paper(hm.group("title").strip(), hm.group("arxiv").strip())
            fixed_papers.append(current_paper)
            expecting_fixed_title = False
            continue

        m = SECTION_RE.match(line)
        if m:
            title = m.group(1)
            if title.startswith("今天最值得重点看的 3 篇"):
                current_section = "top3"
                current_paper = None
                expecting_fixed_title = False
                in_top3 = True
                continue
            if title.lower().startswith("top 3"):
                current_section = "top3"
                current_paper = None
                expecting_fixed_title = False
                in_top3 = True
                continue
            if title.startswith("整体判断"):
                current_section = "overall"
                current_paper = None
                expecting_fixed_title = False
                continue
            current_section = title
            data["sections"].append({"title": title, "papers": []})
            current_paper = None
            continue

        if current_section == "top3":
            if stripped[:1] in {"1", "2", "3"} and ("**" in stripped or stripped[:2].isdigit()):
                data["top3"].append(stripped)
            continue
        if current_section == "overall":
            append_overall(line)
            continue

        if expecting_fixed_title and current_paper is not None:
            tm = TITLE_WITH_ARXIV_RE.match(stripped)
            if tm:
                current_paper["title"] = tm.group("title").strip()
                current_paper["arxiv"] = tm.group("arxiv").strip()
            else:
                current_paper["title"] = stripped
                am = re.search(r"(\d{4}\.\d{4,5}(?:v\d+)?)", stripped)
                if am:
                    current_paper["arxiv"] = am.group(1)
            expecting_fixed_title = False
            continue

        if current_section == "fixed" and current_paper is not None:
            if apply_field(current_paper, line):
                continue
            if data["overall"] is None and not current_paper.get("title"):
                append_overall(line)
            continue

        if not data["sections"]:
            if not fixed_papers and data["overall"] is None:
                append_overall(line)
            continue
        papers = data["sections"][-1]["papers"]
        pm = PAPER_RE.match(line)
        if pm:
            current_paper = new_paper(pm.group(1).strip("`* "), None)
            papers.append(current_paper)
            continue
        if current_paper is None:
            continue
        apply_field(current_paper, line)

    if fixed_papers and not data["sections"]:
        grouped: dict[str, list[dict]] = {}
        order: list[str] = []
        for paper in fixed_papers:
            title = (paper.get("tags") or "").strip() or "精选论文"
            if title not in grouped:
                grouped[title] = []
                order.append(title)
            grouped[title].append(paper)
        data["sections"] = [{"title": title, "papers": grouped[title]} for title in order]

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


def pipeline_steps(section_title: str, paper: dict) -> list[tuple[str, str, str]]:
    title = f"{section_title} {paper.get('title') or ''} {paper.get('tags') or ''} {paper.get('summary') or ''}".lower()

    if any(k in title for k in ["autonomous", "driving", "驾驶", "car", "vehicle", "trajectory", "planning"]):
        return [
            ("输入", "多模态传感器", "相机 / 激光雷达 / 车道与地图"),
            ("理解", "感知与世界模型", "目标、车道、占用、未来状态"),
            ("决策", "预测与规划", "轨迹生成 / 风险评估 / 运动规划"),
            ("输出", "控制执行", "转向、加速、制动或轨迹点"),
        ]
    if any(k in title for k in ["manipulation", "robot", "arm", "grasp", "pick", "place", "policy", "操控", "机械臂"]):
        return [
            ("输入", "视觉与触觉", "RGB / Depth / Proprioception / State"),
            ("理解", "场景与物体解析", "物体、位姿、可操作区域"),
            ("决策", "技能或策略规划", "抓取 / 放置 / 轨迹 / 任务分解"),
            ("输出", "机械臂动作", "末端位姿、关节控制、执行信号"),
        ]
    if any(k in title for k in ["vla", "vision-language-action", "vision language action", "action"]):
        return [
            ("输入", "视觉 + 语言", "图像、文本指令、历史上下文"),
            ("理解", "多模态编码", "对齐视觉语义与任务目标"),
            ("决策", "动作生成", "离散 token / 连续控制 / 计划"),
            ("输出", "机器人动作", "可执行指令或控制序列"),
        ]
    if any(k in title for k in ["vlm", "vision-language", "vision language", "multimodal", "多模态"]):
        return [
            ("输入", "图像 + 问题", "单图 / 多图 / 文本提示"),
            ("理解", "视觉语言编码", "区域、对象、关系、语义"),
            ("推理", "跨模态推理", "问答、检索、解释、决策"),
            ("输出", "文本或结构化结果", "答案、描述、工具调用"),
        ]
    return [
        ("输入", "论文输入", "数据 / 图像 / 传感器 / 文本"),
        ("理解", "表征学习", "特征提取与上下文建模"),
        ("决策", "核心方法", "主模型、策略或优化模块"),
        ("输出", "任务结果", "预测、控制、回答或生成"),
    ]


def render_pipeline_html(section_title: str, paper: dict) -> str:
    steps = pipeline_steps(section_title, paper)
    items = []
    for idx, (phase, label, detail) in enumerate(steps):
        items.append(
            f"""
            <div class='pipeline-step'>
              <div class='pipeline-phase'>{esc(phase)}</div>
              <div class='pipeline-label'>{esc(label)}</div>
              <div class='pipeline-detail'>{esc(detail)}</div>
            </div>
            {"<div class='pipeline-arrow'>→</div>" if idx < len(steps) - 1 else ""}
            """
        )
    return f"""
    <div class='pipeline-card'>
      <div class='pipeline-head'>
        <span class='pipeline-title'>Pipeline 图</span>
        <span class='pipeline-note'>基于标题 / 标签 / 摘要自动归纳</span>
      </div>
      <div class='pipeline-flow'>
        {''.join(items)}
      </div>
    </div>
    """


def render_figure_html(paper: dict) -> str:
    arxiv_id = paper.get('arxiv') or ''
    asset = ensure_figure_asset(arxiv_id)
    if not asset:
        return ''
    fig_url = asset['rel']
    source = asset.get('source') or 'unknown'
    return f"""
    <div class='figure-card'>
      <div class='figure-head'>
        <span class='figure-title'>图像线索</span>
        <span class='figure-note'>{esc(source)}</span>
      </div>
      <a class='figure-link' href='{fig_url}' target='_blank' rel='noreferrer'>
        <img class='figure-img' src='{fig_url}' alt='paper figure preview'>
      </a>
    </div>
    """


def build_run_detail(run: dict, home_link: str = "index.html", archive_link: str = "archive/index.html") -> str:
    counts = [f"{sec['title']} {len(sec['papers'])}" for sec in run["sections"]]
    chips = "".join(f"<span class='badge soft'>{esc(c)}</span>" for c in counts)
    rail_items = [("概览", "#overview")]
    if run["top3"]:
        rail_items.append(("重点 3 篇", "#top3"))
    if run.get("overall"):
        rail_items.append(("总评", "#overall"))
    rail_items.extend((sec["title"], f"#{slugify(sec['title'])}") for sec in run["sections"])
    rail_html = "".join(f"<a class='rail-chip' href='{href}'>{esc(label)}</a>" for label, href in rail_items)
    parts = []
    parts.append(
        f"""
        <section class='hero card'>
          <div class='hero-copy'>
            <div class='eyebrow'>arXiv 每日摘要</div>
            <h1>{esc(run.get('job_name') or 'arXiv Daily')}</h1>
            <p class='lede'>聚焦自动驾驶、机械臂 / 操控、VLA 与 VLM。适合手机快速浏览，也保留完整历史详情。</p>
            <div class='hero-badges'>
              <a class='badge blue' href='{home_link}'>回到首页</a>
              <a class='badge violet' href='{archive_link}'>历史归档</a>
              <span class='badge green'>{run['paper_count']} 篇论文</span>
            </div>
          </div>
          <div class='hero-panel'>
            <div class='stat-card'><span>Job ID</span><strong>{esc(run.get('job_id'))}</strong></div>
            <div class='stat-card'><span>Run Time</span><strong>{esc(run.get('run_time'))}</strong></div>
            <div class='stat-card'><span>Schedule</span><strong>{esc(run.get('schedule'))}</strong></div>
            <div class='stat-card'><span>主题块</span><strong>{len(run['sections'])} 个</strong></div>
          </div>
        </section>
        <section class='section-block'>
          <div class='section-head'><h2>快速跳转</h2><span>滑动即可切换模块</span></div>
          <div class='section-rail'>{rail_html}</div>
        </section>
        """
    )
    if chips:
        parts.append(
            f"""
            <section class='section-block' id='overview'>
              <div class='section-head'><h2>今日概览</h2><span>{run['paper_count']} 篇 · {len(run['sections'])} 个主题块</span></div>
              <div class='section-chips'>{chips}</div>
            </section>
            """
        )
    if run["top3"]:
        top3_html = "".join(f"<div class='top3-item'><span class='rank'>{i+1}</span><div class='top3-text'>{esc(item)}</div></div>" for i, item in enumerate(run["top3"]))
        parts.append(
            f"""
            <section class='section-block' id='top3'>
              <div class='section-head'><h2>今天最值得重点看的 3 篇</h2><span>编辑优先级</span></div>
              <div class='top3'>{top3_html}</div>
            </section>
            """
        )
    if run.get("overall"):
        parts.append(
            f"""
            <section class='section-block' id='overall'>
              <div class='section-head'><h2>整体判断</h2><span>今日总评</span></div>
              <div class='overall-card'>{esc(run['overall'])}</div>
            </section>
            """
        )
    for sec in run["sections"]:
        sec_id = slugify(sec['title'])
        parts.append(f"<section class='section-block' id='{sec_id}'><div class='section-head'><h2>{esc(sec['title'])}</h2><span>{len(sec['papers'])} 篇</span></div><div class='grid'>")
        for p in sec["papers"]:
            arxiv_id = p.get("arxiv") or ""
            arxiv_url = f"https://arxiv.org/abs/{html.escape(arxiv_id)}" if arxiv_id else "https://arxiv.org"
            tag_items = [t.strip() for t in re.split(r"[，,/]", p.get("tags") or "") if t.strip()]
            tag_html = "".join(f"<span class='badge soft'>{esc(t)}</span>" for t in tag_items[:3])
            parts.append(
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
                  {f"<p class='paper-summary'>{esc(p.get('summary'))}</p>" if p.get('summary') else ""}
                  {f"<p class='insight'>{esc(p.get('insight'))}</p>" if p.get('insight') else ""}
                  <div class='paper-facts'>
                    {f"<div class='fact {fact_class(p.get('impl'))}'><span>实现概率</span><strong>{esc(p.get('impl'))}</strong></div>" if p.get('impl') else ""}
                    {f"<div class='fact {fact_class(p.get('open'))}'><span>开源</span><strong>{esc(p.get('open'))}</strong></div>" if p.get('open') else ""}
                    {f"<div class='fact {fact_class(p.get('deploy'))}'><span>可部署</span><strong>{esc(p.get('deploy'))}</strong></div>" if p.get('deploy') else ""}
                  </div>
                  {render_figure_html(p)}
                  <div class='tags'>{tag_html}</div>
                  <div class='paper-actions'><a class='paper-action primary' href='{arxiv_url}' target='_blank' rel='noreferrer'>打开 arXiv</a><a class='paper-action' href='{archive_link}'>{'返回归档'}</a></div>
                </article>
                """
            )
        parts.append("</div></section>")
    return "\n".join(parts)


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
  <link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap' rel='stylesheet'>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f5f4; --surface: rgba(255,255,255,.86); --text: rgba(0,0,0,.92); --muted: #615d59;
      --soft: #a39e98; --line: rgba(0,0,0,.09); --blue: #0075de; --blue-bg: #f2f9ff; --violet: #6d4aff;
      --green: #1aae39; --shadow: 0 24px 70px rgba(0,0,0,.08), 0 2px 10px rgba(0,0,0,.05);
      --shadow-soft: 0 10px 28px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04); --radius-xl: 28px; --radius-lg: 20px; --max: 1180px;
    }}
    * {{ box-sizing: border-box; }} body {{ margin: 0; font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; color: var(--text);
      background: radial-gradient(circle at top left, rgba(0,117,222,.12), transparent 26%), linear-gradient(180deg, #fbfbfa 0%, var(--bg) 42%, #efeeec 100%); min-height: 100vh; }}
    a {{ color: inherit; text-decoration: none; }} .page {{ max-width: var(--max); margin: 0 auto; padding: 24px 18px 56px; }}
    .topbar {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 18px; padding: 10px 14px; background: rgba(255,255,255,.55); backdrop-filter: blur(16px);
      border: 1px solid var(--line); border-radius: 999px; box-shadow: 0 8px 24px rgba(0,0,0,.04); position: sticky; top: 12px; z-index: 5; }}
    .brand {{ display: flex; align-items: center; gap: 10px; font-weight: 800; }} .brand-mark {{ width: 12px; height: 12px; border-radius: 50%; background: linear-gradient(135deg, var(--blue), var(--violet)); }}
    .hint {{ color: var(--muted); font-size: .92rem; }}
    .hero {{ padding: 28px; border-radius: var(--radius-xl); background: linear-gradient(180deg, rgba(255,255,255,.88), rgba(255,255,255,.72)); border: 1px solid rgba(255,255,255,.8); box-shadow: var(--shadow); }}
    .eyebrow {{ display: inline-flex; align-items: center; gap: 8px; font-size: .78rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; color: var(--blue); background: var(--blue-bg); padding: 8px 12px; border-radius: 999px; margin-bottom: 14px; }}
    h1 {{ margin: 0; font-size: clamp(2rem, 4vw, 3.2rem); line-height: 1.02; letter-spacing: -.045em; }} .lede {{ margin: 14px 0 0; max-width: 60ch; color: var(--muted); font-size: 1.02rem; }}
    .hero-badges {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }} .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 7px 12px; font-size: .86rem; font-weight: 700; border: 1px solid transparent; }}
    .badge.blue {{ color: #0056a8; background: var(--blue-bg); border-color: rgba(0,117,222,.12); }} .badge.violet {{ color: #5234d6; background: rgba(109,74,255,.10); }} .badge.green {{ color: #107426; background: rgba(26,174,57,.10); }}
    .section-block {{ margin-top: 22px; }} .section-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 10px; margin-bottom: 12px; padding: 0 4px; }}
    .section-head h2 {{ margin: 0; font-size: 1.15rem; }} .section-head span {{ color: var(--muted); font-size: .92rem; }}
    .archive-grid {{ display: grid; gap: 14px; }} .archive-card {{ padding: 18px; border-radius: var(--radius-lg); background: var(--surface); border: 1px solid rgba(255,255,255,.8); box-shadow: var(--shadow-soft); }}
    .archive-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; }} .archive-date {{ font-weight: 800; font-size: 1.03rem; }} .archive-meta {{ color: var(--muted); font-size: .92rem; margin-top: 4px; }}
    .archive-open {{ flex: none; color: var(--blue); background: var(--blue-bg); border: 1px solid rgba(0,117,222,.12); padding: 8px 12px; border-radius: 999px; font-weight: 800; }}
    .archive-top {{ margin-top: 12px; color: var(--text); line-height: 1.6; }} .overall-card {{ padding: 18px; border-radius: 18px; color: var(--muted); line-height: 1.7; background: var(--surface); border: 1px solid rgba(255,255,255,.8); box-shadow: var(--shadow-soft); }}
    .source {{ margin-top: 22px; color: var(--soft); font-size: .86rem; text-align: center; }}
    @media (max-width: 720px) {{ .topbar {{ position: static; border-radius: 20px; }} .archive-head {{ flex-direction: column; }} .hero {{ padding: 20px; border-radius: 22px; }} }}
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
      display: grid; grid-template-columns: 1.35fr .95fr; gap: 18px;
      padding: 28px; border-radius: 32px;
      background:
        radial-gradient(circle at top right, rgba(109,74,255,.14), transparent 32%),
        linear-gradient(180deg, rgba(255,255,255,.9), rgba(255,255,255,.76));
      border: 1px solid rgba(255,255,255,.84); box-shadow: var(--shadow);
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
    .hero-panel { display: grid; gap: 12px; align-content: start; grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .stat-card, .card, .paper-card, .overall-card {
      background: var(--surface);
      border: 1px solid rgba(255,255,255,.8);
      box-shadow: var(--shadow-soft);
      backdrop-filter: blur(16px);
    }
    .stat-card {
      padding: 15px 14px; border-radius: 18px; min-height: 84px;
      display: grid; gap: 4px; align-content: start;
      background: linear-gradient(180deg, rgba(255,255,255,.94), rgba(249,251,255,.86));
    }
    .stat-card span { display: block; color: var(--muted); font-size: .78rem; letter-spacing: .04em; text-transform: uppercase; font-weight: 800; }
    .stat-card strong { font-size: .98rem; line-height: 1.35; }
    .section-block { margin-top: 22px; }
    .section-head {
      display: flex; justify-content: space-between; align-items: baseline; gap: 10px;
      margin-bottom: 12px; padding: 0 4px;
    }
    .section-head h2 { margin: 0; font-size: 1.15rem; letter-spacing: -.02em; }
    .section-head span { color: var(--muted); font-size: .92rem; }
    .section-chips { display: flex; flex-wrap: wrap; gap: 10px; }
    .section-rail {
      display: flex; gap: 10px; overflow-x: auto; padding: 2px 2px 8px;
      -webkit-overflow-scrolling: touch; scrollbar-width: none;
    }
    .section-rail::-webkit-scrollbar { display: none; }
    .rail-chip {
      flex: 0 0 auto; padding: 10px 14px; border-radius: 999px; white-space: nowrap;
      background: rgba(255,255,255,.82); border: 1px solid rgba(0,0,0,.06);
      box-shadow: 0 8px 20px rgba(0,0,0,.04); font-size: .9rem; font-weight: 800;
    }
    .rail-chip:hover { color: var(--blue); transform: translateY(-1px); }
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
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .paper-card {
      padding: 18px; border-radius: 24px; position: relative; overflow: hidden;
      background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(255,255,255,.86));
      border: 1px solid rgba(255,255,255,.86);
    }
    .paper-card::before {
      content: ''; position: absolute; left: 0; top: 0; right: 0; height: 3px;
      background: linear-gradient(90deg, var(--blue), var(--violet), var(--rose));
      opacity: .9;
    }
    .paper-top { display: flex; justify-content: space-between; gap: 12px; align-items: start; margin-bottom: 10px; }
    .paper-title { font-size: 1.02rem; font-weight: 800; line-height: 1.35; letter-spacing: -.02em; }
    .paper-title a:hover { color: var(--blue); }
    .paper-summary {
      margin: 10px 0 0; color: var(--text); font-size: .95rem; line-height: 1.58;
      display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
    }
    .arxiv-link {
      flex: none; font-size: .78rem; font-weight: 800; color: var(--blue);
      background: var(--blue-bg); border: 1px solid rgba(0,117,222,.12);
      padding: 6px 10px; border-radius: 999px;
    }
    .paper-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 12px 0 8px; }
    .paper-meta div {
      padding: 10px 11px; border-radius: 14px; background: rgba(255,255,255,.74); border: 1px solid var(--line);
    }
    .paper-meta span { display: block; font-size: .78rem; color: var(--muted); margin-bottom: 2px; }
    .paper-meta strong { font-size: .88rem; font-weight: 700; line-height: 1.45; }
    .insight { margin: 10px 0 0; color: var(--text); font-size: .95rem; }
    .paper-facts { display: grid; gap: 8px; margin-top: 12px; }
    .paper-facts .fact { display: grid; gap: 3px; padding: 10px 12px; border-radius: 14px; background: rgba(247,249,252,.9); border: 1px solid rgba(0,0,0,.05); }
    .paper-facts .fact.good { background: linear-gradient(180deg, rgba(243,253,246,.98), rgba(255,255,255,.92)); border-color: rgba(26,174,57,.14); }
    .paper-facts .fact.bad { background: linear-gradient(180deg, rgba(255,245,245,.98), rgba(255,255,255,.92)); border-color: rgba(216,79,79,.14); }
    .paper-facts .fact.neutral { background: linear-gradient(180deg, rgba(247,249,252,.96), rgba(255,255,255,.9)); border-color: rgba(0,0,0,.05); }
    .paper-facts span { font-size: .74rem; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); font-weight: 800; }
    .paper-facts strong { font-size: .92rem; line-height: 1.45; font-weight: 600; }
    .paper-facts .fact.good strong { color: #107426; }
    .paper-facts .fact.bad strong { color: #bb2d2d; }
    .paper-actions {
      display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px;
    }
    .paper-action {
      display: inline-flex; align-items: center; justify-content: center; min-height: 40px;
      padding: 0 14px; border-radius: 999px; border: 1px solid rgba(0,0,0,.08);
      background: rgba(255,255,255,.86); color: var(--text); font-size: .88rem; font-weight: 800;
    }
    .paper-action.primary { color: white; border-color: transparent; background: linear-gradient(135deg, var(--blue), var(--violet)); box-shadow: 0 10px 20px rgba(109,74,255,.22); }
    .figure-card {
      margin-top: 12px; padding: 12px; border-radius: 18px; background: linear-gradient(180deg, rgba(255,255,255,.94), rgba(248,250,255,.92));
      border: 1px solid rgba(109,74,255,.10); box-shadow: inset 0 1px 0 rgba(255,255,255,.72);
    }
    .figure-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 10px; }
    .figure-title { font-weight: 800; font-size: .88rem; color: var(--violet); letter-spacing: .02em; }
    .figure-note { color: var(--muted); font-size: .8rem; }
    .figure-link { display: block; border-radius: 14px; overflow: hidden; border: 1px solid rgba(0,0,0,.05); background: rgba(255,255,255,.9); }
    .figure-img { display: block; width: 100%; height: auto; }
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
    .dock {
      display: none; position: sticky; bottom: 12px; z-index: 8; margin-top: 18px;
      padding: 10px; border-radius: 22px; background: rgba(255,255,255,.72); backdrop-filter: blur(18px);
      border: 1px solid rgba(255,255,255,.86); box-shadow: var(--shadow);
    }
    .dock-items { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .dock-item {
      display: inline-flex; align-items: center; justify-content: center; min-height: 44px;
      padding: 0 12px; border-radius: 16px; background: rgba(247,249,252,.96); border: 1px solid rgba(0,0,0,.06);
      font-size: .9rem; font-weight: 800;
    }
    .dock-item.primary { color: white; border: 0; background: linear-gradient(135deg, var(--blue), var(--violet)); }
    @media (max-width: 900px) {
      .hero, .grid { grid-template-columns: 1fr; }
      .paper-meta { grid-template-columns: 1fr; }
      .topbar { position: static; border-radius: 20px; }
      .hero-panel { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 560px) {
      .page { padding: 14px 12px 40px; }
      .hero { padding: 20px; border-radius: 22px; }
      .paper-card { padding: 16px; }
      h1 { font-size: 2.05rem; }
      .section-chip { flex-basis: 100%; }
      .section-head { flex-direction: column; align-items: start; }
      .archive-head { flex-direction: column; }
      .dock { display: block; }
      .hero-panel { grid-template-columns: 1fr 1fr; }
      .paper-top { flex-direction: column; }
      .arxiv-link { align-self: flex-start; }
      .paper-action { flex: 1 1 0; }
    }
    """


def render_page(
    title: str,
    body: str,
    hint: str = "自动刷新 · 手机友好 · 精选判断",
    home_link: str = "index.html",
    archive_link: str = "archive/index.html",
) -> str:
    css = build_css()
    return f"""<!doctype html>
<html lang='zh-CN'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <meta http-equiv='refresh' content='300'>
  <meta name='theme-color' content='#f6f5f4'>
  <title>{esc(title)}</title>
  <link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap' rel='stylesheet'>
  <style>{css}</style>
</head>
<body>
  <div class='page' id='top'>
    <header class='topbar'>
      <div class='brand'><span class='brand-mark'></span>{esc(title)}</div>
      <div class='hint'>{esc(hint)}</div>
    </header>
    {body}
    <nav class='dock' aria-label='页面导航'>
      <div class='dock-items'>
        <a class='dock-item primary' href='{home_link}'>首页</a>
        <a class='dock-item' href='{archive_link}'>归档</a>
        <a class='dock-item' href='#top'>顶部</a>
      </div>
    </nav>
  </div>
</body>
</html>"""


def generate_site() -> dict[str, str]:
    runs = all_runs()
    pages: dict[str, str] = {}
    if runs:
        latest = runs[0]
        pages["index.html"] = render_page(
            title="arXiv 每日摘要",
            hint=f"最新：{latest.get('run_time') or '未知'}",
            home_link='./index.html',
            archive_link='archive/index.html',
            body=build_run_detail(latest, home_link='./index.html', archive_link='archive/index.html'),
        )
        pages["archive/index.html"] = build_archive_page(runs)
        for run in runs:
            pages[f"runs/{run['slug']}.html"] = render_page(
                title="arXiv 每日摘要",
                hint=f"归档运行：{run.get('run_time') or run['slug']}",
                home_link='../index.html',
                archive_link='../archive/index.html',
                body=build_run_detail(run, home_link='../index.html', archive_link='../archive/index.html'),
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
        pages["index.html"] = render_page("arXiv 每日摘要", empty_body, home_link='./index.html', archive_link='archive/index.html')
        pages["archive/index.html"] = render_page("arXiv 历史归档", "<div class='overall-card'>暂无历史归档。</div>", hint="自动同步 GitHub", home_link='../index.html', archive_link='../archive/index.html')
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
