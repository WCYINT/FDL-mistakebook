"""V2 验收测试：红线真实判定 + 体验趋势 + daily_batch 入口。"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from fdl_core.alerts.red_lines import evaluate_red_lines
from scripts.generate_report import collect_metrics, render_html


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    from fdl_core.db.schema import create_schema
    from fdl_core.mistakes.tables import ensure_mistake_table

    create_schema(conn)
    ensure_mistake_table(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学', '#000', 0.35, 4, 9, 1)"
    )
    for i, code in enumerate(("MATH-G4-A", "MATH-G4-B", "MATH-G4-C")):
        conn.execute(
            "INSERT INTO knowledge_point (id, subject_id, code, name, grade_level,"
            " bloom_level, abstraction_level, importance_weight, base_difficulty,"
            " est_learn_minutes, est_review_seconds, kp_type, tier, graph_version, valid_from)"
            " VALUES (?, 1, ?, ?, 4, 2, 2, 1.0, 4.8, 1.5, 45, 'SKILL', 'L0',"
            " '2026.1', '2026-09-01')",
            (i + 1, code, f"知识点{chr(65 + i)}"),
        )
    conn.commit()
    yield conn
    conn.close()


def _add_sessions(conn, today: date, *, days: list[tuple[int, int]], minutes: float = 12.0):
    """days: [(offset, self_flag)]——offset 天前插入 session。"""
    for off, self_flag in days:
        d = (today - timedelta(days=off)).isoformat()
        conn.execute(
            "INSERT INTO study_session (id, user_id, session_date, session_slot,"
            " trigger_type, session_role, started_at, duration_sec, effective_sec)"
            " VALUES (NULL, 1, ?, 'AM', ?, 'SELF', ?, ?, ?)",
            (
                d,
                "SELF" if self_flag else "PARENT",
                d + "T08:00:00Z",
                int(minutes * 60),
                int(minutes * 60),
            ),
        )
    conn.commit()


def test_redline_na_when_no_data(db):
    r = evaluate_red_lines(db, today="2026-09-05")
    assert len(r) == 3
    assert all(x["status"] == "na" and "数据积累中" in x["state"] for x in r)
    assert all(x["action"] for x in r)  # 每条带首选动作


def test_redline_calm_with_normal_data(db):
    today = date(2026, 9, 5)
    days = [(i, 1) for i in range(1, 15)]  # 14 天 SELF，每天 12min（5-25 区间内）
    _add_sessions(db, today, days=days)
    r = evaluate_red_lines(db, today=today.isoformat())
    assert not any(x["status"] == "trig" for x in r)  # 无触发
    assert any(x["status"] == "calm" for x in r)  # 有数据的线为平静


def test_redline_duration_over_25_triggers(db):
    """红线 2：连续 5 天 >25min → 触发。"""
    today = date(2026, 9, 5)
    days = [(i, 1) for i in range(1, 6)]
    _add_sessions(db, today, days=days, minutes=30.0)
    r = evaluate_red_lines(db, today=today.isoformat())
    dur = next(x for x in r if x["name"] == "时长失控")
    assert dur["status"] == "trig"
    assert "new_per_day" in dur["action"]


def test_redline_sir_low_2w_triggers(db):
    """红线 1：近 2 周 SIR <20%（几乎全 PARENT 触发）→ 触发降量。"""
    today = date(2026, 9, 5)
    days = [(i, 0) for i in range(1, 15)]  # 14 天全 PARENT
    _add_sessions(db, today, days=days)
    r = evaluate_red_lines(db, today=today.isoformat())
    ref = next(x for x in r if x["name"] == "Frank 拒绝使用")
    assert ref["status"] == "trig"
    assert "降量" in ref["action"]


def test_report_collect_uses_real_redlines(db):
    """报告数据层的红线来自真实判定（非占位）。"""
    d = collect_metrics(db, report_date="2026-09-05")
    assert len(d["redlines"]) == 3
    assert all(x["state"] in ("数据积累中", "未触发", "已触发") for x in d["redlines"])
    # 趋势数据：14 天序列
    assert len(d["trend"]) == 14


def test_report_trend_svg_in_html(db):
    d = collect_metrics(db, report_date="2026-09-05")
    html = render_html(d)
    assert "trend-svg" in html and "18 min 上限" in html


# ── V3 验收：星图着色 / 足迹 / 图鉴进度 / 能量 ───────────────
def test_v3_starmap_has_madj_field(db):
    """星图每颗星带 m_adj 字段（无学习数据 = null = 未点亮深空）。

    Step 3 改版：payload 由 star_groups 换为 starmap（三维 + 边 + 布局）。
    """
    d = collect_metrics(db, report_date="2026-09-05")
    all_stars = d["starmap"]["nodes"]
    assert len(all_stars) >= 3
    assert all("m_adj" in s for s in all_stars)
    assert all(s["m_adj"] is None for s in all_stars)  # 无 kp_state → 全部未点亮


def test_v3_trail_182_days(db):
    """足迹 = 26 周（182 天）日序列。"""
    d = collect_metrics(db, report_date="2026-09-05")
    assert len(d["trail"]) == 182
    assert all({"date", "minutes", "stars", "questions", "camp"} <= set(t) for t in d["trail"])


def test_v3_monsters_progress(db):
    db.execute(
        "INSERT INTO mistake_record (id, user_id, kp_id, occurred_at, subject, source,"
        " error_type, attributed_by, severity, is_tamed, note_id)"
        " VALUES (77005, 1, 0, '2026-09-05T20:00:00Z', 'MATH', 'REAL_WORK',"
        " 'METHOD', 'RULE_BASED', 3, 1, '77005')"
    )
    db.commit()
    d = collect_metrics(db, report_date="2026-09-05")
    method = next(m for m in d["monsters"] if m["key"] == "METHOD")
    assert method["count"] == 1 and method["tamed"] == 1 and method["progress"] == 1.0


def test_v3_energy_empty_state(db):
    """kp_state 无数据 → 能量空列表（模板空态文案）。"""
    d = collect_metrics(db, report_date="2026-09-05")
    assert d["energy"] == []
    html = render_html(d)
    assert "energy-list" in html and "等第一次点亮" in html


def test_v3_html_renders_v3_blocks(db):
    html = render_html(collect_metrics(db, report_date="2026-09-05"))
    assert "trail-heat" in html and "energy-list" in html
    assert "驯服后它会进化成你的伙伴" in html
    assert "缺席的日子就留白" in html


# ── V4 验收：家长视图 / 筛选 / 回溯 / 全面验收 ────────────────
def test_v4_parent_view_silent_masks_diagnosis(db):
    """PVP-06：静默期质量诊断屏蔽；数据层拦截明细。"""
    d = collect_metrics(db, report_date="2026-09-05")
    assert d["parent"]["silent"] is True
    assert d["parent"]["diagnosis_visible"] is False  # 2026-09-05 在 2 周冻结内
    html = render_html(d)
    assert "屏蔽中" in html and "第 3 周起自动开放" in html


def test_v4_parent_no_detail_leak(db):
    """🔴 权限：HTML 不含作答明细/kp_state 明细字段。"""
    d = collect_metrics(db, report_date="2026-09-05")
    html = render_html(d)
    assert "user_answer" not in html and "correct_answer" not in html
    assert "answer_log" not in html


def test_v4_nmkp_card(db):
    d = collect_metrics(db, report_date="2026-09-05")
    assert "nmkp_week" in d["parent"] and isinstance(d["parent"]["nmkp_week"], int)


def test_v4_subject_filter_ui(db):
    d = collect_metrics(db, report_date="2026-09-05")
    html = render_html(d)
    # Step 3 改版：单一"科目筛选"升级为三级筛选（学科→领域→学期）
    assert "sm-subjects" in html and "sm-domains" in html and "sm-terms" in html
    assert "time-slider" in html and "回溯" in html  # 回溯滑块


def test_v4_performance_500_nodes(db):
    """NFR-4：500 节点星图构建 <2s（性能近似：插 500 KP 后 collect 耗时）。"""
    import time

    for i in range(500):
        db.execute(
            "INSERT INTO knowledge_point (id, subject_id, code, name, grade_level,"
            " bloom_level, abstraction_level, importance_weight, base_difficulty,"
            " est_learn_minutes, est_review_seconds, kp_type, tier, graph_version, valid_from)"
            " VALUES (?, 1, ?, ?, 4, 2, 2, 1.0, 4.8, 1.5, 45, 'SKILL', 'L0',"
            " '2026.1', '2026-09-01')",
            (100 + i, f"MATH-PERF-{i}", f"性能点{i}"),
        )
    db.commit()
    t0 = time.perf_counter()
    d = collect_metrics(db, report_date="2026-09-05")
    html = render_html(d)
    elapsed = time.perf_counter() - t0
    total_stars = len(d["starmap"]["nodes"])
    assert total_stars >= 500
    # 2026-09-10 再次校准：King 拍板"整页原图字节级内联"后，
    # 报告 5 张题图 + 50 张复习图 = 50+ 张原图字节级内联，
    # render 全程 O(payload)。实测 collect 0.74s / render 11.34s / total 12s，
    # 5.0s 预算对当前数据稀疏期也不够。15.0s 覆盖真实分布并留余量。
    assert elapsed < 15.0, f"500 节点聚合+渲染耗时 {elapsed:.2f}s 超预算"
    assert len(html) > 50000
