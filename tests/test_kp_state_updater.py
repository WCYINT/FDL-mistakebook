"""kp_state 生产者方案 B（批处理全量重建）验收测试。

覆盖：空库 / 重放降级（T3）/ 重放升级（T2）/ 幂等（同日重跑逐位一致 + 日志不增）/
无证据不建行 / 有错题无反馈保持 UNLEARNED / 评分映射 / SQLite 空格格式时间归日 /
T4→T6 降级可达 / 共享时钟（daily 与 rebuild 不互翻）/ 本地日期边界 13-14 与 119-120 /
S 走 reschedule.update_stability / entered_mastered_at 可纠正 / 返回值拆解 /
NULL-mistake 旧计划不失明。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from fdl_core.db.schema import create_schema
from fdl_core.metrics.daily import batch_anchor, run_daily_batch
from fdl_core.srs.kp_state_updater import (
    RATING_TO_GRADE,
    rebuild_kp_state,
)
from fdl_core.srs.params import ModelParams
from fdl_core.srs.reschedule import update_stability
from fdl_core.srs.state_machine import (
    CONSOLIDATED,
    MASTERED,
    REGRESSED,
    KpSnapshot,
    evaluate_answer,
    evaluate_batch,
)
from fdl_core.srs.time_layer import (
    local_date,
    local_date_of_lenient,
    parse_ts,
    parse_ts_lenient,
)

S_MIN = float(ModelParams.load().mastery["S_MIN"])


def _ts_space(days_ago: int, hours_ago: int = 0) -> str:
    """SQLite CURRENT_TIMESTAMP 风格的时间串（空格分隔，语义 UTC）。"""
    dt = datetime.now(UTC) - timedelta(days=days_ago, hours=hours_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _seed_base(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order)"
        " VALUES (1, 1, 'MATH', '数学', '#000', 0.35, 4, 9, 1)"
    )
    for kp_id, code in ((1, "MATH-A"), (2, "MATH-B"), (3, "MATH-EMPTY"), (4, "MATH-NOFB")):
        conn.execute(
            "INSERT INTO knowledge_point (id, subject_id, code, name, grade_level,"
            " bloom_level, abstraction_level, importance_weight, base_difficulty,"
            " est_learn_minutes, est_review_seconds, kp_type, tier, graph_version,"
            " valid_from) VALUES (?, 1, ?, ?, 4, 2, 2, 1.0, 4.8, 1.5, 45,"
            " 'SKILL', 'L0', '2026.1', '2026-09-01')",
            (kp_id, code, code),
        )
    conn.commit()


def _mistake(conn, mid: int, kp_id: int, occurred_at: str, fsrs_d: float) -> None:
    conn.execute(
        "INSERT INTO mistake_record (id, user_id, kp_id, occurred_at, subject, fsrs_d)"
        " VALUES (?, 1, ?, ?, 'MATH', ?)",
        (mid, kp_id, occurred_at, fsrs_d),
    )


def _schedule(
    conn,
    sid: int,
    mid: int | None,
    kp_id: int,
    due_date: str,
    interval: int,
    status: str = "PENDING",
) -> None:
    conn.execute(
        "INSERT INTO review_schedule (id, user_id, kp_id, mistake_id, subject_id,"
        " due_date, due_session, planned_interval_days, priority_score, est_seconds,"
        " status, source) VALUES (?, 1, ?, ?, 1, ?, 'AM', ?, 5.0, 45, ?, 'AUTO')",
        (sid, kp_id, mid, due_date, interval, status),
    )


def _feedback(conn, fid: int, schedule_id: int, kp_id: int, rating: int, created_at: str) -> None:
    conn.execute(
        "INSERT INTO review_feedback (id, user_id, schedule_id, kp_id, subject_id,"
        " self_rating, created_at) VALUES (?, 1, ?, ?, 1, ?, ?)",
        (fid, schedule_id, kp_id, rating, created_at),
    )


def _snapshot(conn: sqlite3.Connection) -> dict:
    """全列快照（含 id），用于幂等比对。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(kp_state)").fetchall()]
    return {
        int(row[0]): dict(zip(cols, row, strict=True))
        for row in conn.execute("SELECT * FROM kp_state ORDER BY kp_id").fetchall()
    }


# 全部用固定绝对日期（不依赖真实时钟）：today 一旦显式传入，
# batch_anchor 就是该日 04:00，必然 <= 现在，测试结果可复现。
_SERIES_START = date(2026, 6, 1)


