"""可复用 UI 组件（P2-16 / P2-18，批次 4）。

- `grade_buttons`：三档动作化评分按钮（UX-02）。语义与 FSRS rating 严格对齐：
  再学一遍=0(Again) / 有点卡=1(Hard) / 搞定=2(Good)；第 4 档 Easy 由系统派生
  （`srs.grade.judge_grade`，不给按钮）。禁负面表情——文案走 i18n/zh.yaml。
- `exit_button`：一键退出（UX-04 / 原则 10）。无挽留弹窗、无罪恶感文案、
  **不写任何惩罚字段**——回调只结束 session，不产生负面记录。
"""

from __future__ import annotations

from collections.abc import Callable

from nicegui import ui

from fdl.ui.theme import t

# 按钮文案 key → grade（🔴 与 FSRS rating 严格对齐）
GRADE_BUTTONS: tuple[tuple[str, int], ...] = (
    ("again", 0),  # 🔁 再学一遍 → Again
    ("hard", 1),  # 🤔 有点卡 → Hard
    ("good", 2),  # ✅ 搞定！ → Good（Easy 由系统派生）
)


def grade_buttons(
    on_grade: Callable[[int], None],
    on_ask_recall: Callable[[], None] | None = None,
) -> ui.row:
    """三档动作化评分按钮；点击回调 `on_grade(grade)`。

    `on_ask_recall`（P2-17 / UX-03）：提供时，Frank 按「有点卡」（grade=1）
    先触发判别提示「不看答案，你能想起来吗？」——Q3 延后项随批次 4 落地。
    """
    row = ui.row().classes("gap-3 w-full justify-center")
    with row:
        for key, grade in GRADE_BUTTONS:

            def _click(g=grade):
                if g == 1 and on_ask_recall is not None:
                    on_ask_recall()
                on_grade(g)

            ui.button(t("grade", key), on_click=lambda _=None, g=grade: _click(g)).classes(
                "text-lg px-6 py-3"
            )
    return row


def predictability_label(index: int, total: int, minutes: int) -> ui.label:
    """P2-20 / UX-09 可预测性（Q3 延后项随批次 4 落地）。

    🔴 D-04：分母 = 当前 session 量（不显示全天总量）；文案走 zh.yaml。
    """
    text = t("session", "predictability").format(index=index, total=total, minutes=minutes)
    return ui.label(text).classes("text-base text-gray-600")


def exit_button(on_exit: Callable[[], None]) -> ui.button:
    """一键退出「今天先到这里」——直接结束，零挽留、零惩罚。

    🔴 原则 10：不弹二次确认、不写惩罚字段、不影响任何成就。
    """
    return ui.button(t("exit", "button"), on_click=on_exit).classes("text-base").props("flat")
