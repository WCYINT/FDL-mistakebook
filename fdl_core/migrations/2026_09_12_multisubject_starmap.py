"""Phase 1 多科地基迁移（2026-09-12）：kp_relation 建表 + 四科基线登记。

背景
----
"学习星图"要支持科目维度（语文/数学/英语/科学 + 细分领域），但：
- `subject` 表只有 MATH/CHINESE 两行 → 缺 ENGLISH/SCIENCE；
- 跨学科关联无边表（`kp_prerequisite` 专属"学习先决"且被 kp_gate 当门禁，
  不能混入类比/应用语义）→ 需新增 `kp_relation`。

本迁移做什么
------------
1. `create_schema()`：幂等建 `kp_relation` 表（DDL 在 schema.py，含
   `CHECK (from_kp_id < to_kp_id)` 无向边规范化约束 + UNIQUE 去重）。
2. `ensure_canonical_subjects()`：登记四科基线（MATH/CHINESE 已存在 → 跳过；
   ENGLISH/SCIENCE 新建）。幂等：重复执行不重复建。

不做的事
--------
- 不删除/修改任何既有 KP 与 subject 行；
- 不写 kp_relation 边（连边是 Phase 3，且需 LLM 推断 + 人工复核）。

安全
----
执行前 WAL checkpoint 再 cp 主文件到 `<原路径>.bak-multisubject-<时间戳>`。
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
        "backup": None,
        "tables_ready": [],
        "subjects_created": [],
        "subjects_existing": [],
    }

    # 步骤 1：备份
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak-multisubject-{ts}"
    try:
        ck = sqlite3.connect(db_path)
        try:
            ck.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            ck.close()
        shutil.copyfile(db_path, backup_path)
        summary["backup"] = backup_path
        summary["steps"].append(f"已备份 → {backup_path}")
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"备份失败（继续执行）：{exc}")

    # 步骤 2：建表（幂等）
    from fdl_core.db.schema import create_schema, get_connection

    conn = get_connection(db_path)
    try:
        create_schema(conn)
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "kp_relation" in have:
            summary["tables_ready"].append("kp_relation")
        summary["steps"].append(f"kp_relation 就绪：{'是' if 'kp_relation' in have else '否'}")

        # 步骤 3：学科基线
        from fdl_core.notes.kp_classifier import ensure_canonical_subjects

        r = ensure_canonical_subjects(conn)
        summary["subjects_created"] = r["created"]
        summary["subjects_existing"] = r["existing"]
        summary["steps"].append(f"四科基线：新建 {r['created'] or '无'} / 已存在 {r['existing']}")
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        summary["errors"].append(f"迁移失败：{type(exc).__name__}: {exc}")
    finally:
        conn.close()
    return summary


if __name__ == "__main__":
    import json

    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
