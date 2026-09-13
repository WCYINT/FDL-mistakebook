"""kp_state 生产者（方案 B：每日批处理全量重建）。

背景：kp_state 表结构完整但长期 0 行——没有任何代码在复习后写入它，导致
报告能量 R(t) 恒 1.0、星图无 m_adj、掌握度周报无源数据、A2 观察期缺掌握度输入。
方案 B 在每日 04:00 批处理里从既有事实表**全量重算**每个 KP 的状态机与分数，
UPSERT 进 kp_state。选它是因为：天然幂等可重跑、不碰复习热路径、与 A2 的
"每日批处理" 哲学一致。

数据来源与口径：
- ``mistake_record``（挂载 / 复现 / 驯服）：KP 挂载证据、难度 D（fsrs_d 均值）、
  mistake_count、first_exposed_at；
- ``review_feedback.self_rating``（1 陌生 / 2 模糊 / 3 掌握 / 4 熟练）：
  ``grade = RATING_TO_GRADE[rating] = rating - 1``，按时间升序重放驱动状态机；
- ``review_schedule``（planned_interval_days / PENDING due）：plan_exists、无作答
  KP 的 S 引导、逾期比 overdue_ratio、next_due_at。计划按
  ``COALESCE(mistake.kp_id, rs.kp_id)`` 归因——兼容 mistake_id 为 NULL 的旧计划。

反馈到 KP 的归因 SQL（三段链，顺序 = 证据强度）：
    review_feedback rf
      LEFT JOIN review_schedule rs ON rs.id = rf.schedule_id
      LEFT JOIN mistake_record  m  ON m.id  = rs.mistake_id
``kp_id = COALESCE(NULLIF(m.kp_id,0), NULLIF(rf.kp_id,0), NULLIF(rs.kp_id,0))``，
即 mistake.kp_id → feedback.kp_id → schedule.kp_id；三级皆 0/NULL → 跳过该事件
（不臆造归因）。末位 rs.kp_id 是 A1 时代的显式 KP 引用，属恢复既有证据。

设计要点（逐条对应 King 拍板）：
1. **只有有证据的 KP 建行**：至少 1 条关联 mistake_record 或 1 条可归因反馈；
   无证据 KP 不建行，daily_metric 的状态计数保持诚实。
2. **天然幂等**：时间锚点用与 run_daily_batch 共享的 ``daily.batch_anchor``
   （本地当日 04:00，且绝不超前），同一天内重复重建的所有计算列逐位一致、
   无漂移；跃迁日志只在 "重建后 status 与库中不同" 时写一条
   （triggered_by="rebuild"），重复运行不新增日志。
3. **不触碰复习热路径**：本模块只被 scripts/daily_batch.py 调用；
   mark_reviewed 绝不引用它（复习主流程零开销）。
4. **UNLEARNED 语义**：有 mistake 但零反馈事件的 KP 不升级（无作答不构成学习
   证据，铁律 1：升级绝不因时间流逝自动发生）；S 取最新计划间隔引导值。
5. **G（深度分）暂无生产者**：depth_score([], now) = 0.2 基线，待深度行为
   采集落地后改为真实行为序列。

S 演化：走 PRD §6.5 的规范实现 ``reschedule.update_stability``（答对按 R 调制
增长 K_GROW、答错 ×K_DECAY、超过 S_CAP_GROWTH 封顶）。``INTERVAL_FACTOR`` 只用于
S→间隔换算，**不**用于 S 增长。grade==1（模糊）也会小幅增长（GRADE_WEIGHT 0.3）。

P（表现分）口径说明：``performance_score`` 实现按列表序号加权（i=0 权重最高），
与其 "seq=0 最新" 注释一致，因此本模块传入 **newest-first** 列表
（``_p_newest_first``）。难度用该 KP 的 fsrs_d 均值（缺省 5.0）。

已知限制（刻意不做，非待办疏忽）：
- **``answer_log`` 未纳入重放**：重放证据源单一取 ``review_feedback``。
  ``answer_log`` 含 kp_id/grade/is_valid_evidence，本可作兜底证据，但当前无法保证
  与 review_feedback 不重复计数（同一复习可能两表各留一条），双计会虚增
  exposure/作答数。待两条链的去重规则明确后再接入。后果：只有 answer_log 真实
  作答、没有 review_feedback 的 KP 会显示 UNLEARNED / exposure=0。
- **T10 生产源缺失**：T10（reteach 后首次做对 → LEARNING）要求
  ``is_reteach=True``，当前无任何代码把它置真，故 STRUGGLING 无升级出口，
  只能靠 T11 时间驱动降到 REGRESSED。属 dormant 而非断链。
- **``updated_at`` 语义 = 批处理锚点**（当日 04:00，或凌晨运行时回退到 00:00），
  **不是**墙钟写入时刻。这是幂等的必要代价：同一天重跑必须给出同一个值。
- **``retrievability`` / ``retrievability_calc_at`` 是锚点时刻值**，非读取时刻实时值；
  消费方若要实时 R，用 ``retrievability(stability_days, (now - last_review_at)/86400)``
  自行现算（公式唯一实现在 ``fdl_core.metrics.daily.retrievability``）。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from fdl_core.metrics.daily import batch_anchor, retrievability
from fdl_core.srs.mastery import (
    depth_score,
    effective_answers,
    mastery_adj,
    performance_score,
)
from fdl_core.srs.params import ModelParams
from fdl_core.srs.reschedule import update_stability
from fdl_core.srs.state_machine import (
    CONSOLIDATED,
    MASTERED,
    UNLEARNED,
    KpSnapshot,
    TransitionResult,
    evaluate_answer,
    evaluate_batch,
    log_transition,
)
from fdl_core.srs.time_layer import (
    fmt_ts,
    local_date,
    parse_ts_lenient,
)

# 评分映射（review_feedback.self_rating → 状态机 grade）
# 1 陌生→0 Again / 2 模糊→1 / 3 掌握→2 / 4 熟练→3
RATING_TO_GRADE = {1: 0, 2: 1, 3: 2, 4: 3}

MODEL_VERSION = "rebuild-v1"


def _parse_evidence_ts(raw: object) -> datetime | None:
    """宽容解析证据时间戳（兼容 SQLite CURRENT_TIMESTAMP 空格格式，语义 UTC）。"""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return parse_ts_lenient(str(raw))
    except (ValueError, TypeError):
        return None


def _p_newest_first(answers_asc: list[dict], difficulty: float, params: ModelParams) -> float:
    """P：把时间升序的作答序列反转成 newest-first 后交给 performance_score。

    performance_score 内部权重 ``LAMBDA ** i``（i=0 最高），对应其 "seq=0 最新"
    口径；它不消费 seq 字段，因此顺序即权重，必须 newest-first。
    """
    newest_first = [
        {"grade": a["grade"], "difficulty": difficulty, "seq": i}
        for i, a in enumerate(reversed(answers_asc))
    ]
    return performance_score(newest_first, params)


def _kp_difficulty(mistakes: list[dict]) -> float:
    """KP 难度 = 关联 mistake_record.fsrs_d 均值（缺省 5.0 先验）。"""
    vals = [float(m["fsrs_d"]) for m in mistakes if m.get("fsrs_d") is not None]
    return sum(vals) / len(vals) if vals else 5.0


def _seed_stability(schedules: list[dict], s_min: float, interval_factor: float) -> float:
    """零作答 KP 的 S 引导：最新计划间隔 / INTERVAL_FACTOR（clamp >= S_MIN）。

    候选集合优先取 PENDING（当前生效的计划才代表"我们打算让孩子隔多久再见到
    这个知识点"）；若一条 PENDING 都没有（全部已 DONE 且尚未排下一次），回退到
    全体计划——此时用最近一次的历史间隔作引导，总好过直接塌回 S_MIN 让 R(t) 虚高。
    """
    if not schedules:
        return s_min
    candidates = [s for s in schedules if str(s.get("status") or "").upper() == "PENDING"]
    if not candidates:
        candidates = schedules
    latest = max(
        candidates,
        key=lambda s: (str(s.get("due_date") or ""), int(s.get("id") or 0)),
    )
    interval = latest.get("planned_interval_days")
    if not interval or float(interval) <= 0:
        return s_min
    return max(float(interval) / interval_factor, s_min)


def _parse_due(due: object) -> date | None:
    """due_date 是 DATE 列（YYYY-MM-DD），统一走宽容时间层解析为日期。"""
    dt = _parse_evidence_ts(due)
    return dt.date() if dt else None


def _next_due_and_overdue(schedules: list[dict], today: date) -> tuple[str | None, float]:
    """该 KP 最早的 PENDING due → (next_due_at, overdue_ratio)。"""
    pending = [
        s
        for s in schedules
        if str(s.get("status") or "").upper() == "PENDING" and s.get("due_date")
    ]
    if not pending:
        return None, 0.0
    earliest = min(pending, key=lambda s: (str(s["due_date"]), int(s.get("id") or 0)))
    due = _parse_due(earliest.get("due_date"))
    next_due_at = str(earliest["due_date"])
    if due is None or due >= today:
        return next_due_at, 0.0
    interval = earliest.get("planned_interval_days")
    if not interval or float(interval) <= 0:
        return next_due_at, 0.0
    return next_due_at, (today - due).days / float(interval)


def _collect_evidence(conn: sqlite3.Connection, user_id: int) -> tuple[dict, dict, dict, dict]:
    """采集 mistake / feedback / schedule / KP 元数据（均按 KP 分桶）。"""
    kp_meta = {
        int(r[0]): {"subject_id": int(r[1]), "code": str(r[2])}
        for r in conn.execute("SELECT id, subject_id, code FROM knowledge_point")
    }

    kp_mistakes: dict[int, list[dict]] = {}
    for mid, kp_id, occurred_at, created_at, reappear, is_tamed, last_rep, fsrs_d in conn.execute(
        "SELECT id, kp_id, occurred_at, created_at, reappear_count, is_tamed,"
        " last_reappear_at, fsrs_d FROM mistake_record"
        " WHERE user_id=? AND kp_id IS NOT NULL AND kp_id > 0",
        (user_id,),
    ).fetchall():
        kp_mistakes.setdefault(int(kp_id), []).append(
            {
                "id": int(mid),
                "occurred_at": occurred_at,
                "created_at": created_at,
                "reappear_count": int(reappear or 0),
                "is_tamed": int(is_tamed or 0),
                "last_reappear_at": last_rep,
                "fsrs_d": fsrs_d,
            }
        )

    kp_feedback: dict[int, list[dict]] = {}
    # 三段归因链（顺序 = 证据强度）：mistake.kp_id → feedback.kp_id → schedule.kp_id。
    # 末位 rs.kp_id 是 A1 时代的调度单元（schedule 行上的显式 KP 引用），属恢复既有
    # 证据而非臆造——NULLIF 已把 0 占位（未挂载语义）排除，两者皆非零才采用；
    # 与下方 kp_schedules 的口径对称，消除"计划挂得上、反馈证据被丢"的不一致。
    for fid, rating, created_at, kp_id, planned_interval, due_date, sched_id in conn.execute(
        "SELECT rf.id, rf.self_rating, rf.created_at,"
        " COALESCE(NULLIF(m.kp_id, 0), NULLIF(rf.kp_id, 0), NULLIF(rs.kp_id, 0)) AS kp_id,"
        " rs.planned_interval_days, rs.due_date, rs.id"
        " FROM review_feedback rf"
        " LEFT JOIN review_schedule rs ON rs.id = rf.schedule_id"
        " LEFT JOIN mistake_record  m  ON m.id  = rs.mistake_id"
        " WHERE rf.user_id=?",
        (user_id,),
    ).fetchall():
        if kp_id is None or int(kp_id) <= 0:
            continue  # 三段链全不可达 → 无法归因，不臆造
        r = int(rating)
        grade = RATING_TO_GRADE.get(r, min(max(r - 1, 0), 3))
        ts = _parse_evidence_ts(created_at)
        if ts is None:
            continue
        kp_feedback.setdefault(int(kp_id), []).append(
            {
                "id": int(fid),
                "rating": r,
                "grade": grade,
                "ts": ts,
                "planned_interval_days": planned_interval,
                "due_date": due_date,
                "schedule_id": sched_id,
            }
        )

    kp_schedules: dict[int, list[dict]] = {}
    # LEFT JOIN + COALESCE：旧计划可能只挂了 rs.kp_id（mistake_id 为 NULL，A3 迁移前的
    # 数据形态），此前用 INNER JOIN mistake_record 会让这类计划整体失明
    # （plan_exists / next_due_at / overdue_ratio 全丢）。两侧取非零值兜底。
    for sid, status, due_date, planned_interval, kp_id in conn.execute(
        "SELECT rs.id, rs.status, rs.due_date, rs.planned_interval_days,"
        " COALESCE(NULLIF(m.kp_id, 0), NULLIF(rs.kp_id, 0)) AS kp_id"
        " FROM review_schedule rs"
        " LEFT JOIN mistake_record m ON m.id = rs.mistake_id"
        " WHERE rs.user_id=?",
        (user_id,),
    ).fetchall():
        if kp_id is None or int(kp_id) <= 0:
            continue
        kp_schedules.setdefault(int(kp_id), []).append(
            {
                "id": int(sid),
                "status": status,
                "due_date": due_date,
                "planned_interval_days": planned_interval,
            }
        )

    return kp_mistakes, kp_feedback, kp_schedules, kp_meta


_INSERT_COLUMNS = (
    "user_id",
    "kp_id",
    "subject_id",
    "status",
    "prev_status",
    "status_changed_at",
    "entered_mastered_at",
    "stability_days",
    "difficulty",
    "retrievability",
    "retrievability_calc_at",
    "lapses",
    "performance_score",
    "depth_score",
    "ability_estimate",
    "mastery_raw",
    "confidence",
    "mastery_adj",
    "exposure_count",
    "effective_answer_count",
    "total_answer_count",
    "consecutive_good_count",
    "consecutive_again_count",
    "mistake_count",
    "first_exposed_at",
    "last_review_at",
    "next_due_at",
    "is_active",
    "model_version",
    "created_at",
    "updated_at",
)

_INSERT_SQL = (
    f"INSERT INTO kp_state ({', '.join(_INSERT_COLUMNS)})"
    f" VALUES ({', '.join('?' * len(_INSERT_COLUMNS))})"
    " ON CONFLICT(user_id, kp_id) DO UPDATE SET"
    " subject_id=excluded.subject_id,"
    " status=excluded.status,"
    # prev_status / status_changed_at 仅在状态真变化时改写（幂等：不变化则保留旧值）
    " prev_status=CASE WHEN excluded.status <> kp_state.status"
    "   THEN kp_state.status ELSE kp_state.prev_status END,"
    " status_changed_at=CASE WHEN excluded.status <> kp_state.status"
    "   THEN excluded.status_changed_at ELSE kp_state.status_changed_at END,"
    # entered_mastered_at：新计算值优先（可纠正早期误记），无新值则保留旧值。
    # 与 prev_status/status_changed_at 的 "仅变化时改写" 不同——进入 MASTERED 的
    # 时刻是事实，重放能算出更准的值就该覆盖。
    " entered_mastered_at=COALESCE(excluded.entered_mastered_at,"
    "   kp_state.entered_mastered_at),"
    " stability_days=excluded.stability_days,"
    " difficulty=excluded.difficulty,"
    " retrievability=excluded.retrievability,"
    " retrievability_calc_at=excluded.retrievability_calc_at,"
    " lapses=excluded.lapses,"
    " performance_score=excluded.performance_score,"
    " depth_score=excluded.depth_score,"
    " ability_estimate=excluded.ability_estimate,"
    " mastery_raw=excluded.mastery_raw,"
    " confidence=excluded.confidence,"
    " mastery_adj=excluded.mastery_adj,"
    " exposure_count=excluded.exposure_count,"
    " effective_answer_count=excluded.effective_answer_count,"
    " total_answer_count=excluded.total_answer_count,"
    " consecutive_good_count=excluded.consecutive_good_count,"
    " consecutive_again_count=excluded.consecutive_again_count,"
    " mistake_count=excluded.mistake_count,"
    " first_exposed_at=excluded.first_exposed_at,"
    " last_review_at=excluded.last_review_at,"
    " next_due_at=excluded.next_due_at,"
    # 人工冻结（is_active=0）不被批处理复活
    " is_active=kp_state.is_active,"
    " model_version=excluded.model_version,"
    " updated_at=excluded.updated_at"
)


def _replay_kp(
    *,
    code: str,
    mistakes: list[dict],
    feedbacks: list[dict],
    schedules: list[dict],
    subject_id: int,
    anchor: datetime,
    today: date,
    params: ModelParams,
) -> tuple[tuple, str, str | None, list[dict]]:
    """重放单个 KP 的全部作答事件 + 今日批处理态评估。

    返回 ``(insert_values, status, last_rule, replay_transitions)``；
    ``replay_transitions`` 是重放过程中的完整步进（含未落库的中间跃迁），
    与最终写入 ``kp_state_transition`` 的日志（见 ``rebuild_kp_state``）语义不同。

    [注意] 事件后 R 语义（2026-09-13 修复 T6/T7 死路）：
    ``m_after`` 必须用**事件后**的可提取性计算，否则 T6/T7 的 ΔM_adj 恒为零、
    降级永不触发：
    - 答对（grade>=1）→ ``r_after = 1.0``（刚成功回忆，对齐 reschedule 答对后
      ``retrievability=1.0`` 的既有口径）；
    - 答错（grade==0）→ ``r_after = r_before``（失败不会刷新记忆，沿用衰减值）。
    ``m_before`` 取**上一事件的 m_after**（跨事件跟踪），首个事件退回事件前先验，
    这样 ΔM_adj 表达的是 "上一次作答后 → 本次作答后" 的真实变化。
    """
    m = params.mastery
    s_min = float(m["S_MIN"])
    interval_factor = float(m["INTERVAL_FACTOR"])
    anchor_iso = fmt_ts(anchor)

    events = sorted(feedbacks, key=lambda e: (e["ts"], e["id"]))
    difficulty = _kp_difficulty(mistakes)
    plan_exists = len(schedules) > 0
    # G（深度分）：暂无深度行为生产者，恒为基线 0.2
    g_score = depth_score([], anchor, params)

    # 零作答 KP 的 S 引导值；有作答则从 S_MIN 起按事件演化
    s = _seed_stability(schedules, s_min, interval_factor) if not events else s_min

    status = UNLEARNED
    exposure = total = consec_good = consec_again = lapses = 0
    last_again_date: str | None = None
    entered_mastered: str | None = None
    mastered_grades: list[int] = []
    answers_asc: list[dict] = []
    last_ts: datetime | None = None
    last_rule: str | None = None
    replay_transitions: list[dict] = []
    prev_m_after: float | None = None  # 上一事件的 m_after（ΔM_adj 基准）

    for ev in events:
        ts = ev["ts"]
        grade = ev["grade"]
        pre_status = status  # 事件前状态：T6/T7 判定 + mastered 评分域
        days_since_prev = (ts - last_ts).total_seconds() / 86400 if last_ts is not None else 0.0
        r_before = retrievability(s, days_since_prev)
        p_before = _p_newest_first(answers_asc, difficulty, params)
        n_real_before = total
        m_before = (
            prev_m_after
            if prev_m_after is not None
            else mastery_adj(r_before, p_before, g_score, n_real_before, params).mastery_adj
        )
        r_after = 1.0 if grade >= 1 else r_before

        # 事件聚合（含本次事件）
        exposure += 1
        total += 1
        if grade >= 2:
            consec_good += 1
            consec_again = 0
        elif grade == 0:
            consec_again += 1
            consec_good = 0
            lapses += 1
            last_again_date = local_date(ts).isoformat()
        else:  # grade == 1：模糊 → 两条连续计数同时打断
            consec_good = 0
            consec_again = 0

        answers_asc.append({"grade": grade, "difficulty": difficulty})
        p_after = _p_newest_first(answers_asc, difficulty, params)
        m_after = mastery_adj(r_after, p_after, g_score, total, params)
        prev_m_after = m_after.mastery_adj

        snap = KpSnapshot(
            code=code,
            status=status,
            exposure_count=exposure,
            n_eff=effective_answers(n_real_before, params),
            total_answer_count=total,
            consecutive_good_count=consec_good,
            consecutive_again_count=consec_again,
            stability_days=s,
            retrievability=r_before,
            m_adj=m_after.mastery_adj,
            m_adj_before=m_before,
            grade=grade,
            plan_exists=plan_exists,
            overdue_ratio=0.0,
            last_again_date=last_again_date,
            entered_mastered_date=entered_mastered,
            mastered_review_grades=list(mastered_grades),
            last_review_date=local_date(last_ts).isoformat() if last_ts else None,
            is_active=True,
        )
        # today=事件当天：T4 的 14 天窗必须用事件当天的日期衡量历史时刻
        result = evaluate_answer(snap, params, today=local_date(ts))
        if result.changed:
            replay_transitions.append(
                {"rule": result.rule, "from": status, "to": result.new_status, "at": ts.isoformat()}
            )
            last_rule = result.rule
            status = result.new_status
            if status == MASTERED and entered_mastered is None:
                entered_mastered = fmt_ts(ts)
        if pre_status in (MASTERED, CONSOLIDATED):
            # T5 的 "保持期 >=2 次复习" / T12 的 "近 2 次 grade" 证据。
            # 域含 CONSOLIDATED：巩固期仍在复习，其评分同样应是归档判定的证据，
            # 否则 last2 永远停留在很久以前的 MASTERED 期（口径偏旧）。
            mastered_grades.append(grade)

        # S 演化：走 PRD §6.5 的规范实现（K_GROW/K_DECAY/S_CAP_GROWTH），
        # 不用 INTERVAL_FACTOR 连乘（INTERVAL_FACTOR 只用于 S→间隔换算）。
        s = update_stability(s, grade, r_before, params)

        last_ts = ts

    # ── 重放结束：最终分数 ───────────────────────────────────
    days_since_last = (anchor - last_ts).total_seconds() / 86400 if last_ts is not None else 0.0
    r_final = retrievability(s, days_since_last)
    p_final = _p_newest_first(answers_asc, difficulty, params)
    m_final = mastery_adj(r_final, p_final, g_score, total, params)

    next_due_at, overdue_ratio = _next_due_and_overdue(schedules, today)
    last_review_date = local_date(last_ts).isoformat() if last_ts is not None else None

    snap_final = KpSnapshot(
        code=code,
        status=status,
        exposure_count=exposure,
        n_eff=m_final.n_eff,
        total_answer_count=total,
        consecutive_good_count=consec_good,
        consecutive_again_count=consec_again,
        stability_days=s,
        retrievability=r_final,
        m_adj=m_final.mastery_adj,
        m_adj_before=m_final.mastery_adj,
        grade=None,
        plan_exists=plan_exists,
        overdue_ratio=overdue_ratio,
        last_again_date=last_again_date,
        entered_mastered_date=entered_mastered,
        mastered_review_grades=list(mastered_grades),
        last_review_date=last_review_date,
        is_active=True,
    )
    batch_result = evaluate_batch(snap_final, today=today, params=params)
    if batch_result.changed:
        replay_transitions.append(
            {
                "rule": batch_result.rule,
                "from": status,
                "to": batch_result.new_status,
                "at": anchor.isoformat(),
            }
        )
        last_rule = batch_result.rule
        status = batch_result.new_status

    # first_exposed_at = 最早证据时间（mistake 发生/建档 + 首次反馈）
    evidence_ts = [
        t
        for t in (
            [_parse_evidence_ts(x.get("occurred_at")) for x in mistakes]
            + [_parse_evidence_ts(x.get("created_at")) for x in mistakes]
            + [e["ts"] for e in events]
        )
        if t is not None
    ]
    first_exposed_at = fmt_ts(min(evidence_ts)) if evidence_ts else None

    values = (
        None,  # user_id 占位（调用处替换）
        None,  # kp_id 占位
        subject_id,
        status,
        None,  # prev_status：仅 insert 为 NULL；更新走 CASE
        anchor_iso,  # status_changed_at（新建/变化时由 CASE 决定是否采用）
        entered_mastered,
        s,
        difficulty,
        r_final,
        anchor_iso,  # retrievability_calc_at
        lapses,
        m_final.p,
        m_final.g,
        m_final.a,
        m_final.mastery_raw,
        m_final.confidence,
        m_final.mastery_adj,
        exposure,
        int(round(m_final.n_eff)),  # effective_answer_count（INTEGER 列）
        total,
        consec_good,
        consec_again,
        len(mistakes),
        first_exposed_at,
        fmt_ts(last_ts) if last_ts is not None else None,
        next_due_at,
        1,  # is_active（更新时由 SQL 保留原值）
        MODEL_VERSION,
        anchor_iso,  # created_at（仅 insert 生效）
        anchor_iso,  # updated_at
    )
    return values, status, last_rule, replay_transitions


def rebuild_kp_state(
    conn: sqlite3.Connection,
    user_id: int = 1,
    today: date | None = None,
    params: ModelParams | None = None,
) -> dict:
    """全量重建 kp_state（方案 B）。幂等：同一天内重跑结果逐位一致。

    返回::

        {
          "kps":  参与重建的 KP 数,
          "created": 新建行数, "updated": 已有行更新数,
          # 真正写入 kp_state_transition 的行（仅"已有行且状态与库中不同"时才写）
          "transitions":        [{kp, from, to, rule}],
          # 重放过程中的完整步进（含新建行未落库的中间跃迁，仅供信息/诊断）
          "replay_transitions": [{kp, from, to, rule, at}],
        }

    取舍说明（非原子，刻意不改）：``log_transition`` 内部各自 ``commit``，加上
    循环里的 UPSERT，本函数整体**不是**单事务。中途异常（如某行违反约束）可能留下
    部分已写入的行与日志。这里依赖**幂等重跑自愈**：本函数设计为"全量重算 + UPSERT"，
    次日（或立即）重跑会把每行重算到正确值，无需回滚补偿；代价是失败瞬间存在短暂的
    部分状态。若未来需要严格原子，应改为显式 ``BEGIN``/``COMMIT`` 并把日志写入
    改为只 append 不 commit（属架构改动，当前规模下收益不足）。
    """
    params = params or ModelParams.load()
    today = today or local_date()
    # 共享时钟：与 run_daily_batch 同源（见 daily.batch_anchor），避免双时钟把状态来回翻
    anchor = batch_anchor(today)

    kp_mistakes, kp_feedback, kp_schedules, kp_meta = _collect_evidence(conn, user_id)
    kp_ids = sorted((set(kp_mistakes) | set(kp_feedback)) & set(kp_meta))
    if not kp_ids:
        return {
            "kps": 0,
            "created": 0,
            "updated": 0,
            "transitions": [],
            "replay_transitions": [],
        }

    existing_status = {
        int(r[0]): str(r[1])
        for r in conn.execute("SELECT kp_id, status FROM kp_state WHERE user_id=?", (user_id,))
    }

    created = updated = 0
    logged_transitions: list[dict] = []
    all_replay: list[dict] = []

    for kp_id in kp_ids:
        meta = kp_meta[kp_id]
        mistakes = kp_mistakes.get(kp_id, [])
        feedbacks = kp_feedback.get(kp_id, [])
        schedules = kp_schedules.get(kp_id, [])

        values, status, last_rule, kp_replay = _replay_kp(
            code=meta["code"],
            mistakes=mistakes,
            feedbacks=feedbacks,
            schedules=schedules,
            subject_id=meta["subject_id"],
            anchor=anchor,
            today=today,
            params=params,
        )
        insert_values = (user_id, kp_id) + values[2:]
        conn.execute(_INSERT_SQL, insert_values)

        for t in kp_replay:
            all_replay.append({"kp": meta["code"], **t})

        old_status = existing_status.get(kp_id)
        if old_status is None:
            # 新建非跃迁 → 不写日志（避免每次全量重建都堆创建记录）
            created += 1
            continue
        updated += 1
        if old_status != status:
            result = TransitionResult(
                new_status=status,
                rule=last_rule or "REBUILD",
                reason="批处理重建（与库中状态不一致）",
            )
            _snap_for_log = KpSnapshot(
                code=meta["code"],
                status=old_status,
                exposure_count=int(values[18]),
                n_eff=float(values[19]),
                total_answer_count=int(values[20]),
                consecutive_good_count=int(values[21]),
                consecutive_again_count=int(values[22]),
                stability_days=float(values[7]),
                retrievability=float(values[9]),
                m_adj=float(values[17]),
                m_adj_before=float(values[17]),
            )
            log_transition(
                conn,
                user_id=user_id,
                kp_id=kp_id,
                result=result,
                snapshot=_snap_for_log,
                triggered_by="rebuild",
            )
            logged_transitions.append(
                {
                    "kp": meta["code"],
                    "from": old_status,
                    "to": status,
                    "rule": result.rule,
                }
            )

    conn.commit()
    return {
        "kps": len(kp_ids),
        "created": created,
        "updated": updated,
        "transitions": logged_transitions,
        "replay_transitions": all_replay,
    }
