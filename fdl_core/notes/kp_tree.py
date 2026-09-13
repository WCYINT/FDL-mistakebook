"""知识点树与学科大纲（P2-04 / KB-01）。

层级（≥3 层）：学科 → 知识域 → 知识点（`parent_code` 构建父子子树）。
`build_tree` 输出单学科的 `{知识域: [根 KpNode]}`；学科由调用上下文（目录）决定，
`write_kp_tree` 负责端到端：扫描学科目录 → 构树 → 导出 `00-大纲/kp-tree.md`。

复杂度：构树 O(n)、环检测 O(n+e)、渲染 O(n)，线性无性能压力。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fdl_core.notes.frontmatter import CardFormatError
from fdl_core.notes.kp_card import KpCard, read_card

# 四科展示名（PRD §6.2.1 subject.name，固定四科）
SUBJECT_NAMES = {
    "MATH": "数学",
    "CHINESE": "语文",
    "ENGLISH": "英语",
    "SCIENCE": "科学",
}


class KpTreeError(ValueError):
    """知识点树错误（parent_code 成环 / 编码重复）。"""


@dataclass
class KpNode:
    """树节点：知识点。"""

    code: str
    name: str
    grade_term: str
    children: list[KpNode] = field(default_factory=list)


def _detect_cycle(parent_of: dict[str, str | None]) -> None:
    """三色 DFS 检测 parent_code 环；有环抛 `KpTreeError`（含"成环"）。"""
    color: dict[str, int] = {}

    def dfs(code: str) -> None:
        color[code] = 1  # 访问中
        parent = parent_of.get(code)
        if parent is not None and parent in parent_of:
            pc = color.get(parent)
            if pc == 1:
                raise KpTreeError(f"知识点树存在成环：{code} → {parent}")
            if pc is None:
                dfs(parent)
        color[code] = 2  # 完成

    for code in parent_of:
        if color.get(code) is None:
            dfs(code)


def build_tree(cards: list[KpCard]) -> dict[str, list[KpNode]]:
    """构建知识点树：`{knowledge_domain: [根 KpNode]}`。

    - 重复 `code` → `KpTreeError`（含"重复"）。
    - `parent_code` 指向已存在节点 → 挂为子节点；指向不存在/空 → 作为该域根节点。
    - `parent_code` 成环 → `KpTreeError`（含"成环"）。
    """
    codes = [c.frontmatter["code"] for c in cards]
    seen: set[str] = set()
    dup: set[str] = set()
    for code in codes:
        if code in seen:
            dup.add(code)
        seen.add(code)
    if dup:
        raise KpTreeError(f"知识点编码重复：{sorted(dup)}")

    parent_of = {c.frontmatter["code"]: c.frontmatter.get("parent_code") for c in cards}
    _detect_cycle(parent_of)

    nodes = {
        c.frontmatter["code"]: KpNode(
            code=c.frontmatter["code"],
            name=c.frontmatter["name"],
            grade_term=c.frontmatter.get("grade_term", ""),
        )
        for c in cards
    }

    tree: dict[str, list[KpNode]] = {}
    for c in cards:
        fm = c.frontmatter
        domain = fm.get("knowledge_domain") or "未分类"
        code = fm["code"]
        parent = fm.get("parent_code")
        tree.setdefault(domain, [])
        if parent is not None and parent in nodes:
            nodes[parent].children.append(nodes[code])
        else:
            tree[domain].append(nodes[code])
    return tree


def count_nodes(nodes: list[KpNode]) -> int:
    """迭代统计子树节点数（避免深树递归超限）。"""
    total = 0
    stack = list(nodes)
    while stack:
        node = stack.pop()
        total += 1
        stack.extend(node.children)
    return total


def _render_node(lines: list[str], root: KpNode, depth: int) -> None:
    """迭代 DFS 前序渲染子树（父先子后，同层按 code 升序）。"""
    stack: list[tuple[KpNode, int]] = [(root, depth)]
    while stack:
        node, d = stack.pop()
        indent = "  " * d
        gt = f"[{node.grade_term}] " if node.grade_term else ""
        lines.append(f"{indent}- {gt}**{node.name}** `{node.code}`")
        for child in sorted(node.children, key=lambda n: n.code, reverse=True):
            stack.append((child, d + 1))


def export_tree_md(tree: dict[str, list[KpNode]], subject_code: str) -> str:
    """渲染知识点树为 Markdown（单学科）。"""
    total = sum(count_nodes(roots) for roots in tree.values())
    lines: list[str] = [f"# {subject_code} 知识点树", "", f"共 {total} 个知识点", ""]
    for domain in sorted(tree):
        roots = tree[domain]
        lines.append(f"## {domain}（{count_nodes(roots)}）")
        lines.append("")
        for root in sorted(roots, key=lambda n: n.code):
            _render_node(lines, root, 0)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def scan_kp_cards(dir_path: Path | str) -> list[KpCard]:
    """扫描目录下所有 `.md` 卡片；非卡片（无 frontmatter）跳过；目录不存在返回空。"""
    d = Path(dir_path)
    if not d.exists():
        return []
    cards: list[KpCard] = []
    for p in sorted(d.rglob("*.md")):
        try:
            card = read_card(p)
        except CardFormatError:
            continue  # 跳过非卡片文件（如 README/kp-tree.md）
        if card.frontmatter.get("type") != "knowledge_point":
            continue  # 跳过题目/错题等其他类型卡
        cards.append(card)
    return cards


def write_kp_tree(dir_path: Path | str) -> Path:
    """端到端：扫描学科目录 → 构树 → 导出 `00-大纲/kp-tree.md`，返回导出路径。"""
    d = Path(dir_path)
    cards = scan_kp_cards(d)
    subject = cards[0].frontmatter.get("subject", "MATH") if cards else "MATH"
    tree = build_tree(cards)
    out = d / "00-大纲" / "kp-tree.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(export_tree_md(tree, subject), encoding="utf-8")
    return out
