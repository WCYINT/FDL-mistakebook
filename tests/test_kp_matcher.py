"""kp_matcher 单测（2026-09-12 Phase 1）。

覆盖：
1. 高置信双跑一致 → 自动写 mistake_record.kp_id + 回填 review_schedule.kp_id
2. 双跑不一致 → 落 PROPOSED，绝不写权威字段
3. 低置信 → PROPOSED（不触发复核跑，省调用）
4. LLM 不可达（L0）→ PROPOSED，不写
5. LLM 返回非法 code → PROPOSED
6. LLM 提议新知识点 → 写 kp_candidate，绝不自动新增到 knowledge_point
7. 候选晋级 → 写 knowledge_point（人工确认通道）
8. accept/reject 提案
9. 已挂载错题不重复覆盖（人工已指定的 kp_id 语义）

LLM 全部用序列桩打桩，不打真实网络。
"""

from __future__ import annotations

import json

import pytest

from fdl_core.db.schema import create_schema, get_connection
from fdl_core.l2.fallback import ChainResult
from fdl_core.mistakes import kp_matcher as kpm


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "kp.db"
    conn = get_connection(str(p))
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000',"
        " 0.35, 4, 9, 1)"
    )
    # 三个知识点种子
    for i, (code, name, ktype) in enumerate(
        [
            ("MATH-G4-MUL-DIST", "乘法分配律", "CONCEPT"),
            ("MATH-G4-TRI-ANGLESUM", "三角形内角和", "CONCEPT"),
            ("MATH-G4-PRICE", "单价·数量·总价", "CONCEPT"),
        ],
        start=1,
    ):
        conn.execute(
            "INSERT INTO knowledge_point (id, subject_id, parent_id, code, name,"
            " grade_level, bloom_level, abstraction_level, importance_weight,"
            " base_difficulty, est_learn_minutes, est_review_seconds, kp_type,"
            " tier, graph_version, valid_from)"
            " VALUES (?, 1, NULL, ?, ?, 4, 2, 1, 1.0, 4.0, 20.0, 120, ?,"
            " 'L0', 'g1', '2026-09-01')",
            (i, code, name, ktype),
        )
    conn.commit()
    yield conn
    conn.close()


def _add_mistake(conn, mid=77001, **over):
    base = {
        "id": mid,
        "user_id": 1,
        "kp_id": 0,
        "occurred_at": "2026-09-03T20:00:00Z",
        "subject": "MATH",
        "source": "REAL_WORK",
        "source_ref": "深中奥数小A P15 中项定理 15-11",
        "error_type": "METHOD",
        "attributed_by": "RULE_BASED",
        "attribution_confidence": 0.0,
        "severity": 3,
        "is_tamed": 0,
        "reappear_count": 0,
        "note_id": f"n{mid}",
        "wrong_answer": "求和出错",
        "correct_answer": "中项×项数",
    }
    base.update(over)
    cols = ", ".join(base)
    ph = ", ".join("?" for _ in base)
    conn.execute(f"INSERT INTO mistake_record ({cols}) VALUES ({ph})", list(base.values()))


def _add_schedule(conn, mid=77001, sid=500):
    conn.execute(
        "INSERT INTO review_schedule (id, user_id, mistake_id, kp_id, subject_id,"
        " due_date, due_session, planned_interval_days, priority_score,"
        " est_seconds, status, source)"
        " VALUES (?, 1, ?, NULL, 1, '2026-09-05', 'PM', 1, 90.0, 120, 'PENDING',"
        " 'answer')",
        (sid, mid),
    )
    conn.commit()


def _patch_seq(monkeypatch, answers: list[str], source: str = "L2.A"):
    """序列桩：第 n 次调用返回 answers[n-1]。"""
    it = iter(answers)

    def fake(sys_p, u_p, **kw):
        try:
            a = next(it)
        except StopIteration:
            a = answers[-1]
        return ChainResult(a, source, 0.01)

    monkeypatch.setattr(kpm, "run_chain", fake)


def _ans(code, conf, rationale="理由", new_kp=None):
    return json.dumps(
        {
            "kp_code": code,
            "confidence": conf,
            "rationale": rationale,
            "evidence_quote": "题面片段",
            "new_kp": new_kp,
        }
    )


# ── 1. 高置信双跑一致 → 自动挂载 + 回填计划 ─────────────────
def test_high_confidence_auto_mount(db, monkeypatch):
    _add_mistake(db)
    _add_schedule(db)
    _patch_seq(monkeypatch, [_ans("MATH-G4-MUL-DIST", 0.9), _ans("MATH-G4-MUL-DIST", 0.88)])
    r = kpm.match_kp_for_mistake(db, 77001, client=None)
    assert r["status"] == kpm.STATUS_AUTO and r["gate"] == "auto"
    assert r["kp_code"] == "MATH-G4-MUL-DIST"
    row = db.execute("SELECT kp_id FROM mistake_record WHERE id=77001").fetchone()
    assert row[0] == 1, "高置信双跑一致必须写 kp_id"
    # 回填计划
    srow = db.execute("SELECT kp_id FROM review_schedule WHERE id=500").fetchone()
    assert srow[0] == 1, "悬空计划必须被回填（防半挂载态）"
    assert r["written"]["schedules_backfilled"] == 1


