"""批次 3+4 验收测试：MS-02 三层归因链 / MS-03 怪兽标签 / MS-04 映射 /
D-3 session_role / ING-07 空白卷重做 / SRS-06 年限层 / 双写 note_id 验证。
"""

from __future__ import annotations

import sqlite3

import pytest

from fdl_core.db.schema import create_schema, ensure_session_role_column
from fdl_core.ingest.redo import start_redo_from_clean
from fdl_core.mistakes.attribution import (
    ATTRIBUTION_GRADE_MAP,
    MONSTERS,
    attribution_to_grade,
    run_attribution_chain,
    validate_monster_tag,
)
from fdl_core.mistakes.dual_write import link_note, verify_dual_write
from fdl_core.mistakes.tables import ensure_mistake_table
from fdl_core.srs.scheduling_rules import apply_annual_tier, incomplete_days_streak


# ── MS-02 三层归因链 ───────────────────────────────────────
def test_chain_check_fixed_is_mistake():
    r = run_attribution_chain(check_fixed=True, hint_fixed=False)
    assert (r.outcome, r.grade, r.escalated) == ("MISTAKE_FIXED", 2, False)
    assert r.label == "失误"


def test_chain_hint_fixed_is_hard():
    r = run_attribution_chain(check_fixed=False, hint_fixed=True)
    assert (r.outcome, r.grade) == ("SOLVED_WITH_HINT", 1)
    assert r.label == "提示下可解" and r.escalated


def test_chain_truly_cant_is_again():
    r = run_attribution_chain(check_fixed=False, hint_fixed=False)
    assert (r.outcome, r.grade) == ("TRULY_CANT", 0)
    assert r.label == "真不会"


def test_chain_skip_goes_straight_to_truly_cant():
    """🔴 可随时跳过：跳过检查 = 直接真不会（Again 完整重置）。"""
    r = run_attribution_chain(check_fixed=False, hint_fixed=False, skipped=True)
    assert r.outcome == "TRULY_CANT" and r.escalated is False


def test_chain_check_fixed_wins_over_hint():
    """检查层改对时不再进提示层（降级优先）。"""
    r = run_attribution_chain(check_fixed=True, hint_fixed=True)
    assert r.outcome == "MISTAKE_FIXED"


# ── MS-04 映射 ─────────────────────────────────────────────
def test_attribution_grade_map_strict():
    assert ATTRIBUTION_GRADE_MAP == {
        "MISTAKE_FIXED": 2,
        "SOLVED_WITH_HINT": 1,
        "TRULY_CANT": 0,
    }
    assert attribution_to_grade("TRULY_CANT") == 0
    with pytest.raises(ValueError):
        attribution_to_grade("UNKNOWN")


# ── MS-03 怪兽标签 ─────────────────────────────────────────
def test_seven_monsters_plus_other():
    """验收 2：7 类怪兽 + OTHER 全覆盖。"""
    core = [k for k in MONSTERS if k != "OTHER"]
    assert len(core) == 7
    for k in ("CARELESS", "CONFUSION", "MISREAD", "METHOD", "EXPRESSION", "FORGOT", "TIMEOUT"):
        assert k in MONSTERS and MONSTERS[k]


def test_validate_monster_tag_rejects_llm_suggest():
    """🔴 无 LLM_SUGGEST（PRD §4.2.2）。"""
    validate_monster_tag("METHOD", "RULE_BASED")
    with pytest.raises(ValueError, match="LLM_SUGGEST"):
        validate_monster_tag("METHOD", "LLM_SUGGEST")
    with pytest.raises(ValueError, match="未知怪兽"):
        validate_monster_tag("DRAGON", "FRANK")


# ── D-3 session_role ───────────────────────────────────────
def test_new_schema_has_session_role():
    conn = sqlite3.connect(":memory:")
    create_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(study_session)")]
    assert "session_role" in cols
    conn.execute(
        "INSERT INTO study_session (id, user_id, session_date, session_slot, trigger_type,"
        " session_role, started_at, duration_sec, effective_sec)"
        " VALUES (1, 1, '2026-09-04', 'AM', 'SELF', 'KING_ACCOMPANIED',"
        " '2026-09-04T08:00:00Z', 600, 580)"
    )
    assert conn.execute("SELECT session_role FROM study_session WHERE id=1").fetchone()[0] == (
        "KING_ACCOMPANIED"
    )


