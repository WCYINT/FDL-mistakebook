"""反馈层闭环方案 B 单测（2026-09-13 King 拍板）。

覆盖：
1. LLM 不可达（L0）→ None 且不写库
2. 输出不可解析 → 重试 1+CONFIRM_RETRY_MAX 次后 None，不写库
3. 正常路径 → trigger/action_type/mistake_id 正确、payload 键齐全
4. 同 feedback_id 重跑 → skipped=duplicate，行数不变
5. 新反馈写入时旧 PENDING 标 SKIPPED（只留最新）
6. 置信闸门 → 低置信 needs_review=True / 高置信 False
7. collect_intervention_summary 的 by_trigger / recent
8. schedule_async_feedback_loop：:memory: 返回 ok:False 不抛；文件库线程路径 ok:True
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from fdl_core.db.schema import create_schema, get_connection
from fdl_core.l2.fallback import ChainResult
from fdl_core.mistakes import feedback_loop as fl


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "fbl.db"
    conn = get_connection(str(p))
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学',"
        " '#000', 0.35, 4, 9, 1)"
    )
    conn.execute(
        "INSERT INTO mistake_record (id, user_id, kp_id, occurred_at, subject,"
        " source, error_type, diagnosis_type, attributed_by, severity, is_tamed,"
        " reappear_count, note_id)"
        " VALUES (1, 1, 0, '2026-09-05T20:00:00Z', 'MATH', 'REAL_WORK',"
        " 'METHOD', 'CALC', 'RULE_BASED', 3, 0, 2, '1')"
    )
    # 两条调度（均 DONE，避免 PENDING 部分唯一索引冲突），均挂同一错题
    _add_schedule(conn, 11, 1, "DONE")
    _add_schedule(conn, 12, 1, "DONE")
    conn.commit()
    yield conn
    conn.close()


def _add_schedule(conn, sid, mid, status):
    conn.execute(
        "INSERT INTO review_schedule (id, user_id, mistake_id, subject_id, due_date,"
        " due_session, planned_interval_days, priority_score, est_seconds, status, source)"
        " VALUES (?, 1, ?, 1, '2026-09-05', 'PM', 1, 1.0, 300, ?, 'A3')",
        (sid, mid, status),
    )


def _add_feedback(conn, fid, sid, rating, note=None):
    conn.execute(
        "INSERT INTO review_feedback (id, user_id, schedule_id, subject_id,"
        " self_rating, note) VALUES (?, 1, ?, 1, ?, ?)",
        (fid, sid, rating, note),
    )
    conn.commit()


def _ans(conf=0.9, root="CALC", brk="竖式进位丢失", alignment="consistent"):
    return json.dumps(
        {
            "breakpoint": brk,
            "root_diagnosis": root,
            "diagnosis_alignment": alignment,
            "actions": [
                {
                    "type": "SIMPLIFY",
                    "instruction": "每日 10 题限时竖式 15 分钟",
                    "duration_min": 15,
                }
            ],
            "confidence": conf,
            "summary": "证据有限，结论待积累。",
        },
        ensure_ascii=False,
    )


def _patch_chain(monkeypatch, answer: str, source: str = "L2.A", counter=None):
    def fake(sys_p, u_p, **kw):
        if counter is not None:
            counter.append(1)
        return ChainResult(answer, source, 0.01)

    monkeypatch.setattr(fl, "run_chain", fake)


# ── 1. L0 → None，不写库 ─────────────────────────────────────
def test_l0_returns_none(db, monkeypatch):
    _add_feedback(db, 101, 12, 2)
    _patch_chain(monkeypatch, "任何东西", source="L0")
    assert fl.analyze_feedback_loop(db, mistake_id=1, feedback_id=101) is None
    assert db.execute("SELECT COUNT(*) FROM intervention_action").fetchone()[0] == 0


# ── 2. 不可解析 → 重试后 None ────────────────────────────────
def test_unparseable_retries_then_none(db, monkeypatch):
    _add_feedback(db, 102, 12, 2)
    monkeypatch.setattr(fl, "CONFIRM_RETRY_MAX", 2)
    calls = []
    _patch_chain(monkeypatch, "抱歉，我无法给出 JSON", counter=calls)
    assert fl.analyze_feedback_loop(db, mistake_id=1, feedback_id=102) is None
    assert len(calls) == 1 + 2, "解析失败必须重试 CONFIRM_RETRY_MAX 次"
    assert db.execute("SELECT COUNT(*) FROM intervention_action").fetchone()[0] == 0


def test_parse_retry_recovers(db, monkeypatch):
    """第一次不可解析、第二次成功 → 正常落库（重试不是白费）。"""
    _add_feedback(db, 103, 12, 2)
    monkeypatch.setattr(fl, "CONFIRM_RETRY_MAX", 2)
    state = {"n": 0}

    def fake(sys_p, u_p, **kw):
        state["n"] += 1
        return ChainResult("乱码" if state["n"] == 1 else _ans(), "L2.A", 0.01)

    monkeypatch.setattr(fl, "run_chain", fake)
    r = fl.analyze_feedback_loop(db, mistake_id=1, feedback_id=103)
    assert r is not None and state["n"] == 2


# ── 3. 正常路径 → 落库 ───────────────────────────────────────
def test_persist_normal(db, monkeypatch):
    _add_feedback(db, 104, 12, 3)
    _patch_chain(monkeypatch, _ans(conf=0.9))
    r = fl.analyze_feedback_loop(db, mistake_id=1, feedback_id=104)
    assert r is not None and r["needs_review"] is False
    row = db.execute(
        "SELECT trigger, action_type, mistake_id, kp_id, subject_id, status, payload_json"
        " FROM intervention_action"
    ).fetchone()
    assert row[0] == "FEEDBACK_LOOP"
    assert row[1] == "SIMPLIFY"
    assert row[2] == 1
    assert row[3] is None, "kp_id=0（未挂载）应写 NULL"
    assert row[4] == 1
    assert row[5] == "PENDING"
    payload = json.loads(row[6])
    for key in (
        "feedback_id",
        "breakpoint",
        "root_diagnosis",
        "diagnosis_alignment",
        "actions",
        "confidence",
        "needs_review",
        "source_layer",
        "model_version",
        "analyzed_at",
    ):
        assert key in payload, f"payload 缺键 {key}"
    assert payload["feedback_id"] == 104
    assert payload["root_diagnosis"] == "CALC"
    assert payload["model_version"] == fl.FEEDBACK_PROMPT_VERSION
    assert r["intervention_id"] > 0


def test_dry_run_no_write(db, monkeypatch):
    _add_feedback(db, 105, 12, 3)
    _patch_chain(monkeypatch, _ans())
    r = fl.analyze_feedback_loop(db, mistake_id=1, feedback_id=105, write=False)
    assert r is not None
    assert db.execute("SELECT COUNT(*) FROM intervention_action").fetchone()[0] == 0


# ── 4. 去重 ─────────────────────────────────────────────────
def test_duplicate_feedback_skipped(db, monkeypatch):
    _add_feedback(db, 106, 12, 2)
    _patch_chain(monkeypatch, _ans())
    fl.analyze_feedback_loop(db, mistake_id=1, feedback_id=106)
    r2 = fl.analyze_feedback_loop(db, mistake_id=1, feedback_id=106)
    assert r2 is not None and r2.get("skipped") == "duplicate"
    assert db.execute("SELECT COUNT(*) FROM intervention_action").fetchone()[0] == 1


# ── 5. 新反馈 → 旧的 PENDING 标 SKIPPED ──────────────────────
def test_new_feedback_supersedes_previous(db, monkeypatch):
    _add_feedback(db, 107, 12, 2)
    _patch_chain(monkeypatch, _ans(brk="第一次断点"))
    fl.analyze_feedback_loop(db, mistake_id=1, feedback_id=107)
    # 同题再来一条新反馈（另一条 schedule）
    _add_schedule(db, 13, 1, "DONE")
    _add_feedback(db, 108, 13, 1)
    _patch_chain(monkeypatch, _ans(brk="第二次断点"))
    fl.analyze_feedback_loop(db, mistake_id=1, feedback_id=108)
    rows = db.execute("SELECT status, payload_json FROM intervention_action ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "SKIPPED", "旧 PENDING 应被标 SKIPPED"
    assert rows[1][0] == "PENDING"
    assert "第二次断点" in rows[1][1]


# ── 6. 置信闸门 ─────────────────────────────────────────────
def test_confidence_gate(db, monkeypatch):
    monkeypatch.setattr(fl, "GATE", 0.6)
    _add_feedback(db, 109, 12, 1)
    _patch_chain(monkeypatch, _ans(conf=0.3))
    low = fl.analyze_feedback_loop(db, mistake_id=1, feedback_id=109)
    assert low is not None and low["needs_review"] is True, "低置信仍写库但标待复核"

    _add_schedule(db, 14, 1, "DONE")
    _add_feedback(db, 110, 14, 4)
    _patch_chain(monkeypatch, _ans(conf=0.95))
    high = fl.analyze_feedback_loop(db, mistake_id=1, feedback_id=110)
    assert high is not None and high["needs_review"] is False


# ── 7. 报告汇总扩展 ─────────────────────────────────────────
def test_collect_intervention_summary_extended(db):
    from scripts.generate_report import collect_intervention_summary

    db.execute(
        "INSERT INTO intervention_action (user_id, mistake_id, subject_id, trigger,"
        " action_type, payload_json, status)"
        " VALUES (1, NULL, 1, 'PARETO_ROOT', 'DIAGNOSE', '{}', 'PENDING')"
    )
    db.execute(
        "INSERT INTO intervention_action (user_id, mistake_id, subject_id, trigger,"
        " action_type, payload_json, status)"
        " VALUES (1, 1, 1, 'FEEDBACK_LOOP', 'SIMPLIFY', ?, 'PENDING')",
        (json.dumps({"breakpoint": "竖式进位丢失", "needs_review": True}, ensure_ascii=False),),
    )
    db.execute(
        "INSERT INTO intervention_action (user_id, mistake_id, subject_id, trigger,"
        " action_type, payload_json, status)"
        " VALUES (1, 1, 1, 'FEEDBACK_LOOP', 'HINT', '{}', 'DONE')"
    )
    db.commit()
    s = collect_intervention_summary(db)
    assert s["available"] is True
    assert s["pending_interventions"] == 2
    assert s["total_interventions"] == 3
    assert s["by_trigger"] == {"PARETO_ROOT": 1, "FEEDBACK_LOOP": 1}
    assert len(s["recent"]) == 2
    top = s["recent"][0]
    assert top["trigger"] == "FEEDBACK_LOOP" and top["action_type"] == "SIMPLIFY"
    assert top["needs_review"] is True
    assert top["snippet"] == "竖式进位丢失"


def test_collect_intervention_summary_empty(db):
    from scripts.generate_report import collect_intervention_summary

    s = collect_intervention_summary(db)
    assert s["available"] is True
    assert s["by_trigger"] == {} and s["recent"] == []


# ── 8. 异步调度 ─────────────────────────────────────────────
def test_async_memory_db_rejected():
    conn = sqlite3.connect(":memory:")
    try:
        res = fl.schedule_async_feedback_loop(conn, mistake_id=1, feedback_id=1)
        assert res["ok"] is False
        assert "内存库" in res["reason"]
    finally:
        conn.close()


def test_async_thread_path(db, monkeypatch):
    _add_feedback(db, 111, 12, 3)
    called = []
    monkeypatch.setattr(fl, "_ASYNC_DELAY_SEC", 0.0)
    monkeypatch.setattr(fl, "analyze_feedback_loop", lambda *a, **k: called.append(k) or None)
    res = fl.schedule_async_feedback_loop(db, mistake_id=1, feedback_id=111)
    assert res == {"ok": True, "async": True}
    for _ in range(50):
        if called:
            break
        time.sleep(0.02)
    assert called, "后台线程应调用分析函数"


def test_no_feedback_returns_none(db):
    assert fl.analyze_feedback_loop(db, mistake_id=1) is None
    assert fl.analyze_feedback_loop(db, mistake_id=999) is None


# ── 9. kp_state 读取口径（2026-09-13 与 p7-kpstate 对齐）──────
def _add_kp(conn, kp_id=100, code="G4-CALC-001"):
    """挂载用的知识点行（kp_state.kp_id 有 FK 约束，必须先有 knowledge_point）。"""
    conn.execute(
        "INSERT INTO knowledge_point (id, subject_id, code, name, grade_level,"
        " bloom_level, abstraction_level, importance_weight, base_difficulty,"
        " est_learn_minutes, est_review_seconds, kp_type, tier, graph_version,"
        " valid_from)"
        " VALUES (?, 1, ?, '三位数乘法的进位', 4, 2, 2, 0.8, 0.5, 25.0, 300,"
        " 'CONCEPT', 'CORE', 'v1', '2026-09-01')",
        (kp_id, code),
    )
    conn.commit()


def _add_kp_state(
    conn,
    *,
    user_id=1,
    kp_id=100,
    status="REVIEWING",
    mastery_adj=0.42,
    p_score=0.71,
    r_val=0.33,
    exposure=4,
    answers=3,
):
    conn.execute(
        "INSERT INTO kp_state (user_id, kp_id, subject_id, status, stability_days,"
        " difficulty, mastery_adj, performance_score, retrievability,"
        " exposure_count, total_answer_count)"
        " VALUES (?, ?, 1, ?, 3.0, 5.0, ?, ?, ?, ?, ?)",
        (user_id, kp_id, status, mastery_adj, p_score, r_val, exposure, answers),
    )
    conn.commit()


def test_kp_state_evidence_enriched(db):
    db.execute("UPDATE mistake_record SET kp_id=100 WHERE id=1")
    _add_kp(db)
    db.commit()
    _add_feedback(db, 120, 12, 2)
    _add_kp_state(db)
    ev = fl.collect_feedback_evidence(db, mistake_id=1)
    ks = ev["kp_state"]
    assert ks["status"] == "REVIEWING"
    assert ks["m_adj"] == 0.42
    assert ks["stability_days"] == 3.0
    assert ks["performance_score"] == 0.71
    assert ks["retrievability"] == 0.33
    assert ks["exposure_count"] == 4
    assert ks["total_answer_count"] == 3
    assert ev["degraded"] == [], "有 KP 状态时不应进 degraded"


def test_kp_state_is_user_scoped(db):
    """UNIQUE 是 (user_id, kp_id)：多用户下不得取到别人的行。"""
    db.execute("UPDATE mistake_record SET kp_id=100 WHERE id=1")
    _add_kp(db)
    db.commit()
    _add_feedback(db, 121, 12, 2)
    _add_kp_state(db, user_id=2, kp_id=100, mastery_adj=0.99, p_score=0.99, r_val=0.99)
    _add_kp_state(db, user_id=1, kp_id=100, mastery_adj=0.11, p_score=0.21, r_val=0.22)
    ev = fl.collect_feedback_evidence(db, mistake_id=1)
    assert ev["kp_state"]["m_adj"] == 0.11, "必须按 mistake 的 user_id 过滤"


def test_kp_state_unlearned_treated_as_prior(db):
    """有行 != 已复习过：UNLEARNED 必须标注为冷启动先验，不当真实表现。"""
    db.execute("UPDATE mistake_record SET kp_id=100 WHERE id=1")
    _add_kp(db)
    db.commit()
    _add_feedback(db, 122, 12, 2)
    _add_kp_state(db, status="UNLEARNED", mastery_adj=0.3, exposure=0, answers=0)
    ev = fl.collect_feedback_evidence(db, mistake_id=1)
    _, user_prompt = fl.build_feedback_prompt(ev)
    assert "尚无作答证据" in user_prompt
    assert "先验" in user_prompt


def test_kp_state_evidence_reaches_prompt(db):
    """P 与 R 两条正交线索要真的进 prompt（断点识别的关键输入）。"""
    db.execute("UPDATE mistake_record SET kp_id=100 WHERE id=1")
    _add_kp(db)
    db.commit()
    _add_feedback(db, 123, 12, 2)
    _add_kp_state(db, p_score=0.71, r_val=0.33)
    ev = fl.collect_feedback_evidence(db, mistake_id=1)
    _, user_prompt = fl.build_feedback_prompt(ev)
    assert "P=0.71" in user_prompt
    assert "R=0.33" in user_prompt
    assert "04:00 锚点值" in user_prompt


def test_kp_unmounted_skips_kp_state_lookup(db):
    """kp_id=0（未挂载）→ 不做 KP 查询，prompt 明示未挂载，不误报 degraded 记录。"""
    _add_feedback(db, 124, 12, 2)
    ev = fl.collect_feedback_evidence(db, mistake_id=1)
    assert ev["kp_id"] == 0
    assert ev["kp_state"] is None
    _, user_prompt = fl.build_feedback_prompt(ev)
    assert "未挂载知识点" in user_prompt
