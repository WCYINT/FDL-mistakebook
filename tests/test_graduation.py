"""P2-1 毕业归档单元测试（不连生产库，全部用临时 sqlite）。

参考 tests/test_self_purify.py 的临时库建法：
- sqlite3.connect(tmp_path/...) + fdl_core.db.schema.create_schema
- 每题场景独立建库，逐条核对 check_graduation 的返回值与库内副作用
"""

from __future__ import annotations

import sqlite3

# 让 `import fdl_core...` 可用（conftest 已把仓库根加入 sys.path，这里兜底）
import sys
from pathlib import Path

import pytest

from fdl_core.srs.graduation import check_graduation

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 测试统一用固定"今天"，保证 due_date 比较确定性
TODAY = "2026-09-10"


@pytest.fixture
def db_path(tmp_path):
    """建一个空 FDL 临时库，返回其路径。"""
    p = tmp_path / "fdl.db"
    conn = sqlite3.connect(p)
    from fdl_core.db.schema import create_schema

    create_schema(conn)
    conn.commit()
    conn.close()
    return p


def _add_subject(conn, sid=1, code="MATH"):
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (?, 1, ?, '数学', '#000', 0.35, 4, 9, 1)",
        (sid, code),
    )


def _add_mistake(conn, mid, **over):
    """插入一条 mistake_record，默认最小非空字段；over 覆盖任意列。"""
    base = {
        "id": mid,
        "user_id": 1,
        "kp_id": 0,
        "occurred_at": "2026-09-03T20:00:00Z",
        "subject": "MATH",
        "source": "REAL_WORK",
        "error_type": "METHOD",
        "attributed_by": "RULE_BASED",
        "severity": 3,
        "is_tamed": 0,
        "reappear_count": 0,
        "note_id": f"n{mid}",
    }
    base.update(over)
    cols = ", ".join(base)
    ph = ", ".join("?" for _ in base)
    conn.execute(
        f"INSERT INTO mistake_record ({cols}) VALUES ({ph})",
        list(base.values()),
    )


def _add_schedule(conn, sid, *, mistake_id, status="PENDING", due_date=TODAY, subject_id=1):
    conn.execute(
        "INSERT INTO review_schedule (id, user_id, subject_id, due_date, due_session,"
        " planned_interval_days, priority_score, est_seconds, status, source, mistake_id)"
        " VALUES (?, 1, ?, ?, 'AM', 1, 1.0, 60, ?, 'TEST', ?)",
        (sid, subject_id, due_date, status, mistake_id),
    )


def _add_feedback(conn, fid, schedule_id, rating, created_at="2026-09-05T10:00:00Z", subject_id=1):
    """写一条复习反馈（schedule_id 关联 review_schedule，用于 JOIN 归属到错题）。"""
    conn.execute(
        "INSERT INTO review_feedback (id, user_id, schedule_id, kp_id, subject_id,"
        " self_rating, created_at) VALUES (?, 1, ?, 0, ?, ?, ?)",
        (fid, schedule_id, subject_id, rating, created_at),
    )


# ── 1. 空库：checked=0、graduated=[]，全程不崩溃 ────────────────
def test_empty_db(db_path):
    conn = sqlite3.connect(db_path)
    res = check_graduation(conn, dry_run=True)
    conn.close()
    assert res["checked"] == 0
    assert res["graduated"] == []
    assert res["dry_run"] is True
    assert res["skipped_reasons"] == {
        "no_history": 0,
        "insufficient_grade": 0,
        "has_overdue": 0,
        "already_resolved": 0,
    }


