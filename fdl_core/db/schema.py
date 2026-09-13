"""FDL 数据库 Schema 定义（P2-01 / OPS-01）。

P0 建表批次：PRD v1.1 §6.2 的 9 张核心表 + §6.3 的 `exam_record`（共 10 张）。
字段逐条对齐 §6.2；类型映射到 SQLite affinity。

通用约定（PRD §6.1）：
- 所有表含 `id INTEGER PRIMARY KEY`（自增）+ `created_at` + `updated_at`
- 时间统一存 UTC（TEXT ISO-8601），展示 Asia/Shanghai；统一时间层由 P2-06 落地
- 枚举字段用 TEXT + 应用层约束（SQLite 友好，不在 SQL 加 CHECK）
- 布尔用 INTEGER 0/1；JSON 用 TEXT；软删除用 `deleted_at`（仅 subject 显式声明）

类型映射（PostgreSQL 风格 → SQLite affinity）：
    BIGINT / SMALLINT / INT  →  INTEGER
    DECIMAL(p,s)             →  REAL
    VARCHAR(n) / TEXT        →  TEXT
    TIMESTAMP / DATE         →  TEXT（ISO-8601 UTC / YYYY-MM-DD）
    BOOLEAN                  →  INTEGER (0/1)
    JSON                     →  TEXT
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# 时间戳默认值：UTC 当前时间（ISO-8601，秒级）。P2-06 落地统一时间层后如需调整格式，在此处集中改。
_TS_DEFAULT = "(strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"

# ── 建表顺序：按外键依赖拓扑（被引用表先建）─────────────────────────────
# subject → knowledge_point → kp_prerequisite → question → review_schedule
#   → daily_task → study_session → answer_log → kp_state → exam_record
_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS subject (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    code            TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    short_name      TEXT,
    color_hex       TEXT    NOT NULL,
    icon            TEXT,
    rotation_weight REAL    NOT NULL,
    grade_start     INTEGER NOT NULL,
    grade_end       INTEGER NOT NULL,
    sort_order      INTEGER NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at      TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    deleted_at      TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_point (
    id                 INTEGER PRIMARY KEY,
    subject_id         INTEGER NOT NULL REFERENCES subject(id),
    parent_id          INTEGER REFERENCES knowledge_point(id),
    code               TEXT    NOT NULL,
    name               TEXT    NOT NULL,
    description        TEXT,
    grade_level        INTEGER NOT NULL,
    semester           INTEGER,
    knowledge_domain   TEXT,
    bloom_level        INTEGER NOT NULL,
    abstraction_level  INTEGER NOT NULL,
    importance_weight  REAL    NOT NULL,
    exam_frequency     INTEGER,
    base_difficulty    REAL    NOT NULL,
    est_learn_minutes  REAL    NOT NULL,
    est_review_seconds INTEGER NOT NULL,
    kp_type            TEXT    NOT NULL,
    source             TEXT,
    source_ref         TEXT,
    tier               TEXT    NOT NULL,
    tier_reason        TEXT,
    graph_version      TEXT    NOT NULL,
    valid_from         TEXT    NOT NULL,
    valid_to           TEXT,
    superseded_by      INTEGER REFERENCES knowledge_point(id),
    created_at         TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at         TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    UNIQUE (subject_id, code)
);

CREATE TABLE IF NOT EXISTS kp_prerequisite (
    id                 INTEGER PRIMARY KEY,
    kp_id              INTEGER NOT NULL REFERENCES knowledge_point(id),
    prerequisite_kp_id INTEGER NOT NULL REFERENCES knowledge_point(id),
    strength           TEXT    NOT NULL,
    created_at         TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at         TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    UNIQUE (kp_id, prerequisite_kp_id)
);

CREATE TABLE IF NOT EXISTS question (
    id                 INTEGER PRIMARY KEY,
    subject_id         INTEGER NOT NULL REFERENCES subject(id),
    primary_kp_id      INTEGER NOT NULL REFERENCES knowledge_point(id),
    question_type      TEXT    NOT NULL,
    stem               TEXT    NOT NULL,
    stem_image_url     TEXT,
    options            TEXT,
    answer             TEXT    NOT NULL,
    explanation        TEXT,
    difficulty         REAL    NOT NULL,
    expected_seconds   INTEGER NOT NULL,
    variant_type       TEXT    NOT NULL,
    parent_question_id INTEGER REFERENCES question(id),
    source             TEXT    NOT NULL,
    quality_score      REAL,
    is_golden          INTEGER NOT NULL DEFAULT 0,
    is_validated       INTEGER NOT NULL DEFAULT 1,
    usage_count        INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at         TEXT    NOT NULL DEFAULT {_TS_DEFAULT}
);

CREATE TABLE IF NOT EXISTS review_schedule (
    id                    INTEGER PRIMARY KEY,
    user_id               INTEGER NOT NULL,
    kp_id                 INTEGER          REFERENCES knowledge_point(id),  -- 2026-09-09 可空（未挂载 KP 时 NULL）
    mistake_id            INTEGER          REFERENCES mistake_record(id),  -- 2026-09-08 A3：调度单元
    subject_id            INTEGER NOT NULL REFERENCES subject(id),
    due_date              TEXT    NOT NULL,
    due_session           TEXT    NOT NULL,
    planned_interval_days INTEGER NOT NULL,
    interval_fuzz_applied REAL,
    priority_score        REAL    NOT NULL,
    est_seconds           INTEGER NOT NULL,
    suggested_question_id INTEGER REFERENCES question(id),
    status                TEXT    NOT NULL,
    overdue_days          INTEGER NOT NULL DEFAULT 0,
    source                TEXT    NOT NULL,
    scheduled_for_date    TEXT,
    created_at            TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at            TEXT    NOT NULL DEFAULT {_TS_DEFAULT}
);

-- 部分唯一索引：调度单元为 mistake_id（2026-09-08 迁移）
-- 同错题同状态只允许一个 PENDING（DONE 历史可并存——重排场景合法）
-- 注意：原索引建在 (user_id, kp_id)，但 kp_id 全为 0 占位 → 全库只能共存 1 条 PENDING，
-- 任一新复习动作都会 UNIQUE 冲突崩溃。此处改为 (user_id, mistake_id)。
DROP INDEX IF EXISTS ux_review_pending;
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_pending
    ON review_schedule (user_id, mistake_id) WHERE status = 'PENDING';

-- A3（2026-09-08）：调度单元改 mistake_id（原 kp_id 被借用作 mistake id，语义错乱）
-- 按"错题卡"而非"知识点"调度——契合 FDL 当前 kp_id 全 0 占位、知识点体系未建的现实
CREATE INDEX IF NOT EXISTS idx_review_schedule_mistake
    ON review_schedule (mistake_id, status);

-- 复习反馈表：用户在复习完成后填写（含录音/图片/文件附件路径），用于追潮与错题迭代
-- 一条记录对应一次 review_schedule 的 DONE 事件，可选多附件
-- 设计原则：与 review_schedule 解耦（即使 schedule 被重排/删除，反馈历史仍保留）
CREATE TABLE IF NOT EXISTS review_feedback (
    id                INTEGER PRIMARY KEY,
    user_id           INTEGER NOT NULL,
    schedule_id       INTEGER REFERENCES review_schedule(id),  -- 软关联（schedule 删后保留）
    kp_id             INTEGER          REFERENCES knowledge_point(id),  -- 2026-09-09 可空（未挂载 KP 时 NULL）
    subject_id        INTEGER NOT NULL REFERENCES subject(id),
    self_rating       INTEGER NOT NULL,  -- 1陌生/2模糊/3掌握/4熟练
    duration_seconds  INTEGER,           -- 本次复习耗时（秒）
    note              TEXT,               -- 用户文字记录
    attachments_json  TEXT,               -- JSON array: items have kind/path/mime/sha256/note
    srs_interval_days REAL,               -- 触发时 planned_interval_days（用于审计）
    created_at        TEXT    NOT NULL DEFAULT {_TS_DEFAULT}
);

CREATE INDEX IF NOT EXISTS idx_review_feedback_user_kp_created
    ON review_feedback (user_id, kp_id, created_at);

-- 干预动作表（OpenMAIC 借鉴 B：错因诊断 → 反馈闭环）。
-- 由"首次答错/逾期/低信心"等触发，记录三层归因诊断结果与推荐干预，
-- 供复习编排（session_orchestrator）调度执行。与 mistake_record 软关联
-- （错题重做/删除后干预历史仍保留，便于溯源）。
CREATE TABLE IF NOT EXISTS intervention_action (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER NOT NULL,
    mistake_id          INTEGER          REFERENCES mistake_record(id),  -- 软关联
    kp_id               INTEGER          REFERENCES knowledge_point(id),  -- 2026-09-09 可空（未挂载 KP 时 NULL）
    subject_id          INTEGER NOT NULL REFERENCES subject(id),
    trigger             TEXT    NOT NULL,  -- FIRST_WRONG / OVERDUE / LOW_CONFIDENCE / STAGNATION
    action_type         TEXT    NOT NULL,  -- DIAGNOSE / RETEACH / SIMPLIFY / HINT / PARENT_NUDGE
    payload_json        TEXT,              -- JSON：三层归因 / 推荐内容
    scheduled_for_date  TEXT,
    status              TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING / DONE / SKIPPED
    created_at          TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at          TEXT    NOT NULL DEFAULT {_TS_DEFAULT}
);

CREATE INDEX IF NOT EXISTS idx_intervention_user_status
    ON intervention_action (user_id, status, scheduled_for_date);

CREATE TABLE IF NOT EXISTS daily_task (
    id                 INTEGER PRIMARY KEY,
    user_id            INTEGER NOT NULL,
    task_date          TEXT    NOT NULL,
    session_slot       TEXT    NOT NULL,
    task_type          TEXT    NOT NULL,
    kp_id              INTEGER NOT NULL REFERENCES knowledge_point(id),
    subject_id         INTEGER NOT NULL REFERENCES subject(id),
    question_ids       TEXT,
    review_schedule_id INTEGER REFERENCES review_schedule(id),
    title              TEXT    NOT NULL,
    instruction        TEXT,
    est_seconds        INTEGER NOT NULL,
    sort_order         INTEGER NOT NULL,
    status             TEXT    NOT NULL,
    skip_reason        TEXT,
    actual_seconds     INTEGER,
    xp_earned          INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at         TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    clean_image        TEXT
);

CREATE TABLE IF NOT EXISTS study_session (
    id                    INTEGER PRIMARY KEY,
    user_id               INTEGER NOT NULL,
    session_date          TEXT    NOT NULL,
    session_slot          TEXT    NOT NULL,
    trigger_type          TEXT    NOT NULL,
    -- D-3：SELF / KING_ACCOMPANIED（陪伴独立计时，不污染 SRS 容量）
    session_role          TEXT    NOT NULL DEFAULT 'SELF',
    started_at            TEXT    NOT NULL,
    ended_at              TEXT,
    duration_sec          INTEGER NOT NULL,
    effective_sec         INTEGER NOT NULL,
    free_explore_sec      INTEGER NOT NULL DEFAULT 0,
    task_count            INTEGER NOT NULL DEFAULT 0,
    completed_task_count  INTEGER NOT NULL DEFAULT 0,
    answer_count          INTEGER NOT NULL DEFAULT 0,
    correct_count         INTEGER NOT NULL DEFAULT 0,
    task_type_breakdown   TEXT,
    subject_breakdown     TEXT,
    mastered_count        INTEGER NOT NULL DEFAULT 0,
    regressed_count       INTEGER NOT NULL DEFAULT 0,
    deep_behavior_count   INTEGER NOT NULL DEFAULT 0,
    question_raised_count INTEGER NOT NULL DEFAULT 0,
    mood_before           INTEGER,
    mood_after            INTEGER,
    app_open_at           TEXT,
    launch_latency_sec    INTEGER,
    is_overtime           INTEGER NOT NULL DEFAULT 0,
    is_valid              INTEGER NOT NULL DEFAULT 1,
    is_exploration_day    INTEGER NOT NULL DEFAULT 0,
    device                TEXT,
    parent_accompanied    INTEGER NOT NULL DEFAULT 0,
    -- OpenMAIC 借鉴 B（复习会话状态机 + 租约，2026-09-09）：
    session_state         TEXT    NOT NULL DEFAULT 'ACTIVE',  -- CREATED/ACTIVE/PAUSED/COMPLETED/ABANDONED/ERROR
    session_kind          TEXT    NOT NULL DEFAULT 'REVIEW',  -- REVIEW/LEARN/MIXED
    lease_token           TEXT,                          -- orchestrator 租约令牌
    lease_expires_at      TEXT,                          -- 租约过期 UTC
    lease_until           TEXT,                          -- 2026-09-09 King 要求：租约到期（UTC ISO）
    resume_count          INTEGER NOT NULL DEFAULT 0,    -- 2026-09-09 King 要求：PAUSED→ACTIVE 次数
    last_event_at         TEXT,                          -- 2026-09-09 King 要求：最后一次状态迁移时间
    review_mistake_id     INTEGER,                       -- 本次复习针对的错题（软关联）
    created_at            TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at            TEXT    NOT NULL DEFAULT {_TS_DEFAULT}
);

CREATE TABLE IF NOT EXISTS answer_log (
    id                    INTEGER PRIMARY KEY,
    user_id               INTEGER NOT NULL,
    session_id            INTEGER NOT NULL REFERENCES study_session(id),
    task_id               INTEGER NOT NULL REFERENCES daily_task(id),
    question_id           INTEGER NOT NULL REFERENCES question(id),
    kp_id                 INTEGER NOT NULL REFERENCES knowledge_point(id),
    task_type             TEXT    NOT NULL,
    grade                 INTEGER NOT NULL,
    is_correct            INTEGER NOT NULL,
    response_seconds      INTEGER,
    expected_seconds      INTEGER NOT NULL,
    time_ratio            REAL,
    hint_used             INTEGER NOT NULL DEFAULT 0,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    input_mode            TEXT    NOT NULL,
    self_confidence       INTEGER,
    asr_first_token_ms    INTEGER,
    user_answer           TEXT,
    explanation           TEXT,
    difficulty_at_time    REAL    NOT NULL,
    r_at_answer           REAL    NOT NULL,
    s_before              REAL    NOT NULL,
    s_after               REAL    NOT NULL,
    d_before              REAL    NOT NULL,
    d_after               REAL    NOT NULL,
    m_adj_before          REAL,
    m_adj_after           REAL,
    interval_days         INTEGER,
    planned_interval_days INTEGER,
    overdue_days          INTEGER,
    question_variant_type TEXT,
    is_first_of_day       INTEGER NOT NULL DEFAULT 0,
    is_valid_evidence     INTEGER NOT NULL DEFAULT 1,
    grinding_flag         INTEGER NOT NULL DEFAULT 0,
    overtime_flag         INTEGER NOT NULL DEFAULT 0,
    answered_at           TEXT    NOT NULL,
    created_at            TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at            TEXT    NOT NULL DEFAULT {_TS_DEFAULT}
);

CREATE TABLE IF NOT EXISTS kp_state (
    id                      INTEGER PRIMARY KEY,
    user_id                 INTEGER NOT NULL,
    kp_id                   INTEGER NOT NULL REFERENCES knowledge_point(id),
    subject_id              INTEGER NOT NULL REFERENCES subject(id),
    status                  TEXT    NOT NULL,
    prev_status             TEXT,
    status_changed_at       TEXT,
    entered_mastered_at     TEXT,
    stability_days          REAL    NOT NULL,
    difficulty              REAL    NOT NULL,
    retrievability          REAL,
    retrievability_calc_at  TEXT,
    lapses                  INTEGER NOT NULL DEFAULT 0,
    performance_score       REAL,
    depth_score             REAL,
    ability_estimate        REAL,
    mastery_raw             REAL,
    confidence              REAL,
    mastery_adj             REAL,
    exposure_count          INTEGER NOT NULL DEFAULT 0,
    effective_answer_count  INTEGER NOT NULL DEFAULT 0,
    total_answer_count      INTEGER NOT NULL DEFAULT 0,
    consecutive_good_count  INTEGER NOT NULL DEFAULT 0,
    consecutive_again_count INTEGER NOT NULL DEFAULT 0,
    mistake_count           INTEGER NOT NULL DEFAULT 0,
    first_exposed_at        TEXT,
    last_review_at          TEXT,
    last_reteach_at         TEXT,
    next_due_at             TEXT,
    last_deep_behavior_at   TEXT,
    grinding_suspected      INTEGER NOT NULL DEFAULT 0,
    grinding_freeze_until   TEXT,
    needs_parent_attention  INTEGER NOT NULL DEFAULT 0,
    school_mastered         INTEGER NOT NULL DEFAULT 0,
    is_active               INTEGER NOT NULL DEFAULT 1,
    model_version           TEXT,
    created_at              TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at              TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    UNIQUE (user_id, kp_id)
);

CREATE TABLE IF NOT EXISTS exam_record (
    id                    INTEGER PRIMARY KEY,
    user_id               INTEGER NOT NULL,
    subject_id            INTEGER NOT NULL REFERENCES subject(id),
    kp_id                 INTEGER NOT NULL REFERENCES knowledge_point(id),
    exam_name             TEXT    NOT NULL,
    exam_date             TEXT    NOT NULL,
    exam_type             TEXT    NOT NULL,
    score                 REAL    NOT NULL,
    full_score            REAL    NOT NULL,
    weight                REAL    NOT NULL,
    is_correct            INTEGER NOT NULL,
    question_snapshot_url TEXT,
    created_at            TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at            TEXT    NOT NULL DEFAULT {_TS_DEFAULT}
);

-- ── 索引（对齐 PRD §6.2 各表"索引"注记）────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_answer_log_user_kp_time
    ON answer_log (user_id, kp_id, answered_at DESC);
CREATE INDEX IF NOT EXISTS idx_answer_log_answered_at
    ON answer_log (answered_at);
CREATE INDEX IF NOT EXISTS idx_answer_log_user_type_time
    ON answer_log (user_id, task_type, answered_at);

CREATE INDEX IF NOT EXISTS idx_kp_state_user_status_due
    ON kp_state (user_id, status, next_due_at);
CREATE INDEX IF NOT EXISTS idx_kp_state_user_status
    ON kp_state (user_id, status);
CREATE INDEX IF NOT EXISTS idx_kp_state_user_mastery
    ON kp_state (user_id, mastery_adj);

CREATE INDEX IF NOT EXISTS idx_review_schedule_user_status_due
    ON review_schedule (user_id, status, due_date, priority_score);

CREATE INDEX IF NOT EXISTS idx_study_session_user_date
    ON study_session (user_id, session_date);
CREATE INDEX IF NOT EXISTS idx_study_session_user_started
    ON study_session (user_id, started_at);
CREATE INDEX IF NOT EXISTS idx_study_session_user_valid_date
    ON study_session (user_id, is_valid, session_date);

-- ─── 错题本核心表 ─────────────────────────────────────────────
-- mistake_record（V1 错题卡；2026-09-08 补 DDL——主库早期手工建，schema.py 未同步）
CREATE TABLE IF NOT EXISTS mistake_record (
    id                      INTEGER PRIMARY KEY,
    user_id                 INTEGER NOT NULL,
    kp_id                   INTEGER NOT NULL,
    root_cause_kp_id        INTEGER,
    question_id             INTEGER,
    occurred_at             TEXT    NOT NULL,
    subject                 TEXT    NOT NULL,
    source                  TEXT    NOT NULL DEFAULT 'REAL_WORK',
    source_ref              TEXT,
    error_type              TEXT,
    error_subtype           TEXT,
    attributed_by           TEXT    NOT NULL DEFAULT 'RULE_BASED',
    attribution_confidence  REAL    NOT NULL DEFAULT 0.0,
    severity                INTEGER NOT NULL DEFAULT 3,
    needs_reteach           INTEGER NOT NULL DEFAULT 0,
    input_mode              TEXT,
    wrong_answer            TEXT,
    correct_answer          TEXT,
    resolved_at             TEXT,
    reappear_count          INTEGER NOT NULL DEFAULT 0,
    last_reappear_at        TEXT,
    is_tamed                INTEGER NOT NULL DEFAULT 0,
    note_id                 TEXT,
    img_original            TEXT,
    img_clean               TEXT,
    fsrs_s                  REAL,    -- A2 FSRS 状态（升级后启用）：当前稳定性 S
    fsrs_d                  REAL,    -- A2 FSRS 状态（升级后启用）：当前难度 D
    diagnosis_type          TEXT    NOT NULL DEFAULT 'CALC',  -- 2026-09-09 P0 · PPT P12 4 类错因：CONCEPT/CALC/MISREAD/NORM
    created_at              TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at              TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    UNIQUE (user_id, note_id)  -- 幂等键：每卡一条（kp_id 可为 0 占位待挂载）
);
CREATE INDEX IF NOT EXISTS idx_mistake_user_kp
    ON mistake_record (user_id, kp_id);
CREATE INDEX IF NOT EXISTS idx_mistake_note
    ON mistake_record (note_id);

-- 兼容老库（2026-09-08 A3→A2 升级）：mistake_record 加 fsrs_s/fsrs_d 列
-- 由 ensure_fsrs_columns() 在 create_schema() 末尾执行（PRAGMA 检测已存在则跳过）

-- daily_metric（日度聚合；驾驶舱日均时长等卡数据来源）
CREATE TABLE IF NOT EXISTS daily_metric (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER NOT NULL,
    metric_date         TEXT    NOT NULL,
    session_count       INTEGER NOT NULL DEFAULT 0,
    self_started_count  INTEGER NOT NULL DEFAULT 0,
    answer_count        INTEGER NOT NULL DEFAULT 0,
    correct_count       INTEGER NOT NULL DEFAULT 0,
    pass_rate           REAL,
    new_learned         INTEGER NOT NULL DEFAULT 0,
    today_reviewed_answers INTEGER NOT NULL DEFAULT 0,  -- 当日 REVIEW 作答数（原名 reviews_done，2026-09-08 P23 改名消歧义）
    active_minutes      REAL    NOT NULL DEFAULT 0.0,
    valid_day           INTEGER NOT NULL DEFAULT 0,
    xp_earned           INTEGER NOT NULL DEFAULT 0,
    kp_learning         INTEGER NOT NULL DEFAULT 0,
    kp_reviewing        INTEGER NOT NULL DEFAULT 0,
    kp_struggling       INTEGER NOT NULL DEFAULT 0,
    kp_mastered         INTEGER NOT NULL DEFAULT 0,
    kp_consolidated     INTEGER NOT NULL DEFAULT 0,
    kp_regressed        INTEGER NOT NULL DEFAULT 0,
    kp_archived         INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at          TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    UNIQUE (user_id, metric_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_metric_user_date
    ON daily_metric (user_id, metric_date);

-- kp_state_transition（KP 状态机跃迁记录；驯服判定可审计追溯）
CREATE TABLE IF NOT EXISTS kp_state_transition (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER NOT NULL,
    kp_id               INTEGER NOT NULL,
    from_status         TEXT    NOT NULL,
    to_status           TEXT    NOT NULL,
    rule                TEXT    NOT NULL,
    is_upgrade          INTEGER NOT NULL,
    triggered_by        TEXT    NOT NULL,
    conditions_snapshot TEXT    NOT NULL,
    created_at          TEXT    NOT NULL DEFAULT {_TS_DEFAULT}
);

-- report_meta（报告级元数据；如 followups_rephoto / silent_period 等状态）
CREATE TABLE IF NOT EXISTS report_meta (
    key        TEXT PRIMARY KEY,
    status     TEXT,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- weekly_metric（周度聚合；驾驶舱 NMKP / WAD 等卡数据来源）
CREATE TABLE IF NOT EXISTS weekly_metric (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER NOT NULL,
    week_start          TEXT    NOT NULL,
    nmkp                INTEGER NOT NULL DEFAULT 0,
    nmkp_mde_flag       TEXT    NOT NULL DEFAULT 'FLAT',
    sir_avg             REAL    NOT NULL DEFAULT 0.0,
    wad_days            INTEGER NOT NULL DEFAULT 0,
    pass_rate_avg       REAL    NOT NULL DEFAULT 0.0,
    active_minutes_sum  REAL    NOT NULL DEFAULT 0.0,
    silent_period       INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at          TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    UNIQUE (user_id, week_start)
);
CREATE INDEX IF NOT EXISTS idx_weekly_metric_user_week
    ON weekly_metric (user_id, week_start);

-- attribution_taxonomy（归因类别动态表；2026-09-12 新增）
-- 为什么：原先 MONSTERS / DIAG_WEIGHT 是代码常量，类别无法"持续细化与演进"。
-- 改为数据行驱动：树形（parent_code）+ 版本（version）+ 可停用（status），
-- 使归因类别随分析逻辑精细化而演进，无需改代码。
CREATE TABLE IF NOT EXISTS attribution_taxonomy (
    id           INTEGER PRIMARY KEY,
    code         TEXT    NOT NULL,               -- 唯一类别 code（如 CONCEPT / CONCEPT.FRACTION_MEANING）
    label        TEXT    NOT NULL,               -- 中文展示名（如 概念不清）
    parent_code  TEXT,                           -- 父类别 code（NULL = 顶层）
    weight       REAL    NOT NULL DEFAULT 0.0,   -- 综合优先级权重（替代 DIAG_WEIGHT 硬编码）
    version      INTEGER NOT NULL DEFAULT 1,     -- 类别定义版本（细化 / 改权重时 +1）
    status       TEXT    NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE / DEPRECATED
    is_seed      INTEGER NOT NULL DEFAULT 0,     -- 1 = 由 MONSTERS v1 种子迁入
    description  TEXT,
    created_at   TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at   TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    UNIQUE (code)
);
CREATE INDEX IF NOT EXISTS idx_attr_taxonomy_status
    ON attribution_taxonomy (status, parent_code);

-- attribution_proposal（LLM 归因提案 + 完整审计链；2026-09-12 新增）
-- 为什么：LLM 判断不能无痕地覆盖权威字段。先落提案，经闸门放行才写
-- mistake_record.diagnosis_type；每条提案可回溯到 (feedback_id, model, prompt_version, rationale)。
CREATE TABLE IF NOT EXISTS attribution_proposal (
    id                INTEGER PRIMARY KEY,
    mistake_id        INTEGER NOT NULL REFERENCES mistake_record(id),
    schedule_id       INTEGER          REFERENCES review_schedule(id),
    feedback_id       INTEGER          REFERENCES review_feedback(id),
    evidence_source   TEXT    NOT NULL,          -- AUDIO_ASR / TEXT_NOTE / IMAGE_VLM / EXISTING_META
    evidence_digest   TEXT,                      -- 送进 LLM 的证据摘要（截断存储）
    model             TEXT,                      -- 模型名（如 MiniMax-M3）
    prompt_version    TEXT,                      -- 提示词版本（可迭代）
    proposed_code     TEXT,                      -- LLM 建议的既有类别 code
    proposed_new_code TEXT,                      -- 建议的"全新类别"code（非空 → 走候选，绝不自动新增）
    confidence        REAL    NOT NULL DEFAULT 0.0,
    rationale         TEXT,                      -- LLM 给出的理由（人类可读，用于复核）
    alt_json          TEXT,                      -- 其他候选 JSON 数组（元素含 code / confidence）
    source_layer      TEXT,                      -- L2.A / L1-C / L1-A / L0（降级链来源）
    status            TEXT    NOT NULL DEFAULT 'PROPOSED',  -- PROPOSED / AUTO_ACCEPTED / ACCEPTED / REJECTED / SUPERSEDED
    decided_by        TEXT,                      -- LLM_ASSISTED / PARENT / FRANK
    decided_at        TEXT,
    created_at        TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at        TEXT    NOT NULL DEFAULT {_TS_DEFAULT}
);
CREATE INDEX IF NOT EXISTS idx_attr_proposal_mistake
    ON attribution_proposal (mistake_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attr_proposal_status
    ON attribution_proposal (status, created_at);

-- taxonomy_candidate（LLM 提议的新类别候选；2026-09-12 新增）
-- 为什么（King 2026-09-12 拍板）：类别演进必须"LLM 提候选 + 人工确认晋级"，
-- 不允许自动新增，否则类别会失控膨胀 / 语义重叠。
CREATE TABLE IF NOT EXISTS taxonomy_candidate (
    id               INTEGER PRIMARY KEY,
    candidate_code   TEXT    NOT NULL,
    candidate_label  TEXT    NOT NULL,
    parent_code      TEXT,
    rationale        TEXT,
    evidence_count   INTEGER NOT NULL DEFAULT 1,  -- 支撑该候选的提案数
    sample_json      TEXT,                         -- 样本（mistake_id / proposal id）JSON
    status           TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING / PROMOTED / REJECTED
    promoted_code    TEXT,
    decided_by       TEXT,
    decided_at       TEXT,
    created_at       TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at       TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    UNIQUE (candidate_code)
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_candidate_status
    ON taxonomy_candidate (status, created_at);

-- kp_match_proposal（LLM 知识点挂载提案 + 审计链；2026-09-12 新增）
-- 为什么：错题挂载知识点此前依赖人工（且 confirm 端点硬编码 kp_id=0），
-- 挂载率长期 0%。改为 LLM 读题面从 knowledge_point 提候选 + 双跑一致闸门，
-- 与 attribution_proposal 同款审计链（哪次分析 / 哪个模型 / 什么理由）。
CREATE TABLE IF NOT EXISTS kp_match_proposal (
    id               INTEGER PRIMARY KEY,
    mistake_id       INTEGER NOT NULL REFERENCES mistake_record(id),
    proposed_kp_id   INTEGER          REFERENCES knowledge_point(id),  -- 匹配到的 KP（落库校验后回填）
    proposed_kp_code TEXT,             -- LLM 输出的 code（校验用）
    confidence       REAL    NOT NULL DEFAULT 0.0,
    rationale        TEXT,             -- 为什么是这个知识点（引用题面证据）
    source_layer     TEXT,             -- L2.A / L1-C / L1-A / L0
    model            TEXT,
    prompt_version   TEXT,
    confirm_json     TEXT,             -- 双跑审计（policy / runs / agreed）
    status           TEXT    NOT NULL DEFAULT 'PROPOSED',  -- PROPOSED/AUTO_ACCEPTED/ACCEPTED/REJECTED/SUPERSEDED
    decided_by       TEXT,
    decided_at       TEXT,
    created_at       TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at       TEXT    NOT NULL DEFAULT {_TS_DEFAULT}
);
CREATE INDEX IF NOT EXISTS idx_kp_match_mistake
    ON kp_match_proposal (mistake_id, created_at);
CREATE INDEX IF NOT EXISTS idx_kp_match_status
    ON kp_match_proposal (status, created_at);

-- kp_candidate（LLM 提议的新知识点候选；2026-09-12 新增）
-- King 政策：与 taxonomy_candidate 同款——LLM 提候选 + 人工确认晋级，
-- 绝不自动新增知识点（否则知识树会失控膨胀）。
CREATE TABLE IF NOT EXISTS kp_candidate (
    id              INTEGER PRIMARY KEY,
    candidate_code  TEXT    NOT NULL,   -- 如 MATH-G4-FRACTION-MEANING
    candidate_name  TEXT    NOT NULL,
    parent_code     TEXT,               -- 建议挂载的父节点 code
    kp_type         TEXT,               -- CONCEPT / SKILL / FACT
    rationale       TEXT,               -- 为什么现有 27 个 KP 覆盖不了
    evidence_count  INTEGER NOT NULL DEFAULT 1,
    sample_json     TEXT,               -- 支撑样本（mistake_id 列表）
    status          TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING / PROMOTED / REJECTED
    promoted_kp_id  INTEGER REFERENCES knowledge_point(id),
    decided_by      TEXT,
    decided_at      TEXT,
    created_at      TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at      TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    UNIQUE (candidate_code)
);
CREATE INDEX IF NOT EXISTS idx_kp_candidate_status
    ON kp_candidate (status, created_at);

-- kp_relation（知识点关联边，2026-09-12 Phase 1 新增）
-- 🔴 为什么与 kp_prerequisite 分表（关键设计决策）：
--   kp_prerequisite 语义专属"学习先决"，且被 notes/kp_gate.py 用作**门禁**
--   （HARD 先决未达 LEARNING 不得进入新学候选）；跨学科"关联"（类比/应用/
--   共享方法/对比）不是先决关系，混入会污染门禁语义 → 单独建表。
-- 方向约定：跨学科关联**无向**——写入时规范化 from_kp_id < to_kp_id，
--   并加 CHECK 约束防回归（查询无需 OR 两侧）。
-- relation_type：ANALOGY 类比 / APPLICATION 应用 / SHARED_METHOD 共享方法 / CONTRAST 对比
-- source：LLM（自动推断）/ HUMAN（人工确认）
CREATE TABLE IF NOT EXISTS kp_relation (
    id            INTEGER PRIMARY KEY,
    from_kp_id    INTEGER NOT NULL REFERENCES knowledge_point(id),
    to_kp_id      INTEGER NOT NULL REFERENCES knowledge_point(id),
    relation_type TEXT    NOT NULL,
    note          TEXT,
    confidence    REAL,
    source        TEXT,
    created_at    TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    updated_at    TEXT    NOT NULL DEFAULT {_TS_DEFAULT},
    UNIQUE (from_kp_id, to_kp_id, relation_type),
    CHECK (from_kp_id < to_kp_id)
);
CREATE INDEX IF NOT EXISTS idx_kp_relation_from ON kp_relation (from_kp_id);
CREATE INDEX IF NOT EXISTS idx_kp_relation_to   ON kp_relation (to_kp_id);
CREATE INDEX IF NOT EXISTS idx_kp_relation_type ON kp_relation (relation_type);

-- kp_candidate 复核字段（2026-09-12 新增：今日复习页"待确认候选"处理）
-- 背景：King 要求在复习页逐条处理候选（同意/驳回），驳回须填原因并交 LLM 二审。
-- 用 ALTER 兼容老库（ensure_kp_candidate_review_columns 幂等执行）：
--   suggestion / suggestion_reason / reject_reason / llm_review_json / llm_reviewed_at
-- schema 版本追踪表（由 fdl_core.migrations.runner 维护，幂等）：
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT {_TS_DEFAULT}
);
"""

