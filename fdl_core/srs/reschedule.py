"""作答后重排程（SRS 闭环核心 · 审计补齐 2026-09-06）。

🔴 缺口背景：BOOTSTRAP_S/I、K_GROW/K_DECAY、INTERVAL_FACTOR 在 §6.5 定义，
但此前**无任何代码引用**——S 更新、间隔计算、review_schedule 写入缺失，
复习计划队列（_due_reviews）消费一个永远为空的池子，记忆曲线无从体现。

本模块补齐：作答事件 → S 更新 → 下次间隔 I → review_schedule 写入。

S 更新规则（工程推导，标注为工程决策待 King 复核）：
- 答对（grade≥1）：S_new = S_old × (1 + K_GROW × (1−R) × grade_weight)
  （R 越低说明越难记，增长越多——FSRS"成功回忆后记忆增强"语义；
   grade_weight: {1: 0.3, 2: 0.6, 3: 1.0}）
- 答错（grade=0）：S_new = max(S_MIN, S_old × K_DECAY)（遗忘重置）
- 间隔：I_next = clamp(S_new × INTERVAL_FACTOR, BOOTSTRAP_I[grade], I_MAX)
  （I = S × 1.637 即"R 衰减到 R_TARGET=0.85 的天数"——艾宾浩斯节律的解析解）
- 抖动 ±5%（interval_fuzz，写 schedule 时应用）
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass

from fdl_core.srs.params import ModelParams
from fdl_core.srs.time_layer import fmt_ts, now_utc

GRADE_WEIGHT = {0: 0.0, 1: 0.3, 2: 0.6, 3: 1.0}
I_MAX_DAYS = 365.0


@dataclass
class RescheduleResult:
    kp_id: int
    grade: int
    s_old: float
    s_new: float
    interval_days: float
    due_date: str
    review_schedule_id: int | None


def _bootstrap(params: ModelParams, key: str, grade: int, default: float) -> float:
    m = params.mastery
    table = m.get(key) or {}
    v = table.get(grade)
    if v is None:
        v = table.get(min(grade, max(table or {0: default})))  # 兜底取最低档
    return float(v) if v is not None else default


def update_stability(s_old: float, grade: int, retrievability: float, params: ModelParams) -> float:
    """作答后 S 更新：答对按 R 调制增长，答错 ×K_DECAY 重置。"""
    m = params.mastery
    s_min, s_max = m["S_MIN"], m["S_MAX"]
    if grade == 0:
        s_new = max(s_min, s_old * m["K_DECAY"])
    else:
        grow = 1.0 + m["K_GROW"] * (1.0 - min(retrievability, 1.0)) * GRADE_WEIGHT[grade]
        s_new = s_old * grow
    cap = s_max
    if "S_CAP_GROWTH" in m and s_old > m["S_CAP_GROWTH"]:
        cap = s_old  # 超过 S_CAP_GROWTH 后不再增长（只防衰减过低）
    return round(min(max(s_new, s_min), min(s_max, cap)), 4)


def next_interval_days(s_new: float, grade: int, params: ModelParams) -> float:
    """下次间隔 = S_new × INTERVAL_FACTOR（R 衰减到 R_TARGET 的解析解），下限 BOOTSTRAP_I。"""
    m = params.mastery
    i = s_new * m["INTERVAL_FACTOR"]
    floor_i = _bootstrap(params, "BOOTSTRAP_I", grade, 1.0)
    return round(max(i, floor_i, 1.0), 2)


def post_answer_reschedule(
    conn: sqlite3.Connection,
    *,
    kp_id: int,
    user_id: int = 1,
    grade: int,
    answered_at: str | None = None,
    params: ModelParams | None = None,
    fuzz: bool = True,
) -> RescheduleResult:
    """作答后：更新 kp_state.stability_days/retrievability + 写 review_schedule。

    幂等安全：同 KP 旧 PENDING 计划置 DONE（被新计划取代）。

    ⚠️⚠️⚠️ A1 遗留函数 —— 启用前必须重构 ⚠️⚠️⚠️
    本函数调度单元仍用 kp_id（见下方「作废」与「写入」两处），与 A3 的
    mistake_id 范式不兼容：
      - kp_id 在错题体系里恒为 0（知识点未挂载）→ 作废条件
        `WHERE user_id=? AND kp_id=? AND status='PENDING'` 会误伤其他错题的 PENDING 计划；
      - INSERT 未写 mistake_id → review_schedule.mistake_id 范式缺失，A3 闭环无法定位。
    当前无生产调用方（死代码）。若要启用：把签名加 `mistake_id` 参数，
    作废改为 `WHERE user_id=? AND mistake_id=? AND status='PENDING'`，
    INSERT 增加 `mistake_id` 列与值。重构前请勿在生产路径调用。
    """
    p = params or ModelParams.load()
    stamp = answered_at or fmt_ts(now_utc())
    today = stamp[:10]

    row = conn.execute(
        "SELECT stability_days, last_review_at FROM kp_state WHERE user_id=? AND kp_id=?",
        (user_id, kp_id),
    ).fetchone()
    m = p.mastery
    if row is None:
        # 冷启动：用首答档位（T1 刚转 LEARNING，kp_state 尚未落行）
        s_old = _bootstrap(p, "BOOTSTRAP_S", grade, 0.8)
        r_at_answer = 0.85  # 首答按 R_TARGET 档
        cold = True
    else:
        s_old = float(row[0])
        cold = False
        # 🔴 R(t) 由「上次复习 → 本次作答」的时间流逝现算（记忆曲线本质），
        # 不读存储的 retrievability 字段（该字段上次作答后被重置为 1.0，
        # 批处理未跑时无衰减——连续作答会误判 R=1.0 导致 S 永不增长）
        import datetime as dt

        last_rev = row[1]
        if last_rev:
            days_since = max(
                (dt.date.fromisoformat(stamp[:10]) - dt.date.fromisoformat(last_rev[:10])).days,
                0,
            )
            r_at_answer = (1 + m["F"] * days_since / s_old) ** m["C"]
        else:
            r_at_answer = 0.85

    s_new = update_stability(s_old, grade, r_at_answer, p)
    i_next = next_interval_days(s_new, grade, p)
    if fuzz:
        f = p.scheduling.get("interval_fuzz", 0.05)
        i_next = round(i_next * (1 + random.uniform(-f, f)), 2)
    # 间隔先取整再与 date 相加：避免 timedelta(days=0.95).days==0 塌缩成"今天到期"。
    # max(1, ...) 保证至少 1 天（fsrs_next_interval 可能给出 <1 的浮点间隔）。
    due = (
        __import__("datetime").date.fromisoformat(today)
        + __import__("datetime").timedelta(days=max(1, int(round(i_next))))
    ).isoformat()

    # S/R 回写（kp_state 行由 T1 落；冷启动兜底补行）
    if cold:
        conn.execute(
            "INSERT INTO kp_state (user_id, kp_id, subject_id, status, exposure_count,"
            " total_answer_count, difficulty, stability_days, retrievability,"
            " last_review_at, created_at, updated_at)"
            " SELECT ?, ?, subject_id, 'LEARNING', 1, 1, base_difficulty, ?, 1.0, ?, ?, ?"
            " FROM knowledge_point WHERE id=?",
            (user_id, kp_id, s_new, stamp, stamp, stamp, kp_id),
        )
    else:
        conn.execute(
            "UPDATE kp_state SET stability_days=?, retrievability=1.0,"
            " last_review_at=?, updated_at=? WHERE user_id=? AND kp_id=?",
            (s_new, stamp, stamp, user_id, kp_id),
        )

    # 旧 PENDING 作废 + 新计划写入
    # ⚠️ A1 遗留：作废按 kp_id（应改 mistake_id，见函数 docstring 警告）
    conn.execute(
        "UPDATE review_schedule SET status='DONE', updated_at=?"
        " WHERE user_id=? AND kp_id=? AND status='PENDING'",
        (stamp, user_id, kp_id),
    )
    cur = conn.execute(
        # ⚠️ A1 遗留：INSERT 未写 mistake_id（应补 mistake_id 列与值，见函数 docstring 警告）
        "INSERT INTO review_schedule (user_id, kp_id, subject_id, due_date, due_session,"
        " planned_interval_days, interval_fuzz_applied, status, source, priority_score,"
        " est_seconds, created_at, updated_at)"
        " SELECT ?, ?, subject_id, ?, 'PM', ?, ?, 'PENDING', 'answer', 50.0, 60, ?, ?"
        " FROM knowledge_point WHERE id=?",
        (user_id, kp_id, due, i_next, fuzz, stamp, stamp, kp_id),
    )
    schedule_id = cur.lastrowid
    conn.commit()
    return RescheduleResult(kp_id, grade, s_old, s_new, i_next, due, schedule_id)