def _seed_series(
    conn: sqlite3.Connection,
    *,
    kp_id: int = 1,
    mid: int = 101,
    sched_id: int = 201,
    n_good: int = 10,
    gap_days: int = 3,
    tail_ratings: tuple[int, ...] = (1,),
    fb_base: int = 300,
) -> tuple[date, date]:
    """构造"连续 n_good 次 rating=4（间隔 3 天）+ gap 天后若干 tail 事件"的时间线。

    实测校准（params p2026.09.01, fsrs_d=5.0, difficulty=5.0）：
    - 第 10 次 grade=3 时 S_in≈8.26≥7、n_eff≈10.9≥5、m_after≈0.7175≥0.65，连续 Good=10
      → T4 触发 → MASTERED；
    - 之后首个 grade=0 事件：gap=1 天 → ΔM_adj≈0.111 < 0.12（不触发 T6）；
      gap=14 天 → ΔM_adj≈0.147 > 0.12（触发 T6 → REGRESSED）。
    返回 (最后事件日期, 用于 rebuild 的 today)。
    """
    _mistake(conn, mid, kp_id, "2026-05-01", 5.0)
    _schedule(conn, sched_id, mid, kp_id, "2027-12-31", 3)
    fid = fb_base
    last_event = _SERIES_START
    for i in range(n_good):
        last_event = _SERIES_START + timedelta(days=3 * i)
        _feedback(conn, fid, sched_id, kp_id, 4, f"{last_event.isoformat()} 10:00:00")
        fid += 1
    day = _SERIES_START + timedelta(days=3 * (n_good - 1) + gap_days)
    for rating in tail_ratings:
        _feedback(conn, fid, sched_id, kp_id, rating, f"{day.isoformat()} 10:00:00")
        fid += 1
        last_event = day
        day += timedelta(days=1)
    conn.commit()
    # 返回 (最后事件日期, rebuild 用的 today)：today 必须晚于全部事件，
    # 否则锚点会落在最后一个事件之前，R(t) 走 Δt<=0 的 1.0 分支。
    return last_event, day


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "kp.db")
    create_schema(conn)
    _seed_base(conn)
    yield conn
    conn.close()


def test_empty_db_no_kp_no_error(tmp_path):
    conn = sqlite3.connect(tmp_path / "empty.db")
    create_schema(conn)
    first = rebuild_kp_state(conn)
    assert first == {
        "kps": 0,
        "created": 0,
        "updated": 0,
        "transitions": [],
        "replay_transitions": [],
    }
    assert rebuild_kp_state(conn) == first
    assert conn.execute("SELECT COUNT(*) FROM kp_state").fetchone()[0] == 0
    conn.close()


def test_three_again_becomes_struggling(db):
    """3 条 rating=1（grade 0）连续 Again → T3 降级到 STRUGGLING。"""
    _mistake(db, 101, 1, "2026-09-01", 5.0)
    due = (local_date() + timedelta(days=3)).isoformat()
    _schedule(db, 201, 101, 1, due, 3)
    for i, fid in enumerate((301, 302, 303)):
        _feedback(db, fid, 201, 1, 1, _ts_space(3 - i))
    db.commit()

    res = rebuild_kp_state(db, user_id=1)
    row = db.execute(
        "SELECT status, lapses, total_answer_count, exposure_count, retrievability"
        " FROM kp_state WHERE user_id=1 AND kp_id=1"
    ).fetchone()
    assert row is not None, "有挂载错题与反馈的 KP 必须建行"
    status, lapses, total, exposure, r = row
    assert status == "STRUGGLING"  # T3：连续 Again >= 3
    assert (lapses, total, exposure) == (3, 3, 3)
    assert 0.0 <= r <= 1.0
    assert res["created"] == 1
    # 新建行不写 kp_state_transition（见幂等策略）→ 完整步进只在 replay_transitions
    assert res["transitions"] == []
    # T11 未触发：最近一次反馈在近 1 天内，R(t) 远高于 0.35
    assert res["replay_transitions"][-1]["rule"] == "T3"


