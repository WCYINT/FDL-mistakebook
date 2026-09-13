"""Markdown + YAML frontmatter 解析与生成（P2-03 / KB-02）。

卡片格式（PRD §5.6）::

    ---
    type: knowledge_point
    code: MATH-G4-FRAC-ADD
    ...
    ---

    # 正文标题

往返无损（语义级）：`split → join → split` 两次结果一致。
frontmatter 中的 YAML 日期字面量（如 `valid_from: 2026-09-01`）会被规范化为
ISO-8601 字符串，保证 dict 可比、可序列化、可 JSON Schema 校验。
"""

from __future__ import annotations

from datetime import date, datetime

import yaml

_DELIMITER = "---"


class CardFormatError(ValueError):
    """卡片 frontmatter 格式错误。"""


def _normalize(value):
    """递归规范化：date/datetime → ISO 字符串，保持 dict/list 结构。"""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def _normalize_body(body: str) -> str:
    """正文规范化：去前导空行、尾部恰一个换行（空正文 → 空串）。

    保证 `split → join → split` 在文件级换行差异下仍语义一致。
    """
    body = body.strip("\n")
    return body + "\n" if body else ""


def split_card(text: str) -> tuple[dict, str]:
    """解析卡片文本 → `(frontmatter, body)`。

    - 首行必须为 `---`；第二个 `---` 行闭合 frontmatter，其余为正文。
    - frontmatter 必须是 YAML 映射（dict），否则抛 `CardFormatError`。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _DELIMITER:
        raise CardFormatError("卡片必须以 '---' 开头")
    for i in range(1, len(lines)):
        if lines[i].strip() == _DELIMITER:
            yaml_text = "\n".join(lines[1:i])
            body = _normalize_body("\n".join(lines[i + 1 :]))
            fm = yaml.safe_load(yaml_text) or {}
            if not isinstance(fm, dict):
                raise CardFormatError("frontmatter 必须是 YAML 映射（key: value）")
            return _normalize(fm), body
    raise CardFormatError("frontmatter 未闭合（缺少第二个 '---'）")


def join_card(frontmatter: dict, body: str) -> str:
    """生成卡片文本（`join_card(*split_card(text))` 与 text 语义一致）。"""
    yaml_text = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return f"{_DELIMITER}\n{yaml_text}{_DELIMITER}\n{_normalize_body(body)}"
