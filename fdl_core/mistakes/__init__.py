"""FDL 归因子包（阶段三 MS）。"""

from fdl_core.mistakes.attribution import (
    ATTRIBUTION_GRADE_MAP,
    MONSTERS,
    attribution_to_grade,
    run_attribution_chain,
    validate_monster_tag,
)
from fdl_core.mistakes.auto_capture import CapturedMistake, capture_from_answer
from fdl_core.mistakes.dual_write import link_note, verify_dual_write
from fdl_core.mistakes.review import mark_reviewed
from fdl_core.mistakes.tables import ensure_mistake_table

__all__ = [
    "ATTRIBUTION_GRADE_MAP",
    "CapturedMistake",
    "MONSTERS",
    "attribution_to_grade",
    "capture_from_answer",
    "ensure_mistake_table",
    "link_note",
    "mark_reviewed",
    "run_attribution_chain",
    "validate_monster_tag",
    "verify_dual_write",
]
