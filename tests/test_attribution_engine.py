"""attribution_engine 单测（2026-09-12 King 拍板版）。

覆盖：
1. 种子幂等 / taxonomy 读取与缓存失效 / 无表降级
2. LLM 不可达（L0 降级）→ 落 PROPOSED、confidence=0、**不写**权威字段
3. LLM 输出不可解析 → PROPOSED
4. 高置信自动晋升 → diagnosis_type 写入 + attributed_by='LLM_ASSISTED'
5. 低置信 → 留 PROPOSED；人工 accept_proposal 后才写入
6. LLM 提议新类别 → 写 taxonomy_candidate，**绝不自动新增**（King 政策）
7. code 不在 ACTIVE taxonomy → 转人工复核
8. accept / reject 提案 + list_proposals

LLM 通过 monkeypatch run_chain 打桩，不打真实网络。
"""

from __future__ import annotations

import json

import pytest

from fdl_core.db.schema import create_schema, get_connection
from fdl_core.l2.fallback import ChainResult
from fdl_core.mistakes import attribution_engine as eng
from fdl_core.mistakes import attribution_taxonomy as tx


# ── 建库与造数 ──────────────────────────────────────────────
@pytest.fixture()
def db(tmp_path):
    """每用例独立临时库（不碰生产库）；建表后种子化，模拟迁移后的真实状态。"""
    p = tmp_path / "attr.db"
    conn = get_connection(str(p))
    create_schema(conn)
    tx.ensure_seeded(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000',"
        " 0.35, 4, 9, 1)"
    )
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
        "source_ref": "测试来源 P1 Q2",
        "error_type": "METHOD",
        "attributed_by": "RULE_BASED",
        "attribution_confidence": 0.0,
        "severity": 3,
        "is_tamed": 0,
        "reappear_count": 0,
        "note_id": f"n{mid}",
    }
    base.update(over)
    cols = ", ".join(base)
    ph = ", ".join("?" for _ in base)
    conn.execute(f"INSERT INTO mistake_record ({cols}) VALUES ({ph})", list(base.values()))


def _add_feedback(conn, mid=77001, note="把这道题讲给妈妈听，讲到一半卡住了", rating=1):
    """造 schedule + feedback，返回 (schedule_id, feedback_id)。"""
    conn.execute(
        "INSERT INTO review_schedule (id, user_id, mistake_id, subject_id, due_date,"
        " due_session, planned_interval_days, priority_score, est_seconds, status,"
        " source) VALUES (500, 1, ?, 1, '2026-09-05', 'PM', 1, 90.0, 120, 'PENDING',"
        " 'answer')",
        (mid,),
    )
    cur = conn.execute(
        "INSERT INTO review_feedback (user_id, schedule_id, subject_id, self_rating,"
        " duration_seconds, note, attachments_json, srs_interval_days)"
        " VALUES (1, 500, 1, ?, 95, ?, NULL, 1)",
        (rating, note),
    )
    conn.commit()
    return 500, cur.lastrowid


def _patch_chain(monkeypatch, answer: str, source: str = "L2.A"):
    """把引擎里的 run_chain 换成受控桩（每次调用返回同一答案）。"""
    monkeypatch.setattr(
        eng,
        "run_chain",
        lambda sys_p, u_p, **kw: ChainResult(answer, source, 0.01),
    )


def _patch_chain_seq(monkeypatch, answers: list[str], source: str = "L2.A"):
    """序列桩：第 n 次调用返回 answers[n-1]，耗尽后停在最后一个。

    用于一致闸门测试——两次独立调用必须能给出不同答案。
    """
    it = iter(answers)

    def fake(sys_p, u_p, **kw):
        try:
            a = next(it)
        except StopIteration:
            a = answers[-1]
        return ChainResult(a, source, 0.01)

    monkeypatch.setattr(eng, "run_chain", fake)


# ── 1. taxonomy 基础 ────────────────────────────────────────
def test_seed_idempotent_and_cache_invalidate(db):
    # fixture 已种子化 → 再次调用应为 0（幂等，不覆盖人工调整）
    assert tx.ensure_seeded(db) == 0
    w = tx.load_weights(db)
    assert w["CONCEPT"] == 0.30 and w["CARELESS"] == 0.05
    tx.update_weight(db, "CONCEPT", 0.42)
    assert tx.load_weights(db)["CONCEPT"] == 0.42, "改权后缓存必须失效重载"


def test_fallback_when_no_table(tmp_path):
    import sqlite3

    raw = sqlite3.connect(":memory:")
    assert tx.load_active(raw) == {}
    assert tx.load_weights(raw) == tx.BUILTIN_FALLBACK_WEIGHTS
    raw.close()