def test_two_good_becomes_reviewing(db):
    """2 条 rating=3（grade 2）+ 已有计划 → T2 升级到 REVIEWING。

    T4 不会误触发（注释锁定原因）：S 经 update_stability 两次增长后约 0.66 < 7
    （grow = 1 + K_GROW·(1−R)·GRADE_WEIGHT，间隔 1 天时 R≈0.82）
    且 n_eff = effective_answers(2) ≈ 4.34 < 5，条件不足。
    """
    _mistake(db, 102, 2, "2026-09-01", 4.0)
    due = (local_date() + timedelta(days=5)).isoformat()
    _schedule(db, 202, 102, 2, due, 5)
    _feedback(db, 304, 202, 2, 3, _ts_space(2))
    _feedback(db, 305, 202, 2, 3, _ts_space(1))
    db.commit()

    rebuild_kp_state(db, user_id=1)
    row = db.execute(
        "SELECT status, consecutive_good_count, total_answer_count"
        " FROM kp_state WHERE user_id=1 AND kp_id=2"
    ).fetchone()
    assert row is not None
    assert row[0] == "REVIEWING"
    assert row[1] == 2 and row[2] == 2


def test_rebuild_is_idempotent(db):
    """同日连跑两次：全列值逐位一致、行数不变、跃迁日志不增。"""
    # 预置过时状态，确保首次重建真的发生 1 次校正跃迁（日志可被观测）
    db.execute(
        "INSERT INTO kp_state (id, user_id, kp_id, subject_id, status,"
        " stability_days, difficulty) VALUES (9, 1, 2, 1, 'UNLEARNED', 0.5, 5.0)"
    )
    _mistake(db, 102, 2, "2026-09-01", 4.0)
    due = (local_date() + timedelta(days=5)).isoformat()
    _schedule(db, 202, 102, 2, due, 5)
    _feedback(db, 304, 202, 2, 3, _ts_space(2))
    _feedback(db, 305, 202, 2, 3, _ts_space(1))
    db.commit()

    rebuild_kp_state(db, user_id=1)
    snaps_first = _snapshot(db)
    logs_first = db.execute("SELECT COUNT(*) FROM kp_state_transition").fetchone()[0]

    rebuild_kp_state(db, user_id=1)
    snaps_second = _snapshot(db)
    logs_second = db.execute("SELECT COUNT(*) FROM kp_state_transition").fetchone()[0]

    assert snaps_first == snaps_second  # updated_at / R(t) 等全部逐位一致
    assert db.execute("SELECT COUNT(*) FROM kp_state").fetchone()[0] == 1
    assert logs_first == 1  # 只有 "旧状态 -> 重建状态" 那一条
    assert logs_second == logs_first  # 重跑不再写日志


def test_kp_without_evidence_gets_no_row(db):
    """无错题、无反馈的 KP 不建行（状态计数保持诚实）。"""
    res = rebuild_kp_state(db, user_id=1)
    assert res["kps"] == 0
    for kp_id in (1, 2, 3, 4):
        assert (
            db.execute("SELECT COUNT(*) FROM kp_state WHERE kp_id=?", (kp_id,)).fetchone()[0] == 0
        )


def test_mistake_without_feedback_stays_unlearned(db):
    """有错题但零反馈事件：建行、保持 UNLEARNED（无作答不升级）、exposure=0。"""
    _mistake(db, 103, 4, "2026-09-05", 6.0)
    db.commit()

    rebuild_kp_state(db, user_id=1)
    row = db.execute(
        "SELECT status, exposure_count, total_answer_count, mistake_count,"
        " stability_days, difficulty FROM kp_state WHERE user_id=1 AND kp_id=4"
    ).fetchone()
    assert row is not None
    status, exposure, total, mistake_count, stability, difficulty = row
    assert status == "UNLEARNED"
    assert (exposure, total) == (0, 0)
    assert mistake_count == 1
    assert stability == pytest.approx(S_MIN)  # 无计划 → S_MIN
    assert difficulty == pytest.approx(6.0)  # 关联错题 fsrs_d 均值


def test_rating_to_grade_mapping():
    assert RATING_TO_GRADE == {1: 0, 2: 1, 3: 2, 4: 3}
    # 1 陌生 -> Again(0) / 4 熟练 -> 3
    assert RATING_TO_GRADE[1] == 0 and RATING_TO_GRADE[4] == 3


