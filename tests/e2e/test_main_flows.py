"""端到端测试骨架（C5.3）：5 条主链路。

阶段一仅定义链路骨架；阶段二/四实现后取消 skip 并填充断言。
覆盖 PRD §4.2.4 五条主链路。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="阶段二起填充 NiceGUI 主链路")


def test_flow_ingest():
    """链路 1：录入（King 拍照/OCR/错题）→ 双写 SQLite + Markdown。"""
    raise NotImplementedError("阶段三实现：ING 主链路")


def test_flow_daily_review():
    """链路 2：每日复习（Frank 答题）→ answer_log 全字段回写。"""
    raise NotImplementedError("阶段二实现：SRS 每日复习链路")


def test_flow_attribution():
    """链路 3：归因（答错 → 行为化归因 → FSRS rating 映射）。"""
    raise NotImplementedError("阶段三实现：MS 归因重试链")


def test_flow_metric_aggregation():
    """链路 4：指标聚合（daily_metric 快照，可视化只读此表）。"""
    raise NotImplementedError("阶段四实现：MT 预聚合")


def test_flow_weekly_report():
    """链路 5：周报生成（weekly_metric + NMKP + SVG 报告）。"""
    raise NotImplementedError("阶段四实现：VIS 周报")