def test_fallback_when_table_exists_but_empty(tmp_path):
    """表存在但为空（新库未跑迁移）→ 引擎不得"永久转人工"。"""
    p = tmp_path / "empty.db"
    conn = get_connection(str(p))
    create_schema(conn)  # 建表但**不种子**
    assert tx.has_taxonomy_table(conn)
    act = tx.load_active(conn)
    assert act, "空表必须回落到内置默认，而不是空集"
    assert set(act) == set(tx.BUILTIN_FALLBACK_WEIGHTS)
    assert tx.is_valid_code(conn, "CONCEPT")
    conn.close()


# ── 2/3. LLM 不可达 / 不可解析 ──────────────────────────────
def test_llm_unreachable_lands_proposed_not_authoritative(db, monkeypatch):
    _add_mistake(db)
    _add_feedback(db)
    _patch_chain(monkeypatch, "", "L0")
    r = eng.analyze_feedback(db, 77001, client=None)
    assert r["ok"] and r["status"] == eng.STATUS_PROPOSED and r["confidence"] == 0.0
    # 权威字段必须未被触碰
    row = db.execute(
        "SELECT diagnosis_type, attributed_by FROM mistake_record WHERE id=77001"
    ).fetchone()
    assert row[0] == "CALC" and row[1] == "RULE_BASED"


def test_unparseable_output_lands_proposed(db, monkeypatch):
    _add_mistake(db)
    _add_feedback(db)
    _patch_chain(monkeypatch, "我觉得应该是概念问题。", "L2.A")  # 无 JSON
    r = eng.analyze_feedback(db, 77001, client=None)
    assert r["status"] == eng.STATUS_PROPOSED


# ── 4. 高置信自动晋升 ───────────────────────────────────────
def test_high_confidence_auto_accept(db, monkeypatch):
    _add_mistake(db)
    _add_feedback(db)
    _patch_chain(
        monkeypatch,
        json.dumps(
            {
                "code": "CONCEPT",
                "confidence": 0.92,
                "rationale": "讲到一半卡住，属概念不清",
                "evidence_quote": "讲到一半卡住了",
                "new_category": None,
            }
        ),
    )
    r = eng.analyze_feedback(db, 77001, client=None)
    assert r["status"] == eng.STATUS_AUTO and r["gate"] == "auto"
    row = db.execute(
        "SELECT diagnosis_type, attributed_by, attribution_confidence"
        " FROM mistake_record WHERE id=77001"
    ).fetchone()
    assert row[0] == "CONCEPT" and row[1] == "LLM_ASSISTED" and row[2] == 0.92
    # error_type（Frank/家长元认知轨道）必须未被覆盖
    assert (
        db.execute("SELECT error_type FROM mistake_record WHERE id=77001").fetchone()[0] == "METHOD"
    )


# ── 5. 低置信转人工 ─────────────────────────────────────────
def test_low_confidence_waits_for_human(db, monkeypatch):
    _add_mistake(db)
    _add_feedback(db)
    _patch_chain(
        monkeypatch,
        json.dumps(
            {
                "code": "MISREAD",
                "confidence": 0.45,
                "rationale": "证据不足",
                "evidence_quote": "",
                "new_category": None,
            }
        ),
    )
    r = eng.analyze_feedback(db, 77001, client=None)
    assert r["status"] == eng.STATUS_PROPOSED and r["gate"] == "manual_queue"
    pid = r["proposal_id"]
    # 人工采纳后才写
    acc = eng.accept_proposal(db, pid, decided_by="PARENT")
    assert acc["ok"] and acc["written"]
    row = db.execute(
        "SELECT diagnosis_type, attributed_by FROM mistake_record WHERE id=77001"
    ).fetchone()
    assert row == ("MISREAD", "LLM_ASSISTED")


# ── 6. 新类别 → 候选，绝不自动新增 ──────────────────────────
def test_new_category_goes_to_candidate_only(db, monkeypatch):
    _add_mistake(db)
    _add_feedback(db)
    _patch_chain(
        monkeypatch,
        json.dumps(
            {
                "code": "CONCEPT",
                "confidence": 0.9,
                "rationale": "现有类别不够细",
                "evidence_quote": "x",
                "new_category": {
                    "code": "CONCEPT.SIGN",
                    "label": "符号意识缺失",
                    "parent_code": "CONCEPT",
                    "rationale": "多次在符号上出错",
                },
            }
        ),
    )
    r = eng.analyze_feedback(db, 77001, client=None)
    assert r["status"] == eng.STATUS_AUTO
    cands = tx.list_candidates(db)
    assert len(cands) == 1 and cands[0]["candidate_code"] == "CONCEPT.SIGN"
    # 新类别绝不能直接出现在 ACTIVE taxonomy
    assert not tx.is_valid_code(db, "CONCEPT.SIGN")
    # 人工晋级后才可用
    tx.promote_candidate(db, cands[0]["id"], decided_by="PARENT", weight=0.25)
    assert tx.is_valid_code(db, "CONCEPT.SIGN")
    assert tx.load_weights(db)["CONCEPT.SIGN"] == 0.25


