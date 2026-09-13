"""每日任务生成（P2-11 / SRS-05）。

D-04 四条规定（缺一不可）：
① 内部按日均 11.5 道做容量规划（review_per_day=15 软 / 20 硬）；
② UI 只显示当前 session 量——本模块产出的就是 session 级任务列表；
③ **MVP 前 8 周单次呈现上限 8 道**，超出进"可选加练"（非强制）；
④ 积压增长触发降载（relief：暂停新学 + 复习上限 10 + R 升序优先），绝不加量。

排序：复习按 R(t) **升序**（最先忘的先做）；新学按 importance_weight 降序。
抖动：计划间隔 ±5%（写 review_schedule 时应用，本模块消费 due 队列）。
周日 = 自由探究日：不跑复习队列，只产 1 个开放式探究任务（PRD §6.6）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from fdl_core.srs.params import ModelParams
from fdl_core.srs.time_layer import local_date

AM, PM = "AM", "PM"


class DailyTaskError(ValueError):
    """每日任务生成错误。"""


@dataclass
class SessionPlan:
    """单 session 任务计划（晨/晚）。"""

    slot: str
    tasks: list[dict]
    total_est_seconds: int
    overflow_review_ids: list[int]  # 超出呈现上限的复习（可选加练，非强制）


def _due_reviews(conn, user_id: int, d, limit: int) -> list[dict]:
    """到期复习池：due_date ≤ d 且 PENDING，按 priority_score 降序。"""
    rows = conn.execute(
        "SELECT rs.id, rs.kp_id, rs.subject_id, rs.due_date, rs.priority_score,"
        " rs.est_seconds, rs.suggested_question_id, ks.retrievability"
        " FROM review_schedule rs LEFT JOIN kp_state ks"
        " ON ks.user_id = rs.user_id AND ks.kp_id = rs.kp_id"
        " WHERE rs.user_id=? AND rs.status='PENDING' AND rs.due_date <= ?"
        " ORDER BY COALESCE(ks.retrievability, 0.0) ASC, rs.priority_score DESC"
        " LIMIT ?",
        (user_id, d.isoformat(), limit),
    ).fetchall()
    return [
        {
            "schedule_id": r[0],
            "kp_id": r[1],
            "subject_id": r[2],
            "due_date": r[3],
            "priority_score": r[4],
            "est_seconds": r[5],
            "question_id": r[6],
            "retrievability": r[7],
        }
        for r in rows
    ]


def _new_learn_candidates(conn, user_id: int, limit: int) -> list[dict]:
    """新学候选：UNLEARNED 且 is_active，importance_weight 降序。"""
    rows = conn.execute(
        "SELECT k.id, k.subject_id, k.importance_weight, k.est_learn_minutes"
        " FROM knowledge_point k LEFT JOIN kp_state s"
        " ON s.user_id=? AND s.kp_id=k.id"
        " WHERE COALESCE(s.status,'UNLEARNED')='UNLEARNED'"
        " AND COALESCE(s.is_active, 1)=1"
        " ORDER BY k.importance_weight DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [{"kp_id": r[0], "subject_id": r[1], "importance_weight": r[2]} for r in rows]


def _relief_mode(conn, user_id: int, params: ModelParams) -> bool:
    """降载判定：逾期积压 > review_debt_cap → 暂停新学 + 复习上限 10。"""
    cap = params.scheduling["review_debt_cap"]
    row = conn.execute(
        "SELECT COUNT(*) FROM review_schedule WHERE user_id=? AND status='PENDING'"
        " AND due_date < ?",
        (user_id, local_date().isoformat()),
    ).fetchone()
    return row[0] > cap


def generate_daily_tasks(
    conn: sqlite3.Connection,
    user_id: int,
    target_date=None,
    params: ModelParams | None = None,
    on_vacation: bool = False,
) -> list[SessionPlan]:
    """生成晨/晚两个 session 的任务计划（不写库，由调用方落 daily_task）。"""
    params = params or ModelParams.load()
    d = target_date or local_date()
    sched = params.scheduling

    # 周日探究日：不跑复习队列
    if d.weekday() == 6:  # Sunday
        return [
            SessionPlan(
                slot=PM,
                tasks=[
                    {
                        "task_type": "EXPLORE",
                        "title": "自由探究日：选一个你的为什么",
                        "est_seconds": sched["evening_minutes"] * 60,
                    }
                ],
                total_est_seconds=sched["evening_minutes"] * 60,
                overflow_review_ids=[],
            )
        ]

    relief = _relief_mode(conn, user_id, params)
    review_cap = sched["review_per_day"]
    if relief:
        review_cap = min(review_cap, sched["relief_mode"]["review_cap"])
    if on_vacation:
        review_cap = min(review_cap, sched["vacation_mode"]["review_cap"])

    pool = _due_reviews(conn, user_id, d, review_cap * 2)  # 取 2 倍池用于跨 session 分配
    new_candidates = (
        [] if relief or on_vacation else _new_learn_candidates(conn, user_id, sched["new_per_day"])
    )

    # ── 晨 session：仅复习，≤ morning_minutes，呈现上限 8 ──
    am_cap = min(sched["presentation_cap"], sched["presentation_cap"] if relief else review_cap)
    am_budget = sched["morning_minutes"] * 60
    am_tasks: list[dict] = []
    am_used = 0
    am_overflow: list[int] = []
    for r in pool:
        if len(am_tasks) >= am_cap or am_used + r["est_seconds"] > am_budget:
            am_overflow.append(r["schedule_id"])
            continue
        am_tasks.append(
            {
                "task_type": "REVIEW",
                "kp_id": r["kp_id"],
                "subject_id": r["subject_id"],
                "review_schedule_id": r["schedule_id"],
                "question_id": r["question_id"],
                "est_seconds": r["est_seconds"],
                "title": "复习一道快忘掉的题",
            }
        )
        am_used += r["est_seconds"]
        pool = [x for x in pool if x["schedule_id"] != r["schedule_id"]]

    # ── 晚 session：新学（≤kp_max_per_day）+ 剩余复习 + 深度 ──
    pm_budget = sched["evening_minutes"] * 60
    pm_tasks: list[dict] = []
    pm_used = 0
    for kp in new_candidates[: sched["kp_max_per_day"]]:
        est = int(kp["importance_weight"] * 90)  # 新学单点预估（约 1.5min）
        if pm_used + est > pm_budget or len(pm_tasks) >= sched["presentation_cap"]:
            break
        pm_tasks.append(
            {
                "task_type": "NEW_LEARN",
                "kp_id": kp["kp_id"],
                "subject_id": kp["subject_id"],
                "est_seconds": est,
                "title": "新知识：今天学一个新本领",
            }
        )
        pm_used += est
    for r in pool:
        if len(pm_tasks) >= sched["presentation_cap"] or pm_used + r["est_seconds"] > pm_budget:
            am_overflow.append(r["schedule_id"])  # 顺延/加练
            continue
        pm_tasks.append(
            {
                "task_type": "REVIEW",
                "kp_id": r["kp_id"],
                "subject_id": r["subject_id"],
                "review_schedule_id": r["schedule_id"],
                "question_id": r["question_id"],
                "est_seconds": r["est_seconds"],
                "title": "睡前复习：把今天的东西钉牢",
            }
        )
        pm_used += r["est_seconds"]

    plans = [
        SessionPlan(AM, am_tasks, am_used, []),
        SessionPlan(PM, pm_tasks, pm_used, am_overflow),
    ]
    return [p for p in plans if p.tasks]
