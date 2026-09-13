"""FDL notes 子包：Markdown + YAML 卡片读写（P2-03 起）。

知识点卡片（kp_card）阶段二落地；错题/探究卡片随阶段三 ING 模块补充。
"""

from fdl_core.notes.frontmatter import CardFormatError, join_card, split_card
from fdl_core.notes.kp_card import (
    CardValidationError,
    KpCard,
    default_schema_path,
    read_card,
    validate_card,
    write_card,
)
from fdl_core.notes.kp_tree import (
    KpNode,
    KpTreeError,
    build_tree,
    count_nodes,
    export_tree_md,
    scan_kp_cards,
    write_kp_tree,
)
from fdl_core.notes.schema_validator import validate

__all__ = [
    "CardFormatError",
    "CardValidationError",
    "KpCard",
    "KpNode",
    "KpTreeError",
    "build_tree",
    "count_nodes",
    "default_schema_path",
    "export_tree_md",
    "join_card",
    "read_card",
    "scan_kp_cards",
    "split_card",
    "validate",
    "validate_card",
    "write_card",
    "write_kp_tree",
]
