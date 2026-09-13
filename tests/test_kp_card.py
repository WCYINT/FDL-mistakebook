"""P2-03 知识点卡片读写验收测试。

验证：frontmatter 往返无损、schema 校验（通过/拦截）、
🔴 硬验收「改 YAML（schema）不改代码」、日期字面量规范化。
"""

from __future__ import annotations

import json

import pytest

from fdl_core.notes.frontmatter import CardFormatError, join_card, split_card
from fdl_core.notes.kp_card import (
    CardValidationError,
    KpCard,
    default_schema_path,
    read_card,
    validate_card,
    write_card,
)
from fdl_core.notes.schema_validator import validate

VALID_FM = {
    "type": "knowledge_point",
    "id": 10241,
    "code": "MATH-G4-FRAC-ADD",
    "subject": "MATH",
    "name": "异分母分数加减",
    "grade_level": 4,
    "semester": 1,
    "grade_term": "G4A",
    "knowledge_domain": "数与运算",
    "parent_code": "MATH-G4-FRACTION",
    "kp_type": "SKILL",
    "bloom_level": 2,
    "abstraction_level": 2,
    "importance_weight": 1.0,
    "exam_frequency": 4,
    "base_difficulty": 4.8,
    "est_learn_minutes": 1.5,
    "est_review_seconds": 45,
    "tier": "L0",
    "tier_reason": "考纲明确要求 且 考频4",
    "source": "TEXTBOOK",
    "source_ref": "人教版四上 P89",
    "prerequisites": [
        {"code": "MATH-G4-FRAC-CMP", "strength": "HARD"},
        {"code": "MATH-G4-FRAC-REDUCE", "strength": "HARD"},
    ],
    "graph_version": "2026.1",
    "valid_from": "2026-09-01",
    "valid_to": None,
    "superseded_by": None,
    "tags": ["分数", "运算"],
}

VALID_BODY = """# 异分母分数加减

## 是什么
分母不同的分数相加减，要先**通分**，再按同分母法则计算。

## 为什么（探究钩子）
1/2 + 1/3 如果直接加得到 2/5，但实际 ≈ 0.83。
"""


# ── frontmatter 解析 / 往返无损 ─────────────────────────────
def test_split_join_roundtrip():
    fm, body = split_card(join_card(VALID_FM, VALID_BODY))
    assert fm == VALID_FM
    assert body == VALID_BODY


def test_kp_card_roundtrip_text():
    c1 = KpCard.from_text(join_card(VALID_FM, VALID_BODY))
    c2 = KpCard.from_text(c1.to_text())
    assert c1.frontmatter == c2.frontmatter
    assert c1.body == c2.body


def test_write_read_roundtrip(tmp_path):
    card = KpCard(frontmatter=dict(VALID_FM), body=VALID_BODY)
    p = write_card(tmp_path / "G4A" / "G4A-异分母分数加减.md", card)
    loaded = read_card(p)
    assert loaded.frontmatter == card.frontmatter
    assert loaded.body == card.body


def test_yaml_date_literal_normalized():
    """`valid_from: 2026-09-01`（YAML 日期字面量）→ ISO 字符串。"""
    text = "---\ntype: knowledge_point\nvalid_from: 2026-09-01\n---\n\n正文"
    fm, body = split_card(text)
    assert fm["valid_from"] == "2026-09-01"
    assert body == "正文\n"  # 规范化：前导空行去除、尾部恰一个换行


def test_split_missing_delimiter_raises():
    with pytest.raises(CardFormatError):
        split_card("没有分隔符的文本")


def test_split_unclosed_raises():
    with pytest.raises(CardFormatError):
        split_card("---\ntype: knowledge_point\n但未闭合")


def test_split_non_mapping_raises():
    with pytest.raises(CardFormatError):
        split_card("---\n只是一个字符串\n---\n正文")


# ── schema 校验 ─────────────────────────────────────────────
def test_valid_card_passes_schema():
    errors = validate_card(KpCard(VALID_FM, VALID_BODY))
    assert errors == []


def test_missing_required_field_fails():
    fm = {k: v for k, v in VALID_FM.items() if k != "bloom_level"}
    errors = validate_card(KpCard(fm, VALID_BODY))
    assert any("bloom_level" in e for e in errors)


def test_bad_enum_fails():
    fm = dict(VALID_FM, subject="PHYSICS")
    errors = validate_card(KpCard(fm, VALID_BODY))
    assert any("subject" in e for e in errors)


def test_out_of_range_fails():
    fm = dict(VALID_FM, bloom_level=9)
    errors = validate_card(KpCard(fm, VALID_BODY))
    assert any("bloom_level" in e for e in errors)


def test_wrong_type_fails():
    fm = dict(VALID_FM, grade_level="四")
    errors = validate_card(KpCard(fm, VALID_BODY))
    assert any("grade_level" in e for e in errors)


def test_bad_code_pattern_fails():
    fm = dict(VALID_FM, code="not a code")
    errors = validate_card(KpCard(fm, VALID_BODY))
    assert any("code" in e for e in errors)


def test_write_invalid_card_blocked(tmp_path):
    fm = dict(VALID_FM, kp_type="WRONG")
    card = KpCard(fm, VALID_BODY)
    with pytest.raises(CardValidationError):
        write_card(tmp_path / "bad.md", card)
    assert not (tmp_path / "bad.md").exists()  # 拦截后不落盘


# ── 🔴 硬验收：改 YAML（schema）不改代码 ─────────────────────
def test_change_schema_without_code_change(tmp_path):
    """修改 schema JSON 的约束 → 校验行为变化，全程零代码改动。"""
    schema = {
        "type": "object",
        "required": ["type", "code", "grade_level"],
        "properties": {
            "type": {"const": "knowledge_point"},
            "code": {"type": "string"},
            "grade_level": {"type": "integer", "minimum": 1, "maximum": 6},
        },
    }
    sp = tmp_path / "schema.json"
    sp.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")

    # grade_level=4 在 max=6 内 → 通过
    ok = {k: v for k, v in VALID_FM.items() if k in ("type", "code", "grade_level")}
    assert validate(ok, json.loads(sp.read_text())) == []

    # 🔴 只改 schema JSON（max 6→3），不改任何代码 → grade_level=4 被拦截
    schema["properties"]["grade_level"]["maximum"] = 3
    sp.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
    errors = validate(ok, json.loads(sp.read_text()))
    assert any("grade_level" in e for e in errors)


# ── 真实 schema 文件 ────────────────────────────────────────
def test_real_schema_file_exists_and_validates():
    sp = default_schema_path()
    assert sp.exists()
    schema = json.loads(sp.read_text(encoding="utf-8"))
    assert schema["title"].startswith("FDL 知识点卡片")
    assert validate(VALID_FM, schema) == []
