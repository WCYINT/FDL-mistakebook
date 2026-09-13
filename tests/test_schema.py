"""P2-01 建表验收测试。

验证 10 张表（§6.2 9 张 + §6.3 exam_record）字段逐条对齐、
唯一约束与外键约束生效、created_at 默认值填充。
"""

from __future__ import annotations

import sqlite3

import pytest

from fdl_core.db.schema import TABLE_NAMES, create_schema, get_connection

# 期望字段清单（逐表对齐 PRD §6.2 / §6.3；顺序无关，用集合比对）
EXPECTED_COLUMNS: dict[str, list[str]] = {
    "subject": [
        "id",
        "user_id",
        "code",
        "name",
        "short_name",
        "color_hex",
        "icon",
        "rotation_weight",
        "grade_start",
        "grade_end",
        "sort_order",
        "is_active",
        "created_at",
        "updated_at",
        "deleted_at",
    ],
    "knowledge_point": [
        "id",
        "subject_id",
        "parent_id",
        "code",
        "name",
        "description",
        "grade_level",
        "semester",
        "knowledge_domain",
        "bloom_level",
        "abstraction_level",
        "importance_weight",
        "exam_frequency",
        "base_difficulty",
        "est_learn_minutes",
        "est_review_seconds",
        "kp_type",
        "source",
        "source_ref",
        "tier",
        "tier_reason",
        "graph_version",
        "valid_from",
        "valid_to",
        "superseded_by",
        "created_at",
        "updated_at",
    ],
    "kp_prerequisite": [
        "id",
        "kp_id",
        "prerequisite_kp_id",
        "strength",
        "created_at",
        "updated_at",
    ],
    "question": [
        "id",
        "subject_id",
        "primary_kp_id",
        "question_type",
        "stem",
        "stem_image_url",
        "options",
        "answer",
        "explanation",
        "difficulty",
        "expected_seconds",
        "variant_type",
        "parent_question_id",
        "source",
        "quality_score",
        "is_golden",
        "is_validated",
        "usage_count",
        "created_at",
        "updated_at",
    ],
    "review_schedule": [
        "id",
        "user_id",
        "kp_id",
        "mistake_id",  # mistake_id：A3 调度单元（2026-09-08）
        "subject_id",
        "due_date",
        "due_session",
        "planned_interval_days",
        "interval_fuzz_applied",
        "priority_score",
        "est_seconds",
        "suggested_question_id",
        "status",
        "overdue_days",
        "source",
        "scheduled_for_date",
        "created_at",
        "updated_at",
    ],
    "daily_task": [
        "id",
        "user_id",
        "task_date",
        "session_slot",
        "task_type",
        "kp_id",
        "subject_id",
        "question_ids",
        "review_schedule_id",
        "title",
        "instruction",
        "est_seconds",
        "sort_order",
        "status",
        "skip_reason",
        "actual_seconds",
        "xp_earned",
        "created_at",
        "updated_at",
        "clean_image",
    ],
    "study_session": [
        "id",
        "user_id",
        "session_date",
        "session_slot",
        "trigger_type",
        "session_role",  # D-3（PRD §3.5 验收 4）
        "session_role",  # D-3（PRD §3.5 验收 4）
        "started_at",
        "ended_at",
        "duration_sec",
        "effective_sec",
        "free_explore_sec",
        "task_count",
        "completed_task_count",
        "answer_count",
        "correct_count",
        "task_type_breakdown",
        "subject_breakdown",
        "mastered_count",
        "regressed_count",
        "deep_behavior_count",
        "question_raised_count",
        "mood_before",
        "mood_after",
        "app_open_at",
        "launch_latency_sec",
        "is_overtime",
        "is_valid",
        "is_exploration_day",
        "device",
        "parent_accompanied",
        "session_state",
        "session_kind",
        "lease_token",
        "lease_expires_at",
        "lease_until",
        "resume_count",
        "last_event_at",  # 2026-09-09 King 要求：租约到期 + 续租次数 + 最后事件
        "review_mistake_id",  # OpenMAIC 借鉴 B：会话状态机 + 租约（2026-09-09）
        "created_at",
        "updated_at",
    ],
    "answer_log": [
        "id",
        "user_id",
        "session_id",
        "task_id",
        "question_id",
        "kp_id",
        "task_type",
        "grade",
        "is_correct",
        "response_seconds",
        "expected_seconds",
        "time_ratio",
        "hint_used",
        "retry_count",
        "input_mode",
        "self_confidence",
        "asr_first_token_ms",
        "user_answer",
        "explanation",
        "difficulty_at_time",
        "r_at_answer",
        "s_before",
        "s_after",
        "d_before",
        "d_after",
        "m_adj_before",
        "m_adj_after",
        "interval_days",
        "planned_interval_days",
        "overdue_days",
        "question_variant_type",
        "is_first_of_day",
        "is_valid_evidence",
        "grinding_flag",
        "overtime_flag",
        "answered_at",
        "created_at",
        "updated_at",
    ],
    "kp_state": [
        "id",
        "user_id",
        "kp_id",
        "subject_id",
        "status",
        "prev_status",
        "status_changed_at",
        "entered_mastered_at",
        "stability_days",
        "difficulty",
        "retrievability",
        "retrievability_calc_at",
        "lapses",
        "performance_score",
        "depth_score",
        "ability_estimate",
        "mastery_raw",
        "confidence",
        "mastery_adj",
        "exposure_count",
        "effective_answer_count",
        "total_answer_count",
        "consecutive_good_count",
        "consecutive_again_count",
        "mistake_count",
        "first_exposed_at",
        "last_review_at",
        "last_reteach_at",
        "next_due_at",
        "last_deep_behavior_at",
        "grinding_suspected",
        "grinding_freeze_until",
        "needs_parent_attention",
        "school_mastered",
        "is_active",
        "model_version",
        "created_at",
        "updated_at",
    ],
    "exam_record": [
        "id",
        "user_id",
        "subject_id",
        "kp_id",
        "exam_name",
        "exam_date",
        "exam_type",
        "score",
        "full_score",
        "weight",
        "is_correct",
        "question_snapshot_url",
        "created_at",
        "updated_at",
    ],
    "intervention_action": [
        "id",
        "user_id",
        "mistake_id",
        "kp_id",
        "subject_id",
        "trigger",
        "action_type",
        "payload_json",
        "scheduled_for_date",
        "status",
        "created_at",
        "updated_at",
    ],
}