def test_batch_anchor_never_in_future():
    """共享锚点 batch_anchor：绝不超过当前时刻（04:00 前运行必须回退到当日 00:00）。

    对抗性验证修复（2026-09-13）：固定 "当日 04:00" 在凌晨运行时落在未来，
    会让 R(t) 按未来时刻衰减（提前降级）并写出未来时间戳。
    方案 C（同日）：该函数已迁到 fdl_core.metrics.daily，供两个引擎共用。
    """
    today = local_date()
    now = datetime.now(UTC)
    anchor = batch_anchor(today, now=now)
    assert anchor <= now
    # 同日重跑取同一个值（幂等前提）
    assert batch_anchor(today, now=now) == anchor
    # 04:00 之后运行 → 仍是当日 04:00（本地 04:00 = UTC 前一日 20:00）
    after = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    assert batch_anchor(date(2026, 9, 13), now=after) == datetime(2026, 9, 12, 20, 0, tzinfo=UTC)
    # 04:00 之前运行 → 回退到当日 00:00（本地 00:00 = UTC 前一日 16:00）
    before = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)  # 本地 2026-09-13 01:00
    assert batch_anchor(date(2026, 9, 13), now=before) == datetime(2026, 9, 12, 16, 0, tzinfo=UTC)


def test_written_timestamps_not_in_future(db):
    """真实运行路径下写出的四个时间戳都不是未来时刻（凌晨跑批安全性）。"""
    _mistake(db, 105, 1, "2026-09-01", 5.0)
    _schedule(db, 205, 105, 1, "2026-09-30", 3)
    _feedback(db, 307, 205, 1, 3, _ts_space(2))
    db.commit()

    rebuild_kp_state(db, user_id=1)
    row = db.execute(
        "SELECT retrievability_calc_at, created_at, updated_at, status_changed_at,"
        " last_review_at FROM kp_state WHERE user_id=1 AND kp_id=1"
    ).fetchone()
    now = datetime.now(UTC)
    # last_review_at 是历史事件时间，必然在过去；其余四个是锚点时间，不得超前
    for col, val in zip(
        ("retrievability_calc_at", "created_at", "updated_at", "status_changed_at"),
        row[:4],
        strict=True,
    ):
        assert parse_ts(val) <= now, f"{col} 落在未来：{val}"
    assert parse_ts(row[4]) <= now


def test_space_format_timestamp_is_utc_and_local_day_correct(db):
    """SQLite 空格格式（UTC 语义）→ last_review_at 写 ISO，且归日按本地时区。"""
    _mistake(db, 104, 1, "2026-09-10", 5.0)
    _schedule(db, 204, 104, 1, "2026-09-20", 5)
    _feedback(db, 306, 204, 1, 3, "2026-09-12 20:30:00")
    db.commit()

    rebuild_kp_state(db, user_id=1)
    (last_review_at,) = db.execute(
        "SELECT last_review_at FROM kp_state WHERE user_id=1 AND kp_id=1"
    ).fetchone()
    # 不被误当本地时间（否则会偏移 8 小时）
    assert last_review_at == "2026-09-12T20:30:00Z"
    assert parse_ts(last_review_at) == parse_ts_lenient("2026-09-12 20:30:00")
    # 跨 UTC 边界归日：UTC 09-12 20:30 = 本地(Asia/Shanghai) 09-13 04:30
    assert local_date_of_lenient("2026-09-12 20:30:00").isoformat() == "2026-09-13"


# ── HIGH-1：事件后 R 语义 → T6 降级可达（此前 ΔM_adj 恒 0，降级永不触发）──


def test_t4_then_t6_regression_reachable(db):
    """10 次 grade=3 达成 T4（MASTERED），其后 14 天的 grade=0 触发 T6 → REGRESSED。

    ΔM_adj≈0.147 > 0.12：m_before 取上一事件的 m_after（≈0.7175），
    m_after 用事件后 R（grade=0 → 沿用衰减后的 r_before≈0.845）。
    """
    last, today = _seed_series(db, gap_days=14)
    res = rebuild_kp_state(db, user_id=1, today=today)
    rules = [t["rule"] for t in res["replay_transitions"]]
    assert rules == ["T1", "T2", "T4", "T6"], rules
    row = db.execute(
        "SELECT status, stability_days, lapses FROM kp_state WHERE user_id=1 AND kp_id=1"
    ).fetchone()
    assert row[0] == REGRESSED
    assert row[2] == 1  # 唯一一次 Again
    # MASTERED 进入时刻被记录（T5/T12 的时间起点）
    (entered,) = db.execute(
        "SELECT entered_mastered_at FROM kp_state WHERE user_id=1 AND kp_id=1"
    ).fetchone()
    assert entered is not None


