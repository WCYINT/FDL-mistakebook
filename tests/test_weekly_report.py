"""掌握度周报（P2-3）单测。

覆盖任务书要求的 6 项：
1. 空库不崩 + 四项指标均显示「—」+ HTML 能生成
2. 单条 review_feedback → 该错因复习次数=1
3. 上周 avg=2 / 本周 avg=4 → 判定「上升」
4. 顽固题（reappear_count>=3 且最近评分<=2）→ 出现在顽固清单
5. 回灌幂等：同周跑两次，report_meta 该 key 仅一行
6. --json 输出可被 json.loads 解析

测试惯例：用临时 sqlite 库 + create_schema 建表，不触碰生产库。
时区一律走 time_layer，构造 created_at 时先按本地日期生成再转 UTC，
确保"本周/上周"筛选正确（绝不用 s[:10]）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

import pytest

import scripts.fdl_weekly_report as wr
from fdl_core.db.schema import create_schema
from fdl_core.srs.time_layer import LOCAL_TZ, TS_FORMAT, to_utc

# 固定参照周（已核实 2026-09-07 为周一 → ISO 周 2026-W37）
WEEK = "2026-W37"
WEEK_PREV = "2026-W36"  # 上周，monday = 2026-08-31


def _make_created(local_iso: str, hour: int = 12) -> str:
    """把"本地日期 + 小时"转成 UTC 存库字符串（走 time_layer，禁止切片）。"""
    ld = datetime.fromisoformat(local_iso)
    local_dt = ld.replace(hour=hour, minute=0, second=0, tzinfo=LOCAL_TZ)
    return to_utc(local_dt).strftime(TS_FORMAT)


@pytest.fixture
def db_path(tmp_path):
    """建一个带完整 schema 的临时库，返回路径。"""
    p = tmp_path / "fdl.db"
    conn = sqlite3.connect(p)
    create_schema(conn)
    conn.close()
    return p


def _insert_mistake(conn, *, mid, dt, reappear=1):
    """插入一条错题（mistake_record）。"""
    conn.execute(
        "INSERT INTO mistake_record "
        "(id,user_id,kp_id,occurred_at,subject,diagnosis_type,reappear_count) "
        "VALUES (?,1,0,?,?,?,?)",
        (mid, "2026-01-01", "MATH", dt, reappear),
    )
    conn.commit()


def _seed_feedback(
    conn,
    *,
    mid,
    dt,
    rating,
    reappear=1,
    created_local="2026-09-09",
    planned=1,
    sched_created_local="2026-09-09",
):
    """插入一条"错题 + 复习计划 + 复习反馈"的最小闭环，并返回 schedule id。"""
    _insert_mistake(conn, mid=mid, dt=dt, reappear=reappear)
    return _add_feedback(
        conn,
        mid=mid,
        rating=rating,
        created_local=created_local,
        planned=planned,
        sched_created_local=sched_created_local,
    )


def _add_feedback(
    conn,
    *,
    mid,
    rating,
    created_local="2026-09-09",
    planned=1,
    sched_created_local="2026-09-09",
):
    """为已存在的错题追加一条"复习计划 + 复习反馈"，返回 schedule id。

    2026-09-12 修复：review_schedule 有部分唯一索引
    ux_review_pending ON (user_id, mistake_id) WHERE status='PENDING'，
    同一错题只能存在一条 PENDING 计划。原实现直接 INSERT，模拟"第 2 次复习"
    时即违反约束（IntegrityError）。此处先作废旧 PENDING（置 DONE），
    与 mark_reviewed 的真实语义一致（复习完成 → 旧计划 DONE → 写新计划）。
    """
    sc = _make_created(sched_created_local)
    conn.execute(
        "UPDATE review_schedule SET status='DONE', updated_at=?"
        " WHERE mistake_id=? AND status='PENDING'",
        (sc, mid),
    )
    conn.execute(
        "INSERT INTO review_schedule "
        "(user_id,mistake_id,kp_id,subject_id,due_date,due_session,"
        " planned_interval_days,priority_score,est_seconds,status,source,created_at,updated_at) "
        "VALUES (1,?,NULL,1,?,'PM',?,90.0,120,'PENDING','test',?,?)",
        (mid, "2026-09-10", planned, sc, sc),
    )
    sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    fc = _make_created(created_local)
    conn.execute(
        "INSERT INTO review_feedback "
        "(user_id,schedule_id,kp_id,subject_id,self_rating,created_at) "
        "VALUES (1,?,NULL,1,?,?)",
        (sid, rating, fc),
    )
    conn.commit()
    return sid


# ────────────────────────────────────────────────────────────
# 1. 空库
# ────────────────────────────────────────────────────────────
def test_empty_db_generates_html_with_dash(db_path, tmp_path):
    out = tmp_path / "weekly_report_2026-W37.html"
    rc = wr.main(["--db", str(db_path), "--week", WEEK, "--out", str(out), "--json"])
    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert "掌握度周报" in html
    # 无样本指标应显示「—」
    assert "—" in html

    # JSON 可解析且四项指标均为"无样本"
    j = json.loads((tmp_path / "weekly_report_2026-W37.json").read_text(encoding="utf-8"))
    assert j["week"] == WEEK
    assert all(j["review_counts"]["week"][k] == 0 for k in wr.DIAG_TYPES)
    assert all(j["mastery"][k]["this"] is None for k in wr.DIAG_TYPES)
    assert len(j["stubborn"]) == 0

    # 禁止 emoji
    assert not re.search(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]", html)


# ────────────────────────────────────────────────────────────
# 2. 单条反馈 → 该错因复习次数=1
# ────────────────────────────────────────────────────────────
def test_single_feedback_count_one(db_path):
    conn = sqlite3.connect(db_path)
    _seed_feedback(conn, mid=1, dt="CALC", rating=3, reappear=1, planned=4)
    conn.close()

    conn = sqlite3.connect(db_path)
    m = wr.compute_metrics(conn, WEEK)
    conn.close()
    assert m["review_counts"]["week"]["CALC"] == 1
    assert m["review_counts"]["cum"]["CALC"] == 1
    # 其他错因无样本 → 0（渲染为「—」）
    assert m["review_counts"]["week"]["CONCEPT"] == 0
    # 间隔指标：本周平均间隔应等于该计划 planned_interval_days
    assert m["interval"]["CALC"]["this"] == 4


# ────────────────────────────────────────────────────────────
# 3. 趋势：上周 avg=2 / 本周 avg=4 → 上升
# ────────────────────────────────────────────────────────────
def test_mastery_trend_up(db_path):
    conn = sqlite3.connect(db_path)
    # 上周：CALC 反馈 rating=2（落在 2026-W36 内）
    _seed_feedback(
        conn,
        mid=10,
        dt="CALC",
        rating=2,
        created_local="2026-09-02",
        sched_created_local="2026-09-02",
    )
    # 本周：CALC 反馈 rating=4（落在 2026-W37 内）
    _seed_feedback(
        conn,
        mid=11,
        dt="CALC",
        rating=4,
        created_local="2026-09-09",
        sched_created_local="2026-09-09",
    )
    conn.close()

    conn = sqlite3.connect(db_path)
    m = wr.compute_metrics(conn, WEEK)
    conn.close()
    assert m["mastery"]["CALC"]["last"] == 2.0
    assert m["mastery"]["CALC"]["this"] == 4.0
    assert m["mastery"]["CALC"]["trend"] == "上升"


# ────────────────────────────────────────────────────────────
# 4. 顽固题：reappear_count>=3 且最近评分<=2 → 出现在清单
# ────────────────────────────────────────────────────────────
def test_stubborn_list(db_path):
    conn = sqlite3.connect(db_path)
    _insert_mistake(conn, mid=90, dt="CALC", reappear=3)
    # 先放一条较早 rating=4（确保"最近一次"是后面的 rating=1）
    _add_feedback(
        conn, mid=90, rating=4, created_local="2026-09-08", sched_created_local="2026-09-08"
    )
    # 最近一次 rating=1（本周）
    _add_feedback(
        conn, mid=90, rating=1, created_local="2026-09-10", sched_created_local="2026-09-10"
    )
    conn.close()

    conn = sqlite3.connect(db_path)
    m = wr.compute_metrics(conn, WEEK)
    conn.close()
    ids = [s["id"] for s in m["stubborn"]]
    assert 90 in ids
    item = next(s for s in m["stubborn"] if s["id"] == 90)
    assert item["rc"] >= 3
    assert item["last_rating"] <= 2
    # 回灌计数：CALC 顽固数=1
    assert m["stubborn_by_type"]["CALC"]["count"] == 1


# ────────────────────────────────────────────────────────────
# 5. 回灌幂等：同周跑两次，report_meta 仅一行
# ────────────────────────────────────────────────────────────
def test_write_back_idempotent(db_path, tmp_path):
    conn = sqlite3.connect(db_path)
    _seed_feedback(
        conn,
        mid=90,
        dt="CALC",
        rating=1,
        reappear=3,
        created_local="2026-09-10",
        sched_created_local="2026-09-10",
    )
    conn.close()

    out = tmp_path / "r.html"
    for _ in range(2):
        rc = wr.main(["--db", str(db_path), "--week", WEEK, "--out", str(out)])
        assert rc == 0

    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM report_meta WHERE key='weekly_stubborn_CALC'"
    ).fetchone()[0]
    value = conn.execute(
        "SELECT value FROM report_meta WHERE key='weekly_stubborn_CALC'"
    ).fetchone()[0]
    conn.close()
    assert n == 1  # 幂等：不重复
    payload = json.loads(value)
    assert payload["count"] == 1
    assert payload["ids"] == [90]


# ────────────────────────────────────────────────────────────
# 6. --json 输出可被 json.loads 解析
# ────────────────────────────────────────────────────────────
def test_json_output_parseable(db_path, tmp_path):
    out = tmp_path / "weekly_report_2026-W37.html"
    rc = wr.main(["--db", str(db_path), "--week", WEEK, "--out", str(out), "--json"])
    assert rc == 0
    jpath = tmp_path / "weekly_report_2026-W37.json"
    assert jpath.exists()
    # 必须可被解析（不抛异常）
    data = json.loads(jpath.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["week"] == WEEK
    assert "review_counts" in data and "mastery" in data and "interval" in data