# ── 7. 非法 code 转人工 ─────────────────────────────────────
def test_invalid_code_routes_to_manual(db, monkeypatch):
    _add_mistake(db)
    _add_feedback(db)
    _patch_chain(
        monkeypatch,
        json.dumps(
            {
                "code": "NOT_A_CODE",
                "confidence": 0.99,
                "rationale": "幻觉类别",
                "evidence_quote": "",
                "new_category": None,
            }
        ),
    )
    r = eng.analyze_feedback(db, 77001, client=None)
    assert r["status"] == eng.STATUS_PROPOSED
    row = db.execute("SELECT diagnosis_type FROM mistake_record WHERE id=77001").fetchone()
    assert row[0] == "CALC", "幻觉 code 绝不能写进权威字段"


# ── 8. 复核 API ─────────────────────────────────────────────
def test_list_and_reject_proposals(db, monkeypatch):
    _add_mistake(db)
    _add_feedback(db)
    _patch_chain(
        monkeypatch,
        json.dumps(
            {
                "code": "CALC",
                "confidence": 0.5,
                "rationale": "算错",
                "evidence_quote": "",
                "new_category": None,
            }
        ),
    )
    r = eng.analyze_feedback(db, 77001, client=None)
    pend = eng.list_proposals(db, status=eng.STATUS_PROPOSED)
    assert [p["id"] for p in pend] == [r["proposal_id"]]
    rej = eng.reject_proposal(db, r["proposal_id"], decided_by="PARENT", reason="证据不足")
    assert rej["ok"]
    assert eng.list_proposals(db, status=eng.STATUS_PROPOSED) == []


# ── 9. 解析器鲁棒性 ─────────────────────────────────────────
def test_parse_llm_json_tolerates_wrappers():
    assert eng.parse_llm_json('```json\n{"code":"CALC","confidence":0.5}\n```')["code"] == "CALC"
    assert eng.parse_llm_json('前置说明 {"code":"NORM","confidence":0.9} 后置')["code"] == "NORM"
    assert eng.parse_llm_json("") is None
    assert eng.parse_llm_json("完全没有 json") is None


# ── 10. review_feedback 无记录时也能归因（只凭元数据）───────
def test_no_feedback_still_analyzes(db, monkeypatch):
    _add_mistake(db)
    _patch_chain(
        monkeypatch,
        json.dumps(
            {
                "code": "CONCEPT",
                "confidence": 0.85,
                "rationale": "来源与错答提示概念问题",
                "evidence_quote": "",
                "new_category": None,
            }
        ),
    )
    r = eng.analyze_feedback(db, 77001, client=None)
    assert r["ok"] and r["status"] == eng.STATUS_AUTO
    assert r["written"]


# ── 11. 异步调度：内存库拒绝 / 真库去重 ─────────────────────
def test_async_schedule_guards(db):
    r1 = eng.schedule_async_analysis(db, mistake_id=77001)
    assert r1["ok"]
    r2 = eng.schedule_async_analysis(db, mistake_id=77001)
    assert r2.get("duplicate"), "同任务在跑必须去重"


# ── 12. validate_monster_tag：LLM_ASSISTED 合法 / LLM_SUGGEST 仍拒绝 ──
def test_attributed_by_policy():
    from fdl_core.mistakes.attribution import validate_monster_tag

    # 注意 error_type 走 MONSTERS 旧口径词汇（METHOD/CONFUSION/...），不是新口径
    validate_monster_tag("METHOD", "LLM_ASSISTED")  # 新合法值
    with pytest.raises(ValueError, match="LLM_SUGGEST"):
        validate_monster_tag("METHOD", "LLM_SUGGEST")  # 既有禁令不变


# ── 13. 一致闸门（King 2026-09-12：两次独立一致才自动）────────
def test_two_agreeing_runs_auto_accept(db, monkeypatch):
    """两次独立调用同 code 且置信都过阈 → 自动写入。"""
    _add_mistake(db)
    _add_feedback(db)
    ans = json.dumps(
        {
            "code": "CONCEPT",
            "confidence": 0.9,
            "rationale": "稳定结论",
            "evidence_quote": "",
            "new_category": None,
        }
    )
    _patch_chain_seq(monkeypatch, [ans, ans])
    r = eng.analyze_feedback(db, 77001, client=None)  # 默认 confirm_runs=2
    assert r["status"] == eng.STATUS_AUTO and r["confirm"]["agreed"] is True
    assert len(r["confirm"]["runs"]) == 2
    row = db.execute(
        "SELECT diagnosis_type, attributed_by FROM mistake_record WHERE id=77001"
    ).fetchone()
    assert row == ("CONCEPT", "LLM_ASSISTED")


