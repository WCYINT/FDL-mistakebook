"""FDL 数据层子包（L0 核心层）。

P2-01 起引入 SQLite 建表与连接管理；后续 KB 读写（P2-03）、
调度持久化（P2-06）等均挂载于此。
"""

from fdl_core.db.schema import (
    TABLE_NAMES,
    create_schema,
    get_connection,
    table_names,
)

__all__ = [
    "TABLE_NAMES",
    "create_schema",
    "get_connection",
    "table_names",
]
