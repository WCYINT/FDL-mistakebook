"""影子测试集（C5.2）：4 个边界场景。

阶段一仅定义边界场景骨架；阶段二 SRS 8 态跃迁实现后取消 skip 并填充断言。

边界场景追溯：
- G11 同日多次作答规则
- G12 回归（已掌握 KP 长时间未复习）
- G14 双写冲突（note_id 一致性）
- G15 中断恢复
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="阶段二 SRS 8 态跃迁实现后启用")


def test_g11_same_day_repeat_answer():
    """G11 同日多次作答：同一 KP 同日多次作答时，只记一次有效作答（防刷）。"""
    raise NotImplementedError("阶段二实现：验证 answer_log 同日去重逻辑")


def test_g12_regression():
    """G12 回归：已掌握 KP 因长期未复习，R(t) 衰减触发降级到待复习态。"""
    raise NotImplementedError("阶段二实现：验证 8 态跃迁中的降级顺序（降级 > 升级）")


def test_g14_double_write_conflict():
    """G14 双写冲突：SQLite 与 Markdown 双写中断时，note_id 双向索引保持一致。"""
    raise NotImplementedError("阶段三实现：验证 ING 双写 note_id 一致性 100%")


def test_g15_interruption_recovery():
    """G15 中断恢复：作答中途中断，重启后不产生半写入状态，可安全恢复。"""
    raise NotImplementedError("阶段二实现：验证作答事务的原子性与恢复")