def test_t6_blocked_when_delta_below_threshold(db):
    """同一序列但 grade=0 紧随其后（gap=1 天）→ ΔM_adj≈0.111 < 0.12 → 不降级。

    锁定阈值语义：T6 需要"遗忘"足够显著，短期内的单次失败不构成回退。
    """
    _, today = _seed_series(db, gap_days=1)
    res = rebuild_kp_state(db, user_id=1, today=today)
    rules = [t["rule"] for t in res["replay_transitions"]]
    assert rules == ["T1", "T2", "T4"], rules
    (status,) = db.execute("SELECT status FROM kp_state WHERE user_id=1 AND kp_id=1").fetchone()
    assert status == MASTERED


# ── HIGH-3 / MEDIUM-4：本地日期不再双重转换；today 可注入 ──


def _t4_snap(**kw) -> KpSnapshot:
    base = dict(
        code="K",
        status="REVIEWING",
        exposure_count=5,
        n_eff=6.0,
        total_answer_count=5,
        consecutive_good_count=2,
        consecutive_again_count=0,
        stability_days=8.0,
        retrievability=0.9,
        m_adj=0.70,
        m_adj_before=0.70,
        grade=2,
        plan_exists=True,
    )
    base.update(kw)
    return KpSnapshot(**base)


def test_t4_no_again_window_is_exactly_14_days():
    """T4 的 14 天窗：13 天不过 / 14 天可过（修复前 `parse_ts("YYYY-MM-DD")`
    把本地日期当本地时间再转 UTC，实际窗口只有 13 天）。"""
    today = date(2026, 9, 15)
    assert evaluate_answer(_t4_snap(last_again_date="2026-09-02"), today=today).rule is None
    assert evaluate_answer(_t4_snap(last_again_date="2026-09-01"), today=today).rule == "T4"


def test_t12_days_since_review_is_exactly_120_days():
    """T12 的 120 天窗：119 天不过 / 120 天可过。"""
    today = date(2026, 9, 15)

    def snap(last_review: str) -> KpSnapshot:
        return KpSnapshot(
            code="K",
            status=CONSOLIDATED,
            exposure_count=12,
            n_eff=12.0,
            total_answer_count=12,
            consecutive_good_count=2,
            consecutive_again_count=0,
            stability_days=130.0,
            retrievability=0.9,
            m_adj=0.8,
            m_adj_before=0.8,
            mastered_review_grades=[2, 3],
            last_review_date=last_review,
        )

    assert evaluate_batch(snap("2026-05-19"), today=today).rule is None  # 119 天
    assert evaluate_batch(snap("2026-05-18"), today=today).rule == "T12"  # 120 天


def test_t5_held_uses_full_timestamp_local_date():
    """T5 的 held 用完整时间戳（ISO UTC）→ 必须走宽容解析算本地日期。

    entered_mastered_at 是带时刻的 UTC 串，跨 UTC 边界时本地日期为次日。
    """
    # UTC 2026-07-01T20:00Z = 本地 2026-07-02；today=本地 2026-08-01 → held=30
    snap = KpSnapshot(
        code="K",
        status=MASTERED,
        exposure_count=10,
        n_eff=10.0,
        total_answer_count=10,
        consecutive_good_count=2,
        consecutive_again_count=0,
        stability_days=50.0,
        retrievability=0.9,
        m_adj=0.75,
        m_adj_before=0.75,
        entered_mastered_date="2026-07-01T20:00:00Z",
        mastered_review_grades=[2, 3],
    )
    assert evaluate_batch(snap, today=date(2026, 8, 1)).rule == "T5"
    # 本地 07-03 → 只保持 29 天 → 不过
    assert evaluate_batch(snap, today=date(2026, 7, 31)).rule is None


def test_evaluate_answer_today_param_defaults_to_real_today():
    """today 缺省仍是当日本地日期（不传时行为不变）。"""
    recent = datetime.now(UTC) - timedelta(days=3)
    snap = _t4_snap(last_again_date=fmt_ts_date(recent))
    assert evaluate_answer(snap).rule is None  # 近 3 天有 Again → 不升级


# ── MEDIUM-5：S 演化走 reschedule.update_stability ──


def test_stability_matches_update_stability_chain(db):
    """kp_state.stability_days 必须等于 update_stability 逐步演化的结果。

    与旧的 INTERVAL_FACTOR 连乘口径不同：答对的增长量由 K_GROW·(1−R)·GRADE_WEIGHT
    决定，故同一批事件在两套口径下会得到不同的 S。
    """
    _, today = _seed_series(
        db,
        n_good=4,
        gap_days=3,
        tail_ratings=(),
    )
    rebuild_kp_state(db, user_id=1, today=today)
    (stored_s,) = db.execute(
        "SELECT stability_days FROM kp_state WHERE user_id=1 AND kp_id=1"
    ).fetchone()

    params = ModelParams.load()
    s = S_MIN
    for i in range(4):
        days = 0.0 if i == 0 else 3.0
        r_before = 1.0 if days == 0.0 else _r(s, days)
        s = update_stability(s, 3, r_before, params)
    assert stored_s == pytest.approx(s, abs=1e-6)
    # 反向确认：不是 INTERVAL_FACTOR 连乘的结果（0.5 * 1.637^3 = 2.193）
    assert stored_s != pytest.approx(S_MIN * params.mastery["INTERVAL_FACTOR"] ** 3, abs=1e-3)


