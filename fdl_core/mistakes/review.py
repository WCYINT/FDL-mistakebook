"""错题复习动作记录（VIS-12 闭环）：复习完成 → mistake_record 回写。

`mark_reviewed`：复习一题 → reappear_count+1、last_reappear_at=今日。
🔴 语义：复习是"驯服努力"——**不直接置 is_tamed**（驯服 = 同类错误
连续 3 周下降，MS-07），也不自动 resolved（解决需独立重做验证）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime

from fdl_core.srs.time_layer import LOCAL_TZ, fmt_ts, local_date, now_utc, to_utc

logger = logging.getLogger("fdl.mistakes.review")

# === A3 方案：Leitner 固定阶梯 + 评分调节（2026-09-08 实施）===
# 学术依据：
#   - Leitner (1972) 盒子：答对进阶、答错回退
#   - 背单词花园（K12 商业标杆）固定阶梯 1/2/4/7/15
#   - Wilson et al. 2019 Nature Comms「85% 法则」（隐含于 grade 档位）
#   - Barzagar Nazari & Ebersbach 2019：三年级间隔效应 6 周衰减 → 阶梯上限 60 天
# 优势：零依赖冷启动（第 1 次即 1 天）、K12 节奏验证、可解释、15 行代码
A3_STEP = [1, 2, 4, 7, 15, 21]  # 固定阶梯（天，上限从 60→21 · Barzagar 2019 四年级适配）
A3_MULT = {4: 1.5, 3: 1.0, 2: 0.5, 1: 0}  # 评分乘数：4熟练/3掌握/2模糊/1陌生
A3_MAX_DAYS = 365  # 硬上限

# 每日复习上限（借鉴 Anki 每日上限 + 百词斩"总量固定"实验结论）
# 百词斩测试几百名大学生后确定：每天新学+复习总量固定，是最能坚持下去的方式
DAILY_REVIEW_CAP = 15  # 每日到期复习卡上限
DAILY_NEW_CAP = 5  # 每日新学卡上限

# === A2 自动升级开关（2026-09-08）===
# 默认 A3；满足 docs/research/SRS-算法选型-A1A2A3.md §三-A3 末尾的 4 条件时，
# run_a2_upgrade_check() 检测到 ready=True 后切换为 A2，下次 mark_reviewed 自动走 A2。
# 单向切换（A3 → A2），不回退；可手动 rollback_to_a3() 强制回 A3。
CURRENT_SCHEDULER = "A3"  # 全局状态；运行时由 run_a2_upgrade_check 更新


def current_scheduler() -> str:
    """读取当前调度器（A3/A2）。"""
    return CURRENT_SCHEDULER


def switch_to_a2() -> None:
    """切换到 A2（FSRS）。仅可单向：不会自动回 A3。"""
    global CURRENT_SCHEDULER
    if CURRENT_SCHEDULER != "A2":
        CURRENT_SCHEDULER = "A2"


def _persist_scheduler(conn: sqlite3.Connection, value: str) -> None:
    """把调度器选择持久化到 report_meta（key=scheduler_current）。

    🔴 为什么必须持久化（2026-09-12 排障结论）：CURRENT_SCHEDULER 是**进程内**
    全局变量，而真正执行 mark_reviewed 的是 fdl_serve 进程。原实现里
    daily_batch 调 run_a2_upgrade_check 只 flip 自己进程的内存值，随进程退出
    即消失——fdl_serve 永远停在 A3，"自动升级"跨进程根本不生效。
    持久化后各进程经 _effective_scheduler() 拉齐。失败绝不抛（辅助能力）。
    """
    try:
        conn.execute(
            "INSERT INTO report_meta (key, status, value, updated_at)"
            " VALUES ('scheduler_current', 'ACTIVE', ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (value, fmt_ts(now_utc())),
        )
        conn.commit()
    except Exception:
        logging.getLogger("fdl.mistakes.review").warning(
            "调度器状态持久化失败（不影响本次复习）", exc_info=True
        )


def _effective_scheduler(conn: sqlite3.Connection) -> str:
    """复习调度分发用的"生效调度器"：内存已是 A2 直接用（单向不回退）；
    否则查持久化值（其他进程可能已升级）。一次 SELECT，复习频次下可忽略。
    """
    global CURRENT_SCHEDULER
    if CURRENT_SCHEDULER == "A2":
        return "A2"
    try:
        row = conn.execute("SELECT value FROM report_meta WHERE key='scheduler_current'").fetchone()
        if row and str(row[0]).upper() == "A2":
            CURRENT_SCHEDULER = "A2"
    except Exception:
        pass  # 表缺失/库异常 → 保持内存值（A3），绝不让升级检查拖垮复习
    return CURRENT_SCHEDULER


def rollback_to_a3(conn: sqlite3.Connection | None = None) -> None:
    """手动强制回 A3（应急用，不应作为正常路径）。

    conn 给定时同步清除持久化值——否则下一个进程会被 _effective_scheduler
    重新拉回 A2，回滚就成了假动作。
    """
    global CURRENT_SCHEDULER
    CURRENT_SCHEDULER = "A3"
    if conn is not None:
        _persist_scheduler(conn, "A3")


def run_a2_upgrade_check(conn: sqlite3.Connection) -> dict:
    """每日调度器升级检查（应每日 04:00 批处理时调用）。

    返回 dict：
      - ready (bool): 是否满足 4 项条件
      - switched (bool): 本次调用是否触发切换
      - result (dict): UpgradeCheckResult 详情
      - scheduler (str): 当前调度器

    满足条件则自动切换（switched=True）；否则保持 A3。
    """
    from fdl_core.srs.a2_upgrade import should_upgrade_to_a2

    global CURRENT_SCHEDULER
    check = should_upgrade_to_a2(conn)
    switched = False
    if check.ready and CURRENT_SCHEDULER != "A2":
        switch_to_a2()
        switched = True
        # 🔴 2026-09-12 排障实锤：必须持久化。CURRENT_SCHEDULER 是进程内全局，
        # daily_batch 里 flip 完随进程退出即消失，fdl_serve（真正执行复习调度的
        # 进程）永远停在 A3——不持久化的"自动升级"是跨进程假动作。
        _persist_scheduler(conn, "A2")
    return {
        "ready": check.ready,
        "switched": switched,
        "result": check.to_dict(),
        "scheduler": CURRENT_SCHEDULER,
    }


def a3_next_interval(reappear_count: int, grade: int) -> int:
    """A3：按复习次数 + 本次评分算下次间隔（天）。

    参数：
        reappear_count: 本次复习**之后**的累计复习次数（≥1）
        grade: 1=陌生 / 2=模糊 / 3=掌握 / 4=熟练

    规则：
        grade=1（陌生）→ 回退到 1 天，阶梯重置（reappear_count 由调用方归零）
        grade≥2        → interval = round(STEP[min(n-1, len-1)] × MULT[grade])

    返回：间隔天数（≥1，≤365）
    """
    if grade == 1:
        return 1  # 陌生：1 天后重来（温和——不像 SM-2 那样"归零重来"打击信心）
    n = max(reappear_count, 1)
    base = A3_STEP[min(n - 1, len(A3_STEP) - 1)]
    interval = round(base * A3_MULT.get(grade, 1.0))
    return max(1, min(interval, A3_MAX_DAYS))


# === A2 FSRS 计算（lazy import 避免循环）===


def _a2_compute_interval(
    conn: sqlite3.Connection,
    mid: int,
    reappear_count: int,
    grade: int,
) -> int:
    """A2 FSRS：根据上次复习状态 + 本次评分算下次间隔（天）。

    实现要点：
        1. 前 3 次复习走 A3 阶梯护栏（FSRS_LEITNER_GUARD）——避免冷启动抖动
        2. 第 4 次起调 fsrs_update 算新 S，fsrs_next_interval 算间隔
        3. mistake_record 需有 fsrs_s / fsrs_d 列持久化状态（缺则用初始值）

    返回：间隔天数（≥1，≤365）
    """
    # 顶部 import（避免函数内 import 触发 UnboundLocalError）
    from fdl_core.srs.a2_upgrade import (
        FSRS_LEITNER_GUARD,
        fsrs_init_d,
        fsrs_init_s,
        fsrs_next_interval,
        fsrs_r,
        fsrs_update,
    )

    # 前 3 次护栏（FSRS 官方推荐：<1000 条复习不要训练）
    if reappear_count <= FSRS_LEITNER_GUARD:
        # 前 3 次走 A3 固定阶梯护栏：grade 是 FDL 4 档，直接用于 a3_next_interval
        # （a3_next_interval 语义与 FDL 档位一致：grade=1 陌生→1 天重置，grade≥2 按倍率），
        # 此处**不走** FSRS 量表映射——FSRS 映射仅用于第 4 次起的 fsrs_init_d / fsrs_update。
        return a3_next_interval(reappear_count, grade)

    # 4 次以上进入完整 FSRS 计算

    # 读 S/D（无则初始化）
    row = conn.execute(
        "SELECT fsrs_s, fsrs_d FROM mistake_record WHERE id=?",
        (mid,),
    ).fetchone()
    if not row:
        return 1
    s = row[0] or fsrs_init_s(grade)
    d = row[1] or fsrs_init_d(grade)

    # 算调用时刻的 R(t)：上次复习时间从 kp_state.last_review_at 取
    # kp_state 由 post_answer_reschedule 维护；mistake_record.last_reappear_at
    # 也可作 fallback（用户体验视角）
    kp_id_row = conn.execute("SELECT kp_id FROM mistake_record WHERE id=?", (mid,)).fetchone()
    last_review = None
    if kp_id_row and kp_id_row[0]:
        kr = conn.execute(
            "SELECT last_review_at FROM kp_state WHERE user_id=1 AND kp_id=?",
            (kp_id_row[0],),
        ).fetchone()
        if kr and kr[0]:
            last_review = kr[0]
    if not last_review:
        # fallback：用 last_reappear_at 或 updated_at
        lr2 = conn.execute(
            "SELECT last_reappear_at, updated_at FROM mistake_record WHERE id=?",
            (mid,),
        ).fetchone()
        last_review = lr2[0] if lr2 and lr2[0] else (lr2[1] if lr2 else None)

    # 算调用时刻的 R(t)
    import datetime as dt

    if last_review:
        try:
            last_dt = dt.datetime.fromisoformat(last_review.replace("Z", "+00:00"))
            elapsed = (dt.datetime.now(dt.UTC) - last_dt).total_seconds() / 86400.0
        except Exception:
            elapsed = 1.0
    else:
        elapsed = 1.0
    r = fsrs_r(elapsed, s)

    # 单步更新
    new_s, new_d = fsrs_update(s, d, r, grade, elapsed)
    interval = fsrs_next_interval(new_s)

    # 持久化（mistake_record 自身的 fsrs_s/fsrs_d）
    conn.execute(
        "UPDATE mistake_record SET fsrs_s=?, fsrs_d=?, updated_at=? WHERE id=?",
        (new_s, new_d, fmt_ts(now_utc()), mid),
    )
    return max(1, min(int(round(interval)), A3_MAX_DAYS))


def mark_reviewed(
    conn: sqlite3.Connection,
    mistake_ids: list[int],
    *,
    day: str | None = None,
    grade: int = 3,
    duration_seconds: int | None = None,
    note: str | None = None,
    reschedule: bool = True,
) -> list[int]:
    """标记错题今日已复习（reappear_count+1、last_reappear_at=今日），并按 A3 排下次复习。

    A3 闭环（2026-09-08）：复习完成 → 旧 PENDING 计划标 DONE → 按阶梯+评分算
    下次间隔 → 写新 PENDING 计划（due_date = 今天 + 间隔）。这样复习队列才会
    "滚动散开到未来日期"，而非全部堆积在今天。

    参数：
        day:        指定复习日（默认本地日期）
        grade:      本次掌握度 1=陌生 / 2=模糊 / 3=掌握 / 4=熟练（默认 3）
        reschedule: 是否自动排下次复习（默认 True；False 则只标记不重排）

    幂等：同一题当天重复调用只计一次（last_reappear_at 已是今天则跳过）。
    返回实际更新的 id 列表。
    """
    # day 默认本地日期（与 classify 一致），保证跨 UTC 边界幂等判断也正确
    today = day or local_date().isoformat()
    stamp = fmt_ts(now_utc())
    updated = []
    for mid in mistake_ids:
        row = conn.execute(
            "SELECT last_reappear_at FROM mistake_record WHERE id=?", (mid,)
        ).fetchone()
        if row is None:
            continue
        # 幂等判断：UTC last_reappear_at 转本地日期后与 today 比较
        if row[0]:
            lr_local = (
                to_utc(datetime.fromisoformat(row[0].replace("Z", "+00:00")))
                .astimezone(LOCAL_TZ)
                .date()
                .isoformat()
            )
            if lr_local == today:
                continue  # 今天已标记——幂等
        conn.execute(
            "UPDATE mistake_record SET reappear_count = reappear_count + 1,"
            " last_reappear_at = ?, updated_at = ? WHERE id=?",
            (stamp, stamp, mid),
        )
        updated.append(mid)

        # === OpenMAIC 借鉴 B（2026-09-09）：接通 review_feedback + 首次答错触发三层归因 ===
        # 1. 每次复习（无论 grade）写入 review_feedback（保持 D+1/7/21 三关节奏）
        # 2. grade=1（陌生）= 首次答错 → 触发三层归因干预动作
        # mistake_record 没有 subject_id 列（subject 是 TEXT 而非 FK）——用 JOIN 取
        m_row = conn.execute(
            "SELECT m.kp_id, (SELECT s.id FROM subject s WHERE s.code = m.subject LIMIT 1)"
            " FROM mistake_record m WHERE m.id=?",
            (mid,),
        ).fetchone()
        # kp_id=0（未挂载知识点的旧数据）→ NULL，避免 FK 违规
        kp_id = m_row[0] if (m_row and m_row[0] and m_row[0] != 0) else None
        subject_id = m_row[1] if (m_row and m_row[1]) else 1
        # 写 review_feedback（每次复习都记）
        sc_row = conn.execute(
            "SELECT id, planned_interval_days FROM review_schedule "
            "WHERE mistake_id=? AND status='PENDING' ORDER BY due_date LIMIT 1",
            (mid,),
        ).fetchone()
        schedule_id = sc_row[0] if sc_row else None
        srs_interval = sc_row[1] if sc_row else None
        feedback_id = record_review_feedback(
            conn,
            user_id=1,
            schedule_id=schedule_id,
            kp_id=kp_id,
            subject_id=subject_id,
            self_rating=grade,
            duration_seconds=duration_seconds,
            note=note,
            srs_interval_days=srs_interval,
        )

        # === 2026-09-12 LLM 归因钩子（King 拍板：每次复习反馈都触发错因分析）===
        # 异步后台线程执行（自带独立连接），绝不阻塞、绝不影响复习主流程。
        # record_review_feedback 内部已 commit，子线程新连接能看到这条反馈。
        # 闸门（高置信自动 / 低置信转人工）与审计链见 attribution_engine.py。
        try:
            from fdl_core.mistakes.attribution_engine import schedule_async_analysis

            schedule_async_analysis(conn, mistake_id=mid, feedback_id=feedback_id)
        except Exception:
            logger.warning("归因分析调度失败（不影响复习主流程）mid=%s", mid, exc_info=True)

        # === 2026-09-13 反馈层闭环方案 B 钩子（King 拍板：每次反馈 → 个性化干预）===
        # 与上面归因钩子同款：异步后台线程（自带独立连接），绝不阻塞、绝不抛出；
        # 这是唯一钩子点——ASR / 表单 / 端点路径都经 mark_reviewed。
        try:
            from fdl_core.mistakes.feedback_loop import schedule_async_feedback_loop

            schedule_async_feedback_loop(conn, mistake_id=mid, feedback_id=feedback_id)
        except Exception:
            logger.warning("反馈闭环分析调度失败（不影响复习主流程）mid=%s", mid, exc_info=True)

        # grade=1（陌生）= 首次答错 → 触发三层归因干预动作
        if grade == 1:
            trigger_first_wrong_attribution(
                conn,
                mistake_id=mid,
                kp_id=kp_id,
                subject_id=subject_id,
                user_id=1,
            )

        # === A3 / A2 排下次复习（按当前调度器分发）===
        if not reschedule:
            continue
        # 读取复习后的 reappear_count
        rc_row = conn.execute(
            "SELECT reappear_count FROM mistake_record WHERE id=?", (mid,)
        ).fetchone()
        rc = rc_row[0] if rc_row else 1
        # 答错时阶梯重置（grade=1 在 a3_next_interval 与 a2_update 都重置）
        if grade == 1:
            conn.execute("UPDATE mistake_record SET reappear_count = 0 WHERE id=?", (mid,))
            rc = 0
        # 按生效调度器选算法（2026-09-12：读持久化状态——A2 升级可能由
        # 每日批处理进程做出，本进程内存未必同步）
        if _effective_scheduler(conn) == "A2":
            interval = _a2_compute_interval(conn, mid, rc, grade)
        else:
            interval = a3_next_interval(rc, grade)
        # 旧 PENDING 计划作废（按 mistake_id，避免误伤同 kp 的其他卡）
        conn.execute(
            "UPDATE review_schedule SET status='DONE', updated_at=?"
            " WHERE mistake_id=? AND status='PENDING'",
            (stamp, mid),
        )
        # 写新计划（due_date 用整数天相加，避免 timedelta 取整塌缩）
        # planned_interval_days 显式 = due_date 与今日之差的天数，保证报告"下次间隔 N 天"
        # 展示口径与 due_date 严格一致（不复用 interval 变量，防未来重构错位）。
        planned_days = int(interval)
        due = (
            datetime.fromisoformat(today).date()
            + __import__("datetime").timedelta(days=planned_days)
        ).isoformat()
        conn.execute(
            "INSERT INTO review_schedule (user_id, kp_id, mistake_id, subject_id,"
            " due_date, due_session, planned_interval_days, interval_fuzz_applied,"
            " status, source, priority_score, est_seconds, created_at, updated_at)"
            " SELECT 1, NULLIF(m.kp_id, 0), m.id,"  # kp_id=0 → NULL（避免 FK 违规，未挂载 KP）
            " COALESCE((SELECT s.id FROM subject s WHERE s.code = m.subject LIMIT 1), 1),"
            " ?, 'PM', ?, NULL, 'PENDING', 'answer',"
            " 90.0, 120, ?, ? FROM mistake_record m WHERE m.id=?",
            (due, planned_days, stamp, stamp, mid),
        )

        # === P2-1 毕业归档（记忆层净化）：复习达标即自动退出活跃池 ===
        # 置 resolved_at + is_tamed 后，display_filter 会自动把它从"今日复习"过滤掉，
        # 无需任何前端改动。此处为"静默增强"——任何异常都不应拖累复习主流程。
        try:
            from fdl_core.srs.graduation import check_graduation

            check_graduation(conn, [mid])
        except Exception:
            # 毕业检测是辅助能力：失败绝不影响本次复习回写
            import logging

            logging.getLogger("fdl.srs.graduation").warning(
                "毕业检测失败（不影响复习主流程）mid=%s", mid, exc_info=True
            )
    conn.commit()
    return updated


# ── 自动状态判定（从 DB 字段推导，无需人工告知）─────────────
# 判定依据（mistake_record 字段）：
#   resolved_at 非空                    → 已处理（独立重做验证通过）
#   last_reappear_at = 今天 且未解决    → 今日已复习
#   last_reappear_at 非空（历史日期）   → 已复习（历史）
#   其余（is_tamed=0）                  → 待复习
STATUS_RESOLVED = "已解决"
STATUS_REVIEWED_TODAY = "今日已复习"
STATUS_REVIEWED = "已复习"
STATUS_PENDING = "待复习"


def classify_status(row: dict, today: str) -> str:
    """单条错题的状态判定（按上表规则，顺序即优先级）。

    `today` 应为本地日期（YYYY-MM-DD）——直接由 collect_metrics 传入 local_date().isoformat()。
    `last_reappear_at` 存库为 UTC ISO 字符串——必须**先转本地日期**再与 today 比较，
    否则跨 UTC 边界时会少算/多算"今日已复习"。
    """
    if row.get("resolved_at"):
        return STATUS_RESOLVED
    lr = row.get("last_reappear_at")
    if lr:
        # UTC → 本地（Asia/Shanghai）日期：YYYY-MM-DD
        try:
            from datetime import datetime

            from fdl_core.srs.time_layer import LOCAL_TZ, to_utc

            dt = to_utc(datetime.fromisoformat(lr.replace("Z", "+00:00")))
            lr_local_date = dt.astimezone(LOCAL_TZ).date().isoformat()
        except Exception:
            # 解析彻底失败：降级到本地今天，而非用 UTC 切片 lr[:10]
            # （后者跨 UTC 边界会把"昨日 UTC 存的历史时间"误判为今/昨，破坏幂等）。
            lr_local_date = local_date().isoformat()
        if lr_local_date == today:
            return STATUS_REVIEWED_TODAY
        return STATUS_REVIEWED
    return STATUS_PENDING


def classify_all(conn: sqlite3.Connection, *, today: str | None = None) -> dict:
    """全量错题状态自动识别 → 分类清单 + 汇总（图鉴/复习入口共用）。

    today 默认 **本地日期**（Asia/Shanghai），不再是 UTC 切片——
    保证跨 UTC 边界（下午 4 点后）也能正确判定"今日已复习"。
    """
    today = today or local_date().isoformat()
    rows = [
        dict(
            zip(
                (
                    "id",
                    "occurred_at",
                    "error_type",
                    "resolved_at",
                    "last_reappear_at",
                    "source_ref",
                    "img_original",
                ),
                r,
                strict=False,
            )
        )
        for r in conn.execute(
            "SELECT id, occurred_at, error_type, resolved_at, last_reappear_at,"
            " source_ref, img_original FROM mistake_record ORDER BY occurred_at"
        )
    ]
    for r in rows:
        r["status"] = classify_status(r, today)
    summary = Counter(r["status"] for r in rows)
    return {
        "items": rows,
        "summary": {
            STATUS_RESOLVED: summary.get(STATUS_RESOLVED, 0),
            STATUS_REVIEWED_TODAY: summary.get(STATUS_REVIEWED_TODAY, 0),
            STATUS_REVIEWED: summary.get(STATUS_REVIEWED, 0),
            STATUS_PENDING: summary.get(STATUS_PENDING, 0),
        },
        "pending_ids": [r["id"] for r in rows if r["status"] == STATUS_PENDING],
    }


def create_initial_review_schedule(
    conn: sqlite3.Connection,
    mistake_ids: list[int],
    *,
    base_day: str | None = None,
    interval_days: int = 1,
) -> int:
    """错题录入时创建**首次复习计划**（订正复习：次日档，BOOTSTRAP_I[1]=1 天）。

    闭环补全：post_answer_reschedule 只在作答后触发；错题入库（ING）时
    由本函数落首条 review_schedule（due=录入次日），之后进入 SRS 循环。
    """
    import datetime as dt

    base = base_day or local_date().isoformat()
    due = (dt.date.fromisoformat(base) + dt.timedelta(days=interval_days)).isoformat()
    n = 0
    for mid in mistake_ids:
        # A3：调度单元改 mistake_id（原 kp_id 借用 mistake id，语义错乱）
        # 存在性检查与作废都按 mistake_id 定位，避免同 kp 下所有计划被一次作废
        exists = conn.execute(
            "SELECT 1 FROM review_schedule WHERE status='PENDING' AND mistake_id=?",
            (mid,),
        ).fetchone()
        if exists:
            continue
        stamp = fmt_ts(now_utc())
        # subject_id：mistake_record.kp_id 全为 0（知识点未挂载），
        # 改为从 subject 表取真实学科 ID（按 mistake.subject 字段匹配 code，兜底 1）
        conn.execute(
            "INSERT INTO review_schedule (user_id, kp_id, mistake_id, subject_id,"
            " due_date, due_session, planned_interval_days, interval_fuzz_applied,"
            " status, source, priority_score, est_seconds, created_at, updated_at)"
            " SELECT 1, NULLIF(m.kp_id, 0), m.id,"
            " COALESCE((SELECT s.id FROM subject s WHERE s.code = m.subject LIMIT 1), 1),"
            " ?, 'PM', ?, NULL, 'PENDING', 'ingest',"
            " 90.0, 120, ?, ? FROM mistake_record m WHERE m.id=?"
            # 2026-09-12 修复：已 resolved（已解决/已毕业）的错题不再补建复习计划。
            # 根因：原实现只查"是否已有 PENDING 计划"，未排除已解决的错题，
            # 导致已 resolved 的 #77001 在补建迁移中被建了计划、并进入复习队列
            # （净化工具 status_conflict 由此告警）。
            " AND m.resolved_at IS NULL",
            (due, interval_days, stamp, stamp, mid),
        )
        n += 1
    conn.commit()
    return n


def stagger_pending_schedules(
    conn: sqlite3.Connection,
    *,
    day: str | None = None,
    cap: int | None = None,
) -> dict:
    """A3 存量错峰：把堆积在同一天的 PENDING 计划按每日上限散开到未来日期。

    问题：所有计划 due_date 相同（历史播种 + timedelta 取整塌缩）→ 用户每天
    看到几十条"逾期补做"，体验是"补旧账"而非"按曲线推进"。

    策略：
      1. 取出全部 PENDING 计划，按 (reappear_count ASC, id ASC) 排序
         ——复习次数少的排前面（最该先复习）
      2. 第 i 条的到期日 = base + (i // cap) 天
         ——每天最多 cap 条（默认 DAILY_REVIEW_CAP=15）
      3. 更新 due_date + planned_interval_days

    返回：{total, cap, days_span, spread}（spread = {日期: 条数}）
    """
    import collections
    import datetime as dt

    today = day or local_date().isoformat()
    cap = cap or DAILY_REVIEW_CAP
    rows = conn.execute(
        "SELECT rs.id, COALESCE(m.reappear_count, 0) AS rc"
        " FROM review_schedule rs"
        " LEFT JOIN mistake_record m ON m.id = rs.mistake_id"
        " WHERE rs.status='PENDING'"
        " ORDER BY rc ASC, rs.id ASC"
    ).fetchall()
    if not rows:
        return {"total": 0, "cap": cap, "days_span": 0, "spread": {}}

    base = dt.date.fromisoformat(today)
    stamp = fmt_ts(now_utc())
    spread: dict[str, int] = collections.Counter()
    for i, (sid, _rc) in enumerate(rows):
        offset = i // cap
        due = (base + dt.timedelta(days=int(offset))).isoformat()
        # 第一批 offset=0 落在"今天"（历史逾期卡，立即可复习）；
        # planned_interval_days 至少记 1（A3 首档），避免 0 值语义歧义
        conn.execute(
            "UPDATE review_schedule SET due_date=?, planned_interval_days=?,"
            " updated_at=? WHERE id=?",
            (due, max(1, int(offset)), stamp, sid),
        )
        spread[due] += 1
    conn.commit()
    return {
        "total": len(rows),
        "cap": cap,
        "days_span": len(spread),
        "spread": dict(sorted(spread.items())),
    }


# ── OpenMAIC 借鉴 B：复习反馈写入 + 首次答错三层归因（2026-09-09）──────────
# 设计：复习反馈闭环（review_feedback）+ 错因诊断干预（intervention_action）。
# 这两块是**新增独立入口**，不改动 mark_reviewed 既有 SRS 闭环行为。


def record_review_feedback(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    schedule_id: int | None,
    kp_id: int,
    subject_id: int,
    self_rating: int,
    duration_seconds: int | None = None,
    note: str | None = None,
    attachments_json: str | None = None,
    srs_interval_days: float | None = None,
) -> int:
    """写入一条复习反馈（review_feedback 表）。

    供复习页/驾驶舱追潮与错题迭代；与 review_schedule 解耦（schedule 删除后保留）。
    返回新建反馈 id。
    """
    cur = conn.execute(
        "INSERT INTO review_feedback "
        "(user_id, schedule_id, kp_id, subject_id, self_rating, duration_seconds, "
        " note, attachments_json, srs_interval_days) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            user_id,
            schedule_id,
            kp_id,
            subject_id,
            self_rating,
            duration_seconds,
            note,
            attachments_json,
            srs_interval_days,
        ),
    )
    conn.commit()
    return cur.lastrowid


def trigger_first_wrong_attribution(
    conn: sqlite3.Connection,
    *,
    mistake_id: int,
    kp_id: int,
    subject_id: int,
    user_id: int = 1,
) -> int | None:
    """首次答错触发三层归因诊断 → 落一条 intervention_action（trigger=FIRST_WRONG）。

    三层归因（错因诊断闭环）：
      - layer1 表象(symptom)：错题自身的 wrong_answer / correct_answer
      - layer2 错因类型(error_type)：mistake_record.error_type / error_subtype
      - layer3 根因(root_cause)：mistake_record.root_cause_kp_id（指向根因知识点）

    幂等：同一 mistake 已存在未 DONE 的 FIRST_WRONG 干预则跳过，避免重复派单。
    返回新建 intervention_action 的 id；已存在则返回 None。
    """
    row = conn.execute(
        "SELECT error_type, error_subtype, root_cause_kp_id, wrong_answer, correct_answer "
        "FROM mistake_record WHERE id=?",
        (mistake_id,),
    ).fetchone()
    if row is None:
        return None
    error_type, error_subtype, root_cause_kp_id, wrong, correct = row

    exists = conn.execute(
        "SELECT 1 FROM intervention_action "
        "WHERE mistake_id=? AND trigger='FIRST_WRONG' AND status<>'DONE'",
        (mistake_id,),
    ).fetchone()
    if exists:
        return None

    payload = {
        "layer1_symptom": {"wrong_answer": wrong, "correct_answer": correct},
        "layer2_error_type": {
            "error_type": error_type,
            "error_subtype": error_subtype,
        },
        "layer3_root_cause": {"root_cause_kp_id": root_cause_kp_id},
    }
    cur = conn.execute(
        "INSERT INTO intervention_action "
        "(user_id, mistake_id, kp_id, subject_id, trigger, action_type, payload_json, status) "
        "VALUES (?,?,?,?,'FIRST_WRONG','DIAGNOSE',?,'PENDING')",
        (user_id, mistake_id, kp_id, subject_id, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    return cur.lastrowid
