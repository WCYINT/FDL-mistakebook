"""每日批处理与 daily_metric 预聚合（P2-13 / MT-02）。

🔴 规则 3（PRD §3.3.3）：批处理只跑 T5/T8/T11/T12（时间驱动类跃迁），
**绝不处理任何升级跃迁**——升级只能来自真实作答证据。

04:00 批处理流程：
1. 刷新每个活跃 KP 的 R(t)（retrievability，基于上次复习与 S 衰减）；
2. 对 REVIEWING/STRUGGLING/MASTERED/CONSOLIDATED 态 KP 跑 `evaluate_batch`
   （仅时间驱动跃迁）并写 kp_state_transition 日志；
3. 聚合昨日 answer_log/study_session → daily_metric（可视化只读此表）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from fdl_core.metrics.tables import ensure_metric_tables
from fdl_core.srs.asr_date import draft_review_date
from fdl_core.srs.mastery import mastery_adj
from fdl_core.srs.params import ModelParams
from fdl_core.srs.state_machine import (
    KpSnapshot,
    evaluate_batch,
    log_transition,
)
from fdl_core.srs.time_layer import fmt_ts, local_date, local_date_range, now_utc, parse_ts

# R(t) 衰减：R = (1 + F · Δt / S)^C，F=0.236 C=-0.5（PRD §6.5）
_R_F, _R_C = 0.236, -0.5

# 每日批处理时刻（本地 04:00）——见 batch_anchor 的幂等与 clamp 说明
_BATCH_HOUR_LOCAL = 4

# ASR 复习录音草稿目录（口述复习是 Frank 当前唯一的复习方式，必须计入日活分钟）
_ASR_DRAFTS_DIR = Path(__file__).resolve().parents[2] / "data" / "asr_drafts"


def batch_anchor(today: date | None = None, now: datetime | None = None) -> datetime:
    """批处理共享时间锚点 = 本地当日 04:00（UTC aware），且**绝不超过当前时刻**。

    [注意] 这是 run_daily_batch 与 kp_state 重建**共用的唯一时钟**（2026-09-13 方案 C）。
    两处独立取时（一处 now_utc、一处固定 04:00）会让同一个 R(t) 算出两个值，
    进而把状态在阈值附近来回翻转、每天多写一条跃迁日志。

    幂等的关键：同一天内多次调用返回同一值，R(t) / retrievability_calc_at /
    updated_at 等时间相关量完全一致，不会因 "重跑时刻差几毫秒" 产生漂移。

    [注意] 为什么必须 clamp（2026-09-13 对抗性验证修复）：
    若在本地 04:00 之前运行（人工凌晨跑批、launchd 提前唤醒），固定的"当日 04:00"
    会落在**未来**，后果有两个且都是真实错误：
      1. R(t) 按未来时刻起算 → 衰减量偏大 → 提前触发降级
         （实测：最后一次 Again 在 15.15 天前、S=0.5 的 KP，真实 R=0.3503
          应保持 STRUGGLING，未来锚点算出 R=0.3492 → 误判 T11 → REGRESSED）；
      2. 写出的 retrievability_calc_at / updated_at / status_changed_at /
         created_at 是未来时间戳，污染下游读时展示。
    回退到当日 00:00 仍是当日内的确定值，故同日重跑依旧逐位一致。
    """
    day = today or local_date()
    day_start, _ = local_date_range(day)
    anchor = day_start + timedelta(hours=_BATCH_HOUR_LOCAL)
    if anchor > (now or now_utc()):
        return day_start
    return anchor


def retrievability(stability_days: float, days_since: float) -> float:
    """可提取性 R(t)：Δt 天未复习后的记忆保留率。"""
    if stability_days <= 0 or days_since <= 0:
        return 1.0
    return max(min((1 + _R_F * days_since / stability_days) ** _R_C, 1.0), 0.0)


def run_daily_batch(
    conn: sqlite3.Connection,
    user_id: int = 1,
    today: date | None = None,
    params: ModelParams | None = None,
    now: datetime | None = None,
) -> dict:
    """04:00 批处理：刷新 R(t) → 时间驱动跃迁 → daily_metric 聚合。

    R(t) 与所有时间戳统一走 ``batch_anchor``（与 kp_state 重建同源，见其 docstring）。
    ``now`` 仅供测试注入；生产不传。
    """
    params = params or ModelParams.load()
    today = today or local_date()
    anchor = batch_anchor(today, now=now)
    ensure_metric_tables(conn)

    # 1+2. 活跃 KP 刷新 R(t) + 时间驱动跃迁
    rows = conn.execute(
        " SELECT k.id, k.code, s.status, s.stability_days, s.difficulty, s.mastery_adj,"
        " s.effective_answer_count, s.total_answer_count, s.exposure_count,"
        " s.consecutive_good_count, s.consecutive_again_count,"
        " s.last_review_at, s.entered_mastered_at, s.performance_score, s.depth_score"
        " FROM kp_state s JOIN knowledge_point k ON k.id = s.kp_id"
        " WHERE s.user_id=? AND s.is_active=1"
        " AND s.status IN ('REVIEWING','STRUGGLING','MASTERED','CONSOLIDATED')",
        (user_id,),
    ).fetchall()

    transitions = []
    anchor_iso = fmt_ts(anchor)
    for r in rows:
        (
            kp_id,
            code,
            status,
            stab,
            diff,
            m_adj,
            n_eff,
            total,
            expo,
            cg,
            ca,
            last_review,
            entered_m,
            p_score,
            g_score,
        ) = r
        days_since = 0.0
        if last_review:
            days_since = (anchor - parse_ts(last_review)).total_seconds() / 86400
        r_t = retrievability(stab or 0.5, days_since)
        overdue_ratio = 0.0
        conn.execute(
            "UPDATE kp_state SET retrievability=?, retrievability_calc_at=?, updated_at=?"
            " WHERE user_id=? AND kp_id=?",
            (r_t, anchor_iso, anchor_iso, user_id, kp_id),
        )
        m = mastery_adj(r_t, p_score or 0.45, g_score or 0.2, n_eff or 0.0, params)
        snap = KpSnapshot(
            code=code,
            status=status,
            exposure_count=expo or 0,
            n_eff=n_eff or 0.0,
            total_answer_count=total or 0,
            consecutive_good_count=cg or 0,
            consecutive_again_count=ca or 0,
            stability_days=stab or 0.5,
            retrievability=r_t,
            m_adj=m.mastery_adj,
            m_adj_before=m_adj or 0.0,
            overdue_ratio=overdue_ratio,
            entered_mastered_date=entered_m,
        )
        result = evaluate_batch(snap, today=today, params=params)
        if result.changed:
            conn.execute(
                "UPDATE kp_state SET status=?, prev_status=?, status_changed_at=?, updated_at=?"
                " WHERE user_id=? AND kp_id=?",
                (result.new_status, status, anchor_iso, anchor_iso, user_id, kp_id),
            )
            log_transition(
                conn,
                user_id=user_id,
                kp_id=kp_id,
                result=result,
                snapshot=snap,
                triggered_by="daily_batch",
            )
        transitions.append(
            {"kp": code, "from": status, "to": result.new_status, "rule": result.rule}
        )

    conn.commit()

    # 3. 聚合昨日 daily_metric
    metric = aggregate_daily(conn, user_id, today - timedelta(days=1))
    return {"transitions": transitions, "daily_metric": metric}


def _draft_review_date(draft: dict, filename: str) -> date | None:
    """ASR 录音归日（兼容别名 → 共享口径实现，见 fdl_core.srs.asr_date）。

    🔴 禁止对 UTC 时间戳做 `[:10]` 切片——统一走 `draft_review_date`。
    """
    return draft_review_date(draft, filename)


def asr_review_minutes(d: date, drafts_dir: Path | None = None) -> float:
    """指定本地日期的 ASR 口述复习分钟 = Σ duration_sec / 60。

    口径与 `scripts/generate_report.py` 的权威实现一致：录音时长直接计入复习
    时长（录音全程即有效复习行为，无「非有效时间」概念）。
    读取失败逐文件跳过——批处理不能因草稿损坏而中断。
    """
    drafts_dir = drafts_dir or _ASR_DRAFTS_DIR
    if not drafts_dir.exists():
        return 0.0
    total_sec = 0.0
    for df in sorted(drafts_dir.glob("*.draft.json")):
        try:
            draft = json.loads(df.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _draft_review_date(draft, df.name) == d:
            total_sec += float(draft.get("duration_sec") or 0.0)
    return total_sec / 60.0


def _ensure_pass_rate_nullable(conn: sqlite3.Connection) -> None:
    """迁移（P27）：daily_metric.pass_rate 放开 NOT NULL，让「无作答日」能存 NULL。

    NULL = 无样本（报告显示「—」），与真实 0% 及格率语义区分。
    幂等：已可空则直接返回；老库走一次表重建（daily_metric 是可从事实表重算的
    预聚合表，重建无信息损失）。
    """
    info = conn.execute("PRAGMA table_info(daily_metric)").fetchall()
    if not any(r[1] == "pass_rate" and r[3] for r in info):
        return
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_metric'"
    ).fetchone()[0]
    new_ddl = re.sub(r"(pass_rate\s+REAL)[^,\n]*", r"\1", ddl, count=1)
    new_ddl = new_ddl.replace("daily_metric", "daily_metric__mig", 1)
    cols = ",".join(r[1] for r in info)
    conn.executescript(
        f"{new_ddl};\n"
        f"INSERT INTO daily_metric__mig ({cols}) SELECT {cols} FROM daily_metric;\n"
        "DROP TABLE daily_metric;\n"
        "ALTER TABLE daily_metric__mig RENAME TO daily_metric;\n"
        "CREATE INDEX IF NOT EXISTS idx_daily_metric_user_date"
        " ON daily_metric (user_id, metric_date);"
    )
    conn.commit()


def aggregate_daily(
    conn: sqlite3.Connection,
    user_id: int,
    d: date,
    asr_drafts_dir: Path | None = None,
) -> dict:
    """聚合指定日期的 daily_metric（预聚合，可视化只读此表）。

    # reviews_done → today_reviewed_answers（2026-09-08 P23：消除歧义；
    # 保持 SUM(answer_log WHERE task_type='REVIEW') 口径）
    """
    ensure_metric_tables(conn)
    _ensure_pass_rate_nullable(conn)
    day_start, day_end = __import__(
        "fdl_core.srs.time_layer", fromlist=["local_date_range"]
    ).local_date_range(d)
    s0, s1 = fmt_ts(day_start), fmt_ts(day_end)

    # 🔴 计数与时长口径必须一致：全部只统计有效会话（is_valid=1）。
    # 时长用 effective_sec（有效学习秒），duration_sec 是挂钟时长（含分神/中断）。
    sess = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN is_valid THEN 1 ELSE 0 END),0),"
        " COALESCE(SUM(CASE WHEN is_valid AND trigger_type='SELF' THEN 1 ELSE 0 END),0),"
        " COALESCE(SUM(CASE WHEN is_valid THEN effective_sec ELSE 0 END),0)"
        " FROM study_session WHERE user_id=? AND session_date=?",
        (user_id, d.isoformat()),
    ).fetchone()
    ans = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(is_correct),0),"
        " COALESCE(SUM(task_type='NEW_LEARN'),0), COALESCE(SUM(task_type='REVIEW'),0)"
        " FROM answer_log WHERE user_id=? AND answered_at>=? AND answered_at<?"
        " AND is_valid_evidence=1",
        (user_id, s0, s1),
    ).fetchone()
    xp = conn.execute(
        "SELECT COALESCE(SUM(xp_earned),0) FROM daily_task WHERE user_id=? AND task_date=?",
        (user_id, d.isoformat()),
    ).fetchone()[0]
    dist = conn.execute(
        "SELECT status, COUNT(*) FROM kp_state WHERE user_id=? GROUP BY status",
        (user_id,),
    ).fetchall()
    dist_d = {st: c for st, c in dist}

    sessions, self_started, active_sec = sess
    answers, correct, new_learned, today_reviewed_answers = ans
    # 无作答 → NULL（无样本），不写 0.0（会与真实 0% 及格率混淆）
    pass_rate = (correct / answers) if answers else None
    # 日活分钟 = 有效会话 effective_sec + ASR 口述复习时长
    active_minutes = round(active_sec / 60.0 + asr_review_minutes(d, asr_drafts_dir), 1)

    upsert_sql = (
        "INSERT INTO daily_metric (user_id, metric_date, session_count, self_started_count,"
        " answer_count, correct_count, pass_rate, new_learned, today_reviewed_answers,"
        " active_minutes,"
        " valid_day, xp_earned, kp_learning, kp_reviewing, kp_struggling, kp_mastered,"
        " kp_consolidated, kp_regressed, kp_archived, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(user_id, metric_date) DO UPDATE SET"
        " session_count=excluded.session_count, self_started_count=excluded.self_started_count,"
        " answer_count=excluded.answer_count, correct_count=excluded.correct_count,"
        " pass_rate=excluded.pass_rate, new_learned=excluded.new_learned,"
        " today_reviewed_answers=excluded.today_reviewed_answers,"
        " active_minutes=excluded.active_minutes,"
        " valid_day=excluded.valid_day, xp_earned=excluded.xp_earned,"
        " kp_learning=excluded.kp_learning, kp_reviewing=excluded.kp_reviewing,"
        " kp_struggling=excluded.kp_struggling, kp_mastered=excluded.kp_mastered,"
        " kp_consolidated=excluded.kp_consolidated, kp_regressed=excluded.kp_regressed,"
        " kp_archived=excluded.kp_archived, updated_at=excluded.updated_at"
    )
    upsert_vals = (
        user_id,
        d.isoformat(),
        sessions,
        self_started,
        answers,
        correct,
        pass_rate,
        new_learned,
        today_reviewed_answers,
        active_minutes,
        int(active_minutes >= 5.0),
        xp,
        dist_d.get("LEARNING", 0),
        dist_d.get("REVIEWING", 0),
        dist_d.get("STRUGGLING", 0),
        dist_d.get("MASTERED", 0),
        dist_d.get("CONSOLIDATED", 0),
        dist_d.get("REGRESSED", 0),
        dist_d.get("ARCHIVED", 0),
        fmt_ts(now_utc()),
        fmt_ts(now_utc()),
    )
    conn.execute(upsert_sql, upsert_vals)
    conn.commit()
    return {"date": d.isoformat(), "answers": answers, "pass_rate": pass_rate}
