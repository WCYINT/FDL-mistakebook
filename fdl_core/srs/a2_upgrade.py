"""
fdl_core/srs/a2_upgrade.py — A3 → A2 自动升级调度（2026-09-08）

🟢 背景：
    A3（Leitner 阶梯）当前阶段最适合——50 条错题、零知识点先验、零复习历史。
    但 A3 的"群体均值阶梯"对个体差异不敏感，对极端难/易的卡不够灵活。
    一旦满足以下条件，应自动升级到 A2（FSRS 默认参数）：
      ① A3 方案运行 ≥ 7 天（防止数据不足时切换）
      ② knowledge_point 已建 ≥ 50 条（知识点体系基础就位）
      ③ mistake_record 已有 ≥ 80% 挂载真实 KP（kp_id != 0）
      ④ 累计 review_schedule 历史（DONE+作废）≥ 100 条

🟢 升级触发器（应每日 04:00 跑）：
    def should_upgrade_to_a2(conn) -> UpgradeCheckResult:
        检四个条件，返回 {ready, reasons, metrics}。
        ready=True 才返回 True，并附 metrics 快照。

🟢 切换机制：
    一旦 ready=True，下次 mark_reviewed 自动走 A2。
    算法本身通过全局开关控制：A3_ACTIVE = True / False。
    切换是单向（不会自动回 A3），但可手动 rollback。

🟢 A2 FSRS 参数（17 个 w 值，抄 Anki FSRS-4.5 默认，**禁止训练**）：
    官方明令：少于 1000 条复习记录就用默认参数。
    FDL 当前远未达 1000 条门槛——直接硬编码即可。

参考：
    - docs/research/SRS-算法选型-A1A2A3.md §一-②
    - FSRS 官方仓库 https://github.com/open-spaced-repetition/ts-fsrs  # audit-ok: 文档引用
    - Woźniak SM-17 两成分模型（稳定性 + 可提取性）
    - Wilson et al. 2019 Nature Communications「85% 法则」
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC

# === A3 升级条件阈值（参数化以便日后调整）===
A3_MIN_RUN_DAYS = 7  # A3 方案最少运行天数
A2_MIN_KP_COUNT = 50  # 知识点体系最少条目数
A2_MIN_KP_COVERAGE = 0.80  # 错题挂载 KP 的最低比例
A2_MIN_REVIEW_HISTORY = 100  # 累计复习记录最低条数

# === A2 FSRS-4.5 默认参数（17 个 w 值，**禁止训练**）===
# 数据来源：Anki 官方 FSRS 仓库默认参数（commit v4.5.0）
# 见 https://github.com/open-spaced-repetition/fsrs4anki/blob/main/fsrs4anki/optimizer/defaults/v4_5_0.json
FSRS_W = {
    "w0": 0.4072,
    "w1": 1.1829,
    "w2": 3.1262,
    "w3": 15.4722,
    "w4": 7.2102,
    "w5": 0.5316,
    "w6": 1.0651,
    "w7": 0.0589,
    "w8": 1.5330,
    "w9": 0.1192,
    "w10": 1.0006,
    "w11": 1.9395,
    "w12": 0.1100,
    "w13": 0.2939,
    "w14": 2.0077,
    "w15": 0.2315,
    "w16": 2.9466,
    "w17": 1.0286,
}

# FSRS 关键常数
FSRS_DECAY = -0.5  # 衰减指数（公式 C）
FSRS_FACTOR = 19 / 81  # 19/81 ≈ 0.2346（公式 F）
FSRS_R_TARGET = 0.85  # 目标保留率（Wilson 2019 Nature 85% 法则）
FSRS_S_MIN = 0.01  # 稳定性下限
FSRS_S_MAX = 36500.0  # 稳定性上限（约 100 年）
FSRS_D_MIN = 1.0  # 难度下限
FSRS_D_MAX = 10.0  # 难度上限
FSRS_LEITNER_GUARD = 3  # 前 3 次复习走 Leitner 固定阶梯护栏

# === FDL ↔ FSRS 评分量表映射（口径统一，修复「差一档」）===
# FDL 4 档：1陌生 / 2模糊 / 3掌握 / 4熟练
# FSRS 0-4：0=Again(重来) / 1=Hard(模糊) / 2=Good(掌握) / 3=Easy(熟练)
# 既往 bug：FDL 1(陌生) 在 fsrs_update 的成功/失败判定里被当 fail，
# 但在 fsrs_init_d 里却被当 FSRS-1(Hard) → 同一"陌生"作答难度更新差一档。
# 统一：所有入口先把 FDL 档位映射到 FSRS 0-4 再算。
FDL_TO_FSRS = {1: 0, 2: 1, 3: 2, 4: 3}


def _to_fsrs_grade(grade: int) -> int:
    """FDL 4 档 → FSRS 0-4；越界值兜底为 clamp(grade-1, 0, 3)。"""
    return FDL_TO_FSRS.get(grade, max(0, min(3, grade - 1)))


@dataclass
class UpgradeCheckResult:
    """升级条件检查结果。"""

    ready: bool  # 是否满足全部条件
    reasons: list[str]  # 不满足的原因列表（ready=True 时为空）
    metrics: dict  # 当前 4 项指标的快照
    a3_first_seen_at: str | None  # A3 首次启用日期（ISO）

    def to_dict(self) -> dict:
        return asdict(self)


def should_upgrade_to_a2(conn: sqlite3.Connection) -> UpgradeCheckResult:
    """检查是否满足升级 A2 的全部条件。

    返回 UpgradeCheckResult，含 4 项指标的当前值与未满足原因。
    """
    metrics: dict = {}

    # 1. A3 运行天数：从 review_schedule.source='answer' 第一条 created_at 算起
    #    （A3 升级前所有 DONE 计划都是 ingest；第一次 answer 源即 A3 上线）
    # 🔴 口径修正（2026-09-08）：原用 review_schedule 的 source='answer' DONE 记录，
    # 但 2026-09-06 那批复习发生在 mark_reviewed 支持 reschedule 之前
    # （老版只更新 reappear_count，不写 review_schedule），导致历史复习零痕迹。
    # 权威来源 = mistake_record.last_reappear_at（每次 mark_reviewed 必写）。
    row = conn.execute(
        "SELECT MIN(last_reappear_at) FROM mistake_record WHERE last_reappear_at IS NOT NULL"
    ).fetchone()
    a3_first = row[0] if row and row[0] else None
    metrics["a3_first_seen_at"] = a3_first
    metrics["a3_run_days"] = 0
    a3_days_ok = False
    if a3_first:
        from datetime import datetime

        try:
            started = datetime.fromisoformat(a3_first.replace("Z", "+00:00"))
            now = datetime.now(UTC)
            run_days = (now - started).days
            metrics["a3_run_days"] = run_days
            a3_days_ok = run_days >= A3_MIN_RUN_DAYS
        except Exception:
            pass

    # 2. knowledge_point 数量
    kp_count = conn.execute("SELECT COUNT(*) FROM knowledge_point").fetchone()[0]
    metrics["kp_count"] = kp_count
    kp_count_ok = kp_count >= A2_MIN_KP_COUNT

    # 3. mistake_record 已挂载 KP 的比例（kp_id != 0）
    row = conn.execute(
        "SELECT"
        " COUNT(*) AS total,"
        " SUM(CASE WHEN kp_id != 0 THEN 1 ELSE 0 END) AS linked"
        " FROM mistake_record"
    ).fetchone()
    total_mis, linked_mis = row[0] or 0, row[1] or 0
    coverage = (linked_mis / total_mis) if total_mis > 0 else 0.0
    metrics["mistake_total"] = total_mis
    metrics["mistake_linked"] = linked_mis
    metrics["mistake_coverage"] = round(coverage, 3)
    coverage_ok = coverage >= A2_MIN_KP_COVERAGE and total_mis > 0

    # 4. 累计复习历史条数
    #    🔴 口径修正（2026-09-08）：原用 review_schedule 的 DONE 记录，
    #    同样因历史复习未写 review_schedule 而恒为 0。
    #    权威口径 = SUM(mistake_record.reappear_count)（累计复习次数）。
    history = conn.execute(
        "SELECT COALESCE(SUM(reappear_count), 0) FROM mistake_record"
    ).fetchone()[0]
    metrics["review_history_count"] = history
    metrics["reviewed_cards"] = conn.execute(
        "SELECT COUNT(*) FROM mistake_record WHERE last_reappear_at IS NOT NULL"
    ).fetchone()[0]
    history_ok = history >= A2_MIN_REVIEW_HISTORY

    reasons = []
    if not a3_days_ok:
        reasons.append(
            f"A3 运行天数不足：当前 {metrics['a3_run_days']} 天，需 ≥ {A3_MIN_RUN_DAYS} 天"
        )
    if not kp_count_ok:
        reasons.append(f"知识点数量不足：当前 {kp_count} 条，需 ≥ {A2_MIN_KP_COUNT} 条")
    if not coverage_ok:
        reasons.append(
            f"错题挂载 KP 比例不足：当前 {coverage * 100:.1f}%，"
            f"需 ≥ {A2_MIN_KP_COVERAGE * 100:.0f}%"
        )
    if not history_ok:
        reasons.append(f"复习历史记录不足：当前 {history} 条，需 ≥ {A2_MIN_REVIEW_HISTORY} 条")

    return UpgradeCheckResult(
        ready=not reasons,
        reasons=reasons,
        metrics=metrics,
        a3_first_seen_at=a3_first,
    )


# === A2 FSRS 默认参数核心算法 ===


def fsrs_init_d(grade: int) -> float:
    """FSRS 初始难度 D₀(G)：仅用第一次作答评分计算。

    D₀(G) = w4 − e^(w5·(G−1)) + 1
    grade 为 FDL 4 档（1陌生/2模糊/3掌握/4熟练），内部映射到 FSRS 0-4
    （陌生=Again=0 / 模糊=Hard=1 / 掌握=Good=2 / 熟练=Easy=3）后再算。
    """
    import math

    G = max(0, min(4, _to_fsrs_grade(grade)))
    return FSRS_W["w4"] - math.exp(FSRS_W["w5"] * (G - 1)) + 1


def fsrs_init_s(grade: int = 3) -> float:
    """FSRS 初始稳定性 S₀：FSRS-4.5 默认 w0（与 grade 无关，全局常数）。

    官方明令：<1000 条复习记录用默认值，不要按 grade 区分初始化。
    """
    return FSRS_W["w0"]


def fsrs_next_interval(s: float) -> float:
    """FSRS 间隔公式：I = S / R_TARGET^(1/C) − S（C = −0.5）"""
    return s * (FSRS_R_TARGET ** (1 / FSRS_DECAY)) - s


def fsrs_update(
    s: float,
    d: float,
    r: float,  # 调用时的可提取性 R(t)
    grade: int,  # FDL 4 档（1陌生/2模糊/3掌握/4熟练），内部转 FSRS 0-4
    elapsed_days: float,  # 上次复习距今的天数
) -> tuple[float, float]:
    """FSRS-4.5 单步更新：返回新 S、新 D。

    答对（FSRS g≥2: Good/Easy）:
        S' = S · (1 + e^w8 · (11−D) · S^(−w9) · (e^w10·(1−R) − 1))
    答错（FSRS g<2: Again/Hard）:
        S' = w11 · D^(−w12) · ((S+1)^w13 − 1) · e^(w14·(1−R))

    难度更新：D' = D − w6 · (g − 3) + w7 · (R − 0.5)
    （g 统一为 FSRS 量表，确保成功/失败判定与难度更新同一档位，修复"差一档"）
    """
    import math

    g = _to_fsrs_grade(grade)
    # 答对路径
    if g >= 2:
        new_s = s * (
            1
            + math.exp(FSRS_W["w8"])
            * (11 - d)
            * (s ** (-FSRS_W["w9"]))
            * (math.exp(FSRS_W["w10"] * (1 - r)) - 1)
        )
    else:
        new_s = (
            FSRS_W["w11"]
            * (d ** (-FSRS_W["w12"]))
            * (((s + 1) ** FSRS_W["w13"]) - 1)
            * math.exp(FSRS_W["w14"] * (1 - r))
        )
    # 难度更新（简化版；FSRS-4.5 完整公式包含更复杂的均值回归项）
    # D' = D - w6 * (g - 3) + w7 * (R - 0.5)
    new_d = d - FSRS_W["w6"] * (g - 3) + FSRS_W["w7"] * (r - 0.5)
    # 均值回归（结构护栏 #1）
    new_d = 0.5 * (new_d + d) + 0.5 * FSRS_W["w4"]
    new_d = max(FSRS_D_MIN, min(FSRS_D_MAX, new_d))
    new_s = max(FSRS_S_MIN, min(FSRS_S_MAX, new_s))
    return round(new_s, 4), round(new_d, 4)


def fsrs_r(t: float, s: float) -> float:
    """可提取性 R(t) = (1 + F·t/S)^C（C = −0.5）"""
    return (1 + FSRS_FACTOR * max(0, t) / max(s, 0.01)) ** FSRS_DECAY
