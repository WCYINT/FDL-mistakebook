"""测试辅助（仅供 tests/ 使用）：合成"作业页"图生成器。"""

from __future__ import annotations

import cv2
import numpy as np


def make_page(path, text_lines: int = 3, red_marks: bool = False) -> str:
    """生成一张合成作业页（白底 + 黑色标题行 + 可选红笔批注），返回路径。"""
    page = np.full((1120, 864, 3), 255, dtype=np.uint8)
    for i in range(text_lines):
        y = 150 + i * 220
        cv2.putText(
            page,
            f"Homework {i + 1}",
            (80, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.2,
            (30, 30, 30),
            2,
            cv2.LINE_AA,
        )
        cv2.line(page, (80, y + 14), (760, y + 14), (20, 20, 20), 3)
    if red_marks:
        cv2.circle(page, (700, 300), 40, (0, 0, 255), 6)
    cv2.imwrite(str(path), page)
    return str(path)
