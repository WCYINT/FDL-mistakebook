"""Phase 2 批次改写单测（2026-09-12 · King 四项需求）。

覆盖：
1. `detect_olympiad` 双信号：文本（source_ref/description/name）+ 候选血缘
   （description "由 LLM 候选 #N 晋级" → kp_candidate.sample_json）
2. 导出落桶：奥数 KP 的卡片导出到 `01-知识点/小A/`（普通卡不受影响）
3. `normalize_domain_vocabulary`：DB + 卡片双侧课标化；dry-run 不写；幂等
4. `retag_olympiad_cards`：grade_term → "小A" + 文件移动；幂等；缺卡进 missing
5. `backfill_card_domains`：仅填空不覆盖；DB 空则跳过；dry-run 不写
"""

from __future__ import annotations

import pytest

from fdl_core.db.schema import create_schema, get_connection
from fdl_core.notes import db_sync
from fdl_core.notes import kp_maintenance as km
from fdl_core.notes.kp_card import KpCard, read_card, write_card


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "p2.db"
    conn = get_connection(str(p))
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (user_id, code, name, short_name, color_hex,"
        " rotation_weight, grade_start, grade_end, sort_order) VALUES"
        " (1,'MATH','数学','数','#0E7C7B',0.35,4,9,1)"
    )
    conn.execute(
        "INSERT INTO subject (user_id, code, name, short_name, color_hex,"
        " rotation_weight, grade_start, grade_end, sort_order) VALUES"
        " (1,'CHINESE','语文','语','#C0392B',0.30,4,9,2)"
    )
    conn.commit()
    yield conn
    conn.close()


def _add_kp(conn, code, name, subject_id=1, **over):
    base = {
        "subject_id": subject_id,
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
        "tier": "L1",
        "graph_version": "g1",
        "valid_from": "2026-09-01",
    }
    base.update(over)
    cols = ", ".join(base)
    ph = ", ".join("?" for _ in base)
    cur = conn.execute(f"INSERT INTO knowledge_point ({cols}) VALUES ({ph})", list(base.values()))
    conn.commit()
    return cur.lastrowid


def _card_fm(code, name, **over):
    fm = {
        "type": "knowledge_point",
        "code": code,
        "subject": "MATH",
        "name": name,
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
    }
    fm.update(over)
    return fm


# ── 1. detect_olympiad ───────────────────────────────────────
def test_detect_olympiad_text_signal(db):
    assert db_sync.detect_olympiad(db, code="X", source_ref="深中奥数小A P36 数表") is True
    assert db_sync.detect_olympiad(db, code="X", name="奥数专题·数字谜") is True
    assert db_sync.detect_olympiad(db, code="X", source_ref="北师大版四上 P80") is False
    assert db_sync.detect_olympiad(db, code="X", description="由 LLM 候选 #1 晋级") is False


def test_detect_olympiad_lineage_signal(db):
    cur = db.execute(
        "INSERT INTO kp_candidate (candidate_code, candidate_name, sample_json)"
        " VALUES ('MATH-G4-PATTERN-TABLE','数表中的规律',"
        ' \'[{"mistake_id":1,"source_ref":"深中奥数小A P21 数表找规律 题5"}]\')'
    )
    cid = cur.lastrowid
    db.commit()
    assert (
        db_sync.detect_olympiad(
            db,
            code="MATH-G4-PATTERN-TABLE",
            description=f"由 LLM 候选 #{cid} 晋级（decided_by=FRANK）",
        )
        is True
    )
    # 血缘到非奥数候选 → False
    cur2 = db.execute(
        "INSERT INTO kp_candidate (candidate_code, candidate_name, sample_json)"
        " VALUES ('MATH-G3-LENGTH-CONVERT','长度单位换算',"
        ' \'[{"mistake_id":2,"source_ref":"三上知训 P7 长度单位换算"}]\')'
    ).lastrowid
    db.commit()
    assert (
        db_sync.detect_olympiad(
            db,
            code="MATH-G3-LENGTH-CONVERT",
            description=f"由 LLM 候选 #{cur2} 晋级（decided_by=FRANK）",
        )
        is False
    )