# ── 2. 不满足 reappear（reappear=3、最近两次 grade=4）→ 不毕业 ──
def test_insufficient_reappear_not_graduate(db_path):
    conn = sqlite3.connect(db_path)
    _add_subject(conn)
    _add_mistake(conn, 1, reappear_count=3)
    _add_schedule(conn, 1, mistake_id=1, due_date=TODAY)
    # 历史足够但不满足 reappear 门槛 → 应在 reappear 检查处被挡
    _add_feedback(conn, 1, schedule_id=1, rating=4, created_at="2026-09-05T10:00:00Z")
    _add_feedback(conn, 2, schedule_id=1, rating=4, created_at="2026-09-06T10:00:00Z")
    conn.commit()

    res = check_graduation(conn, today=TODAY, dry_run=True)
    conn.close()
    assert 1 not in res["graduated"]
    assert res["skipped_reasons"]["no_history"] == 1
    assert res["skipped_reasons"]["insufficient_grade"] == 0
    assert res["skipped_reasons"]["has_overdue"] == 0


# ── 3. 不满足评分（reappear=6、最近两次 grade=[4,2]）→ 不毕业 ──
def test_insufficient_grade_not_graduate(db_path):
    conn = sqlite3.connect(db_path)
    _add_subject(conn)
    _add_mistake(conn, 1, reappear_count=6)
    _add_schedule(conn, 1, mistake_id=1, due_date=TODAY)
    # 最近两条（按 created_at DESC）为 [2(later), 4(earlier)]，有一次 <3 → 不毕业
    _add_feedback(conn, 1, schedule_id=1, rating=4, created_at="2026-09-05T10:00:00Z")
    _add_feedback(conn, 2, schedule_id=1, rating=2, created_at="2026-09-06T10:00:00Z")
    conn.commit()

    res = check_graduation(conn, today=TODAY, dry_run=True)
    conn.close()
    assert 1 not in res["graduated"]
    assert res["skipped_reasons"]["insufficient_grade"] == 1
    assert res["skipped_reasons"]["no_history"] == 0


# ── 4. 有逾期计划（reappear=6、grade=[4,4]、存在 due<today 的 PENDING）→ 不毕业 ──
def test_has_overdue_not_graduate(db_path):
    conn = sqlite3.connect(db_path)
    _add_subject(conn)
    _add_mistake(conn, 1, reappear_count=6)
    # due_date < TODAY → 逾期，应被 has_overdue 挡下
    _add_schedule(conn, 1, mistake_id=1, status="PENDING", due_date="2026-09-01")
    _add_feedback(conn, 1, schedule_id=1, rating=4, created_at="2026-09-05T10:00:00Z")
    _add_feedback(conn, 2, schedule_id=1, rating=4, created_at="2026-09-06T10:00:00Z")
    conn.commit()

    res = check_graduation(conn, today=TODAY, dry_run=True)
    conn.close()
    assert 1 not in res["graduated"]
    assert res["skipped_reasons"]["has_overdue"] == 1
    assert res["skipped_reasons"]["insufficient_grade"] == 0


# ── 5. 满足全部条件 → 进 graduated，且库内 resolved_at 非空、is_tamed=1 ──
def test_meets_all_conditions_graduates_and_updates(db_path):
    conn = sqlite3.connect(db_path)
    _add_subject(conn)
    _add_mistake(conn, 1, reappear_count=6)
    _add_schedule(conn, 1, mistake_id=1, due_date=TODAY)  # == TODAY，非逾期
    _add_feedback(conn, 1, schedule_id=1, rating=4, created_at="2026-09-05T10:00:00Z")
    _add_feedback(conn, 2, schedule_id=1, rating=4, created_at="2026-09-06T10:00:00Z")
    conn.commit()

    res = check_graduation(conn, today=TODAY, dry_run=False)
    assert 1 in res["graduated"]

    row = conn.execute("SELECT resolved_at, is_tamed FROM mistake_record WHERE id=1").fetchone()
    conn.close()
    assert row[0] is not None, "毕业后 resolved_at 应非空"
    assert row[1] == 1, "毕业后 is_tamed 应为 1"