def test_disagreement_blocks_auto(db, monkeypatch):
    """两次结论不一致 → 即使都高置信也必须转人工，绝不写权威字段。"""
    _add_mistake(db)
    _add_feedback(db)
    a1 = json.dumps(
        {
            "code": "CONCEPT",
            "confidence": 0.9,
            "rationale": "r1",
            "evidence_quote": "",
            "new_category": None,
        }
    )
    a2 = json.dumps(
        {
            "code": "MISREAD",
            "confidence": 0.9,
            "rationale": "r2",
            "evidence_quote": "",
            "new_category": None,
        }
    )
    _patch_chain_seq(monkeypatch, [a1, a2])
    r = eng.analyze_feedback(db, 77001, client=None)
    assert r["status"] == eng.STATUS_PROPOSED and r["confirm"]["agreed"] is False
    assert (
        db.execute("SELECT attributed_by FROM mistake_record WHERE id=77001").fetchone()[0]
        == "RULE_BASED"
    ), "不一致时绝不能写权威字段"


def test_second_run_low_confidence_blocks_auto(db, monkeypatch):
    """第二次置信不足 → 不放行（可复现性要求两次都过阈）。"""
    _add_mistake(db)
    _add_feedback(db)
    a1 = json.dumps(
        {
            "code": "CONCEPT",
            "confidence": 0.95,
            "rationale": "r1",
            "evidence_quote": "",
            "new_category": None,
        }
    )
    a2 = json.dumps(
        {
            "code": "CONCEPT",
            "confidence": 0.5,
            "rationale": "r2",
            "evidence_quote": "",
            "new_category": None,
        }
    )
    _patch_chain_seq(monkeypatch, [a1, a2])
    r = eng.analyze_feedback(db, 77001, client=None)
    assert r["status"] == eng.STATUS_PROPOSED
    assert r["confirm"]["agreed"] is False


def test_low_first_run_skips_confirm_call(db, monkeypatch):
    """第一次就低置信 → 不浪费复核调用（只调 1 次）。"""
    _add_mistake(db)
    _add_feedback(db)
    calls = []

    def fake(sys_p, u_p, **kw):
        calls.append(1)
        return ChainResult(
            json.dumps(
                {
                    "code": "CALC",
                    "confidence": 0.4,
                    "rationale": "低",
                    "evidence_quote": "",
                    "new_category": None,
                }
            ),
            "L2.A",
            0.01,
        )

    monkeypatch.setattr(eng, "run_chain", fake)
    r = eng.analyze_feedback(db, 77001, client=None)
    assert len(calls) == 1, "首跑低置信不应触发复核跑"
    assert r["status"] == eng.STATUS_PROPOSED


def test_confirm_runs_one_keeps_single_call_semantics(db, monkeypatch):
    """显式 confirm_runs=1 → 回退旧单跑语义（对照实验/降级用）。"""
    _add_mistake(db)
    _add_feedback(db)
    ans = json.dumps(
        {
            "code": "NORM",
            "confidence": 0.9,
            "rationale": "r",
            "evidence_quote": "",
            "new_category": None,
        }
    )
    calls = []

    def fake(sys_p, u_p, **kw):
        calls.append(1)
        return ChainResult(ans, "L2.A", 0.01)

    monkeypatch.setattr(eng, "run_chain", fake)
    r = eng.analyze_feedback(db, 77001, client=None, confirm_runs=1)
    assert len(calls) == 1
    assert r["status"] == eng.STATUS_AUTO


def test_confirm_audit_recorded_in_proposal(db, monkeypatch):
    """每次独立跑的结果必须落进 alt_json 审计字段（全链可回溯）。"""
    _add_mistake(db)
    _add_feedback(db)
    ans = json.dumps(
        {
            "code": "CONCEPT",
            "confidence": 0.9,
            "rationale": "r",
            "evidence_quote": "",
            "new_category": None,
        }
    )
    _patch_chain_seq(monkeypatch, [ans, ans])
    r = eng.analyze_feedback(db, 77001, client=None)
    audit = json.loads(
        db.execute(
            "SELECT alt_json FROM attribution_proposal WHERE id=?", (r["proposal_id"],)
        ).fetchone()[0]
    )
    assert audit["confirm"]["policy"] == "two_run_agreement"
    assert len(audit["confirm"]["runs"]) == 2
