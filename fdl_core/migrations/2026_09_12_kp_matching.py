"""KP 挂载体系 Phase 1：建两表（kp_match_proposal / kp_candidate）。

背景（2026-09-12 实测确认）
--------------------------
错题挂载知识点的链路此前**两处断裂**，导致挂载率长期 0%（0 / 55）：

1. 后端：`scripts/fdl_serve.py` 的 confirm 端点 INSERT 列含 `kp_id`，
   但 VALUES 里硬编码 `0`——**不读请求参数**。
2. 前端：确认表单（cf-qno/stem/optA-D/answer/correct/reason/explain）
   **没有知识点选择控件**。

King 拍板（方案文档 docs/research/KP挂载体系落地方案-2026-09-12.md）：
知识点匹配改为**由 LLM 自动提炼**——复用归因引擎架构（双跑一致闸门 +
提案审计链 + 候选人工晋级），不依赖人工圈选。

本迁移做什么
------------
1. 建两张表（DDL 已在 `fdl_core/db/schema.py`，此处调 create_schema 幂等落地）：
   - `kp_match_proposal`  挂载提案 + 审计链（同 attribution_proposal 模式）
   - `kp_candidate`       新知识点候选（LLM 提候选 + 人工确认晋级）
2. 无种子数据（知识点主表 knowledge_point 已有 27 行，无需种）。

幂等性
------
create_schema 全为 IF NOT EXISTS；重复执行零副作用。

安全
----
执行前先 WAL checkpoint 再 cp 主文件到 `<原路径>.bak-kp-match-<时间戳>`。
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from fdl_core.paths import get_paths


def migrate(db_path: str | Path | None = None) -> dict:
    """建两表。返回结构化结果（runner 据此判成败）。"""
    db_path = str(db_path or get_paths().primary_db_path)
    summary: dict = {
        "db_path": db_path,
        "steps": [],
        "errors": [],
        "backup": None,
        "tables_ready": [],
    }

    # 步骤 1：WAL checkpoint 后备份（先备份再改）
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak-kp-match-{ts}"
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
        # 备份失败不阻断（测试库/临时库场景），但如实记录
        summary["errors"].append(f"备份失败（继续执行）：{exc}")
        summary["backup"] = None

    # 步骤 2：建表（幂等；DDL 单一来源在 schema.py）
    from fdl_core.db.schema import create_schema, get_connection

    conn = get_connection(db_path)
    try:
        create_schema(conn)
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("kp_match_proposal", "kp_candidate"):
            if t in have:
                summary["tables_ready"].append(t)
        summary["steps"].append(f"两表已就绪：{summary['tables_ready']}")
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        summary["errors"].append(f"建表失败：{type(exc).__name__}: {exc}")
    finally:
        conn.close()

    return summary


if __name__ == "__main__":
    import json

    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
