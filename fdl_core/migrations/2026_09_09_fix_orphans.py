"""
fdl_core/migrations/2026_09_09_fix_orphans.py

孤儿数据修复迁移（2026-09-09）。

背景（历史 bug 根因家族）：
    旧播种 SQL 曾把 `mistake_record.id` 写进 `review_schedule.kp_id`、
    把 `m.kp_id`（全 0）写进 `review_schedule.subject_id`。
    2026-09-08 代码已改用 `mistake_id` 调度，但历史 48 条脏数据未回填。
    实测：`PRAGMA foreign_key_check` = 96 行违规（48 kp_id + 48 subject_id）。

本迁移修复四类孤儿/脏数据，全程幂等，可重复执行：

  1. 备份主库（shutil.copy2 -> <db>.bak-<timestamp>，与既有命名一致）
  2. 补 CHINESE 学科（subject 表仅有 MATH，但 mistake_record 有 CHINESE(10)）
  3. 回填 review_schedule.subject_id：经 mistake_id 关联 mistake_record.subject
     -> subject.code 映射 subject.id
  4. 修 review_schedule.kp_id 孤儿：表重建令 kp_id 可空，孤儿置 NULL
     （保留列、最小改动、FK 立即干净；NULL 不触发 FK 检查）
  5. 处理 mistake_id IS NULL 的 PENDING（id=16，对应错题已删）：标 DONE
     + subject_id 归默认 MATH + kp_id 置 NULL，保留历史不破坏计数
  6. 重算 overdue_days：用本地日期（Asia/Shanghai，取自
     fdl_core.srs.time_layer.local_date）而非 date('now')(UTC)

幂等：
  - CHINESE 已存在则跳过插入
  - subject_id 仅更新仍孤儿（NOT IN subject.id）的行
  - kp_id 已可空则跳过重建；孤儿置 NULL，重跑影响 0 行
  - id=16 已 DONE 则跳过
  - overdue 仅更新 overdue 的 PENDING 行

返回 dict：{steps, errors, backup_path, subject_added,
            subject_id_fixed, kp_id_fixed, null_mistake_handled,
            overdue_updated, fk_violations_after, rows_before, rows_after}

可直接运行（默认作用于生产主库，由 get_paths().primary_db_path 解析）：
    python fdl_core/migrations/2026_09_09_fix_orphans.py [--db /path/to/copy.db]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
from datetime import datetime

from fdl_core.paths import get_paths
from fdl_core.srs.time_layer import local_date

# 重建 review_schedule 后需恢复的全部索引（与既有迁移保持一致）
_INDEX_DDL = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_review_pending"
    " ON review_schedule (user_id, mistake_id) WHERE status = 'PENDING'",
    "CREATE INDEX IF NOT EXISTS idx_review_schedule_user_status_due"
    " ON review_schedule (user_id, status, due_date, priority_score)",
    "CREATE INDEX IF NOT EXISTS idx_review_schedule_mistake"
    " ON review_schedule (mistake_id, status)",
]

# 仅把 kp_id 的 NOT NULL 去掉，保留其 REFERENCES knowledge_point(id)
_KP_NOT_NULL_RE = re.compile(r"kp_id\s+INTEGER\s+NOT NULL\s+REFERENCES\s+knowledge_point\(id\)")


def _table_ddl(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='review_schedule'"
    ).fetchone()
    return row[0] if row else None


def _kp_is_not_null(ddl: str | None) -> bool:
    if not ddl:
        return False
    return bool(_KP_NOT_NULL_RE.search(ddl))


def _make_kp_nullable(ddl: str) -> str:
    """把 kp_id 的 NOT NULL 去掉，其余 DDL 原样保留。"""
    return _KP_NOT_NULL_RE.sub("kp_id INTEGER REFERENCES knowledge_point(id)", ddl)


def _backup(db_path: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = str(db_path) + f".bak-{ts}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def migrate(db_path: str | None = None) -> dict:
    """执行孤儿修复迁移。db_path 为 None 时作用于生产主库。"""
    if db_path is None:
        db_path = get_paths().primary_db_path

    summary: dict = {
        "steps": [],
        "errors": [],
        "backup_path": None,
        "subject_added": False,
        "subject_id_fixed": 0,
        "kp_id_fixed": 0,
        "null_mistake_handled": 0,
        "overdue_updated": 0,
        "fk_violations_after": None,
        "rows_before": None,
        "rows_after": None,
    }

    # —— 0) 备份 ——
    try:
        summary["backup_path"] = _backup(db_path)
        summary["steps"].append(f"backup -> {summary['backup_path']}")
    except Exception as e:  # noqa: BLE001
        summary["errors"].append(f"backup: {e}")
        return summary

    conn = sqlite3.connect(db_path)
    conn.isolation_level = None  # 手动控制事务，避免与 DDL 自动提交语义冲突
    conn.execute("PRAGMA foreign_keys=OFF")  # 重建表需要；数据最终均为合法 FK

    # 行数基线
    summary["rows_before"] = conn.execute("SELECT COUNT(*) FROM review_schedule").fetchone()[0]

    # ===== 阶段 A：令 kp_id 可空（如需），并置孤儿为 NULL =====
    ddl_before = _table_ddl(conn)
    try:
        if _kp_is_not_null(ddl_before):
            new_ddl = _make_kp_nullable(ddl_before)
            new_ddl = re.sub(
                r'CREATE TABLE\s+"?review_schedule"?',
                "CREATE TABLE _review_schedule_new",
                new_ddl,
                count=1,
            )
            conn.execute("DROP TABLE IF EXISTS _review_schedule_new")
            conn.execute(new_ddl)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(review_schedule)").fetchall()]
            col_list = ", ".join(cols)
            conn.execute(
                f"INSERT INTO _review_schedule_new ({col_list})"
                f" SELECT {col_list} FROM review_schedule"
            )
            conn.execute("DROP TABLE review_schedule")
            conn.execute("ALTER TABLE _review_schedule_new RENAME TO review_schedule")
            for idx_sql in _INDEX_DDL:
                conn.execute(idx_sql)
            summary["steps"].append("rebuild review_schedule: kp_id now nullable")
        else:
            summary["steps"].append("kp_id already nullable — skip rebuild")

        # 孤儿 kp_id -> NULL
        cur = conn.execute(
            "UPDATE review_schedule"
            " SET kp_id = NULL"
            " WHERE kp_id IS NOT NULL"
            "   AND kp_id NOT IN (SELECT id FROM knowledge_point)"
        )
        summary["kp_id_fixed"] = cur.rowcount
        summary["steps"].append(f"set {cur.rowcount} orphan kp_id -> NULL")
    except Exception as e:  # noqa: BLE001
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        summary["errors"].append(f"kp_phase: {e}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()
        return summary

    # ===== 阶段 B：学科补全 + subject_id 回填 + id=16 孤儿 + overdue =====
    try:
        conn.execute("BEGIN")

        # 2) 补 CHINESE 学科
        exists = conn.execute("SELECT 1 FROM subject WHERE code = 'CHINESE'").fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO subject"
                " (user_id, code, name, short_name, color_hex, icon,"
                "  rotation_weight, grade_start, grade_end, sort_order,"
                "  is_active)"
                " VALUES (1, 'CHINESE', '语文', '语', '#C0392B', 'book',"
                "  0.30, 4, 9, 2, 1)"
            )
            summary["subject_added"] = True
            summary["steps"].append("insert subject CHINESE (id=2)")
        else:
            summary["steps"].append("subject CHINESE already exists — skip")

        # 3) 回填 subject_id（仅孤儿行；重跑影响 0 行）
        cur = conn.execute(
            "UPDATE review_schedule"
            " SET subject_id = ("
            "   SELECT s.id FROM subject s"
            "   JOIN mistake_record m ON m.subject = s.code"
            "   WHERE m.id = review_schedule.mistake_id"
            " )"
            " WHERE review_schedule.mistake_id IS NOT NULL"
            "   AND review_schedule.subject_id NOT IN (SELECT id FROM subject)"
        )
        summary["subject_id_fixed"] = cur.rowcount
        summary["steps"].append(f"backfill subject_id for {cur.rowcount} rows")

        # 5) 处理 mistake_id IS NULL 的 PENDING（id=16，孤儿）
        math_id = conn.execute("SELECT id FROM subject WHERE code = 'MATH'").fetchone()[0]
        cur = conn.execute(
            "UPDATE review_schedule"
            " SET status = 'DONE', subject_id = ?, kp_id = NULL,"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            " WHERE mistake_id IS NULL AND status = 'PENDING'",
            (math_id,),
        )
        summary["null_mistake_handled"] = cur.rowcount
        summary["steps"].append(f"handle {cur.rowcount} NULL-mistake PENDING -> DONE")

        # 6) 重算 overdue_days（用本地日期，Asia/Shanghai）
        today_str = local_date().isoformat()
        cur = conn.execute(
            "UPDATE review_schedule"
            " SET overdue_days = CAST(julianday(?) - julianday(due_date) AS INTEGER)"
            " WHERE status = 'PENDING' AND due_date < ?"
            "   AND overdue_days IS NOT CAST(julianday(?) - julianday(due_date) AS INTEGER)",
            (today_str, today_str, today_str),
        )
        summary["overdue_updated"] = cur.rowcount
        summary["steps"].append(
            f"recompute overdue_days for {cur.rowcount} overdue PENDING rows"
            f" (local today={today_str})"
        )

        # 校验：FK 必须 0 违规；行数不变（无删除）
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        summary["fk_violations_after"] = len(fk)
        rows_after = conn.execute("SELECT COUNT(*) FROM review_schedule").fetchone()[0]
        summary["rows_after"] = rows_after
        if fk:
            raise RuntimeError(f"FK violations remain after fix: {len(fk)} -> {fk[:5]}")
        if rows_after != summary["rows_before"]:
            raise RuntimeError(f"rows mismatch: before={summary['rows_before']} after={rows_after}")

        conn.execute("COMMIT")
        summary["steps"].append("commit (phase B)")
    except Exception as e:  # noqa: BLE001
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        summary["errors"].append(f"data_phase: {e}")

    conn.execute("PRAGMA foreign_keys=ON")
    conn.close()
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="optional db path (default: production)")
    args = ap.parse_args()
    print(json.dumps(migrate(args.db), ensure_ascii=False, indent=2))
