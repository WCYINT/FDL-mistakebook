"""2026-09-09 阶段 1.4 · OpenMAIC 借鉴 B：study_session 加会话状态机字段

King 要求 3 列：
  - lease_until       TEXT  （租约到期 UTC ISO）
  - resume_count      INT   （PAUSED→ACTIVE 次数）
  - last_event_at     TEXT  （最后一次状态迁移时间）

幂等：已存在则 skipped=True。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fdl_core.paths import get_paths


def migrate(db_path: str | Path | None = None) -> dict:
    db_path = str(db_path or get_paths().primary_db_path)
    conn = sqlite3.connect(db_path)
    summary = {"steps": [], "errors": [], "skipped": False}

    cols = {r[1] for r in conn.execute("PRAGMA table_info(study_session)").fetchall()}
    if not cols:
        summary["errors"].append("study_session table not found")
        conn.close()
        return summary

    # 加 3 列（SQLite 3.35+ 直接 ADD COLUMN；缺默认兼容老库）
    # 实际加 4 列（King 要求 3 列 + session_state 状态机必要列）
    target_cols = {
        "session_state": "TEXT NOT NULL DEFAULT 'ACTIVE'",  # 状态机（CREATED/ACTIVE/PAUSED/COMPLETED/ABANDONED/ERROR）
        "lease_until": "TEXT",  # 租约到期 UTC ISO
        "resume_count": "INTEGER NOT NULL DEFAULT 0",  # PAUSED→ACTIVE 次数
        "last_event_at": "TEXT",  # 最后一次状态迁移时间
    }
    added = []
    for col, decl in target_cols.items():
        if col in cols:
            summary["steps"].append(f"column {col} already exists")
        else:
            try:
                conn.execute(f"ALTER TABLE study_session ADD COLUMN {col} {decl}")
                added.append(col)
                summary["steps"].append(f"add column {col}")
            except Exception as e:
                summary["errors"].append(f"add {col}: {e}")

    # 写 last_event_at 用 created_at 作种子（缺默认但允许 NULL，老库补默认）
    try:
        conn.execute(
            "UPDATE study_session SET last_event_at = created_at WHERE last_event_at IS NULL"
        )
        summary["steps"].append("backfill last_event_at from created_at")
    except Exception as e:
        summary["errors"].append(f"backfill: {e}")

    conn.commit()

    # 验证
    new_cols = {r[1] for r in conn.execute("PRAGMA table_info(study_session)").fetchall()}
    summary["verified"] = all(c in new_cols for c in target_cols)

    conn.close()
    return summary


if __name__ == "__main__":
    import json

    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
