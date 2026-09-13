"""T3-01 预处理管线：方向矫正 + 透视矫正（ING-01）。

针对真实手机照的三类几何问题：
1. EXIF 方向标记（部分手机不写）→ 显式应用；
2. 90°/270° 旋转（横拍竖排）→ 文本行投影方差法自动检测；
3. 页面透视变形 → 最大四边形轮廓 + 单应性变换。

输出标准化 BGR 图（JPEG 质量固定，保证可复现）。
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageOps

from fdl_core.ingest.errors import IngestError


def load_bgr(path: str) -> np.ndarray:
    """读图 + EXIF 方向修正 → BGR ndarray。"""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # 应用 EXIF orientation
    if img is None:
        raise IngestError(f"无法读取图片：{path}")
    return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)


def _text_row_variance(gray: np.ndarray) -> float:
    """文本行水平时行投影方差最大（行间白隙与文字行交替）。"""
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    profile = bw.sum(axis=1).astype(np.float64)
    return float(profile.var())


def detect_rotation(gray: np.ndarray) -> int:
    """检测需要顺时针旋转的角度（0/90/180/270）。

    对每个候选方向计算行投影方差，文字行呈水平时方差最大。
    """
    best_angle, best_score = 0, -1.0
    for angle in (0, 90, 180, 270):
        rotated = np.rot90(gray, k=angle // 90)
        score = _text_row_variance(rotated)
        if score > best_score:
            best_angle, best_score = angle, score
    return best_angle


def deskew(gray: np.ndarray) -> np.ndarray:
    """应用检测出的旋转，返回文字行水平的图。"""
    angle = detect_rotation(gray)
    return np.rot90(gray, k=angle // 90) if angle else gray


def perspective_crop(bgr: np.ndarray, min_area_ratio: float = 0.3) -> np.ndarray:
    """检测最大四边形（页面）并做透视矫正；未检出则返回原图。"""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = bgr.shape[:2]
    page_area = h * w
    best: tuple[float, np.ndarray] | None = None
    for c in contours:
        area = cv2.contourArea(c)
        if area < page_area * min_area_ratio:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            if best is None or area > best[0]:
                best = (area, approx.reshape(4, 2))
    if best is None:
        return bgr
    pts = best[1].astype(np.float32)
    # 顶点排序：tl/tr/br/bl（按和与差）
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    tl, br, tr, bl = pts[np.argmin(s)], pts[np.argmax(s)], pts[np.argmin(d)], pts[np.argmax(d)]
    tw, th = int(np.linalg.norm(br - bl)), int(np.linalg.norm(tr - tl))
    th2 = int(np.linalg.norm(tl - bl))
    width, height = max(tw, 100), max(max(th, th2), 100)
    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype=np.float32), dst)
    return cv2.warpPerspective(bgr, m, (width, height))


def detect_rotation_v2(bgr: np.ndarray) -> int:
    """方向判定 v2（2026-09-10）：方差法定"行水平组" + OCR 消 180° 歧义。

    根因：行投影方差对 180° 完全对称（k 与 k+2 同分）——方差法只能判
    "文字行是否水平"，无法区分正放/倒放。P2 实测 k=1 与 k=3 同分
    （99,931,682,076），正倒选错时子图文字垂直（King："题目是横着的，
    截图要横着"）。

    消歧：在方差法选出的行水平组（k 与 k+2）内，各取一个宽 ~1400px 的
    低分辨率缩略图跑 OCR，**中文字符产出多者**为正方向（倒置时 OCR 仍能
    读但产出显著减少）。仅组内 2 次 OCR，成本可控。
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    k_rep = detect_rotation(gray) // 90  # 行水平组代表（0/2 或 1/3）
    group = (k_rep, (k_rep + 2) % 4)

    from fdl_core.ingest import ocr as _ocr

    def _cn_count(k: int) -> int:
        rot = np.rot90(bgr, k=k)
        scale = 1400 / max(rot.shape[1], 1)
        small = (
            cv2.resize(rot, (1400, max(1, int(rot.shape[0] * scale))))
            if rot.shape[1] > 1400
            else rot
        )
        r = _ocr.recognize(small)
        return len(__import__("re").findall(r"[\u4e00-\u9fff]", r.text))

    counts = {k: _cn_count(k) for k in group}
    return max(counts, key=counts.get)


def preprocess(path: str) -> np.ndarray:
    """完整预处理：读图（EXIF）→ 方向矫正（v2 含 180° 消歧）→ 透视矫正 → 标准化。"""
    bgr = load_bgr(path)
    angle_k = detect_rotation_v2(bgr)
    if angle_k:
        bgr = np.rot90(bgr, k=angle_k)
    return perspective_crop(bgr)