# ── 2. 导出落桶 ──────────────────────────────────────────────
def test_export_olympiad_lands_in_xiaoa(db, tmp_path):
    _add_kp(db, "MATH-G4-OLY-A", "数表规律", source_ref="深中奥数小A P36 数表 36-11")
    _add_kp(db, "MATH-G4-PLAIN", "三角形内角和", source_ref="北师大版四上 P80")
    sd = tmp_path / "01-知识点"
    db_sync.export_kp_to_cards(db, "MATH", subject_dir=sd)
    # 文件命名遵循统一规范（编码-描述.md）
    from fdl_core.notes.naming import card_filename

    oly = card_filename("MATH-G4-OLY-A", "数表规律")
    plain = card_filename("MATH-G4-PLAIN", "三角形内角和")
    assert (sd / "小A" / oly).exists(), "奥数卡须落 小A 桶"
    assert (sd / "G4A" / plain).exists(), "普通卡仍按 grade_term"
    fm = read_card(sd / "小A" / oly).frontmatter
    assert fm["grade_term"] == "小A"


# ── 3. 领域术语课标化 ────────────────────────────────────────
def test_normalize_domain_vocabulary_db_and_card(db, tmp_path):
    kp_id = _add_kp(db, "MATH-G4-MUL-COMM", "乘法交换律", knowledge_domain="数与运算")
    sd = tmp_path / "01-知识点"
    write_card(
        sd / "G4A" / "MATH-G4-MUL-COMM.md",
        KpCard(
            frontmatter=_card_fm("MATH-G4-MUL-COMM", "乘法交换律", knowledge_domain="数与运算"),
            body="# 乘法交换律\n",
        ),
    )
    r = km.normalize_domain_vocabulary(
        db,
        subjects=("MATH",),
        kp_dirs={"MATH": sd},
    )
    assert r["db_updated"] == 1 and len(r["cards_updated"]) == 1
    assert (
        db.execute("SELECT knowledge_domain FROM knowledge_point WHERE id=?", (kp_id,)).fetchone()[
            0
        ]
        == "数与代数"
    )
    assert (
        read_card(sd / "G4A" / "MATH-G4-MUL-COMM.md").frontmatter["knowledge_domain"] == "数与代数"
    )

    # 幂等：再跑一次没有新改写
    r2 = km.normalize_domain_vocabulary(db, subjects=("MATH",), kp_dirs={"MATH": sd})
    assert r2["db_updated"] == 0 and r2["cards_updated"] == []


def test_normalize_domain_dry_run_no_write(db, tmp_path):
    kp_id = _add_kp(db, "MATH-G4-X", "旧域", knowledge_domain="数与运算")
    sd = tmp_path / "01-知识点"
    write_card(
        sd / "G4A" / "MATH-G4-X.md",
        KpCard(
            frontmatter=_card_fm("MATH-G4-X", "旧域", knowledge_domain="数与运算"), body="# 旧域\n"
        ),
    )
    r = km.normalize_domain_vocabulary(
        db,
        subjects=("MATH",),
        kp_dirs={"MATH": sd},
        dry_run=True,
    )
    assert r["db_updated"] == 1  # 报告计数
    assert (
        db.execute("SELECT knowledge_domain FROM knowledge_point WHERE id=?", (kp_id,)).fetchone()[
            0
        ]
        == "数与运算"
    ), "dry-run 不得写库"
    assert read_card(sd / "G4A" / "MATH-G4-X.md").frontmatter["knowledge_domain"] == "数与运算", (
        "dry-run 不得写卡"
    )


