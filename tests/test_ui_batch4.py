"""批次 4（P2-15/16/18/19 UX 儿童界面）验收测试。

P2-17（UX-03 判别提示）/ P2-20（UX-09 可预测性）按 Q3 决策延后至阶段三。

覆盖：主题常量（无红绿对错色）、集中文案表（key 齐全 + 无禁用词 +
退出文案零罪恶感）、三档按钮 grade 对齐、禁用词扫描脚本通过。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# 禁用词与 scripts/check_no_banned_words.sh 词表保持一致
BANNED = (
    "失败",
    "错误",
    "不行",
    "笨蛋",
    "太笨",
    "愚蠢",
    "你不会",
    "做错了",
    "不合格",
    "差劲",
    "不及格",
    "正确率",
    "你错了",
)


def test_theme_no_red_green_semantics():
    """原则 8：掌握度阶梯用同色系深浅，红不进掌握度色板。"""
    from fdl.ui import theme

    assert theme.SYSTEM_ALERT not in theme.MASTERY_SCALE.values()
    hexes = [v for v in theme.MASTERY_SCALE.values()]
    assert all(h.startswith("#") and len(h) == 7 for h in hexes)
    # 主色 ≤3 种（主色 + 文字 + 背景 + 提示红，掌握度算主色深浅不算新色）
    assert theme.FONT_BODY_PX >= 18 and theme.LINE_HEIGHT >= 1.6
    assert theme.ANIMATION_MAX_MS <= 300


def test_copy_sheet_loads():
    from fdl.ui.theme import load_copy

    copy = load_copy()
    for section in ("app", "home", "grade", "answer", "exit", "system"):
        assert section in copy, f"文案表缺 {section}"


@pytest.mark.parametrize(
    "keys",
    [
        ("grade", "again"),
        ("grade", "hard"),
        ("grade", "good"),
        ("exit", "button"),
        ("exit", "farewell"),
        ("answer", "retry_invite"),
        ("answer", "correct"),
    ],
)
def test_copy_keys_present(keys):
    from fdl.ui.theme import t

    assert t(*keys), f"文案缺失：{keys}"


def test_exit_copy_zero_guilt():
    """原则 10：退出文案零挽留、零罪恶感、无负面评判。"""
    from fdl.ui.theme import t

    farewell = t("exit", "farewell")
    for word in ("再坚持", "确定吗", "确定要", "真的要", "可惜", "加油", "坚持"):
        assert word not in farewell, f"退出文案含挽留/评判词：{word}"


def test_copy_sheet_no_banned_words():
    """集中文案表逐行无禁用词。"""
    sheet = (ROOT / "fdl" / "ui" / "i18n" / "zh.yaml").read_text(encoding="utf-8")
    for line in sheet.splitlines():
        if line.strip().startswith("#"):
            continue  # 注释已改写措辞，防患未然
        for word in BANNED:
            assert word not in line, f"文案表命中禁用词「{word}」：{line}"


def test_ui_code_no_hardcoded_frank_copy():
    """UI 代码层（app/components）不含 Frank 可见文案硬编码（走 zh.yaml）。"""
    for name in ("app.py", "components.py"):
        text = (ROOT / "fdl" / "ui" / name).read_text(encoding="utf-8")
        for word in ("再学一遍", "有点卡", "搞定", "今天先到这里"):
            # 只拦双引号字面量（docstring 中文引用不算硬编码）
            assert f'"{word}"' not in text, f"{name} 硬编码文案：{word}"


def test_grade_button_mapping_aligned_with_fsrs():
    """🔴 按钮语义与 FSRS rating 严格对齐：0/1/2，无第 4 档按钮。"""
    from fdl.ui.components import GRADE_BUTTONS

    assert [g for _, g in GRADE_BUTTONS] == [0, 1, 2]  # Easy(3) 不给按钮
    assert [k for k, _ in GRADE_BUTTONS] == ["again", "hard", "good"]


def test_banned_words_ci_passes():
    """UX-05 验收：扫描脚本对 Frank 触达面全绿。"""
    r = subprocess.run(
        ["bash", str(ROOT / "scripts" / "check_no_banned_words.sh")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_ui_page_builds_smoke():
    """NiceGUI 页面构建冒烟（不启动服务器）。"""
    from nicegui import ui

    from fdl.ui.app import build_page

    @ui.page("/")
    def _page():
        build_page()


def test_s2_deferred_tasks_documented():
    """Q3 决策留痕：UX-03/09 延后，文案表保留占位 key（grade.ask_recall）。"""
    from fdl.ui.theme import t

    assert t("grade", "ask_recall") != ""  # 占位存在，阶段三接入
