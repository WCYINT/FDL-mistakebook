"""间隔驱动动态调度器单元测试（不连生产库，全部用临时 sqlite）。

参考 tests/test_self_purify.py 的临时库建法：sqlite3.connect + create_schema。
覆盖：独立计时 / 复苏起点 / 紧迫度公式 / 错因权重 / 启动加成 / cap 截断 /
未到期不进队列 / write_back_scores 回写 / revive 铺开 + dry_run。
"""

from __future__ import annotations

import sqlite3

from fdl_core.db.schema import create_schema
from fdl_core.srs.queue_scheduler import (
    build_daily_queue,
    compute_states,
    revive_and_schedule,
    write_back_scores,
)


def _init(conn: sqlite3.Connection) -> None:
    """建 schema 并插入一个 subject（review_schedule.subject_id NOT NULL）。"""
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000',"
        " 0.35, 4, 9, 1)"
    )
    conn.commit()


def _add_mistake(
    conn: sqlite3.Connection,
    mid: int,
    *,
    last_reappear_at: str | None = None,
    diagnosis_type: str = "CALC",
    source_ref: str = "",
    created_at: str = "2026-09-01T00:00:00Z",
) -> None:
    """插入一条 mistake_record（默认最小非空字段）。"""
    conn.execute(
        "INSERT INTO mistake_record "
        "(id, user_id, kp_id, occurred_at, subject, error_type, attributed_by,"
        " reappear_count, last_reappear_at, diagnosis_type, source_ref, note_id,"
        " created_at, updated_at) "
        "VALUES (?, 1, 0, '2026-09-01T20:00:00Z', 'MATH', 'METHOD', 'RULE_BASED', 0,"
        " ?, ?, ?, ?, ?, ?)",
        (mid, last_reappear_at, diagnosis_type, source_ref, f"n{mid}", created_at, created_at),
    )
    conn.commit()


def _add_schedule(
    conn: sqlite3.Connection,
    sid: int,
    *,
    mistake_id: int,
    due_date: str,
    planned_interval_days: int = 1,
    status: str = "PENDING",
    created_at: str = "2026-09-01T00:00:00Z",
) -> None:
    """插入一条 review_schedule（默认最小非空字段）。"""
    conn.execute(
        "INSERT INTO review_schedule "
        "(id, user_id, mistake_id, subject_id, due_date, due_session,"
        " planned_interval_days, priority_score, est_seconds, status, source,"
        " created_at, updated_at) "
        "VALUES (?, 1, ?, 1, ?, 'AM', ?, 90.0, 60, ?, 'TEST', ?, ?)",
        (sid, mistake_id, due_date, planned_interval_days, status, created_at, created_at),
    )
    conn.commit()


