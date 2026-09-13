"""间隔驱动的复习队列动态调度器（SRS 复苏机制）。

实现 King 三条硬性要求：
  1) 依据记忆曲线规律，为每个问题分别按天独立计时复习间隔；
  2) 将"复习间隔时间"作为排列与处置的核心依据；
  3) 用于动态调整问题的复习优先级和调度顺序。

设计要点（详见各函数 docstring）：
- 计时起点 start_at 独立：已复习过的题从 last_reappear_at 起算；
  从未复习的题从 review_schedule.created_at 的日期部分起算（绝不 NULL）。
- 紧迫度 urgency = overdue_days / interval_days：
  同样逾期 3 天，间隔 1 天的题 urgency=3.0（记忆痕迹浅、遗忘快）远比
  间隔 15 天的题 urgency=0.2 紧急——这正是"以间隔为核心依据"的体现。
- 综合优先级 priority_score = urgency + 启动加成(0.5) + 错因权重。
- 复苏 revive_and_schedule 替代旧的静态 stagger_pending_schedules：
  按优先级降序铺开到未来日期，每日上限 daily_cap（首周 King 定 8 条）。

时区：一律用 fdl_core.srs.time_layer（本地 Asia/Shanghai 每日分界）。
禁止 datetime.date.today() / 对 UTC 串 s[:10] 切片。
数据库连接由调用方传入（conn），本模块不自行开库。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

# 归因权重改为 taxonomy 驱动（2026-09-12 King 拍板：类别可演进，不再硬编码）。
# load_weights 内部自带降级：表不存在 → 返回与 DIAG_WEIGHT 等值的内置默认。
from fdl_core.mistakes import attribution_taxonomy as _taxonomy
from fdl_core.mistakes.review import DAILY_REVIEW_CAP
from fdl_core.srs.time_layer import fmt_ts, local_date, now_utc, parse_ts

logger = logging.getLogger(__name__)

# 错因权重：概念不清(CONCEPT)最该优先——不堵会衍生新错；
# 粗心(CARELESS)权重最低。用于综合优先级的同间隔/同逾期加成。
DIAG_WEIGHT = {
    "CONCEPT": 0.3,
    "MISREAD": 0.2,
    "CALC": 0.1,
    "NORM": 0.1,
    "CARELESS": 0.05,
    "OTHER": 0.0,
}

# 2026-09-12 归因类别动态化：本常量降级为「taxonomy 不可用时的兜底默认」。
# 真正的权重来源是 attribution_taxonomy 表（可人工调权 / LLM 提候选晋级），
# compute_states 每次经 _taxonomy.load_weights(conn) 读取。
# 保留模块级符号是因为 tests/test_queue_scheduler.py 与既有调用方 import 它。
DIAG_WEIGHT: dict[str, float] = {
    "CONCEPT": 0.3,
    "MISREAD": 0.2,
    "CALC": 0.1,
    "NORM": 0.1,
    "CARELESS": 0.05,
    "OTHER": 0.0,
}

# 复苏铺开每日默认上限（King 首周定 8-10 条，取 8；与 DAILY_REVIEW_CAP=15 区分）。
REVIVE_DAILY_CAP = 8


@dataclass
class QueueItem:
    schedule_id: int
    mistake_id: int | None
    interval_days: int  # 当前间隔天数（来自 planned_interval_days，至少 1）
    start_at: str | None  # 计时起点：last_reappear_at（已复习）或 created_at（未复习）
    due_date: str  # 到期日
    overdue_days: int  # today - due_date，正数=逾期，负数=未到期
    urgency: float  # 紧迫度（核心指标，见算法定义）
    is_startup: bool  # True = 从未复习过（复苏对象）
    priority_score: float  # 综合优先级
    diag_type: str  # 错因（CONCEPT/CALC/MISREAD/NORM/CARELESS/OTHER/None）
    source_ref: str  # 题面来源（诊断用，可空串）

    # 派生展示字段（置于末尾带默认值，满足 dataclass 字段顺序约束）
    interval_label: str = ""


def _as_local_date(s: str | None) -> str | None:
    """任意存库时间/日期串 → 本地(Asia/Shanghai)日期串(YYYY-MM-DD)。

    为什么：本地时区是每日分界唯一口径（Frank 在深圳）。统一走
    time_layer.parse_ts 解析，避免直接对 UTC 串 s[:10] 切片踩时区坑。
    """
    if not s:
        return None
    s = s.strip()
    if "T" in s or " " in s:
        # 含时间部分：当 UTC 时间戳解析到本地日期
        return local_date(parse_ts(s)).isoformat()
    return dt.date.fromisoformat(s).isoformat()


def _parse_date(s: str) -> dt.date:
    """YYYY-MM-DD 串 → date（ISO 字典序 == 日期序，比较安全）。"""
    return dt.date.fromisoformat(s)


def compute_states(conn, today: str | None = None) -> list[QueueItem]:
    """计算全部 PENDING 复习计划的间隔驱动状态。

    要求 1（独立计时）：start_at 取 last_reappear_at（已复习）或 created_at 日期部分。
    要求 2（间隔驱动紧迫度）：urgency = overdue_days / interval_days。
    要求 3（综合优先级）：priority_score = urgency + 启动加成 + 错因权重。
    排序键（降序）：(priority_score, overdue_days, -schedule_id)。
    """
    today_str = today or local_date().isoformat()
    today_d = _parse_date(today_str)

    rows = conn.execute(
        """
        SELECT rs.id, rs.mistake_id, rs.due_date, rs.planned_interval_days,
               rs.created_at, rs.source,
               m.last_reappear_at, m.diagnosis_type, m.source_ref
        FROM review_schedule rs
        LEFT JOIN mistake_record m ON m.id = rs.mistake_id
        WHERE rs.status = 'PENDING'
          -- 2026-09-12 修复：排除"错题已 resolved"的僵尸计划。
          -- 根因：create_initial_review_schedule / 补建迁移都没排除已解决错题，
          -- 导致已 resolved 的 #77001 也拿到 PENDING 计划并进入队列
          -- （净化工具 status_conflict 由此从 0 变 1）。
          -- 已毕业的错题不应再占用复习队列名额。
          AND (m.id IS NULL OR m.resolved_at IS NULL)
        """
    ).fetchall()

    items: list[QueueItem] = []
    mismatch = 0
    # 权重来源：attribution_taxonomy 表（可人工调权 / 候选晋级）。
    # 每次调用读一次（内部带签名缓存）；表不可用时自动回落到与 DIAG_WEIGHT 等值的默认。
    weights = _taxonomy.load_weights(conn)
    for r in rows:
        try:
            (
                sid,
                mistake_id,
                due_date,
                planned_interval_days,
                created_at,
                _source,
                last_reappear_at,
                diagnosis_type,
                source_ref,
            ) = r

            # 间隔至少 1 天（A3 首档语义，避免 0 值歧义）
            interval_days = max(1, int(planned_interval_days or 1))

            # 要求 1：独立计时起点
            if last_reappear_at:
                start_at = _as_local_date(last_reappear_at)
                is_startup = False
            else:
                # 从未复习：起点取 created_at 日期部分（保证非 NULL）
                start_at = _as_local_date(created_at) or today_str
                is_startup = True

            # 到期日以 review_schedule.due_date 为准；校验是否与 start+interval 一致
            due_d = _parse_date(due_date)
            expected_due = _parse_date(start_at) + dt.timedelta(days=interval_days)
            if expected_due != due_d:
                mismatch += 1

            # 要求 2：以间隔为核心依据的紧迫度
            # 同样逾期 3 天：间隔 1 天 urgency=3.0 >> 间隔 15 天 urgency=0.2
            overdue_days = (today_d - due_d).days
            urgency = overdue_days / max(1, interval_days)

            # 要求 3：综合优先级（权重走 taxonomy，类别可演进）
            diag_type = diagnosis_type or "OTHER"
            priority_score = (
                urgency
                + (0.5 if is_startup else 0.0)  # 从未复习优先（状态未知，先诊断）
                + weights.get(diag_type, 0.0)  # 概念不清优先（不堵会衍生新错）
            )

            label = f"间隔 {interval_days} 天 · 已逾期 {overdue_days} 天 · 紧迫度 {urgency:.2f}"

            items.append(
                QueueItem(
                    schedule_id=sid,
                    mistake_id=mistake_id,
                    interval_days=interval_days,
                    start_at=start_at,
                    due_date=due_date,
                    overdue_days=overdue_days,
                    urgency=urgency,
                    is_startup=is_startup,
                    priority_score=priority_score,
                    diag_type=diag_type,
                    source_ref=source_ref or "",
                    interval_label=label,
                )
            )
        except Exception as exc:  # 单条异常不中断整体
            logger.warning("compute_states 跳过 schedule_id=%s: %s", r[0] if r else None, exc)
            continue

    if mismatch:
        # note：以 due_date 为准，仅记录不一致条数供审计
        logger.warning(
            "compute_states：%d 条 due_date 与 start_at+interval 不一致（以 due_date 为准）",
            mismatch,
        )

    # 排序键（降序）：优先级高→逾期久→id 小稳定
    items.sort(
        key=lambda it: (it.priority_score, it.overdue_days, -it.schedule_id),
        reverse=True,
    )
    return items


def build_daily_queue(conn, today: str | None = None, cap: int | None = None) -> list[QueueItem]:
    """当日到期队列：只取 PENDING 且 due_date <= today，按优先级降序截断 cap。

    未到期的（due_date > today）不进当日队列——当日只处理真正到/逾期的题。
    cap 默认 DAILY_REVIEW_CAP=15。
    """
    today_str = today or local_date().isoformat()
    cap = cap if cap is not None else DAILY_REVIEW_CAP

    items = [it for it in compute_states(conn, today_str) if it.due_date <= today_str]
    items.sort(
        key=lambda it: (it.priority_score, it.overdue_days, -it.schedule_id),
        reverse=True,
    )
    return items[:cap]


def write_back_scores(conn, items: list[QueueItem]) -> int:
    """回写 priority_score(REAL) 与 overdue_days(INTEGER) 到 review_schedule。

    这两个字段必须名副其实：priority_score 为综合优先级浮点，
    overdue_days 为整数逾期天数（写入整数，符合 INTEGER 字段）。
    返回更新行数。
    """
    stamp = fmt_ts(now_utc())  # 存库统一 UTC
    n = 0
    for it in items:
        try:
            conn.execute(
                "UPDATE review_schedule SET priority_score=?, overdue_days=?,"
                " updated_at=? WHERE id=?",
                (float(it.priority_score), int(it.overdue_days), stamp, it.schedule_id),
            )
            n += 1
        except Exception as exc:  # 单条异常不中断整体
            logger.warning("write_back_scores 跳过 schedule_id=%s: %s", it.schedule_id, exc)
            continue
    conn.commit()
    return n


def revive_and_schedule(
    conn,
    *,
    today: str | None = None,
    daily_cap: int | None = None,
    weekday_cap: int = 10,
    weekend_cap: int = 20,
    dry_run: bool = False,
) -> dict:
    """复苏铺开：替代旧的静态 stagger_pending_schedules。

    King 新容量规则（日历感知）：
    - 工作日（周一~周五，isoweekday < 6）每天 weekday_cap 条（默认 10）；
    - 周末（周六=6、周日=7，isoweekday >= 6）每天 weekend_cap 条（默认 20）。

    铺开逻辑：取全部 PENDING 计划 → compute_states（按 priority_score 降序）
    → 从 today 起**逐日**填充，每天的容量取该日对应口径，直到全部铺完。
    第 i 条按所在日期分组，new_due = 该日日期（不再用 i // cap 取整）。

    backward-compat：若显式传入 daily_cap，则**优先于**日历规则，回落到旧的
    固定每日容量语义（每天 daily_cap 条）。

    铺开时 planned_interval_days 不改（保持 A3 阶梯语义），只改 due_date。
    每次铺开后调用 write_back_scores 回写 priority/overdue。

    dry_run=True 时不写库，只返回预演结果（库内 due_date 不变）。
    返回：{total, cap, days_span, spread, caps, startup_count, top, dry_run}。
    """
    today_str = today or local_date().isoformat()
    today_d = _parse_date(today_str)

    items = compute_states(conn, today_str)
    total = len(items)
    startup_count = sum(1 for it in items if it.is_startup)

    spread: dict[str, int] = {}
    caps: dict[str, int] = {}
    stamp = fmt_ts(now_utc())

    # daily_cap 显式传入 → 固定容量（向后兼容）；否则走日历感知容量
    use_calendar = daily_cap is None

    idx = 0
    day_d = today_d
    while idx < total:
        # 该日容量：日历模式按星期几取，固定模式统一用 daily_cap
        if use_calendar:
            day_cap = weekend_cap if day_d.isoweekday() >= 6 else weekday_cap
        else:
            day_cap = daily_cap
        day_str = day_d.isoformat()
        caps[day_str] = day_cap  # 记录该日"容量上限"，便于核对

        for _ in range(day_cap):
            if idx >= total:
                break
            it = items[idx]
            new_due = day_str
            spread[new_due] = spread.get(new_due, 0) + 1

            if not dry_run:
                conn.execute(
                    "UPDATE review_schedule SET due_date=?, updated_at=? WHERE id=?",
                    (new_due, stamp, it.schedule_id),
                )
                # 注意：不重算 overdue_days/priority_score！
                # 复苏对象原本就逾期，overdue_days 应保留 compute_states 算出的
                # "真实逾期天数"（正值），priority_score 也沿用原排序键——
                # 这样回写后"今天到期"的题仍带正逾期（核对校验3要求），
                # 而 due_date 只是新的排期日。二者口径分离，互不污染。
            idx += 1

        day_d = day_d + dt.timedelta(days=1)  # 进入下一日

    if not dry_run and items:
        # 铺开后回写优先级与逾期天数
        write_back_scores(conn, items)

    days_span = len(spread)
    top = [
        {
            "schedule_id": it.schedule_id,
            "mistake_id": it.mistake_id,
            "interval_label": it.interval_label,
            "priority_score": round(it.priority_score, 2),
        }
        for it in items[:10]
    ]

    return {
        "total": total,
        # 日历模式 cap=None（无固定每日容量），固定模式回显 daily_cap（向后兼容）
        "cap": None if use_calendar else daily_cap,
        "days_span": days_span,
        "spread": dict(sorted(spread.items())),
        "caps": dict(sorted(caps.items())),
        "startup_count": startup_count,
        "top": top,
        "dry_run": dry_run,
    }
