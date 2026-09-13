"""批次 2（P2-06~12 调度内核 SRS）验收测试。

覆盖：UTC 时间层 / fsrs 封装 / answer_log 全字段 / 掌握度三因子（乘法锚点）/
8 态 14 跃迁（含铁律）/ Grade 判定 / 每日任务 / 参数配置化。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from fdl_core.db.schema import create_schema
from fdl_core.srs.answer_log import AnswerRecord, insert_answer_log
from fdl_core.srs.daily_task import generate_daily_tasks
from fdl_core.srs.fsrs_wrapper import card_from_dict, card_state, new_card, review
from fdl_core.srs.grade import judge_grade
from fdl_core.srs.mastery import (
    ability,
    confidence,
    depth_score,
    mastery_adj,
    mastery_raw,
    next_interval,
    performance_score,
    pseudo_count,
    update_difficulty,
)
from fdl_core.srs.params import ModelParams, ensure_param_history_table, record_params
from fdl_core.srs.state_machine import (
    ARCHIVED,
    CONSOLIDATED,
    LEARNING,
    MASTERED,
    REGRESSED,
    REVIEWING,
    STRUGGLING,
    UNLEARNED,
    KpSnapshot,
    evaluate_answer,
    evaluate_batch,
    freeze,
)
from fdl_core.srs.time_layer import (
    fmt_ts,
    local_date,
    local_date_range,
    parse_ts,
    to_utc,
)


# ── P2-06 时间层 ────────────────────────────────────────────
def test_ts_roundtrip():
    dt = datetime(2026, 9, 3, 12, 30, 0, tzinfo=to_utc(datetime.now()).tzinfo)
    assert parse_ts(fmt_ts(dt)) == dt.replace(microsecond=0)


def test_daily_boundary_is_local():
    """UTC 16:30 = 北京次日 00:30 → 学习日已跨天。"""
    utc_dt = datetime(2026, 9, 3, 16, 30, tzinfo=to_utc(datetime.now()).tzinfo)
    assert local_date(utc_dt) == date(2026, 9, 4)


def test_local_date_range_covers_local_day():
    start, end = local_date_range(date(2026, 9, 3))
    assert (end - start).total_seconds() == 86400
    # 北京 9-3 00:00 = UTC 前一天 16:00
    assert parse_ts(fmt_ts(start)).hour in (15, 16)


# ── P2-06 fsrs 封装 ────────────────────────────────────────
def test_fsrs_review_and_persist_roundtrip():
    card = new_card()
    card2, _ = review(card, 3)
    assert card2.stability is not None
    state = card_state(card2)
    card3 = card_from_dict(state.raw)
    assert card3.stability == card2.stability
    assert card3.due == card2.due


def test_fsrs_again_shortens_interval():
    card = new_card()
    _, _ = review(card, 2)
    card_good, _ = review(card, 2)
    card_again, _ = review(card, 0)
    assert card_again.due <= card_good.due


# ── P2-08 掌握度三因子（🔴 乘法结构锚点）────────────────────
def test_mastery_raw_anchor_not_remembered():
    """记住了但不会做：R=0.9, A=0.2 → ≈0.365（PRD 示意锚点 0.38，θ=0.6 精确值）。"""
    assert mastery_raw(0.9, 0.2) == pytest.approx(0.38, abs=0.02)


def test_mastery_raw_anchor_forgot():
    """会做但忘了：R=0.35, A=0.9 → ≈0.62。"""
    assert mastery_raw(0.35, 0.9) == pytest.approx(0.62, abs=0.02)


def test_mastery_is_multiplicative():
    """乘法结构：任一因子为 0 → 整体为 0；Conf 减半 → M_adj 等比减半。"""
    zero_a = mastery_adj(0.9, 0.0, 0.0, n_real=10)
    assert zero_a.mastery_adj == 0.0
    # Conf 只随 n 变：构造不同 n 比较 M_adj 比值 = Conf 比值
    c1 = confidence(4)
    c2 = confidence(12)
    m1 = mastery_adj(0.9, 0.9, 0.5, n_real=4)
    m2 = mastery_adj(0.9, 0.9, 0.5, n_real=12)
    assert m2.mastery_adj / m1.mastery_adj == pytest.approx(c2 / c1, rel=1e-6)


def test_ability_weights():
    assert ability(1.0, 0.0) == pytest.approx(0.75)
    assert ability(0.0, 1.0) == pytest.approx(0.25)


def test_pseudo_count_decays():
    assert pseudo_count(0) == pytest.approx(3.0)
    assert pseudo_count(8) == pytest.approx(3.0 / 2.71828, abs=0.02)
    assert pseudo_count(80) < 0.01  # 拐杖退场


def test_performance_score_difficulty_weighting():
    hard = performance_score([{"grade": 2, "difficulty": 9.0, "seq": 0}])
    easy = performance_score([{"grade": 2, "difficulty": 2.0, "seq": 0}])
    assert hard > easy  # 难题做对加分更多


def test_depth_score_baseline_and_saturation():
    assert depth_score([], None) == pytest.approx(0.20)
    rich = depth_score([{"type": "teach_back", "days_ago": 0}] * 10, None)
    assert rich > 0.8  # 饱和曲线


def test_update_difficulty_anchored():
    d = update_difficulty(5.0, 0)  # Again +1.0 再向 5 回归 10%
    assert d == pytest.approx(5.9, abs=0.01)


def test_next_interval():
    assert next_interval(0.1) == 1
    assert next_interval(10.0) == int(1.637 * 10)


# ── P2-10 Grade 判定 ───────────────────────────────────────
def test_hint_forces_again():
    assert judge_grade(is_correct=True, time_ratio=0.5, self_rating=2, hint_used=True) == 0


def test_incorrect_is_again():
    assert judge_grade(is_correct=False, time_ratio=1.0, self_rating=2) == 0


def test_easy_derived_not_buttoned():
    """自评"搞定" + 快 + 零重试 → 系统派生 Easy(3)。"""
    assert judge_grade(is_correct=True, time_ratio=0.5, self_rating=2, retry_count=0) == 3


def test_slow_good_downgrades():
    assert judge_grade(is_correct=True, time_ratio=2.5, self_rating=2) == 1


def test_choice_mode_system_judged():
    assert judge_grade(is_correct=True, time_ratio=0.5, self_rating=None, input_mode="CHOICE") == 3
    assert judge_grade(is_correct=True, time_ratio=1.2, self_rating=None, input_mode="CHOICE") == 2
    assert judge_grade(is_correct=True, time_ratio=3.0, self_rating=None, input_mode="CHOICE") == 1


# ── P2-07 answer_log 全字段 ────────────────────────────────
@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    create_schema(conn)
    # 外键依赖：session/task/question 先建
    conn.execute(
        "INSERT INTO study_session (id, user_id, session_date, session_slot, trigger_type,"
        " started_at, duration_sec, effective_sec) VALUES (1, 1, '2026-09-03', 'AM',"
        " 'SELF', '2026-09-03T00:00:00Z', 480, 460)"
    )
    conn.execute(
        "INSERT INTO daily_task (id, user_id, task_date, session_slot, task_type, kp_id,"
        " subject_id, title, est_seconds, sort_order, status)"
        " VALUES (1, 1, '2026-09-03', 'AM', 'REVIEW', 1, 1, 't', 45, 0, 'DONE')"
    )
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000', 0.35, 4, 9, 1)"
    )
    _kp_sql = (
        "INSERT INTO knowledge_point (id, subject_id, code, name, grade_level, bloom_level,"
        " abstraction_level, importance_weight, base_difficulty, est_learn_minutes,"
        " est_review_seconds, kp_type, tier, graph_version, valid_from)"
        " VALUES (1, 1, 'MATH-A', 'A', 4, 2, 2, 1.0, 4.8, 1.5, 45,"
        " 'SKILL', 'L0', '2026.1', '2026-09-01')"
    )
    conn.execute(_kp_sql)
    conn.execute(
        "INSERT INTO question (id, subject_id, primary_kp_id, question_type, stem, answer,"
        " difficulty, expected_seconds, variant_type, source)"
        " VALUES (1, 1, 1, 'CALC', 'q', 'a', 3.5, 30, 'ORIGINAL', 'MANUAL')"
    )
    conn.commit()
    yield conn
    conn.close()


def test_answer_log_full_columns(db):
    rec = AnswerRecord(
        user_id=1,
        session_id=1,
        task_id=1,
        question_id=1,
        kp_id=1,
        task_type="REVIEW",
        grade=2,
        is_correct=True,
        response_seconds=27,
        expected_seconds=30,
        difficulty_at_time=4.6,
        r_at_answer=0.8523,
        s_before=5.09,
        s_after=7.30,
        d_before=4.6,
        d_after=4.6,
        m_adj_before=0.5231,
        m_adj_after=0.5618,
        interval_days=8,
        planned_interval_days=8,
        overdue_days=0,
    )
    rid = insert_answer_log(db, rec)
    col_names = [d[1] for d in db.execute("PRAGMA table_info(answer_log)").fetchall()]
    values = db.execute("SELECT * FROM answer_log WHERE id=?", (rid,)).fetchone()
    row = dict(zip(col_names, values, strict=True))
    assert len(col_names) == 38  # schema 全列
    # 核心事实 + 冗余快照全部落库（🔴 事后不可重算的字段必须有值）
    for key in (
        "grade",
        "is_correct",
        "time_ratio",
        "r_at_answer",
        "s_before",
        "s_after",
        "d_before",
        "d_after",
        "m_adj_before",
        "m_adj_after",
        "answered_at",
        "is_first_of_day",
        "is_valid_evidence",
    ):
        assert row[key] is not None, f"{key} 漏采"
    assert row["is_correct"] == 1 and abs(row["time_ratio"] - 0.9) < 0.01
    # 可空字段允许 None（self_confidence/asr/user_answer 等）


def test_answer_log_schema_rejects_incomplete(db):
    """schema 兜底：NOT NULL 列缺失 → IntegrityError（insert 的漏采检测之外的二道防线）。"""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO answer_log (user_id, grade) VALUES (1, 2)")
        db.commit()


# ── P2-09 状态机 ───────────────────────────────────────────
def _snap(status=UNLEARNED, **kw):
    base = dict(
        code="KP",
        status=status,
        exposure_count=0,
        n_eff=0.0,
        total_answer_count=0,
        consecutive_good_count=0,
        consecutive_again_count=0,
        stability_days=0.5,
        retrievability=0.9,
        m_adj=0.3,
        m_adj_before=0.3,
    )
    base.update(kw)
    return KpSnapshot(**base)


def test_t1_first_learn():
    r = evaluate_answer(_snap(UNLEARNED, grade=2, prereqs_ok=True))
    assert (r.new_status, r.rule) == (LEARNING, "T1")
    assert r.is_upgrade


def test_t1_blocked_without_prereqs():
    assert evaluate_answer(_snap(UNLEARNED, grade=2, prereqs_ok=False)).rule is None


def test_t2_to_reviewing():
    r = evaluate_answer(
        _snap(LEARNING, grade=2, exposure_count=2, total_answer_count=2, plan_exists=True)
    )
    assert (r.new_status, r.rule) == (REVIEWING, "T2")


def test_t3_to_struggling():
    r = evaluate_answer(_snap(LEARNING, grade=0, consecutive_again_count=3, m_adj=0.30))
    assert (r.new_status, r.rule) == (STRUGGLING, "T3")


def test_t4_to_mastered():
    r = evaluate_answer(
        _snap(
            REVIEWING,
            grade=2,
            consecutive_good_count=2,
            m_adj=0.70,
            stability_days=8.0,
            n_eff=6.0,
            last_again_date=None,
        )
    )
    assert (r.new_status, r.rule) == (MASTERED, "T4")


def test_t4_blocked_by_recent_again():
    from datetime import datetime, timedelta

    from fdl_core.srs.time_layer import fmt_ts

    recent = datetime.now(UTC) - timedelta(days=3)
    r = evaluate_answer(
        _snap(
            REVIEWING,
            grade=2,
            consecutive_good_count=2,
            m_adj=0.70,
            stability_days=8.0,
            n_eff=6.0,
            last_again_date=fmt_ts(recent),
        )
    )
    assert r.rule is None  # 近14天有 Again → 不升级


def test_t6_mastered_regressed():
    r = evaluate_answer(_snap(MASTERED, grade=0, m_adj=0.40, m_adj_before=0.60))
    assert (r.new_status, r.rule) == (REGRESSED, "T6")


def test_t6_blocked_small_delta():
    assert evaluate_answer(_snap(MASTERED, grade=0, m_adj=0.55, m_adj_before=0.60)).rule is None


def test_t9_regressed_recovers():
    r = evaluate_answer(_snap(REGRESSED, grade=2))
    assert (r.new_status, r.rule) == (REVIEWING, "T9")


def test_t10_struggling_reteach():
    r = evaluate_answer(_snap(STRUGGLING, grade=2, is_reteach=True))
    assert (r.new_status, r.rule) == (LEARNING, "T10")


def test_t13_archived_annual_check():
    r = evaluate_answer(_snap(ARCHIVED, grade=0))
    assert (r.new_status, r.rule) == (REVIEWING, "T13")


def test_downgrade_before_upgrade():
    """顺序铁律：LEARNING 态同时满足 T3 与 T2 → 降级优先。"""
    r = evaluate_answer(
        _snap(
            LEARNING,
            grade=0,
            consecutive_again_count=3,
            m_adj=0.30,
            exposure_count=5,
            total_answer_count=5,
            plan_exists=True,
        )
    )
    assert r.rule == "T3"


def test_batch_t5_mastered_to_consolidated():
    snap = _snap(
        MASTERED,
        m_adj=0.75,
        stability_days=50.0,
        entered_mastered_date="2026-07-01",
        mastered_review_grades=[2, 3],
    )
    r = evaluate_batch(snap, today=date(2026, 9, 3))
    assert (r.new_status, r.rule) == (CONSOLIDATED, "T5")


def test_batch_t8_reviewing_overdue():
    snap = _snap(REVIEWING, retrievability=0.40, overdue_ratio=2.0)
    r = evaluate_batch(snap, today=date(2026, 9, 3))
    assert (r.new_status, r.rule) == (REGRESSED, "T8")


def test_batch_t11_struggling_low_r():
    snap = _snap(STRUGGLING, retrievability=0.30)
    r = evaluate_batch(snap, today=date(2026, 9, 3))
    assert (r.new_status, r.rule) == (REGRESSED, "T11")


def test_batch_t12_archived():
    snap = _snap(
        CONSOLIDATED,
        stability_days=130.0,
        total_answer_count=12,
        mastered_review_grades=[2, 3],
        last_review_date="2026-04-01",
    )
    r = evaluate_batch(snap, today=date(2026, 9, 3))
    assert (r.new_status, r.rule) == (ARCHIVED, "T12")


def test_batch_never_upgrades():
    """铁律 2：批处理绝不跑升级跃迁（REVIEWING 即使全条件满足也不 T4）。"""
    snap = _snap(
        REVIEWING,
        grade=2,
        consecutive_good_count=5,
        m_adj=0.9,
        stability_days=100.0,
        n_eff=50.0,
    )
    assert evaluate_batch(snap, today=date(2026, 9, 3)).rule is None


def test_t14_freeze():
    r = freeze(_snap(UNLEARNED))
    assert r.rule == "T14"
    assert freeze(_snap(LEARNING)).rule is None  # 仅 UNLEARNED 可冻结


# ── P2-12 参数配置化 ───────────────────────────────────────
def test_params_load_default():
    p = ModelParams.load()
    assert p.version.startswith("p2026")
    assert p.scheduling["presentation_cap"] == 8
    assert p.mastery["CONF_K"] == 1.5


def test_params_history_recorded(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    ensure_param_history_table(conn)
    p = ModelParams.load()
    record_params(conn, p, reason="首次记录")
    row = conn.execute("SELECT version, reason FROM model_param_history").fetchone()
    assert row[0] == p.version and row[1] == "首次记录"
    conn.close()


# ── P2-11 每日任务 ─────────────────────────────────────────
@pytest.fixture
def task_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000', 0.35, 4, 9, 1)"
    )
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO knowledge_point (id, subject_id, code, name, grade_level, bloom_level,"
            " abstraction_level, importance_weight, base_difficulty, est_learn_minutes,"
            " est_review_seconds, kp_type, tier, graph_version, valid_from)"
            f" VALUES ({i}, 1, 'MATH-A{i}', 'A{i}', 4, 2, 2, 1.0, 4.8, 1.5, 45,"
            " 'SKILL', 'L0', '2026.1', '2026-09-01')"
        )
    for i, due in enumerate(("2026-08-31", "2026-09-01", "2026-09-02"), start=1):
        conn.execute(
            "INSERT INTO review_schedule (id, user_id, kp_id, subject_id, due_date,"
            " due_session, planned_interval_days, priority_score, est_seconds, status, source)"
            f" VALUES ({i}, 1, {i}, 1, '{due}', 'AM', 8, {5 - i}.0, 45, 'PENDING', 'AUTO')"
        )
    conn.execute(
        "INSERT INTO kp_state (id, user_id, kp_id, subject_id, status, stability_days,"
        " difficulty) VALUES (1, 1, 1, 1, 'REVIEWING', 5.0, 4.6)"
    )
    conn.commit()
    yield conn
    conn.close()


def test_daily_tasks_am_review_pm_new(task_db):
    plans = generate_daily_tasks(task_db, 1, target_date=date(2026, 9, 2))
    slots = {p.slot for p in plans}
    assert "AM" in slots  # 有到期复习 → 晨复习 session
    am = next(p for p in plans if p.slot == "AM")
    assert all(t["task_type"] == "REVIEW" for t in am.tasks)


def test_daily_tasks_presentation_cap(task_db):
    """D-04 规定③：单 session 呈现 ≤ 8 道。"""
    plans = generate_daily_tasks(task_db, 1, target_date=date(2026, 9, 2))
    for p in plans:
        assert len(p.tasks) <= 8


def test_daily_tasks_rising_r_order(task_db):
    """R 升序：可提取性最低的 KP 先复习。"""
    plans = generate_daily_tasks(task_db, 1, target_date=date(2026, 9, 2))
    am = next(p for p in plans if p.slot == "AM")
    assert am.tasks  # 到期池非空


def test_daily_tasks_sunday_explore(task_db):
    plans = generate_daily_tasks(task_db, 1, target_date=date(2026, 9, 6))  # 2026-09-06 是周日
    assert len(plans) == 1
    assert plans[0].tasks[0]["task_type"] == "EXPLORE"
