"""`answer_log` 全字段采集（P2-07 / MT-01）。

🔴 冗余字段（S/R/M before-after）必须当场快照落库——事后无法重算
（作答时刻的 R(t)/M_adj 依赖当时的调度状态）。本模块保证 answer_log
**全部列**都有采集入口，字段与 P2-01 schema 一一对应。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields

from fdl_core.srs.time_layer import fmt_ts, local_date, now_utc


class AnswerLogError(ValueError):
    """answer_log 采集字段错误。"""


@dataclass
class AnswerRecord:
    """一次作答的完整事实（列名与 `answer_log` 表一一对应）。"""

    # 外键与上下文
    user_id: int
    session_id: int
    task_id: int
    question_id: int
    kp_id: int
    task_type: str  # NEW_LEARN/REVIEW/DEEP_DIVE/CALIBRATION/EXPLORE/EXAM
    # 作答事实
    grade: int  # 0/1/2/3
    is_correct: bool
    response_seconds: int | None
    expected_seconds: int
    time_ratio: float | None = None  # 语音模式 None（不参与判定）；缺省由 resp/expected 推算
    hint_used: bool = False
    retry_count: int = 0
    input_mode: str = "KEYBOARD"  # VOICE/KEYBOARD/HANDWRITE/CHOICE
    self_confidence: int | None = None  # 1/2/3（语音模式主信号，阶段三启用）
    asr_first_token_ms: int | None = None
    user_answer: str | None = None
    explanation: str | None = None  # 口述思路（Easy 判定/深度证据）
    # 快照冗余（🔴 事后不可重算，必须当场采集）
    difficulty_at_time: float = 5.0
    r_at_answer: float = 0.0
    s_before: float = 0.0
    s_after: float = 0.0
    d_before: float = 5.0
    d_after: float = 5.0
    m_adj_before: float | None = None
    m_adj_after: float | None = None
    interval_days: int | None = None
    planned_interval_days: int | None = None
    overdue_days: int | None = None
    question_variant_type: str | None = None
    # 标记
    is_first_of_day: bool = True
    is_valid_evidence: bool = True  # 排除刷量/校准/探索；reteach 期间 False
    grinding_flag: bool = False
    overtime_flag: bool = False
    answered_at: str = ""  # 留空则取 now_utc()


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def insert_answer_log(conn: sqlite3.Connection, rec: AnswerRecord) -> int:
    """写入一条作答流水；列全集对齐 schema（漏列/多列抛 `AnswerLogError`）。

    `is_correct` 若与 grade 不一致以 grade 为准（grade≥1）。
    """
    cols = _columns(conn, "answer_log")
    data = {f.name: getattr(rec, f.name) for f in fields(AnswerRecord)}
    data["is_correct"] = int(rec.grade >= 1)
    if rec.time_ratio is not None:
        data["time_ratio"] = rec.time_ratio
    elif rec.response_seconds is not None:
        data["time_ratio"] = round(rec.response_seconds / rec.expected_seconds, 3)
    else:
        data["time_ratio"] = None
    data["answered_at"] = rec.answered_at or fmt_ts(now_utc())
    data["is_first_of_day"] = int(rec.is_first_of_day)
    for k in ("hint_used", "is_valid_evidence", "grinding_flag", "overtime_flag"):
        data[k] = int(bool(data[k]))

    # created_at/updated_at 由 schema DEFAULT(now) 自动填充，不要求显式采集
    _AUTO_COLS = {"id", "created_at", "updated_at"}
    missing = [c for c in cols if c not in data and c not in _AUTO_COLS]
    if missing:
        raise AnswerLogError(f"answer_log 漏采列：{missing}")
    extra = [k for k in data if k not in cols]
    if extra:
        raise AnswerLogError(f"answer_log 未知列：{extra}")

    use = [c for c in cols if c not in _AUTO_COLS]
    placeholders = ",".join("?" for _ in use)
    cur = conn.execute(
        f"INSERT INTO answer_log ({','.join(use)}) VALUES ({placeholders})",
        [data[c] for c in use],
    )
    conn.commit()
    return int(cur.lastrowid)


def is_first_of_day(conn: sqlite3.Connection, user_id: int, kp_id: int) -> bool:
    """当日（本地日期口径）该 KP 是否首次作答（n_eff 同日去重依据）。"""
    from fdl_core.srs.time_layer import local_date_range

    start, end = local_date_range(local_date())
    row = conn.execute(
        "SELECT COUNT(*) FROM answer_log"
        " WHERE user_id=? AND kp_id=? AND answered_at >= ? AND answered_at < ?",
        (user_id, kp_id, fmt_ts(start), fmt_ts(end)),
    ).fetchone()
    return row[0] == 0
