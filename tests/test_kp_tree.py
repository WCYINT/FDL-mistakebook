"""P2-04 知识点树验收测试。

验证：扫描、≥3 层构树、孤儿容错、环检测、导出格式、
端到端写 kp-tree.md、性能冒烟（500 节点 O(n)）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fdl_core.notes.kp_card import write_card
from fdl_core.notes.kp_tree import (
    KpTreeError,
    build_tree,
    count_nodes,
    export_tree_md,
    scan_kp_cards,
    write_kp_tree,
)

_BODY = "# 占位\n"


def _fm(code: str, name: str, *, domain="数与运算", parent=None, subject="MATH", gt="G4A"):
    return {
        "type": "knowledge_point",
        "code": code,
        "subject": subject,
        "name": name,
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
        "knowledge_domain": domain,
        "grade_term": gt,
        "parent_code": parent,
    }


def _card(fm: dict):
    """构造 KpCard（绕过 schema 校验直接组对象）。"""
    from fdl_core.notes.kp_card import KpCard

    return KpCard(frontmatter=fm, body=_BODY)


def _tree(fms: list[dict]) -> dict:
    """dict frontmatter 列表 → build_tree（测试便捷入口）。"""
    return build_tree([_card(fm) for fm in fms])


def _write_cards(tmp_path: Path, cards: list[dict]) -> Path:
    d = tmp_path / "1-Math"
    for i, fm in enumerate(cards):
        gt = fm.get("grade_term", "G4A")
        write_card(d / "01-知识点" / gt / f"{i:03d}-{fm['code']}.md", _card(fm))
    return d


# ── 扫描 ───────────────────────────────────────────────────
def test_scan_finds_cards(tmp_path):
    cards = [_fm("MATH-G4-A", "A"), _fm("MATH-G4-B", "B", domain="图形与几何")]
    d = _write_cards(tmp_path, cards)
    found = scan_kp_cards(d)
    assert len(found) == 2
    assert {c.frontmatter["code"] for c in found} == {"MATH-G4-A", "MATH-G4-B"}


def test_scan_skips_non_card_files(tmp_path):
    d = tmp_path / "1-Math"
    kp = d / "01-知识点" / "G4A"
    kp.mkdir(parents=True)
    (kp / "README.md").write_text("# 不是卡片\n", encoding="utf-8")  # 无 frontmatter
    assert scan_kp_cards(d) == []


def test_scan_missing_dir_returns_empty(tmp_path):
    assert scan_kp_cards(tmp_path / "不存在") == []


# ── 构树（≥3 层）───────────────────────────────────────────
def test_build_tree_three_levels():
    cards = [
        _fm("MATH-G4-FRACTION", "分数"),
        _fm("MATH-G4-FRAC-ADD", "异分母分数加减", parent="MATH-G4-FRACTION"),
        _fm("MATH-G4-FRAC-ADD-MIX", "带分数加减", parent="MATH-G4-FRAC-ADD"),
    ]
    tree = _tree(cards)
    # 第 1 层：域；第 2 层：根知识点；第 3、4 层：子、孙
    root = tree["数与运算"][0]
    assert root.code == "MATH-G4-FRACTION"
    child = root.children[0]
    assert child.code == "MATH-G4-FRAC-ADD"
    grandchild = child.children[0]
    assert grandchild.code == "MATH-G4-FRAC-ADD-MIX"


def test_orphan_parent_as_root():
    cards = [
        _fm("MATH-G4-X", "X", parent="MATH-G4-不存在"),
        _fm("MATH-G4-Y", "Y"),
    ]
    tree = _tree(cards)
    codes = {n.code for n in tree["数与运算"]}
    # 孤儿 X 与正常根 Y 都作为根
    assert codes == {"MATH-G4-X", "MATH-G4-Y"}


def test_cycle_detected():
    cards = [
        _fm("MATH-G4-A", "A", parent="MATH-G4-B"),
        _fm("MATH-G4-B", "B", parent="MATH-G4-A"),
        _fm("MATH-G4-C", "C"),
    ]
    with pytest.raises(KpTreeError, match="成环"):
        _tree(cards)


def test_duplicate_code_rejected():
    cards = [_fm("MATH-G4-A", "A"), _fm("MATH-G4-A", "又A")]
    with pytest.raises(KpTreeError, match="重复"):
        _tree(cards)


# ── 导出 ───────────────────────────────────────────────────
def test_export_md_contains_structure(tmp_path):
    cards = [
        _fm("MATH-G4-FRACTION", "分数"),
        _fm("MATH-G4-FRAC-ADD", "异分母分数加减", parent="MATH-G4-FRACTION"),
        _fm("MATH-G4-ANGLE", "角的度量", domain="图形与几何"),
    ]
    tree = _tree(cards)
    md = export_tree_md(tree, "MATH")
    assert "# MATH 知识点树" in md
    assert "## 数与运算（2）" in md
    assert "## 图形与几何（1）" in md
    assert "`MATH-G4-FRAC-ADD`" in md
    assert "[G4A] **异分母分数加减**" in md


def test_count_nodes():
    cards = [
        _fm("MATH-G4-F", "F"),
        _fm("MATH-G4-F-1", "F1", parent="MATH-G4-F"),
        _fm("MATH-G4-F-1-2", "F1-2", parent="MATH-G4-F-1"),
    ]
    tree = _tree(cards)
    assert count_nodes(tree["数与运算"]) == 3


# ── 端到端 ─────────────────────────────────────────────────
def test_write_kp_tree_end_to_end(tmp_path):
    cards = [
        _fm("MATH-G4-FRACTION", "分数"),
        _fm("MATH-G4-FRAC-ADD", "异分母分数加减", parent="MATH-G4-FRACTION"),
    ]
    d = _write_cards(tmp_path, cards)
    out = write_kp_tree(d)
    assert out == d / "00-大纲" / "kp-tree.md"
    text = out.read_text(encoding="utf-8")
    assert "MATH 知识点树" in text
    assert "共 2 个知识点" in text


def test_write_kp_tree_empty_dir(tmp_path):
    d = tmp_path / "空学科"
    out = write_kp_tree(d)
    assert "共 0 个知识点" in out.read_text(encoding="utf-8")


# ── 性能冒烟（O(n)，500 节点 < 1s）─────────────────────────
def test_build_tree_perf_smoke():
    cards = []
    for i in range(500):
        parent = f"MATH-G4-N{i - 1}" if i > 0 else None
        cards.append(_fm(f"MATH-G4-N{i}", f"节点{i}", parent=parent))
    t0 = time.perf_counter()
    tree = _tree(cards)
    elapsed = time.perf_counter() - t0
    assert len(tree["数与运算"]) == 1  # 单链
    assert count_nodes(tree["数与运算"]) == 500
    assert elapsed < 1.0, f"500 节点构建耗时 {elapsed:.2f}s，疑似退化"


# ── 回归：P2-03 往返不受影响 ───────────────────────────────
def test_kp_card_still_roundtrips(tmp_path):
    fm = _fm("MATH-G4-R", "回归")
    d = _write_cards(tmp_path, [fm])
    found = scan_kp_cards(d)
    assert found[0].frontmatter == fm
