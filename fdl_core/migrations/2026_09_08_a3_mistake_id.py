"""
fdl_core/migrations/2026_09_08_a3_mistake_id.py

A3 方案迁移（2026-09-08）：
1. review_schedule 加 mistake_id 列（调度单元从 kp_id 改为 mistake_id）
2. 回填存量数据：mistake_id = kp_id（因为历史上 kp_id 存的就是 mistake id）
3. 加索引 idx_review_schedule_mistake

幂等：IF NOT EXISTS / 已存在则跳过，可重复执行。

背景（P4 根因）：
    原播种 SQL `SELECT 1, m.id, m.kp_id, ...` 把 mistake id 写进了 kp_id 字段，
    而 mistake_record.kp_id 全为 0（知识点体系未建），导致：
    - 50 张错题卡共享同一行 kp_state，后一张覆盖前一张的 S 值
    - `UPDATE ... WHERE kp_id=?` 把同 kp 下所有计划一次作废
    本迁移把调度单元显式分离为 mistake_id，并回填历史数据。
"""

from __future__ import annotations

import sqlite3

from fdl_core.paths import get_paths


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def migrate() -> dict:
    """执行迁移；返回 {steps, errors, mistake_id_exists, backfilled, index_exists}。"""
    conn = sqlite3.connect(get_paths().primary_db_path)
    cur = conn.cursor()
    summary: dict = {"steps": [], "errors": []}

    # 1) 加列
    cols = _columns(conn, "review_schedule")
    if "mistake_id" not in cols:
        try:
            cur.execute(
                "ALTER TABLE review_schedule ADD COLUMN mistake_id INTEGER"
                " REFERENCES mistake_record(id)"
            )
            summary["steps"].append("add column mistake_id")
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(f"add column: {e}")
    else:
        summary["steps"].append("column mistake_id already exists")

    # 2) 回填：历史数据 mistake_id = kp_id（kp_id 当时存的就是 mistake id）
    #    仅回填 mistake_id IS NULL 且 kp_id 能在 mistake_record 中匹配到的行
    try:
        cur.execute(
            "UPDATE review_schedule SET mistake_id = kp_id"
            " WHERE mistake_id IS NULL"
            "   AND kp_id IN (SELECT id FROM mistake_record)"
        )
        summary["backfilled"] = cur.rowcount
        summary["steps"].append(f"backfill {cur.rowcount} rows")
    except Exception as e:  # noqa: BLE001
        summary["errors"].append(f"backfill: {e}")

    # 3) 索引
    try:
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_review_schedule_mistake"
            " ON review_schedule (mistake_id, status)"
        )
        summary["steps"].append("create index idx_review_schedule_mistake")
    except Exception as e:  # noqa: BLE001
        summary["errors"].append(f"index: {e}")

    # 校验
    cols = _columns(conn, "review_schedule")
    summary["mistake_id_exists"] = "mistake_id" in cols
    cur.execute("SELECT COUNT(*) FROM review_schedule WHERE mistake_id IS NOT NULL")
    summary["rows_with_mistake_id"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM review_schedule")
    summary["rows_total"] = cur.fetchone()[0]
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_review_schedule_mistake'"
    )
    summary["index_exists"] = cur.fetchone() is not None

    conn.commit()
    conn.close()
    return summary


if __name__ == "__main__":
    import json

    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
