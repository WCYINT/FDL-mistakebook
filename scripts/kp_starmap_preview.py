#!/usr/bin/env python3
"""学习星图 · 关联边预览与清单生成（Step 2 验证工具，2026-09-12）。

读 kp_relation + knowledge_point + subject，生成两个产物：
  1. site/kp_starmap_preview.html        —— 星图结构预览
     （分簇布局 / 自动边实线 / 待复核虚线 / 跨科边高亮 / hover 高亮关联）
  2. docs/research/学习星图-关联边清单.md —— 全部边的可读清单（按来源分组）

用法
----
    python scripts/kp_starmap_preview.py
    python scripts/kp_starmap_preview.py --db /tmp/x.db   # 副本验证

设计
----
- 分簇：MATH 按 knowledge_domain 分 4 簇；KP 数 < 10 的学科（如语文）并为单簇。
- 布局：确定性流式布局（簇内网格 + 簇间水平排列），位置稳定可复现；
- 视觉：实线=LLM_AUTO 自动生效，虚线=LLM_SUGGEST 待复核，
        橙色=跨学科边，空心点=无任何边的孤点。
"""

from __future__ import annotations

import argparse
import html
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.db.schema import get_connection  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402

# 布局常量
CELL_W, CELL_H = 66, 54
PAD, TITLE_H, GAP = 16, 24, 48
MARGIN_X, HEADER_H, FOOTER_H = 30, 128, 24
DOT_R = 4.6
TRUNC = 5

DOMAIN_ORDER = ("数与代数", "图形与几何", "统计与概率", "综合与实践")


def _load(conn):
    subj = {
        r[0]: {"name": r[1], "color": r[2]}
        for r in conn.execute("SELECT code, name, color_hex FROM subject WHERE deleted_at IS NULL")
    }
    nodes = [
        {
            "id": r[0],
            "code": r[1],
            "name": r[2],
            "domain": r[3] or "未分类",
            "subject": r[4],
            "color": subj.get(r[4], {}).get("color", "#0E7C7B"),
            "subj_name": subj.get(r[4], {}).get("name", r[4]),
        }
        for r in conn.execute(
            "SELECT k.id, k.code, k.name, k.knowledge_domain, s.code"
            " FROM knowledge_point k JOIN subject s ON s.id=k.subject_id"
            " ORDER BY s.sort_order, k.code"
        )
    ]
    edges = [
        {
            "id": r[0],
            "f": r[1],
            "t": r[2],
            "type": r[3],
            "conf": r[4],
            "source": r[5],
            "note": r[6] or "",
        }
        for r in conn.execute(
            "SELECT id, from_kp_id, to_kp_id, relation_type, confidence,"
            " source, note FROM kp_relation ORDER BY confidence DESC, id"
        )
    ]
    return nodes, edges


def _cluster_of(node, subject_sizes):
    """MATH 等大科按 domain 分簇；小科（<10 KP）并为单簇。"""
    if subject_sizes.get(node["subject"], 0) >= 10:
        return f"{node['subj_name']} · {node['domain']}"
    return f"{node['subj_name']}（{node['subject']}）"


def _layout(nodes):
    """→ (clusters, node_pos {id:(x,y)}, W, H)。确定性流式布局。"""
    sizes: dict[str, int] = {}
    for n in nodes:
        sizes[n["subject"]] = sizes.get(n["subject"], 0) + 1
    groups: dict[str, list] = {}
    for n in nodes:
        groups.setdefault(_cluster_of(n, sizes), []).append(n)

    def sort_key(label):
        d = label.split("· ")[-1]
        return (
            0 if "数学" in label else 1,
            DOMAIN_ORDER.index(d) if d in DOMAIN_ORDER else 99,
            label,
        )

    clusters = []
    for label in sorted(groups, key=sort_key):
        members = groups[label]
        cols = max(1, math.ceil(math.sqrt(len(members))))
        rows = math.ceil(len(members) / cols)
        clusters.append(
            {
                "label": label,
                "members": members,
                "cols": cols,
                "rows": rows,
                "w": cols * CELL_W + 2 * PAD,
                "h": rows * CELL_H + 2 * PAD + TITLE_H,
            }
        )

    # 流式排布（一行放不下可换行）
    max_row_w = 1408 - 2 * MARGIN_X
    x = y = 0.0
    row_h = 0.0
    node_pos: dict[int, tuple[float, float]] = {}
    for c in clusters:
        if x + c["w"] > max_row_w and x > 0:
            x = 0.0
            y += row_h + GAP
            row_h = 0.0
        c["x"], c["y"] = x, y
        row_h = max(row_h, c["h"])
        for i, n in enumerate(c["members"]):
            col, row = i % c["cols"], i // c["cols"]
            node_pos[n["id"]] = (
                c["x"] + PAD + col * CELL_W + CELL_W / 2,
                c["y"] + TITLE_H + PAD + row * CELL_H + 12,
            )
        x += c["w"] + GAP
    total_w = max((c["x"] + c["w"] for c in clusters), default=400) + 2 * MARGIN_X
    total_h = (
        HEADER_H
        + sum(
            max((c["h"] for c in clusters if abs(c["y"] - yy) < 1), default=0) + GAP
            for yy in sorted({c["y"] for c in clusters})
        )
        - GAP
        + FOOTER_H
        if clusters
        else 300
    )
    return clusters, node_pos, total_w, total_h


