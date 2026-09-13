"""多科地基单测（Phase 1 · 2026-09-12）。

覆盖四项交付：
1. subject 自动新增：归一化 / 幂等 / code 去重 / 名称撞车复用 / canonical 配色
2. kp_relation：CHECK（无向边规范化）/ UNIQUE / FK
3. 反向导出：只补缺卡（保护人工编辑）/ schema 校验 / grade_term 推导
4. 接线往返：DB ↔ 卡片 幂等 + **DB-only 字段不丢失**（回归防护，见下）

🔴 回归防护重点：`sync_kp_to_db` 原用 `INSERT OR REPLACE`（SQLite 语义 = DELETE
+ INSERT），未列出的列被重置 → 实测会清空 `knowledge_domain` / `description` /
`source_ref` / `tier_reason`。已改为 UPDATE + COALESCE。本文件锁死该行为。
"""

from __future__ import annotations

import sqlite3

import pytest

from fdl_core.db.schema import create_schema, get_connection
from fdl_core.notes import db_sync
from fdl_core.notes import kp_classifier as kc
from fdl_core.notes.kp_card import KpCard, read_card, write_card


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "ms.db"
    conn = get_connection(str(p))
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (user_id, code, name, short_name, color_hex,"
        " rotation_weight, grade_start, grade_end, sort_order) VALUES"
        " (1,'MATH','数学','数','#0E7C7B',0.35,4,9,1)"
    )
    conn.commit()
    yield conn
    conn.close()


def _add_kp(conn, code, name, **over):
    base = {
        "subject_id": 1,
        "code": code,
        "name": name,
        "grade_level": 4,
        "bloom_level": 2,
        "abstraction_level": 2,
        "importance_weight": 1.0,
        "base_difficulty": 5.0,
        "est_learn_minutes": 1.5,
        "est_review_seconds": 45,
        "kp_type": "SKILL",
        "tier": "L0",
        "graph_version": "g1",
        "valid_from": "2026-09-01",
    }
    base.update(over)
    cols = ", ".join(base)
    ph = ", ".join("?" for _ in base)
    cur = conn.execute(f"INSERT INTO knowledge_point ({cols}) VALUES ({ph})", list(base.values()))
    conn.commit()
    return cur.lastrowid


# ── 1. subject 自动新增 ─────────────────────────────────────
def test_normalize_subject_code():
    assert kc.normalize_subject_code("math") == "MATH"
    assert kc.normalize_subject_code("CHN") == "CHINESE"
    assert kc.normalize_subject_code("语文") == "CHINESE"
    assert kc.normalize_subject_code(" sci ") == "SCIENCE"
    assert kc.normalize_subject_code("EN") == "ENGLISH"
    assert kc.normalize_subject_code("") == ""


def test_normalize_domain():
    """领域名归一到课标名（历史卡片的'数与运算'→'数与代数'）。"""
    assert kc.normalize_domain("数与运算") == "数与代数"
    assert kc.normalize_domain(" 图形 ") == "图形与几何"
    assert kc.normalize_domain("概率统计") == "统计与概率"
    assert kc.normalize_domain("图形与几何") == "图形与几何"  # 已规范 → 原样
    assert kc.normalize_domain(None) is None
    assert kc.normalize_domain("") is None


def test_ensure_subject_creates_with_canonical_meta(db):
    r = kc.ensure_subject(db, "ENGLISH")
    assert r["created"] is True
    row = db.execute(
        "SELECT code, name, short_name, color_hex, sort_order FROM subject WHERE id=?",
        (r["subject_id"],),
    ).fetchone()
    assert row == ("ENGLISH", "英语", "英", "#5B8DBE", 3)


def test_ensure_subject_dedup_by_code(db):
    r1 = kc.ensure_subject(db, "ENGLISH")
    r2 = kc.ensure_subject(db, "english")  # 小写
    r3 = kc.ensure_subject(db, "ENG")  # 别名
    assert r1["subject_id"] == r2["subject_id"] == r3["subject_id"]
    assert r2["created"] is False and r3["created"] is False
    n = db.execute("SELECT COUNT(*) FROM subject WHERE code='ENGLISH'").fetchone()[0]
    assert n == 1