# ── 1. 独立计时：已复习的题从 last_reappear_at 起算 ──────────────
def test_independent_timing_reviewed(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    _add_mistake(conn, 1, last_reappear_at="2026-09-01")
    _add_schedule(
        conn,
        1,
        mistake_id=1,
        planned_interval_days=4,
        due_date="2026-09-05",
        created_at="2026-09-01T08:00:00Z",
    )
    items = compute_states(conn, today="2026-09-10")
    assert len(items) == 1
    it = items[0]
    assert it.start_at == "2026-09-01"  # 取 last_reappear_at
    assert it.is_startup is False
    assert it.interval_days == 4


# ── 2. 复苏起点：从未复习的题从 created_at 日期部分起算，is_startup=True ─
def test_startup_timing_from_created(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    _add_mistake(conn, 2, last_reappear_at=None, created_at="2026-08-20T10:00:00Z")
    _add_schedule(
        conn,
        2,
        mistake_id=2,
        planned_interval_days=1,
        due_date="2026-08-21",
        created_at="2026-08-20T10:00:00Z",
    )
    items = compute_states(conn, today="2026-09-10")
    assert len(items) == 1
    it = items[0]
    assert it.start_at == "2026-08-20"  # 取 created_at 日期部分
    assert it.is_startup is True


# ── 3. 紧迫度公式：同逾期 3 天，间隔 1 天(urgency 3.0) 远紧急于间隔 15 天(0.2) ─
def test_urgency_formula(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    # A：间隔 1 天、逾期 3 天
    _add_mistake(conn, 1, last_reappear_at="2026-09-06")
    _add_schedule(
        conn,
        1,
        mistake_id=1,
        planned_interval_days=1,
        due_date="2026-09-07",
        created_at="2026-09-06T00:00:00Z",
    )
    # B：间隔 15 天、逾期 3 天
    _add_mistake(conn, 2, last_reappear_at="2026-09-06")
    _add_schedule(
        conn,
        2,
        mistake_id=2,
        planned_interval_days=15,
        due_date="2026-09-07",
        created_at="2026-09-06T00:00:00Z",
    )
    items = compute_states(conn, today="2026-09-10")
    A = next(it for it in items if it.schedule_id == 1)
    B = next(it for it in items if it.schedule_id == 2)
    assert abs(A.urgency - 3.0) < 1e-9
    assert abs(B.urgency - 0.2) < 1e-9
    assert A.priority_score > B.priority_score
    assert items.index(A) < items.index(B)  # A 排在 B 前面


# ── 4. 错因权重：同 interval/overdue 下 CONCEPT 优先于 CARELESS ──
def test_diag_weight(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    today = "2026-09-10"
    due = "2026-09-07"  # 逾期 3 天
    _add_mistake(conn, 1, last_reappear_at="2026-09-06", diagnosis_type="CONCEPT")
    _add_schedule(
        conn,
        1,
        mistake_id=1,
        planned_interval_days=1,
        due_date=due,
        created_at="2026-09-06T00:00:00Z",
    )
    _add_mistake(conn, 2, last_reappear_at="2026-09-06", diagnosis_type="CARELESS")
    _add_schedule(
        conn,
        2,
        mistake_id=2,
        planned_interval_days=1,
        due_date=due,
        created_at="2026-09-06T00:00:00Z",
    )
    items = compute_states(conn, today=today)
    concept = next(it for it in items if it.schedule_id == 1)
    careless = next(it for it in items if it.schedule_id == 2)
    # CONCEPT 权重 0.3 > CARELESS 0.05
    assert concept.priority_score > careless.priority_score
    assert abs(concept.priority_score - careless.priority_score - 0.25) < 1e-9


# ── 5. 启动加成：同 interval/overdue 下 is_startup=True 的 priority 高 0.5 ──
def test_startup_bonus(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    today = "2026-09-10"
    due = "2026-09-07"  # 逾期 3 天
    # 非启动：已复习
    _add_mistake(conn, 1, last_reappear_at="2026-09-06", diagnosis_type="CALC")
    _add_schedule(
        conn,
        1,
        mistake_id=1,
        planned_interval_days=1,
        due_date=due,
        created_at="2026-09-06T00:00:00Z",
    )
    # 启动：从未复习
    _add_mistake(conn, 2, last_reappear_at=None, diagnosis_type="CALC")
    _add_schedule(
        conn,
        2,
        mistake_id=2,
        planned_interval_days=1,
        due_date=due,
        created_at="2026-09-06T00:00:00Z",
    )
    items = compute_states(conn, today=today)
    non_startup = next(it for it in items if it.schedule_id == 1)
    startup = next(it for it in items if it.schedule_id == 2)
    assert startup.is_startup is True
    assert non_startup.is_startup is False
    assert abs(startup.priority_score - non_startup.priority_score - 0.5) < 1e-9
    assert startup.priority_score > non_startup.priority_score


# ── 6. cap 截断：20 条到期计划，build_daily_queue(cap=8) 返回 8 条 ──
def test_cap_truncation(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    for i in range(1, 21):
        _add_mistake(conn, i, last_reappear_at="2026-09-06")
        _add_schedule(
            conn,
            i,
            mistake_id=i,
            planned_interval_days=1,
            due_date="2026-09-07",
            created_at="2026-09-06T00:00:00Z",
        )
    items = build_daily_queue(conn, today="2026-09-10", cap=8)
    assert len(items) == 8


# ── 7. 未到期不进队列：due_date 在未来的计划不出现 ───────────────
def test_future_not_in_queue(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    # 未来计划
    _add_mistake(conn, 1, last_reappear_at="2026-09-06")
    _add_schedule(
        conn,
        1,
        mistake_id=1,
        planned_interval_days=1,
        due_date="2026-09-20",
        created_at="2026-09-06T00:00:00Z",
    )
    # 到期计划
    _add_mistake(conn, 2, last_reappear_at="2026-09-06")
    _add_schedule(
        conn,
        2,
        mistake_id=2,
        planned_interval_days=1,
        due_date="2026-09-07",
        created_at="2026-09-06T00:00:00Z",
    )
    items = build_daily_queue(conn, today="2026-09-10")
    ids = {it.schedule_id for it in items}
    assert 1 not in ids  # 未来计划被排除
    assert 2 in ids  # 到期计划保留


# ── 8. write_back_scores：priority_score(浮点) 与 overdue_days(整数) 真实写回 ──
def test_write_back_scores(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    _add_mistake(conn, 1, last_reappear_at="2026-09-06", diagnosis_type="CALC")
    _add_schedule(
        conn,
        1,
        mistake_id=1,
        planned_interval_days=1,
        due_date="2026-09-07",
        created_at="2026-09-06T00:00:00Z",
    )
    items = compute_states(conn, today="2026-09-10")
    n = write_back_scores(conn, items)
    assert n == 1
    row = conn.execute(
        "SELECT priority_score, overdue_days FROM review_schedule WHERE id=1"
    ).fetchone()
    priority, overdue = row
    # 逾期 3 天、间隔 1 天、CALC 权重 0.1 → 3.0 + 0.1 = 3.1
    assert abs(priority - 3.1) < 1e-9
    assert isinstance(priority, float)
    assert overdue == 3
    assert isinstance(overdue, int)


# ── 9. revive 铺开 + dry_run：20 条、daily_cap=8 → days_span=3、spread 8/8/4 ──
def test_revive_spread_dry_run(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    today = "2026-09-10"
    for i in range(1, 21):
        _add_mistake(conn, i, last_reappear_at="2026-09-01")
        _add_schedule(
            conn,
            i,
            mistake_id=i,
            planned_interval_days=1,
            due_date="2026-09-07",
            created_at="2026-09-01T00:00:00Z",
        )

    before = dict(conn.execute("SELECT id, due_date FROM review_schedule ORDER BY id").fetchall())

    res = revive_and_schedule(conn, today=today, daily_cap=8, dry_run=True)
    assert res["total"] == 20
    assert res["cap"] == 8
    assert res["days_span"] == 3
    assert res["spread"] == {
        "2026-09-10": 8,
        "2026-09-11": 8,
        "2026-09-12": 4,
    }
    assert res["dry_run"] is True

    # dry_run 不写库：due_date 不变
    after = dict(conn.execute("SELECT id, due_date FROM review_schedule ORDER BY id").fetchall())
    assert before == after


# ── 10. revive 非 dry_run 真正铺开并回写（对照）─────────────────
def test_revive_spread_writes(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    today = "2026-09-10"
    for i in range(1, 21):
        _add_mistake(conn, i, last_reappear_at="2026-09-01")
        _add_schedule(
            conn,
            i,
            mistake_id=i,
            planned_interval_days=1,
            due_date="2026-09-07",
            created_at="2026-09-01T00:00:00Z",
        )
    res = revive_and_schedule(conn, today=today, daily_cap=8, dry_run=False)
    assert res["dry_run"] is False
    # 铺开后 due_date 已被改写（首批落在 today）
    due_dates = [
        d
        for (_id, d) in conn.execute(
            "SELECT id, due_date FROM review_schedule ORDER BY id"
        ).fetchall()
    ]
    assert due_dates.count("2026-09-10") == 8
    # 回写优先级字段已非硬编码 90.0
    ps = conn.execute("SELECT priority_score FROM review_schedule LIMIT 1").fetchone()[0]
    assert ps != 90.0


# ── 11. 日历感知铺开：25 条、today=周五、weekday_cap=10/weekend_cap=20 ──
# 周五(工作日)容量 10 → 先铺 10 条；剩余 15 条落到周六(周末)容量 20，
# 但只剩 15 条故周六实际铺 15。caps 体现各日"容量上限"：周五10/周六20。
def test_revive_calendar_aware_spread(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    today = "2026-09-11"  # 周五（isoweekday=5，工作日）
    for i in range(1, 26):  # 25 条待铺开
        _add_mistake(conn, i, last_reappear_at="2026-09-01")
        _add_schedule(
            conn,
            i,
            mistake_id=i,
            planned_interval_days=1,
            due_date="2026-09-07",
            created_at="2026-09-01T00:00:00Z",
        )

    res = revive_and_schedule(
        conn,
        today=today,
        weekday_cap=10,
        weekend_cap=20,
        dry_run=True,
    )
    assert res["total"] == 25
    assert res["cap"] is None  # 日历模式无固定每日容量
    assert res["days_span"] == 2
    # 按总数列出正确分布：周五 10 条 + 周六 15 条（周六容量 20 但仅余 15）
    assert res["spread"] == {
        "2026-09-11": 10,  # 周五（工作日）10
        "2026-09-12": 15,  # 周六（周末）容量 20，仅剩 15 条故铺 15
    }
    # caps 正确反映日历容量：周五=10（工作日），周六=20（周末上限）
    assert res["caps"] == {
        "2026-09-11": 10,
        "2026-09-12": 20,
    }
    assert res["dry_run"] is True

    # dry_run 不写库：due_date 不变
    after = dict(conn.execute("SELECT id, due_date FROM review_schedule ORDER BY id").fetchall())
    assert all(d == "2026-09-07" for d in after.values())


# ── 12. 显式 daily_cap 优先于日历规则（daily_cap=5 → 每天固定 5 条）──
def test_revive_daily_cap_overrides_calendar(tmp_sqlite):
    conn = tmp_sqlite
    _init(conn)
    today = "2026-09-11"  # 周五（日历模式本应是工作日 10）
    for i in range(1, 14):  # 13 条待铺开
        _add_mistake(conn, i, last_reappear_at="2026-09-01")
        _add_schedule(
            conn,
            i,
            mistake_id=i,
            planned_interval_days=1,
            due_date="2026-09-07",
            created_at="2026-09-01T00:00:00Z",
        )

    res = revive_and_schedule(
        conn,
        today=today,
        daily_cap=5,
        dry_run=True,
    )
    assert res["total"] == 13
    assert res["cap"] == 5  # 显式 daily_cap 回显
    assert res["days_span"] == 3  # 5+5+3
    assert res["spread"] == {
        "2026-09-11": 5,  # 周五
        "2026-09-12": 5,  # 周六
        "2026-09-13": 3,  # 周日（仅余 3）
    }
    # daily_cap 模式下每天容量统一为 5（覆盖日历规则）
    assert res["caps"] == {
        "2026-09-11": 5,
        "2026-09-12": 5,
        "2026-09-13": 5,
    }
