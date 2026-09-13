"""P1 五项修复的自动化回归测试（2026-09-13）。

覆盖：
- #1 提案重复采纳报错 → kp_matcher.accept_proposal 幂等成功 + 自愈权威挂载边
- #2 今日复习时间 → collect_metrics 的 today_review_min / today_review_source
- #4 已复习按复习日分块 → review_items 的 review_dates / last_review_date
- #5 趋势缺今日 → trend 合并 asr_by_date（asr_min 字段）

#3（上传后自动刷新报告，scripts/fdl_serve.py）不在本文件覆盖——由端到端验证负责。

约束：
- 只读源码，不改任何生产文件；全部走 tmp_path + create_schema（不碰生产库）。
- 所有会读真实文件系统的私有加载器一律 monkeypatch，测试不依赖 data/ 目录内容。
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from fdl_core.mistakes import kp_matcher as kpm

REPORT_DATE = "2026-09-13"


# ── 夹具 ────────────────────────────────────────────────────
@pytest.fixture
def db(tmp_path):
    """报告/KP 共用测试库：schema + subject + 2 个知识点。"""
    conn = sqlite3.connect(tmp_path / "flow.db")
    from fdl_core.db.schema import create_schema
    from fdl_core.mistakes.tables import ensure_mistake_table

    create_schema(conn)
    ensure_mistake_table(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000',"
        " 0.35, 4, 9, 1)"
    )
    for i, (code, name) in enumerate(
        [("MATH-G4-MUL-DIST", "乘法分配律"), ("MATH-G4-PRICE", "单价·数量·总价")], start=1
    ):
        conn.execute(
            "INSERT INTO knowledge_point (id, subject_id, parent_id, code, name,"
            " grade_level, bloom_level, abstraction_level, importance_weight,"
            " base_difficulty, est_learn_minutes, est_review_seconds, kp_type,"
            " tier, graph_version, valid_from)"
            " VALUES (?, 1, NULL, ?, ?, 4, 2, 1, 1.0, 4.0, 20.0, 120, 'CONCEPT',"
            " 'L0', 'g1', '2026-09-01')",
            (i, code, name),
        )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def isolate_fs(monkeypatch):
    """隔离所有读真实文件系统的私有加载器（默认无数据）。"""
    from scripts import generate_report as gr

    monkeypatch.setattr(gr, "_load_review_audio_analyses", lambda: [])
    monkeypatch.setattr(gr, "_load_audio_match_map", lambda: {})
    monkeypatch.setattr(gr, "_collect_needs_review", lambda: [])
    return monkeypatch


# ── 通用构造 ────────────────────────────────────────────────
def _add_mistake(conn, mid, *, kp_id=0, occurred_at="2026-09-03T20:00:00Z", last_reappear_at=None):
    conn.execute(
        "INSERT INTO mistake_record (id, user_id, kp_id, occurred_at, subject, source,"
        " error_type, attributed_by, severity, is_tamed, note_id, last_reappear_at)"
        " VALUES (?, 1, ?, ?, 'MATH', 'REAL_WORK', 'METHOD', 'RULE_BASED', 3, 0, ?, ?)",
        (mid, kp_id, occurred_at, f"n{mid}", last_reappear_at),
    )
    conn.commit()


def _add_schedule(conn, sid, mid, *, kp_id=None, status="PENDING", due_date="2026-09-05"):
    conn.execute(
        "INSERT INTO review_schedule (id, user_id, mistake_id, kp_id, subject_id,"
        " due_date, due_session, planned_interval_days, priority_score, est_seconds,"
        " status, source) VALUES (?, 1, ?, ?, 1, ?, 'PM', 1, 90.0, 120, ?, 'answer')",
        (sid, mid, kp_id, due_date, status),
    )
    conn.commit()


def _add_feedback(conn, fid, schedule_id, created_at, *, kp_id=1):
    """created_at 用 SQLite CURRENT_TIMESTAMP 空格格式，语义 UTC。"""
    conn.execute(
        "INSERT INTO review_feedback (id, user_id, schedule_id, kp_id, subject_id,"
        " self_rating, created_at) VALUES (?, 1, ?, ?, 1, 3, ?)",
        (fid, schedule_id, kp_id, created_at),
    )
    conn.commit()


def _add_session(conn, session_date, effective_sec, *, sid=900):
    conn.execute(
        "INSERT INTO study_session (id, user_id, session_date, session_slot,"
        " trigger_type, session_role, started_at, ended_at, duration_sec, effective_sec)"
        " VALUES (?, 1, ?, 'PM', 'SELF', 'SELF', ?, ?, ?, ?)",
        (
            sid,
            session_date,
            f"{session_date}T19:30:00Z",
            f"{session_date}T20:00:00Z",
            effective_sec,
            effective_sec,
        ),
    )
    conn.commit()


def _asr_item(draft_name, duration_sec, *, created_at=None):
    return {
        "audio": draft_name.replace(".draft.json", ""),
        "audio_path": "",
        "duration_sec": duration_sec,
        "char_count": 0,
        "text_raw": "",
        "engine": "SenseVoice-Small",
        "analysis": None,
        "draft_name": draft_name,
        "created_at": created_at,
    }


def _patch_asr(monkeypatch, items):
    """打桩 ASR 草稿加载器；每次调用返回新副本，避免调用间互相污染。"""
    from scripts import generate_report as gr

    def fake():
        return [dict(it) for it in items]

    monkeypatch.setattr(gr, "_load_review_audio_analyses", fake)


def _add_proposal(conn, pid, mid, kp_id, code, *, status=kpm.STATUS_PROPOSED):
    conn.execute(
        "INSERT INTO kp_match_proposal (id, mistake_id, proposed_kp_id,"
        " proposed_kp_code, confidence, rationale, source_layer, status,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, 0.5, 'r', 'L2.A', ?,"
        " '2026-09-12T10:00:00Z', '2026-09-12T10:00:00Z')",
        (pid, mid, kp_id, code, status),
    )
    conn.commit()


# ════════════════════════════════════════════════════════════
# #1 提案重复采纳：幂等 + 自愈
# ════════════════════════════════════════════════════════════
def test_accept_proposal_proposed_then_accept(db):
    """#1a PROPOSED → 采纳：ok=True、状态 ACCEPTED、kp_id 写为提案值、计划回填。"""
    _add_mistake(db, 77001, kp_id=0)
    _add_schedule(db, 501, 77001, kp_id=None)
    _add_proposal(db, 11, 77001, 2, "MATH-G4-PRICE")

    res = kpm.accept_proposal(db, 11)
    assert res["ok"] is True
    assert res["written"]["mistake_updated"] is True
    assert res["written"]["schedules_backfilled"] == 1
    assert res["kp_id"] == 2

    status = db.execute("SELECT status FROM kp_match_proposal WHERE id=11").fetchone()[0]
    assert status == kpm.STATUS_ACCEPTED
    assert db.execute("SELECT kp_id FROM mistake_record WHERE id=77001").fetchone()[0] == 2
    # _write_authoritative_kp 同步回填悬空计划（防半挂载态）
    assert db.execute("SELECT kp_id FROM review_schedule WHERE id=501").fetchone()[0] == 2