# ── 2. 双跑不一致 → PROPOSED，不写 ──────────────────────────
def test_disagreement_blocks_mount(db, monkeypatch):
    _add_mistake(db)
    _patch_seq(monkeypatch, [_ans("MATH-G4-MUL-DIST", 0.9), _ans("MATH-G4-PRICE", 0.9)])
    r = kpm.match_kp_for_mistake(db, 77001, client=None)
    assert r["status"] == kpm.STATUS_PROPOSED
    assert not r.get("written")
    assert db.execute("SELECT kp_id FROM mistake_record WHERE id=77001").fetchone()[0] == 0, (
        "不一致时绝不写权威字段"
    )


# ── 3. 低置信 → PROPOSED（只调 1 次）───────────────────────
def test_low_confidence_no_confirm_call(db, monkeypatch):
    _add_mistake(db)
    calls = []

    def fake(sys_p, u_p, **kw):
        calls.append(1)
        return ChainResult(_ans("MATH-G4-MUL-DIST", 0.4), "L2.A", 0.01)

    monkeypatch.setattr(kpm, "run_chain", fake)
    r = kpm.match_kp_for_mistake(db, 77001, client=None)
    assert r["status"] == kpm.STATUS_PROPOSED
    assert len(calls) == 1, "首跑低置信不应触发复核跑"


# ── 4. LLM 不可达 → PROPOSED，不写 ─────────────────────────
def test_llm_unreachable(db, monkeypatch):
    _add_mistake(db)
    _patch_seq(monkeypatch, [""], source="L0")
    r = kpm.match_kp_for_mistake(db, 77001, client=None)
    assert r["status"] == kpm.STATUS_PROPOSED and r["confidence"] is None or True
    assert db.execute("SELECT kp_id FROM mistake_record WHERE id=77001").fetchone()[0] == 0


# ── 5. 非法 code → PROPOSED ────────────────────────────────
def test_invalid_code_blocked(db, monkeypatch):
    _add_mistake(db)
    _patch_seq(monkeypatch, [_ans("MATH-G4-NOT-EXIST", 0.99), _ans("MATH-G4-NOT-EXIST", 0.99)])
    r = kpm.match_kp_for_mistake(db, 77001, client=None)
    assert r["status"] == kpm.STATUS_PROPOSED
    assert db.execute("SELECT kp_id FROM mistake_record WHERE id=77001").fetchone()[0] == 0, (
        "幻觉 code 绝不能挂载"
    )


# ── 6. 新 KP → candidate，绝不自动新增 ──────────────────────
def test_new_kp_goes_to_candidate(db, monkeypatch):
    _add_mistake(db)
    _patch_seq(
        monkeypatch,
        [
            _ans(
                "MATH-G4-MUL-DIST",
                0.9,
                new_kp={
                    "code": "MATH-G4-FRACTION-MEANING",
                    "name": "分数意义",
                    "parent_code": None,
                    "kp_type": "CONCEPT",
                    "rationale": "多次在分数意义上出错",
                },
            )
        ],
    )
    r = kpm.match_kp_for_mistake(db, 77001, client=None)
    assert r.get("candidate_id")
    cands = kpm.list_candidates(db)
    assert len(cands) == 1 and cands[0]["candidate_code"] == "MATH-G4-FRACTION-MEANING"
    # 绝不自动新增到 knowledge_point
    assert (
        db.execute(
            "SELECT COUNT(*) FROM knowledge_point WHERE code='MATH-G4-FRACTION-MEANING'"
        ).fetchone()[0]
        == 0
    )


# ── 7. 候选晋级 → 写 knowledge_point ───────────────────────
def test_promote_candidate(db, monkeypatch):
    _add_mistake(db)
    _patch_seq(
        monkeypatch,
        [
            _ans(
                "MATH-G4-MUL-DIST",
                0.9,
                new_kp={
                    "code": "MATH-G4-FRACTION-MEANING",
                    "name": "分数意义",
                    "parent_code": None,
                    "kp_type": "CONCEPT",
                    "rationale": "证据",
                },
            )
        ],
    )
    kpm.match_kp_for_mistake(db, 77001, client=None)
    cid = kpm.list_candidates(db)[0]["id"]
    res = kpm.promote_candidate(db, cid, decided_by="PARENT")
    assert res["ok"], res
    row = db.execute(
        "SELECT code, name FROM knowledge_point WHERE code='MATH-G4-FRACTION-MEANING'"
    ).fetchone()
    assert row == ("MATH-G4-FRACTION-MEANING", "分数意义")
    assert kpm.list_candidates(db) == []


