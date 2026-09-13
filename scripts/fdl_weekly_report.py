#!/usr/bin/env python3
"""掌握度周报回灌（P2-3）：每周量化"越用越聪明"。

背景
----
FDL 是单机小学生错题本。King 要求把"进步可见、顽固点暴露"做成**每周报告**：
让复习努力被量化，让长期卡住的错因浮出水面。本报告是 P2-3 的落地。

设计要点（务必遵守，已在代码注释里写清"为什么"）：
1. 时区一律走 `fdl_core.srs.time_layer`：所有"本周/上周"的界定都基于
   **本地日期（Asia/Shanghai）**，存储是 UTC。禁止 `date.today()` / `s[:10]` 切片——
   切片会在 UTC 16:00 之后把"昨日 UTC 存的历史时间"误判成本周/上周。
2. "错误归属"必须 JOIN：`review_feedback` 没有 `mistake_id` 列，需经
   `review_schedule`（`rf.schedule_id = rs.id` → `rs.mistake_id = m.id`）才能拿到
   错因 `diagnosis_type` 与错题本身。
3. A3 阶梯 `[1,2,4,7,15,21]` 从 `fdl_core.mistakes.review` 导入，**禁止硬编码**。
4. "无样本"的指标统一显示「—」而非 0（FDL 既有口径约定：0 表示"有数据但为零"，
   「—」表示"根本没有样本可比"，语义不同）。
5. 回灌 `report_meta` 用 `INSERT ... ON CONFLICT(key) DO UPDATE` 保证**幂等**；
   回灌失败绝不连累报告生成（try/except + 日志）。
6. 输出为**单文件自包含**静态 HTML：自带内联 CSS（复用 FDL 报告视觉变量名），
   用内联 div 宽度画条形，**不引任何图表库 / 不发起外部请求**。
7. 全报告**禁止 emoji**（状态标记用中文：上升 / 持平 / 下降 / 顽固 / 新增 / 在缩 / 加重）。
8. 纯标准库 + 项目已有模块，无新依赖。

CLI
---
    python scripts/fdl_weekly_report.py [--week YYYY-Www] [--db <path>]
                                        [--out <path>] [--json]

默认周 = 本地日期所在 ISO 周（周一为一周起点）。
默认输出 = data/weekly_report_<YYYY>-W<ww>.html（--json 时同目录多写一个 .json）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

# A3 阶梯从 review 模块导入，不在此硬编码（任务硬约束）
from fdl_core.mistakes.review import A3_STEP

# ── 项目既有模块（唯一允许的依赖）──────────────────────────────
from fdl_core.srs.time_layer import (
    LOCAL_TZ,
    TS_FORMAT,
    local_date,
    now_utc,
    parse_ts,
)

# FDL 四类错因（mistake_record.diagnosis_type 取值）
DIAG_TYPES = ("CONCEPT", "CALC", "MISREAD", "NORM")
# 错因中文名（仅展示用）
DIAG_CN = {
    "CONCEPT": "概念",
    "CALC": "计算",
    "MISREAD": "审题",
    "NORM": "规范",
}

logger = logging.getLogger("fdl_weekly_report")

# 趋势 / 状态判定阈值（平均掌握度 ±0.2 视为持平，符合任务约定）
TREND_THRESHOLD = 0.2


# ════════════════════════════════════════════════════════════════
# 周 / 时间边界工具
# ════════════════════════════════════════════════════════════════
def parse_week_arg(week_str: str) -> date:
    """把 'YYYY-Www' 解析成本周周一（本地日期）。

    用 `%G-W%V-%u` 而非手动算术，确保 ISO 周（含跨年周、年份归并）计算正确。
    """
    return datetime.strptime(f"{week_str}-1", "%G-W%V-%u").date()


def current_week_str() -> str:
    """默认周 = 本地日期所在 ISO 周（周一为一周起点）。"""
    y, w, _ = local_date().isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(monday: date) -> tuple[str, str]:
    """本周（本地周一 00:00 → 下周一 00:00）的 UTC 边界字符串。

    为什么转 UTC 再比较：存储的 created_at 是 UTC ISO 字符串；本函数是"本地周"，
    必须先把本地周一/周日零点用 LOCAL_TZ 转为 UTC，再以同格式（TS_FORMAT 带 Z）
    做字符串范围比较。这样 UTC 16:00 之后（=本地次日）的记录会正确归入下一周，
    绝不能用 `created_at[:10]` 直接切 UTC 日期。
    """
    start_local = datetime.combine(monday, time.min, tzinfo=LOCAL_TZ)
    end_local = datetime.combine(monday + timedelta(days=7), time.min, tzinfo=LOCAL_TZ)
    start_utc = start_local.astimezone(UTC).strftime(TS_FORMAT)
    end_utc = end_local.astimezone(UTC).strftime(TS_FORMAT)
    return start_utc, end_utc


def prev_week_str(monday: date) -> str:
    """上周同格式的周字符串（用于趋势对比）。"""
    y, w, _ = (monday - timedelta(days=7)).isocalendar()
    return f"{y}-W{w:02d}"


# ════════════════════════════════════════════════════════════════
# A3 阶梯档位归类
# ════════════════════════════════════════════════════════════════
def classify_a3_step(interval_days: int | float) -> int:
    """把某个 planned_interval_days 归到 A3 阶梯的第几档（返回 0..len(A3_STEP)-1）。

    为什么用"最接近"而非"相等"：planned_interval_days 是
    `round(A3_STEP 基础档 × 评分倍率)`，评分乘率会让它偏离固定档
    （如 grade=4 熟练 → 4×1.5=6 天，最接近第 4 档 7）。因此取与 A3_STEP 各档
    欧氏距离最小的档位作为"当前所在阶梯档"。
    """
    best = 0
    best_diff = abs(interval_days - A3_STEP[0])
    for i, step in enumerate(A3_STEP):
        diff = abs(interval_days - step)
        if diff < best_diff:
            best_diff = diff
            best = i
    return best


# ════════════════════════════════════════════════════════════════
# 指标计算
# ════════════════════════════════════════════════════════════════
def _empty_per_type() -> dict:
    """四种错因都先占位（None 表示"无样本"，渲染时显示「—」）。"""
    return {dt: None for dt in DIAG_TYPES}


def compute_metrics(conn: sqlite3.Connection, week_str: str) -> dict:
    """计算本周四项指标（按错因分组），返回可直接渲染 / 序列化的 dict。"""
    monday = parse_week_arg(week_str)
    this_start, this_end = week_bounds(monday)
    last_monday = monday - timedelta(days=7)
    last_start, last_end = week_bounds(last_monday)

    cur = conn.cursor()

    # ── 指标 1：复习次数（本周 / 累计），按错因 ──────────────────
    # 本周：review_feedback.created_at 落在本 ISO 周内（JOIN 取错因）。
    weekly_counts = {dt: 0 for dt in DIAG_TYPES}
    for dt, n in cur.execute(
        """
        SELECT m.diagnosis_type, COUNT(*)
        FROM review_feedback rf
        JOIN review_schedule rs ON rs.id = rf.schedule_id
        JOIN mistake_record m ON m.id = rs.mistake_id
        WHERE rf.created_at >= ? AND rf.created_at < ?
        GROUP BY m.diagnosis_type
        """,
        (this_start, this_end),
    ).fetchall():
        if dt in weekly_counts:
            weekly_counts[dt] = n

    # 累计：所有历史复习（无时间条件）按错因。
    cum_counts = {dt: 0 for dt in DIAG_TYPES}
    for dt, n in cur.execute(
        """
        SELECT m.diagnosis_type, COUNT(*)
        FROM review_feedback rf
        JOIN review_schedule rs ON rs.id = rf.schedule_id
        JOIN mistake_record m ON m.id = rs.mistake_id
        GROUP BY m.diagnosis_type
        """
    ).fetchall():
        if dt in cum_counts:
            cum_counts[dt] = n

    # ── 指标 2：平均掌握度趋势（本周 AVG vs 上周 AVG）────────────
    def _avg_self_rating(start: str, end: str) -> dict:
        d = _empty_per_type()
        for dt, avg in cur.execute(
            """
            SELECT m.diagnosis_type, AVG(rf.self_rating)
            FROM review_feedback rf
            JOIN review_schedule rs ON rs.id = rf.schedule_id
            JOIN mistake_record m ON m.id = rs.mistake_id
            WHERE rf.created_at >= ? AND rf.created_at < ?
            GROUP BY m.diagnosis_type
            """,
            (start, end),
        ).fetchall():
            if dt in d:
                d[dt] = round(avg, 2)
        return d

    mastery_this = _avg_self_rating(this_start, this_end)
    mastery_last = _avg_self_rating(last_start, last_end)

    mastery = {}
    for dt in DIAG_TYPES:
        t, lv = mastery_this[dt], mastery_last[dt]
        if t is None or lv is None:
            trend = "—"  # 任一周无样本 → 无法比较 → 「—」
        else:
            delta = t - lv
            if delta > TREND_THRESHOLD:
                trend = "上升"
            elif delta < -TREND_THRESHOLD:
                trend = "下降"
            else:
                trend = "持平"
        mastery[dt] = {"this": t, "last": lv, "trend": trend}

    # ── 指标 3a：间隔增长速率（本周 vs 上周 planned_interval_days 均值）──
    # "本周平均间隔" = 本周新建的复习计划（created_at 落在本周）的间隔均值。
    def _avg_interval(start: str, end: str) -> dict:
        d = _empty_per_type()
        for dt, avg in cur.execute(
            """
            SELECT m.diagnosis_type, AVG(rs.planned_interval_days)
            FROM review_schedule rs
            JOIN mistake_record m ON m.id = rs.mistake_id
            WHERE rs.created_at >= ? AND rs.created_at < ?
            GROUP BY m.diagnosis_type
            """,
            (start, end),
        ).fetchall():
            if dt in d:
                d[dt] = round(avg, 2)
        return d

    interval_this = _avg_interval(this_start, this_end)
    interval_last = _avg_interval(last_start, last_end)

    # ── 指标 3b：当前处于 A3 阶梯第几档的分布（PENDING 计划快照）──
    step_dist = {dt: [0] * len(A3_STEP) for dt in DIAG_TYPES}
    for dt, pid in cur.execute(
        """
        SELECT m.diagnosis_type, rs.planned_interval_days
        FROM review_schedule rs
        JOIN mistake_record m ON m.id = rs.mistake_id
        WHERE rs.status = 'PENDING'
        """
    ).fetchall():
        if dt in step_dist:
            step_dist[dt][classify_a3_step(pid)] += 1

    interval = {}
    for dt in DIAG_TYPES:
        t, lv = interval_this[dt], interval_last[dt]
        delta = round(t - lv, 2) if (t is not None and lv is not None) else None
        interval[dt] = {
            "this": t,
            "last": lv,
            "delta": delta,
            "step_dist": step_dist[dt],
        }

    # ── 指标 4：顽固题清单 ──────────────────────────────────────
    # 规则：reappear_count >= 3 且"最近一次" self_rating <= 2。
    # "最近一次" 需按 created_at 取每个错题的最新反馈（SQL 不便取每组最新，故在 Python 归并）。
    latest = {}  # mistake_id -> {rating, dt, rc, created_at}
    for created_at, rating, mid, dt, rc in cur.execute(
        """
        SELECT rf.created_at, rf.self_rating, rs.mistake_id,
               m.diagnosis_type, m.reappear_count
        FROM review_feedback rf
        JOIN review_schedule rs ON rs.id = rf.schedule_id
        JOIN mistake_record m ON m.id = rs.mistake_id
        ORDER BY rf.created_at DESC
        """
    ).fetchall():
        if mid is None or mid in latest:
            continue  # 已记录该错题的"最近一次"（因 DESC 排序，首个即最新）
        latest[mid] = {
            "rating": rating,
            "dt": dt,
            "rc": rc,
            "created_at": created_at,
        }

    stubborn = []
    stubborn_by_type = {dt: {"count": 0, "ids": []} for dt in DIAG_TYPES}
    for mid, info in latest.items():
        if info["rc"] is None:
            continue
        if info["rc"] >= 3 and info["rating"] <= 2:
            # 最近复习日：UTC created_at → 本地日期（禁止切 UTC 字符串）
            try:
                last_date = local_date(parse_ts(info["created_at"])).isoformat()
            except Exception:
                last_date = "—"
            stubborn.append(
                {
                    "id": mid,
                    "dt": info["dt"],
                    "rc": info["rc"],
                    "last_rating": info["rating"],
                    "last_date": last_date,
                    "_sort_created": info["created_at"] or "",
                }
            )
            if info["dt"] in stubborn_by_type:
                stubborn_by_type[info["dt"]]["count"] += 1
                stubborn_by_type[info["dt"]]["ids"].append(mid)

    # 排序：复习次数多者优先，其次最近复习更近者优先；最多 10 条
    stubborn.sort(key=lambda r: (-(r["rc"] or 0), r["_sort_created"]), reverse=False)
    stubborn.sort(key=lambda r: (-(r["rc"] or 0), r["_sort_created"]))
    stubborn = stubborn[:10]
    for r in stubborn:
        r.pop("_sort_created", None)

    return {
        "week": week_str,
        "week_range": (monday.isoformat(), (monday + timedelta(days=6)).isoformat()),
        "generated_at": local_date(now_utc()).isoformat(),
        "review_counts": {"week": weekly_counts, "cum": cum_counts},
        "mastery": mastery,
        "interval": interval,
        "stubborn": stubborn,
        "stubborn_by_type": stubborn_by_type,
    }


# ════════════════════════════════════════════════════════════════
# 回灌 report_meta（幂等 + 失败隔离）
# ════════════════════════════════════════════════════════════════
def write_back_stubborn(
    conn: sqlite3.Connection,
    week_str: str,
    stubborn_by_type: dict,
) -> tuple[dict, dict]:
    """把"顽固错因"回灌到 report_meta，并返回（上周快照, 本周结论）。

    - key 形如 `weekly_stubborn_<错因>`，value 为 JSON：
      `{"week": "2026-W37", "count": N, "ids": [...]}`，status='ACTIVE'。
    - 用 `INSERT ... ON CONFLICT(key) DO UPDATE` 保证**幂等**（同周重跑不重复）。
    - 先读"上一次"快照（回灌前），再据此给出 在缩/顽固/新增/加重/— 结论。
    - 整个回灌包在 try/except 里：失败只记日志、回滚，**绝不影响报告生成**。
    """
    prev: dict[str, int] = {}
    try:
        for key, value in conn.execute(
            "SELECT key, value FROM report_meta WHERE key LIKE 'weekly_stubborn_%'"
        ).fetchall():
            dt = key.replace("weekly_stubborn_", "")
            try:
                prev[dt] = int(json.loads(value).get("count", 0))
            except Exception:
                prev[dt] = 0
    except Exception as exc:  # 读快照失败也要继续出报告
        logger.warning("读取上周顽固快照失败（不影响报告）: %s", exc)
        prev = {}

    conclusions: dict[str, str] = {}
    now = now_utc().strftime(TS_FORMAT)
    try:
        for dt, info in stubborn_by_type.items():
            cur_count = info["count"]
            prev_count = prev.get(dt, 0)
            # 结论：本周顽固数 vs 上一次持久化顽固数
            if cur_count == 0 and prev_count == 0:
                conclusions[dt] = "—"
            elif cur_count == 0 and prev_count > 0:
                conclusions[dt] = "在缩"  # 上周有、本周清零 → 缩
            elif cur_count > 0 and prev_count == 0:
                conclusions[dt] = "新增"  # 上周无、本周出现 → 新增
            elif cur_count > 0 and prev_count > 0:
                if cur_count < prev_count:
                    conclusions[dt] = "在缩"
                elif cur_count == prev_count:
                    conclusions[dt] = "顽固"
                else:
                    conclusions[dt] = "加重"
            else:
                conclusions[dt] = "—"
            # 仅当本周确实出现顽固题才回灌（无样本不写空记录）
            if cur_count > 0:
                payload = json.dumps(
                    {"week": week_str, "count": cur_count, "ids": info["ids"]},
                    ensure_ascii=False,
                )
                conn.execute(
                    "INSERT INTO report_meta (key, status, value, updated_at) "
                    "VALUES (?, 'ACTIVE', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "status=excluded.status, value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (f"weekly_stubborn_{dt}", payload, now),
                )
        conn.commit()
    except Exception as exc:
        logger.error("回灌 report_meta 失败（不影响报告生成）: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass

    return prev, conclusions


# ════════════════════════════════════════════════════════════════
# HTML 渲染（单文件自包含，自带 CSS 变量，纯内联条形）
# ════════════════════════════════════════════════════════════════
def _fmt(v, nd=2):
    """None → 「—」；数字按 nd 位小数。"""
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _trend_class(trend: str) -> str:
    return {
        "上升": "ok",
        "下降": "danger",
        "持平": "muted",
        "—": "muted",
    }.get(trend, "muted")


def _status_class(s: str) -> str:
    return {
        "新增": "danger",
        "加重": "danger",
        "顽固": "warn",
        "在缩": "ok",
        "—": "muted",
    }.get(s, "muted")


CSS = """
:root{
  --ink:#2B2B2B; --panel:#FFFDF5; --bg:#FBF6EC;
  --teal:#0E7C7B; --gold:#D99A2B;
  --ok:#3E7A52; --warn:#C07A1B; --danger:#B4552D;
  --muted:#7A6F5D; --line:#D8CCB0;
  --hand:"Marker Felt","Hannotate SC","Kaiti SC","STKaiti","Segoe Print","Comic Sans MS",cursive;
  --sketch-radius:255px 15px 225px 15px / 15px 225px 15px 255px;
}
*{box-sizing:border-box;margin:0;}
body{color:var(--ink);font-family:var(--hand);font-size:17px;line-height:1.7;
  background-color:var(--bg);
  background-image:repeating-linear-gradient(transparent,transparent 27px,rgba(120,100,60,.08) 28px);}
