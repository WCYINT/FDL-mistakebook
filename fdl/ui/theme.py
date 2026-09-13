"""儿童友好主题 · 最小版（P2-15 / UX-01，S2 削减后）。

§2.3 十原则的最小落地（Q3 决策：UX-03/09 与视觉打磨延后至阶段三）：
- 色板：单一青绿主色 + 同色系深浅表掌握度（原则 8：无红绿对错色，红仅用于系统级提示）
- 字号：正文 ≥18px、行高 1.6（原则 1）
- 动效：单条 ≤300ms（原则 7）
- 文案：全部走 `i18n/zh.yaml` 集中文案表（原则 5/6）
🔴 页面禁止裸写样式值——一律引用本模块常量。
"""

from __future__ import annotations

from pathlib import Path

import yaml

# ── 色板（原则 8：同色系深浅表掌握度；主色 ≤3；红仅系统级）─────────────
PRIMARY = "#1D9E75"  # 青绿主色（探险/成长）
# 掌握度阶梯：浅（待修炼）→ 深（已掌握），同一青绿色系
MASTERY_SCALE = {
    "UNLEARNED": "#DCEDE6",  # 最浅：未学（暗星）
    "LEARNING": "#A8D8C2",
    "REVIEWING": "#6FC2A0",
    "STRUGGLING": "#3FA97D",
    "MASTERED": "#1D9E75",
    "CONSOLIDATED": "#12765A",
    "REGRESSED": "#5B8FA8",  # 蓝-紫系：可"重铸"，不用红
    "ARCHIVED": "#0C4A38",  # 最深：内化完成
}
TEXT = "#1F2A26"  # 正文深色
MUTED = "#5C6B64"  # 次要文字
SURFACE = "#F7FAF8"  # 背景
SYSTEM_ALERT = "#C0392B"  # 🔴 红色唯一用途：系统级提示（网络/SSD 未挂载）

# ── 字号与密度（原则 1：正文 ≥18px、行高 ≥1.6）────────────────────
FONT_BODY_PX = 18
FONT_TITLE_PX = 28
LINE_HEIGHT = 1.6
MAX_TEXT_BLOCKS = 3  # 单任务视图文字块 ≤3
MAX_TEXT_CHARS = 120  # 总字数 ≤120

# ── 动效（原则 7：单条 ≤300ms，无全屏打断）────────────────────────
ANIMATION_MAX_MS = 300
FEEDBACK_MAX_MS = 1000  # 结果判定反馈 <1s

# ── 交互边界（原则 2/4）─────────────────────────────────────────
SINGLE_TASK_MAX_MIN = 15  # 连续交互 ≤15 分钟
SINGLE_QUESTION_MAX_SEC = 90  # 单题 ≤90 秒
REST_HINT_AFTER_MIN = 20  # 停留超 20 分钟中性休息提示
MAX_INPUT_CHARS = 20  # 单次文字输入 ≤20 字

_COPY_PATH = Path(__file__).resolve().parent / "i18n" / "zh.yaml"
_copy_cache: dict | None = None


def load_copy() -> dict:
    """加载集中文案表（i18n/zh.yaml）。"""
    global _copy_cache
    if _copy_cache is None:
        _copy_cache = yaml.safe_load(_COPY_PATH.read_text(encoding="utf-8")) or {}
    return _copy_cache


def t(*keys: str, default: str = "") -> str:
    """文案查取：`t("grade", "again")` → "🔁 再学一遍"。"""
    node: object = load_copy()
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node if isinstance(node, str) else default


def apply_theme() -> None:
    """集中应用主题（唯一允许设置颜色的入口）。"""
    from nicegui import ui

    ui.colors(primary=PRIMARY)
    ui.add_head_html(
        f"<style>body {{ background:{SURFACE}; color:{TEXT};"
        f" font-size:{FONT_BODY_PX}px; line-height:{LINE_HEIGHT}; }}</style>"
    )
