"""
fdl_core/migrations/2026_09_08_rebuild_review_schedule.py

P1 第二半修复（2026-09-08）：重建 review_schedule 表，去掉内联 UNIQUE。

背景（同一 kp_id≡0 占位根因家族）：
    修复 ux_review_pending 后，新 PENDING 的 kp_id 仍为 0（= m.kp_id）。
    A3 算法首次复习 interval=1 天 → 今天复习 2 张不同卡 → 两张 due=明天
    → `(user_id=1, kp_id=0, due_date=明天)` 撞原建表 DDL 里的内联
    `UNIQUE (user_id, kp_id, due_date)`（sqlite_autoindex_review_schedule_1）→ 崩溃。
    即 P1 只修了一半，正常复习动作仍会崩。

SQLite 不支持 ALTER TABLE ... DROP CONSTRAINT，故采用标准表重建流程：
    PRAGMA foreign_keys=OFF
    BEGIN
      CREATE TABLE _review_schedule_new (...去掉内联 UNIQUE...)
      INSERT INTO _review_schedule_new SELECT <全部列> FROM review_schedule
      DROP TABLE review_schedule
      ALTER TABLE _review_schedule_new RENAME TO review_schedule
      重建索引：ux_review_pending / idx_review_schedule_user_status_due / idx_review_schedule_mistake
    COMMIT
    PRAGMA foreign_keys=ON

安全：
    - 迁移前先备份主库到 <db_dir>/fdl.db.bak-<timestamp>
    - rows_before == rows_after，不等则 ROLLBACK 并报错
    - 全程事务，任一步失败 ROLLBACK

幂等：若当前 DDL 已无内联 UNIQUE(user_id, kp_id, due_date) 则 skipped=True 直接返回。

返回 dict：{steps, errors, backup_path, rows_before, rows_after,
            ddl_before, ddl_after, inline_unique_removed, skipped}

可直接运行：
    python fdl_core/migrations/2026_09_08_rebuild_review_schedule.py
打印 JSON。
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime

from fdl_core.paths import get_paths

# 需在重建后恢复的全部索引（含部分唯一索引 ux_review_pending 的 mistake_id 版）
_INDEX_DDL = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_review_pending"
    " ON review_schedule (user_id, mistake_id) WHERE status = 'PENDING'",
    "CREATE INDEX IF NOT EXISTS idx_review_schedule_user_status_due"
    " ON review_schedule (user_id, status, due_date, priority_score)",
    "CREATE INDEX IF NOT EXISTS idx_review_schedule_mistake"
    " ON review_schedule (mistake_id, status)",
]

_INLINE_UNIQUE_RE = re.compile(r",\s*UNIQUE\s*\([^)]*\)", re.IGNORECASE)


def _table_ddl(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='review_schedule'"
    ).fetchone()
    return row[0] if row else None


def _has_inline_unique(ddl: str | None) -> bool:
    if not ddl:
        return False
    return bool(re.search(r"UNIQUE\s*\([^)]*\)", ddl, re.IGNORECASE))


def _strip_inline_unique(ddl: str) -> str:
    """移除表级内联 UNIQUE 约束，保留其余全部列/类型/默认值/FK。"""
    new = _INLINE_UNIQUE_RE.sub("", ddl).rstrip().rstrip(";").rstrip()
    if not new.endswith(")"):
        new = new + "\n)"
    return new


def migrate() -> dict:
    paths = get_paths()
    db_path = paths.primary_db_path
    summary: dict = {
        "steps": [],
        "errors": [],
        "backup_path": None,
        "rows_before": None,
        "rows_after": None,
        "ddl_before": None,
        "ddl_after": None,
        "inline_unique_removed": False,
        "skipped": False,
    }

    # —— 前置：读当前 DDL，判定是否仍需重建 ——
    conn = sqlite3.connect(db_path)
    ddl_before = _table_ddl(conn)
    summary["ddl_before"] = ddl_before
    summary["rows_before"] = conn.execute("SELECT COUNT(*) FROM review_schedule").fetchone()[0]

    if not _has_inline_unique(ddl_before):
        # 已是新结构 → 跳过
        summary["steps"].append("inline UNIQUE already absent — skip rebuild")
        summary["skipped"] = True
        summary["ddl_after"] = ddl_before
        summary["rows_after"] = summary["rows_before"]
        conn.close()
        return summary

    # —— 安全：先备份主库 ——
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = str(db_path) + f".bak-{ts}"
    try:
        shutil.copy2(db_path, backup_path)
        summary["backup_path"] = backup_path
        summary["steps"].append(f"backup -> {backup_path}")
    except Exception as e:  # noqa: BLE001
        summary["errors"].append(f"backup: {e}")
        conn.close()
        return summary

    # —— 构造新 DDL（去掉内联 UNIQUE）——
    new_ddl = _strip_inline_unique(ddl_before)
    new_ddl = new_ddl.replace(
        "CREATE TABLE review_schedule", "CREATE TABLE _review_schedule_new", 1
    )
    summary["inline_unique_removed"] = True

    # 重建必须在 foreign_keys=OFF 下进行（DROP 被引用表需要）
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        # 1) 建新表
        conn.execute(new_ddl)
        summary["steps"].append("create _review_schedule_new (no inline UNIQUE)")
        # 2) 拷贝全列（列序一致，SELECT * 安全）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(review_schedule)").fetchall()]
        col_list = ", ".join(cols)
        conn.execute(
            f"INSERT INTO _review_schedule_new ({col_list}) SELECT {col_list} FROM review_schedule"
        )
        n_copied = conn.execute("SELECT COUNT(*) FROM _review_schedule_new").fetchone()[0]
        summary["steps"].append(f"copy {n_copied} rows")
        # 3) 删旧表 + 改名
        conn.execute("DROP TABLE review_schedule")
        conn.execute("ALTER TABLE _review_schedule_new RENAME TO review_schedule")
        summary["steps"].append("drop old + rename new -> review_schedule")
        # 4) 重建索引
        for idx_sql in _INDEX_DDL:
            conn.execute(idx_sql)
        summary["steps"].append(
            "recreate indexes (ux_review_pending / idx_user_status_due / idx_mistake)"
        )
        # 5) 校验行数
        rows_after = conn.execute("SELECT COUNT(*) FROM review_schedule").fetchone()[0]
        summary["rows_after"] = rows_after
        if rows_after != summary["rows_before"]:
            raise RuntimeError(f"rows mismatch: before={summary['rows_before']} after={rows_after}")
        conn.execute("COMMIT")
        summary["steps"].append("commit")
    except Exception as e:  # noqa: BLE001
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        summary["errors"].append(f"rebuild: {e}")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()

    if not summary["errors"]:
        # 复读新 DDL 以确认
        c2 = sqlite3.connect(db_path)
        summary["ddl_after"] = _table_ddl(c2)
        c2.close()

    return summary


if __name__ == "__main__":
    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