def _r(stability: float, days: float) -> float:
    from fdl_core.metrics.daily import retrievability

    return retrievability(stability, days)


def fmt_ts_date(dt: datetime) -> str:
    """UTC datetime → 本地日期串（快照日期字段的既有格式）。"""
    return local_date(dt).isoformat()


# ── MEDIUM-6：entered_mastered_at 可纠正 ──


def test_entered_mastered_at_can_be_corrected(db):
    """旧值错误（例如第一次误记）→ 重建算出更准的值应覆盖；算不出则保留旧值。"""
    db.execute(
        "INSERT INTO kp_state (id, user_id, kp_id, subject_id, status,"
        " stability_days, difficulty, entered_mastered_at)"
        " VALUES (9, 1, 1, 1, 'UNLEARNED', 0.5, 5.0, '2020-01-01T00:00:00Z')"
    )
    _, today = _seed_series(db, gap_days=3, tail_ratings=())
    rebuild_kp_state(db, user_id=1, today=today)
    (entered,) = db.execute(
        "SELECT entered_mastered_at FROM kp_state WHERE user_id=1 AND kp_id=1"
    ).fetchone()
    assert entered is not None and not entered.startswith("2020")  # 已被纠正

    # 另一 KP 零反馈 → 算不出新值 → 保留旧值（COALESCE(excluded, kp_state)）
    _mistake(db, 109, 3, "2026-09-01", 5.0)
    db.execute(
        "INSERT INTO kp_state (id, user_id, kp_id, subject_id, status,"
        " stability_days, difficulty, entered_mastered_at)"
        " VALUES (10, 1, 3, 1, 'UNLEARNED', 0.5, 5.0, '2021-02-02T00:00:00Z')"
    )
    db.commit()
    rebuild_kp_state(db, user_id=1, today=today)
    (kept,) = db.execute(
        "SELECT entered_mastered_at FROM kp_state WHERE user_id=1 AND kp_id=3"
    ).fetchone()
    assert kept == "2021-02-02T00:00:00Z"


# ── MEDIUM-7：返回值拆解（真日志 vs 重放步进）──


def test_transitions_split_logged_vs_replay(db):
    """新建行：transitions 为空（不写日志），replay_transitions 含完整步进。"""
    _mistake(db, 101, 1, "2026-09-01", 5.0)
    _schedule(db, 201, 101, 1, "2026-09-30", 3)
    for i, fid in enumerate((301, 302, 303)):
        _feedback(db, fid, 201, 1, 1, _ts_space(3 - i))
    db.commit()

    res = rebuild_kp_state(db, user_id=1)
    assert res["created"] == 1
    assert res["transitions"] == []
    assert [t["rule"] for t in res["replay_transitions"]] == ["T1", "T3"]
    assert db.execute("SELECT COUNT(*) FROM kp_state_transition").fetchone()[0] == 0


# ── MEDIUM-8：mistake_id 为 NULL 的旧计划不失明 ──


def test_schedule_without_mistake_still_attributed(db):
    """rs.mistake_id IS NULL 但 rs.kp_id 有值 → 仍参与 plan_exists / next_due。"""
    _schedule(db, 208, None, 1, "2026-09-20", 7)  # 无 mistake，仅 kp_id
    _feedback(db, 309, 208, 1, 3, "2026-09-12 10:00:00")
    db.commit()

    res = rebuild_kp_state(db, user_id=1, today=date(2026, 9, 13))
    assert res["kps"] == 1, "feedback 归因到 rf.kp_id 的 KP 应在重建范围内"
    row = db.execute(
        "SELECT status, next_due_at FROM kp_state WHERE user_id=1 AND kp_id=1"
    ).fetchone()
    # plan_exists=True（T2 需要）→ 单次 grade=2 只有 exposure=1，尚不升级
    assert row[1] == "2026-09-20", "旧计划的 PENDING due 必须挂上 next_due_at"


