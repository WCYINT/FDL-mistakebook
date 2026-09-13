"""周指标与 NMKP（P2-14 / MT-03）。

NMKP = 周 T4/T5 跃迁数 − T6/T7 回退数（北极星指标，稳态目标 5/周，健康 3–9）。
`nmkp_mde_flag`：UP/DOWN/FLAT 噪声过滤（周环比 |Δ| < MDE=2 → FLAT）。
🔴 **静默期对 King 隐藏**（R-11 信任风险）：上线后 8–17 周 NMKP 几乎必然为 0，
静默期指标不呈现；解除信号（先到者）：① 出现第 1 个 MASTERED；
② 累计 n_eff≥5 的知识点数 ≥10 个。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from fdl_core.metrics.tables import ensure_metric_tables
from fdl_core.srs.state_machine import ensure_transition_table
from fdl_core.srs.time_layer import fmt_ts, local_date, local_date_range, now_utc

MDE = 2  # 最小可检测效应（周 NMKP 变化 < 2 视为噪声）
SILENT_MIN_MASTERED = 1  # 解除信号①：第 1 个 MASTERED
SILENT_MIN_SOLIDS = 10  # 解除信号②：n_eff≥5 的 KP 数


def week_start_of(d: date) -> date:
    """周一为一周起点（PRD 排期口径 W1=周二 09-01 起，指标周按自然周一对齐）。"""
    return d - timedelta(days=d.weekday())


def silent_period(conn: sqlite3.Connection, user_id: int = 1) -> bool:
    """静默期判定：无 MASTERED 且 n_eff≥5 的 KP < 10 → 静默。"""
    mastered = conn.execute(
        "SELECT COUNT(*) FROM kp_state WHERE user_id=? AND status IN ('MASTERED','CONSOLIDATED')",
        (user_id,),
    ).fetchone()[0]
    solids = conn.execute(
        "SELECT COUNT(*) FROM kp_state WHERE user_id=? AND effective_answer_count >= 5",
        (user_id,),
    ).fetchone()[0]
    return mastered < SILENT_MIN_MASTERED and solids < SILENT_MIN_SOLIDS


def compute_nmkp(conn: sqlite3.Connection, user_id: int, ws: date, we: date) -> int:
    """NMKP = |T4/T5 跃迁| − |T6/T7 跃迁|（区间按 created_at 过滤）。"""
    ensure_transition_table(conn)
    s0 = fmt_ts(local_date_range(ws)[0])
    s1 = fmt_ts(local_date_range(we - timedelta(days=1))[1])
    ups = conn.execute(
        "SELECT COUNT(*) FROM kp_state_transition WHERE user_id=? AND rule IN ('T4','T5')"
        " AND created_at>=? AND created_at<?",
        (user_id, s0, s1),
    ).fetchone()[0]
    downs = conn.execute(
        "SELECT COUNT(*) FROM kp_state_transition WHERE user_id=? AND rule IN ('T6','T7')"
        " AND created_at>=? AND created_at<?",
        (user_id, s0, s1),
    ).fetchone()[0]
    return ups - downs


def aggregate_weekly(
    conn: sqlite3.Connection,
    user_id: int = 1,
    week_start: date | None = None,
) -> dict:
    """聚合一周 daily_metric → weekly_metric（含 NMKP/MDE flag/静默期）。"""
    ensure_metric_tables(conn)
    ws = week_start or local_date()
    we = ws + timedelta(days=7)
    days = [(ws + timedelta(days=i)).isoformat() for i in range(7)]
    ph = ",".join("?" for _ in days)

    wk_sql = (
        "SELECT COALESCE(AVG(CASE WHEN session_count>0"
        " THEN self_started_count*1.0/session_count END),0),"
        " COALESCE(SUM(valid_day),0),"
        " COALESCE(AVG(CASE WHEN answer_count>0 THEN pass_rate END),0),"
        " COALESCE(SUM(active_minutes),0)"
        f" FROM daily_metric WHERE user_id=? AND metric_date IN ({ph})"
    )
    row = conn.execute(wk_sql, (user_id, *days)).fetchone()
    sir_avg, wad, rpr, minutes = row

    nmkp = compute_nmkp(conn, user_id, ws, we)

    # MDE 噪声过滤：与上周 NMKP 比较
    prev = conn.execute(
        "SELECT nmkp FROM weekly_metric WHERE user_id=? AND week_start=?",
        (user_id, (ws - timedelta(days=7)).isoformat()),
    ).fetchone()
    if prev is None:
        flag = "FLAT"
    else:
        delta = nmkp - prev[0]
        flag = "UP" if delta >= MDE else ("DOWN" if delta <= -MDE else "FLAT")

    is_silent = int(silent_period(conn, user_id))

    conn.execute(
        "INSERT INTO weekly_metric (user_id, week_start, nmkp, nmkp_mde_flag, sir_avg,"
        " wad_days, pass_rate_avg, active_minutes_sum, silent_period, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(user_id, week_start) DO UPDATE SET"
        " nmkp=excluded.nmkp, nmkp_mde_flag=excluded.nmkp_mde_flag, sir_avg=excluded.sir_avg,"
        " wad_days=excluded.wad_days, pass_rate_avg=excluded.pass_rate_avg,"
        " active_minutes_sum=excluded.active_minutes_sum, silent_period=excluded.silent_period,"
        " updated_at=excluded.updated_at",
        (
            user_id,
            ws.isoformat(),
            nmkp,
            flag,
            round(sir_avg, 3),
            wad,
            round(rpr, 3),
            round(minutes, 1),
            is_silent,
            fmt_ts(now_utc()),
            fmt_ts(now_utc()),
        ),
    )
    conn.commit()
    return {"week_start": ws.isoformat(), "nmkp": nmkp, "mde_flag": flag, "silent": bool(is_silent)}