.wrap{max-width:1080px;margin:0 auto;padding:0 16px 48px;}
header.hero{background:var(--panel);border-bottom:3px solid var(--ink);
  border-radius:0 0 var(--sketch-radius);padding:22px 16px 14px;text-align:center;}
header.hero h1{font-size:30px;}
header.hero .meta{color:var(--muted);font-size:14px;margin-top:6px;}
.banner{max-width:760px;margin:14px auto;padding:8px 14px;background:#FBF1DD;
  border:1px solid #E7CFA0;border-radius:10px;color:#9A6A12;font-size:13px;text-align:center;}
section{background:var(--panel);border:2px solid var(--ink);border-radius:var(--sketch-radius);
  padding:16px 18px;margin:16px 0;box-shadow:2px 3px 0 rgba(43,43,43,.18);}
section h2{font-size:21px;margin-bottom:4px;}
section .sub{color:var(--muted);font-size:13px;margin-bottom:10px;}
table{width:100%;border-collapse:collapse;font-size:15px;}
th,td{border-bottom:1px dashed var(--line);padding:7px 8px;text-align:left;}
th{color:var(--muted);font-weight:500;font-size:13px;}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;}
.tag{display:inline-block;padding:1px 10px;border:2px solid var(--ink);border-radius:155px 15px 225px 15px/15px 225px 15px 255px;font-size:14px;}
.tag.ok{color:var(--ok);border-color:var(--ok);}
.tag.danger{color:var(--danger);border-color:var(--danger);}
.tag.warn{color:var(--warn);border-color:var(--warn);}
.tag.muted{color:var(--muted);border-color:var(--muted);}
.barcell{position:relative;}
.bar{height:12px;background:var(--gold);border-radius:6px;min-width:2px;display:inline-block;vertical-align:middle;}
.barnum{font-size:12px;color:var(--muted);margin-left:6px;}
.note{font-size:13px;color:var(--muted);margin-top:10px;}
.empty{color:var(--muted);font-style:normal;padding:6px 2px;}
footer{text-align:center;color:var(--muted);font-size:12px;padding:18px 0;}
"""


def render_html(metrics: dict, prev: dict, conclusions: dict) -> str:
    """把指标渲染成自包含 HTML 字符串。"""
    week = metrics["week"]
    rng = metrics["week_range"]
    gen = metrics["generated_at"]
    rc = metrics["review_counts"]
    mastery = metrics["mastery"]
    interval = metrics["interval"]
    stubborn = metrics["stubborn"]

    # ── 面板 1：复习次数 ──
    rows1 = []
    for dt in DIAG_TYPES:
        w = rc["week"][dt]
        c = rc["cum"][dt]
        rows1.append(
            f"<tr><td>{DIAG_CN[dt]} <span class='muted'>({dt})</span></td>"
            f"<td class='num'>{_fmt(w, 0)}</td>"
            f"<td class='num'>{_fmt(c, 0)}</td></tr>"
        )
    panel1 = (
        "<section><h2>一、复习次数（按错因）</h2>"
        "<div class='sub'>本周 = 本地日期所在 ISO 周（周一为起点）；累计 = 历史全部复习。</div>"
        "<table><thead><tr><th>错因</th><th class='num'>本周次数</th><th class='num'>累计次数</th></tr></thead>"
        f"<tbody>{''.join(rows1)}</tbody></table>"
        "<div class='note'>无样本的错因显示「—」（表示根本没有复习记录，而非『零次复习』）。</div></section>"
    )

    # ── 面板 2：平均掌握度趋势 ──
    rows2 = []
    for dt in DIAG_TYPES:
        m = mastery[dt]
        cls = _trend_class(m["trend"])
        rows2.append(
            f"<tr><td>{DIAG_CN[dt]} <span class='muted'>({dt})</span></td>"
            f"<td class='num'>{_fmt(m['this'])}</td>"
            f"<td class='num'>{_fmt(m['last'])}</td>"
            f"<td><span class='tag {cls}'>{m['trend']}</span></td></tr>"
        )
    panel2 = (
        "<section><h2>二、平均掌握度趋势（本周 vs 上周）</h2>"
        "<div class='sub'>自评分 1=陌生 / 2=模糊 / 3=掌握 / 4=熟练；变化幅度超过 ±0.2 才判定上升/下降。</div>"
        "<table><thead><tr><th>错因</th><th class='num'>本周均值</th><th class='num'>上周均值</th><th>趋势</th></tr></thead>"
        f"<tbody>{''.join(rows2)}</tbody></table></section>"
    )

    # ── 面板 3：间隔增长速率 + A3 阶梯档位分布 ──
    rows3 = []
    for dt in DIAG_TYPES:
        iv = interval[dt]
        delta_txt = _fmt(iv["delta"])
        if iv["delta"] is not None:
            if iv["delta"] > 0:
                delta_txt = f"+{iv['delta']:.2f}"
            delta_cls = "ok" if iv["delta"] > 0 else ("danger" if iv["delta"] < 0 else "muted")
        else:
            delta_cls = "muted"
        rows3.append(
            f"<tr><td>{DIAG_CN[dt]} <span class='muted'>({dt})</span></td>"
            f"<td class='num'>{_fmt(iv['this'])}</td>"
            f"<td class='num'>{_fmt(iv['last'])}</td>"
            f"<td class='num'><span class='tag {delta_cls}'>{delta_txt}</span></td></tr>"
        )
    # 阶梯分布（每错因一行，6 档内联条形）
    maxcnt = max((max(iv["step_dist"]) for iv in interval.values()), default=0) or 1
    rows3b = []
    for dt in DIAG_TYPES:
        dist = interval[dt]["step_dist"]
        cells = []
        for _i, cnt in enumerate(dist):
            width = (cnt / maxcnt * 100) if maxcnt else 0
            cells.append(
                f"<td class='barcell'>{cnt}"
                f"<br><span class='bar' style='width:{width:.0f}%'></span></td>"
            )
        rows3b.append(
            f"<tr><td>{DIAG_CN[dt]} <span class='muted'>({dt})</span></td>{''.join(cells)}</tr>"
        )
    step_head = "".join(f"<th class='num'>{A3_STEP[i]}天</th>" for i in range(len(A3_STEP)))
    panel3 = (
        "<section><h2>三、间隔增长速率（最硬的掌握指标）</h2>"
        "<div class='sub'>复习计划间隔越长 = 记忆越稳。本周均值对比上周新建计划的 planned_interval_days。</div>"
        "<table><thead><tr><th>错因</th><th class='num'>本周平均间隔</th><th class='num'>上周平均间隔</th><th class='num'>变化</th></tr></thead>"
        f"<tbody>{''.join(rows3)}</tbody></table>"
        "<h2 style='margin-top:18px;font-size:18px;'>当前处于 A3 阶梯第几档（PENDING 计划分布）</h2>"
        "<div class='sub'>A3 阶梯档位（天）："
        + " / ".join(str(s) for s in A3_STEP)
        + "；条形长度 = 该档计划数。</div>"
        f"<table><thead><tr><th>错因</th>{step_head}</tr></thead><tbody>{''.join(rows3b)}</tbody></table>"
        "<div class='note'>间隔是『记忆稳固度』最硬的信号：间隔变长说明同类错因正在被驯服。</div></section>"
    )

    # ── 面板 4：顽固题清单 ──
    if stubborn:
        rows4 = []
        for s in stubborn:
            rows4.append(
                f"<tr><td class='num'>{s['id']}</td>"
                f"<td>{DIAG_CN.get(s['dt'], s['dt'])} <span class='muted'>({s['dt']})</span></td>"
                f"<td class='num'>{s['rc']}</td>"
                f"<td class='num'>{s['last_rating']}</td>"
                f"<td>{s['last_date']}</td></tr>"
            )
        panel4 = (
            "<section><h2>四、顽固题清单</h2>"
            "<div class='sub'>筛选：复习次数 ≥ 3 且最近一次自评分 ≤ 2（最多 10 条）。这些是需要重点干预的错因。</div>"
            "<table><thead><tr><th class='num'>错题ID</th><th>错因</th><th class='num'>复习次数</th>"
            "<th class='num'>最近评分</th><th>最近复习日</th></tr></thead>"
            f"<tbody>{''.join(rows4)}</tbody></table></section>"
        )
    else:
        panel4 = (
            "<section><h2>四、顽固题清单</h2>"
            "<div class='empty'>本周无顽固题（复习次数 ≥ 3 且最近评分 ≤ 2 的错题为空）。继续保持！</div></section>"
        )

    # ── 面板 5：顽固错因对比（回灌结论）──
    concl_rows = []
    for dt in DIAG_TYPES:
        cur_c = metrics["stubborn_by_type"][dt]["count"]
        prev_c = prev.get(dt, 0)
        s = conclusions.get(dt, "—")
        if cur_c == 0 and prev_c == 0:
            continue  # 从未出现顽固 → 不占行
        cls = _status_class(s)
        concl_rows.append(
            f"<tr><td>{DIAG_CN[dt]} <span class='muted'>({dt})</span></td>"
            f"<td class='num'>{cur_c}</td>"
            f"<td class='num'>{prev_c}</td>"
            f"<td><span class='tag {cls}'>{s}</span></td></tr>"
        )
    if concl_rows:
        panel5 = (
            "<section><h2>五、顽固错因对比（回灌结论）</h2>"
            "<div class='sub'>对比上一次回灌的 weekly_stubborn_*：新增=上周无本周有；"
            "顽固=数量持平；在缩=本周减少/清零；加重=本周增多。</div>"
            "<table><thead><tr><th>错因</th><th class='num'>本周顽固数</th>"
            "<th class='num'>上次顽固数</th><th>状态</th></tr></thead>"
            f"<tbody>{''.join(concl_rows)}</tbody></table></section>"
        )
    else:
        panel5 = (
            "<section><h2>五、顽固错因对比（回灌结论）</h2>"
            "<div class='empty'>上一次与本周均无顽固错因记录。</div></section>"
        )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FDL 掌握度周报 {week}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <h1>掌握度周报</h1>
  <div class="meta">周期 {week}（{rng[0]} ~ {rng[1]}）· 生成于本地日期 {gen}</div>
</header>
<div class="banner">本周数据按本地时间（Asia/Shanghai）的 ISO 周界定；存储为 UTC，已折算到本地日界，避免跨日误判。</div>
{panel1}
{panel2}
{panel3}
{panel4}
{panel5}
<footer>FDL 掌握度周报 · 单机本地生成 · 数据截止于报告生成时刻</footer>
</div>
</body>
</html>
"""
    return html


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════
def _default_db_path() -> Path:
    """默认库 = 配置中的高频主库（primary_db_dir）；失败回退 data/fdl.db。"""
    try:
        from fdl_core.paths import get_paths

        return Path(str(get_paths().primary_db_path))
    except Exception:
        root = Path(__file__).resolve().parent.parent
        return root / "data" / "fdl.db"


