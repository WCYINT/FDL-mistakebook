"""掌握度三因子模型（P2-08 / SRS-03）。

🔴 乘法结构（PRD §3.2 SRS-03，单测锁定）：
    M_raw = R^(1−θ) × A^θ          θ=0.6（记忆 0.4 × 能力 0.6）
    A     = W_P·P + W_G·G          0.75P + 0.25G
    M_adj = Conf × M_raw           对外口径 + 状态机判定
    Conf  = n_eff / (n_eff + CONF_K)
    n_eff = n_real + m_eff，m_eff = M_PSEUDO_N · e^(−n_real/N_PRIOR_DECAY)
    （衰减式伪计数：冷启动拐杖，随真实作答退场）

锚点（PRD）："记住了但不会做"（R=0.9, A=0.2）→ M_raw ≈ 0.38；
"会做但忘了"（R=0.35, A=0.9）→ M_raw ≈ 0.62 —— 乘法下两者都不算掌握。

P（难度加权近期表现）：作答分 = GRADE_SCORE[g] × 难度系数(1+κ·(D−5)/5)，
按 λ^k 时间衰减加权，先验 PRIOR_P 以 m_eff 作虚拟样本数参与平均（衰减式先验）。
G（深度分）：G = G_BASELINE + 0.80 × (1 − e^(−raw))，行为按 60 天半衰期衰减。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from fdl_core.srs.params import ModelParams

DEFAULTS = ModelParams.load()


@dataclass
class MasteryComponents:
    """各因子分量（供 kp_state 冗余存储与调试）。"""

    r: float
    p: float
    g: float
    a: float
    confidence: float
    mastery_raw: float
    mastery_adj: float
    n_eff: float
    m_eff_pseudo: float


def pseudo_count(n_real: float, params: ModelParams = DEFAULTS) -> float:
    """衰减式伪计数 m_eff = M_PSEUDO_N · e^(−n/N_PRIOR_DECAY)。"""
    m = params.mastery
    return m["M_PSEUDO_N"] * math.exp(-max(n_real, 0.0) / m["N_PRIOR_DECAY"])


def effective_answers(n_real: float, params: ModelParams = DEFAULTS) -> float:
    """n_eff = n_real + m_eff（伪计数作虚拟样本）。"""
    return max(n_real, 0.0) + pseudo_count(n_real, params)


def confidence(n_real: float, params: ModelParams = DEFAULTS) -> float:
    """Conf = n_eff / (n_eff + CONF_K)。冷启动（n=0）→ 3/4.5 ≈ 0.667 先验置信。"""
    m = params.mastery
    n_eff = effective_answers(n_real, params)
    return n_eff / (n_eff + m["CONF_K"])


def ability(p: float, g: float, params: ModelParams = DEFAULTS) -> float:
    """A = W_P·P + W_G·G，clamp [0,1]。"""
    m = params.mastery
    a = m["W_P"] * p + m["W_G"] * g
    return min(max(a, 0.0), 1.0)


def mastery_raw(r: float, a: float, params: ModelParams = DEFAULTS) -> float:
    """M_raw = R^(1−θ) × A^θ（乘法结构，单测锁定）。"""
    theta = params.mastery["THETA"]
    r_c = min(max(r, 0.0), 1.0)
    a_c = min(max(a, 0.0), 1.0)
    return (r_c ** (1 - theta)) * (a_c**theta)


def mastery_adj(
    r: float,
    p: float,
    g: float,
    n_real: float,
    params: ModelParams = DEFAULTS,
) -> MasteryComponents:
    """完整三因子计算，返回全部分量。"""
    conf = confidence(n_real, params)
    a = ability(p, g, params)
    raw = mastery_raw(r, a, params)
    return MasteryComponents(
        r=r,
        p=p,
        g=g,
        a=a,
        confidence=conf,
        mastery_raw=raw,
        mastery_adj=conf * raw,  # 🔴 乘法：任一因子为 0 → 整体为 0
        n_eff=effective_answers(n_real, params),
        m_eff_pseudo=pseudo_count(n_real, params),
    )


# ── P：难度加权近期表现 ─────────────────────────────────────
def performance_score(
    answers: list[dict],
    params: ModelParams = DEFAULTS,
) -> float:
    """P = (Σ w_k·s_k + PRIOR_P·m_eff) / (Σ w_k + m_eff)。

    `answers`：按时间升序的作答列表 `[{"grade": 0-3, "difficulty": D, "seq": 距今序号}]`，
    seq=0 最新。难度系数对 grade≥1 的作答生效（难题做对加分更多；Again 基分 0 不受影响）。
    """
    m = params.mastery
    if not answers:
        return float(m["PRIOR_P"])
    kappa = m["KAPPA"]
    weighted = 0.0
    weights = 0.0
    for i, ans in enumerate(answers):
        base = float(m["GRADE_SCORE"].get(int(ans.get("grade", 0)), 0.0))
        difficulty = float(ans.get("difficulty", 5.0))
        factor = 1.0
        if base > 0:  # 难度加权只作用于得分作答
            factor = 1.0 + kappa * (difficulty - 5.0) / 5.0
        s = min(max(base * factor, 0.0), 1.2)
        w = m["LAMBDA"] ** i  # 越新权重越高
        weighted += w * s
        weights += w
    m_eff = pseudo_count(len(answers), params)
    prior = float(m["PRIOR_P"])
    return min(max((weighted + prior * m_eff) / (weights + m_eff), 0.0), 1.0)


# ── G：深度分 ──────────────────────────────────────────────
DEPTH_WEIGHTS = {
    "teach_back": 0.35,
    "question_raised": 0.30,
    "cross_link": 0.30,
    "variant_solved": 0.25,
    "self_explore": 0.25,
    "error_fixed": 0.15,
}


def depth_score(
    behaviors: list[dict],
    now,
    params: ModelParams = DEFAULTS,
) -> float:
    """G = G_BASELINE + 0.80 × (1 − e^(−raw))。

    `behaviors`：`[{"type": teach_back..., "days_ago": n}]`，单条贡献 = 权重 × 0.5^(days/60)。
    """
    m = params.mastery
    half_life = m["DEPTH_HALF_LIFE"]
    raw = 0.0
    for b in behaviors:
        w = DEPTH_WEIGHTS.get(b.get("type", ""), 0.0)
        days = float(b.get("days_ago", 0))
        raw += w * (0.5 ** (days / half_life))
    g = m["G_BASELINE"] + 0.80 * (1 - math.exp(-raw))
    return min(max(g, 0.0), 1.0)


# ── D 更新与 S 引导（供作答链路调用）────────────────────────
def update_difficulty(d: float, grade: int, params: ModelParams = DEFAULTS) -> float:
    """D ← D + D_UPDATE[grade]，再向先验 5.0 回归 D_ANCHOR_RATE，clamp [1,10]。"""
    m = params.mastery
    delta = float(m["D_UPDATE"].get(int(grade), 0.0))
    d_new = d + delta
    d_new = d_new + (5.0 - d_new) * m["D_ANCHOR_RATE"]
    return min(max(d_new, 1.0), 10.0)


def next_interval(stability: float, params: ModelParams = DEFAULTS) -> int:
    """计划间隔 = INTERVAL_FACTOR × S（天），向下取整至少 1。"""
    m = params.mastery
    return max(int(m["INTERVAL_FACTOR"] * max(stability, m["S_MIN"])), 1)