def test_accept_proposal_twice_is_idempotent(db):
    """#1b 重复采纳同提案：ok=True + already=True，旧行为会报「提案已被采纳」。"""
    _add_mistake(db, 77001, kp_id=0)
    _add_proposal(db, 11, 77001, 2, "MATH-G4-PRICE")

    first = kpm.accept_proposal(db, 11)
    assert first["ok"] is True
    assert "already" not in first

    second = kpm.accept_proposal(db, 11)
    assert second["ok"] is True, "重复采纳必须幂等成功（本次修复的回归点）"
    assert second["already"] is True
    assert "error" not in second
    assert second["kp_id"] == 2


def test_accept_proposal_self_heals_drifted_kp(db):
    """#1c 状态 ACCEPTED 但 kp_id 被改跑偏 → 再采纳自愈回提案值。"""
    _add_mistake(db, 77001, kp_id=2)
    _add_proposal(db, 11, 77001, 2, "MATH-G4-PRICE", status=kpm.STATUS_ACCEPTED)

    # 人为把权威挂载边改到另一个知识点（模拟中断残留/人工误改）
    db.execute("UPDATE mistake_record SET kp_id=1 WHERE id=77001")
    db.commit()

    res = kpm.accept_proposal(db, 11)
    assert res["ok"] is True
    assert res["already"] is True
    assert res["self_healed"] is True
    assert db.execute("SELECT kp_id FROM mistake_record WHERE id=77001").fetchone()[0] == 2, (
        "自愈必须把 kp_id 恢复为提案值"
    )


