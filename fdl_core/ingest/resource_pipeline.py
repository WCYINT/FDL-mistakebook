"""复习资料摄入管线（多类型资源 → FDL 分析 → 参数更新）。

用途（2026-09-12 King 需求）
----------------------------
今日复习页面「上传复习资料」按钮的后端核心：把**音频 / 图片 / 视频 / 文件**
四类资料统一走一条可复跑管线，完成后让 FDL 的相关参数（复习历史、驾驶舱
数字、近 14 天趋势等）全面刷新。

管线（四阶段，与前端进度条一一对应）
------------------------------------
    ① preprocess  预处理：收集目标 → 按扩展名分类 → 校验存在性
    ② recognize   资料识别：
         audio  → SenseVoice 转写（本地离线）
         image  → ingest_photo（预处理→红黑分离→OCR→错题分段）
         video  → 提取音轨（依赖 ffmpeg；不可用则如实标记待人工）
         doc    → 文本提取（txt/md 直读；pdf 走内嵌文本）
    ③ analyze     FDL 分析：音频/文本走 LLM 结构化分析（学科/知识点/错因/掌握度）
         → 落 asr_drafts 草稿；随后 auto-apply（匹配错题 → mark_reviewed）
    ④ refresh     参数更新：调用方重新生成报告（数字卡/趋势/星图随库刷新）

设计约束
--------
- **幂等**：音频由 `asr_ingest.ingest_one` 的"已摄入跳过"保证；图片由
  ingest_photo 的归档语义保证；重复提交同一路径不会重复计数。
- **零硬编码**：路径经 `fdl_core.paths` 解析。
- **不吞异常**：单项失败记录到 errors，不中断整批（批处理纪律）。
- **进度回调**：`progress_cb(stage, percent, message)`，由调用方（serve 的
  后台线程）写入 job 表，前端轮询。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from fdl_core.paths import get_paths

# ── 类型分派表（扩展名 → 资料类别）────────────────────────────
AUDIO_EXT = {".m4a", ".wav", ".mp3", ".aac", ".flac", ".ogg", ".opus"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
DOC_EXT = {".txt", ".md", ".pdf", ".json", ".csv"}

ALL_EXT = AUDIO_EXT | IMAGE_EXT | VIDEO_EXT | DOC_EXT

# 目录递归时跳过的隐藏/缓存目录
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".obsidian", ".venv"}


@dataclass
class ResourceResult:
    """单个资源的处理结果。"""

    path: str
    kind: str  # audio / image / video / doc / unknown
    ok: bool = False
    skipped: bool = False
    detail: str = ""  # 人类可读的一句话结果
    review_needed: bool = False  # 是否需要人工复核
    meta: dict = field(default_factory=dict)


def classify(path: Path) -> str:
    """扩展名 → 资料类别。"""
    ext = Path(path).suffix.lower()
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in DOC_EXT:
        return "doc"
    return "unknown"


def collect_targets(target: Path) -> list[Path]:
    """目录 → 递归收集支持的资源；单文件 → 自身。已排序、去隐藏目录。"""
    target = Path(target)
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(target.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.name.startswith("."):
            continue
        if p.suffix.lower() in ALL_EXT:
            out.append(p)
    return out


# ── ②③ 各类型处理 ────────────────────────────────────────────
def _handle_audio(path: Path, client) -> ResourceResult:
    """音频：复用 asr_ingest（转写 → LLM 分析 → 草稿）。"""
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from asr_ingest import ingest_one

    r = ingest_one(path, analyze_llm=client is not None, client=client)
    if r.get("skipped"):
        return ResourceResult(
            str(path), "audio", ok=True, skipped=True, detail="已摄入过（草稿已存在）"
        )
    if not r.get("ok"):
        return ResourceResult(
            str(path), "audio", detail=r.get("reason") or "转写失败", review_needed=True
        )
    return ResourceResult(
        str(path),
        "audio",
        ok=True,
        detail=(
            f"转写 {r['chars']} 字 · {r['duration_sec']}s"
            + (" · 已结构化分析" if r.get("analysis_written") else " · 未分析")
        ),
        meta={"name": r.get("name"), "chars": r.get("chars")},
    )


def _handle_doc(path: Path, client) -> ResourceResult:
    """文本类：直读（txt/md/json/csv）；PDF 走内嵌文本抽取（pypdf，缺失则降级）。"""
    text = ""
    try:
        if path.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader  # 可选依赖
            except ImportError:
                return ResourceResult(
                    str(path),
                    "doc",
                    review_needed=True,
                    detail="未安装 pypdf，无法抽取 PDF 文本 → 待人工处理（可另存为 txt/md 后重传）",
                )
            reader = PdfReader(str(path))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return ResourceResult(
            str(path), "doc", detail=f"读取失败：{type(exc).__name__}: {exc}", review_needed=True
        )
    text = text.strip()
    if not text:
        return ResourceResult(str(path), "doc", detail="文件无可用文本内容", review_needed=True)
    if client is None:
        return ResourceResult(
            str(path), "doc", ok=True, detail=f"已读取 {len(text)} 字（未启用 LLM 分析）"
        )
    # LLM 分析：与音频同款结构化（学科/知识点/错因/掌握度）
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from asr_ingest import analyze as _analyze

    pseudo = {"text_raw": text[:6000]}
    a = None
    try:
        a = _analyze(pseudo, client)
    except Exception:  # noqa: BLE001 — 分析失败不丢读取成果
        a = None
    return ResourceResult(
        str(path),
        "doc",
        ok=True,
        detail=f"已读取 {len(text)} 字" + ("· 已结构化分析" if a else "· 分析未完成"),
        meta={"chars": len(text), "analysis": bool(a)},
    )


def _handle_video(path: Path, client) -> ResourceResult:
    """视频：提取音轨需 ffmpeg；不可用时如实标记待人工（不假装成功）。"""
    import shutil as _sh

    if _sh.which("ffmpeg") is None:
        return ResourceResult(
            str(path),
            "video",
            review_needed=True,
            detail="未安装 ffmpeg，无法提取音轨 → 待人工处理（可先手动导出音频再上传）",
        )
    return ResourceResult(
        str(path),
        "video",
        review_needed=True,
        detail="检测到 ffmpeg，但视频音轨流程尚未接入 → 待人工处理",
    )


def _handle_image(path: Path, subject_dir: Path, title: str) -> ResourceResult:
    """图片：走既有 ingest_photo（错题录入管线）。"""
    from fdl_core.ingest import ingest_photo

    try:
        r = ingest_photo(path, subject_dir, title=title)
    except Exception as exc:  # noqa: BLE001
        return ResourceResult(
            str(path), "image", detail=f"录入失败：{type(exc).__name__}: {exc}", review_needed=True
        )
    if r.needs_review:
        return ResourceResult(
            str(path),
            "image",
            review_needed=True,
            detail=f"OCR 置信 {r.ocr.avg_confidence:.0%} → 待人工确认",
            meta={"engine": r.ocr.engine},
        )
    return ResourceResult(
        str(path),
        "image",
        ok=True,
        detail=f"已入库（{r.ocr.engine} · {len(r.ocr.lines)} 行）",
        meta={"engine": r.ocr.engine, "lines": len(r.ocr.lines)},
    )


# ── 主入口 ───────────────────────────────────────────────────
def process_resources(
    target: Path,
    *,
    progress_cb=None,
    client=None,
    apply_reviews: bool = True,
    image_subject_dir: Path | None = None,
) -> dict:
    """处理一个路径（文件或目录）下的全部资源。

    参数：
        image_subject_dir: 图片归档目录（由调用方按路径智能分流后传入；
            缺省回退到数学学科目录）。与 fdl_serve._subject_dir 的分流语义一致。

    返回 {ok, total, by_kind, results[], applied[], errors[], needs_review[]}
    """

    def emit(stage: str, percent: int, message: str) -> None:
        if progress_cb is not None:
            try:
                progress_cb(stage, max(0, min(100, int(percent))), message)
            except Exception:  # noqa: BLE001 — 进度回调失败不影响处理
                pass

    target = Path(target)
    emit("preprocess", 2, "扫描资料…")
    targets = collect_targets(target)
    if not targets:
        return {
            "ok": True,
            "total": 0,
            "by_kind": {},
            "results": [],
            "applied": [],
            "errors": [],
            "needs_review": [],
            "message": "未找到可处理的资料（支持音频/图片/视频/文档）",
        }

    by_kind: dict[str, int] = {}
    for p in targets:
        k = classify(p)
        by_kind[k] = by_kind.get(k, 0) + 1

    emit(
        "preprocess",
        6,
        f"发现 {len(targets)} 个资料（" + "、".join(f"{k} {v}" for k, v in by_kind.items()) + "）",
    )

    paths = get_paths()
    if image_subject_dir is not None:
        subject_dir = Path(image_subject_dir)
    else:
        subject_dir = paths.subject_dir("MATH") / "03-错题快照"
    subject_dir.mkdir(parents=True, exist_ok=True)
    source_label = target.name if target.is_dir() else target.parent.name

    results: list[ResourceResult] = []
    for i, p in enumerate(targets, 1):
        kind = classify(p)
        base = 6 + int((i - 1) / len(targets) * 78)
        emit("recognize", base, f"（{i}/{len(targets)}）{kind} · {p.name}")
        try:
            if kind == "audio":
                r = _handle_audio(p, client)
            elif kind == "doc":
                r = _handle_doc(p, client)
            elif kind == "video":
                r = _handle_video(p, client)
            elif kind == "image":
                r = _handle_image(
                    p, subject_dir, f"{source_label}_{p.stem}" if source_label else p.stem
                )
            else:
                r = ResourceResult(str(p), kind, detail="不支持的类型", review_needed=True)
        except Exception as exc:  # noqa: BLE001 — 单项失败不中断
            r = ResourceResult(
                str(p), kind, detail=f"{type(exc).__name__}: {exc}", review_needed=True
            )
        results.append(r)
        emit("recognize", base + 2, f"（{i}/{len(targets)}）{r.detail[:60]}")

    # ③ 分析完成 → 音频自动 apply（匹配错题 → mark_reviewed）
    applied: list[int] = []
    if apply_reviews and any(r.kind == "audio" and r.ok and not r.skipped for r in results):
        emit("analyze", 86, "音频已落草稿，正在匹配错题并更新复习历史…")
        applied = _auto_apply_reviews()

    errors = [{"path": r.path, "error": r.detail} for r in results if not r.ok and not r.skipped]
    needs_review = [
        {"path": r.path, "kind": r.kind, "reason": r.detail} for r in results if r.review_needed
    ]
    ok_n = sum(1 for r in results if r.ok and not r.skipped)
    skip_n = sum(1 for r in results if r.skipped)
    emit(
        "analyze",
        92,
        f"处理完成：成功 {ok_n} · 跳过 {skip_n} · 待人工 {len(needs_review)}"
        + (f" · 复习记录更新 {len(applied)} 条" if applied else ""),
    )

    return {
        "ok": True,
        "total": len(targets),
        "by_kind": by_kind,
        "results": [r.__dict__ for r in results],
        "applied": applied,
        "errors": errors,
        "needs_review": needs_review,
    }


def _auto_apply_reviews(date: str | None = None) -> list[int]:
    """把新落盘的音频草稿匹配到错题并 mark_reviewed（幂等：当天重复不计数）。

    复用 scripts/apply_asr_reviews.py 的正式入口逻辑，避免两份实现。
    """
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from generate_report import _load_review_audio_analyses

    from fdl_core.db.schema import get_connection
    from fdl_core.mistakes.review import mark_reviewed
    from fdl_core.srs.asr_integration import batch_apply

    conn = get_connection(str(get_paths().primary_db_path))
    try:
        drafts = _load_review_audio_analyses()
        lookup: dict[int, dict] = {}
        for row in conn.execute(
            "SELECT id, kp_id, note_id, source_ref FROM mistake_record"
        ).fetchall():
            lookup[row[0]] = {"kp_id": row[1], "note_id": row[2] or "", "source_ref": row[3] or ""}

        def _mark(mid: int) -> bool:
            try:
                return len(mark_reviewed(conn, [mid], day=date)) > 0
            except Exception:  # noqa: BLE001
                return False

        results = batch_apply(drafts, lookup, _mark)
        return [
            r["mid"]
            for r in results
            if r.get("mid") is not None and r.get("new_status") == "已复习"
        ]
    finally:
        conn.close()


def refresh_report() -> str:
    """④ 参数更新：重新生成报告（数字卡/趋势/星图随库刷新）。返回输出路径。"""
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from generate_report import main as gen

    return str(gen(get_paths().primary_db_path))
