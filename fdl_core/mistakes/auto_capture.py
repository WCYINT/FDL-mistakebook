"""ING-04 系统内练习自动捕获错题（零录入）。

挂钩点：每次作答入库后调用 `capture_from_answer`——
`grade=0`（答错）且 `is_valid_evidence=1` → **自动**生成 `mistake_record`。
这是错题的主来源（占比目标 ≥70%）；来源标记 `source='SYSTEM_AUTO'`，
error_type 留空（待 MS-02 行为链 / MS-03 点选补全），severity 取 PRD 缺省 3。

🔴 幂等：同一作答只捕获一次（按 user_id+kp_id+occurred_at 唯一约束 + 幂等查询）。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

# 录题即建首条复习计划：闭合 SRS 闭环，让新错题当日进入复习队列
from fdl_core.mistakes.review import create_initial_review_schedule
from fdl_core.mistakes.tables import ensure_mistake_table
from fdl_core.srs.time_layer import fmt_ts, now_utc


@dataclass
class CapturedMistake:
    mistake_id: int
    kp_id: int
    answer_log_id: int
    wrong_answer: str | None
    correct_answer: str | None
    schedule_created: int = 0  # 本次捕获顺带建出的首条复习计划数（0=建计划失败，不影响捕获）


def capture_from_answer(
    conn: sqlite3.Connection,
    answer_log_id: int,
    *,
    user_id: int = 1,
) -> CapturedMistake | None:
    """作答入库后调用：答错 → 自动生成 mistake_record（零录入动作）。

    返回 None 的情形：作答不存在 / 答对 / 非有效证据（校准/探索/刷量）/
    已捕获过（幂等）。
    """
    ensure_mistake_table(conn)
    row = conn.execute(
        "SELECT user_id, kp_id, task_type, grade, is_correct, is_valid_evidence,"
        " user_answer, answered_at, question_id, input_mode"
        " FROM answer_log WHERE id=?",
        (answer_log_id,),
    ).fetchone()
    if row is None:
        return None
    (uid, kp_id, task_type, grade, is_correct, valid, user_answer, answered_at, qid, mode) = row
    if not (grade == 0 and valid):  # 只捕获答错且有效的作答
        return None

    correct = None
    if qid:
        q = conn.execute("SELECT answer FROM question WHERE id=?", (qid,)).fetchone()
        correct = q[0] if q else None

    dup = conn.execute(
        "SELECT id FROM mistake_record WHERE user_id=? AND kp_id=? AND occurred_at=?",
        (uid, kp_id, answered_at),
    ).fetchone()
    if dup:
        return None  # 幂等：同一作答不重复捕获

    cur = conn.execute(
        "INSERT INTO mistake_record (user_id, kp_id, question_id, occurred_at, subject,"
        " source, source_ref, attributed_by, attribution_confidence, severity,"
        " needs_reteach, input_mode, wrong_answer, correct_answer, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            uid,
            kp_id,
            qid,
            answered_at,
            "MATH",
            "SYSTEM_AUTO",  # ING-04 零录入主来源
            f"answer_log#{answer_log_id}",
            "RULE_BASED",
            0.0,
            3,
            0,
            mode or "KEYBOARD",
            user_answer,
            correct,
            fmt_ts(now_utc()),
            fmt_ts(now_utc()),
        ),
    )
    new_id = int(cur.lastrowid or 0)
    conn.commit()

    # 闭环补全：录题成功后即建首条复习计划（次日档），让新错题当日进入 SRS 队列。
    # 建计划是增强项——捕获（写 mistake_record）才是主流程职责，故用 try/except 包住：
    # 即便建计划失败，mistake_record 已提交、捕获结果不受影响，仅记日志留痕。
    schedule_created = 0
    try:
        schedule_created = create_initial_review_schedule(conn, [new_id], interval_days=1)
    except Exception as exc:  # 建计划失败不能让作答捕获失败
        logging.getLogger("fdl.auto_capture").warning("建首条复习计划失败 mid=%s: %s", new_id, exc)

    return CapturedMistake(
        mistake_id=new_id,
        kp_id=kp_id,
        answer_log_id=answer_log_id,
        wrong_answer=user_answer,
        correct_answer=correct,
        schedule_created=schedule_created,
    )
