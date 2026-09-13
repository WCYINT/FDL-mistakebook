"""T3-02 红黑色道分离（HSV 色域掩码）。

将照片拆为两层，供下游分别消费：
- 黑笔层（题目印刷体 + Frank 手写答案）→ OCR 识别；
- 红笔层（教师/家长批注：勾、圈、订正）→ 错题判定（阶段三 MS）。

🔴 这是 2026-09-03 错题误判（红黑混排）的根治方案：HSV 中红色占 H 两段
（0-10 与 170-180），S/V 下限滤除纸张反光与浅色噪声。
"""

from __future__ import annotations

import cv2
import numpy as np

from fdl_core.ingest.errors import (
    RED_H_HI,
    RED_H_HI2,
    RED_H_LO,
    RED_H_LO2,
    RED_S_MIN,
    RED_V_MIN,
)

WHITE = 255


def _red_mask(hsv: np.ndarray) -> np.ndarray:
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    m1 = (h >= RED_H_LO) & (h <= RED_H_HI)
    m2 = (h >= RED_H_LO2) & (h <= RED_H_HI2)
    return (m1 | m2) & (s >= RED_S_MIN) & (v >= RED_V_MIN)


def split_layers(bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 `(black_layer, red_layer)` 两张 BGR 图（各自前景保留、其余置白）。

    - black_layer：非红像素保留（印刷体 + 手写答案）；
    - red_layer：仅红像素保留（批注）。
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    red = _red_mask(hsv)
    black = ~red

    black_layer = bgr.copy()
    black_layer[red] = WHITE
    red_layer = bgr.copy()
    red_layer[black] = WHITE
    return black_layer, red_layer


def red_coverage(bgr: np.ndarray) -> float:
    """红笔像素占比（粗略判断本页是否含批改）。"""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return float(_red_mask(hsv).mean())