# ── 4. 奥数卡学期桶 ──────────────────────────────────────────
def test_retag_olympiad_moves_card(db, tmp_path):
    _add_kp(db, "MATH-G4-OLY-B", "数字谜", source_ref="深中奥数小A P55 数字谜")
    _add_kp(db, "MATH-G4-NORM", "普通卡", source_ref="北师大版四上 P80")
    sd = tmp_path / "01-知识点"
    wp = write_card(
        sd / "G4A" / "MATH-G4-OLY-B.md",
        KpCard(frontmatter=_card_fm("MATH-G4-OLY-B", "数字谜"), body="# 数字谜\n"),
    )
    write_card(
        sd / "G4A" / "MATH-G4-NORM.md",
        KpCard(frontmatter=_card_fm("MATH-G4-NORM", "普通卡"), body="# 普通卡\n"),
    )
    r = km.retag_olympiad_cards(db, subject_code="MATH", kp_dir=sd)
    assert r["detected"] == ["MATH-G4-OLY-B"]
    assert len(r["moved"]) == 1
    assert not wp.exists(), "旧路径应被移走"
    assert (sd / "小A" / "MATH-G4-OLY-B.md").exists()
    assert read_card(sd / "小A" / "MATH-G4-OLY-B.md").frontmatter["grade_term"] == "小A"
    assert (sd / "G4A" / "MATH-G4-NORM.md").exists(), "普通卡不动"

    # 幂等：再跑 → already，不再移动
    r2 = km.retag_olympiad_cards(db, subject_code="MATH", kp_dir=sd)
    assert r2["moved"] == [] and r2["already"] == ["MATH-G4-OLY-B"]


def test_retag_olympiad_missing_card_recorded(db, tmp_path):
    _add_kp(db, "MATH-G4-OLY-NOCARD", "无卡奥数", source_ref="深中奥数小A P12")
    sd = tmp_path / "01-知识点"
    r = km.retag_olympiad_cards(db, subject_code="MATH", kp_dir=sd)
    assert r["detected"] == ["MATH-G4-OLY-NOCARD"]
    assert r["missing"] == ["MATH-G4-OLY-NOCARD"]


# ── 5. 卡片域回填 ────────────────────────────────────────────
def test_backfill_only_fills_empty(db, tmp_path):
    _add_kp(db, "MATH-G4-F1", "待填", knowledge_domain="图形与几何")
    _add_kp(db, "MATH-G4-F2", "已有值")
    _add_kp(db, "MATH-G4-F3", "DB也空")
    sd = tmp_path / "01-知识点"
    write_card(
        sd / "G4A" / "MATH-G4-F1.md",
        KpCard(frontmatter=_card_fm("MATH-G4-F1", "待填"), body="# 待填\n"),
    )
    write_card(
        sd / "G4A" / "MATH-G4-F2.md",
        KpCard(
            frontmatter=_card_fm("MATH-G4-F2", "已有值", knowledge_domain="统计与概率"),
            body="# 已有值\n",
        ),
    )
    write_card(
        sd / "G4A" / "MATH-G4-F3.md",
        KpCard(frontmatter=_card_fm("MATH-G4-F3", "DB也空"), body="# DB也空\n"),
    )
    r = km.backfill_card_domains(db, subject_code="MATH", kp_dir=sd)
    assert [it["code"] for it in r["filled"]] == ["MATH-G4-F1"]
    assert r["skipped_no_db"] == ["MATH-G4-F3"]
    assert read_card(sd / "G4A" / "MATH-G4-F1.md").frontmatter["knowledge_domain"] == "图形与几何"
    assert (
        read_card(sd / "G4A" / "MATH-G4-F2.md").frontmatter["knowledge_domain"] == "统计与概率"
    ), "已有值不得被覆盖"


def test_backfill_dry_run_no_write(db, tmp_path):
    _add_kp(db, "MATH-G4-F9", "干跑", knowledge_domain="数与代数")
    sd = tmp_path / "01-知识点"
    write_card(
        sd / "G4A" / "MATH-G4-F9.md",
        KpCard(frontmatter=_card_fm("MATH-G4-F9", "干跑"), body="# 干跑\n"),
    )
    r = km.backfill_card_domains(db, subject_code="MATH", kp_dir=sd, dry_run=True)
    assert len(r["filled"]) == 1
    assert read_card(sd / "G4A" / "MATH-G4-F9.md").frontmatter.get("knowledge_domain") is None
