"""FDL 自我净化体检工具单元测试（不连生产库，全部用临时 sqlite）。

参考 tests/test_report_v1.py / tests/test_report_v2.py 的临时库建法：
- sqlite3.connect(tmp_path/...) + fdl_core.db.schema.create_schema
- 检测逻辑为可 import 函数 run_checks(db_path)，CLI 仅薄壳
- 通过 --db 等价机制：直接把临时库路径传给 run_checks
"""

from __future__ import annotations

import json
import sqlite3

# 让 `import scripts.fdl_self_purify` 可用（conftest 已把仓库根加入 sys.path）。
import sys
from pathlib import Path

import pytest

from scripts.fdl_self_purify import run_checks

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


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
        "note_id": f"n{mid}",
    }
    base.update(over)
    cols = ", ".join(base)
    ph = ", ".join("?" for _ in base)
    conn.execute(
        f"INSERT INTO mistake_record ({cols}) VALUES ({ph})",
        list(base.values()),
    )


def _add_schedule(conn, sid, *, mistake_id, status="PENDING", subject_id=1):
    conn.execute(
        "INSERT INTO review_schedule (id, user_id, subject_id, due_date, due_session,"
        " planned_interval_days, priority_score, est_seconds, status, source, mistake_id)"
        " VALUES (?, 1, ?, '2026-09-01', 'AM', 1, 1.0, 60, ?, 'TEST', ?)",
        (sid, subject_id, status, mistake_id),
    )


# ── 1. 空库：所有检测项 count=0 且全程不崩溃 ─────────────────────
def test_empty_db_all_zero(db_path, tmp_path):
    queue = tmp_path / "review_queue.json"
    queue.write_text("[]", encoding="utf-8")
    res = run_checks(db_path, review_queue_path=queue)
    assert res["summary"]["total_issues"] == 0
    for c in res["checks"]:
        assert c["count"] == 0, f"{c['key']} 应为 0，实际 {c['count']}"
        assert c["note"] is None or isinstance(c["note"], str)
    # 单条异常不中断整体由其他用例间接保证；此处确认结构完整
    keys = {c["key"] for c in res["checks"]}
    assert keys == {
        "zombie_mistakes",
        "diagnosis_type_broken",
        "kp_unmounted",
        "orphan_schedules",
        "broken_image_refs",
        "review_queue_backlog",
        "status_conflict",
        "legacy_label_migration",
        "pitfall_recurrence",
    }


# ── P2-2 失败模式复发检测（第 8 项）──────────────────────────────
# 带 recurrence_check 的三条失败模式（与 docs/pitfalls.jsonl 对齐）。
_PIT5 = {
    "signature": "record_no_review_plan",
    "family": "process",
    "first_seen": "2026-09-10",
    "stack_fingerprint": ["review.py"],
    "symptom": "x",
    "root_cause": "x",
    "fix": "x",
    "validated": True,
    "recurrence_check": "SELECT COUNT(*) FROM mistake_record m WHERE NOT EXISTS(SELECT 1 FROM review_schedule rs WHERE rs.mistake_id=m.id) 应为 0",
}
_PIT6 = {
    "signature": "fake_success_review_hook",
    "family": "ui",
    "first_seen": "2026-09-11",
    "stack_fingerprint": ["template.html"],
    "symptom": "x",
    "root_cause": "x",
    "fix": "x",
    "validated": True,
    "recurrence_check": "模板中 window.__review_feedback_submit = function 的定义应存在（≥1 处）",
}
_PIT7 = {
    "signature": "dual_error_field_broken",
    "family": "data",
    "first_seen": "2026-09-12",
    "stack_fingerprint": ["confirm"],
    "symptom": "x",
    "root_cause": "x",
    "fix": "x",
    "validated": True,
    "recurrence_check": "source='MANUAL' 且 error_type != diagnosis_type 的记录数应为 0",
}