# 建表顺序表（供验收/迁移/测试引用，与 SQL 顺序一致）
TABLE_NAMES: tuple[str, ...] = (
    "subject",
    "knowledge_point",
    "kp_prerequisite",
    "question",
    "mistake_record",  # V1 错题卡（2026-09-08 补 DDL）
    "review_schedule",
    "review_feedback",  # V12.7 复习反馈表（含附件路径 JSON）
    "intervention_action",  # OpenMAIC 借鉴 B：干预动作表（错因诊断/反馈闭环）
    "daily_task",
    "study_session",
    "answer_log",
    "kp_state",
    "daily_metric",  # 日度聚合（驾驶舱日均时长等）
    "weekly_metric",  # 周度聚合（NMKP/WAD）
    "kp_state_transition",  # KP 状态机跃迁记录
    "report_meta",  # 报告元数据（followups_rephoto 等）
    "attribution_taxonomy",  # 归因类别动态表（2026-09-12：类别数据行驱动，可演进）
    "attribution_proposal",  # LLM 归因提案 + 审计链（2026-09-12）
    "taxonomy_candidate",  # 新类别候选（LLM 提候选 + 人工确认晋级，2026-09-12）
    "kp_match_proposal",  # LLM 知识点挂载提案（2026-09-12 Phase 1）
    "kp_candidate",  # 新知识点候选（LLM 提候选 + 人工确认晋级）
    "kp_relation",  # 知识点关联边：跨学科类比/应用（Phase 1，与门禁用的 kp_prerequisite 分表）
    "exam_record",
    "schema_migrations",  # schema 版本追踪（fdl_core.migrations.runner 维护）
)


