"""批次 4 · SRS-06 年限层 + SRS-07 减压模式补全（PRD §6.6 调度参数）。

- 年限层（annual_tier_after=10）：累计复习 ≥10 且近 2 次 grade≥2（Good）
  → 间隔下限 120 天（不压短已有更长间隔）。
- 减压模式触发条件补全（relief_mode）：逾期 >30 **或** 连续 3 天未完成
  （incomplete_days_streak ≥3）→ 触发，处置已在 daily_task（停新学+复习上限 10）。
"""

from __future__ import annotations

import sqlite3

from fdl_core.srs.params import ModelParams

ANNUAL_TIER_AFTER = 10  # 累计复习次数阈值
ANNUAL_TIER_FLOOR_DAYS = 120  # 年限层间隔下限
LAST_TWO_GOOD = 2  # 近 2 次 grade≥2
RELIEF_INCOMPLETE_DAYS = 3  # 连续未完成天数阈值


def apply_annual_tier(
    *,
    interval: int,
    total_answers: int,
    last_two_grades: list[int],
    params: ModelParams | None = None,
) -> int:
    """年限层：条件满足 → 间隔下限 120 天；仅拉长不压短。"""
    p = (params or ModelParams.load()).scheduling
    threshold = p.get("annual_tier_after", ANNUAL_TIER_AFTER)
    floor = p.get("annual_tier_floor_days", ANNUAL_TIER_FLOOR_DAYS)
    if (
        total_answers >= threshold
        and len(last_two_grades) >= 2
        and all(g >= 2 for g in last_two_grades)
    ):
        return max(interval, floor)
    return interval


def incomplete_days_streak(
    conn: sqlite3.Connection,
    *,
    user_id: int = 1,
    today: str,
) -> int:
    """截至 today（不含）的连续未完成任务天数（PENDING 未完成即计入）。"""
    from datetime import date, timedelta

    d0 = date.fromisoformat(today) if isinstance(today, str) else today
    streak = 0
    for i in range(1, 15):  # 最多回看 14 天
        d = (d0 - timedelta(days=i)).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) FROM daily_task WHERE user_id=? AND task_date=? AND status!='DONE'",
            (user_id, d),
        ).fetchone()
        if row[0] > 0:
            streak += 1
        else:
            break
    return streak


def relief_should_trigger(
    conn: sqlite3.Connection,
    *,
    user_id: int = 1,
    overdue_over: int = 30,
    today: str | None = None,
) -> bool:
    """减压触发：逾期积压 >30 或 连续 3 天未完成（SRS-07 补全）。"""
    from fdl_core.srs.time_layer import local_date

    d = today or local_date().isoformat()
    overdue = conn.execute(
        "SELECT COUNT(*) FROM review_schedule"
        " WHERE user_id=? AND status='PENDING' AND due_date < ?",
        (user_id, d),
    ).fetchone()[0]
    if overdue > overdue_over:
        return True
    return incomplete_days_streak(conn, user_id=user_id, today=d) >= RELIEF_INCOMPLETE_DAYS