def test_ensure_subject_dedup_by_name(db):
    """不同 code 但同中文名 → 复用既有行，不重复建（防'语文/CHINESE'并存）。"""
    r1 = kc.ensure_subject(db, "MORAL", name="道德与法治")
    r2 = kc.ensure_subject(db, "CIVICS", name="道德与法治")
    assert r1["created"] is True
    assert r2["created"] is False
    assert r2["subject_id"] == r1["subject_id"]
    assert r2["source"] == "EXISTING_BY_NAME"
    assert db.execute("SELECT COUNT(*) FROM subject WHERE name='道德与法治'").fetchone()[0] == 1


def test_ensure_subject_sort_order_no_collision(db):
    """canonical 的 sort_order 被占用时顺延，不撞号。"""
    kc.ensure_subject(db, "MORAL", name="道德与法治")  # 占 sort=2
    r = kc.ensure_subject(db, "CHINESE")  # canonical sort=2 被占
    n = db.execute("SELECT COUNT(*) FROM subject WHERE sort_order=?", (2,)).fetchone()[0]
    assert n == 1, "sort_order 不应撞号"
    assert (
        db.execute("SELECT sort_order FROM subject WHERE id=?", (r["subject_id"],)).fetchone()[0]
        > 2
    )


def test_ensure_canonical_subjects_idempotent(db):
    r1 = kc.ensure_canonical_subjects(db)
    assert set(r1["existing"]) == {"MATH"}
    assert set(r1["created"]) == {"CHINESE", "ENGLISH", "SCIENCE"}
    r2 = kc.ensure_canonical_subjects(db)
    assert r2["created"] == []
    assert db.execute("SELECT COUNT(*) FROM subject").fetchone()[0] == 4


# ── 2. kp_relation ─────────────────────────────────────────
def test_kp_relation_check_normalizes_direction(db):
    a = _add_kp(db, "MATH-G4-A", "A")
    b = _add_kp(db, "MATH-G4-B", "B")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO kp_relation (from_kp_id, to_kp_id, relation_type) VALUES (?,?,?)",
            (b, a, "ANALOGY"),
        )


def test_kp_relation_unique_and_types(db):
    a = _add_kp(db, "MATH-G4-A", "A")
    b = _add_kp(db, "MATH-G4-B", "B")
    db.execute(
        "INSERT INTO kp_relation (from_kp_id, to_kp_id, relation_type) VALUES (?,?,?)",
        (a, b, "ANALOGY"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO kp_relation (from_kp_id, to_kp_id, relation_type) VALUES (?,?,?)",
            (a, b, "ANALOGY"),
        )
    # 不同 relation_type 可共存
    db.execute(
        "INSERT INTO kp_relation (from_kp_id, to_kp_id, relation_type) VALUES (?,?,?)",
        (a, b, "SHARED_METHOD"),
    )
    assert db.execute("SELECT COUNT(*) FROM kp_relation").fetchone()[0] == 2


def test_kp_relation_fk_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO kp_relation (from_kp_id, to_kp_id, relation_type)"
            " VALUES (999001, 999002, 'ANALOGY')",
        )


# ── 3. 反向导出 ────────────────────────────────────────────
def test_export_only_missing_protects_existing(db, tmp_path):
    _add_kp(db, "MATH-G4-HAS-CARD", "已有卡")
    _add_kp(db, "MATH-G4-NO-CARD", "无卡")
    sd = tmp_path / "01-知识点"
    # 先为 HAS-CARD 写一张卡（体内容是人工标记）
    write_card(
        sd / "G4A" / "MATH-G4-HAS-CARD.md",
        KpCard(
            frontmatter={
                "type": "knowledge_point",
                "code": "MATH-G4-HAS-CARD",
                "subject": "MATH",
                "name": "已有卡",
                "grade_level": 4,
                "semester": 1,
                "grade_term": "G4A",
                "kp_type": "SKILL",
                "bloom_level": 2,
                "abstraction_level": 2,
                "importance_weight": 1.0,
                "base_difficulty": 5.0,
                "est_learn_minutes": 1.5,
                "est_review_seconds": 45,
                "tier": "L0",
                "graph_version": "g1",
                "valid_from": "2026-09-01",
            },
            body="# 已有卡\n\n人工写的重要说明，不能丢\n",
        ),
    )
    r = db_sync.export_kp_to_cards(db, "MATH", subject_dir=sd)
    assert r["exported"] == 1 and r["skipped"] == 1
    # 人工内容必须原样保留
    card = read_card(sd / "G4A" / "MATH-G4-HAS-CARD.md")
    assert "人工写的重要说明" in card.body


