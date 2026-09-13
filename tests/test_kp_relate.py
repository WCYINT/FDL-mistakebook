"""连边构图单测（Phase 2 · 2026-09-12）。

覆盖：
1. 方向规范化：from>to 的提议写库时翻转为 from<to（与建表 CHECK 一致）
2. 三重校验：幻觉 code / 自环 / 非法类型 / 作用域不符 → 丢弃并计数
3. 双跑一致闸门：双跑+高置信 → LLM_AUTO；单跑命中 → LLM_SUGGEST；低置信 → 丢弃
4. 同对去重：类型冲突按 AUTO > SUGGEST、再按置信度裁决
5. 幂等：重复提议已有边跳过，不产生重复行
6. 复核流：confirm → HUMAN_VERIFIED；drop → 删除；drop --dry-run 不删
7. dry-run 不写库
"""

from __future__ import annotations

import pytest

from fdl_core.db.schema import create_schema, get_connection
from fdl_core.notes import kp_relate as kpr


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "rel.db"
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


class FakeLLM:
    """按预设的多跑结果轮流返回（每轮推进，可循环以支持二次 propose）。"""

    def __init__(self, runs: list[list[dict]]):
        self.runs = runs
        self.i = 0
        self.scope_calls: list[str] = []

    def __call__(self, items, *, scope, client):
        self.scope_calls.append(scope)
        run = self.runs[self.i % len(self.runs)]
        self.i += 1
        return {"source_layer": "L2.A", "edges": [dict(e) for e in run]}


def _wire(monkeypatch, runs):
    fake = FakeLLM(runs)
    monkeypatch.setattr(kpr, "_single_run", fake)
    return fake


def _edges_of(conn):
    return conn.execute(
        "SELECT from_kp_id, to_kp_id, relation_type, confidence, source"
        " FROM kp_relation ORDER BY id"
    ).fetchall()


# ── 1. 方向规范化 ────────────────────────────────────────────
def test_direction_normalized(db, monkeypatch):
    a = _add_kp(db, "MATH-G4-A", "甲")
    b = _add_kp(db, "MATH-G4-B", "乙")
    edge = {
        "from": "MATH-G4-B",
        "to": "MATH-G4-A",
        "type": "ANALOGY",
        "confidence": 0.9,
        "reason": "r",
    }
    _wire(monkeypatch, [[edge], [edge]])
    r = kpr.propose_relations(db, scope="within", subject_codes=["MATH"])
    assert r["ok"] and r["inserted"] == 1
    row = _edges_of(db)[0]
    assert row == (a, b, "ANALOGY", 0.9, "LLM_AUTO")  # from<to + 双跑自动档


# ── 2. 三重校验 ──────────────────────────────────────────────
def test_validation_drops(db, monkeypatch):
    _add_kp(db, "MATH-G4-A", "甲")
    _add_kp(db, "MATH-G4-B", "乙")
    runs = [
        [
            {
                "from": "MATH-GHOST",
                "to": "MATH-G4-A",
                "type": "ANALOGY",
                "confidence": 0.9,
                "reason": "幻觉",
            },
            {
                "from": "MATH-G4-A",
                "to": "MATH-G4-A",
                "type": "ANALOGY",
                "confidence": 0.9,
                "reason": "自环",
            },
            {
                "from": "MATH-G4-A",
                "to": "MATH-G4-B",
                "type": "FOO",
                "confidence": 0.9,
                "reason": "非法类型",
            },
        ]
    ]
    _wire(monkeypatch, runs)
    r = kpr.propose_relations(db, scope="within", subject_codes=["MATH"])
    assert r["dropped"] == {"hallucinated_code": 1, "self_edge": 1, "bad_type": 1}
    assert _edges_of(db) == []


def test_type_case_insensitive(db, monkeypatch):
    _add_kp(db, "MATH-G4-A", "甲")
    _add_kp(db, "MATH-G4-B", "乙")
    edge = {
        "from": "MATH-G4-A",
        "to": "MATH-G4-B",
        "type": "analogy",
        "confidence": 0.9,
        "reason": "小写归一",
    }
    _wire(monkeypatch, [[edge], [edge]])
    r = kpr.propose_relations(db, scope="within", subject_codes=["MATH"])
    assert r["inserted"] == 1
    assert _edges_of(db)[0][2] == "ANALOGY"