def create_schema(conn: sqlite3.Connection) -> None:
    """在给定连接上执行全部建表 DDL（幂等：IF NOT EXISTS）。"""
    conn.executescript(_SCHEMA_SQL)
    # 老库兼容：mistake_record 加 fsrs_s/fsrs_d 列（2026-09-08 A3→A2 升级）
    ensure_fsrs_columns(conn)
    # 老库兼容：kp_candidate 加复核字段（2026-09-12 复习页候选处理）
    ensure_kp_candidate_review_columns(conn)
    conn.commit()


def ensure_kp_candidate_review_columns(conn: sqlite3.Connection) -> None:
    """为老库 kp_candidate 补复核字段（PRAGMA 检测已存在则跳过）。

    字段用途（2026-09-12 King 需求"复习页待确认候选处理"）：
    - suggestion / suggestion_reason：系统建议（LLM 去重分析或规则判定）
    - reject_reason：用户驳回原因
    - llm_review_json / llm_reviewed_at：LLM 二审结论与时间
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(kp_candidate)").fetchall()}
    if not cols:
        return  # 表不存在（未跑 create_schema）
    for name, ddl in (
        ("suggestion", "TEXT"),
        ("suggestion_reason", "TEXT"),
        ("reject_reason", "TEXT"),
        ("llm_review_json", "TEXT"),
        ("llm_reviewed_at", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE kp_candidate ADD COLUMN {name} {ddl}")


def ensure_fsrs_columns(conn: sqlite3.Connection) -> None:
    """为老库 mistake_record 补 fsrs_s/fsrs_d 列（PRAGMA 检测已存在则跳过）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mistake_record)").fetchall()}
    if "fsrs_s" not in cols:
        conn.execute("ALTER TABLE mistake_record ADD COLUMN fsrs_s REAL")
    if "fsrs_d" not in cols:
        conn.execute("ALTER TABLE mistake_record ADD COLUMN fsrs_d REAL")


def get_connection(
    db_path: str | Path,
    *,
    foreign_keys: bool = True,
    wal: bool = True,
) -> sqlite3.Connection:
    """建立 SQLite 连接并启用关键 PRAGMA。

    - `foreign_keys`：显式启用外键约束（SQLite 默认关闭）。
    - `wal`：WAL 日志模式，降低写锁冲突（配合 P2-29 备份策略）。
    """
    conn = sqlite3.connect(str(db_path))
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys = ON")
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def table_names(conn: sqlite3.Connection) -> list[str]:
    """返回当前库中实际存在的 FDL 表名（用于验收/诊断）。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def ensure_session_role_column(conn) -> None:
    """D-3 老库迁移：study_session 补 session_role 列（已存在则跳过）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(study_session)").fetchall()]
    if cols and "session_role" not in cols:
        conn.execute(
            "ALTER TABLE study_session ADD COLUMN session_role TEXT NOT NULL DEFAULT 'SELF'"
        )
        conn.commit()
