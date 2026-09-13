"""
fdl_core/migrations/2026_09_08_rename_reviews_done.py

P23 字段改名（2026-09-08）：`daily_metric.reviews_done` → `today_reviewed_answers`。

背景（见 docs/research/统计口径审计报告.md §P23）：
`reviews_done` 这个名字容易被读成「累计复习次数」，但它的实际口径是
`SUM(answer_log WHERE task_type='REVIEW')`，即**当日 REVIEW 作答条数**。
King 拍板：**SQL 口径不变，只改字段名**消除歧义。
（改名零风险：改名前全代码库对 reviews_done 只有「定义 + 写入」，无任何读取方。）

「累计复习次数」语义另有出处（a2_upgrade 的复习历史），二者不可混淆。

幂等：
1. `PRAGMA table_info(daily_metric)` 读当前列名
2. 表不存在              → skipped（新库由 create_schema/ensure_metric_tables 直接建新名）
3. 只有 today_reviewed_answers → skipped（已迁移）
4. 只有 reviews_done      → `ALTER TABLE ... RENAME COLUMN`（SQLite 3.25+）
5. 两列同时存在（异常态） → 报错中止，绝不猜测数据归属，交人工排查

返回 dict：{steps, errors, columns_before, columns_after, skipped}

可直接运行：
    python fdl_core/migrations/2026_09_08_rename_reviews_done.py
打印 JSON。
"""

from __future__ import annotations

import sqlite3

from fdl_core.paths import get_paths

_OLD = "reviews_done"
_NEW = "today_reviewed_answers"


def _columns(conn: sqlite3.Connection) -> list[str]:
    """daily_metric 当前列名；表不存在返回 []。"""
    return [r[1] for r in conn.execute("PRAGMA table_info(daily_metric)").fetchall()]


def migrate() -> dict:
    """执行迁移；返回结构化结果 dict。"""
    conn = sqlite3.connect(get_paths().primary_db_path)
    summary: dict = {
        "steps": [],
        "errors": [],
        "columns_before": [],
        "columns_after": [],
        "skipped": False,
    }

    cols = _columns(conn)
    summary["columns_before"] = cols
    has_old, has_new = _OLD in cols, _NEW in cols

    if not cols:
        summary["steps"].append("table daily_metric not found — skip")
        summary["skipped"] = True
    elif has_old and has_new:
        summary["errors"].append(
            f"ABORT: {_OLD} 与 {_NEW} 两列同时存在（异常态）。"
            "无法判断哪一列持有真实数据，绝不自动合并/丢弃。请人工排查后再迁移。"
        )
    elif has_new:
        summary["steps"].append(f"column already renamed to {_NEW} — skip")
        summary["skipped"] = True
    elif has_old:
        try:
            conn.execute(f"ALTER TABLE daily_metric RENAME COLUMN {_OLD} TO {_NEW}")
            conn.commit()
            summary["steps"].append(f"rename column {_OLD} -> {_NEW}")
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(f"rename column: {e}")
    else:
        summary["errors"].append(
            f"ABORT: daily_metric 既无 {_OLD} 也无 {_NEW} 列，表结构与预期不符。"
        )

    summary["columns_after"] = _columns(conn)
    conn.close()
    return summary


if __name__ == "__main__":
    import json

    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
