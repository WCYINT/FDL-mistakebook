"""批次 4 · ING-07 空白卷重做链路：从 `-clean` 图发起"重做"（而非"重看"）。

重做结果回写 `answer_log`（由作答链路完成）；本模块只负责生成重做任务
（daily_task，`task_type='REVIEW'`，`clean_image` 关联空白卷）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fdl_core.srs.time_layer import local_date


def start_redo_from_clean(
    conn: sqlite3.Connection,
    *,
    kp_id: int,
    clean_path: str | Path,
    user_id: int = 1,
    subject_id: int = 1,
) -> int:
    """从空白卷发起重做：生成 PENDING 重做任务，返回 task_id。"""
    p = Path(clean_path)
    if not p.exists():
        raise FileNotFoundError(f"空白卷不存在：{p}")
    conn.execute(
        "INSERT INTO daily_task (user_id, task_date, session_slot, task_type, kp_id,"
        " subject_id, title, est_seconds, sort_order, status, clean_image)"
        " VALUES (?, ?, 'PM', 'REVIEW', ?, ?, ?, ?, 0, 'PENDING', ?)",
        (
            user_id,
            local_date().isoformat(),
            kp_id,
            subject_id,
            f"空白卷重做：{p.stem}",
            90,
            str(p),
        ),
    )
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
