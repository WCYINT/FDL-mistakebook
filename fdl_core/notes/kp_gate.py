"""前置依赖与门禁（P2-05 / KB-04）。

PRD §3.2 KB-04：`HARD` 前置未达 `LEARNING` 的知识点**不得**进入新学候选；
支持 `SOFT`/`HARD` 两级；可检测环状依赖并报错。
门禁依据（PRD §3.3.3 T1）：首次学习要求前置全部 ≥LEARNING。

环检测用 Kahn 拓扑排序（迭代实现，无递归深度风险）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fdl_core.notes.kp_card import KpCard

HARD = "HARD"
SOFT = "SOFT"
_STRENGTHS = (HARD, SOFT)

# 8 态等级（PRD §3.3.3）；门禁阈值 = LEARNING（≥1 即"已学过"）
STATUS_RANK: dict[str, int] = {
    "UNLEARNED": 0,
    "LEARNING": 1,
    "STRUGGLING": 1,  # 学习中的卡点分支
    "REVIEWING": 2,
    "REGRESSED": 2,  # 曾掌握，回退后仍在循环内
    "MASTERED": 3,
    "CONSOLIDATED": 4,
    "ARCHIVED": 5,
}
GATE_THRESHOLD = STATUS_RANK["LEARNING"]


class PrereqCycleError(ValueError):
    """前置依赖成环。"""


class PrereqMissingError(ValueError):
    """前置指向不存在的知识点。"""


class PrereqStrengthError(ValueError):
    """前置强度非法（仅允许 HARD/SOFT）。"""


@dataclass(frozen=True)
class PrereqEdge:
    """一条依赖边：`kp_code` 依赖 `prereq_code`，强度 `strength`。"""

    kp_code: str
    prereq_code: str
    strength: str


@dataclass
class GateResult:
    """门禁判定结果。"""

    kp_code: str
    allowed: bool
    blockers: list[str] = field(default_factory=list)  # 未达标的 HARD 前置
    soft_hints: list[str] = field(default_factory=list)  # 未达标的 SOFT 前置（仅提示）


class PrereqGraph:
    """知识点前置依赖图 + 新学门禁。"""

    def __init__(self, edges: list[PrereqEdge]):
        self.edges = list(edges)
        self._by_kp: dict[str, list[PrereqEdge]] = {}
        for e in self.edges:
            self._by_kp.setdefault(e.kp_code, []).append(e)

    @classmethod
    def from_cards(cls, cards: list[KpCard], *, strict: bool = True) -> PrereqGraph:
        """从知识点卡片的 `prerequisites` frontmatter 构建依赖图。

        - `strict=True`：前置 code 指向不存在的知识点 → `PrereqMissingError`。
        - 成环 → `PrereqCycleError`；strength 非法 → `PrereqStrengthError`。
        """
        codes = {c.frontmatter["code"] for c in cards}
        edges: list[PrereqEdge] = []
        missing: list[tuple[str, str]] = []
        for c in cards:
            kp = c.frontmatter["code"]
            for p in c.frontmatter.get("prerequisites") or []:
                strength = p.get("strength")
                if strength not in _STRENGTHS:
                    raise PrereqStrengthError(
                        f"{kp} 的前置 {p.get('code')} 强度非法：{strength!r}（仅 HARD/SOFT）"
                    )
                edges.append(PrereqEdge(kp_code=kp, prereq_code=p["code"], strength=strength))
                if p["code"] not in codes:
                    missing.append((kp, p["code"]))
        if strict and missing:
            raise PrereqMissingError(f"前置指向不存在的知识点：{sorted(missing)}")
        graph = cls(edges)
        cycles = graph.detect_cycles()
        if cycles:
            raise PrereqCycleError(f"前置依赖成环：{cycles}")
        return graph

    def prereqs_of(self, kp_code: str) -> list[PrereqEdge]:
        """返回某知识点的全部前置边。"""
        return self._by_kp.get(kp_code, [])

    def detect_cycles(self) -> list[str]:
        """Kahn 拓扑（迭代）检测环。

        返回**环上 + 依赖环**的节点（无法拓扑排序者）——依赖环的节点门禁同样
        永远无法通过，一并报告便于数据修复。空列表 = 无环。
        """
        rev: dict[str, list[str]] = {}  # 反向边：prereq → 依赖它的 kp
        indeg: dict[str, int] = {}  # kp 的前置数
        for e in self.edges:
            rev.setdefault(e.prereq_code, []).append(e.kp_code)
            indeg[e.kp_code] = indeg.get(e.kp_code, 0) + 1
            indeg.setdefault(e.prereq_code, indeg.get(e.prereq_code, 0))
            rev.setdefault(e.kp_code, rev.get(e.kp_code, []))

        queue = [n for n, d in indeg.items() if d == 0]
        processed: set[str] = set()
        while queue:
            n = queue.pop()
            processed.add(n)
            for m in rev.get(n, []):
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
        return sorted(n for n in indeg if n not in processed)

    def check_gate(self, kp_code: str, states: dict[str, str]) -> GateResult:
        """新学门禁判定。

        `states`：`{kp_code: status}`（8 态），缺失的前置视为 `UNLEARNED`。
        HARD 前置 rank < LEARNING → 阻断；SOFT 前置未达标 → 仅提示。
        """
        blockers: list[str] = []
        soft_hints: list[str] = []
        for e in self.prereqs_of(kp_code):
            status = states.get(e.prereq_code, "UNLEARNED")
            if STATUS_RANK.get(status, 0) < GATE_THRESHOLD:
                if e.strength == HARD:
                    blockers.append(e.prereq_code)
                else:
                    soft_hints.append(e.prereq_code)
        return GateResult(
            kp_code=kp_code, allowed=not blockers, blockers=blockers, soft_hints=soft_hints
        )


def build_gate(cards: list[KpCard]) -> PrereqGraph:
    """便捷入口：从卡片构建依赖图（strict，含环/缺失/强度校验）。"""
    return PrereqGraph.from_cards(cards)
