"""T3-05 端到端录入管线 + 归档（ING-01/02/06）。

拍照 → 预处理（矫正）→ 红黑分离 → OCR（双引擎）→ 手写擦除 → 双图归档。

归档布局（PRD §5.3）：
- `04-真实学习物/YYYY-MM/`：original 原始照（🔴 不可覆盖）；
- 错题快照目录：`-original` / `-clean` 并存（`-clean` 供空白重做，ING-07）。
计时埋点贯穿（PRD 预算：单题端到端 ≤ 30 秒）。
OCR 无法辨认的内容标记 `needs_review: true`（待确认项，不猜测）。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from fdl_core.ingest import ocr
from fdl_core.ingest.color_split import red_coverage, split_layers
from fdl_core.ingest.erase import erase_handwriting
from fdl_core.ingest.preprocess import preprocess
from fdl_core.srs.time_layer import local_date

END_TO_END_BUDGET_SEC = 30.0  # PRD §5.6.2 单题预算


@dataclass
class IngestResult:
    """一次录入的完整结果（含计时与待确认标记）。"""

    source: str
    original_path: Path
    clean_path: Path | None
    ocr: ocr.OcrResult
    red_ratio: float
    elapsed_sec: float
    needs_review: bool = False  # 🔴 OCR 低置信 → 待人工确认（不猜测）
    warnings: list[str] = field(default_factory=list)


def ingest_photo(
    image_path: str | Path,
    subject_dir: str | Path,
    *,
    title: str | None = None,
    clean_out: str | Path | None = None,
    force_engine: str | None = None,
) -> IngestResult:
    """端到端录入：真实照片 → 结构化文本 + 双图归档。

    `force_engine`：OCR 引擎强制指定（"apple_vision"/"rapidocr"），None=自动调度。
    """
    t0 = time.perf_counter()
    src = Path(image_path)
    if not src.exists():
        raise FileNotFoundError(src)
    sd = Path(subject_dir)
    day = local_date().isoformat()[:7].replace("-", "-")

    # 1-2. 预处理 + 红黑分离
    bgr = preprocess(str(src))
    black_layer, _ = split_layers(bgr)

    # 3. OCR（黑笔层为主识别对象；空结果时全图复跑兜底）
    result = ocr.recognize(black_layer, force_engine=force_engine)
    if not result.lines:
        result = ocr.recognize(bgr, force_engine=force_engine)

    # 4. 手写擦除（失败走退化路径，不阻塞）
    clean_path: Path | None = None
    clean = erase_handwriting(bgr, clean_out=str(clean_out) if clean_out else None)
    if clean is not None and clean_out is not None:
        clean_path = Path(clean_out)

    # 5. 归档（2026-09-10 King："首先识别图片方向"）：
    #    - 主归档 = preprocess 输出（**方向矫正后**，文字横排可读），
    #      PNG 无损（King 规则：不压缩；rot90 无信息损失，重存为无损格式）；
    #    - 原始字节（可能躺倒）copy2 留档到 original/ 子目录，可追溯不丢失。
    month_dir = sd / "04-真实学习物" / day[:7]
    month_dir.mkdir(parents=True, exist_ok=True)
    orig_dir = month_dir / "original"
    orig_dir.mkdir(parents=True, exist_ok=True)
    archived = month_dir / f"{title or src.stem}.png"
    if not archived.exists():  # 🔴 已存在则不覆盖（保护归档）
        cv2.imwrite(str(archived), bgr)  # PNG 无损
    orig_copy = orig_dir / src.name
    if not orig_copy.exists():
        shutil.copy2(str(src), str(orig_copy))  # 原始字节留档

    elapsed = time.perf_counter() - t0
    needs_review = (not result.lines) or result.avg_confidence < 0.60
    warnings: list[str] = []
    if elapsed > END_TO_END_BUDGET_SEC:
        warnings.append(f"端到端 {elapsed:.1f}s 超出 PRD 预算 30s")
    if not result.lines:
        warnings.append("OCR 无有效文本——标记待人工确认")

    return IngestResult(
        source=str(src),
        original_path=archived,
        clean_path=clean_path,
        ocr=result,
        red_ratio=red_coverage(bgr),
        elapsed_sec=elapsed,
        needs_review=needs_review,
        warnings=warnings,
    )
