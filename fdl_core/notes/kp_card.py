"""知识点卡片读写（P2-03 / KB-02）。

PRD §5.6.1 格式；写入前强制 JSON Schema 校验。
🔴 硬验收：**改 YAML（schema JSON）不改代码**——调整卡片格式约束只改
`config/schema/kp_card.schema.json`，校验逻辑零改动。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from fdl_core.notes.frontmatter import join_card, split_card
from fdl_core.notes.schema_validator import validate
from fdl_core.paths import get_paths


class CardValidationError(ValueError):
    """卡片 schema 校验失败（写入被拦截）。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("卡片校验失败：" + "；".join(errors))


@dataclass
class KpCard:
    """知识点卡片：frontmatter（YAML dict）+ 正文（Markdown str）。"""

    frontmatter: dict
    body: str

    @classmethod
    def from_text(cls, text: str) -> KpCard:
        fm, body = split_card(text)
        return cls(frontmatter=fm, body=body)

    def to_text(self) -> str:
        return join_card(self.frontmatter, self.body)


def default_schema_path() -> Path:
    """默认 schema 路径：`config/schema/kp_card.schema.json`（经 P2-02 路径层定位）。"""
    return get_paths().config_dir / "schema" / "kp_card.schema.json"


def load_schema(schema_path: Path | str | None = None) -> dict:
    sp = Path(schema_path) if schema_path else default_schema_path()
    return json.loads(sp.read_text(encoding="utf-8"))


def validate_card(card: KpCard, schema_path: Path | str | None = None) -> list[str]:
    """校验 frontmatter；返回错误列表（空 = 通过）。"""
    return validate(card.frontmatter, load_schema(schema_path))


def read_card(path: Path | str) -> KpCard:
    """读取知识点卡片。"""
    return KpCard.from_text(Path(path).read_text(encoding="utf-8"))


def write_card(path: Path | str, card: KpCard, schema_path: Path | str | None = None) -> Path:
    """写入知识点卡片；写前强制 schema 校验，不通过抛 `CardValidationError`。"""
    errors = validate_card(card, schema_path)
    if errors:
        raise CardValidationError(errors)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(card.to_text(), encoding="utf-8")
    return p
