"""指标层建表（P2-13 补 P1 表：daily_metric / weekly_metric）。

建表 DDL 统一收口到 `fdl_core.db.schema.create_schema`（单一事实源）：
本函数仅确保 schema 存在（幂等），避免与 schema.py 的 DDL 分叉
（daily_metric / weekly_metric 改一处漏一处的风险）。
"""

from __future__ import annotations

from fdl_core.db.schema import create_schema


def ensure_metric_tables(conn) -> None:
    """确保 daily_metric / weekly_metric（及全量 FDL schema）存在（幂等，单一 DDL 源）。"""
    create_schema(conn)
