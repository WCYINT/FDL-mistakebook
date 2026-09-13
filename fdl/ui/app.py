"""FDL Frank UI（阶段二最小闭环页）。

P2-15/16/18 落地：最小主题 + 三档评分按钮 + 一键退出。
文案全部来自 `i18n/zh.yaml`（禁页面硬编码 Frank 可见文案）。
UX-03（判别提示）/ UX-09（可预测性）按 Q3 决策延至阶段三。
"""

from __future__ import annotations

import os

from nicegui import ui

from fdl.ui.components import exit_button, grade_buttons
from fdl.ui.theme import apply_theme, t

_last_grades: list[int] = []  # 演示用：记录本 session 评分（正式版接 answer_log 链路）


def handle_grade(grade: int) -> None:
    """评分回调：转发给 SRS 链路（阶段二骨架 = 记录 + 中性反馈）。"""
    _last_grades.append(grade)
    ui.notify(
        t("answer", "correct") if grade >= 1 else t("answer", "retry_invite"),
        type="positive" if grade >= 1 else "info",
    )


def handle_exit() -> None:
    """退出回调：直接结束（原则 10：零挽留、零惩罚、零负面记录）。"""
    ui.notify(t("exit", "farewell"), type="positive")


def build_page() -> None:
    """构建 Frank 首页（最小闭环演示）。"""
    apply_theme()
    with ui.column().classes("items-center w-full mt-12 gap-3"):
        ui.label(t("app", "title")).style("font-size:28px; font-weight:700")
        ui.label(t("home", "greeting")).classes("text-lg")
        ui.separator().classes("w-64 my-2")
        grade_buttons(handle_grade)
        ui.separator().classes("w-64 my-2")
        exit_button(handle_exit)
        ui.label(t("home", "rest_hint")).classes("text-sm text-gray-500")


def main() -> None:
    build_page()
    port = int(os.environ.get("FDL_PORT", "8080"))
    ui.run(title=t("app", "title"), port=port, reload=False, show=False)


if __name__ == "__main__":
    main()
