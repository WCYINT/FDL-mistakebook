"""T3-04 手写擦除（ING-01/06）：生成空白重做卷 `-clean` 图。

策略（v1，可退化）：
1. 黑笔层中区分「印刷体」与「手写体」：印刷体笔画小而规则（形态学开运算
   可保留），手写笔画粗大且连笔 → 用形态学估计印刷区，其余前景为手写 mask；
2. OpenCV inpaint（TELEA）按 mask 擦除手写；
3. 🔴 ING-06 硬约束：`-original` 永不覆盖、删除原图操作在代码层禁止——
   `guard_original` 在任何写盘前调用，擦除函数签名只接受 clean 输出路径；
4. 退化路径：mask 覆盖率异常（>60% 或 <1%）→ 不擦除，返回 None
   （调用方保留原图 + 人工遮盖，不猜测）。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from fdl_core.ingest.color_split import split_layers
from fdl_core.ingest.errors import OriginalProtectedError

# 手写 mask 占比阈值（超出视为分离失败，走退化路径）
MASK_RATIO_MIN, MASK_RATIO_MAX = 0.01, 0.60


def guard_original(path: str | Path) -> Path:
    """🔴 ING-06 守门：原图本身禁止作为任何写操作的目标。

    判定：stem 以 `-original` 结尾（如 `M-xxx-original.jpg`）→ 原图，拦截；
    `-original-clean.jpg`（stem 以 `-clean` 结尾）是合法的擦除产物，放行。
    """
    p = Path(path)
    if p.stem.endswith("-original"):
        raise OriginalProtectedError(f"ING-06：禁止写入/覆盖原图 {p}")
    return p


def build_handwriting_mask(black_layer: np.ndarray) -> np.ndarray | None:
    """黑笔层 → 手写 mask（True=手写像素）；分离失败返回 None（退化）。"""
    gray = cv2.cvtColor(black_layer, cv2.COLOR_BGR2GRAY)
    _, fg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # 印刷体：小核开运算保留；手写：粗笔画/连笔在开运算后消失 → 为手写候选
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    printed = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
    mask = cv2.subtract(fg, printed)
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))  # 扩张覆盖笔画边缘
    ratio = mask.mean() / 255.0
    if ratio < MASK_RATIO_MIN or ratio > MASK_RATIO_MAX:
        return None
    return mask


def erase_handwriting(bgr: np.ndarray, *, clean_out: str | Path | None = None) -> np.ndarray | None:
    """擦除手写 → 返回 clean 图；分离失败返回 None（走退化路径）。

    `clean_out` 提供时写盘（路径经 ING-06 守门）。
    """
    if clean_out is not None:
        guard_original(clean_out)  # 🔴 先守门再动笔
    black_layer, _ = split_layers(bgr)
    mask = build_handwriting_mask(black_layer)
    if mask is None:
        return None
    clean = cv2.inpaint(black_layer, mask, 3, cv2.INPAINT_TELEA)
    if clean_out is not None:
        cv2.imwrite(str(clean_out), clean)
    return clean
