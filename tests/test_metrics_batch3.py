"""批次 3（P2-13~14 指标 MT）验收测试。

覆盖：R(t) 衰减、04:00 批处理（🔴 只跑时间驱动跃迁）、daily_metric 聚合、
NMKP 计算、MDE 噪声过滤、静默期隐藏。
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from fdl_core.db.schema import create_schema
from fdl_core.metrics import (
    aggregate_daily,
    aggregate_weekly,
    compute_nmkp,
    ensure_metric_tables,
    retrievability,
    run_daily_batch,
    silent_period,
    week_start_of,
)
from fdl_core.srs.state_machine import ensure_transition_table


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000', 0.35, 4, 9, 1)"
    )
    conn.execute(
        "INSERT INTO knowledge_point (id, subject_id, code, name, grade_level, bloom_level,"
        " abstraction_level, importance_weight, base_difficulty, est_learn_minutes,"
        " est_review_seconds, kp_type, tier, graph_version, valid_from)"
        " VALUES (1, 1, 'MATH-A', 'A', 4, 2, 2, 1.0, 4.8, 1.5, 45,"
        " 'SKILL', 'L0', '2026.1', '2026-09-01')"
    )
    conn.execute(
        "INSERT INTO question (id, subject_id, primary_kp_id, question_type, stem, answer,"
        " difficulty, expected_seconds, variant_type, source)"
        " VALUES (1, 1, 1, 'CALC', 'q', 'a', 3.5, 30, 'ORIGINAL', 'MANUAL')"
    )
    conn.commit()
    yield conn
    conn.close()


def _add_kp_state(
    conn,
    kp_id,
    status,
    *,
    stab=5.0,
    r=None,
    n_eff=0.0,
    total=0,
    m_adj=0.5,
    last_review=None,
    entered_m=None,
    p=0.45,
    g=0.2,
):
    conn.execute(
        "INSERT INTO kp_state (id, user_id, kp_id, subject_id, status, stability_days,"
        " difficulty, retrievability, effective_answer_count, total_answer_count,"
        " mastery_adj, last_review_at, entered_mastered_at, performance_score, depth_score)"
        " VALUES (?,1,?,1,?,?,?,?,?,?,?,?,?,?,?)",
        (kp_id, kp_id, status, stab, 4.6, r, n_eff, total, m_adj, last_review, entered_m, p, g),
    )
    conn.commit()


# ── R(t) 衰减 ─────────────────────────────────────────────
def test_retrievability_decay():
    assert retrievability(5.0, 0) == 1.0
    r1 = retrievability(5.0, 5)
    r2 = retrievability(5.0, 20)
    assert 0 < r2 < r1 < 1.0  # 时间越久保留率越低


# ── P2-13 批处理 ──────────────────────────────────────────
def test_batch_refreshes_r_and_runs_time_transitions(db):
    # STRUGGLING + R=0.2 → T11 → REGRESSED（工程推断规则）
    _add_kp_state(db, 1, "STRUGGLING", stab=0.5, r=None, last_review="2026-08-15T00:00:00Z")
    result = run_daily_batch(db, 1, today=date(2026, 9, 3))
    assert any(t["rule"] == "T11" for t in result["transitions"])
    row = db.execute("SELECT status FROM kp_state WHERE kp_id=1").fetchone()
    assert row[0] == "REGRESSED"


def test_batch_never_upgrades(db):
    """🔴 铁律：REVIEWING 态即使 R 高 S 大，批处理也绝不能升级（T4 只在作答后）。"""
    _add_kp_state(db, 1, "REVIEWING", stab=100.0, last_review="2026-09-02T00:00:00Z")
    result = run_daily_batch(db, 1, today=date(2026, 9, 3))
    fired = [t for t in result["transitions"] if t["rule"] is not None]
    assert not any(t["to"] in ("MASTERED", "CONSOLIDATED", "LEARNING") for t in fired)


def test_daily_metric_aggregated(db):
    # 造昨日作答数据
    db.execute(
        "INSERT INTO study_session (id, user_id, session_date, session_slot, trigger_type,"
        " started_at, duration_sec, effective_sec, is_valid)"
        " VALUES (1, 1, '2026-09-02', 'AM', 'SELF', '2026-09-02T00:00:00Z', 480, 460, 1)"
    )
    db.execute(
        "INSERT INTO daily_task (id, user_id, task_date, session_slot, task_type, kp_id,"
        " subject_id, title, est_seconds, sort_order, status, xp_earned)"
        " VALUES (1, 1, '2026-09-02', 'AM', 'REVIEW', 1, 1, 't', 45, 0, 'DONE', 12)"
    )
    for i, grade in enumerate((2, 0, 2)):
        db.execute(
            "INSERT INTO answer_log (user_id, session_id, task_id, question_id, kp_id,"
            " task_type, grade, is_correct, expected_seconds, input_mode, is_valid_evidence,"
            " difficulty_at_time, r_at_answer, s_before, s_after, d_before, d_after,"
            " answered_at) VALUES (1, 1, 1, 1, 1, 'REVIEW', ?, ?, 30, 'KEYBOARD', 1,"
            " 4.6, 0.9, 5.0, 5.0, 4.6, 4.6, ?)",
            (grade, int(grade >= 1), f"2026-09-02T0{i + 1}:00:00Z"),
        )
    db.commit()
    m = aggregate_daily(db, 1, date(2026, 9, 2))
    assert m["answers"] == 3 and m["pass_rate"] == pytest.approx(2 / 3)
    row = db.execute(
        "SELECT session_count, xp_earned, kp_learning FROM daily_metric"
        " WHERE metric_date='2026-09-02'"
    ).fetchone()
    assert row[0] == 1 and row[1] == 12


def test_daily_metric_upsert_idempotent(db):
    m1 = aggregate_daily(db, 1, date(2026, 9, 2))
    m2 = aggregate_daily(db, 1, date(2026, 9, 2))
    assert m1 == m2
    assert db.execute("SELECT COUNT(*) FROM daily_metric").fetchone()[0] == 1


# ── P2-14 周指标 / NMKP ───────────────────────────────────
def test_week_start_is_monday():
    assert week_start_of(date(2026, 9, 3)).weekday() == 0  # 周四 → 周一
    assert week_start_of(date(2026, 9, 3)) == date(2026, 8, 31)


def test_nmkp_from_transition_log(db):
    ensure_transition_table(db)
    for rule, ts in (
        ("T4", "2026-09-02T10:00:00Z"),
        ("T5", "2026-09-02T11:00:00Z"),
        ("T6", "2026-09-03T10:00:00Z"),
        ("T9", "2026-09-03T11:00:00Z"),
    ):
        db.execute(
            "INSERT INTO kp_state_transition (user_id, kp_id, from_status, to_status, rule,"
            " is_upgrade, triggered_by, conditions_snapshot, created_at)"
            " VALUES (1, 1, 'X', 'Y', ?, 1, 'answer', '{}', ?)",
            (rule, ts),
        )
    db.commit()
    # 周 08-31~09-06：T4+T5=2 升，T6=1 降（T9 非回退不计）→ NMKP=1
    assert compute_nmkp(db, 1, date(2026, 8, 31), date(2026, 9, 6)) == 1


def test_mde_noise_filter(db):
    ensure_metric_tables(db)
    ensure_transition_table(db)
    ensure_transition_table(db)
    # 上周 NMKP=1
    db.execute("INSERT INTO weekly_metric (user_id, week_start, nmkp) VALUES (1, '2026-08-24', 1)")
    db.commit()
    # 本周 NMKP=2（Δ=1 < MDE=2）→ FLAT
    for rule, ts in (("T4", "2026-09-01T10:00:00Z"), ("T4", "2026-09-02T10:00:00Z")):
        db.execute(
            "INSERT INTO kp_state_transition (user_id, kp_id, from_status, to_status, rule,"
            " is_upgrade, triggered_by, conditions_snapshot, created_at)"
            " VALUES (1, 1, 'X', 'Y', ?, 1, 'answer', '{}', ?)",
            (rule, ts),
        )
    db.commit()
    r = aggregate_weekly(db, 1, week_start=date(2026, 8, 31))
    assert r["nmkp"] == 2 and r["mde_flag"] == "FLAT"


def test_silent_period_hides_for_king(db):
    """静默期：无 MASTERED 且无 n_eff≥5 的 KP → silent=1，NMKP 对 King 隐藏。"""
    _add_kp_state(db, 1, "REVIEWING", n_eff=2.0)
    assert silent_period(db, 1)
    r = aggregate_weekly(db, 1, week_start=date(2026, 8, 31))
    assert r["silent"] is True


def test_silent_period_lifted_by_mastered(db):
    """解除信号①：出现第 1 个 MASTERED → 静默期结束。"""
    _add_kp_state(db, 1, "MASTERED", n_eff=6.0)
    assert not silent_period(db, 1)
    r = aggregate_weekly(db, 1, week_start=date(2026, 8, 31))
    assert r["silent"] is False
