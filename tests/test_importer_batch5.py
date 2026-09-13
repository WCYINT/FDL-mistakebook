"""批次 5（P2-21/22 KB 种子）验收测试。

覆盖：D₀ 自动计算、CSV 解析、导入落盘（含 schema 校验）、
重复 code 拦截、模板导出、scan type 过滤（题目卡不混入树）。
"""

from __future__ import annotations

import pytest

from fdl_core.notes.importer import (
    ImportError_,
    auto_base_difficulty,
    export_registry_template,
    import_registry,
    parse_registry,
)
from fdl_core.notes.kp_card import write_card
from fdl_core.notes.kp_tree import scan_kp_cards

CSV_HEADER = (
    "code,name,subject,grade_level,semester,grade_term,knowledge_domain,parent_code,"
    "kp_type,bloom_level,abstraction_level,importance_weight,exam_frequency,tier,"
    "source,source_ref,base_difficulty,question_stem,question_answer,question_type\n"
)


def _write_csv(tmp_path, rows: str) -> str:
    p = tmp_path / "kp-registry.csv"
    p.write_text(CSV_HEADER + rows, encoding="utf-8")
    return str(p)


# ── D₀ 自动计算（PRD §6.2.2 公式）──────────────────────────
def test_auto_difficulty_anchors():
    assert auto_base_difficulty(2, 4, 2) == 4.8  # PRD 示例：小数加减
    assert auto_base_difficulty(4, 4, 2) == 6.6  # 鸡兔同笼
    assert auto_base_difficulty(3, 5, 3) == 7.0  # 五年级一元一次方程
    assert auto_base_difficulty(6, 9, 3) == 10.0  # clamp 上限
    assert auto_base_difficulty(1, 1, 1) >= 1.0  # clamp 下限


# ── CSV 解析 ───────────────────────────────────────────────
def test_parse_registry_rows(tmp_path):
    p = _write_csv(
        tmp_path,
        "MATH-A,A,MATH,4,1,G4A,数与运算,,SKILL,2,2,1.0,4,L0,TEXTBOOK,四上 P2,,q?,a,FILL\n",
    )
    rows = parse_registry(p)
    assert len(rows) == 1 and rows[0]["code"] == "MATH-A"


def test_parse_registry_missing_file(tmp_path):
    with pytest.raises(ImportError_):
        parse_registry(tmp_path / "nope.csv")


def test_parse_registry_empty_rows_skipped(tmp_path):
    row = "MATH-A,A,MATH,4,1,G4A,数与运算,,SKILL,2,2,1.0,4,L0,TEXTBOOK,P2,,q,a,FILL\n"
    p = _write_csv(tmp_path, "\n" + row)
    assert len(parse_registry(p)) == 1


# ── 导入落盘 ───────────────────────────────────────────────
CSV_ROW = (
    "MATH-A,异分母分数加减,MATH,4,1,G4A,数与运算,,SKILL,2,2,1.0,4,L0,TEXTBOOK,四上 P89,,"
    "计算：3/7+2/7=,5/7,FILL\n"
)


def test_import_registry_writes_cards(tmp_path):
    p = _write_csv(tmp_path, CSV_ROW)
    result = import_registry(p, tmp_path)
    assert result == {"kp_imported": 1, "questions_imported": 1, "total": 1}
    kp = tmp_path / "01-知识点" / "G4A" / "G4A-异分母分数加减.md"
    assert kp.exists()
    q = tmp_path / "02-题目" / "Q-G4A-MATH-A.md"
    assert q.exists() and "3/7" in q.read_text(encoding="utf-8")


def test_import_auto_difficulty(tmp_path):
    """base_difficulty 留空 → 自动计算 4.8 并写入卡片。"""
    p = _write_csv(tmp_path, CSV_ROW)
    import_registry(p, tmp_path)
    kp_file = tmp_path / "01-知识点" / "G4A" / "G4A-异分母分数加减.md"
    card_text = kp_file.read_text(encoding="utf-8")
    assert "base_difficulty: 4.8" in card_text
    assert "est_learn_minutes: 1.5" in card_text  # v1.1 学校已教过


def test_import_duplicate_code_rejected(tmp_path):
    p = _write_csv(tmp_path, CSV_ROW + CSV_ROW)
    with pytest.raises(ImportError_, match="重复"):
        import_registry(p, tmp_path)


def test_import_invalid_row_rejected(tmp_path):
    from fdl_core.notes.kp_card import CardValidationError

    bad = "BAD,x,MATH,4,1,G4A,数与运算,,SKILL,9,2,1.0,4,L0,TEXTBOOK,P2,,q,a,FILL\n"
    p = _write_csv(tmp_path, bad)
    # bloom=9 超 schema 范围 + code 段数不足 → write_card schema 校验拦截
    with pytest.raises((ImportError_, CardValidationError)):
        import_registry(p, tmp_path)


def test_import_no_question_row(tmp_path):
    row = "MATH-B,B,MATH,4,1,G4A,数与运算,,SKILL,2,2,1.0,4,L0,TEXTBOOK,P2,,,,\n"
    p = _write_csv(tmp_path, row)
    result = import_registry(p, tmp_path)
    assert result["kp_imported"] == 1 and result["questions_imported"] == 0


def test_export_template(tmp_path):
    p = export_registry_template(tmp_path / "00-大纲" / "kp-registry.csv")
    assert p.exists()
    assert "code,name,subject" in p.read_text(encoding="utf-8-sig")


# ── P2-22 联动：scan 过滤（题目卡不混入知识点树）──────────────
def test_scan_filters_question_cards(tmp_path):
    from fdl_core.notes.kp_card import KpCard

    write_card(
        tmp_path / "kp.md",
        KpCard(
            {
                "type": "knowledge_point",
                "code": "MATH-K",
                "subject": "MATH",
                "name": "K",
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
            },
            "# K\n",
        ),
    )
    # 题目卡（无 name）混在同一目录
    qcard = KpCard({"type": "question", "code": "Q-MATH-K", "stem": "s"}, "# q\n")
    (tmp_path / "q.md").write_text(qcard.to_text(), encoding="utf-8")
    cards = scan_kp_cards(tmp_path)
    assert len(cards) == 1 and cards[0].frontmatter["code"] == "MATH-K"