@pytest.fixture
def schema_conn(tmp_path):
    """已建好 schema 的连接。"""
    conn = get_connection(tmp_path / "schema.db")
    create_schema(conn)
    yield conn
    conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def test_create_all_ten_tables(schema_conn):
    """全部建表（10 + review_feedback），表名与 TABLE_NAMES 一致。"""
    actual = set(
        r[0]
        for r in schema_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    assert actual == set(TABLE_NAMES)
    # 21（归因动态化三表）+ 2（KP 挂载提案 / 新知识点候选）+ 1（kp_relation 跨学科关联边）= 24
    assert len(TABLE_NAMES) == 24


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_columns_aligned_with_prd(schema_conn, table):
    """逐表校验字段集合对齐 PRD §6.2 / §6.3（无遗漏、无多余）。"""
    actual = set(_columns(schema_conn, table))
    expected = set(EXPECTED_COLUMNS[table])
    assert actual == expected, (
        f"{table} 字段不一致\n缺失: {expected - actual}\n多余: {actual - expected}"
    )


def test_subject_code_unique(schema_conn):
    schema_conn.execute(
        "INSERT INTO subject (user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 'MATH', '数学', '#4A90D9',"
        " 0.35, 4, 9, 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO subject (user_id, code, name, color_hex, rotation_weight,"
            " grade_start, grade_end, sort_order) VALUES (1, 'MATH', '数学重复',"
            " '#000000', 0.35, 4, 9, 1)"
        )


def test_knowledge_point_subject_code_unique(schema_conn):
    schema_conn.execute(
        "INSERT INTO subject (user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 'MATH', '数学', '#4A90D9',"
        " 0.35, 4, 9, 1)"
    )
    base = (
        "INSERT INTO knowledge_point (subject_id, code, name, grade_level,"
        " bloom_level, abstraction_level, importance_weight, base_difficulty,"
        " est_learn_minutes, est_review_seconds, kp_type, tier, graph_version,"
        " valid_from) VALUES (1, 'MATH-G4-FRAC-ADD', '异分母分数加减', 4, 2, 2,"
        " 1.0, 4.8, 1.5, 45, 'SKILL', 'L0', '2026.1', '2026-09-01')"
    )
    schema_conn.execute(base)
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(base)


def test_foreign_key_enforced(schema_conn):
    """knowledge_point.subject_id 指向不存在的 subject → 违反外键。"""
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO knowledge_point (subject_id, code, name, grade_level,"
            " bloom_level, abstraction_level, importance_weight, base_difficulty,"
            " est_learn_minutes, est_review_seconds, kp_type, tier, graph_version,"
            " valid_from) VALUES (999, 'MATH-X', '不存在学科', 4, 2, 2, 1.0, 4.8,"
            " 1.5, 45, 'SKILL', 'L0', '2026.1', '2026-09-01')"
        )


def test_kp_prerequisite_unique(schema_conn):
    schema_conn.execute(
        "INSERT INTO subject (user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 'MATH', '数学', '#4A90D9',"
        " 0.35, 4, 9, 1)"
    )
    kp = (
        "INSERT INTO knowledge_point (subject_id, code, name, grade_level,"
        " bloom_level, abstraction_level, importance_weight, base_difficulty,"
        " est_learn_minutes, est_review_seconds, kp_type, tier, graph_version,"
        " valid_from) VALUES (1, 'MATH-KP-A', 'A', 4, 1, 1, 1.0, 3.0, 1.0, 30,"
        " 'CONCEPT', 'L0', '2026.1', '2026-09-01')"
    )
    schema_conn.execute(kp)
    schema_conn.execute(kp.replace("MATH-KP-A", "MATH-KP-B").replace("'A'", "'B'"))
    schema_conn.execute(
        "INSERT INTO kp_prerequisite (kp_id, prerequisite_kp_id, strength) VALUES (1, 2, 'HARD')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO kp_prerequisite (kp_id, prerequisite_kp_id, strength)"
            " VALUES (1, 2, 'HARD')"
        )


def test_created_at_default_filled(schema_conn):
    schema_conn.execute(
        "INSERT INTO subject (user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 'MATH', '数学', '#4A90D9',"
        " 0.35, 4, 9, 1)"
    )
    row = schema_conn.execute("SELECT created_at, updated_at FROM subject WHERE id=1").fetchone()
    assert row[0] is not None
    assert row[1] is not None
