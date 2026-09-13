"""P0 历史数据回填：人工确认错题的 diagnosis_type 真值补链

背景（已实测确认的数据断链）：
  scripts/fdl_serve.py 的 _handle_confirm_mistake() 在人工确认错题时只写 error_type，
  而报告统计口径读的是 diagnosis_type，导致人工确认的记录 diagnosis_type 全落在默认
  值 CALC，真值丢失。本迁移把历史 MANUAL 记录按 error_type 回填 diagnosis_type。

口径（新 4 类 + OTHER），来自 PPT P12：
  CONCEPT / CALC / MISREAD / NORM / OTHER
旧口径（非新 4 类，混入在 error_type 里）跳过，留给人工判断：
  METHOD / CONFUSION / CARELESS

回填规则：
  UPDATE mistake_record SET diagnosis_type = error_type
    WHERE source='MANUAL'
      AND error_type IS NOT NULL AND error_type != ''
      AND error_type IN (新5类)
      AND (diagnosis_type IS NULL OR diagnosis_type='CALC')

幂等：可重复执行不产生副作用——回填后 diagnosis_type = error_type（新5类值），
  不再命中 (diagnosis_type IS NULL OR 'CALC')，故二次运行 UPDATE 影响 0 行。

安全：执行前先备份生产库到 <原路径>.bak-p0-<时间戳>（WAL checkpoint 后再 cp，
  保证备份包含未落盘 WAL 数据）。
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from fdl_core.paths import get_paths

# 新 5 类错因口径（回填白名单）
_NEW_CALIBER = ("CONCEPT", "CALC", "MISREAD", "NORM", "OTHER")
# 旧口径，混入在 error_type 里，不是新 4 类，跳过留给人工判断
_OLD_CALIBER = ("METHOD", "CONFUSION", "CARELESS")


def migrate(db_path: str | Path | None = None) -> dict:
    db_path = str(db_path or get_paths().primary_db_path)
    summary: dict = {
        "db_path": db_path,
        "steps": [],
        "errors": [],
        "backfilled": 0,
        "eligible_new": 0,
        "skipped_old": 0,
        "skipped_old_samples": [],
        "backup": None,
    }

    # 步骤 1：WAL checkpoint 后备份生产库（先备份再改）
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak-p0-{ts}"
    try:
        # checkpoint 把 WAL 未落盘数据写回主库，保证 cp 出的备份一致
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
        cols = {r[1] for r in conn.execute("PRAGMA table_info(mistake_record)").fetchall()}
        if "diagnosis_type" not in cols:
            summary["errors"].append(
                "diagnosis_type 列不存在，请先跑 2026_09_09_add_diagnosis_type"
            )
            return summary

        # 步骤 2：统计待回填（新5类）与待跳过（旧口径）数量
        eligible = conn.execute(
            """SELECT COUNT(*) FROM mistake_record
                 WHERE source='MANUAL'
                   AND error_type IS NOT NULL AND error_type != ''
                   AND error_type IN ({})
                   AND (diagnosis_type IS NULL OR diagnosis_type='CALC')""".format(
                ",".join("?" * len(_NEW_CALIBER))
            ),
            _NEW_CALIBER,
        ).fetchone()[0]
        summary["eligible_new"] = eligible

        old_rows = conn.execute(
            """SELECT id, error_type, diagnosis_type FROM mistake_record
                 WHERE source='MANUAL'
                   AND error_type IS NOT NULL AND error_type != ''
                   AND error_type IN ({})
                   AND (diagnosis_type IS NULL OR diagnosis_type='CALC')""".format(
                ",".join("?" * len(_OLD_CALIBER))
            ),
            _OLD_CALIBER,
        ).fetchall()
        summary["skipped_old"] = len(old_rows)
        summary["skipped_old_samples"] = [
            {"id": r[0], "error_type": r[1], "diagnosis_type": r[2]} for r in old_rows[:10]
        ]
        summary["steps"].append(f"found {eligible} 条新5类可回填；{len(old_rows)} 条旧口径跳过")

        # 步骤 3：回填（仅新5类）
        cur = conn.execute(
            """UPDATE mistake_record SET diagnosis_type = error_type
                 WHERE source='MANUAL'
                   AND error_type IS NOT NULL AND error_type != ''
                   AND error_type IN ({})
                   AND (diagnosis_type IS NULL OR diagnosis_type='CALC')""".format(
                ",".join("?" * len(_NEW_CALIBER))
            ),
            _NEW_CALIBER,
        )
        conn.commit()
        summary["backfilled"] = cur.rowcount
        summary["steps"].append(f"backfilled {cur.rowcount} 行")

        # 校验：剩余仍落在 CALC 的 MANUAL 记录（不含本就为 CALC 真值的）
        remain = conn.execute(
            """SELECT COUNT(*) FROM mistake_record
                 WHERE source='MANUAL' AND diagnosis_type='CALC'
                   AND error_type IS NOT NULL AND error_type != ''
                   AND error_type NOT IN ({})""".format(",".join("?" * len(_NEW_CALIBER))),
            _NEW_CALIBER,
        ).fetchone()[0]
        summary["remaining_ambiguous_cal_K"] = remain
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