# ── 6. dry_run=True → 返回 graduated 但库内不变 ────────────────
def test_dry_run_does_not_mutate(db_path):
    conn = sqlite3.connect(db_path)
    _add_subject(conn)
    _add_mistake(conn, 1, reappear_count=6)
    _add_schedule(conn, 1, mistake_id=1, due_date=TODAY)
    _add_feedback(conn, 1, schedule_id=1, rating=4, created_at="2026-09-05T10:00:00Z")
    _add_feedback(conn, 2, schedule_id=1, rating=4, created_at="2026-09-06T10:00:00Z")
    conn.commit()

    res = check_graduation(conn, today=TODAY, dry_run=True)
    assert 1 in res["graduated"]

    row = conn.execute("SELECT resolved_at, is_tamed FROM mistake_record WHERE id=1").fetchone()
    conn.close()
    assert row[0] is None, "dry_run 不应改库"
    assert row[1] == 0, "dry_run 不应改库"


# ── 7. 已 resolved 的错题不再重复处理（且不改其原值）────────────
def test_already_resolved_not_reprocessed(db_path):
    conn = sqlite3.connect(db_path)
    _add_subject(conn)
    # 已 resolved 的错题（也应满足其他条件，验证"跳过"而非"重复毕业"）
    _add_mistake(conn, 1, reappear_count=6, resolved_at="2026-09-01T10:00:00Z")
    # 未 resolved、满足全部条件的错题
    _add_mistake(conn, 2, reappear_count=6)
    _add_schedule(conn, 1, mistake_id=1, due_date=TODAY)
    _add_schedule(conn, 2, mistake_id=2, due_date=TODAY)
    _add_feedback(conn, 1, schedule_id=2, rating=4, created_at="2026-09-05T10:00:00Z")
    _add_feedback(conn, 2, schedule_id=2, rating=4, created_at="2026-09-06T10:00:00Z")
    conn.commit()

    # 显式传入列表（含已 resolved 的 1 号）
    res = check_graduation(conn, [1, 2], today=TODAY, dry_run=False)
    assert res["checked"] == 2
    assert res["skipped_reasons"]["already_resolved"] == 1
    assert 1 not in res["graduated"]
    assert 2 in res["graduated"]

    r1 = conn.execute("SELECT resolved_at, is_tamed FROM mistake_record WHERE id=1").fetchone()
    r2 = conn.execute("SELECT resolved_at, is_tamed FROM mistake_record WHERE id=2").fetchone()
    conn.close()
    # 1 号原值不被覆盖
    assert r1[0] == "2026-09-01T10:00:00Z"
    assert r1[1] == 0
    # 2 号正常毕业
    assert r2[0] is not None and r2[1] == 1


# ── 8. 全库扫描：checked 计入已 resolved（与生产库 checked=55 口径一致）──
def test_full_scan_counts_resolved_in_checked(db_path):
    conn = sqlite3.connect(db_path)
    _add_subject(conn)
    _add_mistake(conn, 1, reappear_count=6, resolved_at="2026-09-01T10:00:00Z")  # 已 resolved
    _add_mistake(conn, 2, reappear_count=6)  # 满足全部条件
    _add_schedule(conn, 1, mistake_id=1, due_date=TODAY)
    _add_schedule(conn, 2, mistake_id=2, due_date=TODAY)
    _add_feedback(conn, 1, schedule_id=2, rating=4, created_at="2026-09-05T10:00:00Z")
    _add_feedback(conn, 2, schedule_id=2, rating=4, created_at="2026-09-06T10:00:00Z")
    conn.commit()

    res = check_graduation(conn, today=TODAY, dry_run=False)
    conn.close()
    # 全库 2 条全部被扫描计入 checked（含已 resolved 的 1 号）
    assert res["checked"] == 2
    assert res["skipped_reasons"]["already_resolved"] == 1
    assert res["skipped_reasons"]["no_history"] == 0
    assert res["graduated"] == [2]
    # checked == 各 skipped 原因之和 + len(graduated)
    s = res["skipped_reasons"]
    assert res["checked"] == (
        s["no_history"] + s["insufficient_grade"] + s["has_overdue"] + s["already_resolved"]
    ) + len(res["graduated"])
