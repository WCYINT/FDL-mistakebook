"""V1 报告数据层 + 渲染验收测试。"""

from __future__ import annotations

import re
import sqlite3

import pytest

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
    conn.commit()
    yield conn
    conn.close()


def test_collect_metrics_12_cockpit_items(db):
    """验收 2：驾驶舱 12 项全覆盖（含 2026-09-08 新增"今日复习分钟"卡）。"""
    d = collect_metrics(db)
    assert len(d["cockpit"]) == 12
    names = [c["name"] for c in d["cockpit"]]
    for expect in (
        "周新知识点录入",
        "归因覆盖率",
        "今日复习分钟",
        "复习队列积压",
        "WAD 周活跃天数",
        "SIR 自主发起率",
        "日均时长",
        "复习通过率 RPR",
        "平均 R@R",
        "MCD 信心区分度",
        "主动放弃次数",
        "周追问数 QPW",
    ):
        assert expect in names


def test_collect_metrics_sparse_data_shows_na(db):
    """数据稀疏期显示「—」（不做推测）。"""
    d = collect_metrics(db)
    na_items = [c for c in d["cockpit"] if c["display"] == "—"]
    assert len(na_items) >= 4  # SIR/时长/RPR/R@R/MCD 等


def test_collect_metrics_mistake_backlog(db):
    db.execute(
        "INSERT INTO mistake_record (id, user_id, kp_id, occurred_at, subject, source,"
        " error_type, attributed_by, severity, is_tamed, note_id)"
        " VALUES (77001, 1, 0, '2026-09-03T20:00:00Z', 'MATH', 'REAL_WORK',"
        " 'METHOD', 'RULE_BASED', 3, 0, '77001')"
    )
    db.commit()
    d = collect_metrics(db)
    mon = {m["key"]: m["count"] for m in d["monsters"]}
    assert mon["METHOD"] == 1
    assert d["ops"]["mistake_active"] == 1


def test_redlines_three_with_action(db):
    d = collect_metrics(db)
    assert len(d["redlines"]) == 3
    for r in d["redlines"]:
        assert r["action"]  # 每条带首选动作


def test_render_html_no_external_links(db):
    """验收 7：零外链（file:// 离线）。

    2026-09-09 豁免 http://127.0.0.1：本地分析服务 API（fdl_serve.py）是运行时
    可选调用（非资源加载），file:// 下 fetch 失败也有降级 UI，不破坏离线性。
    """
    html = render_html(collect_metrics(db))
    cleaned = html.replace("http://www.w3.org", "").replace("http://127.0.0.1", "")
    assert not re.search(r"https?://", cleaned), (
        "HTML 含外部 http(s) 链接（w3.org 命名空间与 127.0.0.1 本地 API 除外）"
    )
    assert "__REPORT_DATA__" not in html  # 数据已注入


def test_render_html_has_logo_and_tabs(db):
    html = render_html(collect_metrics(db))
    assert "FDL" in html and "学习点灯人" in html  # logo 字标
    for tab in ("驾驶舱", "学习星图", "探险足迹", "错题图鉴", "记忆能量", "家长视图"):
        assert tab in html


def test_main_generates_html(db, tmp_path):
    """端到端：main() 产出可写 HTML 文件。"""
    out = tmp_path / "report.html"
    # main() 用默认 data/fdl.db——这里直接渲染验证
    html = render_html(collect_metrics(db))
    out.write_text(html, encoding="utf-8")
    assert out.exists() and len(out.read_text(encoding="utf-8")) > 5000


def test_main_syncs_report_latest_alias(db, tmp_path, monkeypatch):
    """report_latest.html 别名自动同步（2026-09-13 修复）。

    历史遗留：所有自动化只写 site/report.html，而查看侧习惯打开
    site/report_latest.html（手工副本）→ 两边长期错位。main() 写标准输出后
    同步别名（两文件名永远同内容）；显式 out（测试/预览）不触发同步，
    避免污染真实 site/ 目录。
    """
    from scripts import generate_report as gr

    site = tmp_path / "site"
    monkeypatch.setattr(gr, "OUT_DIR", site)
    db_path = str(tmp_path / "t.db")

    out = gr.main(db_path)
    latest = site / "report_latest.html"
    assert out == site / "report.html" and out.exists()
    assert latest.exists() and latest.read_bytes() == out.read_bytes()

    # 显式 out → 不触碰别名
    site2 = tmp_path / "site2"
    monkeypatch.setattr(gr, "OUT_DIR", site2)
    gr.main(db_path, out=site2 / "preview.html")
    assert not (site2 / "report_latest.html").exists()


def test_no_emoji_in_output(db):
    """P0-1：输出无 emoji 功能图标。"""
    html = render_html(collect_metrics(db))
    emoji_re = re.compile("[\U0001f300-\U0001f9ff\U00002600-\U000026ff\U00002700-\U000027bf]")
    assert not emoji_re.search(html)