def _write_pitfalls(root: Path, entries: list[dict]) -> None:
    """在隔离的 PROJECT_ROOT 下写一份失败模式库（含纪律注释头）。"""
    d = root / "docs"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "# P2-2 失败模式库（追加型账本）：每次修完 bug 追加一行，禁止整篇重写。",
    ]
    for e in entries:
        lines.append(json.dumps(e, ensure_ascii=False))
    (d / "pitfalls.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_template(root: Path, *, with_hook: bool = True) -> None:
    """在隔离的 PROJECT_ROOT 下写一份报告模板。"""
    tdir = root / "fdl_core" / "report"
    tdir.mkdir(parents=True, exist_ok=True)
    content = "window.__review_feedback_submit = function (payload) {};\n" if with_hook else ""
    (tdir / "template.html").write_text(content, encoding="utf-8")


def test_pitfall_recurrence_file_missing(db_path, tmp_path, monkeypatch):
    """失败模式库文件缺失 → 不报错、count=0、note 说明尚未建立。"""
    # 隔离 PROJECT_ROOT 且不复刻 docs/，使 pitfalls.jsonl 不存在
    monkeypatch.setattr("scripts.fdl_self_purify.PROJECT_ROOT", tmp_path)
    res = run_checks(db_path)
    c = next(x for x in res["checks"] if x["key"] == "pitfall_recurrence")
    assert c["count"] == 0
    assert c["note"] is not None and "尚未建立" in c["note"]


def test_pitfall_recurrence_no_review_plan(db_path, tmp_path, monkeypatch):
    """构造一条'无复习计划的错题' → 案例5 检出复发 ≥1。"""
    root = tmp_path / "proj"
    root.mkdir()
    _write_pitfalls(root, [_PIT5, _PIT6, _PIT7])
    _write_template(root, with_hook=True)
    monkeypatch.setattr("scripts.fdl_self_purify.PROJECT_ROOT", root)

    conn = sqlite3.connect(db_path)
    _add_mistake(conn, 88001)  # 默认无复习计划
    conn.commit()
    conn.close()

    res = run_checks(db_path)
    c = next(x for x in res["checks"] if x["key"] == "pitfall_recurrence")
    samp = next(s for s in c["samples"] if s["signature"] == "record_no_review_plan")
    assert samp["recurred"] is True
    assert c["count"] >= 1


def test_pitfall_recurrence_dual_field(db_path, tmp_path, monkeypatch):
    """构造 source=MANUAL 且 error_type=CONCEPT / diagnosis_type=CALC → 案例7 检出复发 ≥1。"""
    root = tmp_path / "proj2"
    root.mkdir()
    _write_pitfalls(root, [_PIT5, _PIT6, _PIT7])
    _write_template(root, with_hook=True)
    monkeypatch.setattr("scripts.fdl_self_purify.PROJECT_ROOT", root)

    conn = sqlite3.connect(db_path)
    # source=MANUAL，error_type=CONCEPT，diagnosis_type 走默认 CALC → 双字段断链
    _add_mistake(conn, 88002, source="MANUAL", error_type="CONCEPT")
    conn.commit()
    conn.close()

    res = run_checks(db_path)
    c = next(x for x in res["checks"] if x["key"] == "pitfall_recurrence")
    samp = next(s for s in c["samples"] if s["signature"] == "dual_error_field_broken")
    assert samp["recurred"] is True
    assert c["count"] >= 1


# ── 2. 僵尸错题 ────────────────────────────────────────────────
def test_zombie_mistake(db_path):
    conn = sqlite3.connect(db_path)
    _add_mistake(conn, 77001)  # reappear_count 默认 0，无 DONE 计划
    conn.commit()
    conn.close()

    res = run_checks(db_path)
    z = next(c for c in res["checks"] if c["key"] == "zombie_mistakes")
    assert z["count"] >= 1
    assert z["samples"][0]["id"] == 77001


# ── 3. 错因断链 ────────────────────────────────────────────────
def test_diagnosis_broken(db_path):
    conn = sqlite3.connect(db_path)
    # error_type=CONCEPT 但 diagnosis_type 默认 CALC → 断链
    _add_mistake(conn, 77002, error_type="CONCEPT")
    conn.commit()
    conn.close()

    res = run_checks(db_path)
    d = next(c for c in res["checks"] if c["key"] == "diagnosis_type_broken")
    assert d["count"] >= 1
    assert d["samples"][0]["id"] == 77002
    assert d["samples"][0]["error_type"] == "CONCEPT"


# ── 4. 孤儿复习计划 ────────────────────────────────────────────
def test_orphan_schedule(db_path):
    conn = sqlite3.connect(db_path)
    _add_subject(conn)
    _add_schedule(conn, 1, mistake_id=None)  # mistake_id 为空
    conn.commit()
    conn.close()

    res = run_checks(db_path)
    o = next(c for c in res["checks"] if c["key"] == "orphan_schedules")
    assert o["count"] >= 1
    assert o["samples"][0]["mistake_id"] is None


# ── 5. 状态矛盾 ────────────────────────────────────────────────
def test_status_conflict(db_path):
    conn = sqlite3.connect(db_path)
    _add_subject(conn)
    _add_mistake(conn, 77003, error_type="METHOD", resolved_at="2026-09-04T20:00:00Z")
    _add_schedule(conn, 2, mistake_id=77003, status="PENDING")
    conn.commit()
    conn.close()

    res = run_checks(db_path)
    s = next(c for c in res["checks"] if c["key"] == "status_conflict")
    assert s["count"] >= 1
    assert s["samples"][0]["mistake_id"] == 77003


# ── 6. --json 输出可写且能被 json.load 解析 ───────────────────
def test_json_output_parseable(db_path, tmp_path, monkeypatch):
    # 让默认 review_queue 指向空文件，保证幂等
    queue = tmp_path / "review_queue.json"
    queue.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("scripts.fdl_self_purify.PROJECT_ROOT", tmp_path)

    from scripts.fdl_self_purify import main

    out = tmp_path / "data"
    out.mkdir()
    report = out / "self_purify_report.json"

    main(["--db", str(db_path), "--json"])

    assert report.exists(), "JSON 报告未生成"
    data = json.loads(report.read_text(encoding="utf-8"))
    assert "checks" in data and isinstance(data["checks"], list)
    assert "summary" in data
    assert data["db_path"] == str(db_path)


def test_kp_unmounted_rate(db_path):
    """知识点未挂载：全部 kp_id=0 时挂载率应为 0%。"""
    conn = sqlite3.connect(db_path)
    _add_mistake(conn, 77010)
    _add_mistake(conn, 77011, kp_id=0)
    conn.commit()
    conn.close()

    res = run_checks(db_path)
    k = next(c for c in res["checks"] if c["key"] == "kp_unmounted")
    assert k["count"] == 2
    assert "挂载率 0.0%" in (k["note"] or "")


def test_review_queue_backlog_counts_pending(db_path, tmp_path):
    """review_queue 中 pending 计入积压，且不崩溃。"""
    queue = tmp_path / "review_queue.json"
    queue.write_text(
        json.dumps(
            [
                {"src": "a", "status": "pending"},
                {"src": "b", "status": "confirmed"},
                {"src": "c", "status": "pending"},
            ]
        ),
        encoding="utf-8",
    )
    res = run_checks(db_path, review_queue_path=queue)
    r = next(c for c in res["checks"] if c["key"] == "review_queue_backlog")
    assert r["count"] == 2


def test_review_queue_missing_file_is_safe(db_path, tmp_path):
    """文件缺失不应报错，count=0 且 note 说明原因。"""
    missing = tmp_path / "no_such.json"
    res = run_checks(db_path, review_queue_path=missing)
    r = next(c for c in res["checks"] if c["key"] == "review_queue_backlog")
    assert r["count"] == 0
    assert r["note"] is not None


# ── 7. 旧口径标签迁移欠账 ──────────────────────────────────────
def test_legacy_migration_broken(db_path):
    """error_type=METHOD + diagnosis_type=CALC（默认）→ 计入欠账。"""
    conn = sqlite3.connect(db_path)
    _add_mistake(conn, 77020, error_type="METHOD", diagnosis_type="CALC")
    conn.commit()
    conn.close()

    res = run_checks(db_path)
    c = next(x for x in res["checks"] if x["key"] == "legacy_label_migration")
    assert c["count"] >= 1
    assert c["samples"][0]["id"] == 77020
    assert "尚未迁移" in (c["note"] or "")


def test_legacy_migration_aligned_not_counted(db_path):
    """error_type=CONCEPT + diagnosis_type=CONCEPT（已对齐新口径）→ 不计入。"""
    conn = sqlite3.connect(db_path)
    _add_mistake(conn, 77021, error_type="CONCEPT", diagnosis_type="CONCEPT")
    conn.commit()
    conn.close()

    res = run_checks(db_path)
    c = next(x for x in res["checks"] if x["key"] == "legacy_label_migration")
    assert c["count"] == 0


# ── 8. 图片引用缺失（2026-09-12 新增：以防 #77056 类问题复发）──────
def test_broken_image_ref_detected(db_path, tmp_path, monkeypatch):
    """img_original 指向不存在的文件 → 检出（相对路径按 git 根拼接）。"""
    monkeypatch.setattr("fdl_core.paths._default_paths", None)
    from fdl_core.paths import FdlPaths

    fake_root = tmp_path / "vault"
    fake_root.mkdir()
    monkeypatch.setattr(
        "fdl_core.paths.get_paths",
        lambda: FdlPaths(root=fake_root),
    )
    conn = sqlite3.connect(db_path)
    _add_mistake(conn, 77056, img_original="2-语文/T-听写错题/missing.jpg")
    conn.commit()
    conn.close()

    res = run_checks(db_path)
    c = next(x for x in res["checks"] if x["key"] == "broken_image_refs")
    assert c["count"] == 1
    assert c["samples"][0]["mistake_id"] == 77056
    assert c["samples"][0]["field"] == "img_original"
    assert "404" in (c["note"] or "")


def test_existing_image_not_counted(db_path, tmp_path, monkeypatch):
    """文件真实存在（先创建）→ 不检出。"""
    from fdl_core.paths import FdlPaths

    fake_root = tmp_path / "vault2"
    (fake_root / "1-Math").mkdir(parents=True)
    img = fake_root / "1-Math" / "ok.jpg"
    img.write_bytes(b"jpeg")
    monkeypatch.setattr("fdl_core.paths.get_paths", lambda: FdlPaths(root=fake_root))
    conn = sqlite3.connect(db_path)
    _add_mistake(conn, 77012, img_original="1-Math/ok.jpg")
    conn.commit()
    conn.close()

    res = run_checks(db_path)
    c = next(x for x in res["checks"] if x["key"] == "broken_image_refs")
    assert c["count"] == 0
