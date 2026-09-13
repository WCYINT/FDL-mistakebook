"""P1 复苏前置：为"无复习计划的错题"补建首条 review_schedule

背景（已实测确认的闭环缺口）：
  create_initial_review_schedule() 的 docstring 写明"错题入库（ING）时由本函数落首条
  review_schedule"，但全代码库搜不到它的任何调用方（只有定义）。导致：
  - /api/mistakes/confirm 只写 mistake_record，不建计划
  - 5 条人工确认错题（#77062-77066）计划数全为 0；全库 8 条错题完全没有复习计划
  - 这些错题永不出现在复习队列，违背 FDL"录错题是为了复习"的核心闭环

本迁移为所有无计划的错题补建首条计划（due=录入次日，interval_days=1），与
create_initial_review_schedule 的语义一致。

幂等：create_initial_review_schedule 内部按 mistake_id 做了 PENDING 存在性检查，
重复跑不会重复建。备份生产库后再改。

安全：执行前先 WAL checkpoint 再 cp 主文件到 <原路径>.bak-p1-sched-<时间戳>。
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from fdl_core.paths import get_paths


def migrate(db_path: str | Path | None = None) -> dict:
    db_path = str(db_path or get_paths().primary_db_path)
    summary: dict = {
        "db_path": db_path,
        "steps": [],
        "errors": [],
        "backfilled": 0,
        "before_missing": 0,
        "after_missing": 0,
        "backup": None,
    }

    # 步骤 1：WAL checkpoint 后备份生产库（先备份再改）
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak-p1-sched-{ts}"
    try:
        ck = sqlite3.connect(db_path)
        try:
            ck.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            ck.close()
        shutil.copyfile(db_path, backup_path)
        summary["backup"] = backup_path
        summary["steps"].append(f"backup -> {backup_path}")
    except Exception as e:
        summary["errors"].append(f"backup failed: {e}")
        return summary

    conn = sqlite3.connect(db_path)
    try:
        # 步骤 2：统计补前"无计划错题"数量
        before = conn.execute(
            "SELECT COUNT(*) FROM mistake_record m "
            "WHERE NOT EXISTS (SELECT 1 FROM review_schedule rs "
            "WHERE rs.mistake_id = m.id)"
        ).fetchone()[0]
        summary["before_missing"] = before

        # 步骤 3：对它们补建首条计划
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT m.id FROM mistake_record m "
                "WHERE NOT EXISTS (SELECT 1 FROM review_schedule rs "
                "WHERE rs.mistake_id = m.id)"
            ).fetchall()
        ]
        from fdl_core.mistakes.review import create_initial_review_schedule

        n = create_initial_review_schedule(conn, ids, interval_days=1)
        summary["backfilled"] = n
        summary["steps"].append(f"backfilled {n} 条首条复习计划（目标 {len(ids)} 条）")

        # 步骤 4：复查补后"无计划错题"数量（幂等验证）
        after = conn.execute(
            "SELECT COUNT(*) FROM mistake_record m "
            "WHERE NOT EXISTS (SELECT 1 FROM review_schedule rs "
            "WHERE rs.mistake_id = m.id)"
        ).fetchone()[0]
        summary["after_missing"] = after
        summary["steps"].append(f"补后无计划错题: {after}（预期 0）")
    except Exception as e:
        summary["errors"].append(f"migrate failed: {e}")
        conn.rollback()
    finally:
        conn.close()
    return summary


if __name__ == "__main__":
    import json

    # 允许 CLI 覆盖库路径：python 该文件 <db_path>
    import sys

    _path = sys.argv[1] if len(sys.argv) > 1 else None
    print(json.dumps(migrate(_path), ensure_ascii=False, indent=2))
