"""调度器状态持久化单测（2026-09-12 排障结论的回归防护）。

背景：run_a2_upgrade_check / switch_to_a2 原实现只改**进程内**全局
CURRENT_SCHEDULER，而真正执行 mark_reviewed 的是 fdl_serve 进程——
daily_batch 里"升级成功"随进程退出即消失，跨进程是假动作。

修复：切换写 report_meta(key=scheduler_current)；复习分发经
_effective_scheduler() 读持久值。本文件锁死这个行为。
"""

from __future__ import annotations

import pytest

from fdl_core.db.schema import create_schema, get_connection
from fdl_core.mistakes import review as rv


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "a2p.db"
    conn = get_connection(str(p))
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000',"
        " 0.35, 4, 9, 1)"
    )
    yield conn
    conn.close()
    rv.CURRENT_SCHEDULER = "A3"  # 全局状态复位，防跨用例污染


def _reset_a3():
    rv.CURRENT_SCHEDULER = "A3"


def test_switch_without_conn_does_not_persist(db):
    """旧行为保留：switch_to_a2() 不带 conn 时只改内存（不写库）。"""
    _reset_a3()
    rv.switch_to_a2()
    assert rv.CURRENT_SCHEDULER == "A2"
    row = db.execute("SELECT value FROM report_meta WHERE key='scheduler_current'").fetchone()
    assert row is None, "不带 conn 的 switch 不应有持久化副作用"


def test_persist_and_effective_roundtrip(db):
    """持久化 → 新"进程"（全局归 A3）读库拉齐。"""
    _reset_a3()
    rv._persist_scheduler(db, "A2")
    row = db.execute(
        "SELECT value, status FROM report_meta WHERE key='scheduler_current'"
    ).fetchone()
    assert row == ("A2", "ACTIVE")
    # 模拟另一个进程：内存还是 A3，应从库拉齐
    _reset_a3()
    assert rv._effective_scheduler(db) == "A2"
    assert rv.CURRENT_SCHEDULER == "A2"


def test_effective_scheduler_on_a3_db_stays_a3(db):
    """库中无记录 → 保持 A3，且不写任何行。"""
    _reset_a3()
    assert rv._effective_scheduler(db) == "A3"
    assert (
        db.execute("SELECT COUNT(*) FROM report_meta WHERE key='scheduler_current'").fetchone()[0]
        == 0
    )


def test_rollback_clears_persisted_state(db):
    """rollback 必须同时清持久化值，否则下个进程会被重新拉回 A2（假回滚）。"""
    _reset_a3()
    rv._persist_scheduler(db, "A2")
    rv.rollback_to_a3(db)
    assert rv.CURRENT_SCHEDULER == "A3"
    row = db.execute("SELECT value FROM report_meta WHERE key='scheduler_current'").fetchone()
    assert row == ("A3",), f"持久化值应被改写为 A3，实际 {row}"


def test_run_a2_upgrade_check_persists_when_ready(db, monkeypatch):
    """ready=True → switch + 持久化；且另一个"A3 内存"的进程能读到。"""
    _reset_a3()

    class FakeCheck:
        ready = True

        def to_dict(self):
            return {"reasons": ["forced"]}

    import fdl_core.srs.a2_upgrade as a2u

    monkeypatch.setattr(a2u, "should_upgrade_to_a2", lambda conn: FakeCheck())
    res = rv.run_a2_upgrade_check(db)
    assert res["switched"] is True and res["scheduler"] == "A2"
    # 跨进程：新"进程"内存 A3 → 经持久化拉齐
    _reset_a3()
    assert rv._effective_scheduler(db) == "A2"


def test_run_a2_upgrade_check_not_ready_no_persist(db, monkeypatch):
    """ready=False → 不切换不落库（不污染 report_meta）。"""
    _reset_a3()

    class FakeCheck:
        ready = False

        def to_dict(self):
            return {"reasons": ["not yet"]}

    import fdl_core.srs.a2_upgrade as a2u

    monkeypatch.setattr(a2u, "should_upgrade_to_a2", lambda conn: FakeCheck())
    res = rv.run_a2_upgrade_check(db)
    assert res["switched"] is False and res["scheduler"] == "A3"
    assert (
        db.execute("SELECT COUNT(*) FROM report_meta WHERE key='scheduler_current'").fetchone()[0]
        == 0
    )


def test_mark_reviewed_dispatch_reads_persisted(db, monkeypatch):
    """复习分发走 _effective_scheduler：另一进程升级后，本进程复习应走 A2 分支。"""
    _reset_a3()
    rv._persist_scheduler(db, "A2")  # 模拟 daily_batch 进程已升级

    called = {"a2": 0, "a3": 0}
    monkeypatch.setattr(rv, "_a2_compute_interval", lambda *a, **k: 5)
    monkeypatch.setattr(rv, "a3_next_interval", lambda *a, **k: 3)
    real_mark = rv.mark_reviewed

    # 观察分发：包装两个算法函数计数
    def a2spy(*a, **k):
        called["a2"] += 1
        return 5

    def a3spy(*a, **k):
        called["a3"] += 1
        return 3

    monkeypatch.setattr(rv, "_a2_compute_interval", a2spy)
    monkeypatch.setattr(rv, "a3_next_interval", a3spy)

    # 造最小错题（无 PENDING 计划也可走：sc_row 为 None 的分支）
    db.execute(
        "INSERT INTO mistake_record (id, user_id, kp_id, occurred_at, subject,"
        " source, error_type, attributed_by, severity, is_tamed, reappear_count,"
        " note_id) VALUES (77001, 1, 0, '2026-09-03T20:00:00Z', 'MATH',"
        " 'REAL_WORK', 'METHOD', 'RULE_BASED', 3, 0, 0, 'n77001')"
    )
    db.commit()
    real_mark(db, [77001], grade=3)
    assert called["a2"] == 1 and called["a3"] == 0, "持久化 A2 后复习必须走 A2 分支"