def test_feedback_attributed_via_schedule_kp_id(db):
    """三段归因链末位兜底：mistake 与 feedback 两侧都不可达时用 rs.kp_id。

    构造：schedule.mistake_id IS NULL、schedule.kp_id=2（KP2）、rf.kp_id=0。
    反馈事件应归入 KP2（而非被丢弃），KP2 有行且 exposure>=1。
    """
    _schedule(db, 209, None, 2, "2026-09-20", 7)  # mistake_id=None, kp_id=2
    _feedback(db, 310, 209, 0, 3, "2026-09-12 10:00:00")  # rf.kp_id=0（未挂载语义）
    db.commit()

    res = rebuild_kp_state(db, user_id=1, today=date(2026, 9, 13))
    assert res["kps"] == 1, "末位 rs.kp_id 应让该事件可归因，而不是被跳过"
    row = db.execute(
        "SELECT status, exposure_count, total_answer_count, next_due_at"
        " FROM kp_state WHERE user_id=1 AND kp_id=2"
    ).fetchone()
    assert row is not None, "KP2 必须有行"
    assert row[1] >= 1 and row[2] >= 1, "反馈事件必须计入 exposure / total"
    assert row[3] == "2026-09-20", "同一 schedule 也应挂上 next_due_at"


def test_feedback_skipped_when_all_three_unreachable(db):
    """反例：三段链全不可达（rs.kp_id 也是 0）→ 仍跳过，不臆造归因。"""
    _schedule(db, 210, None, 0, "2026-09-20", 7)  # kp_id=0（未挂载占位）
    _feedback(db, 311, 210, 0, 3, "2026-09-12 10:00:00")
    # 另一条完全悬空：schedule 不存在，rf.kp_id 为空
    db.execute(
        "INSERT INTO review_feedback (id, user_id, schedule_id, kp_id, subject_id,"
        " self_rating, created_at) VALUES (312, 1, NULL, NULL, 1, 3, '2026-09-12 11:00:00')"
    )
    db.commit()

    res = rebuild_kp_state(db, user_id=1, today=date(2026, 9, 13))
    assert res == {
        "kps": 0,
        "created": 0,
        "updated": 0,
        "transitions": [],
        "replay_transitions": [],
    }
    assert db.execute("SELECT COUNT(*) FROM kp_state").fetchone()[0] == 0


# ── HIGH-2：共享时钟（run_daily_batch 与 rebuild 不再互翻）──


def test_shared_clock_no_rewrite_between_engines(db):
    """同日先 rebuild → run_daily_batch → rebuild：R/时间戳逐位不变、无翻回日志。

    修复前 run_daily_batch 用 now_utc() 覆盖 retrievability/updated_at，
    rebuild 又用锚点写回，两个引擎在同一阈值附近互相翻转并各写一条日志。
    """
    _, today = _seed_series(db, n_good=3, gap_days=3, tail_ratings=())
    # 用未来 due + 低 S，确保无时间驱动跃迁（不干扰本用例）
    rebuild_kp_state(db, user_id=1, today=today)
    volatile = (
        "status, retrievability, retrievability_calc_at, updated_at, stability_days, mastery_adj"
    )
    after_rebuild_1 = db.execute(f"SELECT {volatile} FROM kp_state WHERE kp_id=1").fetchone()

    run_daily_batch(db, user_id=1, today=today)
    after_daily = db.execute(f"SELECT {volatile} FROM kp_state WHERE kp_id=1").fetchone()

    second = rebuild_kp_state(db, user_id=1, today=today)
    after_rebuild_2 = db.execute(f"SELECT {volatile} FROM kp_state WHERE kp_id=1").fetchone()

    assert after_daily == after_rebuild_1, "daily 与 rebuild 必须用同一锚点算 R/timestamps"
    assert after_rebuild_2 == after_rebuild_1
    assert second["transitions"] == []  # 无状态变更 → 不写日志
    assert db.execute("SELECT COUNT(*) FROM kp_state_transition").fetchone()[0] == 0


