"""MS-02 行为化归因重试链 + MS-03 怪兽标签 + MS-04 FSRS 映射。

🔴 三层判定链（PRD §3.3 DFD-2）：
  首次答错 → 🔴 不看答案先检查一遍（可随时跳过）
  ├ 检查后改对 → 归因【失误】→ Good（不重置间隔，记入"我的粗心档案"，仅 Frank 可见）
  ├ 提示层2（给关键步骤）→ 提示下做对 → 归因【提示下可解】→ Hard（重置短间隔）
  └ 提示下仍错 / 跳过 → 归因【真不会】→ Again（完整重置，知识点标待学）

🔴 触发纪律：只在**首次答错**时触发一次，且可跳过；单次答错增加 30–60 秒。
🔴 D-05 双轨裁定：行为链派生的 grade 写 answer_log（调度唯一事实源）；
   Frank 点选怪兽标签写 mistake_record.error_type（元认知+图鉴）——独立记录不互覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── MS-03 七类怪兽 + OTHER（数据层 enum；VIS 图标阶段四用 SVG，禁 emoji）──
MONSTERS: dict[str, str] = {
    "CARELESS": "粗心龙",
    "CONFUSION": "混淆章鱼",
    "MISREAD": "审题鹰",
    "METHOD": "方法龟",
    "EXPRESSION": "表达鹦鹉",
    "FORGOT": "遗忘幽灵",
    "TIMEOUT": "时间蜘蛛",
    "OTHER": "其他",
}
# 2026-09-12 King 拍板新增 LLM_ASSISTED（LLM 归因引擎 attribution_engine.py 用）：
#   语义 = "该归因由 LLM 分析产出、经闸门（高置信自动 / 低置信人工确认）放行"，
#   完整审计链落在 attribution_proposal（哪次反馈 / 哪个模型 / 什么理由）。
# 2026-09-12 King 拍板纳入 VISION_ANALYSIS：VLM 录入通道（ingest/vlm.py）早已在
#   生产数据里写这个值（实测 11+ 条，conf 0.7~0.9），但枚举一直没收——
#   导致"代码纪律与数据矛盾"（《FDL优化路线图》既列事项），任何校验都会误拒真数据。
# 🔴 仍然没有 LLM_SUGGEST——它表示"未经任何验证的裸建议"，PRD §4.2.2 的禁令不变，
#    tests/test_batch34_t3.py 的既有断言（拒绝 LLM_SUGGEST）继续成立。
ATTRIBUTED_BY_VALUES = (
    "FRANK",
    "PARENT",
    "RULE_BASED",
    "LLM_ASSISTED",
    "VISION_ANALYSIS",
)


@dataclass
class AttributionOutcome:
    """三层链的归因结果。"""

    outcome: str  # MISTAKE_FIXED / SOLVED_WITH_HINT / TRULY_CANT
    label: str  # 中文：失误 / 提示下可解 / 真不会
    grade: int  # MS-04 映射：2 / 1 / 0（FSRS rating，调度唯一事实源）
    escalated: bool  # 是否走完了提示层（False=检查层就改对）


def run_attribution_chain(
    *,
    check_fixed: bool,
    hint_fixed: bool,
    skipped: bool = False,
) -> AttributionOutcome:
    """三层判定链（纯函数，供作答 UI/CLI 调用）。

    - `check_fixed`：不看答案检查一遍后改对 →【失误】
    - `hint_fixed`：提示层2 给关键步骤后做对 →【提示下可解】
    - `skipped`：跳过检查（可随时跳过）→ 直接【真不会】
    - 只在首次答错时触发一次（调用方保证），本函数不判断"首次"。
    """
    if check_fixed:
        return AttributionOutcome("MISTAKE_FIXED", "失误", 2, escalated=False)
    if not skipped and hint_fixed:
        return AttributionOutcome("SOLVED_WITH_HINT", "提示下可解", 1, escalated=True)
    return AttributionOutcome("TRULY_CANT", "真不会", 0, escalated=not skipped)


# ── MS-04 归因 → FSRS Rating 映射 ──────────────────────────
# 概念不清/公式记错/完全不会 → Again；计算失误/审题错误/书写不规范 → Good；
# 时间不够 → Hard。Anki 官方警告：FSRS 唯一无法适应的坏习惯是"忘了却按 Hard"。
ATTRIBUTION_GRADE_MAP: dict[str, int] = {
    "MISTAKE_FIXED": 2,  # Good
    "SOLVED_WITH_HINT": 1,  # Hard
    "TRULY_CANT": 0,  # Again
}


def attribution_to_grade(outcome: str) -> int:
    """归因结果 → FSRS rating（严格映射，防"忘了按 Hard"）。"""
    if outcome not in ATTRIBUTION_GRADE_MAP:
        raise ValueError(f"未知归因结果：{outcome}")
    return ATTRIBUTION_GRADE_MAP[outcome]


def validate_monster_tag(error_type: str, attributed_by: str) -> None:
    """怪兽标签入库前校验：7 类 + OTHER；attributed_by 无 LLM_SUGGEST。"""
    if error_type not in MONSTERS:
        raise ValueError(f"未知怪兽标签：{error_type}（允许：{sorted(MONSTERS)}）")
    if attributed_by not in ATTRIBUTED_BY_VALUES:
        raise ValueError(
            "attributed_by 非法（🔴 无 LLM_SUGGEST）："
            f"{attributed_by}（允许：{ATTRIBUTED_BY_VALUES}）"
        )
