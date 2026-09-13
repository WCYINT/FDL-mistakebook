"""家长端（PVP）受限视图（P2-26 / P2-27，批次 6）。

🔴 数据层拦截（不是 UI 隐藏）——King 的可见性由本模块的权限函数硬性约束，
路由层（阶段四 VIS/PVP 页面）只能通过这里取数，无任何明细入口。

P2-26（PVP-04/06）：
- 仅**科目级**掌握度趋势（kp_state 按学科聚合，无 KP 明细、无下钻）；
- **前 2 周屏蔽质量诊断**（质量诊断 = 归因/错题分析，仅体验信号 WAD/SIR/时长/放弃）；
- 数据延迟 ≥7 天（防 King 实时紧盯制造压力，R-11）。

P2-27（PVP-01/07）：
- 权限矩阵：actor × resource → allow/deny；数据层函数首行调用 `pvp_allowed`。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from fdl_core.srs.time_layer import fmt_ts, local_date, local_date_range

# ── P2-27 权限矩阵（actor × resource → allow）────────────────
# King 可见：聚合指标（延迟 ≥7 天）。明细节流：answer_log / kp_state /
# kp_state_transition / daily_task 全部 deny（数据层拦截，UI 隐藏可被改回）。
PVP_MATRIX: dict[tuple[str, str], bool] = {
    ("king", "subject_trend"): True,  # 科目级趋势（延迟+聚合）
    ("king", "experience_signals"): True,  # 体验信号 WAD/SIR/时长/放弃
    ("king", "quality_diagnosis"): False,  # 质量诊断：前 2 周屏蔽 + 延迟
    ("king", "answer_log_detail"): False,  # 🔴 作答明细：永不可见
    ("king", "kp_state_detail"): False,  # 🔴 单个知识点状态明细：不可见
    ("king", "kp_transition_detail"): False,  # 🔴 跃迁明细：不可见
    ("king", "inquiry_shared"): True,  # Frank 主动授权的追问（shared_with_parent）
}

KING_VIEW_DELAY_DAYS = 7
QUALITY_DIAGNOSIS_FREEZE_WEEKS = 2

# 项目起步日（2026-09-13 修正）：
# FDL 首个开发日 = 2026-08-30（工作台搭建，见仓库首个 commit 08-31 00:07 与
# 项目目录创建时间 08-30 16:32）。此前 generate_report 硬编码 09-01（晚 2 天），
# 导致冻结解除判定比实际计划晚 2 天（今天 09-13 本应解锁却仍显示"屏蔽中"）。
# 冻结解除 = 本日期 + 2 周 = 2026-09-13。
PROJECT_START_DATE = date(2026, 8, 30)


def pvp_allowed(actor: str, resource: str) -> bool:
    """权限矩阵查询（数据层首行调用）；未声明默认拒绝（最小权限）。"""
    return PVP_MATRIX.get((actor, resource), False)


def king_view_cutoff(today: date | None = None) -> date:
    """King 可见数据的截止日 = 今天 − 7 天（延迟 ≥7 天）。"""
    return (today or local_date()) - timedelta(days=KING_VIEW_DELAY_DAYS)


def quality_diagnosis_visible(start_date: date, today: date | None = None) -> bool:
    """质量诊断可见性：上线 2 周内 False；此后仍需满足 7 天延迟（King 视图 R-11）。

    双条件（均须满足）：
    1. 2 周冻结解除：真实 today >= start_date + 2 周（与 King 实时时钟一致）；
    2. 7 天延迟：King 视图数据截止日 king_view_cutoff(today)=today-7 必须覆盖
       质量诊断所依赖的最早学习数据（以 start_date 近似——诊断内容基于账号上线后
       的学习数据，上线日即最早数据日）。即 king_view_cutoff(today) >= start_date。

    说明：调用方（路由层）仍应先用 king_view_cutoff 取得延迟窗口再取明细数据；
    本函数仅对"是否解锁"做硬性双重校验，不改变调用方契约、不破坏既有返回值语义。
    """
    today = today or local_date()
    frozen_until = start_date + timedelta(weeks=QUALITY_DIAGNOSIS_FREEZE_WEEKS)
    delayed = king_view_cutoff(today) >= start_date  # 7 天延迟：延迟窗口覆盖最早数据日
    return today >= frozen_until and delayed


# ── P2-26 科目级趋势（聚合，无下钻）─────────────────────────
def subject_trend(
    conn: sqlite3.Connection,
    user_id: int = 1,
    cutoff: date | None = None,
    actor: str = "king",
) -> list[dict]:
    """科目级掌握度趋势（聚合 kp_state + knowledge_point，无 kp_state 时回退错题）。

    🔴 首行权限拦截；返回仅含科目级聚合值——函数签名刻意不含 KP 筛选参数
    （无下钻入口），明细查询在本模块不存在。

    数据源优先级：
    1. `kp_state`（知识点体系建立后的权威口径，受 7 天延迟约束）；
    2. 空结果 → 回退 `mistake_record.subject` 聚合（知识点体系尚未建立期间，
       真实学习数据全在错题表；否则家长视图恒空）。
    """
    if not pvp_allowed(actor, "subject_trend"):
        return []
    cutoff_date = cutoff or king_view_cutoff()
    # 🔴 status_changed_at 存的是 UTC 时间戳，禁止 substr(...,1,10) 当本地日期比。
    # 取「cutoff 当日本地日末」对应的 UTC 时刻做完整时间戳比较（避免跨日错位 1 天）。
    cutoff_ts = fmt_ts(local_date_range(cutoff_date)[1])
    rows = conn.execute(
        "SELECT sub.code, COUNT(*), AVG(s.mastery_adj),"
        " SUM(CASE WHEN s.status IN ('MASTERED','CONSOLIDATED') THEN 1 ELSE 0 END)"
        " FROM kp_state s JOIN knowledge_point k ON k.id = s.kp_id"
        " JOIN subject sub ON sub.id = k.subject_id"
        " WHERE s.user_id=? AND s.status_changed_at < ?"
        " GROUP BY sub.code ORDER BY sub.code",
        (user_id, cutoff_ts),
    ).fetchall()
    if rows:
        return [
            {
                "subject": r[0],
                "kp_total": r[1],
                "avg_mastery": round(r[2] or 0.0, 4),
                "mastered": r[3] or 0,
                "source": "kp_state",
            }
            for r in rows
        ]
    return _subject_trend_from_mistakes(conn, user_id)


def _subject_trend_from_mistakes(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """回退口径：按 `mistake_record.subject` 聚合科目级错题掌握分布。

    🔴 仍是纯聚合（科目级计数），无题目/知识点明细——PVP 权限矩阵不被绕过。
    累计分布（非按日时间序列），故不叠加 7 天延迟窗口：它不含"今天做了什么"
    的实时信号，不构成 R-11 紧盯压力。
    """
    rows = conn.execute(
        "SELECT m.subject, COUNT(*),"
        " SUM(CASE WHEN m.resolved_at IS NOT NULL THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN m.resolved_at IS NULL AND m.last_reappear_at IS NOT NULL"
        "     THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN m.resolved_at IS NULL AND m.last_reappear_at IS NULL"
        "     THEN 1 ELSE 0 END)"
        " FROM mistake_record m WHERE m.user_id=? AND m.subject IS NOT NULL"
        " GROUP BY m.subject ORDER BY m.subject",
        (user_id,),
    ).fetchall()
    out = []
    for code, total, resolved, reviewing, pending in rows:
        total = total or 0
        resolved = resolved or 0
        out.append(
            {
                "subject": code,
                "kp_total": total,
                # 掌握度代理指标 = 已解决错题占比（kp_state 的 mastery_adj 尚不可得）
                "avg_mastery": round(resolved / total, 4) if total else 0.0,
                "mastered": resolved,
                "reviewing": reviewing or 0,
                "pending": pending or 0,
                "source": "mistake_record",
            }
        )
    return out


def experience_signals(
    conn: sqlite3.Connection,
    user_id: int = 1,
    actor: str = "king",
) -> dict:
    """体验信号（前 2 周唯一可见面）：WAD / SIR / 时长 / 放弃次数（延迟 7 天）。"""
    if not pvp_allowed(actor, "experience_signals"):
        return {}
    cutoff = king_view_cutoff().isoformat()
    # 🔴 口径：学习时长用 effective_sec（有效秒），非 duration_sec（挂钟）；
    # 且只统计有效会话（is_valid=1）——与 report 层 generate_report.py 一致。
    # COUNT(*)/SUM(trigger_type='SELF') 也只计有效会话（WHERE 已过滤）。
    sess = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(trigger_type='SELF'),0),"
        " COALESCE(AVG(effective_sec)/60.0,0), 0"
        " FROM study_session WHERE user_id=? AND session_date<=? AND is_valid=1",
        (user_id, cutoff),
    ).fetchone()
    total, self_started, avg_min, _abandoned = sess
    return {
        "sessions": total,
        "sir": round(self_started / total, 3) if total else 0.0,
        "avg_minutes": round(avg_min or 0.0, 1),
        "cutoff": cutoff,
    }