def test_accept_proposal_auto_accepted_idempotent(db):
    """#1d AUTO_ACCEPTED 状态走同一幂等路径：已对齐 → already=True 且不自愈。"""
    _add_mistake(db, 77001, kp_id=1)
    _add_proposal(db, 11, 77001, 1, "MATH-G4-MUL-DIST", status=kpm.STATUS_AUTO)

    res = kpm.accept_proposal(db, 11)
    assert res["ok"] is True
    assert res["already"] is True
    assert res["self_healed"] is False
    assert "error" not in res


# ════════════════════════════════════════════════════════════
# #4 已复习按复习日分块
# ════════════════════════════════════════════════════════════
def test_review_dates_split_by_local_review_day(db, isolate_fs):
    """#4 review_dates 降序去重 + 本地日期（跨 UTC 边界）+ last_review_date 一致。"""
    _add_mistake(db, 77001)
    _add_mistake(db, 77002)  # 无反馈 → 两字段为空
    _add_schedule(db, 501, 77001, status="DONE", due_date="2026-09-11")
    _add_schedule(db, 502, 77001, status="DONE", due_date="2026-09-12")
    # SQLite CURRENT_TIMESTAMP 空格格式（语义 UTC）：
    #   '2026-09-11 20:30:00' UTC → 本地 2026-09-12 04:30
    #   '2026-09-12 20:30:00' UTC → 本地 2026-09-13 04:30
    # 若误用 UTC 切片会得到 09-11/09-12，本用例即可捕获。
    _add_feedback(db, 601, 501, "2026-09-11 20:30:00")
    _add_feedback(db, 602, 502, "2026-09-12 20:30:00")

    from scripts.generate_report import collect_metrics

    d = collect_metrics(db, report_date=REPORT_DATE)
    items = {it["id"]: it for it in d["review_all"]}

    assert items[77001]["review_dates"] == ["2026-09-13", "2026-09-12"]
    assert items[77001]["last_review_date"] == items[77001]["review_dates"][0]
    assert items[77002]["review_dates"] == []
    assert items[77002]["last_review_date"] is None


def test_review_dates_dedup_and_fallback(db, isolate_fs):
    """#4 同一本地日多条反馈去重；无 feedback 时用 last_reappear_at 兜底。"""
    _add_mistake(db, 77001, last_reappear_at="2026-09-12T20:30:00Z")
    _add_schedule(db, 501, 77001, status="DONE", due_date="2026-09-12")
    # 两条反馈落在同一本地日（UTC 09-12 10:00 / 20:30 → 本地均 09-12/09-13）
    _add_feedback(db, 601, 501, "2026-09-12 10:00:00")
    _add_feedback(db, 602, 501, "2026-09-12 20:30:00")
    _add_mistake(db, 77003, last_reappear_at="2026-09-12T20:30:00Z")

    from scripts.generate_report import collect_metrics

    d = collect_metrics(db, report_date=REPORT_DATE)
    items = {it["id"]: it for it in d["review_all"]}

    # 09-12 10:00 UTC → 本地 09-12；09-12 20:30 UTC → 本地 09-13 → 两天且去重
    assert items[77001]["review_dates"] == ["2026-09-13", "2026-09-12"]
    # 无 feedback：last_reappear_at 兜底（UTC → 本地 09-13）
    assert items[77003]["review_dates"] == ["2026-09-13"]
    assert items[77003]["last_review_date"] == "2026-09-13"


