"""建立「归因类别动态化」三表并迁入 v1 种子（King 2026-09-12 拍板）。

背景（为什么要做）
------------------
King 要求：错因归因不能固定为预设类别，需支持「持续细化与迭代，随时间和分析
逻辑的精细化动态演进」。但现状是**两处硬编码**、彼此词汇还不一致：

  1. `fdl_core/mistakes/attribution.py:MONSTERS`（8 类，服务 error_type 旧口径）
     CARELESS/CONFUSION/MISREAD/METHOD/EXPRESSION/FORGOT/TIMEOUT/OTHER
  2. `fdl_core/srs/queue_scheduler.py:DIAG_WEIGHT`（6 类，服务 diagnosis_type 新口径）
     CONCEPT 0.30 / MISREAD 0.20 / CALC 0.10 / NORM 0.10 / CARELESS 0.05 / OTHER 0.00

后果：想细化或新增一个归因类别，必须改代码 + 发版，与「动态演进」背道而驰。

本迁移做什么
------------
1. 建三张表（DDL 已在 `fdl_core/db/schema.py`，此处调 create_schema 幂等落地）：
   - `attribution_taxonomy`  类别主表（树形 parent_code + version + status + weight）
   - `attribution_proposal`  LLM 归因提案 + 审计链（不直接改权威字段）
   - `taxonomy_candidate`    新类别候选（LLM 提候选 + 人工确认晋级）
2. 把上面**两套口径的并集**作为 v1 种子写入 `attribution_taxonomy`（is_seed=1）。
   - weight 有权威来源的（DIAG_WEIGHT）按原值写入，不乱编；
   - 仅存在于旧口径、从未定过权的（CONFUSION/METHOD/EXPRESSION/FORGOT/TIMEOUT）
     weight 写 0.0，description 明确标注「旧口径，暂未定价」——**不臆造权重**。

为什么不直接把 MONSTERS / DIAG_WEIGHT 删掉
------------------------------------------
他们是「当前实际生效的权威值」。本迁移只做「种入数据表」，读取侧改为
「优先读表、读不到再降级到内置常量」，保证：老库、老测试、离线场景都不受影响。
彻底移除硬编码常量是后续步骤。

幂等性
------
- create_schema 全是 IF NOT EXISTS；
- 种子写入用 `INSERT ... ON CONFLICT(code) DO NOTHING`；
- 重复执行不会覆盖人工已调整过的 weight/version。

安全
----
执行前先 WAL checkpoint 再 cp 主文件到 `<原路径>.bak-attr-tax-<时间戳>`。
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from fdl_core.paths import get_paths

# v1 种子的**单一事实源**在 fdl_core/mistakes/attribution_taxonomy.py:SEED_TAXONOMY。
# 本迁移不复制一份常量——否则两处会随时间漂移（迁移改一次、模块改一次）。
# 种子写入全部走 ON CONFLICT DO NOTHING，因此：
#   - 老库：迁移首次执行时补种；
#   - 新库：create_schema 后由 ensure_seeded() 补种；
#   - 人工调过的行：任何路径都不会被覆盖。


def migrate(db_path: str | Path | None = None) -> dict:
    """建三表 + 迁入 v1 种子。返回结构化结果（runner 据此判成败）。"""
    db_path = str(db_path or get_paths().primary_db_path)
    summary: dict = {
        "db_path": db_path,
        "steps": [],
        "errors": [],
        "backup": None,
        "seeded": 0,
        "taxonomy_total": 0,
    }

    # 步骤 1：WAL checkpoint 后备份（先备份再改）
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak-attr-tax-{ts}"
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
        summary["steps"].append(
            "三表已就绪（attribution_taxonomy / attribution_proposal / taxonomy_candidate）"
        )

        # 步骤 3：幂等迁入 v1 种子（单一事实源在 attribution_taxonomy.SEED_TAXONOMY）
        from fdl_core.mistakes.attribution_taxonomy import ensure_seeded

        summary["seeded"] = ensure_seeded(conn)
        summary["steps"].append(f"种子写入 {summary['seeded']} 条（已存在则跳过）")

        summary["taxonomy_total"] = conn.execute(
            "SELECT COUNT(*) FROM attribution_taxonomy"
        ).fetchone()[0]
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        summary["errors"].append(f"建表/种子失败：{type(exc).__name__}: {exc}")
    finally:
        conn.close()

    return summary


if __name__ == "__main__":
    import json

    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
