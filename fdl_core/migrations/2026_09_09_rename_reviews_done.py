"""2026-09-09 P23: daily_metric.reviews_done → today_reviewed_answers

King 拍板：保持 SQL 口径 SUM(answer_log WHERE task_type='REVIEW') 不变，**只改字段名**消除歧义。
理由：reviews_done 0 个下游消费者（只定义+写入，无读取），改名零风险。

幂等：再次运行 skipped=True。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fdl_core.paths import get_paths


def migrate(db_path: str | Path | None = None) -> dict:
    db_path = str(db_path or get_paths().primary_db_path)
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_metric)").fetchall()}
    summary = {"steps": [], "errors": [], "skipped": False}

    if "today_reviewed_answers" in cols and "reviews_done" not in cols:
        summary["skipped"] = True
        summary["steps"].append("already renamed")
        conn.close()
        return summary

    if "reviews_done" not in cols:
        summary["errors"].append("reviews_done column not found")
        conn.close()
        return summary

    # SQLite 3.25+ 支持 RENAME COLUMN
    conn.execute("ALTER TABLE daily_metric RENAME COLUMN reviews_done TO today_reviewed_answers")
    summary["steps"].append("renamed column reviews_done → today_reviewed_answers")
    conn.commit()

    new_cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_metric)").fetchall()}
    if "today_reviewed_answers" not in new_cols or "reviews_done" in new_cols:
        summary["errors"].append("verification failed: column not renamed")
    else:
        summary["verified"] = True

    conn.close()
    return summary


if __name__ == "__main__":
    import json

    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
