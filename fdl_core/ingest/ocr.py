"""T3-03 OCR 双引擎（ING-02）：Apple Vision 主 + RapidOCR 兜底。

统一输出 `[{text, confidence, bbox}]`（bbox=[x, y, w, h] 像素坐标）。
降级策略（PRD ING-02）：Apple Vision 为默认引擎；平均置信度低于阈值或
调用异常时**自动降级** RapidOCR；仍不确定则交人工确认（不猜测）。
两引擎均可离线运行（NFR-1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from fdl_core.ingest.errors import IngestError

# 降级语义（2026-09-04 根因修正）：
# Apple Vision 的 confidence 是三级离散档位（low=0.3 / medium=0.5 / high=1.0），
# 中文整行识别大量落 medium——avg_confidence 是档位均值而非错误率，
# 用连续阈值判定会必然误降级（实测：完全正确的印刷长句 conf=0.5）。
# 因此 Vision 不做置信度降级（识别质量由 PRD 实测 99.2-99.6% 背书），
# 仅在**异常或空结果**时降级；RapidOCR 作为兜底引擎。
MIN_VALID_LINES = 1  # Vision 有效行数下限（低于视为识别失败）
OCR_MAX_SIDE = 1600  # 缩放上限（24MP 全分辨率 RapidOCR ~47s，缩后 ~3s，精度几乎无损）


@dataclass
class OcrLine:
    text: str
    confidence: float
    bbox: list[int]  # [x, y, w, h]


@dataclass
class OcrResult:
    engine: str
    lines: list[OcrLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)

    @property
    def avg_confidence(self) -> float:
        return sum(ln.confidence for ln in self.lines) / len(self.lines) if self.lines else 0.0


def _shrink(bgr: np.ndarray, max_side: int = OCR_MAX_SIDE) -> tuple[np.ndarray, float]:
    """等比缩放到 max_side 内，返回 (缩放图, scale)（bbox 乘 1/scale 映射回原图）。"""
    h, w = bgr.shape[:2]
    side = max(h, w)
    if side <= max_side:
        return bgr, 1.0
    scale = max_side / side
    small = cv2.resize(bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return small, scale


def _rescale_bbox(lines: list[OcrLine], scale: float) -> None:
    if scale == 1.0:
        return
    inv = 1.0 / scale
    for ln in lines:
        ln.bbox = [int(v * inv) for v in ln.bbox]


def _to_png_bytes(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise IngestError("图片编码失败")
    return buf.tobytes()


# ── 引擎 1：Apple Vision（macOS 本地，离线）──────────────────
def _ocr_apple_vision(bgr: np.ndarray) -> OcrResult:
    import Vision
    from Foundation import NSData

    png = _to_png_bytes(bgr)
    nsdata = NSData.dataWithBytes_length_(png, len(png))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(nsdata, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["zh-Hans", "en-US"])
    req.setUsesLanguageCorrection_(True)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise IngestError(f"Apple Vision 失败：{err}")

    h, w = bgr.shape[:2]
    lines: list[OcrLine] = []
    for obs in req.results():
        cand = obs.topCandidates_(1)[0]
        bb = obs.boundingBox()  # 归一化，origin 在左下
        x, y = bb.origin.x * w, (1 - bb.origin.y - bb.size.height) * h
        lines.append(
            OcrLine(
                text=cand.string(),
                confidence=float(cand.confidence()),
                bbox=[int(x), int(y), int(bb.size.width * w), int(bb.size.height * h)],
            )
        )
    lines.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))  # 阅读序：先上后下、先左后右
    return OcrResult(engine="apple_vision", lines=lines)


# ── 引擎 2：RapidOCR（onnx 本地，离线）───────────────────────
def _ocr_rapidocr(bgr: np.ndarray) -> OcrResult:
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    result, _ = engine(bgr)
    lines: list[OcrLine] = []
    for box, text, score in result or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append(
            OcrLine(
                text=str(text),
                confidence=float(score),
                bbox=[int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys))],
            )
        )
    lines.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
    return OcrResult(engine="rapidocr", lines=lines)


# ── 调度：主引擎 + 异常降级 ─────────────────────────────────
def recognize(bgr: np.ndarray, *, force_engine: str | None = None) -> OcrResult:
    """识别入口：Apple Vision 为主引擎；仅**异常或空结果**时降级 RapidOCR。

    降级语义（根因修正）：Vision 的 confidence 是离散档位（0.3/0.5/1.0），
    不与 RapidOCR 的连续置信度可比——**不做置信度比较降级**。
    大图先缩放（OCR_MAX_SIDE），bbox 映射回原坐标。
    `force_engine`：测试用强制指定（"apple_vision" / "rapidocr"）。
    两引擎均失败 → IngestError（交人工确认，不猜测——对齐用户要求）。
    """
    small, scale = _shrink(bgr)
    if force_engine == "rapidocr":
        r = _ocr_rapidocr(small)
        _rescale_bbox(r.lines, scale)
        return r
    try:
        result = _ocr_apple_vision(small)
        if force_engine == "apple_vision":
            _rescale_bbox(result.lines, scale)
            return result
        if len(result.lines) >= MIN_VALID_LINES:
            # 主引擎正常出结果 → 直接采信（档位置信度不作降级依据，见上注）
            _rescale_bbox(result.lines, scale)
            return result
        # Vision 空结果 → 兜底引擎
        fallback = _ocr_rapidocr(small)
        _rescale_bbox(fallback.lines, scale)
        return fallback
    except (IngestError, ImportError):
        if force_engine == "apple_vision":
            raise
        r = _ocr_rapidocr(small)  # Vision 不可用（权限/系统）→ 兜底
        _rescale_bbox(r.lines, scale)
        return r
