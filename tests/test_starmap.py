"""学习星图数据构建单测（Step 3 · 2026-09-12）。

覆盖：
1. 分组修复：学科维度来自 subject 表（语文不再混进 G4 年级组）；
2. 三维字段：subject / domain / term + 学期桶推导（小A 特殊桶）；
3. 关联边：读出 + 跨学科 cross 标记；
4. 布局：坐标齐全 / 确定性（两次构建一致）/ 领域子簇；
5. Obsidian：卡片存在 → 相对路径 + URI 正确编码；缺卡 → None；
6. 统计：nodes/edges/isolated/with_card 等。
"""

from __future__ import annotations

import pytest

from fdl_core.db.schema import create_schema, get_connection
from fdl_core.notes.kp_card import KpCard, write_card
from fdl_core.notes.starmap import build_starmap


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "sm.db"
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


def _add_kp(
    conn,
    code,
    name,
    *,
    subject_id=1,
    domain=None,
    grade=4,
    semester=None,
    source_ref=None,
    description=None,
    **over,
):
    base = {
        "subject_id": subject_id,
        "code": code,
        "name": name,
        "grade_level": grade,
        "semester": semester,
        "knowledge_domain": domain,
        "source_ref": source_ref,
        "description": description,
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


def _empty_dirs(tmp_path):
    """不存在的目录（用于不关心卡片的用例）。"""
    return {"MATH": tmp_path / "no-math", "CHINESE": tmp_path / "no-chn"}


# ── 1. 分组修复 ──────────────────────────────────────────────
def test_grouping_uses_subject_not_code_segment(db, tmp_path):
    """语文 KP 的学科维度 = CHINESE，绝不再被 code.split('-')[1] 混进年级组。"""
    _add_kp(db, "MATH-G4-A", "数学点", domain="数与代数")
    _add_kp(db, "CHN-G4-POEM", "古诗点", subject_id=2, domain="识字与写字")
    sm = build_starmap(db, subject_dirs=_empty_dirs(tmp_path), root=tmp_path, vault_name="V")
    by_code = {n["code"]: n for n in sm["nodes"]}
    assert by_code["MATH-G4-A"]["subject"] == "MATH"
    assert by_code["MATH-G4-A"]["subject_name"] == "数学"
    assert by_code["CHN-G4-POEM"]["subject"] == "CHINESE"
    assert by_code["CHN-G4-POEM"]["subject_name"] == "语文"
    # 旧 bug 的产物（一个 G4 混合组）不得出现：学科维度必须是四科 code
    assert {n["subject"] for n in sm["nodes"]} == {"MATH", "CHINESE"}
    # 学科清单（含 0 KP 的科也登记，供筛选用）
    codes = {s["code"]: s["count"] for s in sm["subjects"]}
    assert codes["MATH"] == 1 and codes["CHINESE"] == 1


# ── 2. 三维字段 + 学期桶 ─────────────────────────────────────
def test_term_derivation_and_olympiad(db, tmp_path):
    _add_kp(db, "MATH-G3-PLAIN", "三上默认", domain="数与代数", grade=3)
    _add_kp(db, "MATH-G4-B-SEM", "四下", domain="数与代数", grade=4, semester=2)
    _add_kp(db, "MATH-G4-OLY-1", "奥数题", domain="数与代数", source_ref="深中奥数小A P36 数表 题5")
    sm = build_starmap(db, subject_dirs=_empty_dirs(tmp_path), root=tmp_path, vault_name="V")
    by_code = {n["code"]: n for n in sm["nodes"]}
    assert by_code["MATH-G3-PLAIN"]["term"] == "G3A"  # 保守默认上学期
    assert by_code["MATH-G4-B-SEM"]["term"] == "G4B"  # semester=2 → 下学期
    assert by_code["MATH-G4-OLY-1"]["term"] == "小A"  # 奥数特殊桶
    # 学期清单："小A" 排最后
    assert sm["terms"][-1] == "小A"
    # domain 原样透出（已课标化的字段）
    assert by_code["MATH-G4-OLY-1"]["domain"] == "数与代数"


def test_domain_fallback_when_empty(db, tmp_path):
    _add_kp(db, "MATH-G4-NOD", "无领域")
    sm = build_starmap(db, subject_dirs=_empty_dirs(tmp_path), root=tmp_path, vault_name="V")
    assert sm["nodes"][0]["domain"] == "未分类"


# ── 3. 关联边 ────────────────────────────────────────────────
def test_edges_and_cross_flag(db, tmp_path):
    a = _add_kp(db, "MATH-G4-A", "甲", domain="数与代数")
    b = _add_kp(db, "MATH-G4-B", "乙", domain="数与代数")
    c = _add_kp(db, "CHN-G4-C", "丙", subject_id=2, domain="识字与写字")
    db.execute(
        "INSERT INTO kp_relation (from_kp_id, to_kp_id, relation_type,"
        " confidence, source, note) VALUES (?,?,?,?,?,?)",
        (min(a, b), max(a, b), "ANALOGY", 0.9, "LLM_AUTO", "同科"),
    )
    db.execute(
        "INSERT INTO kp_relation (from_kp_id, to_kp_id, relation_type,"
        " confidence, source, note) VALUES (?,?,?,?,?,?)",
        (min(a, c), max(a, c), "SHARED_METHOD", 0.7, "LLM_SUGGEST", "跨科"),
    )
    db.commit()
    sm = build_starmap(db, subject_dirs=_empty_dirs(tmp_path), root=tmp_path, vault_name="V")
    assert len(sm["edges"]) == 2
    cross = [e for e in sm["edges"] if e["cross"]]
    same = [e for e in sm["edges"] if not e["cross"]]
    assert len(cross) == 1 and cross[0]["to"] in ("CHN-G4-C",) or cross[0]["from"] in ("CHN-G4-C",)
    assert len(same) == 1
    assert sm["stats"]["cross"] == 1
    assert sm["stats"]["auto"] == 1 and sm["stats"]["suggest"] == 1


def test_isolated_and_connected(db, tmp_path):
    a = _add_kp(db, "MATH-G4-A", "甲", domain="数与代数")
    b = _add_kp(db, "MATH-G4-B", "乙", domain="数与代数")
    _add_kp(db, "MATH-G4-C", "丙孤立", domain="数与代数")
    db.execute(
        "INSERT INTO kp_relation (from_kp_id, to_kp_id, relation_type) VALUES (?,?,?)",
        (min(a, b), max(a, b), "ANALOGY"),
    )
    db.commit()
    sm = build_starmap(db, subject_dirs=_empty_dirs(tmp_path), root=tmp_path, vault_name="V")
    assert sm["stats"]["isolated"] == 1


# ── 4. 布局 ──────────────────────────────────────────────────
def test_layout_deterministic_and_complete(db, tmp_path):
    for i in range(14):
        dom = "数与代数" if i < 8 else "图形与几何"
        _add_kp(db, f"MATH-G4-{i:02d}", f"点{i}", domain=dom)
    sm1 = build_starmap(db, subject_dirs=_empty_dirs(tmp_path), root=tmp_path, vault_name="V")
    sm2 = build_starmap(db, subject_dirs=_empty_dirs(tmp_path), root=tmp_path, vault_name="V")
    pos1 = {n["code"]: (n["x"], n["y"]) for n in sm1["nodes"]}
    pos2 = {n["code"]: (n["x"], n["y"]) for n in sm2["nodes"]}
    assert pos1 == pos2, "同一数据两次构建坐标必须一致（确定性）"
    assert all(n["x"] > 0 and n["y"] > 0 for n in sm1["nodes"])
    assert sm1["width"] > 0 and sm1["height"] > 0
    labels = {c["label"] for c in sm1["clusters"]}
    assert "数学 · 数与代数" in labels and "数学 · 图形与几何" in labels, (
        f"≥10 节点的学科应按领域拆子簇，实际: {labels}"
    )


def test_small_subject_single_cluster(db, tmp_path):
    _add_kp(db, "CHN-G4-A", "语1", subject_id=2, domain="识字与写字")
    _add_kp(db, "CHN-G4-B", "语2", subject_id=2, domain="阅读与鉴赏")
    sm = build_starmap(db, subject_dirs=_empty_dirs(tmp_path), root=tmp_path, vault_name="V")
    labels = {c["label"] for c in sm["clusters"]}
    assert labels == {"语文"}, "小科（<10）并为单簇"


# ── 5. Obsidian 链接 ────────────────────────────────────────
def test_obsidian_uri_and_card_relpath(db, tmp_path):
    _add_kp(db, "MATH-G4-MUL", "乘法交换律", domain="数与代数")
    card_dir = tmp_path / "1-Math" / "01-知识点"
    write_card(
        card_dir / "G4A" / "MATH-G4-MUL.md",
        KpCard(
            frontmatter={
                "type": "knowledge_point",
                "code": "MATH-G4-MUL",
                "subject": "MATH",
                "name": "乘法交换律",
                "grade_level": 4,
                "semester": 1,
                "grade_term": "G4A",
                "kp_type": "CONCEPT",
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
            body="# 乘法交换律\n",
        ),
    )
    sm = build_starmap(
        db,
        subject_dirs={"MATH": card_dir, "CHINESE": tmp_path / "no"},
        root=tmp_path,
        vault_name="work",
    )
    n = sm["nodes"][0]
    assert n["card"] == "1-Math/01-知识点/G4A/MATH-G4-MUL.md"
    assert n["obsidian"].startswith("obsidian://open?vault=work&file=")
    assert "%2F" in n["obsidian"], "斜杠必须编码为 %2F（Obsidian URI 规范）"
    assert n["obsidian"].endswith("MATH-G4-MUL.md")
    # "知识点" 的 UTF-8 百分号编码（证明中文路径被正确编码）
    assert "%E7%9F%A5%E8%AF%86%E7%82%B9" in n["obsidian"], "中文路径必须 URI 编码"
    assert sm["stats"]["with_obsidian"] == 1


def test_missing_card_no_link(db, tmp_path):
    _add_kp(db, "MATH-G4-NOCARD", "无卡点", domain="数与代数")
    sm = build_starmap(db, subject_dirs=_empty_dirs(tmp_path), root=tmp_path, vault_name="V")
    n = sm["nodes"][0]
    assert n["card"] is None and n["obsidian"] is None
    assert sm["stats"]["with_card"] == 0