def test_scope_mismatch_dropped(db, monkeypatch):
    _add_kp(db, "MATH-G4-A", "甲")
    _add_kp(db, "MATH-G4-C", "丙")  # within 至少 2 条才会跑
    _add_kp(db, "CHN-G4-B", "乙", subject_id=2)
    cross_edge = {
        "from": "MATH-G4-A",
        "to": "CHN-G4-B",
        "type": "ANALOGY",
        "confidence": 0.9,
        "reason": "跨科",
    }
    # within 作用域收到跨科边 → 丢弃
    _wire(monkeypatch, [[cross_edge], [cross_edge]])
    r = kpr.propose_relations(db, scope="within", subject_codes=["MATH"])
    assert r["dropped"] == {"scope_mismatch": 1}
    # cross 作用域收到同科边 → 丢弃
    same_edge = {
        "from": "MATH-G4-A",
        "to": "MATH-G4-C",
        "type": "ANALOGY",
        "confidence": 0.9,
        "reason": "同科",
    }
    _wire(monkeypatch, [[same_edge], [same_edge]])
    r2 = kpr.propose_relations(db, scope="cross", subject_codes=["MATH", "CHINESE"])
    assert r2["dropped"] == {"scope_mismatch": 1}


# ── 3. 双跑一致闸门 ──────────────────────────────────────────
def test_gate_tiers(db, monkeypatch):
    _add_kp(db, "MATH-G4-A", "甲")
    _add_kp(db, "MATH-G4-B", "乙")
    _add_kp(db, "MATH-G4-C", "丙")
    _add_kp(db, "MATH-G4-D", "丁")

    def e(f, t, conf, typ="ANALOGY"):
        return {"from": f, "to": t, "type": typ, "confidence": conf, "reason": "x"}

    run1 = [
        e("MATH-G4-A", "MATH-G4-B", 0.90),  # 双跑 → AUTO
        e("MATH-G4-A", "MATH-G4-C", 0.90),  # 单跑 → SUGGEST
        e("MATH-G4-A", "MATH-G4-D", 0.50),
    ]  # 低置信 → 丢弃
    run2 = [
        e("MATH-G4-A", "MATH-G4-B", 0.85),  # 双跑 min=0.85 ≥ 0.8
        e("MATH-G4-B", "MATH-G4-D", 0.70),
    ]  # 单跑中置信 → SUGGEST
    _wire(monkeypatch, [run1, run2])
    r = kpr.propose_relations(db, scope="within", subject_codes=["MATH"])
    assert len(r["auto"]) == 1 and r["auto"][0]["from"] == "MATH-G4-A"
    assert r["auto"][0]["confidence"] == 0.85  # 取双跑最小值
    assert "[双跑一致]" in r["auto"][0]["note"]
    assert len(r["suggest"]) == 2
    assert r["dropped"]["low_confidence"] == 1
    sources = {row[4] for row in _edges_of(db)}
    assert sources == {"LLM_AUTO", "LLM_SUGGEST"}


def test_min_confidence_blocks_auto(db, monkeypatch):
    """双跑命中但 min(conf) < 0.80 → 不自动，降为 SUGGEST。"""
    _add_kp(db, "MATH-G4-A", "甲")
    _add_kp(db, "MATH-G4-B", "乙")
    hi = {
        "from": "MATH-G4-A",
        "to": "MATH-G4-B",
        "type": "ANALOGY",
        "confidence": 0.95,
        "reason": "x",
    }
    lo = dict(hi, confidence=0.70)
    _wire(monkeypatch, [[hi], [lo]])
    r = kpr.propose_relations(db, scope="within", subject_codes=["MATH"])
    assert r["auto"] == [] and len(r["suggest"]) == 1
    assert _edges_of(db)[0][4] == "LLM_SUGGEST"


# ── 4. 同对去重 ──────────────────────────────────────────────
def test_pair_conflict_keeps_strongest(db, monkeypatch):
    _add_kp(db, "MATH-G4-A", "甲")
    _add_kp(db, "MATH-G4-B", "乙")
    auto_e = {
        "from": "MATH-G4-A",
        "to": "MATH-G4-B",
        "type": "ANALOGY",
        "confidence": 0.85,
        "reason": "双跑",
    }
    single_e = {
        "from": "MATH-G4-A",
        "to": "MATH-G4-B",
        "type": "CONTRAST",
        "confidence": 0.95,
        "reason": "单跑但更高分",
    }
    # run1: 两条都在；run2: 只有 auto_e → CONTRAST 为单跑
    _wire(monkeypatch, [[auto_e, single_e], [auto_e]])
    r = kpr.propose_relations(db, scope="within", subject_codes=["MATH"])
    rows = _edges_of(db)
    assert len(rows) == 1, "同一对节点只允许一条边"
    assert rows[0][2] == "ANALOGY" and rows[0][4] == "LLM_AUTO"
    assert r["dropped"]["pair_conflict"] == 1


