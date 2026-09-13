"""8 态 14 跃迁状态机（P2-09 / SRS-04）—— 全阶段最复杂单任务。

🔴 三条铁律（PRD §3.3.3）：
1. **跃迁只在作答事件后触发**（T14 图谱修订除外）；升级绝不因时间流逝自动发生。
2. **每日批处理只跑 T5/T8/T11/T12**（时间驱动类），绝不处理升级跃迁。
3. **评估顺序降级 > 升级**：T3 → T6/T7 → T9/T10/T13 → T2 → T4（防状态抖动）。

T11 工程推断（PRD 未明示源态）：`STRUGGLING → REGRESSED（R<0.35，批处理）`——
否则 STRUGGLING 无时间驱动出口（T10 需 reteach 人工介入）。待 King 复核。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from fdl_core.srs.params import ModelParams
from fdl_core.srs.time_layer import (
    days_between,
    local_date,
    local_date_of_lenient,
    now_utc,
)

UNLEARNED = "UNLEARNED"
LEARNING = "LEARNING"
REVIEWING = "REVIEWING"
STRUGGLING = "STRUGGLING"
MASTERED = "MASTERED"
CONSOLIDATED = "CONSOLIDATED"
REGRESSED = "REGRESSED"
ARCHIVED = "ARCHIVED"

STATES = (UNLEARNED, LEARNING, REVIEWING, STRUGGLING, MASTERED, CONSOLIDATED, REGRESSED, ARCHIVED)

# 升级类跃迁（同日最多 1 次，PRD §1.4）
UPGRADE_RULES = {"T1", "T2", "T4", "T9", "T10", "T13"}

_NO_AGAIN_DAYS = 14  # T4"近14天无Again"


def _as_local_date(s: str | date) -> date:
    """快照里的日期字段 → 本地学习日（date）。

    [注意] 为什么不能用 ``parse_ts(s).date()``（2026-09-13 修复）：
    - ``YYYY-MM-DD``（10 字符，KP 快照的日期列一律是本地日期）被 ``parse_ts``
      当**本地时间**解析，再 ``to_utc()`` 转成 UTC → 日期回退一天
      （如 ``2026-09-01`` → ``2026-08-31T16:00Z`` → ``.date()==08-31``），
      使 T4 的 14 天窗实为 13 天、T12 的 120 天实为 119 天。
    - 带时刻的完整时间戳（如 entered_mastered_at 的 ISO UTC）才需要真转换，
      且必须走宽容解析（语义 UTC），不能当本地时间。

    故：10 字符按纯日期直读；其余走 ``local_date_of_lenient``。
    """
    if isinstance(s, date):
        return s
    t = str(s).strip()
    if len(t) == 10:
        return date.fromisoformat(t)
    return local_date_of_lenient(t)


@dataclass
class KpSnapshot:
    """作答/批处理时刻的 kp_state 快照（状态机唯一输入）。"""

    code: str
    status: str
    exposure_count: int
    n_eff: float
    total_answer_count: int
    consecutive_good_count: int
    consecutive_again_count: int
    stability_days: float
    retrievability: float
    m_adj: float
    m_adj_before: float  # 本次事件前（T6/T7 的 ΔM_adj）
    grade: int | None = None  # 本次作答（批处理为 None）
    is_reteach: bool = False  # 本次作答是否重教场景
    plan_exists: bool = False  # 首条复习计划已生成（T2）
    prereqs_ok: bool = True  # T1 前置门禁（P2-05）
    overdue_ratio: float = 0.0  # T8：逾期 / 计划间隔
    last_again_date: str | None = None  # 最近 Again 的本地日期
    entered_mastered_date: str | None = None  # T5 的 30 天计时起点
    mastered_review_grades: list[int] = field(default_factory=list)  # MASTERED 期间作答
    last_review_date: str | None = None  # T12 距上次复习
    is_active: bool = True


@dataclass
class TransitionResult:
    new_status: str
    rule: str | None  # T1..T14；None = 无跃迁
    reason: str

    @property
    def changed(self) -> bool:
        return self.rule is not None

    @property
    def is_upgrade(self) -> bool:
        return self.rule in UPGRADE_RULES


def _no_change(status: str) -> TransitionResult:
    return TransitionResult(new_status=status, rule=None, reason="条件不满足")


def evaluate_answer(
    s: KpSnapshot,
    params: ModelParams | None = None,
    today: date | None = None,
) -> TransitionResult:
    """作答事件后的跃迁评估（T1/T2/T3/T4/T6/T7/T9/T10/T13）。

    顺序铁律：T3 → T6/T7 → T9/T10/T13 → T2 → T4（降级优先于升级）。

    ``today``：该作答事件发生的**本地学习日**（T4 "近 14 天无 Again" 的判定基准）。
    默认取当前本地日期；历史重放必须传入事件当天的日期，否则 14 天窗会用
    "今天" 去衡量历史事件，把本该升级的历史时刻判成不满足。
    """
    p = (params or ModelParams.load()).transitions
    grade = s.grade
    today = today or local_date()

    if s.status == UNLEARNED:
        # T1：首次学习（作答即学习完成；前置门禁由 P2-05 校验）
        if s.prereqs_ok and grade is not None:
            return TransitionResult(LEARNING, "T1", "首次学习，前置已满足")
        return _no_change(s.status)

    if s.status == LEARNING:
        # ① T3 降级：最近3次全Again 或（累计Again≥3 且 M_adj<0.35）
        t3 = p["T3"]
        if s.consecutive_again_count >= t3["consecutive_again"] or (
            s.consecutive_again_count >= t3["cum_again_min"] and s.m_adj < t3["m_adj_max"]
        ):
            return TransitionResult(STRUGGLING, "T3", "连续 Again，判定卡点")
        # ④ T2 升级：exposure≥2 且 有≥1次grade≥1 且 已生成首条计划
        t2 = p["T2"]
        if (
            s.exposure_count >= t2["exposure_min"]
            and s.total_answer_count >= 1
            and grade is not None
            and grade >= t2["grade_min"]
            and s.plan_exists
        ):
            return TransitionResult(REVIEWING, "T2", "进入复习循环")
        return _no_change(s.status)

    if s.status == REVIEWING:
        # ⑤ T4 升级：连续2次Good 且 M_adj≥0.65 且 S≥7 且 n_eff≥5 且 近14天无Again
        t4 = p["T4"]
        # 单条件即可：last_again_date 为空 → 无 Again 记录，视为满足
        # （原实现的 `or C and D` 第二分支与第一分支恒等价，属冗余，已清理）
        no_again = s.last_again_date is None or (
            days_between(_as_local_date(s.last_again_date), today) >= _NO_AGAIN_DAYS
        )
        if (
            grade is not None
            and grade >= 2
            and s.consecutive_good_count >= t4["consec_good"]
            and s.m_adj >= t4["m_adj"]
            and s.stability_days >= t4["s_days"]
            and s.n_eff >= t4["n_eff"]
            and no_again
        ):
            return TransitionResult(MASTERED, "T4", "达成掌握")
        return _no_change(s.status)

    if s.status == STRUGGLING:
        # ③ T10：完成1次reteach 且 reteach后首次grade≥1 → LEARNING
        if s.is_reteach and grade is not None and grade >= 1:
            return TransitionResult(LEARNING, "T10", "重教后首次做对，攻克卡点")
        return _no_change(s.status)

    if s.status == MASTERED:
        # ② T6：grade=0 且 ΔM_adj≥0.12 → REGRESSED
        t6 = p["T6"]
        if grade == t6["grade"] and (s.m_adj_before - s.m_adj) >= t6["delta_m_adj"]:
            return TransitionResult(REGRESSED, "T6", "掌握后遗忘回退")
        return _no_change(s.status)

    if s.status == CONSOLIDATED:
        # ② T7：同 T6
        t6 = p["T6"]
        if grade == t6["grade"] and (s.m_adj_before - s.m_adj) >= t6["delta_m_adj"]:
            return TransitionResult(REGRESSED, "T7", "巩固后遗忘回退")
        return _no_change(s.status)

    if s.status == REGRESSED:
        # ③ T9：完成1次任意Grade复习 → REVIEWING
        if grade is not None:
            return TransitionResult(REVIEWING, "T9", "回退后完成复习")
        return _no_change(s.status)

    if s.status == ARCHIVED:
        # ③ T13：年度抽检 Again → REVIEWING
        if grade == 0:
            return TransitionResult(REVIEWING, "T13", "年度抽检遗忘")
        return _no_change(s.status)

    return _no_change(s.status)


def evaluate_batch(
    s: KpSnapshot,
    today=None,
    params: ModelParams | None = None,
) -> TransitionResult:
    """每日 04:00 批处理——🔴 只跑 T5/T8/T11/T12（时间驱动类）。

    传入的快照必须来自作答事件之外的时间推进（R(t) 刷新、到期检测）。
    """
    p = (params or ModelParams.load()).transitions
    today = today or local_date()

    if s.status == MASTERED:
        # T5：保持30天 且 MASTERED 期间≥2次复习全grade≥2 且 M_adj≥0.72 且 S≥45 且 G≥0.35
        t5 = p["T5"]
        if s.entered_mastered_date is None:
            return _no_change(s.status)
        held = days_between(_as_local_date(s.entered_mastered_date), today)
        grades = s.mastered_review_grades or []
        if (
            held >= t5["mastered_hold_days"]
            and len(grades) >= t5["reviews_min"]
            and all(g >= 2 for g in grades)
            and s.m_adj >= t5["m_adj"]
            and s.stability_days >= t5["s_days"]
        ):
            return TransitionResult(CONSOLIDATED, "T5", "掌握保持期满，进入巩固")
        return _no_change(s.status)

    if s.status == REVIEWING:
        # T8：逾期>1.5×I 且 R(t)<0.5 → REGRESSED
        t8 = p["T8"]
        if s.overdue_ratio > t8["overdue_ratio"] and s.retrievability < t8["r_max"]:
            return TransitionResult(REGRESSED, "T8", "严重逾期且可提取性过低")
        return _no_change(s.status)

    if s.status == STRUGGLING:
        # T11（工程推断）：R(t)<0.35 → REGRESSED（卡点态的时间驱动出口）
        t11 = p["T11"]
        if s.retrievability < t11["r_max"]:
            return TransitionResult(REGRESSED, "T11", "卡点态可提取性过低")
        return _no_change(s.status)

    if s.status == CONSOLIDATED:
        # T12：S≥120 且 累计复习≥10 且 近2次grade≥2 且 距上次复习≥120天 → ARCHIVED
        t12 = p["T12"]
        last2 = (s.mastered_review_grades or [])[-2:]
        since = days_between(_as_local_date(s.last_review_date), today) if s.last_review_date else 0
        if (
            s.stability_days >= t12["s_days"]
            and s.total_answer_count >= t12["reviews_min"]
            and len(last2) == 2
            and all(g >= 2 for g in last2)
            and since >= t12["days_since_review"]
        ):
            return TransitionResult(ARCHIVED, "T12", "长期巩固，归档")
        return _no_change(s.status)

    return _no_change(s.status)


def freeze(s: KpSnapshot, reason: str = "superseded 或超纲") -> TransitionResult:
    """T14：UNLEARNED → 终止（图谱修订 superseded / 判定超纲，冻结不计入分母）。"""
    if s.status != UNLEARNED:
        return _no_change(s.status)
    return TransitionResult(UNLEARNED, "T14", f"冻结：{reason}（is_active=False）")


# ── kp_state_transition 日志（P1 表，随 P2-09 落地）──────────


def ensure_transition_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kp_state_transition (
            id                  INTEGER PRIMARY KEY,
            user_id             INTEGER NOT NULL,
            kp_id               INTEGER NOT NULL,
            from_status         TEXT    NOT NULL,
            to_status           TEXT    NOT NULL,
            rule                TEXT    NOT NULL,
            is_upgrade          INTEGER NOT NULL,
            triggered_by        TEXT    NOT NULL,
            conditions_snapshot TEXT    NOT NULL,
            created_at          TEXT    NOT NULL
        )
        """
    )
    conn.commit()


def log_transition(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    kp_id: int,
    result: TransitionResult,
    snapshot: KpSnapshot,
    triggered_by: str = "answer",
) -> None:
    """每次跃迁写日志（含 conditions_snapshot 全量快照，NFR-10 可审计）。"""
    ensure_transition_table(conn)
    conn.execute(
        "INSERT INTO kp_state_transition"
        " (user_id, kp_id, from_status, to_status, rule, is_upgrade, triggered_by,"
        "  conditions_snapshot, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            user_id,
            kp_id,
            snapshot.status,
            result.new_status,
            result.rule or "NONE",
            int(result.is_upgrade),
            triggered_by,
            json.dumps(
                {k: v for k, v in snapshot.__dict__.items()},
                ensure_ascii=False,
                default=str,
            ),
            __import__("fdl_core.srs.time_layer", fromlist=["fmt_ts"]).fmt_ts(now_utc()),
        ),
    )
    conn.commit()