def test_export_grade_term_derivation(db, tmp_path):
    _add_kp(db, "MATH-G3-X", "三年级", grade_level=3)
    _add_kp(db, "MATH-G4-Y", "四年级下", source_ref="人教版四年级下册数学广角")
    sd = tmp_path / "01-知识点"
    db_sync.export_kp_to_cards(db, "MATH", subject_dir=sd)
    # 文件命名遵循统一规范（编码-描述.md，见 fdl_core/notes/naming.py）
    from fdl_core.notes.naming import card_filename

    assert (sd / "G3A" / card_filename("MATH-G3-X", "三年级")).exists(), "三年级默认上学期"
    assert (sd / "G4B" / card_filename("MATH-G4-Y", "四年级下")).exists(), (
        "source_ref 含'下册' → G4B"
    )


def test_export_llm_extracted_tagged(db, tmp_path):
    _add_kp(db, "MATH-G4-C", "LLM提炼", source="CUSTOM")
    sd = tmp_path / "01-知识点"
    db_sync.export_kp_to_cards(db, "MATH", subject_dir=sd)
    from fdl_core.notes.naming import card_filename

    card = read_card(sd / "G4A" / card_filename("MATH-G4-C", "LLM提炼"))
    assert "LLM-EXTRACTED" in card.frontmatter.get("tags", [])


# ── 4. 接线往返（含回归防护）──────────────────────────────
def test_sync_preserves_db_only_fields(db, tmp_path):
    """🔴 回归防护：卡片未提供的字段（LLM/人工写进 DB 的）不得被清空。"""
    kp_id = _add_kp(db, "MATH-G4-P", "保字段", grade_level=4)
    sd = tmp_path / "01-知识点"
    db_sync.export_kp_to_cards(db, "MATH", subject_dir=sd)
    # 模拟分类器/人工写入 DB-only 字段
    db.execute(
        "UPDATE knowledge_point SET knowledge_domain='数与代数', description='人工描述' WHERE id=?",
        (kp_id,),
    )
    db.commit()
    db_sync.sync_kp_to_db(db, sd)  # 卡片里 domain/description 为 null
    row = db.execute(
        "SELECT knowledge_domain, description FROM knowledge_point WHERE id=?",
        (kp_id,),
    ).fetchone()
    assert row == ("数与代数", "人工描述"), "INSERT OR REPLACE 的数据丢失 bug 不得回归"


def test_sync_roundtrip_idempotent(db, tmp_path):
    _add_kp(db, "MATH-G4-R1", "往返1")
    _add_kp(db, "MATH-G4-R2", "往返2")
    sd = tmp_path / "01_知识点"
    db_sync.export_kp_to_cards(db, "MATH", subject_dir=sd)
    db_sync.sync_kp_to_db(db, sd)
    snap1 = db.execute("SELECT * FROM knowledge_point ORDER BY id").fetchall()
    db_sync.sync_kp_to_db(db, sd)  # 再同步一次
    snap2 = db.execute("SELECT * FROM knowledge_point ORDER BY id").fetchall()
    assert snap1 == snap2, "往返必须幂等（两次同步结果一致）"
    assert db.execute("SELECT COUNT(*) FROM knowledge_point").fetchone()[0] == 2


def test_sync_card_domain_flows_to_db(db, tmp_path):
    """卡片提供的 domain 应写入 DB（首条真实数据流）。"""
    kp_id = _add_kp(db, "MATH-G4-D", "带域")
    sd = tmp_path / "01_知识点"
    write_card(
        sd / "G4A" / "MATH-G4-D.md",
        KpCard(
            frontmatter={
                "type": "knowledge_point",
                "code": "MATH-G4-D",
                "subject": "MATH",
                "name": "带域",
                "grade_level": 4,
                "semester": 1,
                "grade_term": "G4A",
                "knowledge_domain": "图形与几何",
                "kp_type": "SKILL",
                "bloom_level": 2,
                "abstraction_level": 2,
                "importance_weight": 1.0,
                "base_difficulty": 5.0,
                "est_learn_minutes": 1.5,
                "est_review_seconds": 45,
                "tier": "L0",
                "graph_version": "g1",
                "valid_from": "2026-09-01",
            },
            body="# 带域\n",
        ),
    )
    db_sync.sync_kp_to_db(db, sd)
    assert (
        db.execute("SELECT knowledge_domain FROM knowledge_point WHERE id=?", (kp_id,)).fetchone()[
            0
        ]
        == "图形与几何"
    )
