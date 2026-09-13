"""
测试 display_filter 显示控制逻辑（VIS-13 扩展）。
覆盖 5 个场景：【已解决】隐藏 / 【今日已复习】保留 / 【待复习】显示 /
                 【已复习】隐藏（无 PENDING）/ 【已复习】保留（逾期/到期）。
"""

import sqlite3
from datetime import date, timedelta

from fdl_core.srs.display_filter import (
    filter_review_today_items,
    should_show_in_review_today,
)


def make_conn():
    """临时 SQLite 内存库 + review_schedule 表。"""
    c = sqlite3.connect(":memory:")
    c.executescript("""
    CREATE TABLE review_schedule (
        id INTEGER PRIMARY KEY,
        kp_id INTEGER NOT NULL,
        mistake_id INTEGER,          -- A3 调度单元（2026-09-08）
        subject_id INTEGER NOT NULL DEFAULT 1,
        due_date TEXT NOT NULL,
        due_session TEXT NOT NULL DEFAULT 'PM',
        planned_interval_days REAL NOT NULL,
        status TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'answer',
        created_at TEXT NOT NULL DEFAULT '2026-09-08T00:00:00Z',
        updated_at TEXT NOT NULL DEFAULT '2026-09-08T00:00:00Z'
    );
    CREATE TABLE mistake_record (
        id INTEGER PRIMARY KEY,
        resolved_at TEXT,
        last_reappear_at TEXT
    );
    """)
    return c


def add_schedule(c, kp_id, due_date, status="PENDING"):
    c.execute(
        "INSERT INTO review_schedule (kp_id, subject_id, due_date, planned_interval_days, status)"
        " VALUES (?, 1, ?, 1.0, ?)",
        (kp_id, due_date, status),
    )


# === 场景 1：【已解决】永远隐藏 ===
def test_resolved_always_hidden():
    c = make_conn()
    add_schedule(c, kp_id=101, due_date=date.today().isoformat())
    d = should_show_in_review_today(
        c,
        kp_id=101,
        today=date.today().isoformat(),
        resolved_at="2026-09-05T10:00:00Z",
        status="已解决",
    )
    assert d.should_show is False
    assert d.reason == "resolved"
    print("  ✅ 【已解决】永远隐藏")


# === 场景 2：【今日已复习】+ due_date==today → 保留显示（King 规则"复习未完成时保留"）===
def test_reviewed_today_due_today_shown():
    c = make_conn()
    today = date.today()
    add_schedule(c, kp_id=201, due_date=today.isoformat())
    d = should_show_in_review_today(
        c,
        kp_id=201,
        today=today.isoformat(),
        last_reappear_at=f"{today.isoformat()}T08:30:00Z",
        status="今日已复习",
    )
    assert d.should_show is True
    assert "reviewed_today_due_today" in d.reason
    assert d.is_in_curve_today is True
    print("  ✅ 【今日已复习】+ due_date==today → 保留显示")


# === 场景 3：【待复习】+ due_date==today → 显示 ===
def test_pending_due_today_shown():
    c = make_conn()
    today = date.today()
    add_schedule(c, kp_id=301, due_date=today.isoformat())
    d = should_show_in_review_today(c, kp_id=301, today=today.isoformat(), status="待复习")
    assert d.should_show is True
    assert "pending_due_today" in d.reason
    assert d.is_in_curve_today is True
    print("  ✅ 【待复习】+ due_date==today → 显示")


# === 场景 4：【待复习】+ 逾期（due_date<today）→ 必须显示（避免漏掉）===
def test_pending_overdue_shown():
    c = make_conn()
    today = date.today()
    overdue = (today - timedelta(days=2)).isoformat()
    add_schedule(c, kp_id=401, due_date=overdue)
    d = should_show_in_review_today(c, kp_id=401, today=today.isoformat(), status="待复习")
    assert d.should_show is True
    assert d.is_overdue is True
    print("  ✅ 【待复习】逾期 2 天 → 必须显示")


# === 场景 5：【已复习】+ due_date>today（未来节点）→ 隐藏（避免打扰）===
def test_reviewed_not_due_hidden():
    c = make_conn()
    today = date.today()
    future = (today + timedelta(days=7)).isoformat()
    add_schedule(c, kp_id=501, due_date=future)
    d = should_show_in_review_today(
        c,
        kp_id=501,
        today=today.isoformat(),
        last_reappear_at="2026-08-30T10:00:00Z",
        status="已复习",
    )
    assert d.should_show is False
    assert "not_due" in d.reason
    print("  ✅ 【已复习】+ due_date>today → 隐藏（不在记忆曲线节点）")


# === 场景 6：【已复习】+ 无 PENDING 计划 → 隐藏 ===
def test_reviewed_no_schedule_hidden():
    c = make_conn()
    d = should_show_in_review_today(
        c,
        kp_id=601,
        today=date.today().isoformat(),
        last_reappear_at="2026-09-01T10:00:00Z",
        status="已复习",
    )
    assert d.should_show is False
    assert "no_pending_already_reviewed" in d.reason
    print("  ✅ 【已复习】无 PENDING → 隐藏")


# === 场景 7：批量过滤 ===
def test_batch_filter():
    c = make_conn()
    today = date.today()
    future = (today + timedelta(days=5)).isoformat()
    items = [
        # kp=701 已解决 → 隐藏
        {"id": 701, "kp_id": 701, "status": "已解决", "resolved_at": "2026-09-05T10:00:00Z"},
        # kp=702 待复习 today → 显示
        {"id": 702, "kp_id": 702, "status": "待复习", "last_reappear_at": None},
        # kp=703 已复习 future → 隐藏
        {"id": 703, "kp_id": 703, "status": "已复习", "last_reappear_at": "2026-08-30T10:00:00Z"},
    ]
    add_schedule(c, kp_id=702, due_date=today.isoformat())
    add_schedule(c, kp_id=703, due_date=future)
    kept = filter_review_today_items(c, review_items=items, today=today.isoformat())
    assert len(kept) == 1
    assert kept[0]["id"] == 702
    print(f"  ✅ 批量过滤：{len(items)} → {len(kept)}（预期 1）")


if __name__ == "__main__":
    print("=== display_filter 6+1 场景测试 ===")
    test_resolved_always_hidden()
    test_reviewed_today_due_today_shown()
    test_pending_due_today_shown()
    test_pending_overdue_shown()
    test_reviewed_not_due_hidden()
    test_reviewed_no_schedule_hidden()
    test_batch_filter()
    print("\n✅ 全部通过")
