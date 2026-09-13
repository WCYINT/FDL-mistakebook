"""P2-05 前置依赖与门禁验收测试。

验证：依赖图构建、HARD/SOFT 两级门禁、环检测（Kahn 迭代）、
缺失前置 / 非法强度拦截、8 态等级序。
"""

from __future__ import annotations

import pytest

from fdl_core.notes.kp_card import KpCard
from fdl_core.notes.kp_gate import (
    GATE_THRESHOLD,
    STATUS_RANK,
    GateResult,
    PrereqCycleError,
    PrereqGraph,
    PrereqMissingError,
    PrereqStrengthError,
    build_gate,
)


def _card(code, prereqs=None):
    fm = {
        "type": "knowledge_point",
        "code": code,
        "subject": "MATH",
        "name": code,
        "grade_level": 4,
        "kp_type": "SKILL",
        "bloom_level": 2,
        "abstraction_level": 2,
        "importance_weight": 1.0,
        "base_difficulty": 4.8,
        "est_learn_minutes": 1.5,
        "est_review_seconds": 45,
        "tier": "L0",
        "graph_version": "2026.1",
        "valid_from": "2026-09-01",
        "knowledge_domain": "数与运算",
    }
    if prereqs:
        fm["prerequisites"] = [{"code": c, "strength": s} for c, s in prereqs]
    return KpCard(fm, "# body\n")


BASE = [
    _card("MATH-G4-FRAC-CMP"),  # 无前置
    _card("MATH-G4-FRAC-ADD", [("MATH-G4-FRAC-CMP", "HARD")]),
    _card("MATH-G4-FRAC-MIX", [("MATH-G4-FRAC-ADD", "HARD"), ("MATH-G4-EST", "SOFT")]),
    _card("MATH-G4-EST"),
]


# ── 构建与查询 ──────────────────────────────────────────────
def test_from_cards_builds_edges():
    g = build_gate(BASE)
    assert len(g.edges) == 3
    add_prereqs = g.prereqs_of("MATH-G4-FRAC-ADD")
    assert [(e.prereq_code, e.strength) for e in add_prereqs] == [("MATH-G4-FRAC-CMP", "HARD")]


def test_no_prereqs_returns_empty():
    g = build_gate(BASE)
    assert g.prereqs_of("MATH-G4-EST") == []


# ── 门禁（HARD/SOFT 两级）───────────────────────────────────
def test_gate_pass_when_hard_prereq_learning():
    g = build_gate(BASE)
    r = g.check_gate("MATH-G4-FRAC-ADD", {"MATH-G4-FRAC-CMP": "LEARNING"})
    assert r.allowed
    assert r.blockers == []


@pytest.mark.parametrize(
    "status",
    ["REVIEWING", "STRUGGLING", "REGRESSED", "MASTERED", "CONSOLIDATED", "ARCHIVED"],
)
def test_gate_pass_all_learned_statuses(status):
    """≥LEARNING 的 8 态全部放行（含 STRUGGLING/REGRESSED）。"""
    g = build_gate(BASE)
    assert g.check_gate("MATH-G4-FRAC-ADD", {"MATH-G4-FRAC-CMP": status}).allowed


def test_gate_blocks_unlearned_hard_prereq():
    g = build_gate(BASE)
    r = g.check_gate("MATH-G4-FRAC-ADD", {})  # 前置状态缺失 = UNLEARNED
    assert not r.allowed
    assert r.blockers == ["MATH-G4-FRAC-CMP"]


def test_soft_prereq_hints_not_blocks():
    g = build_gate(BASE)
    r = g.check_gate(
        "MATH-G4-FRAC-MIX",
        {"MATH-G4-FRAC-ADD": "MASTERED"},  # HARD 达标；SOFT 缺失
    )
    assert r.allowed  # SOFT 不阻断
    assert r.soft_hints == ["MATH-G4-EST"]
    assert r.blockers == []


def test_gate_mixed_blockers_and_hints():
    g = build_gate(BASE)
    r = g.check_gate("MATH-G4-FRAC-MIX", {"MATH-G4-EST": "UNLEARNED"})
    assert not r.allowed
    assert r.blockers == ["MATH-G4-FRAC-ADD"]  # HARD 缺失状态 → 阻断
    assert r.soft_hints == ["MATH-G4-EST"]


# ── 环检测（Kahn 迭代）──────────────────────────────────────
def test_cycle_raises_on_build():
    cards = [
        _card("MATH-A", [("MATH-B", "HARD")]),
        _card("MATH-B", [("MATH-A", "SOFT")]),
    ]
    with pytest.raises(PrereqCycleError):
        build_gate(cards)


def test_self_cycle_raises():
    cards = [_card("MATH-A", [("MATH-A", "HARD")])]
    with pytest.raises(PrereqCycleError):
        build_gate(cards)


def test_long_cycle_detected():
    """三节点长环 + 依赖环的节点：Kahn 报告环上 + 依赖环（无法拓扑排序者）。"""
    from fdl_core.notes.kp_gate import PrereqEdge

    g = PrereqGraph(
        [
            PrereqEdge("MATH-A", "MATH-C", "HARD"),
            PrereqEdge("MATH-B", "MATH-A", "HARD"),
            PrereqEdge("MATH-C", "MATH-B", "HARD"),
            PrereqEdge("MATH-D", "MATH-A", "SOFT"),  # 依赖环 → 门禁也被卡，一并报告
        ]
    )
    cyclic = g.detect_cycles()
    assert set(cyclic) == {"MATH-A", "MATH-B", "MATH-C", "MATH-D"}


def test_diamond_no_false_cycle():
    """菱形依赖（共同前置）不是环。"""
    cards = [
        _card("MATH-BASE"),
        _card("MATH-L", [("MATH-BASE", "HARD")]),
        _card("MATH-R", [("MATH-BASE", "HARD")]),
        _card("MATH-TOP", [("MATH-L", "HARD"), ("MATH-R", "HARD")]),
    ]
    g = build_gate(cards)
    assert g.detect_cycles() == []


# ── 数据完整性拦截 ──────────────────────────────────────────
def test_missing_prereq_target_raises():
    cards = [_card("MATH-X", [("MATH-不存在", "HARD")])]
    with pytest.raises(PrereqMissingError):
        build_gate(cards)


def test_missing_prereq_lenient_mode():
    cards = [_card("MATH-X", [("MATH-不存在", "HARD")])]
    g = PrereqGraph.from_cards(cards, strict=False)  # 宽松模式不抛
    assert g.prereqs_of("MATH-X")[0].prereq_code == "MATH-不存在"


def test_bad_strength_raises():
    cards = [_card("MATH-X", [("MATH-Y", "MEDIUM")]), _card("MATH-Y")]
    with pytest.raises(PrereqStrengthError):
        build_gate(cards)


# ── 等级序常量 ──────────────────────────────────────────────
def test_status_rank_ordering():
    assert STATUS_RANK["UNLEARNED"] == 0
    assert STATUS_RANK["LEARNING"] == GATE_THRESHOLD == 1
    assert STATUS_RANK["MASTERED"] > STATUS_RANK["REVIEWING"]
    assert STATUS_RANK["ARCHIVED"] == max(STATUS_RANK.values())


def test_gate_result_type():
    r = GateResult(kp_code="X", allowed=True)
    assert r.blockers == [] and r.soft_hints == []