# ════════════════════════════════════════════════════════════
# #5 趋势合并 ASR（今日不再为 0）
# ════════════════════════════════════════════════════════════
def test_trend_includes_today_asr_minutes(db, isolate_fs):
    """#5 今日 trend 条目存在且 minutes 含 ASR、asr_min 字段正确。"""
    items = [_asr_item("0913复习1.draft.json", 300, created_at="2026-09-13T10:00:00Z")]
    _patch_asr(isolate_fs, items)

    from scripts.generate_report import collect_metrics

    d = collect_metrics(db, report_date=REPORT_DATE)
    trend = {t["date"]: t for t in d["trend"]}
    assert "09-13" in trend, "今日必须出现在 14 天趋势中"
    assert trend["09-13"]["asr_min"] == 5.0
    assert trend["09-13"]["minutes"] == 5.0, "无 study_session 时今日分钟=ASR 分钟"


def test_trend_today_session_plus_asr(db, isolate_fs):
    """#5 同日 study_session + ASR 相加（6 分钟会话 + 5 分钟录音 = 11）。"""
    _add_session(db, REPORT_DATE, effective_sec=360)  # 6.0 min
    items = [_asr_item("0913复习1.draft.json", 300, created_at="2026-09-13T10:00:00Z")]
    _patch_asr(isolate_fs, items)

    from scripts.generate_report import collect_metrics

    d = collect_metrics(db, report_date=REPORT_DATE)
    trend = {t["date"]: t for t in d["trend"]}
    assert trend["09-13"]["asr_min"] == 5.0
    assert trend["09-13"]["minutes"] == 11.0
    # 记录 ASR 的那天之外，asr_min 为 0（不串日）
    assert trend["09-12"]["asr_min"] == 0.0


# ════════════════════════════════════════════════════════════
# #2 今日复习分钟 = session + ASR
# ════════════════════════════════════════════════════════════
def test_today_review_min_sums_session_and_asr(db, isolate_fs):
    """#2 today_review_min == session_min + asr_min，且 source 三键数值一致。"""
    _add_session(db, REPORT_DATE, effective_sec=600)  # 10.0 min
    items = [_asr_item("0913复习1.draft.json", 300, created_at="2026-09-13T10:00:00Z")]
    _patch_asr(isolate_fs, items)

    from scripts.generate_report import collect_metrics

    d = collect_metrics(db, report_date=REPORT_DATE)
    assert d["today_review_source"] == {
        "session_min": 10.0,
        "asr_min": 5.0,
        "date": REPORT_DATE,
    }
    assert d["today_review_min"] == 15.0
    assert d["today_review_min"] == round(
        d["today_review_source"]["session_min"] + d["today_review_source"]["asr_min"], 1
    )


def test_today_review_min_zero_on_empty_day(db, isolate_fs):
    """#2 无数据日 == 0.0（ASR 记录在别的日子不串日）。"""
    items = [_asr_item("0913复习1.draft.json", 300, created_at="2026-09-13T10:00:00Z")]
    _patch_asr(isolate_fs, items)

    from scripts.generate_report import collect_metrics

    d = collect_metrics(db, report_date="2026-09-05")
    assert d["today_review_min"] == 0.0
    assert d["today_review_source"] == {
        "session_min": 0.0,
        "asr_min": 0.0,
        "date": "2026-09-05",
    }


def test_local_date_conversion_cross_utc_boundary():
    """#2/#4 公共前提：SQLite UTC 空格格式经 local_date_of_lenient 归本地日。"""
    from fdl_core.srs.time_layer import local_date_of_lenient

    # 20:30 UTC → 次日 04:30 本地（Asia/Shanghai）
    assert local_date_of_lenient("2026-09-12 20:30:00") == date(2026, 9, 13)
    # 10:00 UTC → 同日 18:00 本地
    assert local_date_of_lenient("2026-09-12 10:00:00") == date(2026, 9, 12)
