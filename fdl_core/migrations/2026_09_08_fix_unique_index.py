"""
fdl_core/migrations/2026_09_08_fix_unique_index.py

生产级阻断 bug 修复（2026-09-08）：

`review_schedule` 的部分唯一索引原建在 `(user_id, kp_id) WHERE status='PENDING'`，
但 `mistake_record.kp_id` 全部为 0（知识点体系未建，kp_id 仅为占位符），
而 `mark_reviewed()` 写新 PENDING 计划时 `kp_id = m.kp_id = 0`。

后果：所有新写的 PENDING 计划都是 (user_id=1, kp_id=0) → 全系统只能共存 1 条，
写第 2 条即 `UNIQUE constraint failed: review_schedule.user_id, review_schedule.kp_id` 崩溃。

修复：把索引改为 `(user_id, mistake_id)`，以"错题卡"为调度单元
（每道题的 PENDING 互不冲突），契合 FDL 当前 kp_id 全 0 占位的现实。

幂等：
1. 先检测 ux_review_pending 当前定义（查 sqlite_master.sql）
2. 若仍是 (user_id, kp_id) 则 DROP 后按 mistake_id 重建
3. 若已是 (user_id, mistake_id) 则跳过
4. 迁移前先检查 PENDING 计划里 mistake_id 的重复/NULL 情况：
   SQLite 唯一索引中 NULL 互不相等，故 mistake_id 为 NULL 的多行可共存（属正常），
   但若发现**非 NULL 的 mistake_id 有重复**，则报告并中止，绝不强行建索引（避免建索引即崩）。

返回 dict：{steps, errors, index_sql_before, index_sql_after,
            pending_total, pending_null_mistake_id, duplicate_mistake_ids, skipped}

可直接运行：
    python fdl_core/migrations/2026_09_08_fix_unique_index.py
打印 JSON。
"""

from __future__ import annotations

import sqlite3

from fdl_core.paths import get_paths


def _index_sql(conn: sqlite3.Connection) -> str | None:
    """返回 ux_review_pending 当前定义（sqlite_master.sql），不存在则 None。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_review_pending'"
    ).fetchone()
    return row[0] if row else None


def _is_on_mistake_id(sql: str | None) -> bool:
    if not sql:
        return False
    s = " ".join(sql.split()).lower()
    return "on review_schedule (user_id, mistake_id)" in s


def migrate() -> dict:
    """执行迁移；返回结构化结果 dict。"""
    conn = sqlite3.connect(get_paths().primary_db_path)
    cur = conn.cursor()
    summary: dict = {
        "steps": [],
        "errors": [],
        "index_sql_before": None,
        "index_sql_after": None,
        "pending_total": 0,
        "pending_null_mistake_id": 0,
        "duplicate_mistake_ids": [],
        "skipped": False,
    }

    # —— 前置检查：现有 PENDING 计划的 mistake_id 重复/NULL 情况 ——
    try:
        summary["pending_total"] = cur.execute(
            "SELECT COUNT(*) FROM review_schedule WHERE status='PENDING'"
        ).fetchone()[0]
        summary["pending_null_mistake_id"] = cur.execute(
            "SELECT COUNT(*) FROM review_schedule WHERE status='PENDING' AND mistake_id IS NULL"
        ).fetchone()[0]
        dups = cur.execute(
            "SELECT mistake_id, COUNT(*) n FROM review_schedule"
            " WHERE status='PENDING' AND mistake_id IS NOT NULL"
            " GROUP BY mistake_id HAVING COUNT(*) > 1"
        ).fetchall()
        summary["duplicate_mistake_ids"] = [{"mistake_id": r[0], "count": r[1]} for r in dups]
    except Exception as e:  # noqa: BLE001
        summary["errors"].append(f"precheck: {e}")

    # 若存在非 NULL 的 mistake_id 重复，绝不强行建索引（建索引会立刻失败）
    if summary["duplicate_mistake_ids"]:
        summary["errors"].append(
            "ABORT: 发现非 NULL 的 mistake_id 在 PENDING 中重复，"
            "强行建唯一索引会失败。请先排查数据。详情见 duplicate_mistake_ids。"
        )
        summary["index_sql_before"] = _index_sql(conn)
        summary["index_sql_after"] = summary["index_sql_before"]
        conn.close()
        return summary

    # —— 检测当前索引定义 ——
    sql_before = _index_sql(conn)
    summary["index_sql_before"] = sql_before

    if _is_on_mistake_id(sql_before):
        # 已是 (user_id, mistake_id) → 跳过
        summary["steps"].append("index already on (user_id, mistake_id) — skip")
        summary["skipped"] = True
    else:
        # 仍是 (user_id, kp_id)（或不存在）→ DROP 后按 mistake_id 重建
        try:
            cur.execute("DROP INDEX IF EXISTS ux_review_pending")
            summary["steps"].append("drop index ux_review_pending")
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(f"drop index: {e}")
        try:
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_review_pending"
                " ON review_schedule (user_id, mistake_id) WHERE status = 'PENDING'"
            )
            summary["steps"].append(
                "create unique index ux_review_pending ON (user_id, mistake_id)"
            )
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(f"create index: {e}")

    summary["index_sql_after"] = _index_sql(conn)
    conn.commit()
    conn.close()
    return summary


if __name__ == "__main__":
    import json

    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