def _default_out_path(week_str: str) -> Path:
    root = Path(__file__).resolve().parent.parent
    return root / "data" / f"weekly_report_{week_str}.html"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="FDL 掌握度周报（P2-3）")
    parser.add_argument("--week", default=None, help="ISO 周 YYYY-Www，默认本地所在周")
    parser.add_argument("--db", default=None, help="SQLite 路径，默认生产主库")
    parser.add_argument("--out", default=None, help="HTML 输出路径")
    parser.add_argument("--json", action="store_true", help="额外写出 JSON 指标文件")
    args = parser.parse_args(argv)

    week_str = args.week or current_week_str()
    db_path = Path(args.db) if args.db else _default_db_path()
    out_path = Path(args.out) if args.out else _default_out_path(week_str)

    logger.info("周=%s db=%s out=%s", week_str, db_path, out_path)

    conn = sqlite3.connect(str(db_path))
    try:
        # 确保 report_meta 存在（极端情况下老库缺表也不崩）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS report_meta ("
            "key TEXT PRIMARY KEY, status TEXT, value TEXT, "
            "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
        )
        conn.commit()

        metrics = compute_metrics(conn, week_str)
        prev, conclusions = write_back_stubborn(conn, week_str, metrics["stubborn_by_type"])
    finally:
        conn.close()

    html = render_html(metrics, prev, conclusions)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    logger.info("HTML 已生成: %s", out_path)

    if args.json:
        json_path = out_path.with_suffix(".json")
        payload = {
            "week": metrics["week"],
            "week_range": metrics["week_range"],
            "generated_at": metrics["generated_at"],
            "review_counts": metrics["review_counts"],
            "mastery": metrics["mastery"],
            "interval": metrics["interval"],
            "stubborn": metrics["stubborn"],
            "stubborn_by_type": metrics["stubborn_by_type"],
            "conclusions": conclusions,
            "prev": prev,
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("JSON 已生成: %s", json_path)

    # 终端简要回报（便于人工/CI 查看）
    print(f"[周报] {week_str} 已生成 -> {out_path}")
    print(
        "  复习次数(本周/累计): "
        + ", ".join(
            f"{DIAG_CN[dt]}={metrics['review_counts']['week'][dt]}/{metrics['review_counts']['cum'][dt]}"
            for dt in DIAG_TYPES
        )
    )
    print(
        "  平均掌握度趋势: "
        + ", ".join(f"{DIAG_CN[dt]}={metrics['mastery'][dt]['trend']}" for dt in DIAG_TYPES)
    )
    print(f"  顽固题数: {len(metrics['stubborn'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
