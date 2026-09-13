"""
fdl_core/srs/display_filter.py — 复习列表显示控制（VIS-13 扩展）

🟢 业务规则：
- 【已解决】(resolved_at 非空) → 永远隐藏（无论 SRS 状态如何）
- 【今日已复习】→ 仅在 due_date==today 时保留显示（复习节点）
- 【已复习】→ 仅在 due_date<=today 时保留（逾期补做）
- 【待复习】→ 显示（今天该复习）

🟢 显示判定的实现：
- 查 review_schedule 表同 kp_id 的 PENDING 计划（due_date 与 today 比较）
- due_date == today：今日复习节点（**不论是否已点开看过**——重复复习算"复习未完成"）
- due_date > today：未来复习节点 → 隐藏
- due_date < today：逾期未复习 → **显示**（避免漏掉）
- 无 PENDING：可能无 SRS 计划 → 默认隐藏（保守）

🟢 时区口径：与 classify_status 一致，UTC 字符串 → 本地日期再比 today。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from fdl_core.mistakes.review import DAILY_REVIEW_CAP


@dataclass
class DisplayDecision:
    """单条错题的"今日复习列表"显示决定。"""

    should_show: bool  # True = 进入今日复习卡
    reason: str  # 调试用：为何隐藏/显示
    next_due_date: str | None  # 下一复习节点（用于 UI tooltip）
    is_overdue: bool  # 逾期未复习
    is_in_curve_today: bool  # 今天是否记忆曲线节点


def should_show_in_review_today(
    conn: sqlite3.Connection,
    *,
    kp_id: int,
    mistake_id: int | None = None,
    today: str,
    resolved_at: str | None = None,
    last_reappear_at: str | None = None,
    status: str | None = None,
    is_tamed: int | None = None,
) -> DisplayDecision:
    """判读单条错题今日复习列表显示与否。

    参数：
      kp_id:        错题对应 KP（必填，用于查 review_schedule）
      mistake_id:    可选 debug 标签
      today:        本地日期字符串（YYYY-MM-DD），与 classify_status 同口径
      resolved_at:  mistake_record.resolved_at（UTC 字符串）
      last_reappear_at: mistake_record.last_reappear_at（UTC 字符串）
      status:       classify_status 输出（"已解决"/"今日已复习"/"已复习"/"待复习"）

    返回 DisplayDecision；should_show=True 时进入复习卡。
    """
    # 规则 0（P20）：【已驯服】永远隐藏
    # is_tamed=1 = 同类错误连续 3 周下降，已"驯服" → 不应再在今日复习列表打扰。
    # 放在【已解决】之后、任何其他逻辑之前（与 resolved 同属"永久隐藏"语义）。
    if is_tamed == 1:
        return DisplayDecision(False, "tamed", None, False, False)

    # 规则 1：【已解决】永远隐藏
    if status == "已解决" or resolved_at:
        return DisplayDecision(False, "resolved", None, False, False)

    # 查 review_schedule 同错题的 PENDING 计划（最近一条）
    # A3（2026-09-08）：调度单元改 mistake_id，优先按它定位；
    # 未传 mistake_id 时回退 kp_id（兼容旧数据与知识点已挂载的场景）
    if mistake_id is not None:
        row = conn.execute(
            "SELECT due_date, planned_interval_days, status FROM review_schedule"
            " WHERE mistake_id=? AND status='PENDING'"
            " ORDER BY due_date ASC LIMIT 1",
            (mistake_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT due_date, planned_interval_days, status FROM review_schedule"
            " WHERE kp_id=? AND status='PENDING'"
            " ORDER BY due_date ASC LIMIT 1",
            (kp_id,),
        ).fetchone()

    if row is None:
        # 无 PENDING 计划 + 状态为【已复习】：下次复习在更远期，不应出现在今日列表
        if status == "已复习":
            return DisplayDecision(False, "no_pending_already_reviewed", None, False, False)
        # 无 PENDING + 待复习（兜底罕见情形）→ 显示（保守）
        if status == "待复习":
            return DisplayDecision(True, "pending_no_schedule", None, False, False)
        # 今日已复习但 SRS 计划缺失（异常态）→ 仍可显示供 Frank 二次确认
        if status == "今日已复习":
            return DisplayDecision(True, "reviewed_today_no_schedule", None, False, True)
        return DisplayDecision(False, "unknown_status", None, False, False)

    due_date, interval, _ = row

    # 规则 2：记忆曲线节点判定
    #   - due_date == today   → 今天该复习（即使已点开过——复习未完成）
    #   - due_date <  today   → 逾期未复习，必须显示
    #   - due_date >  today   → 未到期，今日隐藏
    is_in_curve_today = due_date <= today
    is_overdue = due_date < today

    if not is_in_curve_today:
        # 【已复习/今日已复习】+ due_date > today → 隐藏（避免在不该复习的日期打扰）
        return DisplayDecision(False, f"not_due({due_date}>{today})", due_date, False, False)

    # 规则 3：【今日已复习】特殊场景
    # 当 due_date == today 且 last_reappear_at 转本地 == today → 已完成今日节点
    # 但**仍显示**（按 King 规则"复习未完成时保留"——这里 Frank 可能想二刷）
    # 规则 3：今日已复习 + due_date==today → 保留（复习节点当日）
    if status == "今日已复习":
        reason = f"reviewed_today_due_today({due_date})"
        return DisplayDecision(True, reason, due_date, is_overdue, True)

    # 规则 4：待复习 → 显示
    if status == "待复习":
        reason = f"pending_due_today({due_date})"
        return DisplayDecision(True, reason, due_date, is_overdue, True)

    # 规则 5：【已复习】且 due_date <= today（逾期或到期）→ 显示（兜底）
    if status == "已复习":
        reason = f"reviewed_overdue({due_date})"
        return DisplayDecision(True, reason, due_date, is_overdue, True)

    return DisplayDecision(False, "fallthrough", due_date, is_overdue, is_in_curve_today)


def _filter_review_today_core(
    conn: sqlite3.Connection,
    *,
    review_items: list[dict],
    today: str,
    cap: int | None,
) -> tuple[list[dict], int]:
    """核心过滤 + 排序 + 截断。

    排序优先级（与业务承诺「每日上限 15」一致）：
      1. 当日到期（due_date == today）优先；
      2. 逾期（due_date < today）按到期日越早越优先（先补最旧的）。
    截断到 `cap`（默认 DAILY_REVIEW_CAP=15）；被截断的逾期项不丢弃，
    返回 overflow_count 供报告折叠展示「另有 N 条逾期补做」。

    返回 (kept_list, overflow_count)。
    """
    cap = cap or DAILY_REVIEW_CAP
    decided: list[tuple[dict, DisplayDecision]] = []
    for it in review_items:
        # A3：调度单元为 mistake_id（错题卡 ID）；kp_id 仅作兼容回退
        kp_id = it.get("kp_id") or it.get("id")
        mistake_id = it.get("mistake_id") or it.get("id")
        d = should_show_in_review_today(
            conn,
            kp_id=kp_id,
            mistake_id=mistake_id,
            today=today,
            resolved_at=it.get("resolved_at"),
            last_reappear_at=it.get("last_reappear_at"),
            status=it.get("status"),
            is_tamed=it.get("is_tamed"),
        )
        if d.should_show:
            it["display"] = {
                "reason": d.reason,
                "is_overdue": d.is_overdue,
                "is_in_curve_today": d.is_in_curve_today,
                "next_due_date": d.next_due_date,
            }
            decided.append((it, d))

    # 排序：当日到期优先（is_overdue=False），逾期按 due_date 升序（先补最旧）
    def _sort_key(pair: tuple[dict, DisplayDecision]):
        _it, _d = pair
        due = _d.next_due_date or today  # 无 PENDING 计划（兜底场景）视作当日
        return (_d.is_overdue, due)

    decided.sort(key=_sort_key)

    kept = [it for it, _ in decided]
    overflow = max(0, len(kept) - cap)
    if overflow:
        kept = kept[:cap]
    return kept, overflow


def filter_review_today_items(
    conn: sqlite3.Connection,
    *,
    review_items: list[dict],
    today: str,
    cap: int | None = None,
) -> list[dict]:
    """批量过滤：基于 mistake_id 列表，应用 should_show_in_review_today 过滤。

    review_items 每项需含 {id, kp_id, status, resolved_at, last_reappear_at}

    受 DAILY_REVIEW_CAP（默认 15）约束：超过上限的（主要是逾期）项被截断，
    返回列表长度 ≤ cap。被截断项的统计见 filter_review_today_capped()。
    """
    kept, _ = _filter_review_today_core(conn, review_items=review_items, today=today, cap=cap)
    return kept


def filter_review_today_capped(
    conn: sqlite3.Connection,
    *,
    review_items: list[dict],
    today: str,
    cap: int | None = None,
) -> dict:
    """带统计的批量过滤：返回 dict 便于报告折叠展示被截断的逾期项。

    返回：
      {
        "kept": [...],            # 截断后的今日复习卡（≤ cap）
        "overflow_count": int,    # 被截断的项数（主要为逾期补做）
        "cap": int,               # 实际生效的上限
        "total_shown": int,       # kept 实际长度
      }
    """
    kept, overflow = _filter_review_today_core(
        conn, review_items=review_items, today=today, cap=cap
    )
    return {
        "kept": kept,
        "overflow_count": overflow,
        "cap": cap or DAILY_REVIEW_CAP,
        "total_shown": len(kept),
    }


def recent_review_feedback(
    conn: sqlite3.Connection,
    *,
    mistake_id: int | None = None,
    kp_id: int | None = None,
    limit: int = 5,
) -> list[dict]:
    """读取最近的复习反馈（OpenMAIC 借鉴 B：反馈闭环联动展示）。

    用于复习页/驾驶舱展示"最近反馈"，可按 mistake_id / kp_id 过滤。
    返回按 created_at 倒序的最近 limit 条 {id, schedule_id, kp_id, self_rating, note, created_at}。
    """
    sql = (
        "SELECT id, schedule_id, kp_id, self_rating, note, created_at "
        "FROM review_feedback WHERE 1=1"
    )
    params: list = []
    if mistake_id is not None:
        sql += " AND schedule_id IN (SELECT id FROM review_schedule WHERE mistake_id=?)"
        params.append(mistake_id)
    if kp_id is not None:
        sql += " AND kp_id=?"
        params.append(kp_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    cols = ("id", "schedule_id", "kp_id", "self_rating", "note", "created_at")
    return [dict(zip(cols, r, strict=False)) for r in rows]
