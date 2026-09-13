"""复习调度记忆曲线符合性测试（四场景：正常/逾期/连续答对/连续答错）。

复习节奏模拟：链式复习（每次 answered_at = 上次计划 due_date），
与真实使用节奏一致（同日重复作答不增长 S 是防刷量语义，属正确行为）。
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from fdl_core.srs.params import ModelParams
from fdl_core.srs.reschedule import (
    next_interval_days,
    post_answer_reschedule,
    update_stability,
)

D0 = "2026-09-01"


def _add_days(d: str, n: int) -> str:
    return (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    from fdl_core.db.schema import create_schema

    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000', 0.35, 4, 9, 1)"
    )
    for i in range(3):
        conn.execute(
            "INSERT INTO knowledge_point (id, subject_id, code, name, grade_level,"
            " bloom_level, abstraction_level, importance_weight, base_difficulty,"
            " est_learn_minutes, est_review_seconds, kp_type, tier, graph_version, valid_from)"
            f" VALUES ({i + 1}, 1, 'MATH-G4-A{i}', 'KP{i}', 4, 2, 2, 1.0, 4.8, 1.5, 45,"
            " 'SKILL', 'L0', '2026.1', '2026-09-01')"
        )
    conn.commit()
    yield conn
    conn.close()


def _chain(conn, kp: int, seq: list[tuple[str, int]]):
    """链式复习：seq = [(answered_at, grade), ...]，输出每次 RescheduleResult。"""
    out = []
    for answered, grade in seq:
        out.append(
            post_answer_reschedule(
                conn,
                kp_id=kp,
                grade=grade,
                answered_at=answered,
                fuzz=False,
            )
        )
    return out


# ── 1. S 更新规则单元 ──────────────────────────────────────
def test_s_update_good_grows_bad_decays():
    p = ModelParams.load()
    m = p.mastery
    s_low_r = update_stability(1.2, 2, 0.35, p)  # R 低（逾期后成功）→ 增长多
    s_high_r = update_stability(1.2, 2, 0.85, p)  # R 高（按时）→ 增长少
    assert s_low_r > s_high_r > 1.2
    s_again = update_stability(2.0, 0, 0.2, p)
    assert s_again == pytest.approx(2.0 * m["K_DECAY"], abs=1e-6)  # ×0.35 重置
    assert s_again >= m["S_MIN"]


def test_interval_ebbinghaus_rhythm():
    """间隔 = S×INTERVAL_FACTOR = R 衰减到 0.85 的解析解（艾宾浩斯节律核验）。"""
    p = ModelParams.load()
    m = p.mastery
    assert next_interval_days(0.5, 2, p) >= 2.0  # BOOTSTRAP_I 下限
    s = 2.0
    i = next_interval_days(s, 2, p)
    assert i == pytest.approx(s * m["INTERVAL_FACTOR"], abs=0.01)
    r_at_i = (1 + m["F"] * i / s) ** m["C"]
    assert r_at_i == pytest.approx(m["R_TARGET"], abs=0.01)  # 记忆曲线核验


# ── 2. 四场景端到端 ────────────────────────────────────────
def test_scenario_normal_review(db):
    """正常复习：首答 → 排程 PENDING + due 正确。"""
    r = post_answer_reschedule(db, kp_id=1, grade=2, answered_at=D0 + "T09:00:00Z", fuzz=False)
    assert r.s_new > 0 and r.interval_days >= 2.0
    assert r.due_date > D0
    row = db.execute(
        "SELECT status, due_date FROM review_schedule WHERE kp_id=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row == ("PENDING", r.due_date)


def test_scenario_overdue_review_grows_more(db):
    """逾期复习：距上次复习 10 天（R≈0.55 低）答对 → S 增长多于按时（3 天）。"""
    # KP1 按时（3 天后复习）
    post_answer_reschedule(db, kp_id=1, grade=2, answered_at=D0 + "T09:00:00Z", fuzz=False)
    r1b = post_answer_reschedule(
        db, kp_id=1, grade=2, answered_at=_add_days(D0, 3) + "T09:00:00Z", fuzz=False
    )
    # KP2 逾期（10 天后复习）
    post_answer_reschedule(db, kp_id=2, grade=2, answered_at=D0 + "T09:00:00Z", fuzz=False)
    r2b = post_answer_reschedule(
        db, kp_id=2, grade=2, answered_at=_add_days(D0, 10) + "T09:00:00Z", fuzz=False
    )
    assert r2b.s_new > r1b.s_new  # 逾期成功回忆 → 记忆强化更多（等价 S 更长）


def test_scenario_consecutive_correct_increases(db):
    """连续答对（链式，按计划到期节奏）：S 与间隔逐次递增（艾宾浩斯节奏）。"""
    answered = D0 + "T09:00:00Z"
    results = []
    for _ in range(4):
        r = post_answer_reschedule(db, kp_id=1, grade=2, answered_at=answered, fuzz=False)
        results.append(r)
        answered = r.due_date + "T09:00:00Z"
    ss = [r.s_new for r in results]
    intervals = [r.interval_days for r in results]
    assert ss == sorted(ss) and len(set(ss)) == 4  # S 严格单调递增
    assert intervals == sorted(intervals)  # 间隔递增
    assert intervals[0] >= 2.0  # BOOTSTRAP_I 下限


def test_scenario_consecutive_again_resets(db):
    """连续答错（链式）：S 衰减 ×K_DECAY 至下限附近、间隔重置回最低档。"""
    p = ModelParams.load()
    answered = D0 + "T09:00:00Z"
    last = None
    for _ in range(3):
        r = post_answer_reschedule(db, kp_id=1, grade=0, answered_at=answered, fuzz=False)
        last = r
        answered = _add_days(answered[:10], 1) + "T09:00:00Z"
    assert last.s_new == pytest.approx(
        max(1.2 * p.mastery["K_DECAY"] ** 2, p.mastery["S_MIN"]), abs=0.01
    )
    assert last.interval_days <= 1.5  # Again 后间隔回最低档（次日重学）


def test_scenario_replaces_old_pending(db):
    """旧 PENDING 计划被新计划取代（同 KP 只有一个活跃计划）。"""
    post_answer_reschedule(db, kp_id=1, grade=2, answered_at=D0 + "T09:00:00Z", fuzz=False)
    post_answer_reschedule(
        db, kp_id=1, grade=2, answered_at=_add_days(D0, 3) + "T09:00:00Z", fuzz=False
    )
    n = db.execute(
        "SELECT COUNT(*) FROM review_schedule WHERE kp_id=1 AND status='PENDING'"
    ).fetchone()[0]
    assert n == 1
