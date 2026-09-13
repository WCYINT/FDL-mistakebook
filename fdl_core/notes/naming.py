"""关系图谱统一命名规范（2026-09-12 制定 · King 指令落地）。

规范一句话：**编码 + 描述**。
    节点 = `<编码> · <中文描述>`        例：`MATH-G4-MUL-COMM · 乘法交换律`
    边   = `<类型码中文> · <一句话依据>`  例：`同法 · 数表与图形找规律都靠"观察→归纳"`

本模块是规范的**唯一权威实现**——文件命名、展示标签、边标注都从这里取值，
禁止在别处再拼字符串。

一、编码规则（Code）
-------------------
知识点节点：`<SUBJECT>-<GRADE>-<TOPIC>[-<DETAIL>]`
    SUBJECT  学科段：MATH / CHN / ENG / SCI
             （与 subject 表 code 的映射见 `kp_classifier.SUBJECT_ALIASES`）
    GRADE    年级段：G1..G9
    TOPIC    主题词：大写英文，表意稳定（如 MUL 乘法 / TRI 三角形 / PATTERN 规律）
    DETAIL   可选细分段（如 COMM 交换律 / DIST 分配律）
    正则：`^[A-Z]+(-[A-Z0-9]+)+$`（与 config/schema/kp_card.schema.json 对齐）
关系边：无独立编码——由三元组 `(from_code, to_code, RELATION_TYPE)` 唯一确定；
    RELATION_TYPE ∈ {SHARED_METHOD, ANALOGY, APPLICATION, CONTRAST}，
    中文名固定在 `RELATION_TYPE_CN`，不得另创。

二、描述写法（Description）
--------------------------
节点描述：
    - 中文优先（母语可读是第一目标）；确无通用中文名的用英文；
    - ≤ 14 字；不含括号注释（注释性内容放 source_ref / 卡片正文）；
    - 不重复学科名（数学卡写"乘法交换律"，不写"数学乘法交换律"）。
边描述（note）：
    - 一句话依据，≤ 40 字，写清"为什么连"；
    - 含方向语义（谁是谁的基础/工具），避免只写"相关"。

三、组合与落地形态
------------------
    - 展示标签 `node_label()`   → `编码 · 描述`（中间点分隔，人读）
    - 文件命名 `card_filename()` → `编码-描述.md`（连字符，文件系统友好）
    - 边标签   `edge_label()`   → `类型中文 · 依据`
"""

from __future__ import annotations

import re

# ── 关系类型 → 中文名（与 kp_relate.RELATION_TYPES 同源语义，此处为展示层）──
RELATION_TYPE_CN: dict[str, str] = {
    "SHARED_METHOD": "同法",
    "ANALOGY": "类比",
    "APPLICATION": "应用",
    "CONTRAST": "对比",
}

# 节点编码正则（与卡片 schema 的 pattern 一致）
CODE_RE = re.compile(r"^[A-Z]+(-[A-Z0-9]+)+$")

_LABEL_SEP = " · "  # 展示分隔（中点）
_NAME_MAX = 14  # 描述长度上限（超出截断并加省略号）
_FILENAME_UNSAFE = re.compile(r"[\\/:*?\"<>|\n\r\t]")


def is_valid_code(code: str) -> bool:
    """编码是否合规（大写段 + 连字符，不含中文/空格）。"""
    return bool(CODE_RE.match(str(code or "").strip()))


def clean_name(name: str, *, max_len: int = _NAME_MAX) -> str:
    """描述规范化：去首尾空白 → 去掉括号注释 → 压缩空白 → 截断。

    括号注释不丢——调用方应把它写进 source_ref / 卡片正文。
    """
    s = str(name or "").strip()
    s = re.sub(r"[（(][^）)]*[）)]", "", s)  # 去中英文括号注释
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip() + "…"
    return s


def node_label(code: str, name: str) -> str:
    """节点统一标签：`编码 · 描述`（编码缺失时退回描述）。"""
    c = str(code or "").strip()
    n = clean_name(name)
    if c and n:
        return f"{c}{_LABEL_SEP}{n}"
    return c or n


def card_filename(code: str, name: str) -> str:
    """卡片文件名（不含目录）：`编码-描述.md`。

    - 描述为空的极端情况退回纯编码（保持可读，不产生 `--`）；
    - 剔除文件系统不安全字符（`/ : * ? " < > |`）。
    """
    c = _FILENAME_UNSAFE.sub("", str(code or "").strip())
    n = _FILENAME_UNSAFE.sub("", clean_name(name))
    stem = f"{c}-{n}" if (c and n) else (c or n or "unnamed")
    return f"{stem}.md"


def edge_label(relation_type: str, note: str | None = None) -> str:
    """边统一标签：`类型中文 · 依据`（依据缺失时只给类型）。"""
    cn = RELATION_TYPE_CN.get(str(relation_type or "").upper(), str(relation_type or ""))
    n = str(note or "").strip()
    return f"{cn}{_LABEL_SEP}{n}" if n else cn


def edge_label_full(
    from_code: str, from_name: str, to_code: str, to_name: str, relation_type: str
) -> str:
    """边完整展示（清单/复核用）：`A节点 —[类型]→ B节点`。"""
    cn = RELATION_TYPE_CN.get(str(relation_type or "").upper(), str(relation_type or ""))
    return f"{node_label(from_code, from_name)} —[{cn}]→ {node_label(to_code, to_name)}"
