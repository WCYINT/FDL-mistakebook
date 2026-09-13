"""Pareto 干预分析单测（2026-09-12 King 需求 2）。

覆盖：
1. 证据采集：错因分布占比/驯服/挂载 + 反馈评分
2. LLM 不可达（L0）→ 返回 None，不写库（不猜）
3. 输出不可解析 → 返回 None
4. 正常分析 → 解析、排序、落库（PARETO_ROOT）
5. 幂等：重跑把旧 PENDING 标 SKIPPED，只留最新
6. latest_analysis 读取
"""

from __future__ import annotations

import json

import pytest

from fdl_core.db.schema import create_schema, get_connection
from fdl_core.l2.fallback import ChainResult
from fdl_core.mistakes import intervention as iv


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "iv.db"
    conn = get_connection(str(p))
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学',"
        " '#000', 0.35, 4, 9, 1)"
    )
    conn.commit()
    yield conn
    conn.close()


def _add_mistake(conn, mid, diag, linked=0, tamed=0):
    conn.execute(
        "INSERT INTO mistake_record (id, user_id, kp_id, occurred_at, subject,"
        " source, error_type, diagnosis_type, attributed_by, severity, is_tamed, note_id)"
        " VALUES (?, 1, ?, '2026-09-05T20:00:00Z', 'MATH', 'REAL_WORK',"
        " 'METHOD', ?, 'RULE_BASED', 3, ?, ?)",
        (mid, 100 + mid if linked else 0, diag, tamed, str(mid)),
    )


def _patch_chain(monkeypatch, answer: str, source: str = "L2.A"):
    def fake(sys_p, u_p, **kw):
        return ChainResult(answer, source, 0.01)

    monkeypatch.setattr(iv, "run_chain", fake)


def _ans(roots=None, summary="前 2 项累计 80%，聚焦突破", vital=2):
    return json.dumps(
        {
            "roots": roots
            if roots is not None
            else [
                {
                    "diagnosis": "MISREAD",
                    "share": 0.5,
                    "cumulative": 0.5,
                    "evidence": "占比 50% 且驯服 0",
                    "actions": ["圈画关键信息", "口头复述题意"],
                    "priority": 1,
                },
                {
                    "diagnosis": "CONCEPT",
                    "share": 0.3,
                    "cumulative": 0.8,
                    "evidence": "挂载少",
                    "actions": ["口述定义"],
                    "priority": 2,
                },
            ],
            "vital_few_count": vital,
            "summary": summary,
        }
    )


# ── 1. 证据采集 ──────────────────────────────────────────────
def test_collect_evidence(db):
    _add_mistake(db, 1, "MISREAD", linked=1)
    _add_mistake(db, 2, "MISREAD")
    _add_mistake(db, 3, "CALC")
    db.execute(
        "INSERT INTO review_feedback (user_id, subject_id, self_rating) VALUES (1, 1, 3), (1, 1, 2)"
    )
    db.commit()
    ev = iv.collect_evidence(db)
    assert ev["mistake_total"] == 3
    top = ev["diagnosis_dist"][0]
    assert top["diagnosis"] == "MISREAD" and top["count"] == 2
    assert abs(top["share"] - 2 / 3) < 0.01
    assert top["linked"] == 1
    assert ev["recent_ratings"] == [2, 3] or ev["recent_ratings"] == [3, 2]


# ── 2. LLM 不可达 → None，不写库 ────────────────────────────
def test_llm_unreachable_returns_none(db, monkeypatch):
    _add_mistake(db, 1, "CALC")
    _patch_chain(monkeypatch, "任何东西", source="L0")
    assert iv.pareto_intervention_analysis(db) is None
    assert db.execute("SELECT COUNT(*) FROM intervention_action").fetchone()[0] == 0, (
        "LLM 不可达绝不写库"
    )


def test_unparseable_returns_none(db, monkeypatch):
    _add_mistake(db, 1, "CALC")
    _patch_chain(monkeypatch, "抱歉我没法分析")
    assert iv.pareto_intervention_analysis(db) is None
    assert db.execute("SELECT COUNT(*) FROM intervention_action").fetchone()[0] == 0


# ── 3. 正常分析 → 落库 ───────────────────────────────────────
def test_analysis_persist(db, monkeypatch):
    _add_mistake(db, 1, "MISREAD")
    _add_mistake(db, 2, "CONCEPT")
    _patch_chain(monkeypatch, _ans())
    r = iv.pareto_intervention_analysis(db, write=True)
    assert r is not None
    assert len(r["roots"]) == 2
    assert r["roots"][0]["diagnosis"] == "MISREAD"
    assert r["vital_few_count"] == 2
    row = db.execute(
        "SELECT trigger, action_type, status, payload_json FROM intervention_action"
    ).fetchone()
    assert row[0] == "PARETO_ROOT" and row[1] == "DIAGNOSE" and row[2] == "PENDING"
    payload = json.loads(row[3])
    assert payload["roots"][0]["diagnosis"] == "MISREAD"
    assert len(payload["roots"][0]["actions"]) == 2


def test_analysis_dry_run_no_write(db, monkeypatch):
    _add_mistake(db, 1, "CALC")
    _patch_chain(monkeypatch, _ans())
    r = iv.pareto_intervention_analysis(db, write=False)
    assert r is not None
    assert db.execute("SELECT COUNT(*) FROM intervention_action").fetchone()[0] == 0


# ── 4. 幂等：重跑只留最新 ────────────────────────────────────
def test_rerun_supersedes_previous(db, monkeypatch):
    _add_mistake(db, 1, "MISREAD")
    _patch_chain(monkeypatch, _ans())
    iv.pareto_intervention_analysis(db, write=True)
    _patch_chain(monkeypatch, _ans(summary="第二次分析", vital=1))
    iv.pareto_intervention_analysis(db, write=True)
    rows = db.execute(
        "SELECT status, payload_json FROM intervention_action"
        " WHERE trigger='PARETO_ROOT' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "SKIPPED", "旧分析应被标 SKIPPED"
    assert rows[1][0] == "PENDING"
    assert "第二次分析" in rows[1][1]


def test_latest_analysis_reads_newest(db, monkeypatch):
    _add_mistake(db, 1, "MISREAD")
    _patch_chain(monkeypatch, _ans(summary="一号"))
    iv.pareto_intervention_analysis(db, write=True)
    _patch_chain(monkeypatch, _ans(summary="二号"))
    iv.pareto_intervention_analysis(db, write=True)
    latest = iv.latest_analysis(db)
    assert latest["summary"] == "二号"
    assert "created_at" in latest


def test_no_data_returns_none(db):
    assert iv.pareto_intervention_analysis(db, client=None) is None