def _svg(nodes, edges, *, generated_at: str) -> str:
    clusters, pos, W, H = _layout(nodes)
    node_by_id = {n["id"]: n for n in nodes}
    connected = {e["f"] for e in edges} | {e["t"] for e in edges}
    parts: list[str] = []
    A = parts.append

    for c in clusters:
        A(
            f'<rect class="cluster" x="{c["x"]:.0f}" y="{c["y"]:.0f}"'
            f' width="{c["w"]:.0f}" height="{c["h"]:.0f}" rx="10"/>'
        )
        A(
            f'<text class="clabel" x="{c["x"] + PAD:.0f}"'
            f' y="{c["y"] + 16:.0f}">{html.escape(c["label"])}'
            f"（{len(c['members'])}）</text>"
        )

    # 边（先画，压在节点下）
    for e in edges:
        f, t = node_by_id.get(e["f"]), node_by_id.get(e["t"])
        if not f or not t:
            continue
        x1, y1 = pos[e["f"]]
        x2, y2 = pos[e["t"]]
        cross = f["subject"] != t["subject"]
        cls = "e cross" if cross else ("e auto" if e["source"] == "LLM_AUTO" else "e suggest")
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if abs(y2 - y1) < CELL_H * 1.5 and not cross:
            my -= 16  # 同簇近邻：微拱，避免重叠
        tip = (
            f"{f['name']}（{f['code']}）↔ {t['name']}（{t['code']}）\n"
            f"{e['type']} · {e['source']} · conf={e['conf']:.2f}\n{e['note']}"
        )
        A(
            f'<path class="{cls}" data-f="{e["f"]}" data-t="{e["t"]}"'
            f' d="M {x1:.0f} {y1:.0f} Q {mx:.0f} {my:.0f} {x2:.0f} {y2:.0f}">'
            f"<title>{html.escape(tip)}</title></path>"
        )

    # 节点
    for n in nodes:
        x, y = pos[n["id"]]
        hollow = n["id"] not in connected
        cnt = sum(1 for e in edges if e["f"] == n["id"] or e["t"] == n["id"])
        label = n["name"][:TRUNC] + ("…" if len(n["name"]) > TRUNC else "")
        tip = f"{n['name']}（{n['code']}）\n{n['subj_name']} · {n['domain']}\n关联 {cnt} 条"
        fill = "none" if hollow else n["color"]
        stroke = n["color"] if hollow else "none"
        A(
            f'<g class="n" data-id="{n["id"]}">'
            f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{DOT_R}"'
            f' fill="{fill}" stroke="{stroke}" stroke-width="1.4">'
            f"<title>{html.escape(tip)}</title></circle>"
            f'<text class="nlabel" x="{x:.0f}" y="{y + 19:.0f}">{html.escape(label)}</text>'
            f"</g>"
        )

    stats = _stats(nodes, edges)
    header = f'''<text class="h1" x="30" y="34">学习星图 · 关联边预览（Step 2 产物）</text>
<text class="h2" x="30" y="58">{stats["line1"]}</text>
<text class="h2" x="30" y="78">{stats["line2"]}</text>
<g transform="translate(30,96)">
  <line x1="0" y1="0" x2="34" y2="0" class="e auto"/>
  <text class="lg" x="40" y="4">自动生效（{stats["auto"]}）</text>
  <line x1="170" y1="0" x2="204" y2="0" class="e suggest"/>
  <text class="lg" x="210" y="4">待复核（{stats["suggest"]}）</text>
  <line x1="320" y1="0" x2="354" y2="0" class="e cross"/>
  <text class="lg" x="360" y="4">跨学科（{stats["cross"]}）</text>
  <circle cx="470" cy="0" r="4.6" fill="none" stroke="#5F5E5A" stroke-width="1.4"/>
  <text class="lg" x="480" y="4">孤立点（{stats["isolated"]}）</text>
</g>
<text class="gen" x="{W - 30:.0f}" y="{H - 10:.0f}" text-anchor="end">数据源 fdl.db · 生成于 {generated_at}</text>'''

    return (
        f'<svg id="map" viewBox="0 0 {W:.0f} {H:.0f}" width="100%"'
        f' xmlns="http://www.w3.org/2000/svg">'
        f'<g transform="translate(0,{HEADER_H - 100})">{header}</g>'
        f'<g transform="translate({MARGIN_X},{HEADER_H})">' + "".join(parts) + "</g></svg>"
    )


