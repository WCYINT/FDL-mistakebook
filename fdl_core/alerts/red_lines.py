"""FDL 三条红线告警（C6.3）。

占位函数——阶段四正式接入告警，阶段一仅确保触发写入 logs/alerts/ 不报错。

三条红线（§1.4、§MT-08）：
1. Frank 拒绝使用：连续 3 天拒绝 / SIR 连续 2 周 < 20% -> 立即降量
2. 时长失控：连续 5 天 > 25min 或 < 5min
3. 通过率崩塌：RPR 连续 2 周 < 70%
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from fdl_core.srs.time_layer import fmt_ts, local_date, local_date_range

RED_LINE_NAMES = ("frank_refusal", "duration_out_of_control", "pass_rate_collapse")


def _write_alert(alerts_dir: Path, name: str, payload: dict) -> Path:
    """写入一条告警到 logs/alerts/{name}-{YYYY-MM}.jsonl，返回文件路径。

    阶段一占位：仅追加写入，不触发通知，不抛异常。
    """
    alerts_dir = Path(alerts_dir)
    alerts_dir.mkdir(parents=True, exist_ok=True)
    month = datetime.now().strftime("%Y-%m")
    out = alerts_dir / f"{name}-{month}.jsonl"
    record = {"name": name, "ts": datetime.now().isoformat(), **payload}
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return out


def alert_frank_refusal(alerts_dir: Path, days_refused: int = 0, sir: float | None = None) -> Path:
    """红线 1：Frank 拒绝使用。"""
    return _write_alert(alerts_dir, "frank_refusal", {"days_refused": days_refused, "sir": sir})


def alert_duration_out_of_control(
    alerts_dir: Path, streak_days: int = 0, minutes: float | None = None
) -> Path:
    """红线 2：时长失控（> 25min 或 < 5min 连续 5 天）。"""
    return _write_alert(
        alerts_dir, "duration_out_of_control", {"streak_days": streak_days, "minutes": minutes}
    )


def alert_pass_rate_collapse(alerts_dir: Path, rpr: float | None = None) -> Path:
    """红线 3：通过率崩塌（RPR 连续 2 周 < 70%）。"""
    return _write_alert(alerts_dir, "pass_rate_collapse", {"rpr": rpr})


# ── 阶段四 MT-08：真实判定（数据驱动）────────────────────────
def evaluate_red_lines(conn, *, today: str | None = None) -> list[dict]:
    """三条红线判定（PRD §1.4 / MT-08）。返回结构化结果（供报告渲染）。

    - 红线 1 拒绝使用：连续 3 天（全 SKIPPED 或零活动），或近 2 周 SIR < 20%
      → 首选动作：立即降量（周新学 12→4，日时长 12→8 min）
    - 红线 2 时长失控：连续 5 天 effective >25min 或 <5min
      → 前者查 new_per_day 与 K；后者转红线 1
    - 红线 3 通过率崩塌：RPR 连续 2 周 <70%
      → 先查 R@R：正常(0.80–0.90)则题目太难；<0.75 则间隔太长
    数据不足时 state="数据积累中"（不做推测）。

    ⚠️ 2026-09-13 公式审计修复（docs/research/三条红线-公式审计-2026-09-13.md）：
    - F1（高）：红线1 有效练习日 <10 时不再落 "calm/未触发"（2 天样本的
      SIR 100% 会伪装成健康信号）→ 改判 "na/数据积累中"，对齐「样本不足显示—」原则；
    - F2（高）：连续 3 天"零活动"（study_session 与 ASR 口述录音合并口径）计为拒绝——
      原实现只认 daily_task 全 SKIPPED，完全停用永远不会被捕获；
    - F3（中）：红线2 时长口径由当日会话**均值**改为**总量**（对齐 config
      daily_minutes_cap/floor 的"日总时长"语义）；
    - F4（中）：红线1 拒绝日按注释语义改为"当日任务**全部** SKIPPED"（原 >0 过宽）；
    - F5（中）：会话统计补 `is_valid=1`（与驾驶舱/趋势同口径）。
    """
    d0 = dt.date.fromisoformat(today) if today else local_date()

    # ASR 口述复习分钟（与报告同源：录音是当前主要复习形态，必须计入"是否有活动"）。
    # 惰性导入 + 失败降级为 0：红线判定不能因草稿损坏而崩。
    def _asr_min_of(d: dt.date) -> float:
        try:
            from fdl_core.metrics.daily import asr_review_minutes

            return float(asr_review_minutes(d))
        except Exception:  # noqa: BLE001
            return 0.0

    # 近 14 天逐日数据（🔴 排除今天——今天未结束，0 分钟会稀释超时判定）
    days: list[dict] = []
    for i in range(14, 0, -1):
        d = (d0 - dt.timedelta(days=i)).isoformat()
        sess = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(trigger_type='SELF'),0),"
            " COALESCE(SUM(effective_sec)/60.0, 0)"
            " FROM study_session WHERE session_date=? AND is_valid=1",
            (d,),
        ).fetchone()
        tasks = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(status='SKIPPED'),0),"
            " COALESCE(SUM(status='DONE'),0) FROM daily_task WHERE task_date=?",
            (d,),
        ).fetchone()
        asr_min = _asr_min_of(dt.date.fromisoformat(d))
        days.append(
            {
                "date": d,
                "sess": sess[0],
                "self": sess[1],
                "minutes": sess[2],
                "skipped": tasks[1],
                "tasks_total": tasks[0],
                "asr_min": asr_min,
                "has_activity": bool(sess[0]) or asr_min > 0,
            }
        )

    # 近 2 周 SIR
    two_week = [x for x in days if x["sess"] > 0]
    sir_2w = (
        sum(x["self"] for x in two_week) / sum(x["sess"] for x in two_week) if two_week else None
    )
    # 有效练习日（含 ASR-only 的活动日）——样本门槛用
    active_days = [x for x in days if x["has_activity"]]

    # 近 5 天时长（总量口径，F3）
    last5 = [x["minutes"] for x in days[-5:]]
    over_25 = all(m > 25 for m in last5) if last5 else False
    under_5 = all(0 < m < 5 for m in last5) if last5 else False

    # 近 3 天拒绝（F2/F4）：① 当日任务全 SKIPPED；② 当日零活动（session 与 ASR 均无）。
    # 守卫：窗口内至少有过 1 天活动——全新系统的"尚未开始"不算拒绝。
    last3 = days[-3:]
    _skip_all = [
        x["tasks_total"] > 0 and x["skipped"] == x["tasks_total"] and x["sess"] == 0 for x in last3
    ]
    _idle = [not x["has_activity"] for x in last3]
    refused_3 = (all(_skip_all) or all(_idle)) and len(active_days) >= 1

    # RPR 近 2 周
    # 🔴 与 days 循环一致：窗口**排除今天**（今天未结束会稀释超时/通过率判定）。
    # 用本地日期的 UTC 闭开区间做完整时间戳比较——禁止把本地 midnight 当 UTC(Z) 串，
    # 否则窗口相对真实 UTC 早 8 小时偏移（Asia/Shanghai）。
    rpr = None
    try:
        rpr_start, _ = local_date_range(d0 - dt.timedelta(days=14))
        rpr_end, _ = local_date_range(d0)  # 今天 00:00 本地 = 窗口上界（开区间）
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(grade>=1),0) FROM answer_log"
            " WHERE task_type='REVIEW' AND answered_at >= ? AND answered_at < ?",
            (fmt_ts(rpr_start), fmt_ts(rpr_end)),
        ).fetchone()
        if row[0] >= 5:
            rpr = row[1] / row[0]
    except sqlite3.OperationalError:
        pass

    results = []

    # 红线 1（formula 供报告展示——把判定式与引用关系落到界面，2026-09-13 审计需求）
    _f1_formula = (
        "判定：连续 3 天零活动或任务全跳过 ｜ 或近 2 周 SIR<20%"
        "（有效会话日≥10）｜ 关联：study_session/daily_task/ASR"
    )
    sir_low_2w = sir_2w is not None and sir_2w < 0.20 and len(two_week) >= 10
    if refused_3 or sir_low_2w:
        results.append(
            {
                "name": "Frank 拒绝使用",
                "status": "trig",
                "state": "已触发",
                "action": "立即降量：周新学 12→4，日时长 12→8 min——先保住愿意打开",
                "formula": _f1_formula,
            }
        )
    elif len(two_week) < 10:
        # F1：SIR 样本不足（会话数 <10 日的比值只是噪声）不做判定
        results.append(
            {
                "name": "Frank 拒绝使用",
                "status": "na",
                "state": "数据积累中",
                "action": f"样本不足（有效会话日 {len(two_week)}/10）。"
                "触发时：立即降量（周新学 12→4，日时长 12→8 min）",
                "formula": _f1_formula,
            }
        )
    else:
        results.append(
            {
                "name": "Frank 拒绝使用",
                "status": "calm",
                "state": "未触发",
                "action": f"当前 SIR {sir_2w:.0%}（阈值 20%）",
                "formula": _f1_formula,
            }
        )

    # 红线 2
    _f2_formula = (
        "判定：近 5 天日总时长全部 >25 min 或全部 <5 min ｜ 关联：new_per_day（周新学上限）"
    )
    if over_25 or under_5:
        results.append(
            {
                "name": "时长失控",
                "status": "trig",
                "state": "已触发",
                "action": ("查 new_per_day 与 K" if over_25 else "转红线 1 处理"),
                "formula": _f2_formula,
            }
        )
    elif not any(m > 0 for m in last5):
        results.append(
            {
                "name": "时长失控",
                "status": "na",
                "state": "数据积累中",
                "action": "触发时：>25min 查 new_per_day；<5min 转红线 1",
                "formula": _f2_formula,
            }
        )
    else:
        results.append(
            {
                "name": "时长失控",
                "status": "calm",
                "state": "未触发",
                "action": f"近 5 天日均 {sum(last5) / len(last5):.0f} min（阈值 5–25）",
                "formula": _f2_formula,
            }
        )

    # 红线 3
    _f3_formula = "判定：近 2 周 RPR<70%（REVIEW 样本≥5，本地日闭开窗口）｜ 关联：R@R 复核"
    if rpr is not None and rpr < 0.70:
        results.append(
            {
                "name": "通过率崩塌",
                "status": "trig",
                "state": "已触发",
                "action": "先查 R@R：正常(0.80–0.90)则题目太难；<0.75 则间隔太长",
                "formula": _f3_formula,
            }
        )
    elif rpr is None:
        results.append(
            {
                "name": "通过率崩塌",
                "status": "na",
                "state": "数据积累中",
                "action": "触发时：先查 R@R——正常则题目太难；<0.75 则间隔太长",
                "formula": _f3_formula,
            }
        )
    else:
        results.append(
            {
                "name": "通过率崩塌",
                "status": "calm",
                "state": "未触发",
                "action": f"当前 RPR {rpr:.0%}（阈值 70%）",
                "formula": _f3_formula,
            }
        )

    return results
