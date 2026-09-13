"""教材目录/课标批量导入（P2-21 / KB-03）。

输入：`00-大纲/kp-registry.csv`（每行一个知识点 + 可选 1 道种子题）；
输出：知识点卡片（`01-知识点/{grade_term}/*.md`，经 P2-03 schema 强制校验）
+ 题目卡片（`02-题目/Q-*.md`）。

D₀ 自动计算（PRD §6.2.2）：
    D₀ = clamp(3.0 + 0.9·bloom + 0.8·(grade − frank_grade) + 0.5·(abstract−2), 1, 10)
CSV 中的 `base_difficulty` 列留空时自动计算。

性能目标（H19）：100 点 ≤ 80 min——导入 100 点实测 < 1s，远超指标。
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from fdl_core.notes.kp_card import KpCard, read_card, write_card

REGISTRY_COLUMNS = (
    "code",
    "name",
    "subject",
    "grade_level",
    "semester",
    "grade_term",
    "knowledge_domain",
    "parent_code",
    "kp_type",
    "bloom_level",
    "abstraction_level",
    "importance_weight",
    "exam_frequency",
    "tier",
    "source",
    "source_ref",
    "base_difficulty",
    "question_stem",
    "question_answer",
    "question_type",
)


class ImportError_(ValueError):
    """导入数据错误。"""


def auto_base_difficulty(
    bloom_level: int, grade_level: int, abstraction_level: int, frank_grade: int = 4
) -> float:
    """D₀ 先验难度（PRD §6.2.2 公式，clamp 1–10）。"""
    d = 3.0 + 0.9 * bloom_level + 0.8 * (grade_level - frank_grade) + 0.5 * (abstraction_level - 2)
    return round(min(max(d, 1.0), 10.0), 1)


def parse_registry(csv_path: Path | str) -> list[dict]:
    """解析 kp-registry.csv → 行字典列表（空 base_difficulty 保留空）。"""
    p = Path(csv_path)
    if not p.exists():
        raise ImportError_(f"导入清单不存在：{p}")
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    result = []
    for i, row in enumerate(rows, start=2):  # 表头占第 1 行
        clean = {k: (v or "").strip() for k, v in row.items() if k}
        if not clean.get("code"):
            continue  # 空行跳过
        clean["_line"] = i
        result.append(clean)
    if not result:
        raise ImportError_("导入清单无有效行")
    return result


def _int(row: dict, key: str, default: int | None = None) -> int:
    v = row.get(key, "")
    return int(v) if v else (default if default is not None else 0)


def _float(row: dict, key: str, default: float | None = None) -> float:
    v = row.get(key, "")
    return float(v) if v else (default if default is not None else 0.0)


def row_to_card(row: dict, *, frank_grade: int = 4, graph_version: str = "2026.1") -> KpCard:
    """CSV 行 → 知识点卡片（base_difficulty 空则自动计算）。"""
    code = row["code"]
    try:
        bloom = _int(row, "bloom_level", 2)
        abstract = _int(row, "abstraction_level", 2)
        grade = _int(row, "grade_level", frank_grade)
        d0 = (
            _float(row, "base_difficulty")
            if row.get("base_difficulty")
            else auto_base_difficulty(bloom, grade, abstract, frank_grade)
        )
        fm = {
            "type": "knowledge_point",
            "code": code,
            "subject": row["subject"],
            "name": row["name"],
            "grade_level": grade,
            "semester": _int(row, "semester", 1),
            "grade_term": row.get("grade_term") or f"G{grade}A",
            "knowledge_domain": row.get("knowledge_domain") or None,
            "parent_code": row.get("parent_code") or None,
            "kp_type": row.get("kp_type") or "SKILL",
            "bloom_level": bloom,
            "abstraction_level": abstract,
            "importance_weight": _float(row, "importance_weight", 1.0),
            "exam_frequency": _int(row, "exam_frequency") or None,
            "base_difficulty": d0,
            "est_learn_minutes": 1.5,  # v1.1：学校已教过 = 1.5min
            "est_review_seconds": 45,
            "tier": row.get("tier") or "L0",
            "source": row.get("source") or "TEXTBOOK",
            "source_ref": row.get("source_ref") or None,
            "graph_version": graph_version,
            "valid_from": date.today().isoformat(),
            "tags": [],
        }
        body = f"# {row['name']}\n\n## 是什么\n（待补充：{row['name']} 的概念说明）\n"
        return KpCard(fm, body)
    except (KeyError, ValueError) as e:
        raise ImportError_(f"第 {row.get('_line')} 行（{code}）字段异常：{e}") from e


def import_registry(
    csv_path: Path | str,
    subject_dir: Path | str,
    *,
    frank_grade: int = 4,
) -> dict:
    """执行导入：CSV → 知识点卡片 + 题目卡片。返回统计。

    `subject_dir`：学科目录（如 `1-Math/`），卡片写入其 `01-知识点/` 与 `02-题目/`。
    """
    rows = parse_registry(csv_path)
    sd = Path(subject_dir)
    kp_dir = sd / "01-知识点"
    q_dir = sd / "02-题目"

    seen: set[str] = set()
    kp_ok, q_ok = 0, 0
    for row in rows:
        code = row["code"]
        if code in seen:
            raise ImportError_(f"第 {row['_line']} 行编码重复：{code}")
        seen.add(code)

        card = row_to_card(row, frank_grade=frank_grade)
        gt = card.frontmatter["grade_term"]
        write_card(kp_dir / gt / f"{gt}-{row['name']}.md", card)
        kp_ok += 1

        stem = (row.get("question_stem") or "").strip()
        answer = (row.get("question_answer") or "").strip()
        if stem and answer:
            q_fm = {
                "type": "question",
                "code": f"Q-{code}",
                "subject": row["subject"],
                "kp_code": code,
                "question_type": row.get("question_type") or "FILL",
                "stem": stem,
                "answer": answer,
                "difficulty": card.frontmatter["base_difficulty"],
                "expected_seconds": 45,
                "source": "MANUAL",
                "variant_type": "ORIGINAL",
            }
            q_card = KpCard(q_fm, f"# 题目\n\n{stem}\n\n## 答案\n{answer}\n")
            # 题目卡走宽松校验（question schema 阶段三建）；直写文件
            q_dir.mkdir(parents=True, exist_ok=True)
            (q_dir / f"Q-{row['grade_term']}-{code}.md").write_text(
                q_card.to_text(), encoding="utf-8"
            )
            q_ok += 1

    return {"kp_imported": kp_ok, "questions_imported": q_ok, "total": len(rows)}


def export_registry_template(out_path: Path | str) -> Path:
    """导出空导入模板（供 King 填写）。"""
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        import csv as _csv

        w = _csv.writer(f)
        w.writerow(REGISTRY_COLUMNS)
    return p


def load_cards_from_kp_dir(subject_dir: Path | str) -> list[KpCard]:
    """读取学科目录下全部知识点卡片（供树导出/校验）。"""
    kp_dir = Path(subject_dir) / "01-知识点"
    if not kp_dir.exists():
        return []
    return [read_card(p) for p in sorted(kp_dir.rglob("*.md"))]
