"""「待人工处理」规则单测（2026-09-12 King 规则）。

规则：**所有待人工处理问题必须分别呈现在驾驶舱或今日复习页面。**
本测试锁死三件事：
1. 登记表每一项都有 page（dashboard/review）+ block_id（前端渲染位）；
2. 聚合结果按 page 正确分流、total = 各项之和；
3. 报告模板含全部 block_id 的渲染代码（防止"登记了但没渲染"）。
"""

from __future__ import annotations

import pytest

from fdl_core.db.schema import create_schema, get_connection
from fdl_core.paths import get_paths
from fdl_core.report.pending_human import PENDING_SOURCES, collect_pending_human


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "ph.db"
    conn = get_connection(str(p))
    create_schema(conn)
    conn.execute(
        "INSERT INTO subject (id, user_id, code, name, color_hex, rotation_weight,"
        " grade_start, grade_end, sort_order) VALUES (1, 1, 'MATH', '数学',"
        " '#000', 0.35, 4, 9, 1)"
    )
    conn.commit()
    yield conn
    conn.close()


# ── 1. 登记表完整性（规则核心）───────────────────────────────
def test_every_source_has_render_slot():
    """每个待人工来源必须声明 page 与 block_id（否则界面看不到 → 违反规则）。"""
    for src in PENDING_SOURCES:
        assert src.get("page") in ("dashboard", "review"), f"{src['key']} 缺 page 声明"
        assert src.get("block_id"), f"{src['key']} 缺 block_id（前端渲染位）"
        assert src.get("label") and src.get("action") and src.get("hint")


def test_template_renders_all_block_ids():
    """模板必须包含所有登记来源的渲染位（block_id 出现在 HTML/JS 中）。"""
    tpl = (get_paths().fdl_root / "fdl_core" / "report" / "template.html").read_text(
        encoding="utf-8"
    )
    for src in PENDING_SOURCES:
        assert src["block_id"] in tpl, (
            f"{src['key']} 的渲染位 {src['block_id']} 未出现在模板中（违反规则）"
        )


# ── 2. 聚合正确性 ────────────────────────────────────────────
def test_collect_empty_all_zero(db):
    r = collect_pending_human(db)
    assert r["total"] == 0
    assert len(r["all"]) == len(PENDING_SOURCES)
    # 即使全 0，每一项也要有结构（前端按结构渲染）
    for e in r["all"]:
        assert e["count"] == 0 and e["page"] in ("dashboard", "review")


def test_collect_counts_kp_proposal(db):
    """写入一条 PROPOSED 挂载提案 → 聚合计数 +1，分流到 review 页。"""
    db.execute(
        "INSERT INTO mistake_record (id, user_id, kp_id, occurred_at, subject,"
        " source, error_type, attributed_by, severity, is_tamed, note_id)"
        " VALUES (77099, 1, 0, '2026-09-05T20:00:00Z', 'MATH', 'REAL_WORK',"
        " 'METHOD', 'RULE_BASED', 3, 0, '77099')"
    )
    db.execute(
        "INSERT INTO kp_match_proposal (mistake_id, proposed_kp_code, confidence,"
        " status) VALUES (77099, 'MATH-G4-MUL-COMM', 0.9, 'PROPOSED')"
    )
    db.commit()
    r = collect_pending_human(db)
    # 只有 kp_match_proposal 有 1 条（其余来源均为 0）
    kp = next(e for e in r["review"] if e["key"] == "kp_mount_proposal")
    assert kp["count"] == 1
    assert kp["samples"][0]["summary"] == "MATH-G4-MUL-COMM"
    assert kp["samples"][0]["mistake_id"] == 77099
    # dashboard 侧不应出现它
    assert all(e["key"] != "kp_mount_proposal" for e in r["dashboard"])


def test_collect_counts_intervention(db):
    """干预动作 PENDING → 分流到 dashboard 页。"""
    db.execute(
        "INSERT INTO intervention_action (user_id, subject_id, trigger, action_type,"
        " status) VALUES (1, 1, 'FIRST_WRONG', 'RETEACH', 'PENDING')"
    )
    db.commit()
    r = collect_pending_human(db)
    iv = next(e for e in r["dashboard"] if e["key"] == "intervention_pending")
    assert iv["count"] == 1
    assert all(e["key"] != "intervention_pending" for e in r["review"])


def test_collect_page_partition_is_exhaustive(db):
    """dashboard + review 的并集 == all（不得有任何来源两边都不出现）。"""
    r = collect_pending_human(db)
    ids = {e["key"] for e in r["dashboard"]} | {e["key"] for e in r["review"]}
    assert ids == {e["key"] for e in r["all"]}


# ── 3. 报告 payload 注入 ────────────────────────────────────
def test_report_payload_has_pending_human(db):
    """collect_metrics 必须注入 pending_human（不注入 = 界面无数据）。"""
    from scripts.generate_report import collect_metrics

    d = collect_metrics(db, report_date="2026-09-05")
    assert "pending_human" in d
    ph = d["pending_human"]
    assert "all" in ph and "dashboard" in ph and "review" in ph
    assert len(ph["all"]) == len(PENDING_SOURCES)