def test_migration_adds_role_to_legacy_db():
    """老库迁移：无 session_role 的库 ALTER 补列。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE study_session (id INTEGER PRIMARY KEY, user_id INTEGER,"
        " session_date TEXT, session_slot TEXT, trigger_type TEXT, started_at TEXT,"
        " ended_at TEXT, duration_sec INTEGER, effective_sec INTEGER,"
        " free_explore_sec INTEGER NOT NULL DEFAULT 0, task_count INTEGER NOT NULL DEFAULT 0,"
        " completed_task_count INTEGER NOT NULL DEFAULT 0, answer_count INTEGER NOT NULL DEFAULT 0,"
        " correct_count INTEGER NOT NULL DEFAULT 0, is_valid INTEGER NOT NULL DEFAULT 1)"
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(study_session)")]
    assert "session_role" not in cols
    ensure_session_role_column(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(study_session)")]
    assert "session_role" in cols
    # 幂等
    ensure_session_role_column(conn)


# ── 批次 4：ING-07 空白卷重做 ──────────────────────────────
def test_start_redo_from_clean(tmp_path):
    conn = sqlite3.connect(":memory:")
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000', 0.35, 4, 9, 1)"
    )
    conn.execute(
        "INSERT INTO knowledge_point (id, subject_id, code, name, grade_level, bloom_level,"
        " abstraction_level, importance_weight, base_difficulty, est_learn_minutes,"
        " est_review_seconds, kp_type, tier, graph_version, valid_from)"
        " VALUES (1, 1, 'MATH-A', 'A', 4, 2, 2, 1.0, 4.8, 1.5, 45, 'SKILL', 'L0',"
        " '2026.1', '2026-09-01')"
    )
    clean = tmp_path / "M-001-original-clean.jpg"
    clean.write_bytes(b"jpg")
    conn.commit()
    task_id = start_redo_from_clean(conn, kp_id=1, clean_path=clean, user_id=1)
    row = conn.execute(
        "SELECT task_type, status, clean_image FROM daily_task WHERE id=?", (task_id,)
    ).fetchone()
    assert row[0] == "REVIEW" and row[1] == "PENDING" and row[2] == str(clean)


# ── 批次 4：SRS-06 年限层 / SRS-07 减压 ────────────────────
def test_annual_tier_floor():
    """累计复习 ≥10 且近 2 次 Good → 间隔下限 120 天（PRD annual_tier_after=10）。"""
    assert apply_annual_tier(interval=60, total_answers=12, last_two_grades=[2, 3]) == 120
    assert apply_annual_tier(interval=60, total_answers=12, last_two_grades=[2, 0]) == 60
    assert apply_annual_tier(interval=60, total_answers=5, last_two_grades=[2, 3]) == 60
    # 更长间隔不被压短
    assert apply_annual_tier(interval=200, total_answers=12, last_two_grades=[2, 3]) == 200


def test_incomplete_days_streak(tmp_path):
    conn = sqlite3.connect(":memory:")
    create_schema(conn)
    for d in ("2026-08-30", "2026-08-31", "2026-09-01"):
        conn.execute(
            "INSERT INTO daily_task (id, user_id, task_date, session_slot, task_type, kp_id,"
            " subject_id, title, est_seconds, sort_order, status)"
            f" VALUES (NULL, 1, '{d}', 'AM', 'REVIEW', 1, 1, 't', 45, 0, 'PENDING')"
        )
    conn.commit()
    assert incomplete_days_streak(conn, today="2026-09-02") == 3
    conn.execute("UPDATE daily_task SET status='DONE' WHERE task_date='2026-09-01'")
    conn.commit()
    # 9/1 已完成 → 连续未完成被打断（streak=0；8/31 起的历史不计）
    assert incomplete_days_streak(conn, today="2026-09-02") == 0


# ── 批次 4：G14 双写 note_id 一致性 ────────────────────────
def test_dual_write_link_and_verify(tmp_path):
    conn = sqlite3.connect(":memory:")
    ensure_mistake_table(conn)
    conn.execute(
        "INSERT INTO mistake_record (id, user_id, kp_id, occurred_at, subject, note_id)"
        " VALUES (1, 1, 1, '2026-09-03T20:00:00Z', 'MATH', '77001')"
    )
    conn.commit()
    notes_dir = tmp_path / "cards"
    notes_dir.mkdir()
    (notes_dir / "M-20260903-001.md").write_text(
        "---\ntype: mistake\nid: 77001\n---\n\n# 卡\n", encoding="utf-8"
    )
    (notes_dir / "M-20260903-002.md").write_text(
        "---\ntype: mistake\nid: 77002\n---\n\n# 无 DB 对应\n", encoding="utf-8"
    )
    r = verify_dual_write(conn, notes_dir)
    assert r["db_without_file"] == [] and r["file_without_db"] == ["77002"]
    # link 后一致
    link_note(conn, mistake_id=1, note_id="77001")
    r2 = verify_dual_write(conn, notes_dir)
    assert r2["db_without_file"] == [] and r2["file_without_db"] == ["77002"]