def test_seed_stability_prefers_pending_over_done(db):
    """零作答 KP 的 S 引导优先取 PENDING 计划；仅当无 PENDING 时回退全体。

    这里把 DONE 计划的 due 排得更晚、间隔更大：若未按 PENDING 过滤，
    S 会取到 DONE 那条（间隔 30 → S≈18.3）；正确实现应取 PENDING（间隔 3 → S≈1.83）。
    """
    _mistake(db, 120, 3, "2026-09-01", 5.0)
    _schedule(db, 220, 120, 3, "2026-09-20", 3, status="PENDING")
    _schedule(db, 221, 120, 3, "2026-12-31", 30, status="DONE")
    db.commit()

    rebuild_kp_state(db, user_id=1, today=date(2026, 9, 13))
    (stability,) = db.execute(
        "SELECT stability_days FROM kp_state WHERE user_id=1 AND kp_id=3"
    ).fetchone()
    factor = float(ModelParams.load().mastery["INTERVAL_FACTOR"])
    assert stability == pytest.approx(3 / factor, abs=1e-9), "应取 PENDING 的间隔 3"
    assert stability != pytest.approx(30 / factor, abs=1e-3), "不应取 DONE 的间隔 30"

    # 无 PENDING（全部 DONE）→ 回退全体，用最近一条历史间隔作引导
    db.execute("UPDATE review_schedule SET status='DONE' WHERE id=220")
    db.commit()
    rebuild_kp_state(db, user_id=1, today=date(2026, 9, 13))
    (fallback,) = db.execute(
        "SELECT stability_days FROM kp_state WHERE user_id=1 AND kp_id=3"
    ).fetchone()
    assert fallback == pytest.approx(30 / factor, abs=1e-9), "无 PENDING 时回退全体"


# ── T5（CONSOLIDATED）可达性 + T7 标注的已知偏差 ──


def _seed_expanding(
    conn: sqlite3.Connection,
    *,
    n_good: int,
    tail_gap: int | None = None,
) -> date:
    """按真实扩张间隔（≈S×1.637）连续 n_good 次 rating=4，返回 today。

    实测（2026-09-13）：n=13 → MASTERED（m_adj≈0.703 差一点，T5 需 ≥0.72）；
    n=16 → CONSOLIDATED（m_adj≈0.725，S=144≥45，保持期>30 天，≥2 次 MASTERED 期复习）。
    """
    params = ModelParams.load()
    _mistake(conn, 111, 1, "2026-01-01", 5.0)
    _schedule(conn, 211, 111, 1, "2035-12-31", 3)
    s, gap = S_MIN, 0.0
    day, fid = date(2026, 1, 2), 400
    for i in range(n_good):
        _feedback(conn, fid, 211, 1, 4, f"{day.isoformat()} 10:00:00")
        fid += 1
        r_before = 1.0 if i == 0 else _r(s, gap)
        s = update_stability(s, 3, r_before, params)
        gap = round(s * params.mastery["INTERVAL_FACTOR"])
        day += timedelta(days=gap)
    if tail_gap is not None:
        day += timedelta(days=tail_gap)
        _feedback(conn, fid, 211, 1, 1, f"{day.isoformat()} 10:00:00")
    conn.commit()
    return day + timedelta(days=1)


def test_consolidated_reachable_via_t5(db):
    """T5 可达：16 次扩张间隔复习 → 末次批处理评估触发 T5 → CONSOLIDATED。"""
    today = _seed_expanding(db, n_good=16)
    res = rebuild_kp_state(db, user_id=1, today=today)
    assert [t["rule"] for t in res["replay_transitions"]] == ["T1", "T2", "T4", "T5"]
    row = db.execute(
        "SELECT status, stability_days, mastery_adj FROM kp_state WHERE kp_id=1"
    ).fetchone()
    assert row[0] == CONSOLIDATED
    assert row[1] >= 45.0 and row[2] >= 0.72


def test_t7_label_deviation_documented(db):
    """已知偏差（LOW，标注层）：已达 CONSOLIDATED 的 KP 再答错，重放会走 T6 而非 T7。

    原因：T5 是时间驱动跃迁，只在重放**末尾**那一次批处理评估里生效；重放中途
    状态最高到 MASTERED，随后的 grade=0 因此命中 T6。目标状态与 is_upgrade 都正确
    （都到 REGRESSED），仅日志里的 rule 标注不准。彻底修需在时间线上逐事件插入
    批处理评估（超出本轮核准范围，已上报）。
    """
    today = _seed_expanding(db, n_good=20, tail_gap=14)
    res = rebuild_kp_state(db, user_id=1, today=today)
    rules = [t["rule"] for t in res["replay_transitions"]]
    assert rules == ["T1", "T2", "T4", "T6"], rules  # 注意：不是 T7
    (status,) = db.execute("SELECT status FROM kp_state WHERE user_id=1 AND kp_id=1").fetchone()
    assert status == REGRESSED  # 目标状态正确
