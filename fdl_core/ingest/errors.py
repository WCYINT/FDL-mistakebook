"""ING 录入子包共享错误与常量。"""

from __future__ import annotations


class IngestError(ValueError):
    """录入管线错误。"""


class OriginalProtectedError(PermissionError):
    """🔴 违反 ING-06：试图写入/覆盖 -original 原图。"""


# 红笔色域（HSV）：H 两段（0-10 / 170-180），S/V 下限滤除浅色噪声
RED_H_LO, RED_H_HI = 0, 10
RED_H_LO2, RED_H_HI2 = 170, 180
# 阈值 2026-09-05 数据驱动重定（C2 实测直方图）：
# 红笔主体 H=10-15（S 中位 82）——原 H 上限 10 偏窄漏掉主峰；
# 纸色 H15-20 S≈50，S≥70 可分离。实测红笔占比 0.3%→3.2-4.2%（与视觉相符）
RED_H_HI = 16
RED_H_LO2 = 172
RED_S_MIN, RED_V_MIN = 70, 60
