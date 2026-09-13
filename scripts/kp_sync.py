#!/usr/bin/env python3
"""知识书 ↔ DB 同步接线 CLI（Phase 1 · 多科地基）。

把三条链路串成一个入口（首次接线）：
    subject 学科登记  →  DB ↔ 卡片双向同步  →  (可选) LLM 分类

用法
----
    # 1) 查看当前接线状态（不写库、不调 LLM）—— 推荐先跑
    python scripts/kp_sync.py --status

    # 2) 登记四科基线（幂等；已存在则跳过）
    python scripts/kp_sync.py --ensure-subjects

    # 3) 反向导出：DB → 卡片（只补缺卡，不覆盖已有人工编辑）
    python scripts/kp_sync.py --export --subject MATH --dry-run   # 先预演
    python scripts/kp_sync.py --export --subject MATH

    # 4) 正向同步：卡片 → DB（UPDATE 语义，不丢 DB-only 字段）
    python scripts/kp_sync.py --import --subject MATH

    # 5) 全链路（subject → export → import → 校验）
    python scripts/kp_sync.py --all --subject MATH

    # 6) LLM 分类（Phase 2 用；本期预留接口）
    python scripts/kp_sync.py --classify --limit 10

    # 7) 指定库（副本验证）
    python scripts/kp_sync.py --db /tmp/x.db --status

设计原则
--------
- 默认**只读**：不带动作参数时等价于 --status。
- 破坏性动作（--export --force 覆盖）需显式传参，且默认只补缺卡。
- 所有动作幂等：重复执行不产生重复行/重复文件。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fdl_core.db.schema import get_connection  # noqa: E402
from fdl_core.notes import db_sync  # noqa: E402
from fdl_core.notes import kp_classifier as kc  # noqa: E402
from fdl_core.notes.kp_tree import scan_kp_cards  # noqa: E402
from fdl_core.paths import get_paths  # noqa: E402


def _subject_dir_of(subject_code: str) -> Path:
    """学科知识点卡目录（走 paths，不硬编码）。"""
    return get_paths().subject_dir(subject_code) / "01-知识点"


def cmd_status(conn, subject_code: str) -> None:
    """只读状态：subject 表 / DB KP / 卡片 / 缺卡 / kp_relation。"""
    print("── 接线状态 ──────────────────────────────────")
    print("【学科登记表】")
    for r in conn.execute(
        "SELECT id, code, name, color_hex, sort_order FROM subject"
        " WHERE deleted_at IS NULL ORDER BY sort_order"
    ):
        n = conn.execute(
            "SELECT COUNT(*) FROM knowledge_point WHERE subject_id=?", (r[0],)
        ).fetchone()[0]
        print(f"  #{r[0]} {r[1]:<10} {r[2]:<8} {r[3]:<9} sort={r[4]} KP数={n}")
    missing_canonical = [
        c
        for c in kc.CANONICAL_SUBJECTS
        if not conn.execute(
            "SELECT 1 FROM subject WHERE code=? AND deleted_at IS NULL", (c,)
        ).fetchone()
    ]
    print(f"  四科基线缺失: {missing_canonical or '无'}")

    print("\n【DB 侧知识点】")
    total = conn.execute("SELECT COUNT(*) FROM knowledge_point").fetchone()[0]
    print(f"  总数: {total}")
    for r in conn.execute(
        "SELECT s.code, COUNT(*) FROM knowledge_point k"
        " JOIN subject s ON s.id=k.subject_id GROUP BY s.code ORDER BY 2 DESC"
    ):
        print(f"    {r[0]}: {r[1]}")
    nd = conn.execute(
        "SELECT COUNT(*) FROM knowledge_point WHERE knowledge_domain IS NULL OR knowledge_domain=''"
    ).fetchone()[0]
    print(f"  未分类（domain 空）: {nd}")

    print("\n【卡片侧】")
    sd = _subject_dir_of(subject_code)
    print(f"  目录: {sd}")
    print(f"  存在: {sd.exists()}")
    if sd.exists():
        cards = scan_kp_cards(sd)
        card_codes = {c.frontmatter.get("code") for c in cards}
        db_codes = {
            r[0]
            for r in conn.execute(
                "SELECT code FROM knowledge_point"
                " WHERE subject_id=(SELECT id FROM subject WHERE code=?)",
                (subject_code,),
            )
        }
        print(f"  卡片数: {len(cards)}")
        print(f"  仅有 DB（缺卡）: {len(db_codes - card_codes)}")
        print(f"  仅有卡片（DB 缺）: {len(card_codes - db_codes)}")
        terms = {}
        for c in cards:
            t = c.frontmatter.get("grade_term") or "?"
            terms[t] = terms.get(t, 0) + 1
        print(f"  grade_term 分布: {terms}")

    print("\n【关联边】")
    nrel = conn.execute("SELECT COUNT(*) FROM kp_relation").fetchone()[0]
    npre = conn.execute("SELECT COUNT(*) FROM kp_prerequisite").fetchone()[0]
    print(f"  kp_relation（跨学科关联）: {nrel}")
    print(f"  kp_prerequisite（学习先决）: {npre}")
    print("──────────────────────────────────────────────")


def cmd_ensure_subjects(conn) -> None:
    r = kc.ensure_canonical_subjects(conn)
    print(f"[subjects] 新建: {r['created'] or '无'} | 已存在: {r['existing']}")


def cmd_export(conn, subject_code: str, *, dry_run: bool, force: bool) -> None:
    r = db_sync.export_kp_to_cards(conn, subject_code, only_missing=not force, dry_run=dry_run)
    tag = "预演" if dry_run else "完成"
    print(
        f"[export] {tag}：DB 内 {r['cards_total']} 条 | 导出 {r['exported']}"
        f" | 跳过(已有卡) {r['skipped']} | 错误 {len(r['errors'])}"
    )
    for e in r["errors"][:10]:
        print(f"    ERR {e}")


def cmd_import(conn, subject_code: str) -> None:
    r = db_sync.sync_kp_to_db(conn, _subject_dir_of(subject_code))
    print(f"[import] 卡片 {r['cards']} | 新增 {r['inserted']} | 更新 {r['updated']}")


def cmd_classify(conn, *, limit: int, dry_run: bool) -> None:
    if dry_run:
        rows = conn.execute(
            "SELECT id, code, name FROM knowledge_point"
            " WHERE knowledge_domain IS NULL OR knowledge_domain='' LIMIT ?",
            (limit,),
        ).fetchall()
        print(f"[classify] 预演：将分类 {len(rows)} 条")
        for r in rows:
            print(f"    #{r[0]} {r[1]} | {r[2]}")
        return
    r = kc.classify_kp_batch(conn, limit=limit, client=kc.get_default_client())
    print(
        f"[classify] 处理 {r['total']} | 已分类 {r['classified']}"
        f" | 新建学科 {r['subjects_created'] or '无'}"
        f" | 学科分布 {r['subjects_used']} | 转人工 {len(r['needs_review'])}"
    )
    for e in r.get("errors", [])[:5]:
        print(f"    ERR {e}")
    for nr in r["needs_review"][:5]:
        print(
            f"    REVIEW {nr.get('code')} conf={nr.get('confidence')}"
            f" {str(nr.get('rationale') or '')[:50]}"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="知识书 ↔ DB 同步接线")
    p.add_argument("--db", default=None, help="库路径（默认生产主库）")
    p.add_argument("--subject", default="MATH", help="学科 code（默认 MATH）")
    p.add_argument("--status", action="store_true", help="查看接线状态（默认动作）")
    p.add_argument("--ensure-subjects", action="store_true", help="登记四科基线")
    p.add_argument("--export", action="store_true", help="DB → 卡片（补缺卡）")
    p.add_argument("--import", dest="do_import", action="store_true", help="卡片 → DB")
    p.add_argument("--classify", action="store_true", help="LLM 分类（Phase 2 接口）")
    p.add_argument("--all", action="store_true", help="全链路：subjects→export→import")
    p.add_argument("--dry-run", action="store_true", help="只预演不写")
    p.add_argument("--force", action="store_true", help="export 时覆盖已有卡（危险）")
    p.add_argument("--limit", type=int, default=20, help="classify 处理上限")
    args = p.parse_args(argv)

    conn = get_connection(args.db or str(get_paths().primary_db_path))
    try:
        acted = False
        if args.ensure_subjects or args.all:
            cmd_ensure_subjects(conn)
            acted = True
        if args.export or args.all:
            cmd_export(conn, args.subject, dry_run=args.dry_run, force=args.force)
            acted = True
        if args.do_import or args.all:
            cmd_import(conn, args.subject)
            acted = True
        if args.classify:
            cmd_classify(conn, limit=args.limit, dry_run=args.dry_run)
            acted = True
        if args.status or not acted:
            if acted:
                print()
            cmd_status(conn, args.subject)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
