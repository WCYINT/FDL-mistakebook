"""
fdl_core/migrations/2026_09_07_review_feedback.py
幂等迁移：为已部署的 fdl.db 新增 review_feedback 表（仪表盘/复习页用户反馈）。
- 沿用 schema.py 中 _TS_DEFAULT（datetime('now')）保证与原库一致
- 用 IF NOT EXISTS，对重复执行完全安全
- 单独文件而非嵌入 schema.py：避免冷启动时双源定义冲突
"""

from __future__ import annotations

from fdl_core.db.schema import _TS_DEFAULT


def _conn_sqlite_sqlite():
    """惰性导入路径与连接（保持迁移脚本与 schema.py 解耦）"""
    import sqlite3

    from fdl_core.paths import get_paths

    return sqlite3.connect(get_paths().primary_db_path)


def migrate() -> dict:
    """执行迁移；返回 {step, rows_before, rows_after, errors}。"""
    conn = _conn_sqlite_sqlite()
    cur = conn.cursor()
    summary: dict = {"steps": [], "errors": []}

    statements = [
        # 反馈表（含 schedule_id 软外键；schedule 删除后保留历史）
        f"""
        CREATE TABLE IF NOT EXISTS review_feedback (
            id                INTEGER PRIMARY KEY,
            user_id           INTEGER NOT NULL,
            schedule_id       INTEGER          REFERENCES review_schedule(id),
            kp_id             INTEGER NOT NULL REFERENCES knowledge_point(id),
            subject_id        INTEGER NOT NULL REFERENCES subject(id),
            self_rating       INTEGER NOT NULL,
            duration_seconds  INTEGER,
            note              TEXT,
            attachments_json  TEXT,
            srs_interval_days REAL,
            created_at        TEXT    NOT NULL DEFAULT {_TS_DEFAULT}
        );
        """,
        # 索引：按用户+KP+时间倒序拉取历史反馈（驾驶舱/复习页）
        "CREATE INDEX IF NOT EXISTS idx_review_feedback_user_kp_created "
        "ON review_feedback (user_id, kp_id, created_at);",
    ]

    for i, sql in enumerate(statements):
        try:
            cur.executescript(sql)
            summary["steps"].append(f"step {i + 1} ok")
        except Exception as e:  # noqa: BLE001
            summary["errors"].append(f"step {i + 1}: {e}")

    # 校验：表存在 + 索引在
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='review_feedback'")
    summary["table_exists"] = cur.fetchone() is not None
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='idx_review_feedback_user_kp_created'"
    )
    summary["index_exists"] = cur.fetchone() is not None

    conn.commit()
    conn.close()
    return summary


if __name__ == "__main__":
    import json

    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