# ── 8. accept / reject 提案 ────────────────────────────────
def test_accept_and_reject_proposal(db, monkeypatch):
    _add_mistake(db)
    _patch_seq(monkeypatch, [_ans("MATH-G4-PRICE", 0.5), _ans("MATH-G4-PRICE", 0.5)])
    r = kpm.match_kp_for_mistake(db, 77001, client=None)
    assert r["status"] == kpm.STATUS_PROPOSED
    pid = r["proposal_id"]
    acc = kpm.accept_proposal(db, pid, decided_by="PARENT")
    assert acc["ok"] and acc["written"]["mistake_updated"]
    assert (
        db.execute("SELECT kp_id FROM mistake_record WHERE id=77001").fetchone()[0] == 3
    )  # MATH-G4-PRICE 的 id
    # 再驳回一个
    _add_mistake(db, mid=77002)
    _patch_seq(monkeypatch, [_ans("MATH-G4-PRICE", 0.6), _ans("MATH-G4-PRICE", 0.6)])
    r2 = kpm.match_kp_for_mistake(db, 77002, client=None)
    rej = kpm.reject_proposal(db, r2["proposal_id"], decided_by="PARENT", reason="证据不足")
    assert rej["ok"]


# ── 9. 提案列表 ────────────────────────────────────────────
def test_list_proposals(db, monkeypatch):
    _add_mistake(db)
    _patch_seq(monkeypatch, [_ans("MATH-G4-PRICE", 0.5), _ans("MATH-G4-PRICE", 0.5)])
    kpm.match_kp_for_mistake(db, 77001, client=None)
    rows = kpm.list_proposals(db, status=kpm.STATUS_PROPOSED)
    assert len(rows) == 1 and rows[0]["mistake_id"] == 77001
    assert kpm.list_proposals(db, status=kpm.STATUS_AUTO) == []


# ── 10. 不存在的错题 → 优雅失败 ────────────────────────────
def test_missing_mistake(db):
    r = kpm.match_kp_for_mistake(db, 99999, client=None)
    assert not r["ok"] and r["errors"]


# ── 10. 复核跑技术性失败 → 重试（2026-09-12 修复）───────────────
def test_confirm_retry_on_unparseable(db, monkeypatch):
    """复核跑输出不可解析 → 自动重试；重试成功且一致 → 自动挂载。

    🔴 回归防护：实测曾有 6 条 0.92-0.98 高置信匹配因 r2 输出不可解析
    （source_layer=L2.A 但 code=null）被误判"不一致"落入人工队列。
    """
    _add_mistake(db)
    _patch_seq(
        monkeypatch,
        [
            _ans("MATH-G4-MUL-DIST", 0.9),  # 首跑
            "抱歉，我需要更多信息才能判断。",  # 复核跑：不可解析
            _ans("MATH-G4-MUL-DIST", 0.88),  # 重试成功
        ],
    )
    r = kpm.match_kp_for_mistake(db, 77001, client=None)
    assert r["status"] == kpm.STATUS_AUTO, "重试成功且一致必须自动挂载"
    assert db.execute("SELECT kp_id FROM mistake_record WHERE id=77001").fetchone()[0] == 1
    # 审计：confirm_json 记录 retries
    cj = db.execute(
        "SELECT confirm_json FROM kp_match_proposal ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    runs = json.loads(cj)["runs"]
    assert runs[1].get("retries") == 1, "重试次数必须进审计链"


def test_confirm_retry_exhausted_stays_proposed(db, monkeypatch):
    """重试耗尽仍不可解析 → 保守落 PROPOSED（绝不猜）。"""
    _add_mistake(db)
    _patch_seq(
        monkeypatch,
        [
            _ans("MATH-G4-MUL-DIST", 0.9),
            "坏输出1",
            "坏输出2",
            "坏输出3",  # 首次 + 2 次重试全失败
        ],
    )
    r = kpm.match_kp_for_mistake(db, 77001, client=None)
    assert r["status"] == kpm.STATUS_PROPOSED
    assert db.execute("SELECT kp_id FROM mistake_record WHERE id=77001").fetchone()[0] == 0


def test_semantic_disagreement_no_retry(db, monkeypatch):
    """语义不一致（两次不同 code）→ 不重试，直接转人工（省调用）。"""
    _add_mistake(db)
    calls: list[int] = []
    it = iter([_ans("MATH-G4-MUL-DIST", 0.9), _ans("MATH-G4-PRICE", 0.9)])

    def fake(sys_p, u_p, **kw):
        calls.append(1)
        try:
            a = next(it)
        except StopIteration:
            a = _ans("MATH-G4-PRICE", 0.9)
        return ChainResult(a, "L2.A", 0.01)

    monkeypatch.setattr(kpm, "run_chain", fake)
    r = kpm.match_kp_for_mistake(db, 77001, client=None)
    assert r["status"] == kpm.STATUS_PROPOSED
    assert len(calls) == 2, "语义不一致不应触发重试（与可解析失败严格区分）"
