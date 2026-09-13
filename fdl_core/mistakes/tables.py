"""MS-01 错题模型：`mistake_record` 表（P1 表提前落地，ING-04 前置）。

🔴 Markdown 是人读真相源（§5.6.2 错题卡），SQLite 是机读存储；
`note_id` 双向索引（G14 双写一致性的锚点）。

建表 DDL 统一收口到 `fdl_core.db.schema.create_schema`（单一事实源）：
本函数仅确保 schema 存在（幂等），避免与 schema.py 的 DDL 分叉——
旧版 ensure_mistake_table 自建 DDL 缺 fsrs_s/fsrs_d 列，会破坏 A2 路径。
"""

from __future__ import annotations

from fdl_core.db.schema import create_schema


def ensure_mistake_table(conn) -> None:
    """确保 mistake_record（及全量 FDL schema）存在（幂等，单一 DDL 源）。"""
    create_schema(conn)