# ── 5. 幂等 ──────────────────────────────────────────────────
def test_idempotent_rerun(db, monkeypatch):
    _add_kp(db, "MATH-G4-A", "甲")
    _add_kp(db, "MATH-G4-B", "乙")
    edge = {
        "from": "MATH-G4-A",
        "to": "MATH-G4-B",
        "type": "ANALOGY",
        "confidence": 0.9,
        "reason": "x",
    }
    _wire(monkeypatch, [[edge], [edge]])
    r1 = kpr.propose_relations(db, scope="within", subject_codes=["MATH"])
    assert r1["inserted"] == 1
    r2 = kpr.propose_relations(db, scope="within", subject_codes=["MATH"])
    assert r2["inserted"] == 0 and r2["existing"] == 1
    assert len(_edges_of(db)) == 1, "重复提议不得产生重复行"


# ── 6. 复核流 ────────────────────────────────────────────────
def test_confirm_and_drop(db, monkeypatch):
    _add_kp(db, "MATH-G4-A", "甲")
    _add_kp(db, "MATH-G4-B", "乙")
    s_edge = {
        "from": "MATH-G4-A",
        "to": "MATH-G4-B",
        "type": "ANALOGY",
        "confidence": 0.70,
        "reason": "单跑",
    }
    _wire(monkeypatch, [[s_edge], []])
    kpr.propose_relations(db, scope="within", subject_codes=["MATH"])
    _edges_of(db)[0]
    rid = db.execute("SELECT id FROM kp_relation").fetchone()[0]

    assert kpr.confirm_relations(db, [rid])["confirmed"] == 1
    assert (
        db.execute("SELECT source FROM kp_relation WHERE id=?", (rid,)).fetchone()[0]
        == "HUMAN_VERIFIED"
    )

    # drop --dry-run 不删
    assert kpr.drop_relations(db, [rid], dry_run=True)["dropped"] == 1
    assert db.execute("SELECT COUNT(*) FROM kp_relation").fetchone()[0] == 1
    # 真删
    assert kpr.drop_relations(db, [rid])["dropped"] == 1
    assert db.execute("SELECT COUNT(*) FROM kp_relation").fetchone()[0] == 0


# ── 7. dry-run ───────────────────────────────────────────────
def test_dry_run_no_write(db, monkeypatch):
    _add_kp(db, "MATH-G4-A", "甲")
    _add_kp(db, "MATH-G4-B", "乙")
    edge = {
        "from": "MATH-G4-A",
        "to": "MATH-G4-B",
        "type": "ANALOGY",
        "confidence": 0.9,
        "reason": "x",
    }
    _wire(monkeypatch, [[edge], [edge]])
    r = kpr.propose_relations(db, scope="within", subject_codes=["MATH"], dry_run=True)
    assert len(r["auto"]) == 1 and r["inserted"] == 0
    assert db.execute("SELECT COUNT(*) FROM kp_relation").fetchone()[0] == 0


# ── 8. 统计 ──────────────────────────────────────────────────
def test_edge_stats_cross(db, monkeypatch):
    _add_kp(db, "MATH-G4-A", "甲")
    _add_kp(db, "MATH-G4-B", "乙")
    _add_kp(db, "CHN-G4-C", "丙", subject_id=2)
    same = {
        "from": "MATH-G4-A",
        "to": "MATH-G4-B",
        "type": "ANALOGY",
        "confidence": 0.9,
        "reason": "x",
    }
    cross = {
        "from": "MATH-G4-A",
        "to": "CHN-G4-C",
        "type": "SHARED_METHOD",
        "confidence": 0.9,
        "reason": "y",
    }
    _wire(monkeypatch, [[same], [same]])
    kpr.propose_relations(db, scope="within", subject_codes=["MATH"])
    _wire(monkeypatch, [[cross], [cross]])
    kpr.propose_relations(db, scope="cross", subject_codes=["MATH", "CHINESE"])
    st = kpr.edge_stats(db)
    assert st["total"] == 2 and st["cross"] == 1
    assert st["cross_items"][0]["from_subject"] == "MATH"
    assert st["by_type"] == {"ANALOGY": 1, "SHARED_METHOD": 1}
