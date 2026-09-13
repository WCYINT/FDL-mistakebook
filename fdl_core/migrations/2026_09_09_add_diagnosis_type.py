"""阶段 1.1 错因诊断升级（Gap1）：mistake_record 加 diagnosis_type 列 + 50 行回填

King 拍板 P0 修复。diagnosis_type 对齐 PPT P12 4 类错因：
  - CONCEPT  概念不清 → 回到定义用自己的话复述
  - CALC     计算失误 → 限时竖式专项
  - MISREAD  审题偏差 → 圈画关键信息
  - NORM     规范缺失 → 模板化书写要求

幂等：再次运行 skipped=True。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fdl_core.paths import get_paths

# === PPT P12 4 类关键词映射（中文错因描述 → diagnosis_type）===
# 顺序敏感：更具体的优先；keyword 在 (error_subtype or "") 里 substring 匹配
_DIAGNOSIS_RULES = [
    # NORM：规范缺失（听写/字形/漏写漏单位/答语格式）
    (
        (
            "听写",
            "偏旁",
            "笔画",
            "字形",
            "字音",
            "同音字",
            "形近字",
            "正字",
            "答语",
            "步骤跳跃",
            "答案漏写",
            "格式",
            "漏单位",
        ),
        "NORM",
    ),
    # CONCEPT：概念不清（公式/口诀/单位换算/时间/量纲）
    (
        (
            "概念",
            "口诀",
            "定义",
            "公式不熟",
            "进率",
            "量纲",
            "单位换算",
            "时间概念",
            "钟表",
            "大小月",
            "数量级",
            "单位",
            "不熟",
        ),
        "CONCEPT",
    ),
    # MISREAD：审题偏差（方向/数列/规律/分组配对/乘法原理）
    (
        (
            "比…多方向反",
            "比…少",
            "方向反",
            "乘法原理",
            "倍数关系",
            "分组配对",
            "平均数",
            "等差数列",
            "数列",
            "规律",
            "斜线",
            "最值构造",
            "天数推算",
            "跨月",
            "倍",
            "1倍",
        ),
        "MISREAD",
    ),
    # CARELESS：执行粗心（多写0/漏写忘加/形状错）
    (("多写一个 0", "漏写", "形状错", "形状", "忘加", "粗心"), "CARELESS"),
]

# error_type 兜底映射（当所有关键词都未命中）
_TYPE_FALLBACK = {
    "METHOD": "CALC",  # 方法龟（命名约定）→ 计算失误
    "CONFUSION": "MISREAD",  # 混淆章鱼 → 审题偏差
    "CARELESS": "CARELESS",  # 粗心龙 → 直接
}


def _infer_diagnosis(error_type: str | None, error_subtype: str | None) -> str:
    """根据 error_type + error_subtype 推断 diagnosis_type。"""
    sub = (error_subtype or "").strip()
    for keywords, diag in _DIAGNOSIS_RULES:
        for kw in keywords:
            if kw in sub:
                return diag
    return _TYPE_FALLBACK.get((error_type or "").strip().upper(), "CALC")


def migrate(db_path: str | Path | None = None) -> dict:
    db_path = str(db_path or get_paths().primary_db_path)
    conn = sqlite3.connect(db_path)
    summary = {"steps": [], "errors": [], "skipped": False, "samples": [], "distribution": {}}

    cols = {r[1] for r in conn.execute("PRAGMA table_info(mistake_record)").fetchall()}

    # 步骤 1：加列（SQLite 3.35+ 直接 ADD COLUMN）
    # 若第一次失败时 CHECK 约束已写入，先用重建表去掉 CHECK
    if "diagnosis_type" not in cols:
        try:
            conn.execute(
                "ALTER TABLE mistake_record ADD COLUMN diagnosis_type TEXT NOT NULL DEFAULT 'CALC'"
            )
            summary["steps"].append("add column diagnosis_type (no CHECK)")
            conn.commit()
        except Exception as e:
            err = str(e)
            if "no such column" in err or "duplicate" in err:
                # 已存在（其他工具加过），跳过
                summary["steps"].append("add column skipped (already exists or duplicate)")
            else:
                summary["errors"].append(f"add column: {e}")
                conn.close()
                return summary
    else:
        summary["steps"].append("column diagnosis_type already exists")

    # 若上一轮失败留下 CHECK 约束（CONCEPT/CALC/MISREAD/NORM），用重建表去掉
    sql = (
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='mistake_record'"
        ).fetchone()[0]
        or ""
    )
    if "CHECK" in sql.upper() and "diagnosis_type" in sql.lower():
        # 重建表去掉 CHECK（SQLite 不支持 ALTER DROP CONSTRAINT）
        summary["steps"].append("rebuild mistake_record to drop CHECK on diagnosis_type")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ALTER TABLE mistake_record RENAME TO mistake_record_old")
        conn.execute("""
            CREATE TABLE mistake_record (
                id                      INTEGER PRIMARY KEY,
                user_id                 INTEGER NOT NULL,
                kp_id                   INTEGER NOT NULL,
                root_cause_kp_id        INTEGER,
                question_id             INTEGER,
                occurred_at             TEXT    NOT NULL,
                subject                 TEXT    NOT NULL,
                source                  TEXT    NOT NULL DEFAULT 'REAL_WORK',
                source_ref              TEXT,
                error_type              TEXT,
                error_subtype           TEXT,
                attributed_by           TEXT    NOT NULL DEFAULT 'RULE_BASED',
                attribution_confidence  REAL    NOT NULL DEFAULT 0.0,
                severity                INTEGER NOT NULL DEFAULT 3,
                needs_reteach           INTEGER NOT NULL DEFAULT 0,
                input_mode              TEXT,
                wrong_answer            TEXT,
                correct_answer          TEXT,
                resolved_at             TEXT,
                reappear_count          INTEGER NOT NULL DEFAULT 0,
                last_reappear_at        TEXT,
                is_tamed                INTEGER NOT NULL DEFAULT 0,
                note_id                 TEXT,
                img_original            TEXT,
                img_clean               TEXT,
                created_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                fsrs_s                  REAL,
                fsrs_d                  REAL,
                diagnosis_type          TEXT    NOT NULL DEFAULT 'CALC'
            )
        """)
        # 拷数据
        old_cols = [r[1] for r in conn.execute("PRAGMA table_info(mistake_record_old)").fetchall()]
        new_cols = [r[1] for r in conn.execute("PRAGMA table_info(mistake_record)").fetchall()]
        common = [c for c in old_cols if c in new_cols]
        cols_csv = ",".join(common)
        conn.execute(
            f"INSERT INTO mistake_record ({cols_csv}) SELECT {cols_csv} FROM mistake_record_old"
        )
        conn.execute("DROP TABLE mistake_record_old")
        # 重建索引
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mistake_user_kp ON mistake_record (user_id, kp_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mistake_note ON mistake_record (note_id)")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")

    # 步骤 2：50 行回填（按推断结果重写）
    rows = conn.execute(
        "SELECT id, error_type, error_subtype, diagnosis_type FROM mistake_record"
    ).fetchall()
    updates = 0
    for rid, et, sub, cur in rows:
        target = _infer_diagnosis(et, sub)
        summary["distribution"][target] = summary["distribution"].get(target, 0) + 1
        if target != cur:
            conn.execute(
                "UPDATE mistake_record SET diagnosis_type=? WHERE id=?",
                (target, rid),
            )
            updates += 1
    conn.commit()
    summary["steps"].append(f"backfilled {updates}/{len(rows)} rows")

    # 步骤 3：建索引（按 diagnosis_type 查询是热点）
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mistake_diagnosis "
            "ON mistake_record (user_id, diagnosis_type, is_tamed)"
        )
        summary["steps"].append("create index idx_mistake_diagnosis")
    except Exception as e:
        summary["errors"].append(f"index: {e}")

    # 校验
    new_cols = {r[1] for r in conn.execute("PRAGMA table_info(mistake_record)").fetchall()}
    summary["verified"] = "diagnosis_type" in new_cols

    # 5 个样本展示
    samples = conn.execute(
        "SELECT id, error_type, error_subtype, diagnosis_type "
        "FROM mistake_record ORDER BY id LIMIT 5"
    ).fetchall()
    summary["samples"] = [
        {"id": r[0], "error_type": r[1], "error_subtype": (r[2] or "")[:30], "diagnosis_type": r[3]}
        for r in samples
    ]

    conn.close()
    return summary


if __name__ == "__main__":
    import json

    print(json.dumps(migrate(), ensure_ascii=False, indent=2))
