"""学习星图数据构建（Step 3 · 2026-09-12）。

把"知识点 + 关联边"组装成页面可直接渲染的星图数据：

    三维筛选    学科（subject）→ 领域（knowledge_domain）→ 学期（grade_term）
    关联边      读 kp_relation；跨学科边打 cross 标记（页面高亮区分）
    确定性布局  学科分簇 → 领域子簇 → 簇内网格；坐标在 Python 侧预计算（稳定可复现）
    Obsidian    obsidian://open?vault=<vault>&file=<卡片相对路径>

为什么要预计算布局（而不是前端力导向）
--------------------------------------
- 位置稳定性：同一知识点每次渲染都在同一处（"上次那颗星还在那"）；
- 可测试：坐标进 payload，单测可直接断言；
- 无前端框架约束下的最简实现。

口径说明
--------
- `term`：奥数判命中 → "小A"，否则由 grade_level + semester 推导（复用 `db_sync`，
  与卡片导出同一套规则——单一事实源）；
- `m_adj`：kp_state.mastery_adj（LEFT JOIN，NULL = 未点亮）；
- 边：同一对节点至多一条（建表约束保证）；`cross` = 两端学科不同。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fdl_core.notes.db_sync import OLYMPIAD_TERM, _derive_grade_term, detect_olympiad
from fdl_core.notes.kp_card import read_card
from fdl_core.notes.kp_classifier import CANONICAL_SUBJECTS
from fdl_core.paths import get_paths

# ── 布局常量（模板 viewBox 由本模块输出的 width/height 决定）────
CELL_W, CELL_H = 72, 58  # 簇内网格单元
PAD, TITLE_H, GAP = 18, 26, 52  # 簇内边距 / 标题带 / 簇间距
MARGIN = 16  # 画布外边距
BIG_SUBJECT_MIN = 10  # 学科 KP 数 ≥ 此值 → 按领域拆子簇
LABEL_MAX = 6  # 节点标签展示字数（超出省略）
FLOW_MAX_W = 1500  # 簇排布最大行宽（超出换行）

# 学期排序键（页面也用同一顺序；"小A" 永远排最后）
_TERM_ORDER = {
    "G3A": 0,
    "G3B": 1,
    "G4A": 2,
    "G4B": 3,
    "G5A": 4,
    "G5B": 5,
    "G6A": 6,
    "G6B": 7,
    OLYMPIAD_TERM: 99,
}


def term_sort_key(term: str) -> int:
    return _TERM_ORDER.get(term, 50)


def _scan_card_paths(subject_dirs: dict[str, Path]) -> dict[str, Path]:
    """code → 卡片绝对路径（扫四科知识书目录；非卡片文件跳过）。"""
    idx: dict[str, Path] = {}
    for _code, base in subject_dirs.items():
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.md")):
            try:
                card = read_card(p)
            except Exception:  # noqa: BLE001 —— 非卡片/坏卡不阻塞星图
                continue
            if card.frontmatter.get("type") != "knowledge_point":
                continue
            c = card.frontmatter.get("code")
            if c and str(c) not in idx:
                idx[str(c)] = p
    return idx


def _obsidian_uri(vault_name: str, rel_path: str) -> str:
    """Obsidian URI（vault + file 双参数；按官方要求 %2F 编码斜杠）。"""
    return f"obsidian://open?vault={quote(vault_name, safe='')}&file={quote(rel_path, safe='')}"


def _cluster_label(node_subject: str, subj_name: str, domain: str, subject_size: int) -> str:
    """大科（≥ BIG_SUBJECT_MIN）按领域分簇；小科并为单簇。"""
    if subject_size >= BIG_SUBJECT_MIN:
        return f"{subj_name} · {domain}"
    return subj_name


def _layout(nodes: list[dict]) -> tuple[list[dict], int, int]:
    """确定性星座布局：学科分簇 → 领域子簇 → 簇内网格。

    返回 (clusters, width, height)；同时把 x/y 写回每个 node（就地）。
    """
    sizes: dict[str, int] = {}
    for n in nodes:
        sizes[n["subject"]] = sizes.get(n["subject"], 0) + 1

    groups: dict[tuple, list] = {}
    for n in nodes:
        label = _cluster_label(
            n["subject"], n["subject_name"], n["domain"], sizes.get(n["subject"], 0)
        )
        key = (n["subject_sort"], label)
        groups.setdefault(key, []).append(n)

    clusters: list[dict] = []
    for (_sort_order, label), members in sorted(
        groups.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        cols = max(1, int(len(members) ** 0.5 + 0.9999))
        rows = (len(members) + cols - 1) // cols
        clusters.append(
            {
                "label": label,
                "subject": members[0]["subject"],
                "count": len(members),
                "cols": cols,
                "rows": rows,
                "w": cols * CELL_W + 2 * PAD,
                "h": rows * CELL_H + 2 * PAD + TITLE_H,
                "members": members,
            }
        )

    # 流式排布（一行放不下换行）
    x = y = 0.0
    row_h = 0.0
    for c in clusters:
        if x + c["w"] > FLOW_MAX_W and x > 0:
            x = 0.0
            y += row_h + GAP
            row_h = 0.0
        c["x"], c["y"] = x, y
        row_h = max(row_h, c["h"])
        for i, n in enumerate(c["members"]):
            col, row = i % c["cols"], i // c["cols"]
            n["x"] = round(c["x"] + PAD + col * CELL_W + CELL_W / 2, 1)
            n["y"] = round(c["y"] + TITLE_H + PAD + row * CELL_H + 14, 1)
        x += c["w"] + GAP

    width = int(max((c["x"] + c["w"] for c in clusters), default=600)) + 2 * MARGIN
    by_row: dict[float, float] = {}
    for c in clusters:
        by_row[c["y"]] = max(by_row.get(c["y"], 0), c["h"])
    height = int(sum(by_row.values()) + GAP * max(0, len(by_row) - 1)) + 2 * MARGIN
    for c in clusters:
        c.pop("members", None)
    return clusters, width, height


def build_starmap(
    conn,
    *,
    subject_dirs: dict[str, Path] | None = None,
    root: Path | None = None,
    vault_name: str | None = None,
) -> dict:
    """构建星图 payload（nodes + edges + clusters + stats）。"""
    paths = get_paths()
    root = Path(root) if root else paths.root
    vault_name = vault_name or root.name
    if subject_dirs is None:
        subject_dirs = {code: paths.subject_dir(code) / "01-知识点" for code in CANONICAL_SUBJECTS}
    card_paths = _scan_card_paths(subject_dirs)

    # ── 学科元数据（配色/排序；以 DB 登记为准，canonical 兜底）──
    subj_meta: dict[str, dict] = {}
    for code, name, color, sort_order in conn.execute(
        "SELECT code, name, color_hex, sort_order FROM subject WHERE deleted_at IS NULL"
    ):
        subj_meta[code] = {"name": name, "color": color or "#5F5E5A", "sort_order": sort_order}
    for code, canon in CANONICAL_SUBJECTS.items():
        subj_meta.setdefault(
            code,
            {
                "name": canon["name"],
                "color": canon["color_hex"],
                "sort_order": canon["sort_order"],
            },
        )

    # ── 节点 ──
    nodes: list[dict] = []
    terms_seen: set[str] = set()
    for r in conn.execute(
        "SELECT k.id, k.code, k.name, k.subject_id, s.code AS subject,"
        "       k.knowledge_domain, k.grade_level, k.semester,"
        "       k.source_ref, k.description, st.mastery_adj"
        " FROM knowledge_point k"
        " JOIN subject s ON s.id = k.subject_id"
        " LEFT JOIN kp_state st ON st.kp_id = k.id AND st.user_id = 1"
        " ORDER BY s.sort_order, k.code"
    ):
        (
            kp_id,
            code,
            name,
            _sid,
            subject,
            domain,
            grade_level,
            semester,
            source_ref,
            description,
            m_adj,
        ) = r
        if detect_olympiad(
            conn, code=code, source_ref=source_ref, description=description, name=name
        ):
            term = OLYMPIAD_TERM
        else:
            term = _derive_grade_term(grade_level or 4, semester, source_ref or "")
        meta = subj_meta.get(subject, {"name": subject, "color": "#5F5E5A", "sort_order": 50})
        card_abs = card_paths.get(str(code))
        card_rel = None
        obsidian = None
        if card_abs is not None:
            try:
                card_rel = str(Path(card_abs).relative_to(root))
                obsidian = _obsidian_uri(vault_name, card_rel)
            except ValueError:
                card_rel = None  # 卡片不在 vault 根内 → 不给链接（防坏链）
        terms_seen.add(term)
        nodes.append(
            {
                "id": int(kp_id),
                "code": str(code),
                "name": str(name),
                "subject": subject,
                "subject_name": meta["name"],
                "subject_color": meta["color"],
                "subject_sort": meta["sort_order"],
                "domain": (domain or "未分类"),
                "term": term,
                "m_adj": round(float(m_adj), 2) if m_adj is not None else None,
                "lit": m_adj is not None,
                "card": card_rel,
                "obsidian": obsidian,
            }
        )

    # ── 边 ──
    code_by_id = {n["id"]: n["code"] for n in nodes}
    subj_by_id = {n["id"]: n["subject"] for n in nodes}
    edges: list[dict] = []
    for eid, fid, tid, etype, conf, source, note in conn.execute(
        "SELECT id, from_kp_id, to_kp_id, relation_type, confidence, source, note"
        " FROM kp_relation ORDER BY confidence DESC, id"
    ):
        fc, tc = code_by_id.get(fid), code_by_id.get(tid)
        if not fc or not tc:
            continue  # 端点不在当前节点集（防御；FK 已保证一般不会发生）
        edges.append(
            {
                "id": int(eid),
                "from": fc,
                "to": tc,
                "type": etype,
                "source": source,
                "confidence": round(float(conf), 2) if conf is not None else None,
                "note": note or "",
                "cross": subj_by_id.get(fid) != subj_by_id.get(tid),
            }
        )

    # ── 布局 ──
    clusters, width, height = _layout(nodes)

    # ── 统计 + 学科清单 ──
    connected = {e["from"] for e in edges} | {e["to"] for e in edges}
    by_subject: dict[str, int] = {}
    for n in nodes:
        by_subject[n["subject"]] = by_subject.get(n["subject"], 0) + 1
    subjects = [
        {
            "code": code,
            "name": meta["name"],
            "color": meta["color"],
            "count": by_subject.get(code, 0),
        }
        for code, meta in sorted(subj_meta.items(), key=lambda kv: kv[1]["sort_order"])
    ]
    stats = {
        "nodes": len(nodes),
        "edges": len(edges),
        "auto": sum(1 for e in edges if e["source"] == "LLM_AUTO"),
        "suggest": sum(1 for e in edges if e["source"] == "LLM_SUGGEST"),
        "cross": sum(1 for e in edges if e["cross"]),
        "isolated": sum(1 for n in nodes if n["code"] not in connected),
        "with_card": sum(1 for n in nodes if n["card"]),
        "with_obsidian": sum(1 for n in nodes if n["obsidian"]),
    }
    return {
        "nodes": nodes,
        "edges": edges,
        "clusters": clusters,
        "subjects": subjects,
        "terms": sorted(terms_seen, key=term_sort_key),
        "vault_name": vault_name,
        "width": width,
        "height": height,
        "stats": stats,
    }