def _stats(nodes, edges):
    t = {}
    for e in edges:
        t[e["type"]] = t.get(e["type"], 0) + 1
    auto = sum(1 for e in edges if e["source"] == "LLM_AUTO")
    sugg = sum(1 for e in edges if e["source"] == "LLM_SUGGEST")
    node_by_id = {n["id"]: n for n in nodes}
    cross = sum(1 for e in edges if node_by_id[e["f"]]["subject"] != node_by_id[e["t"]]["subject"])
    connected = {e["f"] for e in edges} | {e["t"] for e in edges}
    iso = len(nodes) - len(connected)
    types = " · ".join(f"{k} {v}" for k, v in sorted(t.items(), key=lambda kv: -kv[1]))
    return {
        "auto": auto,
        "suggest": sugg,
        "cross": cross,
        "isolated": iso,
        "line1": f"{len(nodes)} 个知识点 · {len(edges)} 条关联 ｜ "
        f"自动生效 {auto} · 待复核 {sugg} · 跨学科 {cross} · 孤立点 {iso}",
        "line2": f"关系类型：{types}",
    }


def _html_doc(svg: str, title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; padding: 18px 22px; background: #FAFAF8; color: #2C2C2A;
         font-family: -apple-system, "PingFang SC", "Noto Sans SC", system-ui, sans-serif; }}
  #map {{ max-width: 1460px; }}
  .cluster {{ fill: #FFFFFF; stroke: #E4E2DC; stroke-width: 1; }}
  .clabel {{ font-size: 13px; font-weight: 600; fill: #5F5E5A; }}
  .h1 {{ font-size: 20px; font-weight: 600; fill: #2C2C2A; }}
  .h2 {{ font-size: 13px; fill: #5F5E5A; }}
  .lg {{ font-size: 12px; fill: #5F5E5A; }}
  .gen {{ font-size: 11px; fill: #9A988F; }}
  .nlabel {{ font-size: 10px; fill: #444441; text-anchor: middle; }}
  .n circle {{ cursor: pointer; }}
  .e.auto {{ fill: none; stroke: #0F6E56; stroke-width: 1.8; opacity: .60; }}
  .e.suggest {{ fill: none; stroke: #888780; stroke-width: 1; stroke-dasharray: 4 3; opacity: .42; }}
  .e.cross {{ fill: none; stroke: #BA7517; stroke-width: 2.2; opacity: .9; }}
  .e.hl {{ opacity: 1; stroke-width: 2.6; }}
  .n.dim {{ opacity: .25; }}
  .e.dim {{ opacity: .06; }}
</style>
</head>
<body>
{svg}
<script>
(function () {{
  var nodes = document.querySelectorAll('.n');
  var edges = document.querySelectorAll('.e');
  nodes.forEach(function (g) {{
    g.addEventListener('mouseenter', function () {{
      var id = g.getAttribute('data-id');
      edges.forEach(function (e) {{
        var hit = e.getAttribute('data-f') === id || e.getAttribute('data-t') === id;
        e.classList.toggle('hl', hit);
        e.classList.toggle('dim', !hit);
      }});
      nodes.forEach(function (o) {{
        o.classList.toggle('dim', o !== g);
      }});
    }});
    g.addEventListener('mouseleave', function () {{
      edges.forEach(function (e) {{ e.classList.remove('hl', 'dim'); }});
      nodes.forEach(function (o) {{ o.classList.remove('dim'); }});
    }});
  }});
}})();
</script>
</body>
</html>
"""


def _manifest(nodes, edges, *, generated_at: str) -> str:
    node_by_id = {n["id"]: n for n in nodes}
    st = _stats(nodes, edges)
    lines: list[str] = [
        "# 学习星图 · 关联边清单（Step 2 产物）",
        "",
        f"> 生成：{generated_at} ｜ 数据源：`fdl.db` → `kp_relation`",
        f"> {st['line1']}",
        f"> {st['line2']}",
        "",
    ]
    for title, src in (
        ("一、自动生效（LLM_AUTO）", "LLM_AUTO"),
        ("二、待人工复核（LLM_SUGGEST）", "LLM_SUGGEST"),
    ):
        rows = [e for e in edges if e["source"] == src]
        lines += [f"## {title}（{len(rows)} 条）", ""]
        if not rows:
            lines += ["（无）", ""]
            continue
        lines += [
            "| # | A（知识点） | B（知识点） | 类型 | 置信 | 依据 |",
            "|---|------------|------------|------|------|------|",
        ]
        for e in rows:
            f, t = node_by_id[e["f"]], node_by_id[e["t"]]
            mark = " 🌉" if f["subject"] != t["subject"] else ""
            lines.append(
                f"| {e['id']} | {f['name']}（`{f['code']}`） |"
                f" {t['name']}（`{t['code']}`）{mark} | {e['type']} |"
                f" {e['conf']:.2f} | {e['note'].replace('|', '/')} |"
            )
        lines.append("")
    connected = {e["f"] for e in edges} | {e["t"] for e in edges}
    iso = [n for n in nodes if n["id"] not in connected]
    lines += ["## 三、孤立点（无任何关联）", ""]
    lines += [f"- {n['name']}（`{n['code']}`）· {n['domain']}" for n in iso] or ["（无）"]
    lines += [
        "",
        "## 四、复核操作",
        "",
        "```bash",
        "python scripts/kp_relate.py --review          # 列出待复核边",
        "python scripts/kp_relate.py --confirm 3,5     # 确认为 HUMAN_VERIFIED",
        "python scripts/kp_relate.py --drop 7          # 删除误连的边",
        "```",
        "",
        "> 🌉 = 跨学科边。类型：SHARED_METHOD 同法 / ANALOGY 类比 /"
        " APPLICATION 应用 / CONTRAST 对比。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="学习星图预览与清单生成")
    p.add_argument("--db", default=None, help="库路径（默认生产主库）")
    p.add_argument("--out-html", default=None, help="HTML 输出路径")
    p.add_argument("--out-md", default=None, help="Markdown 输出路径")
    args = p.parse_args(argv)

    paths = get_paths()
    conn = get_connection(args.db or str(paths.primary_db_path))
    try:
        nodes, edges = _load(conn)
    finally:
        conn.close()
    if not nodes:
        print("[starmap] 无知识点，退出")
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    out_html = (
        Path(args.out_html)
        if args.out_html
        else paths.fdl_root / "site" / "kp_starmap_preview.html"
    )
    out_md = (
        Path(args.out_md)
        if args.out_md
        else paths.fdl_root / "docs" / "research" / "学习星图-关联边清单.md"
    )
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(
        _html_doc(_svg(nodes, edges, generated_at=stamp), "学习星图 · 关联边预览"),
        encoding="utf-8",
    )
    out_md.write_text(_manifest(nodes, edges, generated_at=stamp), encoding="utf-8")
    st = _stats(nodes, edges)
    print(
        f"[starmap] 节点 {len(nodes)} | 边 {len(edges)} | 自动 {st['auto']}"
        f" | 待复核 {st['suggest']} | 跨科 {st['cross']} | 孤点 {st['isolated']}"
    )
    print(f"  HTML → {out_html}")
    print(f"  MD   → {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
