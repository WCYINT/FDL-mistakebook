"""P2-1 毕业归档（记忆层净化）。

走完 A3 六级阶梯的错题，若最近两次复习自评均达"掌握"且当前无逾期计划，
则自动"毕业"——退出活跃复习池（置 resolved_at + is_tamed），让队列始终聚焦
（对标遗忘曲线的自然衰减，避免已掌握的题长期占用每日 15 条复习额度）。

为什么是"降权"而非"硬删"：
- 可追溯：保留 mistake_record / review_feedback 历史，便于日后审计与回滚；
- 可回滚：只需把 resolved_at 清空、is_tamed 置 0 即可重新进入活跃池；
- display_filter.should_show_in_review_today 已按 resolved / tamed 过滤，
  所以置这两个字段后即自动从"今日复习"消失，**无需任何前端改动**。

毕业条件（三重，全部满足）：
1) mistake_record.reappear_count >= 6（走完 A3_STEP=[1,2,4,7,15,21] 共 6 档阶梯）；
2) 该错题**最近 2 次**复习的 review_feedback.self_rating **均 >= 3**；
3) 当前无逾期计划：不存在 status='PENDING' 且 due_date < today 的 review_schedule。
（隐含第 4 重：本来就未 resolved —— resolved_at IS NULL）

时区：一律用 time_layer.local_date() / now_utc()，禁止 date.today()（CI 可扩展扫描）。
"""

from __future__ import annotations

import sqlite3

from fdl_core.srs.time_layer import fmt_ts, local_date, now_utc

# 走完 A3 六级阶梯（A3_STEP 共 6 档）→ 累计复习次数达到门槛
GRADUATION_MIN_REAPPEAR = 6
# 最近 N 次复习自评均 >= 此值才算"真正掌握"（3=掌握 / 4=熟练）
GRADUATION_MIN_RECENT_GRADE = 3
GRADUATION_RECENT_N = 2


def check_graduation(
    conn: sqlite3.Connection,
    mistake_ids: list[int] | None = None,
    *,
    today: str | None = None,
    dry_run: bool = False,
) -> dict:
    """检测并（非 dry_run 时）执行毕业归档。

    参数：
        conn:         sqlite 连接
        mistake_ids:  None = 全库扫描（遍历全部错题；已 resolved 的会落入 already_resolved，
                      不参与后续检测）；列表 = 只查这些 id（显式指定的也可含已 resolved 的）
        today:        本地日期 YYYY-MM-DD（默认 local_date()）；用于逾期判定
        dry_run:      True 只返回计划、不改库（生产库验证用）

    返回：
        {
          "checked": int,            # 实际扫描的错题条数（含已 resolved）
          "graduated": [id, ...],    # 满足全部条件、本次毕业的错题 id
          "skipped_reasons": {       # 未毕业原因计数（checked = 各原因之和 + len(graduated)）
              "no_history": int,        # reappear 不足 / 复习反馈历史不足 N 条
              "insufficient_grade": int,# 最近 N 次评分有 < 门槛
              "has_overdue": int,       # 存在逾期 PENDING 计划
              "already_resolved": int,  # 本来就已 resolved（不重复处理）
          },
          "dry_run": bool,
        }

    设计要点：单条异常不能中断整体（try/except 跳过并计入 no_history），保证批量扫描稳健。
    """
    # today 用本地日期（Asia/Shanghai 学习日口径），不用 date.today()
    today = today or local_date().isoformat()
    # resolved_at / updated_at 存 UTC 时间戳，其日期即本地 today（统一用 now_utc()，避免本地时钟偏差）
    stamp = fmt_ts(now_utc())

    # 1) 选出要扫描的 id 列表
    if mistake_ids is None:
        # 全库扫描：取全部 id（含已 resolved），已 resolved 在循环内计入 already_resolved
        ids = [r[0] for r in conn.execute("SELECT id FROM mistake_record ORDER BY id")]
    else:
        ids = list(mistake_ids)

    checked = 0
    graduated: list[int] = []
    skipped = {
        "no_history": 0,
        "insufficient_grade": 0,
        "has_overdue": 0,
        "already_resolved": 0,
    }

    for mid in ids:
        checked += 1
        try:
            row = conn.execute(
                "SELECT resolved_at, reappear_count FROM mistake_record WHERE id=?",
                (mid,),
            ).fetchone()
            if row is None:
                # id 不存在：视作无历史，计入 no_history 但不崩（checked 已计数）
                skipped["no_history"] += 1
                continue
            resolved_at, reappear_count = row

            # (4) 本来就 resolved：不重复处理（避免覆盖原有 resolved_at）
            if resolved_at is not None:
                skipped["already_resolved"] += 1
                continue

            # (1) 走完 A3 六级阶梯：reappear_count >= 6
            if (reappear_count or 0) < GRADUATION_MIN_REAPPEAR:
                skipped["no_history"] += 1
                continue

            # (2) 最近 N 次复习自评均 >= 门槛
            # review_feedback 本身没有 mistake_id，需 JOIN review_schedule.mistake_id 归属；
            # 取法：created_at DESC（同秒按 id DESC）取最近 N 条；不足 N 条视为未达标
            fb = conn.execute(
                "SELECT rf.self_rating FROM review_feedback rf "
                "JOIN review_schedule rs ON rs.id = rf.schedule_id "
                "WHERE rs.mistake_id = ? "
                "ORDER BY rf.created_at DESC, rf.id DESC LIMIT ?",
                (mid, GRADUATION_RECENT_N),
            ).fetchall()
            ratings = [r[0] for r in fb]
            if len(ratings) < GRADUATION_RECENT_N:
                # 复习历史不足（没复习够 N 次）→ 未达标
                skipped["no_history"] += 1
                continue
            if any(g < GRADUATION_MIN_RECENT_GRADE for g in ratings):
                skipped["insufficient_grade"] += 1
                continue

            # (3) 当前无逾期计划：不存在 PENDING 且 due_date < today
            overdue = conn.execute(
                "SELECT 1 FROM review_schedule "
                "WHERE mistake_id = ? AND status = 'PENDING' AND due_date < ? "
                "LIMIT 1",
                (mid, today),
            ).fetchone()
            if overdue is not None:
                skipped["has_overdue"] += 1
                continue

            # 全部满足 → 毕业候选
            graduated.append(mid)
            if not dry_run:
                # 降权而非硬删：置 resolved_at + is_tamed=1，display_filter 自动隐藏
                conn.execute(
                    "UPDATE mistake_record SET resolved_at = ?, is_tamed = 1, "
                    "updated_at = ? WHERE id = ?",
                    (stamp, stamp, mid),
                )
        except Exception:
            # 单条异常不中断整体（例如脏数据 / 字段缺失）；计入 no_history 以保证
            # checked == 各 skipped 原因之和 + len(graduated)，便于排查"为什么没毕业"
            skipped["no_history"] += 1
            continue

    if not dry_run and graduated:
        conn.commit()

    return {
        "checked": checked,
        "graduated": graduated,
        "skipped_reasons": skipped,
        "dry_run": dry_run,
    }
